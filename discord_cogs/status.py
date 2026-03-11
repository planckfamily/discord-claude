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

            lines = ["**Bot Status:**"]
            for name, project in sorted(projects.items()):
                thread_id = project.thread_id
                is_busy = self.bot.claude_runner.is_busy(thread_id) if thread_id else False
                feature = self.bot.feature_manager.get_current_feature(pm.get_project_dir(project))
                status = "**running**" if is_busy else "idle"
                feat_str = f" | `{feature.name}`" if feature else ""
                lines.append(f"- **{name}**: {status}{feat_str}")

            await interaction.response.send_message("\n".join(lines))

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
            feat = state["features"][feature.name]
            feat["session_id"] = None
            feat["total_input_tokens"] = 0
            feat["total_output_tokens"] = 0
            feat["total_cost_usd"] = 0.0
            feat["prompt_count"] = 0
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
