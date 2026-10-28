import io
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from scripts.container_smoke import check_non_root, check_state, main


class ContainerSmokeTests(unittest.TestCase):
    def test_root_is_rejected(self):
        with patch('scripts.container_smoke.os.geteuid', return_value=0):
            with self.assertRaisesRegex(RuntimeError, 'non-root'):
                check_non_root()

    def test_synthetic_state_check_does_not_touch_existing_cookies(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            actual = root / 'youtube-cookies.txt'
            actual.write_text('existing-private-data')
            check_state(root)
            self.assertEqual(actual.read_text(), 'existing-private-data')
            self.assertEqual(list(root.iterdir()), [actual])

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('node'), 'runtime tools missing')
    def test_offline_smoke_uses_real_audio_and_cleans_up(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = io.StringIO()
            with (
                patch.dict(os.environ, {'YOUTUBE_COOKIE_FILE': str(root / 'cookies.txt')}),
                patch('scripts.container_smoke.os.geteuid', return_value=10001),
                redirect_stdout(output),
            ):
                main()
            self.assertIn('Container smoke check passed', output.getvalue())
            self.assertEqual(list(root.iterdir()), [])
