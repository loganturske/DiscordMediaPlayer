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
        self.player = Player(self.media, self.voice, notify, lambda stream: stream,
                             limit=10000, idle_timeout=1)

    async def asyncTearDown(self):
        self.media.import_gate.set()
        self.media.resolve_gate.set()
        await self.player.close()
        await asyncio.wait_for(asyncio.gather(self.player._worker, self.player._importer), 3)

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
        self.assertTrue(self.voice.disconnected)

    async def test_import_backlog_is_bounded(self):
        for _ in range(20):
            self.player.submit('playlist', 42)
        with self.assertRaises(ValueError):
            self.player.submit('overflow', 42)
