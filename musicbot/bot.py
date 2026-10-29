"""Discord commands, per-server voice ownership, and application startup."""

from __future__ import annotations

import asyncio
import logging
import math
import os
import shutil
import signal
import subprocess
from datetime import datetime, timezone
from itertools import islice

import discord
from discord import app_commands

from .audio import audio_source as audio_source
from .config import Settings
from .media import Media, youtube_url
from .player import Player


def safe_text(message: str, limit: int = 1900) -> str:
    """Bound Discord payloads after escaping provider-supplied markdown."""
    return discord.utils.escape_markdown(message)[:limit]


def title_text(title: str, limit: int = 100) -> str:
    """Keep provider titles to one bounded line in queue messages."""
    return ' '.join(title.split())[:limit]


class MusicBot(discord.Client):
    """Authorize commands and serialize voice lifecycle changes per server.

    Player handles playback; Media owns extraction subprocesses. All three layers
    must finish shutdown before the Discord client and event loop are closed.
    """

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings.from_env()
        intents = discord.Intents.default()
        intents.message_content = False
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.tree = app_commands.CommandTree(self)
        self.media = Media(self.settings.cookie_file)
        self.players: dict[int, Player] = {}
        self.locks: dict[int, asyncio.Lock] = {}
        self.limit = self.settings.queue_size
        self.idle_timeout = self.settings.idle_timeout
        self.connecting: set[int] = set()
        self.empty_tasks: dict[int, asyncio.Task] = {}
        self._shutting_down = False
        self._shutdown_task = None
        self.register_commands()

    def guild_lock(self, guild_id: int) -> asyncio.Lock:
        """Reuse one connection/lifecycle lock for each server we have served."""
        lock = self.locks.get(guild_id)
        if lock is None:
            lock = self.locks[guild_id] = asyncio.Lock()
        return lock

    def check_server(self, interaction):
        if self._shutting_down:
            raise ValueError('The bot is shutting down. Please try again shortly.')
        if not interaction.guild_id:
            raise ValueError('Use this command inside a server.')
        if (
            self.settings.allowed_guilds
            and interaction.guild_id not in self.settings.allowed_guilds
        ):
            raise ValueError('This server is not enabled by the bot host.')

    async def setup_hook(self):
        if not self.settings.sync_commands:
            return
        if self.settings.dev_guild:
            guild = discord.Object(id=self.settings.dev_guild)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()
        logging.info('Slash commands synchronized.')

    async def on_ready(self):
        logging.info('Discord connected: bot_id=%s guild_count=%s', self.user.id, len(self.guilds))

    @staticmethod
    def has_listeners(player):
        channel = player.voice.channel
        return bool(channel and any(not member.bot for member in channel.members))

    def check_empty_channel(self, guild_id, player):
        previous = self.empty_tasks.get(guild_id)
        if player.closed or self.has_listeners(player):
            self.empty_tasks.pop(guild_id, None)
            if previous:
                previous.cancel()
        elif previous is None or previous.done():
            self.empty_tasks[guild_id] = asyncio.create_task(
                self.leave_empty_channel(guild_id, player)
            )

    async def leave_empty_channel(self, guild_id, player):
        try:
            await asyncio.sleep(self.settings.empty_timeout)
            async with self.guild_lock(guild_id):
                if (
                    self.players.get(guild_id) is player
                    and not player.closed
                    and not self.has_listeners(player)
                ):
                    await player.close()
                    self.players.pop(guild_id, None)
                    logging.info('Left empty voice channel: guild_id=%s', guild_id)
        finally:
            if self.empty_tasks.get(guild_id) is asyncio.current_task():
                self.empty_tasks.pop(guild_id, None)

    async def on_voice_state_update(self, member, before, after):
        if self._shutting_down or member.guild.id not in self.players:
            return
        async with self.guild_lock(member.guild.id):
            player = self.players.get(member.guild.id)
            if not player or player.closed:
                return
            if member.id == self.user.id and after.channel is None:
                # A queued disconnect event may belong to a connection we replaced.
                if not player.voice.is_connected():
                    await player.close()
                    self.players.pop(member.guild.id, None)
                return
            if player.voice.channel in (getattr(before, 'channel', None), after.channel):
                self.check_empty_channel(member.guild.id, player)

    async def on_guild_remove(self, guild):
        async with self.guild_lock(guild.id):
            task = self.empty_tasks.pop(guild.id, None)
            if task:
                task.cancel()
            player = self.players.pop(guild.id, None)
            if player:
                await player.close()

    async def close(self):
        if self._shutdown_task is None:
            self._shutting_down = True
            self._shutdown_task = asyncio.create_task(self._shutdown(), name='bot-shutdown')
        await asyncio.shield(self._shutdown_task)

    async def _shutdown(self):
        for task in self.empty_tasks.values():
            task.cancel()
        await asyncio.gather(*self.empty_tasks.values(), return_exceptions=True)
        self.empty_tasks.clear()

        async def close_guild(guild_id):
            async with self.guild_lock(guild_id):
                player = self.players.get(guild_id)
                if player:
                    await player.close()

        try:
            async with asyncio.timeout(40):
                await asyncio.gather(
                    *(close_guild(gid) for gid in set(self.players) | self.connecting)
                )
        except TimeoutError:
            logging.error('Player shutdown exceeded deadline.')
        finally:
            self.players.clear()
            try:
                await self.media.close()
            finally:
                await super().close()

    def member_channel(self, interaction):
        self.check_server(interaction)
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
        """Caller holds the guild lock throughout connection and queue submission."""
        channel = self.member_channel(interaction)
        existing = self.players.get(interaction.guild_id)
        if existing and not existing.closed:
            return self.active_player(interaction)
        if existing:
            await existing.close()  # Old voice cleanup must finish before reconnecting.
            self.players.pop(interaction.guild_id, None)
            channel = self.member_channel(interaction)
        if (
            sum(not p.closed for p in self.players.values()) + len(self.connecting)
            >= self.settings.max_players
        ):
            raise ValueError('The bot is at its voice capacity. Please try again later.')
        permissions = channel.permissions_for(interaction.guild.me)
        if not permissions.connect or not permissions.speak or not permissions.view_channel:
            raise ValueError(
                'The bot needs View Channel, Connect, and Speak in your voice channel.'
            )
        text_channel = interaction.channel
        text_permissions = text_channel.permissions_for(interaction.guild.me)
        can_send = (
            text_permissions.send_messages_in_threads
            if isinstance(text_channel, discord.Thread)
            else text_permissions.send_messages
        )
        if not can_send or not text_permissions.view_channel:
            raise ValueError('The bot needs View Channel and Send Messages in this text channel.')
        self.connecting.add(interaction.guild_id)
        voice = None
        try:
            old_voice = interaction.guild.voice_client
            if old_voice:
                await asyncio.wait_for(old_voice.disconnect(force=True), timeout=10)
            voice = await channel.connect(timeout=25, self_deaf=True)
            # Membership may change during the voice handshake.
            if self.member_channel(interaction) != channel:
                raise ValueError('Your voice channel changed while connecting. Try /play again.')

            async def notify(message):
                await text_channel.send(
                    safe_text(message), allowed_mentions=discord.AllowedMentions.none()
                )

            player = Player(
                self.media,
                voice,
                notify,
                audio_source,
                limit=self.limit,
                idle_timeout=self.idle_timeout,
            )
            self.players[interaction.guild_id] = player
            self.check_empty_channel(interaction.guild_id, player)
            logging.info('Voice player created: guild_id=%s', interaction.guild_id)
            return player
        except BaseException:
            cleanup = voice or interaction.guild.voice_client
            if cleanup:
                try:
                    await asyncio.wait_for(cleanup.disconnect(force=True), timeout=5)
                except Exception:
                    logging.warning(
                        'Voice connection cleanup failed: guild_id=%s', interaction.guild_id
                    )
            raise
        finally:
            self.connecting.discard(interaction.guild_id)

    @staticmethod
    def unavailable_interaction(error):
        """An expired or claimed interaction cannot safely be acknowledged again."""
        return isinstance(error, discord.InteractionResponded) or (
            isinstance(error, discord.HTTPException) and error.code in {10062, 40060, 10015}
        )

    @staticmethod
    def log_unavailable_interaction(interaction, error):
        created = getattr(interaction, 'created_at', None)
        age = (
            (datetime.now(timezone.utc) - created).total_seconds()
            if isinstance(created, datetime)
            else None
        )
        logging.warning(
            'Discord interaction unavailable: code=%s command=%s guild_id=%s age_seconds=%s. '
            'Retry the command and check for duplicate bot containers/processes or Discord network delays.',
            getattr(error, 'code', None),
            getattr(interaction.command, 'name', 'unknown'),
            interaction.guild_id,
            round(age, 2) if age is not None else 'unknown',
        )

    async def acknowledge_play(self, interaction):
        """Acknowledge before validation/connection work; never replay a stale command."""
        try:
            await interaction.response.defer(thinking=True)
        except (discord.HTTPException, discord.InteractionResponded) as error:
            if not self.unavailable_interaction(error):
                raise
            self.log_unavailable_interaction(interaction, error)
            return False
        return True

    def register_commands(self):
        @self.tree.command(
            name='play',
            description='Queue a YouTube video or import a playlist (including YouTube Music).',
        )
        @app_commands.guild_only()
        @app_commands.describe(url='HTTPS YouTube video or playlist URL')
        @app_commands.checks.cooldown(
            1, self.settings.play_cooldown, key=lambda i: (i.guild_id, i.user.id)
        )
        async def play(interaction: discord.Interaction, url: str):
            if not await self.acknowledge_play(interaction):
                return
            url = youtube_url(url)
            self.member_channel(interaction)
            async with self.guild_lock(interaction.guild_id):
                player = await self.get_player(interaction)
                player.submit(url, interaction.user.id)
            await interaction.followup.send(
                'Import queued. Tracks will be added in playlist order; playback starts as soon as the first track is ready.'
            )

        @self.tree.command(
            name='queue', description='Show the current track and a page of the shared queue.'
        )
        @app_commands.guild_only()
        async def queue(interaction: discord.Interaction, page: app_commands.Range[int, 1] = 1):
            self.check_server(interaction)
            player = self.players.get(interaction.guild_id)
            if not player or player.closed:
                await interaction.response.send_message('The queue is empty.')
                return
            tracks = player.queue.tracks
            pages = max(1, math.ceil(len(tracks) / 10))
            if page > pages:
                raise ValueError(f'Choose a page between 1 and {pages}.')
            lines = [
                f'Now: {title_text(player.current.title) if player.current else "Nothing"}',
                f'{len(tracks):,} queued • page {page}/{pages} • {len(player.jobs) + int(player.importing)} import(s) pending',
            ]
            for index, track in enumerate(
                islice(tracks, (page - 1) * 10, page * 10), (page - 1) * 10 + 1
            ):
                lines.append(
                    f'{index}. {title_text(track.title, 80)} (requested by user {track.requested_by})'
                )
            await interaction.response.send_message(safe_text('\n'.join(lines)))

        @self.tree.command(
            name='skip', description='Skip the current track, including a track still loading.'
        )
        @app_commands.guild_only()
        async def skip(interaction: discord.Interaction):
            player = self.active_player(interaction)
            player.skip()
            await interaction.response.send_message(
                'Skipped.' if player.current else 'No current track.'
            )

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

        @self.tree.command(
            name='stop', description='Stop playback, clear the queue, and cancel pending imports.'
        )
        @app_commands.guild_only()
        async def stop(interaction: discord.Interaction):
            self.active_player(interaction).stop()
            await interaction.response.send_message('Stopped; queue and pending imports cleared.')

        @self.tree.command(name='leave', description='Clear the queue and disconnect the bot.')
        @app_commands.guild_only()
        async def leave(interaction: discord.Interaction):
            await interaction.response.defer()
            async with self.guild_lock(interaction.guild_id):
                player = self.active_player(interaction)
                await player.close()
                self.players.pop(interaction.guild_id, None)
            await interaction.followup.send('Disconnected; queue cleared.')

        @self.tree.command(
            name='youtube_status',
            description='Check whether the host has installed a YouTube cookie session.',
        )
        @app_commands.guild_only()
        async def youtube_status(interaction: discord.Interaction):
            self.check_server(interaction)
            configured = self.media.cookie_file.is_file()
            await interaction.response.send_message(
                'A saved YouTube cookie file is configured. Validity is checked by YouTube during playback.'
                if configured
                else 'No YouTube cookies configured. Public videos may work; ask the host to follow the README sign-in instructions if needed.',
                ephemeral=True,
            )

        @self.tree.error
        async def on_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
            original = getattr(error, 'original', error)
            if self.unavailable_interaction(original):
                self.log_unavailable_interaction(interaction, original)
                return
            message = (
                str(original)
                if isinstance(original, ValueError)
                else 'Command failed. Check bot permissions and host connectivity, then try again.'
            )
            if isinstance(original, app_commands.CommandOnCooldown):
                message = (
                    f'Please wait {math.ceil(original.retry_after)} seconds before importing again.'
                )
            # Avoid logging raw provider exceptions, tokens, cookies or signed URLs.
            logging.warning(
                'Command failed: type=%s guild_id=%s command=%s',
                type(original).__name__,
                interaction.guild_id,
                getattr(interaction.command, 'name', 'unknown'),
            )
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(safe_text(message), ephemeral=True)
                else:
                    await interaction.response.send_message(safe_text(message), ephemeral=True)
            except (discord.HTTPException, discord.InteractionResponded) as response_error:
                # The error reply may itself outlive the interaction deadline.
                # Never allow another exception to escape CommandTree.on_error.
                if self.unavailable_interaction(response_error):
                    self.log_unavailable_interaction(interaction, response_error)
                else:
                    logging.warning(
                        'Could not deliver command error: type=%s code=%s',
                        type(response_error).__name__,
                        getattr(response_error, 'code', None),
                    )


async def serve(bot, token):
    """SIGINT/SIGTERM both unwind the client context and reap player resources."""
    loop = asyncio.get_running_loop()
    current = asyncio.current_task()
    installed = []
    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(signum, current.cancel)
            installed.append(signum)
        except (NotImplementedError, RuntimeError):
            pass  # Windows uses asyncio.run's default Ctrl-C handler.
    try:
        async with bot:
            await bot.start(token)
    finally:
        for signum in installed:
            loop.remove_signal_handler(signum)


def validate_runtime():
    if not shutil.which('ffmpeg') or not shutil.which('node'):
        raise ValueError('Install FFmpeg and Node.js 20+ before starting the bot.')
    result = subprocess.run(
        ['node', '--version'], capture_output=True, text=True, timeout=5, check=True
    )
    if int(result.stdout.strip().lstrip('v').split('.')[0]) < 20:
        raise ValueError('Node.js 20+ is required for YouTube extraction.')
    discord.opus.Encoder()  # Fail at startup if libopus cannot be loaded.


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    token = os.environ.get('DISCORD_TOKEN')
    if not token:
        raise SystemExit('Set DISCORD_TOKEN in the process environment before starting the bot.')
    try:
        settings = Settings.from_env()
        validate_runtime()
    except (ValueError, OSError, subprocess.SubprocessError, discord.opus.OpusNotLoaded) as error:
        raise SystemExit(f'Invalid bot setup: {error}') from None
    try:
        asyncio.run(serve(MusicBot(settings), token))
    except (KeyboardInterrupt, asyncio.CancelledError):
        logging.info('Bot stopped.')
    except discord.LoginFailure:
        raise SystemExit('Discord rejected the configured bot token.') from None
