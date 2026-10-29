import asyncio
import unittest

from musicbot.media import Track
from musicbot.player import Player, Queue, QueueFull


def track(title):
    return Track('https://www.youtube.com/watch?v=example', title, 42)


async def eventually(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.005)


class QueueTests(unittest.TestCase):
    def test_fifo_capacity_and_clear_invalidates_imports(self):
        queue = Queue(2)
        queue.append(track('one'), 0)
        queue.append(track('two'), 0)
        with self.assertRaises(QueueFull):
            queue.append(track('three'), 0)
        self.assertEqual(queue.pop().title, 'one')
        queue.clear()
        self.assertFalse(queue.append(track('stale'), 0))
        self.assertTrue(queue.append(track('new'), 1))
        self.assertEqual(queue.pop().title, 'new')


class FakeVoice:
    def __init__(self):
        self.played = []
        self.after = None
        self.disconnected = False

    def play(self, source, after):
        self.played.append(source)
        self.after = after

    def stop(self):
        callback, self.after = self.after, None
        if callback:
            callback(None)

    async def disconnect(self):
        self.disconnected = True
        self.stop()


class FakeMedia:
    def __init__(self):
        self.import_gate = asyncio.Event()
        self.import_gate.set()
        self.resolve_gate = asyncio.Event()
        self.resolve_gate.set()

    async def import_tracks(self, url, requester, limit):
        for index in range(min(10000, limit)):
            await self.import_gate.wait()
            yield track(f'{url}-{index}')
            await asyncio.sleep(0)

    async def resolve(self, item):
        await self.resolve_gate.wait()
        if item.title == 'bad':
            raise ValueError('unavailable')
        return item.title


class PlayerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.media, self.voice, self.messages = FakeMedia(), FakeVoice(), []

        async def notify(message):
            self.messages.append(message)

        self.player = Player(
            self.media, self.voice, notify, lambda stream: stream, limit=10000, idle_timeout=1
        )

    async def asyncTearDown(self):
        self.media.import_gate.set()
        self.media.resolve_gate.set()
        await self.player.close()
        await asyncio.wait_for(
            asyncio.gather(self.player._worker, self.player._importer, return_exceptions=True), 3
        )

    async def test_large_import_and_playback_order(self):
        self.player.submit('playlist', 42)
        await eventually(lambda: len(self.player.queue.tracks) == 9999)
        self.assertEqual(self.voice.played, ['playlist-0'])
        self.player.skip()
        await eventually(lambda: len(self.voice.played) == 2)
        self.assertEqual(self.voice.played[1], 'playlist-1')

    async def test_stop_invalidates_in_flight_import(self):
        self.media.import_gate.clear()
        self.player.submit('old', 42)
        await eventually(lambda: self.player.importing)
        self.player.stop()
        self.media.import_gate.set()
        await eventually(lambda: not self.player.importing)
        self.assertEqual(len(self.player.queue.tracks), 0)
        self.assertEqual(self.voice.played, [])
        self.player.submit('new', 42)
        await eventually(lambda: bool(self.voice.played))
        self.assertEqual(self.voice.played[0], 'new-0')

    async def test_skip_during_resolve(self):
        self.media.resolve_gate.clear()
        self.player.queue.append(track('one'), 0)
        self.player.queue.append(track('two'), 0)
        await eventually(lambda: self.player.current is not None)
        self.player.skip()
        self.media.resolve_gate.set()
        await eventually(lambda: bool(self.voice.played))
        self.assertEqual(self.voice.played, ['two'])

    async def test_unavailable_track_does_not_block_queue(self):
        self.player.queue.append(track('bad'), 0)
        self.player.queue.append(track('good'), 0)
        await eventually(lambda: bool(self.voice.played))
        self.assertEqual(self.voice.played, ['good'])
        self.assertTrue(any('Could not play' in message for message in self.messages))

    async def test_playback_error_advances_queue(self):
        self.player.queue.append(track('one'), 0)
        self.player.queue.append(track('two'), 0)
        await eventually(lambda: bool(self.voice.played))
        self.voice.after(RuntimeError('voice failed'))
        await eventually(lambda: len(self.voice.played) == 2)
        self.assertTrue(any('Audio playback failed' in m for m in self.messages))

    async def test_idle_disconnect(self):
        self.player.idle_timeout = 0.01
        self.player.queue.changed.set()
        await eventually(lambda: self.player.closed)
        await self.player.close()
        self.assertTrue(self.voice.disconnected)

    async def test_import_backlog_is_bounded(self):
        for requester in range(20):
            self.player.submit('playlist', requester)
        with self.assertRaises(ValueError):
            self.player.submit('overflow', 42)

    async def test_per_user_limit_includes_active_import(self):
        self.media.import_gate.clear()
        self.player.submit('first', 42)
        await eventually(lambda: self.player.importing)
        for _ in range(4):
            self.player.submit('more', 42)
        with self.assertRaisesRegex(ValueError, '5 pending imports'):
            self.player.submit('overflow', 42)
        self.player.submit('other member', 7)
        self.player.stop()
        self.player.submit('after stop', 42)

    async def test_full_queue_rejects_import_before_network_work(self):
        self.player.queue.limit = 1
        self.player.queue.append(track('one'), 0)
        with self.assertRaises(QueueFull):
            self.player.submit('playlist', 42)

    async def test_skip_cancels_resolution_without_waiting_for_network(self):
        cancelled = asyncio.Event()

        async def resolve(item):
            if item.title == 'stuck':
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
            return item.title

        self.media.resolve = resolve
        self.player.queue.append(track('stuck'), 0)
        self.player.queue.append(track('next'), 0)
        await eventually(lambda: self.player.current is not None)
        # Ensure resolution has entered its coroutine before cancelling.
        await asyncio.sleep(0)
        self.player.skip()
        await eventually(lambda: bool(self.voice.played))
        self.assertEqual(self.voice.played, ['next'])
        self.assertTrue(cancelled.is_set())

    async def test_stop_cancels_import_without_waiting_for_next_result(self):
        self.media.import_gate.clear()
        self.player.submit('stuck', 42)
        await eventually(lambda: self.player.importing)
        self.player.stop()
        await eventually(lambda: not self.player.importing)
        self.assertFalse(self.media.import_gate.is_set())
        self.assertFalse(self.player.jobs)

    async def test_late_callback_from_skipped_track_cannot_finish_next_track(self):
        for title in ('one', 'two', 'three'):
            self.player.queue.append(track(title), 0)
        await eventually(lambda: len(self.voice.played) == 1)
        old_callback = self.voice.after
        self.player.skip()
        await eventually(lambda: len(self.voice.played) == 2)
        old_callback(RuntimeError('late old audio callback'))
        await asyncio.sleep(0.02)
        self.assertEqual(self.voice.played, ['one', 'two'])
        self.assertEqual(self.player.current.title, 'two')

    async def test_missing_stop_callback_does_not_stall_skip(self):
        self.voice.stop = lambda: None
        self.player.queue.append(track('one'), 0)
        self.player.queue.append(track('two'), 0)
        await eventually(lambda: len(self.voice.played) == 1)
        self.player.skip()
        await eventually(lambda: len(self.voice.played) == 2)

    async def test_hanging_notification_does_not_block_playback_or_shutdown(self):
        entered = asyncio.Event()

        async def notify(message):
            entered.set()
            await asyncio.Event().wait()

        self.player.notify = notify
        self.player.notify_timeout = 0.02
        self.player.queue.append(track('one'), 0)
        self.player.queue.append(track('two'), 0)
        await eventually(lambda: entered.is_set())
        self.player.skip()
        await eventually(lambda: len(self.voice.played) == 2)
        await asyncio.wait_for(self.player.close(), 0.5)
        self.assertTrue(self.player._notifier.done())

    async def test_failed_notifications_do_not_kill_player(self):
        async def notify(message):
            raise PermissionError('text permission revoked')

        self.player.notify = notify
        self.player.queue.append(track('one'), 0)
        self.player.queue.append(track('two'), 0)
        await eventually(lambda: len(self.voice.played) == 1)
        self.player.skip()
        await eventually(lambda: len(self.voice.played) == 2)
        self.assertFalse(self.player._notifier.done())

    async def test_notification_backlog_is_bounded(self):
        for index in range(75):
            await self.player.send(f'notification {index}')
        self.assertEqual(self.player._notifications.qsize(), 50)

    async def test_idle_notification_cannot_admit_work_after_closing(self):
        rejected = asyncio.Event()

        async def notify(message):
            if message.startswith('Queue empty'):
                with self.assertRaisesRegex(ValueError, 'disconnected'):
                    self.player.submit('new', 42)
                rejected.set()

        self.player.notify = notify
        self.player.idle_timeout = 0.01
        self.player.queue.changed.set()
        await eventually(rejected.is_set)
        await self.player.close()
        self.assertFalse(self.player.jobs)
        self.assertTrue(self.voice.disconnected)

    async def test_close_reaps_workers_and_is_idempotent(self):
        self.media.resolve_gate.clear()
        self.media.import_gate.clear()
        self.player.queue.append(track('stuck'), 0)
        self.player.submit('playlist', 42)
        await eventually(lambda: self.player.current and self.player.importing)
        disconnects = []

        async def disconnect():
            disconnects.append(True)
            await asyncio.sleep(0.005)
            self.voice.disconnected = True

        self.voice.disconnect = disconnect
        await asyncio.gather(self.player.close(), self.player.close())
        self.assertEqual(disconnects, [True])
        self.assertTrue(self.player._worker.done())
        self.assertTrue(self.player._importer.done())
        self.assertTrue(self.player._notifier.done())
        self.assertIsNone(self.player._resolve_task)
        self.assertIsNone(self.player._import_task)
        with self.assertRaisesRegex(ValueError, 'disconnected'):
            self.player.submit('late', 42)

    async def test_disconnect_timeout_is_bounded(self):
        async def disconnect():
            await asyncio.Event().wait()

        self.voice.disconnect = disconnect
        self.player.disconnect_timeout = 0.01
        await asyncio.wait_for(self.player.close(), 0.5)
        self.assertTrue(self.player._worker.done())
        self.assertTrue(self.player._importer.done())

    async def test_partial_import_failure_keeps_already_added_tracks(self):
        async def partial(*args):
            yield track('one')
            yield track('two')
            raise ValueError('upstream unavailable')

        self.media.import_tracks = partial
        self.player.submit('playlist', 42)
        await eventually(lambda: any('Import failed after 2' in m for m in self.messages))
        self.assertEqual(self.voice.played, ['one'])
        self.assertEqual([t.title for t in self.player.queue.tracks], ['two'])

    async def test_queue_capacity_stops_import_without_dropping_existing_tracks(self):
        self.player.queue.limit = 2

        async def many(*args):
            for title in ('one', 'two', 'three', 'four'):
                yield track(title)

        self.media.import_tracks = many
        self.player.submit('playlist', 42)
        await eventually(lambda: any('Queue full' in m for m in self.messages))
        self.assertLessEqual(len(self.player.queue.tracks), 2)
        self.assertEqual(self.voice.played, ['one'])

    async def test_source_is_cleaned_up_if_voice_rejects_play(self):
        cleaned = []

        class Source:
            def cleanup(self):
                cleaned.append(True)

        self.player.make_source = lambda stream: Source()

        def reject(source, after):
            raise RuntimeError('voice disconnected')

        self.voice.play = reject
        self.player.queue.append(track('one'), 0)
        await eventually(lambda: bool(cleaned))
        self.assertEqual(cleaned, [True])
        self.assertIsNone(self.player._finished)

    async def test_stop_failure_does_not_abort_resource_cleanup(self):
        def fail_stop():
            raise RuntimeError('voice transport already gone')

        self.voice.stop = fail_stop

        async def disconnect():
            self.voice.disconnected = True

        self.voice.disconnect = disconnect
        self.media.resolve_gate.clear()
        self.player.queue.append(track('stuck'), 0)
        await eventually(lambda: self.player.current is not None)
        await asyncio.wait_for(self.player.close(), 0.5)
        self.assertTrue(self.voice.disconnected)
        self.assertTrue(self.player._worker.done())
        self.assertTrue(self.player._importer.done())

    async def test_failed_audio_preflight_never_announces_now_playing(self):
        from musicbot.audio import AudioPlaybackError

        cleaned = []

        class Source:
            def prepare(self):
                raise AudioPlaybackError('FFmpeg could not download audio (HTTP 403).')

            def cleanup(self):
                cleaned.append(True)

        self.player.make_source = lambda _: Source()
        self.player.queue.append(track('failed'), 0)
        await eventually(lambda: any('HTTP 403' in message for message in self.messages))
        self.assertEqual(cleaned, [True])
        self.assertFalse(self.voice.played)
        self.assertFalse(any('Now playing' in message for message in self.messages))

    async def test_audio_preflight_runs_off_loop_and_skip_cleans_source(self):
        import threading

        ready, released = threading.Event(), threading.Event()
        cleaned = []
        loop_thread = threading.get_ident()
        prepare_threads = []

        class Source:
            def prepare(self):
                prepare_threads.append(threading.get_ident())
                ready.set()
                if not released.wait(timeout=3):
                    raise RuntimeError('test preflight was not released')

            def cleanup(self):
                cleaned.append(True)
                released.set()

        self.player.make_source = lambda _: Source()
        self.player.queue.append(track('loading'), 0)
        try:
            await eventually(ready.is_set)
            self.assertFalse(self.voice.played)
            self.player.skip()
            await eventually(lambda: self.player.current is None)
            self.assertEqual(cleaned, [True])
            self.assertNotEqual(prepare_threads, [loop_thread])
            self.assertFalse(self.voice.played)
        finally:
            released.set()
