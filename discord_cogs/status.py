import discord
from discord import app_commands
from discord.ext import commands


class StatusCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="status", description="Show current status for this project")
    async def status(self, interaction: discord.Interaction) -> None:
        channel = interaction.channel

        # If used in a thread, show project-specific status
        if isinstance(channel, discord.Thread):
            project = self.bot.project_manager.get_project_by_thread(channel.id)
            if not project:
                await interaction.response.send_message("This thread isn't linked to a project.", ephemeral=True)
                return

            project_dir = self.bot.project_manager.get_project_dir(project)
            feature = self.bot.feature_manager.get_current_feature(project_dir)
            is_busy = self.bot.claude_runner.is_busy(channel.id)

            lines = [f"**Status for `{project.name}`:**"]
            lines.append(f"- Claude: {'**running**' if is_busy else 'idle'}")
            if feature:
                lines.append(f"- Active feature: `{feature.name}`")
                lines.append(f"- Session: `{feature.session_id[:8]}...`")
            else:
                lines.append("- No active feature")

            from core.state import load_project_state

            state = load_project_state(project_dir)

            # Show model
            model = state.get("model")
            if model:
                lines.append(f"- Model: `{model}`")

            # Show last history entry
            history = state.get("history", [])
            if history:
                last = history[-1]
                lines.append(f"- Last prompt: \"{last['prompt_summary'][:80]}\" by {last['user']}")

            await interaction.response.send_message("\n".join(lines))
        else:
            # Main channel — show overview
            pm = self.bot.project_manager
            projects = pm.projects
            if not projects:
                await interaction.response.send_message("No projects. Run `/sync-projects` first.")
                return

            # Collect active and idle projects
            active_lines = []
            idle_lines = []
            for name, project in sorted(projects.items()):
                thread_id = project.thread_id
                is_busy = self.bot.claude_runner.is_busy(thread_id) if thread_id else False
                feature = self.bot.feature_manager.get_current_feature(pm.get_project_dir(project))
                feat_str = f" | feature: `{feature.name}`" if feature else ""
                thread_link = f" (<#{thread_id}>)" if thread_id else ""

                if is_busy:
                    active_lines.append(f"- **{name}**{thread_link}{feat_str}")
                else:
                    idle_lines.append(f"- {name}{feat_str}")

            lines = ["**Bot Status:**"]
            if self.bot._restart_requested:
                lines.append("\n⚠️ **Restart pending** — waiting for active processes to finish.")
            if active_lines:
                lines.append(f"\n🔄 **Active processes ({len(active_lines)}):**")
                lines.extend(active_lines)
            else:
                lines.append("\nNo active processes.")

            if idle_lines:
                lines.append(f"\n💤 **Idle ({len(idle_lines)}):**")
                lines.extend(idle_lines)

            await interaction.response.send_message("\n".join(lines))

    @app_commands.command(name="restart-scotty", description="Restart the bot process")
    async def restart_scotty(self, interaction: discord.Interaction) -> None:
        prompt_cog = self.bot.cogs.get("ClaudePromptCog")
        active = []
        if prompt_cog and prompt_cog._workers:
            active = [t for t in prompt_cog._workers.values() if not t.done()]

        if active:
            await interaction.response.send_message(
                f"Restart queued — waiting for {len(active)} active process(es) to finish."
            )
        else:
            await interaction.response.send_message("Restarting... be right back!")
        await self.bot.request_restart(interaction.channel)

    @app_commands.command(name="force-restart", description="Restart immediately without waiting for active processes")
    async def force_restart(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message("Force restarting — killing all active processes!")
        self.bot._restart_requested = True
        await self.bot.close()

    @app_commands.command(name="scotty-mode", description="Toggle Scotty personality mode on or off")
    async def scotty_mode(self, interaction: discord.Interaction) -> None:
        from core.state import load_config, save_config
        from core.system_prompt import NO_PERSONA, SCOTTY_PERSONA, write_persona

        config = load_config()
        current = config.get("scotty_mode", False)
        config["scotty_mode"] = not current
        save_config(config)

        if config["scotty_mode"]:
            write_persona(SCOTTY_PERSONA)
            await interaction.response.send_message("Scotty mode **enabled**! Aye, I'll give ye all she's got, Captain!")
        else:
            write_persona(NO_PERSONA)
            await interaction.response.send_message("Scotty mode **disabled**. Back to normal.")

    @app_commands.command(name="cancel", description="Cancel the running Claude process for this project")
    async def cancel(self, interaction: discord.Interaction) -> None:
        channel = interaction.channel
        if not isinstance(channel, discord.Thread):
            await interaction.response.send_message("Use this in a project thread.", ephemeral=True)
            return

        if self.bot.claude_runner.cancel(channel.id):
            await interaction.response.send_message("Cancelled the running Claude process.")
        else:
            await interaction.response.send_message("No Claude process is running.", ephemeral=True)


    @app_commands.command(name="reset-context", description="Reset the Claude session to start with a fresh context window")
    async def reset_context(self, interaction: discord.Interaction) -> None:
        channel = interaction.channel
        if not isinstance(channel, discord.Thread):
            await interaction.response.send_message("Use this in a project thread.", ephemeral=True)
            return

        project = self.bot.project_manager.get_project_by_thread(channel.id)
        if not project:
            await interaction.response.send_message("This thread isn't linked to a project.", ephemeral=True)
            return

        if self.bot.claude_runner.is_busy(channel.id):
            await interaction.response.send_message("Claude is currently running. Cancel it first with `/cancel`.", ephemeral=True)
            return

        project_dir = self.bot.project_manager.get_project_dir(project)
        feature = self.bot.feature_manager.get_current_feature(project_dir)

        from core.state import load_project_state, save_project_state

        state = load_project_state(project_dir)

        if feature and feature.name in state.get("features", {}):
            # Null session to force a fresh context window; keep cumulative token counts
            state["features"][feature.name]["session_id"] = None
            label = f"feature `{feature.name}`"
        else:
            state["default_session_id"] = None
            state["session_usage"] = {
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "total_cost_usd": 0.0,
                "prompt_count": 0,
            }
            label = "project session"

        save_project_state(project_dir, state)
        await interaction.response.send_message(f"Context reset for {label}. The next prompt will start a fresh Claude session.")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(StatusCog(bot))
