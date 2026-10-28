from __future__ import annotations

import asyncio
import logging
import math
import os
from pathlib import Path
import shutil
import subprocess
from urllib.parse import urlparse

import discord
from discord import app_commands

from .media import Media, youtube_url
from .player import Player


def positive_setting(name, default):
    value = int(os.environ.get(name, default))
    if value < 1:
        raise ValueError(f'{name} must be positive.')
    return value


class PCMSource(discord.FFmpegPCMAudio):
    def cleanup(self):
        # discord.py 2.7.1 can drop its stdout pipe after an already-exited
        # FFmpeg process without explicitly closing it.
        stdout = getattr(self, '_stdout', None)
        try:
            super().cleanup()
        finally:
            if stdout:
                stdout.close()


def audio_source(url):
    options = '-nostdin'
    if urlparse(url).scheme in {'http', 'https'}:
        options += ' -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -rw_timeout 20000000 -tls_verify 1'
    return PCMSource(
        url, stderr=subprocess.DEVNULL,
        before_options=options,
        options='-vn -sn -dn',
    )


class MusicBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = False
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.tree = app_commands.CommandTree(self)
        self.media = Media(Path(os.environ.get('YOUTUBE_COOKIE_FILE', 'data/youtube-cookies.txt')))
        self.players: dict[int, Player] = {}
        self.locks: dict[int, asyncio.Lock] = {}
        self.limit = positive_setting('MAX_QUEUE_SIZE', '10000')
        self.idle_timeout = positive_setting('IDLE_TIMEOUT', '300')
        self.register_commands()

    async def setup_hook(self):
        guild_id = os.environ.get('DISCORD_GUILD_ID')
        if guild_id:
            guild = discord.Object(id=int(guild_id))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

    async def on_ready(self):
        logging.info('Connected as %s; commands synced.', self.user)

    async def on_voice_state_update(self, member, before, after):
        if member.id == self.user.id and after.channel is None:
            player = self.players.get(member.guild.id)
            if player:
                await player.close()

    async def close(self):
        for player in list(self.players.values()):
            await player.close()
        # Let an in-flight yt-dlp page finish before closing its generator.
        tasks = [task for p in self.players.values() for task in (p._worker, p._importer)]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await super().close()

    def member_channel(self, interaction):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            raise ValueError('Use this command inside a server.')
        voice = interaction.user.voice
        if not voice or not voice.channel:
            raise ValueError('Join a voice channel first.')
        if isinstance(voice.channel, discord.StageChannel):
            raise ValueError('Use a regular voice channel; Stage channels are not supported.')
        return voice.channel

    def active_player(self, interaction):
        channel = self.member_channel(interaction)
        player = self.players.get(interaction.guild_id)
        if not player or player.closed:
            raise ValueError('There is no active player. Use /play first.')
        if player.voice.channel != channel:
            raise ValueError('Join the bot’s voice channel to control playback.')
        return player

    async def get_player(self, interaction):
        channel = self.member_channel(interaction)
        existing = self.players.get(interaction.guild_id)
        if existing and not existing.closed:
            return self.active_player(interaction)
        permissions = channel.permissions_for(interaction.guild.me)
        if not permissions.connect or not permissions.speak or not permissions.view_channel:
            raise ValueError('The bot needs View Channel, Connect, and Speak in your voice channel.')
        voice = interaction.guild.voice_client
        if voice:
            await voice.disconnect(force=True)
        voice = await channel.connect(timeout=30, self_deaf=True)
        text_channel = interaction.channel

        async def notify(message):
            await text_channel.send(discord.utils.escape_markdown(message), allowed_mentions=discord.AllowedMentions.none())

        player = Player(self.media, voice, notify, audio_source,
                        limit=self.limit, idle_timeout=self.idle_timeout)
        self.players[interaction.guild_id] = player
        return player

    def register_commands(self):
        @self.tree.command(name='play', description='Queue a YouTube video or import a playlist (including YouTube Music).')
        @app_commands.guild_only()
        @app_commands.describe(url='HTTPS YouTube video or playlist URL')
        async def play(interaction: discord.Interaction, url: str):
            url = youtube_url(url)
            self.member_channel(interaction)
            await interaction.response.defer(thinking=True)
            lock = self.locks.setdefault(interaction.guild_id, asyncio.Lock())
            async with lock:
                player = await self.get_player(interaction)
                player.submit(url, interaction.user.id)
            await interaction.followup.send('Import queued. Tracks will be added in playlist order; playback starts as soon as the first track is ready.')

        @self.tree.command(name='queue', description='Show the current track and a page of the shared queue.')
        @app_commands.guild_only()
        async def queue(interaction: discord.Interaction, page: app_commands.Range[int, 1] = 1):
            player = self.players.get(interaction.guild_id)
            if not player or player.closed:
                await interaction.response.send_message('The queue is empty.')
                return
            tracks = list(player.queue.tracks)
            pages = max(1, math.ceil(len(tracks) / 10))
            if page > pages:
                raise ValueError(f'Choose a page between 1 and {pages}.')
            lines = [f'Now: {player.current.title[:150] if player.current else "Nothing"}',
                     f'{len(tracks):,} queued • page {page}/{pages} • {len(player.jobs) + int(player.importing)} import(s) pending']
            for index, track in enumerate(tracks[(page - 1) * 10:page * 10], (page - 1) * 10 + 1):
                lines.append(f'{index}. {track.title[:120]} (requested by user {track.requested_by})')
            await interaction.response.send_message(discord.utils.escape_markdown('\n'.join(lines)))

        @self.tree.command(name='skip', description='Skip the current track, including a track still loading.')
        @app_commands.guild_only()
        async def skip(interaction: discord.Interaction):
            player = self.active_player(interaction)
            player.skip()
            await interaction.response.send_message('Skipped.' if player.current else 'No current track.')

        @self.tree.command(name='pause', description='Pause playback.')
        @app_commands.guild_only()
        async def pause(interaction: discord.Interaction):
            player = self.active_player(interaction)
            if not player.voice.is_playing():
                raise ValueError('No audio is currently playing.')
            player.voice.pause()
            await interaction.response.send_message('Paused.')

        @self.tree.command(name='resume', description='Resume paused playback.')
        @app_commands.guild_only()
        async def resume(interaction: discord.Interaction):
            player = self.active_player(interaction)
            if not player.voice.is_paused():
                raise ValueError('Playback is not paused.')
            player.voice.resume()
            await interaction.response.send_message('Resumed.')

        @self.tree.command(name='stop', description='Stop playback, clear the queue, and cancel pending imports.')
        @app_commands.guild_only()
        async def stop(interaction: discord.Interaction):
            self.active_player(interaction).stop()
            await interaction.response.send_message('Stopped; queue and pending imports cleared.')

        @self.tree.command(name='leave', description='Clear the queue and disconnect the bot.')
        @app_commands.guild_only()
        async def leave(interaction: discord.Interaction):
            player = self.active_player(interaction)
            await interaction.response.defer()
            await player.close()
            await interaction.followup.send('Disconnected; queue cleared.')

        @self.tree.command(name='youtube_status', description='Check whether the host has installed a YouTube cookie session.')
        @app_commands.guild_only()
        async def youtube_status(interaction: discord.Interaction):
            configured = self.media.cookie_file.is_file()
            await interaction.response.send_message(
                'A saved YouTube cookie file is configured. Validity is checked by YouTube during playback.' if configured else
                'No YouTube cookies configured. Public videos may work; ask the host to follow the README sign-in instructions if needed.',
                ephemeral=True)

        @self.tree.error
        async def on_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
            original = getattr(error, 'original', error)
            message = str(original) if isinstance(original, ValueError) else 'Command failed. Check bot permissions and host connectivity, then try again.'
            # Avoid logging raw provider exceptions, tokens, cookies or signed URLs.
            logging.warning('Command failed: %s', type(original).__name__)
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    token = os.environ.get('DISCORD_TOKEN')
    if not token:
        raise SystemExit('Set DISCORD_TOKEN in the process environment before starting the bot.')
    if not shutil.which('ffmpeg') or not shutil.which('node'):
        raise SystemExit('Install FFmpeg and Node.js 20+ before starting the bot.')
    bot = MusicBot()
    bot.run(token, log_handler=None)
