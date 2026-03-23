import asyncio
import os

import discord
from discord import app_commands
from discord.ext import commands

from discord_cogs import captains_only


class VoiceCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @captains_only()
    @app_commands.command(name="voice-events", description="Toggle which bot events trigger voice notifications")
    @app_commands.describe(event="Event to toggle on/off")
    @app_commands.choices(event=[
        app_commands.Choice(name="Run complete", value="run_complete"),
        app_commands.Choice(name="Error", value="error"),
        app_commands.Choice(name="Context critical (85%)", value="context_critical"),
        app_commands.Choice(name="Feature complete", value="feature_complete"),
    ])
    async def voice_events(self, interaction: discord.Interaction, event: app_commands.Choice[str]) -> None:
        from core.state import load_config, save_config
        config = load_config()
        events: list = config.get("voice_events", [])
        if event.value in events:
            events.remove(event.value)
            state = "disabled"
        else:
            events.append(event.value)
            state = "enabled"
        config["voice_events"] = events
        save_config(config)
        await interaction.response.send_message(f"**{event.name}** voice notifications {state}.")

    @app_commands.command(name="voice-test", description="Play a test notification in the configured voice channel")
    @captains_only()
    async def voice_test(self, interaction: discord.Interaction) -> None:
        if not os.getenv("NOTIFY_VOICE_CHANNEL_ID"):
            await interaction.response.send_message(
                "NOTIFY_VOICE_CHANNEL_ID is not set in the environment.", ephemeral=True
            )
            return
        if not os.getenv("ELEVENLABS_API_KEY"):
            await interaction.response.send_message(
                "ELEVENLABS_API_KEY is not set in the environment.", ephemeral=True
            )
            return

        await interaction.response.send_message("Playing test notification...")
        asyncio.create_task(
            self.bot.voice_notifier.play_prompt(interaction.guild, "speak: This is a test notification from the Discord bot.")
        )

    @app_commands.command(name="voice-status", description="Show current voice notification configuration")
    @captains_only()
    async def voice_status(self, interaction: discord.Interaction) -> None:
        from core.state import load_config
        config = load_config()

        channel_id = os.getenv("NOTIFY_VOICE_CHANNEL_ID")
        channel_name = "not set"
        if channel_id:
            ch = interaction.guild.get_channel(int(channel_id))
            channel_name = ch.name if ch else f"unknown ({channel_id})"

        api_key_set = bool(os.getenv("ELEVENLABS_API_KEY"))
        voice_id = os.getenv("ELEVENLABS_VOICE_ID", "JBFqnCBsd6RMkjVDRZzb")
        events = config.get("voice_events", [])

        lines = [
            "**Voice Notification Config:**",
            f"- Channel: {channel_name}",
            f"- API key: {'set' if api_key_set else 'not set'}",
            f"- Voice ID: `{voice_id}`",
            f"- Events: {', '.join(events) if events else 'none'}",
        ]
        await interaction.response.send_message("\n".join(lines))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(VoiceCog(bot))
