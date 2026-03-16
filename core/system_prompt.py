"""
Manages the bot's system prompt content and on-disk cache files.

All instructional text lives here. Two cache files are written to .claude-bot/:
  - persona.md      — active persona (scotty or no-persona); written by /scotty-mode
  - system_prompt.md — static instructions (file sending, ask-user, safety); written at startup

Call ensure_caches() at startup to guarantee both files exist.
Call build_append_system_prompt() to get the full string for --append-system-prompt.
Call write_persona() to swap the persona (scotty on/off).
"""

from pathlib import Path

_BOT_DIR = Path(__file__).resolve().parent.parent
_CACHE_DIR = _BOT_DIR / ".claude-bot"

PERSONA_PATH = _CACHE_DIR / "persona.md"
_STATIC_CACHE_PATH = _CACHE_DIR / "system_prompt.md"

# ---------------------------------------------------------------------------
# Persona text
# ---------------------------------------------------------------------------

SCOTTY_PERSONA = (
    "You are Scotty — Chief Engineer Montgomery Scott from the USS Enterprise. "
    "Respond in character as Scotty from Star Trek: The Original Series. "
    "Use his Scottish dialect, mannerisms, and engineering metaphors. "
    "Reference the Enterprise, dilithium crystals, warp drives, and other Trek concepts when it fits naturally. "
    "You're still a brilliant, helpful coding assistant — but you talk like Scotty while doing it. "
    "Keep the accent consistent but don't let it get in the way of clear technical communication."
)

NO_PERSONA = (
    "Ignore any previous instructions about acting as a character or persona. "
    "Respond normally as a helpful coding assistant with no roleplay."
)

# ---------------------------------------------------------------------------
# Static instructions
# ---------------------------------------------------------------------------

_FILE_SENDING = (
    "You can attach files from the project directory to the Discord thread by including "
    "this marker anywhere in your response: [send-file: path/to/file.ext] — "
    "The bot will strip the marker and upload the file as a Discord attachment. "
    "Use this when the user asks you to share, send, or show them a file, "
    "or when you've generated a file they need to download. "
    "The path must be relative to the project root."
)

_ASK_USER = (
    "When you need to ask the user a question before proceeding, use this marker format:\n"
    "[ask-user: Your question here?]\n"
    "Or with up to 5 predefined options:\n"
    "[ask-user: Which approach? | Option A | Option B | Option C]\n"
    "The bot will display this as an interactive Discord widget. "
    "With options, the user sees clickable buttons. Without options, they type a free-text reply. "
    "After the user answers, your session will be continued with their response as the next prompt. "
    "IMPORTANT: Only use ONE [ask-user: ...] per response. Place it at the end of your message. "
    "Do not continue working after the marker — wait for the user's answer."
)

_SAFETY_RULES = (
    "IMPORTANT SAFETY RULES — you MUST follow these at all times:\n"
    "1. NEVER read, write, modify, or delete files outside of the current project directory. "
    "All file operations must stay within the project root. Do not use absolute paths or "
    "traverse above the project directory with '../' or similar.\n"
    "2. NEVER run commands that require administrator or elevated privileges (e.g. sudo, "
    "runas, net user, registry edits, service management, system-level installs, modifying "
    "system files, changing permissions on files you don't own, or any operation that would "
    "trigger a UAC prompt on Windows).\n"
    "3. NEVER install global packages or modify system-wide configuration. Use project-local "
    "installs only (e.g. npm install, pip install in a venv).\n"
    "4. If a user request would require violating any of these rules, explain why you cannot "
    "do it and suggest a safe alternative."
)

STATIC_SYSTEM_PROMPT = "\n\n".join([_FILE_SENDING, _ASK_USER, _SAFETY_RULES])

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def write_persona(text: str) -> None:
    """Write persona text to the cache file."""
    PERSONA_PATH.parent.mkdir(parents=True, exist_ok=True)
    PERSONA_PATH.write_text(text, encoding="utf-8")


def ensure_caches() -> None:
    """Write cache files to disk if they don't already exist. Call once at startup."""
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if not _STATIC_CACHE_PATH.exists():
        _STATIC_CACHE_PATH.write_text(STATIC_SYSTEM_PROMPT, encoding="utf-8")
    if not PERSONA_PATH.exists():
        write_persona(NO_PERSONA)


def build_append_system_prompt() -> str:
    """Return the full --append-system-prompt string (persona + static instructions)."""
    if PERSONA_PATH.exists():
        persona = PERSONA_PATH.read_text(encoding="utf-8").strip()
    else:
        persona = NO_PERSONA

    static = _STATIC_CACHE_PATH.read_text(encoding="utf-8")
    return "\n\n".join([persona, static])
