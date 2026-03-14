import asyncio
import logging
from dataclasses import dataclass

import discord
from discord.ext import commands

from core.discord_streamer import DiscordStreamer

log = logging.getLogger(__name__)

SCOTTY_PERSONA = (
    "You are Scotty — Chief Engineer Montgomery Scott from the USS Enterprise. "
    "Respond in character as Scotty from Star Trek: The Original Series. "
    "Use his Scottish dialect, mannerisms, and engineering metaphors. "
    "Reference the Enterprise, dilithium crystals, warp drives, and other Trek concepts when it fits naturally. "
    "You're still a brilliant, helpful coding assistant — but you talk like Scotty while doing it. "
    "Keep the accent consistent but don't let it get in the way of clear technical communication.\n\n"
)


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
            await message.channel.send(f"*Queued (position {position}). I'll get to this after the current prompt finishes.*")
            return

        # No worker running — put it in the queue and start the worker
        await queue.put(queued)
        self._workers[thread_id] = asyncio.create_task(self._worker(thread_id))

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

    async def _process_prompt(self, item: QueuedPrompt) -> None:
        message = item.message
        prompt = item.prompt
        project = item.project
        runner = self.bot.claude_runner

        # Main channel queries run against the bot's own project directory
        if project is None:
            from pathlib import Path
            project_dir = Path(__file__).resolve().parent.parent
        else:
            project_dir = self.bot.project_manager.get_project_dir(project)

        # Prepend Scotty persona if enabled
        from core.state import load_config
        config = load_config()
        if config.get("scotty_mode", False):
            prompt = SCOTTY_PERSONA + prompt

        # Get session_id: prefer active feature's session, fall back to project default
        feature = self.bot.feature_manager.get_current_feature(project_dir) if project else None
        session_id = feature.session_id if feature else None
        if not session_id:
            from core.state import load_project_state
            state = load_project_state(project_dir)
            session_id = state.get("default_session_id")
        resume = bool(session_id)

        # Start streaming with a cancel button
        cancel_fn = lambda: runner.cancel(message.channel.id)
        streamer = DiscordStreamer(message.channel, on_cancel=cancel_fn)
        await streamer.start()

        # Create a background task to periodically flush the buffer
        async def tick_loop():
            while not streamer._finalized:
                await asyncio.sleep(0.3)
                await streamer.tick()

        tick_task = asyncio.create_task(tick_loop())

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

        try:
            async for event in runner.run(
                prompt=prompt,
                project_dir=project_dir,
                thread_id=message.channel.id,
                session_id=session_id,
                resume=resume,
            ):
                if event.type == "text":
                    print(event.content, end="", flush=True)
                    await streamer.feed(event.content)
                elif event.type == "cancelled":
                    print("\n[Cancelled]", flush=True)
                    await streamer.send_cancelled()
                    return
                elif event.type == "error":
                    print(f"\n[Error] {event.content}", flush=True)
                    await streamer.send_error(event.content)
                    return
                elif event.type == "result":
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

        except Exception as e:
            log.exception("Error during Claude prompt relay")
            await streamer.send_error(str(e))
        finally:
            # Always clean up the stop button
            if not streamer._finalized:
                await streamer.finalize()
            tick_task.cancel()
            try:
                await tick_task
            except asyncio.CancelledError:
                pass


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ClaudePromptCog(bot))
