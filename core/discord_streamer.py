import asyncio
import logging
import time
from collections.abc import Callable

import discord

log = logging.getLogger(__name__)

CHAR_LIMIT = 1900
EDIT_INTERVAL = 0.3  # seconds between edits


class StopView(discord.ui.View):
    """A persistent view with a Stop button that cancels a running Claude process."""

    def __init__(self, on_cancel: Callable[[], bool]) -> None:
        super().__init__(timeout=None)
        self._on_cancel = on_cancel

    @discord.ui.button(label="Stop", style=discord.ButtonStyle.danger, emoji="\u23f9")
    async def stop_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self._on_cancel():
            button.label = "Stopping..."
            button.disabled = True
            await interaction.response.edit_message(view=self)
        else:
            await interaction.response.send_message("Nothing is running.", ephemeral=True)


class DiscordStreamer:
    def __init__(self, channel: discord.abc.Messageable, on_cancel: Callable[[], bool] | None = None) -> None:
        self.channel = channel
        self.current_message: discord.Message | None = None
        self.current_text: str = ""
        self.all_messages: list[discord.Message] = []
        self._buffer: str = ""
        self._buffer_dirty: bool = False
        self._last_edit: float = 0
        self._lock = asyncio.Lock()
        self._finalized = False
        self._on_cancel = on_cancel
        self._stop_view: StopView | None = None

    async def start(self) -> None:
        if self._on_cancel:
            self._stop_view = StopView(self._on_cancel)
            self.current_message = await self.channel.send("*Thinking...*", view=self._stop_view)
        else:
            self.current_message = await self.channel.send("*Thinking...*")
        self.all_messages.append(self.current_message)
        self.current_text = ""

    async def feed(self, text: str) -> None:
        if self._finalized:
            return

        async with self._lock:
            self._buffer += text
            self._buffer_dirty = True

            # Check if we need to flush
            now = time.monotonic()
            if now - self._last_edit >= EDIT_INTERVAL:
                await self._flush()

    async def tick(self) -> None:
        """Call periodically to flush buffered content."""
        if self._finalized or not self._buffer_dirty:
            return
        async with self._lock:
            now = time.monotonic()
            if self._buffer_dirty and now - self._last_edit >= EDIT_INTERVAL:
                await self._flush()

    async def finalize(self) -> None:
        if self._finalized:
            return
        self._finalized = True
        async with self._lock:
            if self._buffer:
                await self._flush(force=True)
            # Final edit — remove the stop button
            if self.current_message and self.current_text:
                try:
                    await self.current_message.edit(content=self.current_text, view=None)
                except discord.HTTPException:
                    pass
        self._cleanup_view()

    async def send_cancelled(self) -> None:
        self._finalized = True
        async with self._lock:
            if self._buffer:
                await self._flush(force=True)
        suffix = "\n\n*Cancelled.*"
        if self.current_message:
            try:
                await self.current_message.edit(
                    content=(self.current_text + suffix) if self.current_text else "*Cancelled.*",
                    view=None,
                )
            except discord.HTTPException:
                await self.channel.send("*Cancelled.*")
        else:
            await self.channel.send("*Cancelled.*")
        self._cleanup_view()

    async def send_error(self, error_text: str) -> None:
        self._finalized = True
        content = f"**Error:** {error_text}"
        if len(content) > CHAR_LIMIT:
            content = content[:CHAR_LIMIT - 3] + "..."
        if self.current_message and not self.current_text:
            # No streamed text yet — replace the placeholder message with the error
            try:
                await self.current_message.edit(content=content, view=None)
            except discord.HTTPException:
                await self.channel.send(content)
        else:
            # Streamed text exists — remove the stop button from it, then send error separately
            if self.current_message:
                try:
                    await self.current_message.edit(content=self.current_text, view=None)
                except discord.HTTPException:
                    pass
            await self.channel.send(content)
        self._cleanup_view()

    def _cleanup_view(self) -> None:
        if self._stop_view:
            self._stop_view.stop()
            self._stop_view = None

    async def _flush(self, force: bool = False) -> None:
        if not self._buffer_dirty and not force:
            return

        pending = self._buffer
        self._buffer = ""
        self._buffer_dirty = False

        # Keep chunking until all pending text is placed
        while len(self.current_text) + len(pending) > CHAR_LIMIT:
            remaining_space = CHAR_LIMIT - len(self.current_text)

            if remaining_space > 0:
                # Try to split at a newline boundary
                split_at = pending[:remaining_space].rfind("\n")
                if split_at == -1:
                    split_at = remaining_space

                first_part = pending[:split_at]
                pending = pending[split_at:]
            else:
                first_part = ""

            # Handle code block continuity
            if first_part:
                self.current_text += first_part
            closed_text, reopen_prefix = self._handle_code_block_split(self.current_text)
            self.current_text = closed_text

            # Edit the current message with what fits, removing the stop button
            if self.current_message:
                try:
                    await self.current_message.edit(content=self.current_text or "\u200b", view=None)
                except discord.HTTPException as e:
                    log.warning("Failed to edit message: %s", e)

            # Start a new message for the overflow, with the stop button
            self.current_text = reopen_prefix
            self.current_message = await self.channel.send(
                "\u200b",
                view=self._stop_view if self._stop_view else discord.utils.MISSING,
            )
            self.all_messages.append(self.current_message)

        # Remaining pending fits in the current message
        self.current_text += pending
        await self._edit_current(self.current_text)

        self._last_edit = time.monotonic()

    async def _edit_current(self, text: str) -> None:
        if not self.current_message:
            return
        try:
            await self.current_message.edit(content=text or "\u200b")
        except discord.HTTPException as e:
            log.warning("Failed to edit message: %s", e)

    def _handle_code_block_split(self, text: str) -> tuple[str, str]:
        """If text has an unclosed code block, close it and return the reopen prefix."""
        # Count triple backticks
        count = text.count("```")
        if count % 2 == 0:
            # All code blocks are closed
            return text, ""

        # Find the language tag of the last opening ```
        last_open = text.rfind("```")
        after_ticks = text[last_open + 3:]
        lang = ""
        if after_ticks and not after_ticks.startswith("\n"):
            lang_end = after_ticks.find("\n")
            if lang_end != -1:
                lang = after_ticks[:lang_end].strip()

        # Close the block in current text
        closed = text + "\n```"
        # Reopen in next message
        reopen = f"```{lang}\n"
        return closed, reopen
