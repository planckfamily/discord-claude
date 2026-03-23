import discord
from discord import app_commands

REQUIRED_ROLE = "captains"


def has_captain_role(member: discord.Member | discord.User) -> bool:
    """Check if a guild member has the captains role."""
    if not isinstance(member, discord.Member):
        return False
    return any(role.name.lower() == REQUIRED_ROLE for role in member.roles)


def captains_only():
    """app_commands.check decorator that restricts a slash command to captains."""
    async def predicate(interaction: discord.Interaction) -> bool:
        if not has_captain_role(interaction.user):
            await interaction.response.send_message(
                f"You need the **{REQUIRED_ROLE}** role to use this command.",
                ephemeral=True,
            )
            return False
        return True
    return app_commands.check(predicate)
