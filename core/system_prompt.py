"""
Manages the bot's system prompt content and on-disk cache files.

All instructional text lives here. Three cache files are written to .claude-bot/:
  - persona.md             — active persona (scotty or no-persona); written by /scotty-mode
  - system_prompt.md       — static instructions (file sending, ask-user, safety); written at startup
  - append_system_prompt.md — combined file passed to --append-system-prompt-file; rebuilt on change

Call ensure_caches() at startup to guarantee all files exist.
Call get_system_prompt_file() to get the path for --append-system-prompt-file.
Call write_persona() to swap the persona (scotty on/off).
"""

from pathlib import Path

_BOT_DIR = Path(__file__).resolve().parent.parent
_CACHE_DIR = _BOT_DIR / ".claude-bot"

PERSONA_PATH = _CACHE_DIR / "persona.md"
_STATIC_CACHE_PATH = _CACHE_DIR / "system_prompt.md"
_COMBINED_PATH = _CACHE_DIR / "append_system_prompt.md"

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

_DISCORD_FORMATTING = (
    "Your responses are displayed in Discord, which has limited markdown support. "
    "Discord does NOT render markdown tables (pipes and dashes).\n\n"
    "When you need to show tabular or aligned data, always use a monospace code block so columns line up correctly:\n"
    "```\n"
    "Key           Value\n"
    "------------  ---------------\n"
    "Branch        main\n"
    "Tests         42 passing\n"
    "Coverage      87%\n"
    "```\n\n"
    "Keep Discord's 2000-character message limit in mind — for large outputs, "
    "consider summarizing or offering to send the full data as a file."
)

STATIC_SYSTEM_PROMPT = "\n\n".join([_FILE_SENDING, _ASK_USER, _SAFETY_RULES, _DISCORD_FORMATTING])

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _rebuild_combined() -> None:
    """Rebuild the combined system prompt file from persona + static instructions."""
    persona = PERSONA_PATH.read_text(encoding="utf-8").strip() if PERSONA_PATH.exists() else NO_PERSONA
    static = _STATIC_CACHE_PATH.read_text(encoding="utf-8") if _STATIC_CACHE_PATH.exists() else STATIC_SYSTEM_PROMPT
    _COMBINED_PATH.write_text("\n\n".join([persona, static]), encoding="utf-8")


def write_persona(text: str) -> None:
    """Write persona text to the cache file and rebuild the combined prompt."""
    PERSONA_PATH.parent.mkdir(parents=True, exist_ok=True)
    PERSONA_PATH.write_text(text, encoding="utf-8")
    _rebuild_combined()


def ensure_caches() -> None:
    """Write cache files to disk if they don't already exist. Call once at startup."""
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if not _STATIC_CACHE_PATH.exists():
        _STATIC_CACHE_PATH.write_text(STATIC_SYSTEM_PROMPT, encoding="utf-8")
    if not PERSONA_PATH.exists():
        PERSONA_PATH.write_text(NO_PERSONA, encoding="utf-8")
    _rebuild_combined()


def get_system_prompt_file() -> Path:
    """Return the path to the combined system prompt file for --append-system-prompt-file."""
    return _COMBINED_PATH


# ---------------------------------------------------------------------------
# Per-session prompt files (support multiple simultaneous sessions with
# different personas)
# ---------------------------------------------------------------------------

_SESSIONS_DIR = _CACHE_DIR / "sessions"


def write_session_prompt(thread_id: int, persona_content: str) -> Path:
    """
    Write a per-session combined prompt file and return its path.

    Each concurrent session (Discord thread) can have a different persona.
    The file is written to .claude-bot/sessions/{thread_id}/append_system_prompt.md.
    """
    session_dir = _SESSIONS_DIR / str(thread_id)
    session_dir.mkdir(parents=True, exist_ok=True)

    static = _STATIC_CACHE_PATH.read_text(encoding="utf-8") if _STATIC_CACHE_PATH.exists() else STATIC_SYSTEM_PROMPT
    persona = persona_content.strip() if persona_content.strip() else NO_PERSONA
    combined = "\n\n".join([persona, static])

    session_file = session_dir / "append_system_prompt.md"
    session_file.write_text(combined, encoding="utf-8")
    return session_file


def cleanup_session_prompt(thread_id: int) -> None:
    """Remove the per-session prompt file when a session ends."""
    import shutil
    session_dir = _SESSIONS_DIR / str(thread_id)
    if session_dir.exists():
        shutil.rmtree(session_dir, ignore_errors=True)
