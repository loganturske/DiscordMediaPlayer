"""Decode media with bounded private diagnostics and explicit playback failures.

Only classified error messages leave this module. FFmpeg stderr may contain signed
URLs or headers, so its bounded tail must never be logged or exposed to Discord.
"""

from __future__ import annotations

import shlex
import ssl
import subprocess
import threading
from pathlib import Path
from urllib.parse import urlparse

import discord

from .media import AudioStream, stream_headers


class AudioPlaybackError(RuntimeError):
    """A classified playback failure safe to show in logs and Discord."""


class FFmpegDiagnostics:
    """Drain stderr without blocking FFmpeg or retaining unbounded output."""

    MAX_BYTES = 8192

    def __init__(self):
        self._tail = bytearray()
        self._lock = threading.Lock()

    def write(self, data: bytes) -> int:
        with self._lock:
            self._tail.extend(data[-self.MAX_BYTES :])
            del self._tail[: -self.MAX_BYTES]
        return len(data)

    def summary(self) -> str:
        with self._lock:
            text = self._tail.decode('utf-8', errors='replace').lower()
        if any(
            value in text
            for value in (
                'certificate verify',
                'certificate verification',
                'peer certificate',
                'issuer certificate',
            )
        ):
            return 'FFmpeg could not verify the media server TLS certificate. Check the host trust store.'
        for status, explanation in (
            (
                403,
                'YouTube rejected the audio request. Try another video or refresh the saved YouTube session.',
            ),
            (401, 'The media server requires authentication. Refresh the saved YouTube session.'),
            (404, 'The audio stream is no longer available. Queue the video again.'),
            (429, 'YouTube is rate-limiting this host. Wait before trying again.'),
        ):
            if (
                f'http error {status}' in text
                or f'server returned {status}' in text
                or f'{status} ' in text
            ):
                return f'FFmpeg could not download audio (HTTP {status}). {explanation}'
        if any(
            value in text
            for value in ('option not found', 'unrecognized option', 'error setting option')
        ):
            return 'FFmpeg rejected an audio input option. Check the installed FFmpeg version.'
        if any(
            value in text
            for value in (
                'timed out',
                'connection refused',
                'network is unreachable',
                'resolve hostname',
                'name or service not known',
            )
        ):
            return 'FFmpeg could not reach the media server. Check host connectivity, DNS, or proxy settings.'
        return 'FFmpeg could not decode the audio stream. Try another recorded video and check the host setup.'


class PCMSource(discord.FFmpegPCMAudio):
    """A PCM source that detects failed/empty streams before announcing playback."""

    FRAME_BYTES = 3840

    def __init__(self, source, *, executable='ffmpeg', before_options=None, options=None):
        self._diagnostics = FFmpegDiagnostics()
        self._diagnostic_thread = None
        self._stderr_pipe = None
        self._frames = 0
        self._prefetched = None
        self._cleaned = False
        super().__init__(
            source, executable=executable, before_options=before_options, options=options
        )

    def _spawn_process(self, args, **kwargs):
        # The library logs argv at DEBUG and its stderr adapter cannot safely use
        # DEVNULL/PIPE constants. Own this one pipe and never log provider argv.
        kwargs['stderr'] = subprocess.PIPE
        try:
            process = subprocess.Popen(args, **kwargs)
        except OSError:
            raise AudioPlaybackError(
                'FFmpeg could not start. Check the executable and host resources.'
            ) from None
        self._stderr_pipe = process.stderr
        self._diagnostic_thread = threading.Thread(
            target=self._drain_stderr,
            args=(process.stderr,),
            daemon=True,
            name='music-ffmpeg-diagnostics',
        )
        self._diagnostic_thread.start()
        return process

    def _drain_stderr(self, pipe):
        try:
            while data := pipe.read(4096):
                self._diagnostics.write(data)
        except (OSError, ValueError):
            pass  # A concurrent cleanup may close the pipe after killing FFmpeg.

    def _check_process_returncode(self):
        # Avoid the base implementation's exception containing raw stderr.
        if self._process and not self._stopped:
            code = self._process.poll()
            if code is not None and code != 0:
                self._current_error = AudioPlaybackError(self._diagnostics.summary())

    def _read_frame(self):
        frame = self._stdout.read(self.FRAME_BYTES)
        if len(frame) == self.FRAME_BYTES:
            self._frames += 1
            return frame
        # EOF can arrive just before waitpid observes exit status. A single poll
        # misclassifies that race as a successful end of track.
        try:
            code = self._process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            raise AudioPlaybackError(
                'FFmpeg stopped producing audio without exiting. Check the stream and host setup.'
            ) from None
        if self._diagnostic_thread:
            self._diagnostic_thread.join(timeout=1)
        if code != 0 or self._frames == 0:
            raise AudioPlaybackError(self._diagnostics.summary())
        return b''

    def prepare(self):
        """Prefetch one frame off the event loop; playback must pass this check."""
        if self._prefetched is None:
            self._prefetched = self._read_frame()

    def read(self):
        if self._prefetched is not None:
            frame, self._prefetched = self._prefetched, None
            return frame
        return self._read_frame()

    def cleanup(self):
        """Reap FFmpeg and close both pipes, including after partial construction."""
        if self._cleaned:
            return
        self._cleaned = True
        self._stopped = True
        process = getattr(self, '_process', None)
        stdout = getattr(self, '_stdout', None)
        try:
            if process:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)
        finally:
            # Our stderr thread is the only reader; communicate() would introduce
            # a second reader and can race with it during an interrupted stream.
            if stdout:
                stdout.close()
            if self._diagnostic_thread:
                self._diagnostic_thread.join(timeout=2)
            if self._stderr_pipe:
                self._stderr_pipe.close()
            self._process = self._stdout = self._stdin = self._stderr = discord.utils.MISSING


def audio_source(stream: str | AudioStream) -> PCMSource:
    """Preserve extractor request headers and verify HTTPS against the host CA."""
    url = stream.url if isinstance(stream, AudioStream) else stream
    headers = stream_headers(dict(stream.headers)) if isinstance(stream, AudioStream) else ()
    options = ['-nostdin']
    if urlparse(url).scheme in {'http', 'https'}:
        options += [
            '-reconnect',
            '1',
            '-reconnect_streamed',
            '1',
            '-reconnect_delay_max',
            '5',
            '-rw_timeout',
            '20000000',
        ]
        if headers:
            options += ['-headers', ''.join(f'{name}: {value}\r\n' for name, value in headers)]
        if urlparse(url).scheme == 'https':
            options += ['-tls_verify', '1']
            ca_file = ssl.get_default_verify_paths().cafile
            if ca_file and Path(ca_file).is_file():
                options += ['-ca_file', ca_file]
    return PCMSource(url, before_options=shlex.join(options), options='-vn -sn -dn -loglevel error')
