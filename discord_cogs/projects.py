from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from discord_cogs import captains_only


class ProjectsCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="projects", description="List all discovered projects")
    @captains_only()
    async def projects(self, interaction: discord.Interaction) -> None:
        pm = self.bot.project_manager
        projects = pm.projects

        if not projects:
            await interaction.response.send_message("No projects found. Run `/sync-projects` to scan the workspace.")
            return

        lines = ["**Projects:**"]
        for name, project in sorted(projects.items()):
            thread_link = f"<#{project.thread_id}>" if project.thread_id else "no thread"
            # Get active feature
            project_dir = pm.get_project_dir(project)
            feature = self.bot.feature_manager.get_current_feature(project_dir)
            feature_str = f" | feature: `{feature.name}`" if feature else ""
            lines.append(f"- **{name}** → {thread_link}{feature_str}")

        await interaction.response.send_message("\n".join(lines))

    @app_commands.command(name="sync-projects", description="Scan workspace and sync project threads")
    @captains_only()
    async def sync_projects(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        pm = self.bot.project_manager
        results = await pm.sync_projects(self.bot)

        if "error" in results:
            await interaction.followup.send(f"**Error:** {results['error']}")
            return

        if not results:
            await interaction.followup.send("No projects found in workspace.")
            return

        lines = ["**Sync results:**"]
        for name, status in sorted(results.items()):
            lines.append(f"- `{name}`: {status}")

        await interaction.followup.send("\n".join(lines))


    @captains_only()
    @app_commands.command(name="create-project", description="Create a new project directory with a CLAUDE.md and sync it")
    @app_commands.describe(
        name="Project name (used as directory name)",
        description="High-level description of what this project is about",
    )
    async def create_project(self, interaction: discord.Interaction, name: str, description: str) -> None:
        await interaction.response.defer()
        pm = self.bot.project_manager

        # Validate name (safe directory name)
        if not name.replace("-", "").replace("_", "").isalnum():
            await interaction.followup.send("Project name must be alphanumeric (hyphens and underscores allowed).")
            return

        project_dir = pm.workspace / name
        if project_dir.exists():
            await interaction.followup.send(f"Directory `{name}` already exists. Run `/sync-projects` if it needs a thread.")
            return

        # Create directory and CLAUDE.md
        project_dir.mkdir(parents=True)
        claude_md = project_dir / "CLAUDE.md"
        claude_md.write_text(f"# {name}\n\n{description}\n", encoding="utf-8")

        # Sync to create the thread
        results = await pm.sync_projects(self.bot)
        status = results.get(name, "unknown")

        # Find the thread link
        project = pm.projects.get(name)
        if project and project.thread_id:
            await interaction.followup.send(
                f"Project `{name}` created ({status}). Head over to <#{project.thread_id}> to start working on it!"
            )
        else:
            await interaction.followup.send(f"Project `{name}` created ({status}), but thread creation may have failed. Try `/sync-projects`.")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ProjectsCog(bot))
