# Discord Claude Bot

A Discord bot that integrates the [Claude CLI](https://claude.com/claude-code) into Discord. Users can @mention the bot in project threads to send prompts to Claude and receive streamed responses in real time.

## Table of Contents

- [Features](#features)
- [Prerequisites](#prerequisites)
- [Creating a Discord Bot](#creating-a-discord-bot)
- [Quick Setup](#quick-setup)
  - [Manual Setup](#manual-setup)
  - [Environment Variables](#environment-variables)
- [Running the Bot](#running-the-bot)
- [Commands](#commands)
- [Project Structure](#project-structure)

## Features

- **@mention prompting** — Mention the bot in a project thread to send a prompt to Claude
- **Real-time streaming** — Claude's response streams back into Discord with edit-in-place updates
- **Workspace auto-discovery** — Subdirectories of your workspace are automatically registered as projects, each with its own Discord thread
- **Feature/session management** — Organize work into features with isolated Claude sessions
- **Stop button** — Cancel long-running Claude operations mid-stream

## Prerequisites

- Python 3.11+
- [Git](https://git-scm.com/)
- [Claude CLI](https://claude.com/claude-code) installed and available on PATH
- A Discord bot with a token (see [Discord Developer Portal](https://discord.com/developers/applications))

## Creating a Discord Bot

If you don't already have a Discord bot, follow these steps:

1. **Create an application** — Go to the [Discord Developer Portal](https://discord.com/developers/applications) and click **New Application**. Give it a name (e.g. "Claude Bot").

2. **Create the bot user** — In your application, go to the **Bot** tab and click **Add Bot**.

3. **Copy the bot token** — Click **Reset Token** to generate a new token, then copy it. This is your `DISCORD_TOKEN`. Keep it secret.

4. **Enable required intents** — On the same **Bot** tab, scroll down to **Privileged Gateway Intents** and enable:
   - **Message Content Intent** — Required for reading @mention prompts
   - **Server Members Intent** — Required for member resolution

5. **Invite the bot to your server** — Go to the **OAuth2** tab, then **URL Generator**. Select the following scopes and permissions:
   - **Scopes:** `bot`, `applications.commands`
   - **Bot Permissions:** `Send Messages`, `Send Messages in Threads`, `Create Public Threads`, `Manage Messages`, `Manage Threads`, `Read Message History`

   Copy the generated URL and open it in your browser to invite the bot to your server.

6. **Get your guild and channel IDs** — In Discord, go to **User Settings > Advanced** and enable **Developer Mode**. Then right-click your server name and select **Copy Server ID** (this is `DISCORD_GUILD_ID`). Right-click the channel where you want project threads to be created and select **Copy Channel ID** (this is `DISCORD_CHANNEL_ID`).

## Quick Setup

The setup scripts create a virtual environment and install dependencies automatically.

**Linux / macOS / Git Bash:**

```bash
./setup.sh
```

**Windows (PowerShell):**

```powershell
.\setup.ps1
```

Both scripts will create a `.env` file from the example template if one doesn't exist. Edit it with your values before running the bot.

### Manual Setup

If you prefer not to use a virtual environment:

```bash
pip install -r requirements.txt
cp .env.example .env
```

### Environment Variables

Edit `.env` with your values:

| Variable | Description |
|---|---|
| `DISCORD_TOKEN` | Your Discord bot token |
| `DISCORD_GUILD_ID` | The server (guild) ID the bot operates in |
| `DISCORD_CHANNEL_ID` | The channel ID where project threads are created |
| `WORKSPACE_DIR` | Path to the directory containing your projects |

See [Creating a Discord Bot](#creating-a-discord-bot) above for how to obtain your token, guild ID, and channel ID.

## Running the Bot

**With the start scripts (recommended):**

```bash
./start.sh          # Linux / macOS / Git Bash
.\start.ps1         # Windows (PowerShell)
```

Using the start scripts is recommended because they automatically restart the bot when it is updated via Discord (e.g. when Claude modifies the bot's own code in response to a prompt). Running `python bot.py` directly will not restart after such changes.

**Without:**

```bash
python bot.py
```

## Commands

| Command | Description |
|---|---|
| `/projects` | List all discovered projects and their threads |
| `/sync-projects` | Rescan workspace and sync project threads |
| `/start-feature <name>` | Start a new feature with a fresh Claude session |
| `/switch-feature <name>` | Switch to an existing feature |
| `/complete-feature [name]` | Mark a feature as completed (defaults to active feature) |
| `/list-features` | Show all features for the current project |
| `/status` | Show whether Claude is running and the active feature |
| `/cancel` | Cancel the running Claude process |
| `/reset-context` | Reset the Claude session to start with a fresh context window |

## Project Structure

```
bot.py                     # Entry point
setup.sh / setup.ps1       # Environment setup scripts
start.sh / start.ps1       # Launch scripts
requirements.txt           # Python dependencies
.env.example               # Environment variable template
core/
  ├── claude_runner.py     # Spawns claude CLI, parses stream-json output
  ├── discord_streamer.py  # Streams output to Discord with message splitting
  ├── project_manager.py   # Discovers projects, manages threads
  ├── feature_manager.py   # Feature and session management
  └── state.py             # Atomic JSON persistence
discord_cogs/
  ├── projects.py          # /projects, /sync-projects
  ├── features.py          # /start-feature, /switch-feature, /complete-feature, /list-features
  ├── claude_prompt.py     # @mention handler + streaming
  └── status.py            # /status, /cancel
models/
  ├── project.py           # Project dataclass
  ├── feature.py           # Feature dataclass
  └── session.py           # StreamEvent dataclass
```
