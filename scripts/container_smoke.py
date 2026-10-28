"""Offline runtime validation: python -m scripts.container_smoke.

Run inside the built container. This checks installed tools and writable state
without signing in to Discord, contacting YouTube, or reading existing cookies.
All generated audio and synthetic cookie fixtures are removed before returning.
"""

from __future__ import annotations

import asyncio
import http.cookiejar
import os
import subprocess
import tempfile
from pathlib import Path

import discord

from musicbot.bot import MusicBot, audio_source, validate_runtime
from musicbot.config import Settings
from scripts.import_cookies import install

EXPECTED_COMMANDS = {'play', 'queue', 'skip', 'pause', 'resume', 'stop', 'leave', 'youtube_status'}


def check_non_root() -> None:
    """The shipped Linux image must run as an unprivileged user."""
    if not hasattr(os, 'geteuid') or os.geteuid() == 0:
        raise RuntimeError('Run the smoke check as the image default non-root user.')


def check_audio() -> None:
    """Decode a generated tone with FFmpeg and encode one Discord Opus packet."""
    with tempfile.TemporaryDirectory(prefix='discord-audio-check-') as temporary:
        tone = Path(temporary) / 'tone.wav'
        subprocess.run(
            [
                'ffmpeg',
                '-nostdin',
                '-v',
                'error',
                '-f',
                'lavfi',
                '-i',
                'sine=frequency=440:duration=0.1',
                str(tone),
            ],
            check=True,
            timeout=10,
        )
        source = audio_source(str(tone))
        try:
            frame = source.read()
            if len(frame) != 3840:
                raise RuntimeError('FFmpeg did not produce a complete 20 ms PCM frame.')
            encoder = discord.opus.Encoder()
            if not encoder.encode(frame, encoder.SAMPLES_PER_FRAME):
                raise RuntimeError('The Discord Opus encoder produced no packet.')
        finally:
            source.cleanup()


def check_state(state_directory: Path) -> None:
    """Import and reopen a synthetic session beneath the configured state path."""
    # Use a unique child directory, never the real cookie file. Reopening proves
    # disk writes work; retaining this directory across containers is Compose's job.
    with tempfile.TemporaryDirectory(prefix='.container-smoke-', dir=state_directory) as temporary:
        root = Path(temporary)
        source, destination = root / 'export.txt', root / 'private/cookies.txt'
        source.write_text(
            '# Netscape HTTP Cookie File\n'
            '.youtube.com\tTRUE\t/\tTRUE\t2147483647\tSID\tsynthetic-smoke-session\n'
        )
        install(source, destination)
        if destination.stat().st_mode & 0o777 != 0o600:
            raise RuntimeError('The imported cookie file must have mode 0600.')
        reopened = http.cookiejar.MozillaCookieJar(str(destination))
        reopened.load(ignore_discard=True, ignore_expires=True)
        if not any(cookie.value == 'synthetic-smoke-session' for cookie in reopened):
            raise RuntimeError('The synthetic cookie file could not be reopened.')


async def check_commands(settings: Settings) -> None:
    """Construct command definitions without connecting the Discord client."""
    bot = MusicBot(settings)
    try:
        if {command.name for command in bot.tree.get_commands()} != EXPECTED_COMMANDS:
            raise RuntimeError('Expected Discord slash commands are missing.')
    finally:
        await bot.close()


def main() -> None:
    settings = Settings.from_env()
    check_non_root()
    validate_runtime()
    check_audio()
    check_state(settings.cookie_file.parent)
    asyncio.run(check_commands(settings))
    print('Container smoke check passed: non-root, runtime tools, audio, commands, private state.')
    print('Discord connectivity, audible voice playback, and YouTube sign-in require a live check.')


if __name__ == '__main__':
    main()
