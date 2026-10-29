import asyncio
import unittest
from collections import deque
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord
from discord import app_commands

from musicbot.bot import MusicBot, safe_text, serve, validate_runtime
from musicbot.config import Settings
from musicbot.media import Track


class BotAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = MusicBot(Settings.from_env({}))
        self.channel = Mock()
        self.channel.permissions_for.return_value = SimpleNamespace(
            connect=True, speak=True, view_channel=True
        )
        self.voice = Mock()
        self.voice.channel = self.channel
        self.voice.is_connected.return_value = True
        self.voice.disconnect = AsyncMock()
        self.channel.connect = AsyncMock(return_value=self.voice)
        self.member = Mock(spec=discord.Member)
        self.member.id, self.member.bot = 123, False
        self.member.voice = SimpleNamespace(channel=self.channel)
        self.channel.members = [self.member]
        self.text = Mock()
        self.text.permissions_for.return_value = SimpleNamespace(
            send_messages=True, view_channel=True
        )
        self.text.send = AsyncMock()
        self.guild = SimpleNamespace(id=1, me=object(), voice_client=None)
        self.interaction = SimpleNamespace(
            guild=self.guild,
            guild_id=1,
            user=self.member,
            channel=self.text,
            command=None,
            response=AsyncMock(),
            followup=AsyncMock(),
        )
        self.interaction.response.is_done = Mock(return_value=False)

    async def asyncTearDown(self):
        await self.bot.close()

    async def command(self, name, *args, **kwargs):
        cmd = self.bot.tree.get_command(name)
        self.interaction.command = cmd
        return await cmd.callback(self.interaction, *args, **kwargs)

    async def player(self):
        return await self.bot.get_player(self.interaction)

    async def test_connect_validates_permissions_and_membership(self):
        self.channel.permissions_for.return_value.speak = False
        with self.assertRaisesRegex(ValueError, 'Speak'):
            await self.player()
        self.channel.connect.assert_not_awaited()
        self.channel.permissions_for.return_value.speak = True
        self.text.permissions_for.return_value.send_messages = False
        with self.assertRaisesRegex(ValueError, 'Send Messages'):
            await self.player()
        self.text.permissions_for.return_value.send_messages = True

        async def move_during_connect(**kwargs):
            self.member.voice.channel = object()
            return self.voice

        self.channel.connect.side_effect = move_during_connect
        with self.assertRaisesRegex(ValueError, 'changed'):
            await self.player()
        self.voice.disconnect.assert_awaited_once()
        self.assertFalse(self.bot.connecting)
        self.assertFalse(self.bot.players)

    async def test_failed_voice_handshake_cleans_partial_connection(self):
        self.channel.connect.side_effect = TimeoutError
        self.guild.voice_client = self.voice
        with self.assertRaises(TimeoutError):
            await self.player()
        self.assertEqual(self.voice.disconnect.await_count, 2)
        self.assertFalse(self.bot.connecting)

    async def test_play_submits_only_after_connect_and_reuses_player(self):
        async def slow_import(*args):
            await asyncio.Event().wait()
            yield  # pragma: no cover

        self.bot.media.import_tracks = slow_import
        await self.command('play', 'https://youtu.be/abc')
        player = self.bot.players[1]
        self.assertEqual(player.jobs[0].requester, 123)
        await self.command('play', 'https://youtu.be/xyz')
        self.assertEqual(len(player.jobs), 2)
        self.channel.connect.assert_awaited_once()
        self.interaction.followup.send.assert_awaited()

    async def test_player_capacity_includes_connections_in_progress(self):
        self.bot.settings = replace(self.bot.settings, max_players=1)
        self.bot.connecting.add(2)
        with self.assertRaisesRegex(ValueError, 'capacity'):
            await self.player()
        self.bot.connecting.clear()

    async def test_allowlist_and_shutting_down_block_commands(self):
        self.bot.settings = replace(self.bot.settings, allowed_guilds=frozenset({2}))
        for command, args in [
            ('play', ('https://youtu.be/abc',)),
            ('queue', ()),
            ('youtube_status', ()),
        ]:
            with self.assertRaisesRegex(ValueError, 'not enabled'):
                await self.command(command, *args)
        self.bot.settings = replace(self.bot.settings, allowed_guilds=frozenset())
        self.member.voice = None
        with self.assertRaisesRegex(ValueError, 'Join'):
            self.bot.member_channel(self.interaction)
        await self.bot.close()
        with self.assertRaisesRegex(ValueError, 'shutting down'):
            await self.command('play', 'https://youtu.be/abc')

    async def test_queue_payload_fits_discord_with_hostile_titles(self):
        player = await self.player()
        player.current = Track('url', '*' * 150, 1)
        player.queue.tracks = deque(Track('url', '*' * 300, 2**64 - 1) for _ in range(20))
        await self.command('queue')
        message = self.interaction.response.send_message.call_args.args[0]
        self.assertLessEqual(len(message), 1900)
        self.assertIn('page 1/2', message)
        with self.assertRaisesRegex(ValueError, 'between'):
            await self.command('queue', page=3)
        await self.command('stop')
        self.assertFalse(player.queue.tracks)

    async def test_pause_resume_skip_leave(self):
        player = await self.player()
        self.voice.is_playing.return_value = False
        with self.assertRaisesRegex(ValueError, 'No audio'):
            await self.command('pause')
        self.voice.is_playing.return_value = True
        await self.command('pause')
        self.voice.pause.assert_called_once()
        self.voice.is_paused.return_value = False
        with self.assertRaisesRegex(ValueError, 'not paused'):
            await self.command('resume')
        self.voice.is_paused.return_value = True
        await self.command('resume')
        self.voice.resume.assert_called_once()
        await self.command('skip')
        await self.command('leave')
        self.assertTrue(player.closed)
        self.assertNotIn(1, self.bot.players)
        self.voice.disconnect.assert_awaited()

    async def test_queue_and_cookie_status_without_player(self):
        await self.command('queue')
        self.interaction.response.send_message.assert_awaited_with('The queue is empty.')
        await self.command('youtube_status')
        self.assertTrue(self.interaction.response.send_message.call_args.kwargs['ephemeral'])

    async def test_setup_sync_can_be_disabled_or_guild_scoped(self):
        with patch.object(self.bot.tree, 'sync', new_callable=AsyncMock) as sync:
            self.bot.settings = replace(self.bot.settings, sync_commands=False)
            await self.bot.setup_hook()
            sync.assert_not_awaited()
            self.bot.settings = replace(self.bot.settings, sync_commands=True, dev_guild=42)
            await self.bot.setup_hook()
            self.assertEqual(sync.call_args.kwargs['guild'].id, 42)
            self.bot.settings = replace(self.bot.settings, dev_guild=None)
            await self.bot.setup_hook()
            self.assertEqual(sync.call_args.kwargs, {})

    async def test_server_removal_closes_player(self):
        player = await self.player()
        await self.bot.on_guild_remove(self.guild)
        self.assertTrue(player.closed)
        self.assertFalse(self.bot.players)

    async def test_empty_channel_disconnect_and_listener_return(self):
        self.bot.settings = replace(self.bot.settings, empty_timeout=0.01)
        player = await self.player()
        self.channel.members = []
        self.bot.check_empty_channel(1, player)
        task = self.bot.empty_tasks[1]
        self.channel.members = [self.member]
        self.bot.check_empty_channel(1, player)
        await asyncio.gather(task, return_exceptions=True)
        self.assertFalse(player.closed)
        self.channel.members = []
        self.bot.check_empty_channel(1, player)
        await self.bot.empty_tasks[1]
        self.assertTrue(player.closed)
        self.assertFalse(self.bot.players)

    async def test_voice_events_ignore_stale_disconnect(self):
        player = await self.player()
        self.bot._connection.user = SimpleNamespace(id=99)
        member = SimpleNamespace(id=99, guild=self.guild)
        await self.bot.on_voice_state_update(member, None, SimpleNamespace(channel=None))
        self.assertFalse(player.closed)
        self.voice.is_connected.return_value = False
        await self.bot.on_voice_state_update(member, None, SimpleNamespace(channel=None))
        self.assertTrue(player.closed)

    async def test_reconnect_waits_for_previous_shutdown(self):
        previous = SimpleNamespace(closed=True, close=AsyncMock())

        async def closed():
            self.assertEqual(self.channel.connect.await_count, 0)

        previous.close.side_effect = closed
        self.bot.players[1] = previous
        await self.player()
        previous.close.assert_awaited_once()
        self.channel.connect.assert_awaited_once()

    async def test_empty_timer_not_postponed_by_events(self):
        player = await self.player()
        self.channel.members = []
        self.bot.check_empty_channel(1, player)
        original = self.bot.empty_tasks[1]
        self.bot.check_empty_channel(1, player)
        self.assertIs(self.bot.empty_tasks[1], original)
        self.voice.channel = None
        self.assertFalse(self.bot.has_listeners(player))

    async def test_concurrent_shutdown_callers_wait_for_disconnect(self):
        await self.player()
        gate, entered = asyncio.Event(), asyncio.Event()

        async def disconnect():
            entered.set()
            await gate.wait()

        self.voice.disconnect.side_effect = disconnect
        first = asyncio.create_task(self.bot.close())
        await entered.wait()
        second = asyncio.create_task(self.bot.close())
        await asyncio.sleep(0)
        self.assertFalse(first.done())
        self.assertFalse(second.done())
        gate.set()
        await asyncio.gather(first, second)
        self.voice.disconnect.assert_awaited_once()

    async def test_cooldown_is_per_server_and_user(self):
        command = self.bot.tree.get_command('play')
        self.interaction.created_at = discord.utils.utcnow()
        self.assertTrue(await command._check_can_run(self.interaction))
        with self.assertRaises(app_commands.CommandOnCooldown):
            await command._check_can_run(self.interaction)
        self.interaction.guild_id = 2
        self.assertTrue(await command._check_can_run(self.interaction))

    async def test_error_messages_do_not_echo_provider_secrets(self):
        error = app_commands.CommandInvokeError(
            self.bot.tree.get_command('play'), RuntimeError('secret-stream-url')
        )
        await self.bot.tree.on_error(self.interaction, error)
        self.assertNotIn(
            'secret-stream-url', self.interaction.response.send_message.call_args.args[0]
        )
        self.interaction.response.is_done.return_value = True
        error = app_commands.CommandOnCooldown(app_commands.Cooldown(1, 5), 4.5)
        await self.bot.tree.on_error(self.interaction, error)
        self.assertIn('5 seconds', self.interaction.followup.send.call_args.args[0])

    @staticmethod
    def discord_http_error(code=10062, status=404):
        response = SimpleNamespace(status=status, reason='test error')
        return discord.HTTPException(response, {'code': code, 'message': 'private-response-text'})

    async def test_rejected_acknowledgement_does_not_connect_or_import(self):
        for code in (10062, 40060):
            with self.subTest(code=code):
                self.interaction.response.defer.side_effect = self.discord_http_error(code)
                await self.command('play', 'https://youtu.be/abc')
        self.channel.connect.assert_not_awaited()
        self.interaction.response.send_message.assert_not_awaited()
        self.interaction.followup.send.assert_not_awaited()
        self.assertFalse(self.bot.players)

    async def test_play_acknowledges_before_url_validation(self):
        with self.assertRaises(ValueError):
            await self.command('play', 'not-a-youtube-link')
        self.interaction.response.defer.assert_awaited_once()
        self.channel.connect.assert_not_awaited()

    async def test_unavailable_interaction_error_is_not_replied_to(self):
        for code in (10062, 40060, 10015):
            error = app_commands.CommandInvokeError(
                self.bot.tree.get_command('play'), self.discord_http_error(code)
            )
            await self.bot.tree.on_error(self.interaction, error)
        self.interaction.response.send_message.assert_not_awaited()
        self.interaction.followup.send.assert_not_awaited()

    async def test_error_reply_expiring_does_not_raise_a_second_exception(self):
        self.interaction.response.send_message.side_effect = self.discord_http_error()
        error = app_commands.CommandInvokeError(
            self.bot.tree.get_command('play'), ValueError('Use a YouTube URL.')
        )
        await self.bot.tree.on_error(self.interaction, error)
        self.interaction.response.is_done.return_value = True
        self.interaction.followup.send.side_effect = self.discord_http_error(50013, 403)
        await self.bot.tree.on_error(self.interaction, error)

    async def test_unexpected_defer_failure_remains_a_command_failure(self):
        self.interaction.response.defer.side_effect = self.discord_http_error(50013, 403)
        with self.assertRaises(discord.HTTPException):
            await self.command('play', 'https://youtu.be/abc')
        self.channel.connect.assert_not_awaited()

    async def test_service_cancellation_closes_client(self):
        fake = AsyncMock()
        fake.__aenter__.return_value = fake
        entered = asyncio.Event()

        async def start(token):
            entered.set()
            await asyncio.Event().wait()

        fake.start.side_effect = start
        task = asyncio.create_task(serve(fake, 'fake-token'))
        await entered.wait()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        fake.__aexit__.assert_awaited_once()


class RuntimeValidationTests(unittest.TestCase):
    def test_missing_runtime_and_old_node(self):
        with patch('musicbot.bot.shutil.which', return_value=None):
            with self.assertRaisesRegex(ValueError, 'Install'):
                validate_runtime()
        with (
            patch('musicbot.bot.shutil.which', return_value='/bin/node'),
            patch('musicbot.bot.subprocess.run', return_value=SimpleNamespace(stdout='v18.0.0')),
        ):
            with self.assertRaisesRegex(ValueError, '20'):
                validate_runtime()

    def test_safe_text_bounds_after_escaping(self):
        self.assertEqual(len(safe_text('*' * 2000)), 1900)
