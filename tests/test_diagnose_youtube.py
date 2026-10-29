import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock, patch

from musicbot.audio import AudioPlaybackError
from scripts.diagnose_youtube import DiagnosticLogger, probe, run_isolated, safe_label


class DiagnosticTests(unittest.TestCase):
    def test_logger_only_exposes_known_signals(self):
        logger = DiagnosticLogger()
        logger.warning('GVS PO Token missing; https://private.invalid/?secret=private-cookie')
        logger.error('HTTP Error 403 private-cookie')
        self.assertEqual(logger.signals, {'playback_token_mentioned', 'download_forbidden'})
        self.assertNotIn('private-cookie', json.dumps(sorted(logger.signals)))
        self.assertEqual(safe_label('https://private.invalid/token'), 'unavailable')

    def test_probe_distinguishes_native_download_and_decoder_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cookies = root / 'cookies.txt'
            cookies.write_text('private-cookie')
            ydl = Mock()
            ydl.params = {}
            ydl.extract_info.return_value = {
                'url': 'https://cdn.googlevideo.com/audio?private-token',
                'format_id': '251',
                'protocol': 'https',
                'acodec': 'opus',
            }
            ydl.dl.return_value = (True, None)
            source = Mock()
            source.prepare.side_effect = AudioPlaybackError(
                'FFmpeg could not download audio (HTTP 403).'
            )

            @contextmanager
            def extractor(self):
                yield ydl

            with (
                patch('scripts.diagnose_youtube.Media.extractor', extractor),
                patch('scripts.diagnose_youtube.audio_source', return_value=source),
            ):
                result = probe('https://youtube.com/watch?v=test', cookies, root)
            self.assertTrue(result['cookie_file_present'])
            self.assertEqual(result['native_download'], 'passed')
            self.assertEqual(result['ffmpeg_decode'], 'failed')
            self.assertNotIn('private-token', json.dumps(result))
            self.assertNotIn('private-cookie', json.dumps(result))
            source.cleanup.assert_called_once()

    def test_probe_never_reports_raw_provider_exception(self):
        ydl = Mock()
        ydl.params = {}
        ydl.extract_info.side_effect = ValueError('private-token-in-provider-error')

        @contextmanager
        def extractor(self):
            yield ydl

        with (
            tempfile.TemporaryDirectory() as temporary,
            patch('scripts.diagnose_youtube.Media.extractor', extractor),
        ):
            result = probe(
                'https://youtube.com/watch?v=test', Path('/nonexistent'), Path(temporary)
            )
        self.assertEqual(result['extraction'], 'failed')
        self.assertNotIn('private-token', json.dumps(result))

    def test_timeout_reaps_diagnostic_process(self):
        import subprocess

        process = Mock()
        process.pid = 100000000
        process.communicate.side_effect = [subprocess.TimeoutExpired('private', 1), (b'', None)]
        with (
            patch('scripts.diagnose_youtube.subprocess.Popen', return_value=process),
            patch('scripts.diagnose_youtube.os.killpg') as kill,
        ):
            result = run_isolated('https://youtube.com/watch?v=test', timeout=1)
        self.assertEqual(result['diagnostic'], 'timed_out')
        kill.assert_called_once()
        self.assertEqual(process.communicate.call_count, 2)

    def test_successful_worker_output_and_token_exclusion(self):
        process = Mock()
        process.pid = 100000000
        process.communicate.return_value = (
            b'{"native_download":"passed","ffmpeg_decode":"passed"}',
            None,
        )
        with (
            patch.dict('os.environ', {'DISCORD_TOKEN': 'private-discord-token'}),
            patch('scripts.diagnose_youtube.subprocess.Popen', return_value=process) as spawn,
            patch('scripts.diagnose_youtube.os.killpg'),
        ):
            result = run_isolated('https://youtube.com/watch?v=test', anonymous=True)
        self.assertEqual(result['ffmpeg_decode'], 'passed')
        self.assertNotIn('DISCORD_TOKEN', spawn.call_args.kwargs['env'])
        self.assertIn('--anonymous', spawn.call_args.args[0])

    def test_real_worker_rejects_invalid_input_without_network_or_private_output(self):
        result = run_isolated('https://invalid.example/private-token', timeout=5)
        self.assertEqual(result, {'diagnostic': 'worker_failed'})
        self.assertNotIn('private-token', json.dumps(result))
