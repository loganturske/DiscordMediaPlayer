"""Safely compare yt-dlp and FFmpeg downloads from the bot's actual container.

Run: python -m scripts.diagnose_youtube YOUTUBE_URL [--anonymous]
Output contains only allowlisted diagnostic fields, never provider error text,
cookie values, request headers, video titles, or signed stream URLs.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

from yt_dlp.version import __version__

from musicbot.audio import AudioPlaybackError, audio_source
from musicbot.media import AudioStream, Media, stream_headers, stream_url, youtube_url


class DiagnosticLogger:
    """Convert untrusted extractor messages to a small set of public signals."""

    SIGNALS = {
        'playback_token_mentioned': ('po token', 'po_token', 'proof of origin'),
        'javascript_challenge_mentioned': (
            'challenge solving failed',
            'nsig extraction failed',
            'signature extraction failed',
            'javascript runtime',
        ),
        'session_problem_mentioned': (
            'cookies are no longer valid',
            'cookies have expired',
            'account cookies are no longer valid',
        ),
        'sign_in_requested': ('sign in to confirm', 'login required', 'authentication required'),
        'download_forbidden': ('http error 403', '403 forbidden', 'server returned 403'),
        'sabr_streaming_mentioned': ('sabr',),
    }

    def __init__(self):
        self.signals = set()

    def debug(self, message):
        text = str(message).lower()
        for name, needles in self.SIGNALS.items():
            if any(needle in text for needle in needles):
                self.signals.add(name)

    warning = debug
    error = debug


def safe_label(value):
    text = str(value)
    return text if re.fullmatch(r'[A-Za-z0-9_+. -]{1,60}', text) else 'unavailable'


def probe(url: str, cookie_file: Path, snapshot_root: Path) -> dict:
    """Compare one extracted stream with native download and real PCM decoding."""
    report = {'yt_dlp_version': __version__, 'cookie_file_present': cookie_file.is_file()}
    logger = DiagnosticLogger()
    media = Media(cookie_file, snapshot_root=snapshot_root)
    try:
        with media.extractor() as ydl:
            ydl.params['logger'] = logger
            ydl.params['no_warnings'] = False  # Route warnings only to the classifier.
            # Native format tests write only a short temporary sample. A read-only
            # /app must never become their output directory.
            ydl.params['paths'] = {'temp': str(snapshot_root)}
            info = ydl.extract_info(youtube_url(url), download=False)
            if not info or not info.get('url'):
                report['extraction'] = 'no_stream'
                return report
            if info.get('is_live'):
                report['extraction'] = 'live_stream_unsupported'
                return report
            stream = AudioStream(
                stream_url(info['url']), stream_headers(info.get('http_headers', {}))
            )
            report['extraction'] = 'stream_found'
            report['format'] = {
                key: safe_label(info.get(key)) for key in ('format_id', 'protocol', 'acodec')
            }
            try:
                success, _ = ydl.dl(str(snapshot_root / 'native-probe.tmp'), info, test=True)
                report['native_download'] = 'passed' if success else 'failed'
            except Exception:
                report['native_download'] = 'failed'
            source = None
            try:
                source = audio_source(stream)
                source.prepare()
                report['ffmpeg_decode'] = 'passed'
            except AudioPlaybackError as error:
                report['ffmpeg_decode'] = 'failed'
                report['decoder_reason'] = str(error)  # Module-generated classification only.
            except Exception:
                report['ffmpeg_decode'] = 'failed'
                report['decoder_reason'] = 'Unexpected decoder setup failure.'
            finally:
                if source is not None:
                    source.cleanup()
    except Exception:
        # yt-dlp exceptions can contain private URLs. Do not serialize their text.
        report.setdefault('extraction', 'failed')
    finally:
        report['extractor_signals'] = sorted(logger.signals)
    return report


def run_isolated(url: str, *, anonymous=False, timeout=75) -> dict:
    """Bound extraction/decoding and reap child runtimes before removing snapshots."""
    with tempfile.TemporaryDirectory(prefix='discord-diagnostic-') as temporary:
        command = [
            sys.executable,
            '-m',
            'scripts.diagnose_youtube',
            url,
            '--worker-root',
            temporary,
        ]
        if anonymous:
            command.append('--anonymous')
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={key: value for key, value in os.environ.items() if key != 'DISCORD_TOKEN'},
            start_new_session=(os.name == 'posix'),
        )
        try:
            try:
                stdout, _ = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                return {'diagnostic': 'timed_out', 'seconds': timeout}
            try:
                result = json.loads(stdout)
            except (ValueError, UnicodeDecodeError):
                return {'diagnostic': 'worker_failed'}
            return result if isinstance(result, dict) else {'diagnostic': 'worker_failed'}
        finally:
            # The worker may have spawned Node or FFmpeg. Linux containers isolate
            # these descendants in a session that belongs only to this probe.
            if os.name == 'posix':
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            elif process.poll() is None:
                process.kill()
            process.communicate(timeout=5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('url')
    parser.add_argument(
        '--anonymous',
        action='store_true',
        help='Test without reading the saved session; the file is never changed.',
    )
    parser.add_argument('--worker-root', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        url = youtube_url(args.url)
    except ValueError:
        parser.error('Use an HTTPS YouTube video URL.')
    if args.worker_root:
        cookie_file = (
            args.worker_root / 'no-cookies.txt'
            if args.anonymous
            else Path(os.environ.get('YOUTUBE_COOKIE_FILE', 'data/youtube-cookies.txt'))
        )
        result = probe(url, cookie_file, args.worker_root)
    else:
        result = run_isolated(url, anonymous=args.anonymous)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
