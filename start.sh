#!/usr/bin/env bash
# Start the Discord bot using the project's virtual environment.
# Automatically restarts when the bot exits with code 42.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

if [ -f "$SCRIPT_DIR/venv/Scripts/activate" ]; then
    source "$SCRIPT_DIR/venv/Scripts/activate"
else
    source "$SCRIPT_DIR/venv/bin/activate"
fi

while true; do
    python "$SCRIPT_DIR/bot.py" "$@"
    EXIT_CODE=$?
    if [ $EXIT_CODE -ne 42 ]; then
        exit $EXIT_CODE
    fi
    echo "Bot requested restart (exit code 42). Restarting..."
done
