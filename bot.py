import asyncio
import logging
import os
import sys
from pathlib import Path

import discord
from discord.ext import commands
from dotenv import load_dotenv

from core.claude_runner import ClaudeRunner
from core.feature_manager import FeatureManager
from core.project_manager import ProjectManager
from core.voice_notifier import VoiceNotifier

load_dotenv(override=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("bot")

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("DISCORD_GUILD_ID")
CHANNEL_ID = os.getenv("DISCORD_CHANNEL_ID")
WORKSPACE_DIR = os.getenv("WORKSPACE_DIR")

if not DISCORD_TOKEN:
    sys.exit("DISCORD_TOKEN is required in .env")
if not GUILD_ID:
    sys.exit("DISCORD_GUILD_ID is required in .env")
if not CHANNEL_ID:
    sys.exit("DISCORD_CHANNEL_ID is required in .env")
if not WORKSPACE_DIR:
    sys.exit("WORKSPACE_DIR is required in .env")


intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True


RESTART_EXIT_CODE = 42


class ClaudeBot(commands.Bot):
    def __init__(self) -> None:
        super().__init__(command_prefix="!", intents=intents)
        self.claude_runner = ClaudeRunner()
        self.feature_manager = FeatureManager()
        self.voice_notifier = VoiceNotifier(self)
        self.workspace_dir = Path(WORKSPACE_DIR)
        self.project_manager = ProjectManager(
            workspace_dir=WORKSPACE_DIR,
            guild_id=int(GUILD_ID),
            channel_id=int(CHANNEL_ID),
        )
        self._restart_requested = False
        self._restart_channel = None  # channel to notify when restart happens
        # Callbacks invoked when a worker finishes: list of async callables
        self._on_worker_done: list = []

    async def setup_hook(self) -> None:
        await self.load_extension("discord_cogs.projects")
        await self.load_extension("discord_cogs.features")
        await self.load_extension("discord_cogs.claude_prompt")
        await self.load_extension("discord_cogs.status")
        await self.load_extension("discord_cogs.voice")

        guild = discord.Object(id=int(GUILD_ID))
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        log.info("Slash commands synced to guild %s", GUILD_ID)

    async def on_ready(self) -> None:
        log.info("Logged in as %s (ID: %s)", self.user, self.user.id)
        log.info("Workspace: %s", WORKSPACE_DIR)

        # Auto-scan workspace on startup
        results = await self.project_manager.sync_projects(self)
        if results:
            for name, status in sorted(results.items()):
                log.info("Project %s: %s", name, status)
        else:
            log.info("No projects found in workspace")

        # Check if this boot follows a restart
        restart_marker = Path(__file__).resolve().parent / ".claude-bot" / ".restarting"
        if restart_marker.exists():
            restart_marker.unlink()
            try:
                main_channel = self.get_channel(self.project_manager.channel_id)
                if main_channel:
                    await main_channel.send("I'm back online and ready to go!")
            except discord.HTTPException:
                pass

    def is_self_project(self, project_dir) -> bool:
        """Check if a project directory is the bot's own codebase."""
        from pathlib import Path
        try:
            return Path(project_dir).resolve() == Path(__file__).resolve().parent
        except (OSError, ValueError):
            return False

    async def request_restart(self, channel=None) -> None:
        """Signal the bot to restart after all active prompts finish."""
        self._restart_requested = True
        self._restart_channel = channel
        log.info("Restart requested — will restart when all workers finish.")

        # Register the restart check as a worker-done callback
        self._on_worker_done.append(self._check_restart)

        # If nothing is running right now, restart immediately
        await self._check_restart()

    async def _check_restart(self) -> None:
        """Called when a worker finishes. If restart is pending and no workers remain, shut down."""
        if not self._restart_requested:
            return

        prompt_cog = self.cogs.get("ClaudePromptCog")
        if prompt_cog and prompt_cog._workers:
            active = [t for t in prompt_cog._workers.values() if not t.done()]
            if active:
                log.info("Restart pending — %d worker(s) still active.", len(active))
                return

        log.info("All workers drained — restarting now.")
        # Leave a breadcrumb so the next boot knows it was a restart
        restart_marker = Path(__file__).resolve().parent / ".claude-bot" / ".restarting"
        restart_marker.parent.mkdir(exist_ok=True)
        restart_marker.touch()
        # Notify the main channel
        try:
            main_channel = self.get_channel(self.project_manager.channel_id)
            if main_channel:
                await main_channel.send("Restarting... be right back!")
        except discord.HTTPException:
            pass
        await self.close()

    async def notify_worker_done(self) -> None:
        """Called by prompt cog when a worker finishes. Runs all registered callbacks."""
        for callback in list(self._on_worker_done):
            try:
                await callback()
            except Exception:
                log.exception("Error in worker-done callback")

    async def close(self) -> None:
        log.info("Shutting down — cancelling active Claude processes...")
        await self.claude_runner.cancel_all()
        await super().close()


bot = ClaudeBot()


def main() -> None:
    try:
        bot.run(DISCORD_TOKEN, log_handler=None)
    except KeyboardInterrupt:
        log.info("Interrupted")

    if bot._restart_requested:
        log.info("Exiting with code %d to trigger restart", RESTART_EXIT_CODE)
        sys.exit(RESTART_EXIT_CODE)


if __name__ == "__main__":
    main()
