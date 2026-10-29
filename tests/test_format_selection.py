"""Real native HTTP probes prove that format selection skips rejected streams."""

import errno
import os
import tempfile
import threading
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from yt_dlp.utils import ExtractorError

from musicbot.media import Media


@contextmanager
def format_server():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            denied = self.path == '/denied'
            body = b'forbidden' if denied else b'audio-sample' * 2000
            self.send_response(403 if denied else 200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass  # yt-dlp intentionally stops after its short test sample.

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def fixture(base, *, include_working=True):
    formats = []
    if include_working:
        formats.append(
            {
                'format_id': 'working',
                'url': base + '/working',
                'ext': 'webm',
                'acodec': 'opus',
                'vcodec': 'none',
                'abr': 64,
            }
        )
    formats.append(
        {
            'format_id': 'denied',
            'url': base + '/denied',
            'ext': 'webm',
            'acodec': 'opus',
            'vcodec': 'none',
            'abr': 128,
        }
    )
    return {'id': 'fixture', 'title': 'fixture', 'extractor': 'fixture', 'formats': formats}


class FormatSelectionTests(unittest.TestCase):
    def test_rejected_best_format_falls_back_to_downloadable_audio(self):
        try:
            with (
                patch.dict(
                    os.environ, {'http_proxy': '', 'https_proxy': '', 'no_proxy': '127.0.0.1'}
                ),
                format_server() as (base, requests),
                tempfile.TemporaryDirectory() as temporary,
            ):
                media = Media(Path(temporary) / 'no-cookies', snapshot_root=Path(temporary))
                with media.extractor() as ydl:
                    selected = ydl.process_ie_result(fixture(base), download=False)
                    self.assertEqual(selected['format_id'], 'working')
                    self.assertEqual(requests, ['/denied', '/working'])
                    self.assertTrue(Path(ydl.params['paths']['temp']).is_relative_to(temporary))
                self.assertFalse(list(Path(temporary).iterdir()))
        except OSError as error:
            if error.errno in {errno.EACCES, errno.EPERM}:
                self.skipTest('Sandbox does not permit a local test server.')
            raise

    def test_all_rejected_formats_fail_resolution_instead_of_returning_a_bad_url(self):
        try:
            with (
                patch.dict(
                    os.environ, {'http_proxy': '', 'https_proxy': '', 'no_proxy': '127.0.0.1'}
                ),
                format_server() as (base, requests),
                tempfile.TemporaryDirectory() as temporary,
            ):
                media = Media(Path(temporary) / 'no-cookies', snapshot_root=Path(temporary))
                with media.extractor() as ydl, self.assertRaises(ExtractorError):
                    ydl.process_ie_result(fixture(base, include_working=False), download=False)
                self.assertEqual(requests, ['/denied'])
                self.assertFalse(list(Path(temporary).iterdir()))
        except OSError as error:
            if error.errno in {errno.EACCES, errno.EPERM}:
                self.skipTest('Sandbox does not permit a local test server.')
            raise

    def test_metadata_imports_do_not_probe_audio(self):
        with tempfile.TemporaryDirectory() as temporary:
            media = Media(Path(temporary) / 'no-cookies')
            with (
                media.extractor(flat=True) as ydl,
                patch.object(ydl, 'dl', side_effect=AssertionError('metadata must not download')),
            ):
                info = ydl.process_ie_result(fixture('https://cdn.googlevideo.com'), download=False)
            self.assertEqual(info['format_id'], 'denied')
