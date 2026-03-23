import asyncio
import io
import logging
import os

import discord

log = logging.getLogger(__name__)


class VoiceNotifier:
    """Generates audio via ElevenLabs and plays it in a configured Discord voice channel.

    Configuration via environment variables:
      NOTIFY_VOICE_CHANNEL_ID  — Discord voice channel ID to join
      ELEVENLABS_API_KEY       — ElevenLabs API key
      ELEVENLABS_VOICE_ID      — Voice ID for TTS (default: JBFqnCBsd6RMkjVDRZzb)

    Routing:
      - Prompt starts with "speak:" → ElevenLabs TTS endpoint (speech)
      - Everything else             → ElevenLabs Sound Effects endpoint (sfx/ambient/music)

    All errors are logged and swallowed — audio is best-effort and never blocks the bot.
    """

    def __init__(self, bot) -> None:
        self._bot = bot
        # Serialize concurrent play_prompt calls so we don't stomp the voice client
        self._lock = asyncio.Lock()

    def _elevenlabs_client(self, api_key: str):
        from elevenlabs.client import ElevenLabs
        return ElevenLabs(api_key=api_key)

    def _route(self, prompt: str) -> tuple[str, str]:
        """Returns ('tts', text) or ('sfx', description)."""
        if prompt.lower().startswith("speak:"):
            return "tts", prompt[6:].strip()
        return "sfx", prompt

    async def _generate_audio(self, prompt: str, api_key: str, voice_id: str) -> bytes | None:
        """Generate audio bytes from the prompt. Runs ElevenLabs SDK in a thread pool."""
        kind, content = self._route(prompt)
        try:
            client = self._elevenlabs_client(api_key)
            if kind == "tts":
                chunks = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: b"".join(client.text_to_speech.convert(
                        text=content,
                        voice_id=voice_id,
                        model_id="eleven_turbo_v2_5",
                        output_format="mp3_44100_128",
                    )),
                )
            else:
                chunks = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: b"".join(client.text_to_sound_effects.convert(
                        text=content,
                        output_format="mp3_44100_128",
                    )),
                )
            return chunks
        except Exception:
            log.exception("ElevenLabs audio generation failed for prompt: %r", prompt[:80])
            return None

    async def play_prompt(self, guild: discord.Guild, prompt: str) -> None:
        """Generate and play audio for a [play-audio:] marker prompt."""
        channel_id = os.getenv("NOTIFY_VOICE_CHANNEL_ID")
        api_key = os.getenv("ELEVENLABS_API_KEY")
        voice_id = os.getenv("ELEVENLABS_VOICE_ID", "JBFqnCBsd6RMkjVDRZzb")

        if not channel_id or not api_key:
            log.debug("Voice notify skipped — NOTIFY_VOICE_CHANNEL_ID or ELEVENLABS_API_KEY not set.")
            return

        voice_channel = guild.get_channel(int(channel_id))
        if not isinstance(voice_channel, discord.VoiceChannel):
            log.warning("NOTIFY_VOICE_CHANNEL_ID %s is not a VoiceChannel.", channel_id)
            return

        audio_bytes = await self._generate_audio(prompt, api_key, voice_id)
        if not audio_bytes:
            return

        async with self._lock:
            await self._play(guild, voice_channel, audio_bytes)

    async def voice_event(self, guild: discord.Guild, event: str, message: str) -> None:
        """Play a canned TTS notification for an autonomous bot event."""
        from core.state import load_config
        enabled_events = load_config().get("voice_events", [])
        if event not in enabled_events:
            return
        await self.play_prompt(guild, f"speak: {message}")

    async def _play(
        self,
        guild: discord.Guild,
        voice_channel: discord.VoiceChannel,
        audio_bytes: bytes,
    ) -> None:
        """Connect, play audio from memory, then disconnect."""
        voice_client: discord.VoiceClient | None = None
        try:
            # Disconnect any stale connection first
            if guild.voice_client:
                await guild.voice_client.disconnect(force=True)

            voice_client = await voice_channel.connect()
            done = asyncio.Event()

            def after(error):
                if error:
                    log.warning("Voice playback error: %s", error)
                done.set()

            source = discord.FFmpegPCMAudio(io.BytesIO(audio_bytes), pipe=True)
            voice_client.play(source, after=after)
            await asyncio.wait_for(done.wait(), timeout=60)

        except asyncio.TimeoutError:
            log.warning("Voice playback timed out.")
        except Exception:
            log.exception("Voice playback failed.")
        finally:
            if voice_client and voice_client.is_connected():
                await voice_client.disconnect(force=True)
