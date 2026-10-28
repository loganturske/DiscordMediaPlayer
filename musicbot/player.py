"""One server's FIFO queue, playback loop, import loop, and bounded notifications.

All queue mutations run on the asyncio event loop. Discord's audio thread reports
completion through call_soon_threadsafe; it never modifies queue state directly.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from contextlib import aclosing
from dataclasses import dataclass
from typing import Awaitable, Callable

from .media import Track

log = logging.getLogger(__name__)

MAX_PENDING_IMPORTS = 20
NOTIFICATION_BACKLOG = 50


@dataclass(frozen=True, slots=True)
class ImportJob:
    url: str
    requester: int
    generation: int


class QueueFull(ValueError):
    """A new track or import would exceed this server's queue capacity."""


class Queue:
    """All mutations occur on the event loop; generation invalidates old imports."""

    def __init__(self, limit: int):
        if limit < 1:
            raise ValueError('Queue capacity must be positive.')
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

    def clear(self) -> None:
        """Discard waiting tracks and invalidate results from earlier imports."""
        self.generation += 1
        self.tracks.clear()
        self.changed.set()

    def pop(self) -> Track | None:
        return self.tracks.popleft() if self.tracks else None


class Player:
    """Own the voice connection and tasks for one server.

    ``closed`` means shutdown has started. Await ``close()`` before replacing the
    player to ensure its tasks and old voice connection have finished cleanup.
    """

    def __init__(
        self,
        media,
        voice,
        notify: Callable[[str], Awaitable],
        make_source,
        *,
        limit=10000,
        idle_timeout=300,
        notify_timeout=5,
        disconnect_timeout=10,
        user_import_limit=5,
    ):
        self.media = media
        self.voice = voice
        self.notify = notify
        self.make_source = make_source
        self.queue = Queue(limit)
        self.current: Track | None = None
        self.jobs: deque[ImportJob] = deque()
        self.active_import: ImportJob | None = None
        self.importing = False
        self.closed = False
        self.idle_timeout = idle_timeout
        self.notify_timeout = notify_timeout
        self.disconnect_timeout = disconnect_timeout
        self.user_import_limit = user_import_limit
        self._skip = asyncio.Event()
        self._finished: asyncio.Future | None = None
        self._imports = asyncio.Event()
        self._resolve_task: asyncio.Task | None = None
        self._import_task: asyncio.Task | None = None
        self._close_task: asyncio.Task | None = None
        self._notifications: asyncio.Queue[str] = asyncio.Queue(maxsize=NOTIFICATION_BACKLOG)
        self._notifier = asyncio.create_task(self.notification_loop(), name='music-notifications')
        self._worker = asyncio.create_task(self.run(), name='music-playback')
        self._importer = asyncio.create_task(self.import_loop(), name='music-imports')

    def submit(self, url: str, requester: int) -> None:
        """Append an import request after checking queue and requester limits."""
        if self.closed:
            raise ValueError('Player disconnected. Try /play again.')
        if len(self.queue.tracks) >= self.queue.limit:
            raise QueueFull('The queue is full. Wait for playback or clear it first.')
        if len(self.jobs) >= MAX_PENDING_IMPORTS:
            raise ValueError('Too many pending imports. Wait for the current imports to finish.')
        user_jobs = sum(job.requester == requester for job in self.jobs)
        user_jobs += bool(
            self.active_import
            and self.active_import.requester == requester
            and self.active_import.generation == self.queue.generation
        )
        if user_jobs >= self.user_import_limit:
            raise ValueError(
                f'You already have {self.user_import_limit} pending imports. Wait for them to finish.'
            )
        self.jobs.append(ImportJob(url, requester, self.queue.generation))
        self._imports.set()
        self.queue.changed.set()

    async def send(self, message: str) -> None:
        """Queue a best-effort notification without waiting on Discord."""
        # Discord rate limits and unavailable text channels must never stall audio.
        try:
            self._notifications.put_nowait(message)
        except asyncio.QueueFull:
            log.warning('Notification backlog full; dropping a player notification')

    async def notification_loop(self):
        while True:
            message = await self._notifications.get()
            try:
                await asyncio.wait_for(self.notify(message), self.notify_timeout)
            except Exception as error:
                # Do not log message text or provider exception strings.
                log.warning('Player notification failed: %s', type(error).__name__)
            finally:
                self._notifications.task_done()

    async def _import_job(self, job):
        count = 0
        try:
            async with aclosing(
                self.media.import_tracks(job.url, job.requester, self.queue.limit)
            ) as entries:
                async for track in entries:
                    if self.closed or not self.queue.append(track, job.generation):
                        break
                    count += 1
                    if count == 1:
                        await self.send(f'Queued: {track.title[:150]}')
            if not self.closed and job.generation == self.queue.generation:
                await self.send(
                    f'Import finished: {count} track(s) added. Imports are capped at {self.queue.limit:,} entries.'
                )
        except QueueFull:
            await self.send(f'Queue full; added {count} track(s) from this import.')
        except Exception as error:
            log.warning('Playlist import failed: %s', type(error).__name__)
            if not self.closed and job.generation == self.queue.generation:
                await self.send(
                    f'Import failed after {count} track(s). Check the link, YouTube access, and the host cookie session; already added tracks remain queued.'
                )

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
            self.active_import = job
            self._import_task = asyncio.create_task(self._import_job(job), name='music-import-job')
            try:
                await self._import_task
            except asyncio.CancelledError:
                # /stop cancels one job; the importer remains available for new work.
                if self.closed or asyncio.current_task().cancelling():
                    raise
            finally:
                self._import_task = None
                self.active_import = None
                self.importing = False
                self.queue.changed.set()

    def _stop_audio(self):
        try:
            self.voice.stop()
        except Exception as error:
            log.warning('Stopping audio failed: %s', type(error).__name__)

    def skip(self) -> None:
        """Interrupt the current lookup or audio and advance to the next track."""
        if self.current:
            self._skip.set()
            if self._resolve_task and not self._resolve_task.done():
                self._resolve_task.cancel()
            self._stop_audio()
            # Some disconnections never deliver an audio callback.
            if self._finished and not self._finished.done():
                self._finished.set_result(None)

    def stop(self) -> None:
        """Cancel current work and clear the queue while remaining in voice."""
        self.queue.clear()
        self.jobs.clear()
        if self._import_task and not self._import_task.done():
            self._import_task.cancel()
        self.skip()
        if self.current is None:
            self._stop_audio()

    async def close(self) -> None:
        """Idempotently disconnect and wait for bounded resource cleanup."""
        if self._close_task is None:
            # No await between marking closed and invalidating imports: new commands
            # cannot append work to a player that has already decided to disconnect.
            self.closed = True
            self.stop()
            self.queue.changed.set()
            self._imports.set()
            caller = asyncio.current_task()
            tasks = [task for task in (self._worker, self._importer) if task is not caller]
            for task in tasks:
                task.cancel()
            self._close_task = asyncio.create_task(self._shutdown(tasks), name='music-shutdown')
        await asyncio.shield(self._close_task)

    async def _shutdown(self, tasks):
        try:
            if tasks:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True), self.disconnect_timeout
                )
        except TimeoutError:
            log.warning('Player workers exceeded shutdown deadline')
        try:
            await asyncio.wait_for(self.voice.disconnect(), self.disconnect_timeout)
        except Exception as error:
            log.warning('Voice disconnect failed: %s', type(error).__name__)
        finally:
            try:
                await asyncio.wait_for(self._notifications.join(), self.notify_timeout)
            except TimeoutError:
                pass
            self._notifier.cancel()
            await asyncio.gather(self._notifier, return_exceptions=True)

    async def _play(self, track):
        self._resolve_task = asyncio.create_task(self.media.resolve(track), name='music-resolve')
        try:
            stream = await self._resolve_task
        except asyncio.CancelledError:
            if self.closed or asyncio.current_task().cancelling():
                raise
            return  # A skip cancelled this resolution, not the playback worker.
        finally:
            self._resolve_task = None
        if self.closed or self._skip.is_set():
            return
        source = self.make_source(stream)
        loop = asyncio.get_running_loop()
        finished = loop.create_future()
        self._finished = finished

        def complete(error):
            if not finished.done():
                finished.set_result(error)

        def after(error):
            try:
                loop.call_soon_threadsafe(complete, error)
            except RuntimeError:
                pass  # The loop has shut down; this source is already being cleaned up.

        try:
            self.voice.play(source, after=after)
        except Exception:
            self._finished = None
            source.cleanup()
            raise
        try:
            await self.send(f'Now playing: {track.title[:150]}')
            error = await finished
            if error and not self._skip.is_set():
                await self.send('Audio playback failed; moving to the next track.')
        finally:
            self._finished = None

    async def run(self):
        try:
            while not self.closed:
                self.queue.changed.clear()
                track = self.queue.pop()
                if not track:
                    try:
                        await asyncio.wait_for(self.queue.changed.wait(), self.idle_timeout)
                    except TimeoutError:
                        # Recheck tracks too: a submission can race the timeout.
                        if not self.importing and not self.jobs and not self.queue.tracks:
                            await self.send('Queue empty; leaving voice. Use /play to start again.')
                            await self.close()
                    continue
                self.current = track
                self._skip.clear()
                try:
                    await self._play(track)
                except Exception as error:
                    log.warning('Track playback failed: %s', type(error).__name__)
                    if not self.closed:
                        await self.send(
                            f'Could not play {track.title[:150]}. It may be unavailable or require an updated YouTube session. Moving to the next track.'
                        )
                finally:
                    self.current = None
        finally:
            if not self.closed:
                await self.close()
