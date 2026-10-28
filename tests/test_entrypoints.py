import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from musicbot.bot import main
from scripts.import_cookies import main as cookie_main


class EntrypointTests(unittest.TestCase):
    def test_missing_token_and_invalid_configuration_fail_before_connecting(self):
        with patch.dict('os.environ', {}, clear=True):
            with self.assertRaisesRegex(SystemExit, 'Set DISCORD_TOKEN'):
                main()
        with patch.dict(
            'os.environ', {'DISCORD_TOKEN': 'never-log-this', 'MAX_PLAYERS': '0'}, clear=True
        ):
            with self.assertRaisesRegex(SystemExit, 'Invalid bot setup') as error:
                main()
            self.assertNotIn('never-log-this', str(error.exception))

    def test_cookie_cli_installs_and_reports_no_session_contents(self):
        with tempfile.TemporaryDirectory() as temp:
            source, destination = Path(temp) / 'export', Path(temp) / 'data/cookies.txt'
            source.write_text(
                '# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t2147483647\tSID\tprivate-test-session\n'
            )
            output = io.StringIO()
            with (
                patch(
                    'sys.argv',
                    ['import_cookies.py', str(source), '--destination', str(destination)],
                ),
                redirect_stdout(output),
            ):
                cookie_main()
            self.assertTrue(destination.exists())
            self.assertIn('saved privately', output.getvalue())
            self.assertNotIn('private-test-session', output.getvalue())

    def test_malformed_cookie_error_redacts_input(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / 'export'
            source.write_text('private-test-session')
            with patch('sys.argv', ['import_cookies.py', str(source)]):
                with self.assertRaises(SystemExit) as error:
                    cookie_main()
            self.assertIn('Cookie import failed', str(error.exception))
            self.assertNotIn('private-test-session', str(error.exception))
