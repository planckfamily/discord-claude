import asyncio
import logging
import re
from dataclasses import dataclass
from pathlib import Path

import discord
from discord.ext import commands

from core.discord_streamer import DiscordStreamer

SEND_FILE_PATTERN = re.compile(r"\[send-file:\s*(.+?)\]")
ASK_USER_PATTERN = re.compile(r"\[ask-user:\s*(.+?)\]")
PLAY_AUDIO_PATTERN = re.compile(r"\[play-audio:\s*(.+?)\]", re.DOTALL)

log = logging.getLogger(__name__)


class AskUserButton(discord.ui.Button["AskUserView"]):
    def __init__(self, label: str, index: int, view_id: str) -> None:
        super().__init__(style=discord.ButtonStyle.primary, label=label, custom_id=f"ask_{view_id}_{index}")
        self._answer = label

    async def callback(self, interaction: discord.Interaction) -> None:
        view: AskUserView = self.view
        view.answer = self._answer
        view.event.set()
        for item in view.children:
            item.disabled = True
            if isinstance(item, AskUserButton):
                if item._answer == self._answer:
                    item.style = discord.ButtonStyle.success  # green
                else:
                    item.style = discord.ButtonStyle.secondary  # grey
        await interaction.response.edit_message(
            content=f"**Claude asked:** {view.question_text}",
            view=view,
        )
        view.stop()


class AskUserView(discord.ui.View):
    def __init__(self, question_text: str, options: list[str]) -> None:
        import uuid
        super().__init__(timeout=300)
        self.question_text = question_text
        self.answer: str | None = None
        self.event = asyncio.Event()
        view_id = uuid.uuid4().hex[:8]
        for i, opt in enumerate(options[:5]):
            self.add_item(AskUserButton(opt.strip(), i, view_id))

    async def on_timeout(self) -> None:
        self.answer = None
        self.event.set()


BUGS_AND_FIXES = "Bugs & Fixes"


class FeatureGateSelect(discord.ui.Select):
    """Dropdown shown when a prompt arrives with no active feature."""

    def __init__(self, features: list, project_dir, bot):
        options = []
        # Always offer Bugs & Fixes first
        bugs_exists = any(f.name == BUGS_AND_FIXES for f in features)
        bugs_desc = "Resume" if bugs_exists else "Create"
        options.append(discord.SelectOption(
            label=BUGS_AND_FIXES, value=BUGS_AND_FIXES,
            description=f"{bugs_desc} — catch-all for misc work",
        ))
        # Existing features (skip Bugs & Fixes since it's already listed)
        for f in features[:23]:
            if f.name == BUGS_AND_FIXES:
                continue
            desc = f"{f.status}"
            if f.subdir:
                desc += f" · {f.subdir}/"
            options.append(discord.SelectOption(label=f.name, value=f.name, description=desc))
        # Option to create a new feature
        options.append(discord.SelectOption(
            label="Create new feature...", value="__new__",
            description="Type a name for a new feature",
        ))
        super().__init__(placeholder="Select a feature before continuing...", options=options)
        self.project_dir = project_dir
        self.bot = bot
        self.selected_feature = None
        self.event = asyncio.Event()

    async def callback(self, interaction: discord.Interaction):
        choice = self.values[0]
        fm = self.bot.feature_manager

        if choice == "__new__":
            # Send a modal to collect the feature name
            modal = NewFeatureModal(self)
            await interaction.response.send_modal(modal)
            return

        # Resume or create Bugs & Fixes / existing feature
        existing = fm.list_features(self.project_dir)
        exists = any(f.name == choice for f in existing)

        if exists:
            self.selected_feature = fm.resume_feature(self.project_dir, choice)
        else:
            self.selected_feature = fm.start_feature(self.project_dir, choice)

        scope = f" in `{self.selected_feature.subdir}/`" if self.selected_feature.subdir else ""
        action = "Resumed" if exists else "Started"
        await interaction.response.edit_message(
            content=f"{action} feature **`{self.selected_feature.name}`**{scope}. Processing prompt...",
            view=None,
        )
        self.event.set()


class NewFeatureModal(discord.ui.Modal, title="New Feature"):
    name_input = discord.ui.TextInput(label="Feature name", placeholder="e.g. add-auth-system", max_length=80)

    def __init__(self, gate_select: FeatureGateSelect):
        super().__init__()
        self.gate_select = gate_select

    async def on_submit(self, interaction: discord.Interaction):
        name = self.name_input.value.strip()
        if not name:
            await interaction.response.send_message("Feature name cannot be empty.", ephemeral=True)
            return
        fm = self.gate_select.bot.feature_manager
        self.gate_select.selected_feature = fm.start_feature(self.gate_select.project_dir, name)
        await interaction.response.edit_message(
            content=f"Started feature **`{name}`**. Processing prompt...",
            view=None,
        )
        self.gate_select.event.set()


class FeatureGateView(discord.ui.View):
    def __init__(self, features: list, project_dir, bot):
        super().__init__(timeout=120)
        self.select = FeatureGateSelect(features, project_dir, bot)
        self.add_item(self.select)

    async def on_timeout(self):
        self.select.event.set()


@dataclass
class QueuedPrompt:
    message: discord.Message
    prompt: str
    project: object  # Project dataclass


class ClaudePromptCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        # thread_id -> asyncio.Queue of QueuedPrompt
        self._queues: dict[int, asyncio.Queue[QueuedPrompt]] = {}
        # thread_id -> worker task
        self._workers: dict[int, asyncio.Task] = {}

    def _strip_mention(self, content: str) -> str:
        """Remove bot mention from message content."""
        prompt = content
        for mention_str in [f"<@{self.bot.user.id}>", f"<@!{self.bot.user.id}>"]:
            prompt = prompt.replace(mention_str, "")
        return prompt.strip()

    def _build_project_context(self) -> str:
        """Build a context string listing all known projects and their thread IDs."""
        pm = self.bot.project_manager
        projects = pm.projects
        if not projects:
            return ""

        lines = ["\n\nThe following projects are available, each with a dedicated Discord thread:"]
        for name, project in sorted(projects.items()):
            feature = self.bot.feature_manager.get_current_feature(pm.get_project_dir(project))
            feat_str = f" (active feature: {feature.name})" if feature else ""
            lines.append(f"- {name}: thread <#{project.thread_id}>{feat_str}")
        return "\n".join(lines)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        # Ignore own messages
        if message.author == self.bot.user:
            return

        # Must mention the bot
        if self.bot.user not in message.mentions:
            return

        # Warn if a restart is pending, but still accept the prompt (it will delay the restart)
        if self.bot._restart_requested:
            await message.channel.send(
                "⚠️ A restart is scheduled — your prompt will delay it until this run completes."
            )

        # Strip the bot mention from the prompt
        prompt = self._strip_mention(message.content)

        if not prompt:
            await message.channel.send("Send a prompt after mentioning me.")
            return

        # --- Main channel @mention (not in a thread) ---
        if not isinstance(message.channel, discord.Thread):
            # Only respond in the configured channel
            if message.channel.id != self.bot.project_manager.channel_id:
                return

            # Augment prompt with project context so Claude can suggest the right thread
            project_context = self._build_project_context()
            augmented_prompt = prompt + project_context

            channel_id = message.channel.id
            queued = QueuedPrompt(message=message, prompt=augmented_prompt, project=None)

            if channel_id not in self._queues:
                self._queues[channel_id] = asyncio.Queue()

            queue = self._queues[channel_id]

            if channel_id in self._workers and not self._workers[channel_id].done():
                await queue.put(queued)
                position = queue.qsize()
                await message.add_reaction("\U0001f4cb")
                preview = prompt[:200] + ("…" if len(prompt) > 200 else "")
                await message.channel.send(f"*Queued (position {position}):* `{preview}`")
                return

            await queue.put(queued)
            self._workers[channel_id] = asyncio.create_task(self._worker(channel_id))
            return

        # --- Thread @mention ---
        # Resolve project from thread
        project = self.bot.project_manager.get_project_by_thread(message.channel.id)
        if not project:
            return

        thread_id = message.channel.id
        queued = QueuedPrompt(message=message, prompt=prompt, project=project)

        # Get or create queue for this thread
        if thread_id not in self._queues:
            self._queues[thread_id] = asyncio.Queue()

        queue = self._queues[thread_id]

        # If a worker is already running, queue the prompt
        if thread_id in self._workers and not self._workers[thread_id].done():
            await queue.put(queued)
            position = queue.qsize()
            await message.add_reaction("\U0001f4cb")  # clipboard emoji = queued
            preview = prompt[:200] + ("…" if len(prompt) > 200 else "")
            await message.channel.send(f"*Queued (position {position}):* `{preview}`")
            return

        # No worker running — put it in the queue and start the worker
        await queue.put(queued)
        self._workers[thread_id] = asyncio.create_task(self._worker(thread_id))

    async def _collect_answer(self, channel, raw_question: str) -> str:
        """Parse a question string and show a Discord widget to collect the answer."""
        parts = [p.strip() for p in raw_question.split("|")]
        question_text = parts[0]
        options = parts[1:] if len(parts) > 1 else []

        if options:
            view = AskUserView(question_text, options)
            await channel.send(f"**Claude is asking:** {question_text}", view=view)
            try:
                await asyncio.wait_for(view.event.wait(), timeout=300)
            except asyncio.TimeoutError:
                pass
            return view.answer or "No response (timed out)"
        else:
            await channel.send(
                f"**Claude is asking:** {question_text}\n*Reply in this channel to answer.*"
            )
            try:
                reply = await self.bot.wait_for(
                    "message",
                    check=lambda m: m.channel == channel and not m.author.bot,
                    timeout=300,
                )
                return reply.content
            except asyncio.TimeoutError:
                return "No response (timed out)"

    async def _worker(self, thread_id: int) -> None:
        """Process queued prompts for a thread, one at a time."""
        queue = self._queues[thread_id]
        try:
            while not queue.empty():
                item = await queue.get()
                try:
                    await self._process_prompt(item)
                except Exception as e:
                    log.exception("Error processing queued prompt")
                    try:
                        await item.message.channel.send(f"**Error:** {e}")
                    except discord.HTTPException:
                        pass
        finally:
            # Clean up if the queue is empty
            if queue.empty():
                self._queues.pop(thread_id, None)
                self._workers.pop(thread_id, None)
            # Notify the bot that a worker finished
            await self.bot.notify_worker_done()

    async def _run_stream(self, *, channel, runner, prompt, project_dir, run_dir, thread_id,
                          session_id, resume, feature, persona_content="",
                          myvillage_project_id="", model=None,
                          guild=None, project_name=None) -> tuple[str | None, str | None, str]:
        """Run Claude and stream to Discord. Returns (last_session_id, pending_question, response_text).

        project_dir: project root — used for state, token tracking, file security
        run_dir: Claude's working directory — may be a subdirectory when a feature targets one
        model: optional model override (e.g. 'claude-opus-4-6')
        guild/project_name: used for autonomous voice notifications
        """
        # Start streaming with a cancel button
        cancel_fn = lambda: runner.cancel(thread_id)
        streamer = DiscordStreamer(channel, on_cancel=cancel_fn)
        await streamer.start()

        # Create a background task to periodically flush the buffer
        async def tick_loop(s=streamer):
            while not s._finalized:
                await asyncio.sleep(0.3)
                await s.tick()

        tick_task = asyncio.create_task(tick_loop())
        full_response = []
        last_session_id = session_id

        import datetime as _dt
        _started_at = _dt.datetime.now(_dt.timezone.utc)

        try:
            async for event in runner.run(
                prompt=prompt,
                project_dir=run_dir,
                thread_id=thread_id,
                session_id=session_id,
                resume=resume,
                persona_content=persona_content,
                model=model,
            ):
                if event.type == "text":
                    print(event.content, end="", flush=True)
                    full_response.append(event.content)
                    await streamer.feed(event.content)
                elif event.type == "cancelled":
                    print("\n[Cancelled]", flush=True)
                    await streamer.send_cancelled()
                    return last_session_id, None, ""
                elif event.type == "error":
                    print(f"\n[Error] {event.content}", flush=True)
                    await streamer.send_error(event.content)
                    if guild:
                        asyncio.create_task(self.bot.voice_notifier.voice_event(
                            guild, "error",
                            f"Claude hit an error in {project_name}." if project_name else "Claude hit an error."
                        ))
                    return last_session_id, None, ""
                elif event.type == "result":
                    if event.session_id:
                        last_session_id = event.session_id
                    print(flush=True)  # final newline after streaming
                    # Persist session_id and model
                    if event.session_id or event.model:
                        from core.state import load_project_state, save_project_state, load_feature_state, save_feature_state

                        if event.session_id and feature:
                            feat_state = load_feature_state(project_dir)
                            if feature.name in feat_state.get("features", {}):
                                feat_state["features"][feature.name]["session_id"] = event.session_id
                                save_feature_state(project_dir, feat_state)

                        # Save project-level defaults (bot state)
                        state = load_project_state(project_dir)
                        if event.session_id:
                            state["default_session_id"] = event.session_id
                        if event.model:
                            state["model"] = event.model
                        save_project_state(project_dir, state)

                    # Show context health and cost footer
                    if event.input_tokens is not None:
                        context_fill = event.input_tokens
                        context_window = event.context_window or 200_000
                        # CLI may report 200k but newer models support 1M
                        if event.model and ("opus" in event.model or "sonnet" in event.model):
                            context_window = max(context_window, 1_000_000)
                        context_pct = context_fill / context_window * 100

                        if context_pct >= 85:
                            indicator = "\U0001f534"  # red
                            warning = "\n**\u26a0\ufe0f Context window critically full — wrap up this feature now!**"
                            if guild:
                                asyncio.create_task(self.bot.voice_notifier.voice_event(
                                    guild, "context_critical",
                                    f"Context window critically full for {project_name}. Wrap up soon." if project_name
                                    else "Context window critically full. Wrap up soon."
                                ))
                        elif context_pct >= 70:
                            indicator = "\U0001f7e0"  # orange
                            warning = "\n**\u26a0\ufe0f Context window getting large — consider finishing soon.**"
                        elif context_pct >= 50:
                            indicator = "\U0001f7e1"  # yellow
                            warning = "\n*Context window over 50% — keep an eye on it.*"
                        else:
                            indicator = "\U0001f7e2"  # green
                            warning = ""

                        cost_str = f" · ${event.cost_usd:.4f}" if event.cost_usd else ""
                        model_str = f" · `{event.model}`" if event.model else ""
                        session_id_str = f" · session `{last_session_id[:8]}`" if last_session_id else ""
                        context_line = f"*{indicator} {context_fill:,} / {context_window:,} tokens ({context_pct:.1f}%){cost_str}{model_str}{session_id_str}*"

                        # Accumulate cost for the session/feature
                        totals = self.bot.feature_manager.accumulate_tokens(
                            project_dir,
                            input_tokens=context_fill,
                            output_tokens=event.output_tokens or 0,
                            cost_usd=event.cost_usd or 0.0,
                            feature_name=feature.name if feature else None,
                        )

                        # Report cost to myvillage (fire-and-forget, non-blocking)
                        if event.cost_usd:
                            import asyncio as _asyncio
                            from core.bridgecrew_client import report_cost as _report_cost
                            _feature_mv_id = getattr(feature, "myvillage_id", "") if feature else ""
                            _asyncio.get_event_loop().run_in_executor(
                                None,
                                lambda: _report_cost(
                                    project_id=myvillage_project_id,
                                    session_id=last_session_id or "",
                                    model=event.model or "",
                                    cost_usd=event.cost_usd,
                                    input_tokens=prompt_in,
                                    output_tokens=prompt_out,
                                    feature_id=_feature_mv_id,
                                    started_at=_started_at,
                                    completed_at=_dt.datetime.now(_dt.timezone.utc),
                                ),
                            )
                        session_label = f"feature `{feature.name}`" if feature else "session"
                        session_line = ""
                        if totals["total_cost_usd"]:
                            session_line = f"\n*{session_label}: ${totals['total_cost_usd']:.4f} across {totals['prompt_count']} prompt(s)*"

                        await streamer.feed(
                            f"\n\n---\n"
                            f"{context_line}"
                            f"{session_line}"
                            f"{warning}"
                        )

            await streamer.finalize()

            # Autonomous run-complete notification
            if guild:
                asyncio.create_task(self.bot.voice_notifier.voice_event(
                    guild, "run_complete",
                    f"Claude has finished in {project_name}." if project_name else "Claude has finished."
                ))

            # Extract markers from the full response text and clean them from Discord messages
            response_text = "".join(full_response)
            pending_files = SEND_FILE_PATTERN.findall(response_text)
            pending_audio = PLAY_AUDIO_PATTERN.findall(response_text)
            pending_question = None
            ask_match = ASK_USER_PATTERN.search(response_text)
            if ask_match:
                pending_question = ask_match.group(1)

            # Strip markers from the Discord messages if any were found
            if pending_files or pending_question or pending_audio:
                for msg in streamer.all_messages:
                    try:
                        if msg.content:
                            cleaned = SEND_FILE_PATTERN.sub("", msg.content)
                            cleaned = ASK_USER_PATTERN.sub("", cleaned)
                            cleaned = PLAY_AUDIO_PATTERN.sub("", cleaned)
                            if cleaned != msg.content:
                                await msg.edit(content=cleaned.strip() or "\u200b")
                    except discord.HTTPException:
                        pass

            # Fire [play-audio:] prompts as background tasks
            for audio_prompt in pending_audio:
                if guild:
                    asyncio.create_task(
                        self.bot.voice_notifier.play_prompt(guild, audio_prompt.strip())
                    )

            # Attach any files that were referenced
            for rel_path in pending_files:
                rel_path = rel_path.strip()
                file_path = (run_dir / rel_path).resolve()
                # Security: must be within project directory
                try:
                    file_path.relative_to(project_dir.resolve())
                except ValueError:
                    await channel.send(f"Skipped `{rel_path}` — outside project directory.")
                    continue
                if not file_path.exists() or not file_path.is_file():
                    await channel.send(f"Skipped `{rel_path}` — file not found.")
                    continue
                size_mb = file_path.stat().st_size / (1024 * 1024)
                if size_mb > 25:
                    await channel.send(f"Skipped `{rel_path}` — too large ({size_mb:.1f}MB).")
                    continue
                try:
                    await channel.send(f"📎 `{rel_path}`", file=discord.File(str(file_path)))
                except discord.HTTPException as e:
                    await channel.send(f"Failed to send `{rel_path}`: {e}")

            return last_session_id, pending_question, response_text

        except Exception as e:
            log.exception("Error during Claude stream")
            await streamer.send_error(str(e))
            return last_session_id, None, ""
        finally:
            # Always clean up the stop button
            if not streamer._finalized:
                await streamer.finalize()
            tick_task.cancel()
            try:
                await tick_task
            except asyncio.CancelledError:
                pass

    async def run_feature_summary_prompt(self, channel, project, feature) -> None:
        """Send a prompt to Claude to create a feature doc and update CLAUDE.md.
        Called after a feature is marked complete. Always runs from the project root
        so that CLAUDE.md resolves correctly regardless of subdir.
        """
        project_dir = self.bot.project_manager.get_project_dir(project)

        # Feature doc lives under the subdir's own features/ folder, or the project root's
        if feature.subdir:
            feature_doc_path = f"{feature.subdir}/features/{feature.name}.md"
        else:
            feature_doc_path = f"features/{feature.name}.md"

        prompt = (
            f"The feature **`{feature.name}`** has just been marked as completed. "
            f"Please do the following two tasks:\n\n"
            f"1. Create `{feature_doc_path}` (relative to the project root) summarising this feature. Include:\n"
            f"   - What the feature does / what problem it solves\n"
            f"   - Key files changed or created\n"
            f"   - Any important design decisions or tradeoffs\n"
            f"   - Known limitations or follow-up items (if any)\n\n"
            f"2. Open `CLAUDE.md` at the project root and add or update a `## Features` section "
            f"that lists every feature with a one-sentence description and a reference to its doc file. "
            f"Format each entry as a bullet:\n"
            f"   - **{feature.name}**: One sentence description. See `{feature_doc_path}`.\n\n"
            f"Do NOT use the @ symbol when referencing files in CLAUDE.md — write plain paths only. "
            f"Preserve all existing content in CLAUDE.md outside the Features section. "
            f"Keep both files concise."
        )
        await self._run_stream(
            channel=channel,
            runner=self.bot.claude_runner,
            prompt=prompt,
            project_dir=project_dir,
            run_dir=project_dir,
            thread_id=channel.id,
            session_id=feature.session_id,
            resume=True,
            feature=feature,
            # No activity reporting for internal summary prompts
        )

    async def _process_prompt(self, item: QueuedPrompt) -> None:
        message = item.message
        prompt = item.prompt
        project = item.project
        runner = self.bot.claude_runner

        # Main channel queries run against the bot's own project directory
        if project is None:
            project_dir = Path(__file__).resolve().parent.parent
        else:
            project_dir = self.bot.project_manager.get_project_dir(project)

        # Get session_id: prefer active feature's session, fall back to project default
        # Also read the preferred model for this project
        feature = self.bot.feature_manager.get_current_feature(project_dir) if project else None

        # Gate: require a feature selection for project threads
        if project and not feature:
            features = self.bot.feature_manager.list_features(project_dir)
            view = FeatureGateView(features, project_dir, self.bot)
            gate_msg = await message.channel.send(
                "No active feature — pick one before I start working:",
                view=view,
            )
            await view.select.event.wait()
            feature = view.select.selected_feature
            if not feature:
                await gate_msg.edit(content="*Timed out — prompt cancelled.*", view=None)
                return

        session_id = feature.session_id if feature else None
        from core.state import load_project_state
        state = load_project_state(project_dir)
        if not session_id:
            session_id = state.get("default_session_id")
        resume = bool(session_id)
        preferred_model = state.get("preferred_model")

        # Fetch persona content and myvillage project ID
        myvillage_project_id = state.get("myvillage_project_id", "")
        from core.bridgecrew_client import get_project_prompt as _get_prompt, report_activity as _report_activity
        persona_content = _get_prompt(myvillage_project_id) if myvillage_project_id else ""

        # Report the user's message to the activity feed (fire-and-forget)
        if myvillage_project_id:
            loop = asyncio.get_event_loop()
            loop.run_in_executor(
                None,
                lambda: _report_activity(
                    project_id=myvillage_project_id,
                    role="user",
                    author=str(message.author),
                    content=prompt,
                    feature_name=feature.name if feature else None,
                ),
            )

        # If the active feature targets a subdirectory, use that as Claude's working dir
        run_dir = project_dir
        if feature and feature.subdir:
            candidate = project_dir / feature.subdir
            if candidate.is_dir():
                run_dir = candidate

        # Snapshot full diff state so we can detect any changes (committed or not)
        is_self = self.bot.is_self_project(project_dir)
        diff_snapshot = None
        if is_self:
            try:
                diff_proc, head_proc = await asyncio.gather(
                    asyncio.create_subprocess_exec(
                        "git", "diff", "HEAD",
                        cwd=str(project_dir),
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.DEVNULL,
                    ),
                    asyncio.create_subprocess_exec(
                        "git", "rev-parse", "HEAD",
                        cwd=str(project_dir),
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.DEVNULL,
                    ),
                )
                diff_out, _ = await asyncio.wait_for(diff_proc.communicate(), timeout=5)
                head_out, _ = await asyncio.wait_for(head_proc.communicate(), timeout=5)
                diff_snapshot = (head_out.decode().strip(), diff_out.decode())
            except Exception:
                pass

        print(f"\n{'='*60}\n[{message.author}] {prompt}\n{'='*60}", flush=True)

        guild = message.guild
        project_name = project.name if project else None

        # Run the initial prompt
        last_session_id, pending_question, response_text = await self._run_stream(
            channel=message.channel,
            runner=runner,
            prompt=prompt,
            project_dir=project_dir,
            run_dir=run_dir,
            thread_id=message.channel.id,
            session_id=session_id,
            resume=resume,
            feature=feature,
            persona_content=persona_content,
            myvillage_project_id=myvillage_project_id,
            model=preferred_model,
            guild=guild,
            project_name=project_name,
        )

        # Report Claude's response to the activity feed (fire-and-forget)
        if myvillage_project_id and response_text:
            loop = asyncio.get_event_loop()
            loop.run_in_executor(
                None,
                lambda: _report_activity(
                    project_id=myvillage_project_id,
                    role="assistant",
                    author="Claude",
                    content=response_text,
                    feature_name=feature.name if feature else None,
                ),
            )

        # Question loop: collect answer, continue session, repeat if needed
        while pending_question and last_session_id:
            answer = await self._collect_answer(message.channel, pending_question)
            print(f"[Answer] {answer}", flush=True)

            last_session_id, pending_question, _ = await self._run_stream(
                channel=message.channel,
                runner=runner,
                prompt=answer,
                project_dir=project_dir,
                run_dir=run_dir,
                thread_id=message.channel.id,
                session_id=last_session_id,
                resume=True,
                feature=feature,
                persona_content=persona_content,
                myvillage_project_id=myvillage_project_id,
                model=preferred_model,
                guild=guild,
                project_name=project_name,
            )

        # Log to history
        self.bot.feature_manager.add_history(
            project_dir,
            user=str(message.author),
            prompt_summary=prompt,
            feature_name=feature.name if feature else None,
        )

        # Auto-restart if the bot's own code was actually modified
        if is_self and diff_snapshot is not None:
            try:
                old_head, old_diff = diff_snapshot
                diff_proc, head_proc = await asyncio.gather(
                    asyncio.create_subprocess_exec(
                        "git", "diff", "HEAD",
                        cwd=str(project_dir),
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.DEVNULL,
                    ),
                    asyncio.create_subprocess_exec(
                        "git", "rev-parse", "HEAD",
                        cwd=str(project_dir),
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.DEVNULL,
                    ),
                )
                diff_out, _ = await asyncio.wait_for(diff_proc.communicate(), timeout=5)
                head_out, _ = await asyncio.wait_for(head_proc.communicate(), timeout=5)
                head = head_out.decode().strip()
                uncommitted = diff_out.decode()
                if head != old_head or uncommitted != old_diff:
                    await self.bot.request_restart(channel=message.channel)
            except Exception:
                pass


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ClaudePromptCog(bot))
