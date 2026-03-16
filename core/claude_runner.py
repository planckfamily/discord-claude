import asyncio
import json
import logging
import os
from collections.abc import AsyncGenerator
from pathlib import Path

from core.system_prompt import build_append_system_prompt, ensure_caches
from models.session import StreamEvent

log = logging.getLogger(__name__)


class ClaudeRunner:
    def __init__(self) -> None:
        # channel/thread id -> running subprocess
        self._active: dict[int, asyncio.subprocess.Process] = {}
        self._cancelled: set[int] = set()
        ensure_caches()


    def is_busy(self, thread_id: int) -> bool:
        proc = self._active.get(thread_id)
        return proc is not None and proc.returncode is None

    def cancel(self, thread_id: int) -> bool:
        proc = self._active.get(thread_id)
        if proc and proc.returncode is None:
            self._cancelled.add(thread_id)
            proc.kill()
            return True
        return False

    async def cancel_all(self) -> None:
        for thread_id, proc in list(self._active.items()):
            if proc.returncode is None:
                proc.kill()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
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
    ) -> AsyncGenerator[StreamEvent, None]:
        if self.is_busy(thread_id):
            yield StreamEvent(type="error", content="Claude is already running for this project.")
            return

        # Validate session before using it
        if session_id and not self._session_exists(session_id, project_dir):
            log.info("Session %s not found on disk, starting fresh.", session_id)
            session_id = None

        cmd = ["claude", "-p", "--verbose", "--output-format", "stream-json", "--dangerously-skip-permissions"]

        if resume and session_id:
            cmd.extend(["--resume", session_id])
        elif session_id:
            cmd.extend(["--continue", session_id])

        cmd.extend(["--append-system-prompt", build_append_system_prompt()])

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
            self._active[thread_id] = proc

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
                for event in self._parse_line(line, has_emitted_text):
                    if event.type == "text" and event.content.strip():
                        has_emitted_text = True
                    yield event

        # Process any remaining buffer
        if buffer.strip():
            for event in self._parse_line(buffer.strip(), has_emitted_text):
                yield event

    def _parse_line(self, line: bytes, has_emitted_text: bool = False) -> list[StreamEvent]:
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            log.warning("Skipping malformed JSON line: %s", line[:200])
            return []

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

            # modelUsage aggregates across all API turns in the invocation.
            # For context-window fill we want the LAST turn's input tokens,
            # which is roughly total / num_turns for multi-turn (tool-use) calls.
            total_in = (usage.get("inputTokens", 0)
                        + usage.get("cacheReadInputTokens", 0)
                        + usage.get("cacheCreationInputTokens", 0)) if usage else None

            # Estimate last-turn context size for multi-turn invocations
            if total_in and num_turns > 1:
                # Each successive turn is slightly larger than the last, but
                # dividing gives a reasonable lower-bound estimate.
                estimated_context = total_in // num_turns
            else:
                estimated_context = total_in

            # context_window: try modelUsage first, fall back to known defaults
            context_window = usage.get("contextWindow") if usage else None
            if not context_window and model_name:
                # Sensible defaults for known model families
                if "opus" in model_name:
                    context_window = 200000
                elif "sonnet" in model_name:
                    context_window = 200000
                elif "haiku" in model_name:
                    context_window = 200000

            return [StreamEvent(
                type="result",
                content=result if isinstance(result, str) else "",
                session_id=data.get("session_id"),
                cost_usd=float(cost_raw) if cost_raw is not None else None,
                input_tokens=estimated_context,
                output_tokens=usage.get("outputTokens") if usage else None,
                context_window=context_window,
                model=model_name,
            )]

        return []
