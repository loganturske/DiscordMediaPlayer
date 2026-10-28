from contextlib import contextmanager
from pathlib import Path
import tempfile
import unittest

from musicbot.media import Media, youtube_url
from scripts.import_cookies import install


class MediaTests(unittest.TestCase):
    def test_supported_video_and_playlist_urls(self):
        for url in ('https://youtu.be/abc', 'https://www.youtube.com/watch?v=abc',
                    'https://music.youtube.com/playlist?list=abc',
                    'https://youtube.com/shorts/abc', 'https://youtube.com/live/abc'):
            self.assertEqual(youtube_url(url), url)

    def test_reject_other_destinations_and_non_video_pages(self):
        for url in ('http://youtube.com/watch?v=a', 'https://youtube.com.evil.test/watch?v=a',
                    'https://evil.test/a', 'file:///etc/passwd',
                    'https://user:password@youtube.com/watch?v=a',
                    'https://youtube.com:8080/watch?v=a', 'https://youtube.com/@channel',
                    'https://youtube.com/watch', 'https://youtube.com/playlist'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                youtube_url(url)

    def test_playlist_is_lazy_and_canonicalizes_video_urls(self):
        consumed = []
        def entries():
            for n in range(10000):
                consumed.append(n)
                yield {'id': f'video{n}', 'title': f'Track {n}'}
        class Extractor:
            def extract_info(self, *args, **kwargs):
                return {'entries': entries()}
        @contextmanager
        def extractor(**kwargs):
            yield Extractor()
        media = Media(Path('/nonexistent'))
        media.extractor = extractor
        iterator = media.entries('https://youtube.com/playlist?list=test', 7, 10000)
        self.assertEqual(next(iterator).url, 'https://www.youtube.com/watch?v=video0')
        self.assertEqual(consumed, [0])
        iterator.close()

    def test_cookie_import_persists_privately_and_filters_domains(self):
        with tempfile.TemporaryDirectory() as temp:
            source, target = Path(temp) / 'export.txt', Path(temp) / 'data/cookies.txt'
            source.write_text('# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t2147483647\tSID\tfake-session\n.example.com\tTRUE\t/\tTRUE\t2147483647\tother\tdiscard\n')
            install(source, target)
            self.assertIn('fake-session', target.read_text())
            self.assertNotIn('discard', target.read_text())
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            media = Media(target)
            original = target.read_bytes()
            with media.extractor() as ydl:
                snapshot = Path(ydl.params['cookiefile'])
                self.assertNotEqual(snapshot, target)
                snapshot.write_text('# Netscape HTTP Cookie File\n')
            self.assertEqual(target.read_bytes(), original)
            self.assertFalse(snapshot.exists())

    def test_invalid_cookie_import_preserves_existing_session(self):
        with tempfile.TemporaryDirectory() as temp:
            source, target = Path(temp) / 'export.txt', Path(temp) / 'cookies.txt'
            source.write_text('# Netscape HTTP Cookie File\n')
            target.write_text('existing')
            with self.assertRaises(ValueError):
                install(source, target)
            self.assertEqual(target.read_text(), 'existing')
