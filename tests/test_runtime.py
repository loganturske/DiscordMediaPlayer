import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import discord

from musicbot.audio import AudioPlaybackError
from musicbot.bot import MusicBot, audio_source
from musicbot.config import Settings


class BotTests(unittest.TestCase):
    def setUp(self):
        self.bot = MusicBot(Settings.from_env({}))

    def test_commands_registered(self):
        self.assertEqual(
            {c.name for c in self.bot.tree.get_commands()},
            {'play', 'queue', 'skip', 'pause', 'resume', 'stop', 'leave', 'youtube_status'},
        )
        self.assertTrue(all(c.guild_only for c in self.bot.tree.get_commands()))

    def test_any_voice_member_can_control_but_other_channels_cannot(self):
        channel = object()
        user = Mock(spec=discord.Member)
        user.voice = SimpleNamespace(channel=channel)
        interaction = SimpleNamespace(guild=object(), guild_id=1, user=user)
        player = SimpleNamespace(closed=False, voice=SimpleNamespace(channel=channel))
        self.bot.players[1] = player
        self.assertIs(self.bot.active_player(interaction), player)
        user.voice = SimpleNamespace(channel=object())
        with self.assertRaisesRegex(ValueError, 'bot’s voice channel'):
            self.bot.active_player(interaction)

    def test_guilds_cannot_control_each_other(self):
        user = Mock(spec=discord.Member)
        user.voice = SimpleNamespace(channel=object())
        self.bot.players[1] = SimpleNamespace(closed=False)
        with self.assertRaisesRegex(ValueError, 'no active player'):
            self.bot.active_player(SimpleNamespace(guild=object(), guild_id=2, user=user))

    @unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg is not installed')
    def test_real_ffmpeg_pcm_and_discord_opus_encoding(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'tone.wav'
            subprocess.run(
                [
                    'ffmpeg',
                    '-v',
                    'error',
                    '-f',
                    'lavfi',
                    '-i',
                    'sine=frequency=440:duration=0.1',
                    str(path),
                ],
                check=True,
            )
            source = audio_source(str(path))
            try:
                frame = source.read()
                self.assertEqual(len(frame), 3840)  # 20 ms stereo 48 kHz PCM
                encoder = discord.opus.Encoder()
                self.assertTrue(encoder.encode(frame, encoder.SAMPLES_PER_FRAME))
            finally:
                source.cleanup()

    @unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg is not installed')
    def test_ffmpeg_failure_has_no_broken_stderr_thread(self):
        source = audio_source('/nonexistent-discord-media-test-file.wav')
        try:
            with self.assertRaises(AudioPlaybackError):
                source.read()
            self.assertIsNone(source._pipe_reader_thread)
            source._process.wait(timeout=5)
            self.assertNotEqual(source._process.returncode, 0)
        finally:
            source.cleanup()
