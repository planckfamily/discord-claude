import asyncio
import logging
import uuid

import discord
from discord.ext import commands

from core.discord_streamer import DiscordStreamer

log = logging.getLogger(__name__)


class ClaudePromptCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        # Ignore own messages
        if message.author == self.bot.user:
            return

        # Ignore messages that aren't in a thread
        if not isinstance(message.channel, discord.Thread):
            return

        # Must mention the bot
        if self.bot.user not in message.mentions:
            return

        # Resolve project from thread
        project = self.bot.project_manager.get_project_by_thread(message.channel.id)
        if not project:
            return

        # Strip the bot mention from the prompt
        prompt = message.content
        for mention_str in [f"<@{self.bot.user.id}>", f"<@!{self.bot.user.id}>"]:
            prompt = prompt.replace(mention_str, "")
        prompt = prompt.strip()

        if not prompt:
            await message.channel.send("Send a prompt after mentioning me.")
            return

        # Check if Claude is already running for this project
        runner = self.bot.claude_runner
        if runner.is_busy(message.channel.id):
            await message.channel.send("Claude is already working on something in this project. Please wait or use `/cancel`.")
            return

        project_dir = self.bot.project_manager.get_project_dir(project)

        # Get current feature's session_id (if any)
        feature = self.bot.feature_manager.get_current_feature(project_dir)
        session_id = feature.session_id if feature else None
        # If no feature exists, use a default session for the project
        if not session_id:
            state = __import__("core.state", fromlist=["load_project_state"]).load_project_state(project_dir)
            session_id = state.get("default_session_id")
            if not session_id:
                session_id = str(uuid.uuid4())
                state["default_session_id"] = session_id
                __import__("core.state", fromlist=["save_project_state"]).save_project_state(project_dir, state)
            resume = True
        else:
            resume = True

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
                    # Update session_id if returned
                    if event.session_id and feature:
                        from core.state import load_project_state, save_project_state

                        state = load_project_state(project_dir)
                        if feature.name in state.get("features", {}):
                            state["features"][feature.name]["session_id"] = event.session_id
                            save_project_state(project_dir, state)

            await streamer.finalize()

            # Log to history
            self.bot.feature_manager.add_history(
                project_dir,
                user=str(message.author),
                prompt_summary=prompt,
                feature_name=feature.name if feature else None,
            )

        except Exception as e:
            log.exception("Error during Claude prompt relay")
            await streamer.send_error(str(e))
        finally:
            tick_task.cancel()
            try:
                await tick_task
            except asyncio.CancelledError:
                pass


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ClaudePromptCog(bot))
