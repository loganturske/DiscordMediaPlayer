"""Audio regressions exercise real decoding rather than only spawned processes."""

import errno
import io
import os
import shlex
import shutil
import ssl
import subprocess
import sys
import threading
import unittest
import wave
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from musicbot.audio import AudioPlaybackError, PCMSource, audio_source
from musicbot.media import AudioStream


def wav_audio(frames: int = 4800) -> bytes:
    """Build an offline 100 ms WAV fixture without invoking a second FFmpeg."""
    output = io.BytesIO()
    with wave.open(output, 'wb') as fixture:
        fixture.setnchannels(2)
        fixture.setsampwidth(2)
        fixture.setframerate(48000)
        fixture.writeframes(b'\0\0\0\0' * frames)
    return output.getvalue()


@contextmanager
def audio_server(payload: bytes, *, status: int = 200, required_headers=None, tls_context=None):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            actual_status = status
            if required_headers and any(
                self.headers.get(name) != value for name, value in required_headers.items()
            ):
                actual_status = 403
            self.send_response(actual_status)
            self.send_header('Content-Type', 'audio/wav')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    if tls_context is not None:
        server.socket = tls_context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        scheme = 'https' if tls_context is not None else 'http'
        yield f'{scheme}://127.0.0.1:{server.server_port}/audio.wav'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg is not installed')
class AudioTests(unittest.TestCase):
    def test_real_audio_returns_frames_then_clean_eof(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / 'audio.wav'
            path.write_bytes(wav_audio())
            source = audio_source(str(path))
            try:
                frames = []
                while frame := source.read():
                    frames.append(frame)
                self.assertEqual(len(frames), 5)
                self.assertTrue(all(len(frame) == 3840 for frame in frames))
            finally:
                source.cleanup()

    def test_failed_decoder_raises_safe_error_instead_of_silent_eof(self):
        with TemporaryDirectory() as temporary:
            secret = 'private-token-do-not-log'
            source = audio_source(str(Path(temporary) / f'{secret}.wav'))
            try:
                with self.assertRaises(AudioPlaybackError) as failure:
                    source.read()
                self.assertNotIn(secret, str(failure.exception))
                self.assertNotIn(temporary, str(failure.exception))
            finally:
                source.cleanup()

    def test_zero_audio_frames_are_a_failure(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / 'empty.wav'
            path.write_bytes(wav_audio(frames=0))
            source = audio_source(str(path))
            try:
                with self.assertRaises(AudioPlaybackError):
                    source.read()
            finally:
                source.cleanup()

    def test_http_audio_uses_remote_options_and_decodes(self):
        # Real network input catches unsupported flags that local WAV input cannot.
        with patch.dict(os.environ, {'http_proxy': '', 'https_proxy': '', 'no_proxy': '127.0.0.1'}):
            try:
                with audio_server(wav_audio()) as url:
                    source = audio_source(url)
                    try:
                        self.assertEqual(len(source.read()), 3840)
                    finally:
                        source.cleanup()
            except OSError as error:
                if error.errno in {errno.EACCES, errno.EPERM}:
                    self.skipTest('Execution sandbox prevents binding a loopback test server.')
                raise

    def test_remote_request_preserves_required_extractor_headers(self):
        headers = {
            'User-Agent': 'Playback Regression "test" agent',
            'Referer': 'https://www.youtube.com/',
        }
        with patch.dict(os.environ, {'http_proxy': '', 'https_proxy': '', 'no_proxy': '127.0.0.1'}):
            try:
                with audio_server(wav_audio(), required_headers=headers) as url:
                    source = audio_source(AudioStream(url, tuple(headers.items())))
                    try:
                        self.assertEqual(len(source.read()), 3840)
                    finally:
                        source.cleanup()
            except OSError as error:
                if error.errno in {errno.EACCES, errno.EPERM}:
                    self.skipTest('Execution sandbox prevents binding a loopback test server.')
                raise

    def test_priming_preserves_first_frame_and_does_not_duplicate_audio(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / 'audio.wav'
            path.write_bytes(wav_audio())
            source = audio_source(str(path))
            try:
                source.prepare()
                source.prepare()
                frames = []
                while frame := source.read():
                    frames.append(frame)
                self.assertEqual(len(frames), 5)
            finally:
                source.cleanup()

    def test_http_access_denial_has_safe_diagnostic(self):
        with patch.dict(os.environ, {'http_proxy': '', 'https_proxy': '', 'no_proxy': '127.0.0.1'}):
            try:
                with audio_server(b'private-provider-error', status=403) as url:
                    source = audio_source(url + '?session=private-token')
                    try:
                        with self.assertRaises(AudioPlaybackError) as failure:
                            source.read()
                        message = str(failure.exception)
                        self.assertNotIn('private-token', message)
                        self.assertNotIn('private-provider-error', message)
                        self.assertNotIn('127.0.0.1', message)
                        self.assertIn('403', message)
                    finally:
                        source.cleanup()
            except OSError as error:
                if error.errno in {errno.EACCES, errno.EPERM}:
                    self.skipTest('Execution sandbox prevents binding a loopback test server.')
                raise

    @unittest.skipUnless(shutil.which('openssl'), 'OpenSSL is needed for the local TLS fixture')
    def test_https_rejects_untrusted_cert_then_uses_configured_ca(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            certificate, key = root / 'test-ca.pem', root / 'test-key.pem'
            subprocess.run(
                [
                    'openssl',
                    'req',
                    '-x509',
                    '-newkey',
                    'rsa:2048',
                    '-sha256',
                    '-nodes',
                    '-days',
                    '1',
                    '-keyout',
                    str(key),
                    '-out',
                    str(certificate),
                    '-subj',
                    '/CN=localhost',
                    '-addext',
                    'subjectAltName=DNS:localhost,IP:127.0.0.1',
                    '-addext',
                    'basicConstraints=critical,CA:TRUE',
                ],
                check=True,
                capture_output=True,
                timeout=15,
            )
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(certificate, key)
            with patch.dict(
                os.environ, {'http_proxy': '', 'https_proxy': '', 'no_proxy': '127.0.0.1'}
            ):
                try:
                    with audio_server(wav_audio(), tls_context=context) as url:
                        untrusted = audio_source(url + '?session=private-tls-session')
                        try:
                            with self.assertRaises(AudioPlaybackError) as failure:
                                untrusted.read()
                            message = str(failure.exception)
                            self.assertIn('TLS', message)
                            self.assertNotIn('private-tls-session', message)
                            self.assertNotIn('127.0.0.1', message)
                        finally:
                            untrusted.cleanup()
                        # Trust this fixture CA through the supported host trust selector.
                        # Verification remains enabled; the identical server is rejected above.
                        with patch.dict(os.environ, {'SSL_CERT_FILE': str(certificate)}):
                            trusted = audio_source(url)
                            try:
                                self.assertEqual(len(trusted.read()), 3840)
                            finally:
                                trusted.cleanup()
                except OSError as error:
                    if error.errno in {errno.EACCES, errno.EPERM}:
                        self.skipTest('Execution sandbox prevents binding a loopback test server.')
                    raise

    def test_large_stderr_is_drained_without_leaking_private_values(self):
        # Invoke the trusted interpreter directly so the fixture also works on
        # Compose's noexec /tmp mount; no production security setting is relaxed.
        script = (
            'import sys\n'
            'sys.stderr.buffer.write(b"x" * 300000)\n'
            'sys.stderr.write("Server returned 403 Forbidden: https://video.googlevideo.com/audio?signature=private-secret Cookie: private-cookie\\n")\n'
            'sys.stderr.flush()\n'
            'sys.exit(1)\n'
        )
        source = PCMSource(
            'https://video.googlevideo.com/audio?signature=private-secret',
            executable=sys.executable,
            before_options=shlex.join(['-c', script]),
        )
        pool = ThreadPoolExecutor(max_workers=1)
        try:
            result = pool.submit(source.read)
            with self.assertRaises(AudioPlaybackError) as failure:
                result.result(timeout=5)
            self.assertNotIn('private-secret', str(failure.exception))
            self.assertNotIn('private-cookie', str(failure.exception))
            self.assertNotIn('googlevideo.com', repr(failure.exception))
            self.assertLessEqual(len(source._diagnostics._tail), source._diagnostics.MAX_BYTES)
        finally:
            source.cleanup()
            pool.shutdown(wait=True)
