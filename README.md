# Discord Claude Bot

A Discord bot that integrates the [Claude CLI](https://claude.com/claude-code) into Discord. Users can @mention the bot in project threads to send prompts to Claude and receive streamed responses in real time.

## Features

- **@mention prompting** — Mention the bot in a project thread to send a prompt to Claude
- **Real-time streaming** — Claude's response streams back into Discord with edit-in-place updates
- **Workspace auto-discovery** — Subdirectories of your workspace are automatically registered as projects, each with its own Discord thread
- **Feature/session management** — Organize work into features with isolated Claude sessions
- **Stop button** — Cancel long-running Claude operations mid-stream

## Prerequisites

- Python 3.8+
- [Claude CLI](https://claude.com/claude-code) installed and available on PATH
- A Discord bot with a token (see [Discord Developer Portal](https://discord.com/developers/applications))

## Setup

1. **Clone the repo and install dependencies:**

   ```bash
   pip install -r requirements.txt
   ```

2. **Configure environment variables:**

   ```bash
   cp .env.example .env
   ```

   Edit `.env` with your values:

   | Variable | Description |
   |---|---|
   | `DISCORD_TOKEN` | Your Discord bot token |
   | `DISCORD_GUILD_ID` | The server (guild) ID the bot operates in |
   | `DISCORD_CHANNEL_ID` | The channel ID where project threads are created |
   | `WORKSPACE_DIR` | Path to the directory containing your projects |

   To get your guild and channel IDs, enable **Developer Mode** in Discord settings, then right-click the server/channel and select **Copy ID**.

3. **Run the bot:**

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
| `/list-features` | Show all features for the current project |
| `/status` | Show whether Claude is running and the active feature |
| `/cancel` | Cancel the running Claude process |

## Project Structure

```
bot.py                     # Entry point
core/
  ├── claude_runner.py     # Spawns claude CLI, parses stream-json output
  ├── discord_streamer.py  # Streams output to Discord with message splitting
  ├── project_manager.py   # Discovers projects, manages threads
  ├── feature_manager.py   # Feature and session management
  └── state.py             # Atomic JSON persistence
cogs/
  ├── projects.py          # /projects, /sync-projects
  ├── features.py          # /start-feature, /switch-feature, /list-features
  ├── claude_prompt.py     # @mention handler + streaming
  └── status.py            # /status, /cancel
models/
  ├── project.py           # Project dataclass
  ├── feature.py           # Feature dataclass
  └── session.py           # StreamEvent dataclass
```
