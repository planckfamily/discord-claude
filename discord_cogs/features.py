import discord
from discord import app_commands
from discord.ext import commands
from pathlib import Path


class SubdirSelect(discord.ui.Select):
    """Dropdown for picking a subdirectory (or project root)."""

    def __init__(self, subdirs: list[str], feature_name: str, project_dir: Path, bot):
        options = [discord.SelectOption(label="Project root", value="__root__", description="Use the project root directory")]
        for d in subdirs[:24]:  # Discord max 25 options total
            options.append(discord.SelectOption(label=d, value=d))
        super().__init__(placeholder="Choose a directory...", options=options)
        self.feature_name = feature_name
        self.project_dir = project_dir
        self.bot = bot

    async def callback(self, interaction: discord.Interaction):
        subdir = self.values[0] if self.values[0] != "__root__" else None
        feature = self.bot.feature_manager.start_feature(self.project_dir, self.feature_name, subdir=subdir)
        scope = f"`{subdir}/`" if subdir else "project root"
        await interaction.response.edit_message(
            content=(
                f"Feature **`{feature.name}`** started in {scope}.\n"
                f"Session ID: `{feature.session_id[:8]}...`"
            ),
            view=None,
        )


class SubdirView(discord.ui.View):
    def __init__(self, subdirs: list[str], feature_name: str, project_dir: Path, bot):
        super().__init__(timeout=60)
        self.add_item(SubdirSelect(subdirs, feature_name, project_dir, bot))


class FeaturesCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    def _resolve_project(self, interaction: discord.Interaction):
        """Resolve the project from the thread context."""
        channel = interaction.channel
        if not isinstance(channel, discord.Thread):
            return None, None
        project = self.bot.project_manager.get_project_by_thread(channel.id)
        if not project:
            return None, None
        project_dir = self.bot.project_manager.get_project_dir(project)
        return project, project_dir

    @staticmethod
    def _list_subdirs(project_dir: Path) -> list[str]:
        """List immediate subdirectories, excluding hidden and common non-project dirs."""
        exclude = {
            "node_modules", "__pycache__", "dist", "build", "bin", "obj",
            ".git", ".claude", ".claude-bot", ".venv", "venv", "env",
        }
        subdirs = []
        for p in sorted(project_dir.iterdir()):
            if p.is_dir() and not p.name.startswith(".") and p.name not in exclude:
                subdirs.append(p.name)
        return subdirs

    @app_commands.command(name="start-feature", description="Start a new feature with a fresh Claude session")
    @app_commands.describe(name="Feature name (descriptive, e.g. 'add-auth-system')")
    async def start_feature(self, interaction: discord.Interaction, name: str) -> None:
        project, project_dir = self._resolve_project(interaction)
        if not project:
            await interaction.response.send_message("Use this command inside a project thread.", ephemeral=True)
            return

        subdirs = self._list_subdirs(project_dir)
        if subdirs:
            view = SubdirView(subdirs, name, project_dir, self.bot)
            await interaction.response.send_message(
                f"Starting feature **`{name}`** — which directory should it be scoped to?",
                view=view,
                ephemeral=True,
            )
        else:
            # No subdirectories — start at project root directly
            feature = self.bot.feature_manager.start_feature(project_dir, name)
            await interaction.response.send_message(
                f"Feature **`{feature.name}`** started.\n"
                f"Session ID: `{feature.session_id[:8]}...`"
            )

    @app_commands.command(name="resume-feature", description="Resume an existing or completed feature")
    @app_commands.describe(name="Feature name to resume")
    async def resume_feature(self, interaction: discord.Interaction, name: str) -> None:
        project, project_dir = self._resolve_project(interaction)
        if not project:
            await interaction.response.send_message("Use this command inside a project thread.", ephemeral=True)
            return

        feature = self.bot.feature_manager.resume_feature(project_dir, name)
        if not feature:
            available = self.bot.feature_manager.list_features(project_dir)
            names = ", ".join(f"`{f.name}`" for f in available) or "none"
            await interaction.response.send_message(
                f"Feature `{name}` not found. Available: {names}",
                ephemeral=True,
            )
            return

        scope = f" in `{feature.subdir}/`" if feature.subdir else ""
        await interaction.response.send_message(
            f"Resumed feature **`{feature.name}`**{scope}.\n"
            f"Session ID: `{feature.session_id[:8]}...`"
        )

    @app_commands.command(name="complete-feature", description="Mark a feature as completed")
    @app_commands.describe(name="Feature name to complete (defaults to current active feature)")
    async def complete_feature(self, interaction: discord.Interaction, name: str | None = None) -> None:
        project, project_dir = self._resolve_project(interaction)
        if not project:
            await interaction.response.send_message("Use this command inside a project thread.", ephemeral=True)
            return

        feature = self.bot.feature_manager.complete_feature(project_dir, name)
        if not feature:
            if name:
                await interaction.response.send_message(f"Feature `{name}` not found.", ephemeral=True)
            else:
                await interaction.response.send_message("No active feature to complete.", ephemeral=True)
            return

        await interaction.response.send_message(
            f"Feature **`{feature.name}`** marked as completed. Generating feature summary..."
        )

        prompt_cog = self.bot.cogs.get("ClaudePromptCog")
        if prompt_cog and feature.session_id:
            import asyncio
            asyncio.create_task(
                prompt_cog.run_feature_summary_prompt(interaction.channel, project, feature)
            )

    @app_commands.command(name="list-features", description="List all features for this project")
    async def list_features(self, interaction: discord.Interaction) -> None:
        project, project_dir = self._resolve_project(interaction)
        if not project:
            await interaction.response.send_message("Use this command inside a project thread.", ephemeral=True)
            return

        features = self.bot.feature_manager.list_features(project_dir)
        current = self.bot.feature_manager.get_current_feature(project_dir)

        if not features:
            await interaction.response.send_message("No features yet. Use `/start-feature` to create one.")
            return

        lines = [f"**Features for `{project.name}`:**"]
        for f in features:
            marker = " ← active" if current and f.name == current.name else ""
            scope = f" (`{f.subdir}/`)" if f.subdir else ""
            lines.append(f"- `{f.name}` [{f.status}]{scope}{marker}")

        await interaction.response.send_message("\n".join(lines))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(FeaturesCog(bot))
