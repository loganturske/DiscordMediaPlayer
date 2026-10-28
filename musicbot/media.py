from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import shutil
import tempfile
from urllib.parse import parse_qs, urlparse

import yt_dlp


@dataclass(frozen=True)
class Track:
    url: str
    title: str
    requested_by: int


def youtube_url(value: str) -> str:
    """Accept YouTube video/playlist URLs, never arbitrary extractor destinations."""
    value = value.strip()
    parsed = urlparse(value)
    host = (parsed.hostname or '').lower()
    if (parsed.scheme != 'https' or parsed.username or parsed.password
            or parsed.port not in (None, 443)
            or host not in {'youtube.com', 'www.youtube.com', 'music.youtube.com', 'youtu.be'}):
        raise ValueError('Use an HTTPS YouTube video or playlist link.')
    query = parse_qs(parsed.query)
    if host == 'youtu.be':
        valid = bool(parsed.path.strip('/'))
    else:
        valid = (parsed.path == '/watch' and bool(query.get('v'))
                 or parsed.path == '/playlist' and bool(query.get('list'))
                 or parsed.path.startswith(('/shorts/', '/live/')) and bool(parsed.path.split('/')[-1]))
    if not valid:
        raise ValueError('Use a YouTube video or playlist link, not a channel or homepage.')
    return value


class Media:
    def __init__(self, cookie_file: Path):
        self.cookie_file = cookie_file
        self.slots = asyncio.Semaphore(3)

    @contextmanager
    def extractor(self, *, flat=False, limit=10000):
        # yt-dlp can rewrite its cookie jar. Give each extraction a private snapshot,
        # preserving the durable host-supplied session and preventing concurrent writes.
        with tempfile.TemporaryDirectory(prefix='discord-media-') as tmp:
            options = {
                'quiet': True, 'no_warnings': True, 'noprogress': True,
                'socket_timeout': 20, 'retries': 2, 'extractor_retries': 2,
                'format': 'bestaudio/best', 'noplaylist': not flat,
                'extract_flat': 'in_playlist' if flat else False,
                'lazy_playlist': True, 'playlistend': limit,
                'ignoreerrors': flat, 'skip_download': True,
                'js_runtimes': {'node': {}},
                'allowed_extractors': ['youtube.*'],
                # Do not log extractor errors, which can include signed media URLs.
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
        with self.extractor(flat=True, limit=limit) as ydl:
            result = ydl.extract_info(url, download=False)
            if not result:
                raise ValueError('YouTube returned no playable entries.')
            for item in result.get('entries', [result]):
                if not item:
                    continue
                video_id = item.get('id')
                if not video_id:
                    continue
                # Canonical video links avoid reimporting the containing playlist.
                yield Track(youtube_url(f'https://www.youtube.com/watch?v={video_id}'),
                            item.get('title') or video_id, requested_by)

    async def import_tracks(self, url: str, requested_by: int, limit: int):
        iterator = self.entries(youtube_url(url), requested_by, limit)
        sentinel = object()
        async with self.slots:
            try:
                while True:
                    item = await asyncio.to_thread(next, iterator, sentinel)
                    if item is sentinel:
                        break
                    yield item
            finally:
                # Import tasks are stopped cooperatively, never cancelled during next().
                await asyncio.to_thread(iterator.close)

    async def resolve(self, track: Track) -> str:
        def extract():
            with self.extractor() as ydl:
                result = ydl.extract_info(track.url, download=False)
                if not result or not result.get('url'):
                    raise ValueError('No playable audio stream.')
                if result.get('is_live'):
                    raise ValueError('Live streams are not supported; queue a recorded video.')
                return result['url']
        async with self.slots:
            return await asyncio.to_thread(extract)


class QuietLogger:
    def debug(self, message):
        pass

    def warning(self, message):
        pass

    def error(self, message):
        pass
