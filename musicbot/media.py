"""Lazy YouTube metadata extraction with bounded, cancellable worker processes."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
import sys
import tempfile
import weakref
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yt_dlp


@dataclass(frozen=True, slots=True)
class Track:
    """Compact queue metadata; signed audio URLs are resolved only at playback."""

    url: str
    title: str
    requested_by: int


_IDENTIFIER = re.compile(r'[A-Za-z0-9_-]{1,256}\Z')


def youtube_url(value: str) -> str:
    """Accept recorded video/playlist locations, never arbitrary extractor URLs."""
    message = 'Use an HTTPS YouTube video or playlist link.'
    if not isinstance(value, str):
        raise ValueError(message)
    value = value.strip()
    if len(value) > 4096 or any(c.isspace() or ord(c) < 32 for c in value):
        raise ValueError(message)
    try:
        parsed = urlparse(value)
        host = (parsed.hostname or '').lower()
        if (
            parsed.scheme != 'https'
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 443)
            or host not in {'youtube.com', 'www.youtube.com', 'music.youtube.com', 'youtu.be'}
        ):
            raise ValueError(message)
    except ValueError:
        raise ValueError(message) from None
    query = parse_qs(parsed.query)

    def identifier(key):
        values = query.get(key, [])
        return len(values) == 1 and bool(_IDENTIFIER.fullmatch(values[0]))

    if host == 'youtu.be':
        valid = bool(_IDENTIFIER.fullmatch(parsed.path.removeprefix('/')))
    else:
        valid = (
            parsed.path == '/watch'
            and identifier('v')
            or parsed.path == '/playlist'
            and identifier('list')
            or re.fullmatch(r'/(?:shorts|live)/[A-Za-z0-9_-]{1,256}', parsed.path)
        )
    if not valid or ('list' in query and not identifier('list')):
        raise ValueError(message)
    return value


def stream_url(value: str) -> str:
    """Only Google's HTTPS video CDN may be passed to the FFmpeg subprocess."""
    try:
        parsed = urlparse(value)
        host = (parsed.hostname or '').lower()
        valid = (
            parsed.scheme == 'https'
            and not parsed.username
            and not parsed.password
            and parsed.port in (None, 443)
            and (host == 'googlevideo.com' or host.endswith('.googlevideo.com'))
            and not any(c.isspace() or ord(c) < 32 for c in value)
        )
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise ValueError('YouTube returned an unsupported audio destination.')
    return value


class MediaError(ValueError):
    """Safe, actionable error that contains no provider URLs or credentials."""


class Media:
    """Own extraction capacity and worker lifetimes for the bot.

    The bot uses ``import_tracks`` and ``resolve``; both keep blocking yt-dlp
    calls outside the gateway event loop. Synchronous extraction methods are
    used only inside ``extractor_worker``. Always await ``close`` on shutdown.
    """

    def __init__(
        self,
        cookie_file: Path,
        *,
        resolve_timeout=60,
        import_timeout=900,
        inactivity_timeout=60,
        snapshot_root: Path | None = None,
    ):
        self.cookie_file = cookie_file
        # Long playlist jobs must never occupy all playback extraction slots.
        self.import_slots = asyncio.Semaphore(2)
        self.resolve_slots = asyncio.Semaphore(3)
        self.resolve_timeout = resolve_timeout
        self.import_timeout = import_timeout
        self.inactivity_timeout = inactivity_timeout
        self.snapshot_root = snapshot_root
        self._processes = set()
        self._readers = {}
        self._directories = {}
        self._cleanup_tasks = weakref.WeakKeyDictionary()
        self._close_task = None
        self.closed = False

    @contextmanager
    def extractor(self, *, flat=False, limit=10000):
        """Create a worker-local extractor with a private copy of saved cookies."""
        # Worker snapshots live inside a directory owned by its parent process.
        # The parent removes them even if the worker needs to be killed.
        with tempfile.TemporaryDirectory(prefix='discord-media-', dir=self.snapshot_root) as tmp:
            options = {
                'quiet': True,
                'no_warnings': True,
                'noprogress': True,
                'socket_timeout': 20,
                'retries': 2,
                'extractor_retries': 2,
                'format': 'bestaudio/best',
                'noplaylist': not flat,
                'extract_flat': 'in_playlist' if flat else False,
                'lazy_playlist': True,
                'playlistend': limit,
                'ignoreerrors': flat,
                'skip_download': True,
                'cachedir': False,
                'js_runtimes': {'node': {}},
                'allowed_extractors': ['youtube.*'],
                'logger': QuietLogger(),
            }
            if self.cookie_file.is_file():
                snapshot = Path(tmp) / 'cookies.txt'
                shutil.copyfile(self.cookie_file, snapshot)
                snapshot.chmod(0o600)
                options['cookiefile'] = str(snapshot)
            with yt_dlp.YoutubeDL(options) as ydl:
                yield ydl

    def entries(self, url: str, requested_by: int, limit: int):
        """Yield metadata for at most ``limit`` source entries without preloading."""
        with self.extractor(flat=True, limit=limit) as ydl:
            result = ydl.extract_info(youtube_url(url), download=False)
            if not result:
                raise MediaError('YouTube returned no playable entries.')
            # islice stops before fetching entry limit + 1, which could otherwise
            # trigger another remote playlist page even though it will be discarded.
            for item in islice(result.get('entries', [result]), limit):
                if not item:
                    continue
                video_id = item.get('id')
                if not isinstance(video_id, str) or not _IDENTIFIER.fullmatch(video_id):
                    continue
                yield Track(
                    f'https://www.youtube.com/watch?v={video_id}',
                    str(item.get('title') or video_id)[:500],
                    requested_by,
                )

    def resolve_sync(self, url: str) -> str:
        """Resolve one recorded video to a validated, short-lived audio URL."""
        with self.extractor() as ydl:
            result = ydl.extract_info(youtube_url(url), download=False)
            if not result or not result.get('url'):
                raise MediaError('No playable audio stream.')
            if result.get('is_live'):
                raise MediaError('Live streams are not supported; queue a recorded video.')
            return stream_url(result['url'])

    async def _spawn(self):
        # The worker has no reason to receive the Discord credential.
        environment = {k: v for k, v in os.environ.items() if k != 'DISCORD_TOKEN'}
        return await asyncio.create_subprocess_exec(
            sys.executable,
            '-m',
            'musicbot.extractor_worker',
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=65536,
            cwd=Path(__file__).resolve().parent.parent,
            env=environment,
            start_new_session=(os.name == 'posix'),
        )

    async def _cleanup_process(self, process):
        # Kill the whole session so a JS challenge runtime cannot outlive yt-dlp.
        if os.name == 'posix':
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        elif process.returncode is None:
            process.kill()
        # Draining the pipe also releases asyncio's subprocess transport when a
        # fast worker filled its stdout buffer before being cancelled.
        await asyncio.wait_for(process.communicate(), 5)

    async def _terminate(self, process):
        # A single cleanup task owns communicate(); close() and the operation's
        # finally block must never race to drain the same subprocess pipe.
        cleanup = self._cleanup_tasks.get(process)
        if cleanup is None:
            cleanup = asyncio.create_task(self._cleanup_process(process))
            self._cleanup_tasks[process] = cleanup
        _, interrupted = await self._finish_task(cleanup)
        if interrupted:
            raise asyncio.CancelledError

    async def close(self):
        """Stop active reads and reap workers; concurrent callers share cleanup."""
        self.closed = True
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close_operations(asyncio.current_task()))
        _, interrupted = await self._finish_task(self._close_task)
        if interrupted:
            raise asyncio.CancelledError

    async def _close_operations(self, caller):
        owners = {
            owner for owner in self._readers.values() if owner is not caller and not owner.done()
        }
        for owner in owners:
            owner.cancel()
        if owners:
            await asyncio.gather(*owners, return_exceptions=True)
        # A consumer may have stopped between async-generator yields; no task is
        # then blocked reading stdout. Reap that worker and remove its snapshot too.
        for process in list(self._processes):
            await self._terminate(process)
            directory = self._directories.pop(process, None)
            if directory:
                directory.cleanup()
            self._readers.pop(process, None)
            self._processes.discard(process)

    @staticmethod
    async def _finish_task(task):
        """Finish cleanup despite repeated cancellation of its owning task."""
        interrupted = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                interrupted = True
        return task.result(), interrupted

    @asynccontextmanager
    async def _worker(self, mode: str, url: str, *, requester=0, limit=1):
        directory = tempfile.TemporaryDirectory(prefix='discord-worker-')
        with directory as temporary:
            if self.closed:
                raise MediaError('The YouTube extractor is shutting down.')
            spawning = asyncio.create_task(self._spawn())
            try:
                process = await asyncio.shield(spawning)
            except asyncio.CancelledError:
                process, _ = await self._finish_task(spawning)
                await self._finish_task(asyncio.create_task(self._terminate(process)))
                raise
            self._processes.add(process)
            self._directories[process] = directory
            try:
                if self.closed:
                    raise MediaError('The YouTube extractor is shutting down.')
                request = {
                    'mode': mode,
                    'url': url,
                    'requester': requester,
                    'limit': limit,
                    'cookie_file': str(self.cookie_file.resolve()),
                    'snapshot_root': temporary,
                }
                process.stdin.write(json.dumps(request).encode() + b'\n')
                await asyncio.wait_for(process.stdin.drain(), self.inactivity_timeout)
                process.stdin.close()
                yield process
            finally:
                cleanup = asyncio.create_task(self._terminate(process))
                try:
                    _, interrupted = await self._finish_task(cleanup)
                    if interrupted:
                        raise asyncio.CancelledError
                finally:
                    self._processes.discard(process)
                    self._readers.pop(process, None)
                    self._directories.pop(process, None)

    async def _message(self, process, timeout):
        if self.closed:
            raise MediaError('The YouTube extractor is shutting down.')
        try:
            self._readers[process] = asyncio.current_task()
            try:
                line = await asyncio.wait_for(process.stdout.readline(), timeout)
            finally:
                self._readers.pop(process, None)
            if not line:
                raise MediaError('The YouTube extractor exited unexpectedly.')
            message = json.loads(line)
            if not isinstance(message, dict) or message.get('type') not in {
                'track',
                'stream',
                'done',
                'error',
            }:
                raise ValueError
        except (json.JSONDecodeError, ValueError) as error:
            if isinstance(error, MediaError):
                raise
            raise MediaError('Invalid response from the YouTube extractor.') from None
        if message['type'] == 'error':
            raise MediaError(
                'YouTube extraction failed. Check availability and the saved cookie session.'
            )
        return message

    async def import_tracks(self, url: str, requested_by: int, limit: int):
        """Stream metadata with backpressure and bounded import capacity.

        Consumers that stop early must close this async generator, normally
        with ``contextlib.aclosing``, to release its worker and cookie snapshot.
        """
        url = youtube_url(url)
        if limit < 1:
            raise ValueError('Import limit must be positive.')
        # Waiting for an import slot is also bounded. Timers never span a yield,
        # so they cannot cancel unrelated work in the consuming task.
        acquired = False
        try:
            await asyncio.wait_for(self.import_slots.acquire(), self.import_timeout)
            acquired = True
            deadline = asyncio.get_running_loop().time() + self.import_timeout
            async with self._worker('import', url, requester=requested_by, limit=limit) as process:
                count = 0
                while True:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        raise TimeoutError
                    message = await self._message(process, min(remaining, self.inactivity_timeout))
                    if message['type'] == 'done':
                        return
                    if message['type'] != 'track' or count >= limit:
                        raise MediaError('Invalid playlist response from the YouTube extractor.')
                    try:
                        track = Track(
                            youtube_url(message['url']), str(message['title'])[:500], requested_by
                        )
                    except (KeyError, TypeError, ValueError):
                        raise MediaError(
                            'Invalid playlist entry from the YouTube extractor.'
                        ) from None
                    count += 1
                    yield track
        except TimeoutError:
            raise MediaError(
                'YouTube playlist import timed out; already queued tracks are retained.'
            ) from None
        finally:
            if acquired:
                self.import_slots.release()

    async def resolve(self, track: Track) -> str:
        """Look up fresh audio using capacity reserved separately from imports."""
        url = youtube_url(track.url)
        try:
            async with asyncio.timeout(self.resolve_timeout):
                async with self.resolve_slots:
                    async with self._worker('resolve', url) as process:
                        message = await self._message(process, self.resolve_timeout)
                        if message['type'] != 'stream':
                            raise MediaError('Invalid audio response from the YouTube extractor.')
                        return stream_url(message.get('url', ''))
        except TimeoutError:
            raise MediaError('YouTube audio lookup timed out; try again later.') from None


class QuietLogger:
    """Discard provider logs that can contain cookie values or signed URLs."""

    def debug(self, message):
        pass

    def warning(self, message):
        pass

    def error(self, message):
        pass
