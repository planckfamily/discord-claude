import asyncio
import json
import logging
import os
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from pathlib import Path

from core.system_prompt import (
    cleanup_session_prompt,
    ensure_caches,
    get_system_prompt_file,
    write_session_prompt,
)
from models.session import StreamEvent

log = logging.getLogger(__name__)


@dataclass
class ActiveRun:
    process: asyncio.subprocess.Process
    prompt: str
    started_at: float  # time.monotonic()


class ClaudeRunner:
    def __init__(self) -> None:
        # channel/thread id -> active run info
        self._active: dict[int, ActiveRun] = {}
        self._cancelled: set[int] = set()
        ensure_caches()


    def is_busy(self, thread_id: int) -> bool:
        run = self._active.get(thread_id)
        return run is not None and run.process.returncode is None

    def get_active_info(self, thread_id: int) -> tuple[str, float] | None:
        """Returns (prompt, elapsed_seconds) if a run is active, else None."""
        run = self._active.get(thread_id)
        if run and run.process.returncode is None:
            return run.prompt, time.monotonic() - run.started_at
        return None

    def cancel(self, thread_id: int) -> bool:
        run = self._active.get(thread_id)
        if run and run.process.returncode is None:
            self._cancelled.add(thread_id)
            run.process.kill()
            return True
        return False

    async def cancel_all(self) -> None:
        for thread_id, run in list(self._active.items()):
            if run.process.returncode is None:
                run.process.kill()
                try:
                    await asyncio.wait_for(run.process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    pass
        self._active.clear()
        self._cancelled.clear()

    @staticmethod
    def _session_exists(session_id: str, project_dir: Path) -> bool:
        """Check if a Claude session file exists on disk."""
        # Claude stores sessions in ~/.claude/projects/<slug>/<session_id>.jsonl
        # The slug replaces path separators with dashes
        claude_dir = Path.home() / ".claude" / "projects"
        slug = str(project_dir).replace("/", "-").replace("\\", "-").replace(":", "-")
        # Try with and without leading dash (Windows drives produce leading dash)
        for candidate in [slug, slug.lstrip("-")]:
            session_file = claude_dir / candidate / f"{session_id}.jsonl"
            if session_file.exists():
                return True
        return False

    async def run(
        self,
        prompt: str,
        project_dir: Path,
        thread_id: int,
        session_id: str | None = None,
        resume: bool = False,
        persona_content: str = "",
        model: str | None = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        if self.is_busy(thread_id):
            yield StreamEvent(type="error", content="Claude is already running for this project.")
            return

        # Validate session before using it
        if session_id and not self._session_exists(session_id, project_dir):
            log.info("Session %s not found on disk, starting fresh.", session_id)
            session_id = None

        cmd = ["claude", "-p", "--verbose", "--output-format", "stream-json", "--dangerously-skip-permissions"]

        if model:
            cmd.extend(["--model", model])

        if resume and session_id:
            cmd.extend(["--resume", session_id])
        elif session_id:
            cmd.extend(["--continue", session_id])

        # Use a per-session prompt file if a persona is provided, otherwise fall back to global
        if persona_content:
            session_prompt_file = write_session_prompt(thread_id, persona_content)
            cmd.extend(["--append-system-prompt-file", str(session_prompt_file)])
        else:
            cmd.extend(["--append-system-prompt-file", str(get_system_prompt_file())])

        # Use -- to prevent prompt from being parsed as a flag
        cmd.extend(["--", prompt])

        # Build clean environment
        env = {k: v for k, v in os.environ.items()}

        try:
            log.info("Starting: %s", " ".join(cmd))
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(project_dir),
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            log.info("Process started (PID %s)", proc.pid)
            self._active[thread_id] = ActiveRun(process=proc, prompt=prompt, started_at=time.monotonic())

            # Start draining stderr immediately
            stderr_task = asyncio.create_task(self._drain_stderr(proc))

            async for event in self._parse_stream(proc):
                yield event

            # Wait for the process to finish and collect stderr
            await proc.wait()
            stderr_chunks = await stderr_task
            if proc.returncode != 0:
                stderr_text = "".join(stderr_chunks).strip()[:500]
                msg = f"Claude exited with code {proc.returncode}"
                if stderr_text:
                    msg += f": {stderr_text}"
                yield StreamEvent(type="error", content=msg)

        except FileNotFoundError:
            yield StreamEvent(type="error", content="Claude CLI not found. Make sure `claude` is installed and on PATH.")
        except Exception as e:
            yield StreamEvent(type="error", content=f"Failed to run Claude: {e}")
        finally:
            was_cancelled = thread_id in self._cancelled
            self._cancelled.discard(thread_id)
            self._active.pop(thread_id, None)
            # Clean up per-session prompt file if one was written
            if persona_content:
                cleanup_session_prompt(thread_id)
            if was_cancelled:
                yield StreamEvent(type="cancelled")

    async def _drain_stderr(self, proc: asyncio.subprocess.Process) -> list[str]:
        """Read stderr continuously so the pipe buffer never fills up. Returns collected chunks."""
        assert proc.stderr is not None
        chunks: list[str] = []
        while True:
            chunk = await proc.stderr.read(4096)
            if not chunk:
                break
            text = chunk.decode(errors="replace")
            chunks.append(text)
            log.debug("claude stderr: %s", text.strip())
        return chunks

    async def _parse_stream(
        self, proc: asyncio.subprocess.Process
    ) -> AsyncGenerator[StreamEvent, None]:
        assert proc.stdout is not None

        buffer = b""
        has_emitted_text = False
        # Tracks the input token count from the most recent assistant turn.
        # Each assistant event from the CLI contains message.usage with per-call
        # token counts. The last one is the true context-window fill at completion.
        last_turn_input = 0

        while True:
            chunk = await proc.stdout.read(4096)
            if not chunk:
                break

            buffer += chunk
            lines = buffer.split(b"\n")
            # Keep the last incomplete line in the buffer
            buffer = lines[-1]

            for line in lines[:-1]:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    log.warning("Skipping malformed JSON line: %s", line[:200])
                    continue

                # Update last_turn_input from each assistant message's per-call usage.
                # This is more accurate than dividing aggregate totals by num_turns.
                if data.get("type") == "assistant":
                    usage = data.get("message", {}).get("usage", {})
                    if usage:
                        last_turn_input = (
                            usage.get("input_tokens", 0)
                            + usage.get("cache_read_input_tokens", 0)
                            + usage.get("cache_creation_input_tokens", 0)
                        )

                for event in self._parse_line(data, has_emitted_text, last_turn_input):
                    if event.type == "text" and event.content.strip():
                        has_emitted_text = True
                    yield event

        # Process any remaining buffer
        if buffer.strip():
            try:
                data = json.loads(buffer.strip())
            except json.JSONDecodeError:
                log.warning("Skipping malformed JSON line: %s", buffer[:200])
                return
            for event in self._parse_line(data, has_emitted_text, last_turn_input):
                yield event

    def _parse_line(self, data: dict, has_emitted_text: bool = False, last_turn_input: int = 0) -> list[StreamEvent]:
        msg_type = data.get("type", "")

        # Handle assistant messages (CLI stream-json format)
        if msg_type == "assistant":
            blocks = data.get("message", {}).get("content", [])
            text_parts = [b.get("text", "") for b in blocks if b.get("type") == "text"]
            if not text_parts:
                return []
            combined = "".join(text_parts)
            # Separate from previous text (e.g. after a tool_use turn)
            if has_emitted_text:
                combined = "\n\n" + combined
            # Split into paragraphs so Discord messages stream naturally
            paragraphs = combined.split("\n\n")
            return [
                StreamEvent(type="text", content=para + ("\n\n" if i < len(paragraphs) - 1 else ""))
                for i, para in enumerate(paragraphs) if para or i < len(paragraphs) - 1
            ]

        # Handle content_block_delta (streaming deltas)
        if msg_type == "content_block_delta":
            delta = data.get("delta", {})
            if delta.get("type") == "text_delta":
                return [StreamEvent(type="text", content=delta.get("text", ""))]

        # Handle result/final message
        if msg_type == "result":
            cost_raw = data.get("cost_usd") or data.get("total_cost_usd")
            num_turns = data.get("num_turns", 1)
            model_usage = data.get("modelUsage", {})
            model_name = next(iter(model_usage.keys()), None)
            usage = next(iter(model_usage.values()), {})

            log.info(
                "Result payload: cost_usd=%s, num_turns=%s, usage=%s, all_keys=%s",
                cost_raw, num_turns, usage, list(data.keys()),
            )

            result = data.get("result", "")

            # last_turn_input is tracked in _parse_stream from each assistant message's
            # per-call usage — this is the accurate context fill at the final turn.
            # Fall back to total aggregate only if we never saw an assistant message.
            if last_turn_input:
                context_fill = last_turn_input
            else:
                context_fill = (usage.get("inputTokens", 0)
                                + usage.get("cacheReadInputTokens", 0)
                                + usage.get("cacheCreationInputTokens", 0)) if usage else None

            # context_window: try modelUsage first, fall back to known defaults
            context_window = usage.get("contextWindow") if usage else None
            if not context_window and model_name:
                # Sensible defaults for known model families
                if "opus" in model_name:
                    context_window = 1000000
                elif "sonnet" in model_name:
                    context_window = 1000000
                elif "haiku" in model_name:
                    context_window = 200000

            return [StreamEvent(
                type="result",
                content=result if isinstance(result, str) else "",
                session_id=data.get("session_id"),
                cost_usd=float(cost_raw) if cost_raw is not None else None,
                input_tokens=context_fill,
                output_tokens=usage.get("outputTokens") if usage else None,
                context_window=context_window,
                model=model_name,
            )]

        return []
