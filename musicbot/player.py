from __future__ import annotations

import asyncio
from collections import deque
from contextlib import aclosing
from dataclasses import dataclass
from typing import Awaitable, Callable

from .media import Track


@dataclass
class ImportJob:
    url: str
    requester: int
    generation: int


class QueueFull(ValueError):
    pass


class Queue:
    """All mutations occur on the event loop; generation invalidates old imports."""
    def __init__(self, limit: int):
        self.limit = limit
        self.tracks: deque[Track] = deque()
        self.generation = 0
        self.changed = asyncio.Event()

    def append(self, track: Track, generation: int) -> bool:
        if generation != self.generation:
            return False
        if len(self.tracks) >= self.limit:
            raise QueueFull('The queue is full.')
        self.tracks.append(track)
        self.changed.set()
        return True

    def clear(self):
        self.generation += 1
        self.tracks.clear()
        self.changed.set()

    def pop(self):
        return self.tracks.popleft() if self.tracks else None


class Player:
    def __init__(self, media, voice, notify: Callable[[str], Awaitable],
                 make_source, *, limit=10000, idle_timeout=300):
        self.media = media
        self.voice = voice
        self.notify = notify
        self.make_source = make_source
        self.queue = Queue(limit)
        self.current: Track | None = None
        self.jobs: deque[ImportJob] = deque()
        self.importing = False
        self.closed = False
        self.idle_timeout = idle_timeout
        self._skip = asyncio.Event()
        self._finished = asyncio.Event()
        self._imports = asyncio.Event()
        self._worker = asyncio.create_task(self.run())
        self._importer = asyncio.create_task(self.import_loop())

    def submit(self, url, requester):
        if self.closed:
            raise ValueError('Player disconnected. Try /play again.')
        if len(self.jobs) >= 20:
            raise ValueError('Too many pending imports. Wait for the current imports to finish.')
        self.jobs.append(ImportJob(url, requester, self.queue.generation))
        self._imports.set()
        self.queue.changed.set()

    async def send(self, message):
        try:
            await self.notify(message)
        except Exception:
            # Losing permission to the text channel must not kill voice playback.
            pass

    async def import_loop(self):
        while not self.closed:
            self._imports.clear()
            if not self.jobs:
                await self._imports.wait()
                continue
            job = self.jobs.popleft()
            if job.generation != self.queue.generation:
                continue
            self.importing = True
            count = 0
            try:
                async with aclosing(self.media.import_tracks(job.url, job.requester, self.queue.limit)) as entries:
                    async for track in entries:
                        if self.closed or not self.queue.append(track, job.generation):
                            break
                        count += 1
                        if count == 1:
                            await self.send(f'Queued: {track.title[:150]}')
                if not self.closed and job.generation == self.queue.generation:
                    await self.send(f'Import finished: {count} track(s) added. Imports are capped at {self.queue.limit:,} entries.')
            except QueueFull:
                await self.send(f'Queue full; added {count} track(s) from this import.')
            except Exception:
                await self.send(f'Import failed after {count} track(s). Check the link, YouTube access, and the host cookie session; already added tracks remain queued.')
            finally:
                self.importing = False
                self.queue.changed.set()

    def skip(self):
        if self.current:
            self._skip.set()
            self.voice.stop()

    def stop(self):
        self.queue.clear()
        self.jobs.clear()
        self.skip()

    async def close(self):
        if self.closed:
            return
        self.closed = True
        self.stop()
        self.queue.changed.set()
        self._imports.set()
        self._finished.set()
        await self.voice.disconnect()

    async def run(self):
        try:
            while not self.closed:
                self.queue.changed.clear()
                track = self.queue.pop()
                if not track:
                    try:
                        await asyncio.wait_for(self.queue.changed.wait(), self.idle_timeout)
                    except TimeoutError:
                        if not self.importing and not self.jobs:
                            await self.send('Queue empty; leaving voice. Use /play to start again.')
                            await self.close()
                    continue
                self.current = track
                self._skip.clear()
                self._finished.clear()
                try:
                    stream = await self.media.resolve(track)
                    if self.closed or self._skip.is_set():
                        continue
                    source = self.make_source(stream)
                    loop = asyncio.get_running_loop()
                    errors = []
                    def after(error):
                        if error:
                            errors.append(error)
                        loop.call_soon_threadsafe(self._finished.set)
                    try:
                        self.voice.play(source, after=after)
                    except Exception:
                        source.cleanup()
                        raise
                    await self.send(f'Now playing: {track.title[:150]}')
                    await self._finished.wait()
                    if errors:
                        await self.send('Audio playback failed; moving to the next track.')
                except Exception:
                    await self.send(f'Could not play {track.title[:150]}. It may be unavailable or require an updated YouTube session. Moving to the next track.')
                finally:
                    self.current = None
        finally:
            if not self.closed:
                await self.close()
