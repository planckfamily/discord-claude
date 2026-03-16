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

        # Reject new prompts while a restart is pending
        if self.bot._restart_requested:
            await message.channel.send("Restart in progress — not accepting new prompts.")
            return

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

    async def _run_stream(self, *, channel, runner, prompt, project_dir, thread_id,
                          session_id, resume, feature) -> tuple[str | None, str | None]:
        """Run Claude and stream to Discord. Returns (last_session_id, pending_question)."""
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

        try:
            async for event in runner.run(
                prompt=prompt,
                project_dir=project_dir,
                thread_id=thread_id,
                session_id=session_id,
                resume=resume,
            ):
                if event.type == "text":
                    print(event.content, end="", flush=True)
                    full_response.append(event.content)
                    await streamer.feed(event.content)
                elif event.type == "cancelled":
                    print("\n[Cancelled]", flush=True)
                    await streamer.send_cancelled()
                    return last_session_id, None
                elif event.type == "error":
                    print(f"\n[Error] {event.content}", flush=True)
                    await streamer.send_error(event.content)
                    return last_session_id, None
                elif event.type == "result":
                    if event.session_id:
                        last_session_id = event.session_id
                    print(flush=True)  # final newline after streaming
                    # Persist session_id and model
                    if event.session_id or event.model:
                        from core.state import load_project_state, save_project_state

                        state = load_project_state(project_dir)
                        if event.session_id:
                            # Save to feature if active
                            if feature and feature.name in state.get("features", {}):
                                state["features"][feature.name]["session_id"] = event.session_id
                            # Always save as project default
                            state["default_session_id"] = event.session_id
                        if event.model:
                            state["model"] = event.model
                        save_project_state(project_dir, state)

                    # Show per-prompt usage, context window, and session totals
                    if event.input_tokens is not None:
                        prompt_in = event.input_tokens
                        prompt_out = event.output_tokens or 0
                        prompt_total = prompt_in + prompt_out
                        cost_str = f" | ${event.cost_usd:.4f}" if event.cost_usd else ""

                        # Context health indicator (only if we know the window size)
                        context_line = ""
                        warning = ""
                        if event.context_window:
                            context_pct = prompt_in / event.context_window * 100
                            if context_pct >= 85:
                                indicator = "\U0001f534"   # red circle
                                warning = "\n**\u26a0\ufe0f Context window critically full — wrap up this feature now!**"
                            elif context_pct >= 70:
                                indicator = "\U0001f7e0"   # orange circle
                                warning = "\n**\u26a0\ufe0f Context window getting large — consider finishing soon.**"
                            elif context_pct >= 50:
                                indicator = "\U0001f7e1"   # yellow circle
                                warning = "\n*Context window over 50% — keep an eye on it.*"
                            else:
                                indicator = "\U0001f7e2"   # green circle
                            context_line = f"*{indicator} context: ~{prompt_in:,} / {event.context_window:,} tokens ({context_pct:.1f}%)*"
                        else:
                            context_line = f"*context: ~{prompt_in:,} tokens*"

                        # Accumulate cost for the session/feature
                        totals = self.bot.feature_manager.accumulate_tokens(
                            project_dir,
                            input_tokens=prompt_in,
                            output_tokens=prompt_out,
                            cost_usd=event.cost_usd or 0.0,
                            feature_name=feature.name if feature else None,
                        )
                        session_label = f"feature `{feature.name}`" if feature else "session"

                        model_str = f"*model: `{event.model}`*\n" if event.model else ""
                        session_id_str = f"*session: `{event.session_id}`*\n" if event.session_id else ""

                        session_line = ""
                        if totals["total_cost_usd"]:
                            session_line = f"\n*{session_label}: ${totals['total_cost_usd']:.4f} across {totals['prompt_count']} prompt(s)*"

                        await streamer.feed(
                            f"\n\n---\n"
                            f"{model_str}"
                            f"{session_id_str}"
                            f"*this prompt: {prompt_in:,} in + {prompt_out:,} out = {prompt_total:,} tokens{cost_str}*\n"
                            f"{context_line}"
                            f"{session_line}"
                            f"{warning}"
                        )

            await streamer.finalize()

            # Extract markers from the full response text and clean them from Discord messages
            response_text = "".join(full_response)
            pending_files = SEND_FILE_PATTERN.findall(response_text)
            pending_question = None
            ask_match = ASK_USER_PATTERN.search(response_text)
            if ask_match:
                pending_question = ask_match.group(1)

            # Strip markers from the Discord messages if any were found
            if pending_files or pending_question:
                for msg in streamer.all_messages:
                    try:
                        if msg.content:
                            cleaned = SEND_FILE_PATTERN.sub("", msg.content)
                            cleaned = ASK_USER_PATTERN.sub("", cleaned)
                            if cleaned != msg.content:
                                await msg.edit(content=cleaned.strip() or "\u200b")
                    except discord.HTTPException:
                        pass

            # Attach any files that were referenced
            for rel_path in pending_files:
                rel_path = rel_path.strip()
                file_path = (project_dir / rel_path).resolve()
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

            return last_session_id, pending_question

        except Exception as e:
            log.exception("Error during Claude stream")
            await streamer.send_error(str(e))
            return last_session_id, None
        finally:
            # Always clean up the stop button
            if not streamer._finalized:
                await streamer.finalize()
            tick_task.cancel()
            try:
                await tick_task
            except asyncio.CancelledError:
                pass

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
        feature = self.bot.feature_manager.get_current_feature(project_dir) if project else None
        session_id = feature.session_id if feature else None
        if not session_id:
            from core.state import load_project_state
            state = load_project_state(project_dir)
            session_id = state.get("default_session_id")
        resume = bool(session_id)

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

        # Run the initial prompt
        last_session_id, pending_question = await self._run_stream(
            channel=message.channel,
            runner=runner,
            prompt=prompt,
            project_dir=project_dir,
            thread_id=message.channel.id,
            session_id=session_id,
            resume=resume,
            feature=feature,
        )

        # Question loop: collect answer, continue session, repeat if needed
        while pending_question and last_session_id:
            answer = await self._collect_answer(message.channel, pending_question)
            print(f"[Answer] {answer}", flush=True)

            last_session_id, pending_question = await self._run_stream(
                channel=message.channel,
                runner=runner,
                prompt=answer,
                project_dir=project_dir,
                thread_id=message.channel.id,
                session_id=last_session_id,
                resume=True,
                feature=feature,
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
