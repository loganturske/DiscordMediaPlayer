import asyncio
import json
import os
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from musicbot.media import Media, MediaError, Track, stream_url, youtube_url
from scripts.import_cookies import install


class MediaTests(unittest.TestCase):
    def test_supported_video_and_playlist_urls(self):
        for url in (
            'https://youtu.be/abc',
            'https://www.youtube.com/watch?v=abc',
            'https://music.youtube.com/playlist?list=abc',
            'https://youtube.com/shorts/abc',
            'https://youtube.com/live/abc',
        ):
            self.assertEqual(youtube_url(url), url)

    def test_reject_other_destinations_and_non_video_pages(self):
        for url in (
            'http://youtube.com/watch?v=a',
            'https://youtube.com.evil.test/watch?v=a',
            'https://evil.test/a',
            'file:///etc/passwd',
            'https://user:password@youtube.com/watch?v=a',
            'https://youtube.com:8080/watch?v=a',
            'https://youtube.com/@channel',
            'https://youtube.com/watch',
            'https://youtube.com/playlist',
        ):
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
            source.write_text(
                '# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t2147483647\tSID\tfake-session\n.example.com\tTRUE\t/\tTRUE\t2147483647\tother\tdiscard\n'
            )
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


class ValidationTests(unittest.TestCase):
    def test_reject_malformed_and_ambiguous_youtube_inputs(self):
        for url in (
            'https://youtu.be/a/b',
            'https://youtube.com/watch?v=a&v=b',
            'https://youtube.com/watch?v=a&list=https://evil.test',
            'https://youtube.com/watch?v=a\nheader',
            'https://youtube.com:invalid/watch?v=a',
            'https://[bad',
            'https://youtube.com/shorts/a/b',
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                youtube_url(url)

    def test_audio_destination_allowlist(self):
        valid = 'https://rr1---sn.googlevideo.com/videoplayback?signature=example'
        self.assertEqual(stream_url(valid), valid)
        for url in (
            'file:///etc/passwd',
            'http://rr1.googlevideo.com/audio',
            'https://googlevideo.com.evil.test/audio',
            'https://127.0.0.1/audio',
            'https://user:password@googlevideo.com/audio',
            'https://rr1.googlevideo.com:8888/audio',
            'https://rr1.googlevideo.com/a\nb',
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                stream_url(url)

    def test_import_limit_applies_even_if_extractor_ignores_it(self):
        class Extractor:
            def extract_info(self, *args, **kwargs):
                return {'entries': [{'id': 'one'}, None, {'id': 'three'}, {'id': 'four'}]}

        @contextmanager
        def extractor(**kwargs):
            yield Extractor()

        media = Media(Path('/nonexistent'))
        media.extractor = extractor
        entries = list(media.entries('https://youtube.com/playlist?list=test', 7, 3))
        self.assertEqual([t.title for t in entries], ['one', 'three'])

    def test_import_limit_does_not_fetch_the_next_remote_page(self):
        consumed = []

        def remote_entries():
            for item in ({'id': 'one'}, None, {'id': 'three'}):
                consumed.append(item)
                yield item
            raise AssertionError('Fetching another page after reaching the limit')

        class Extractor:
            def extract_info(self, *args, **kwargs):
                return {'entries': remote_entries()}

        @contextmanager
        def extractor(**kwargs):
            yield Extractor()

        media = Media(Path('/nonexistent'))
        media.extractor = extractor
        tracks = list(media.entries('https://youtube.com/playlist?list=test', 7, 3))
        self.assertEqual([track.title for track in tracks], ['one', 'three'])
        self.assertEqual(len(consumed), 3)

    def test_missing_and_malicious_entries_are_omitted(self):
        class Extractor:
            def extract_info(self, *args, **kwargs):
                return {
                    'entries': [None, {'title': 'no id'}, {'id': 'a&list=evil'}, {'id': 'good'}]
                }

        @contextmanager
        def extractor(**kwargs):
            yield Extractor()

        media = Media(Path('/nonexistent'))
        media.extractor = extractor
        self.assertEqual(
            [t.title for t in media.entries('https://youtube.com/playlist?list=test', 7, 10)],
            ['good'],
        )

    def test_sync_resolve_rejects_live_unavailable_and_external_streams(self):
        class Extractor:
            result = None

            def extract_info(self, *args, **kwargs):
                return self.result

        @contextmanager
        def extractor(**kwargs):
            yield Extractor()

        media = Media(Path('/nonexistent'))
        media.extractor = extractor
        for result in (
            None,
            {},
            {'url': 'https://cdn.googlevideo.com/a', 'is_live': True},
            {'url': 'https://evil.test/a'},
        ):
            Extractor.result = result
            with self.assertRaises(ValueError):
                media.resolve_sync('https://youtube.com/watch?v=test')
        Extractor.result = {'url': 'https://cdn.googlevideo.com/a'}
        self.assertEqual(
            media.resolve_sync('https://youtube.com/watch?v=test'), Extractor.result['url']
        )


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.media = Media(
            Path('/nonexistent'), resolve_timeout=2, inactivity_timeout=2, import_timeout=2
        )
        self.processes = []
        self.patchers = []
        self.track = Track('https://youtube.com/watch?v=test', 'Test', 7)

    async def asyncTearDown(self):
        await self.media.close()
        for process in self.processes:
            if process.returncode is None:
                process.kill()
                await process.communicate()
        for patcher in self.patchers:
            patcher.stop()

    def worker(self, script):
        async def spawn():
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                '-u',
                '-c',
                'import json,sys,time\nr=json.loads(sys.stdin.readline())\n' + script,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=(os.name == 'posix'),
            )
            self.processes.append(process)
            return process

        patcher = patch.object(self.media, '_spawn', spawn)
        patcher.start()
        self.patchers.append(patcher)

    async def test_import_and_resolve_use_separate_capacity(self):
        self.worker("""if r['mode'] == 'import':
 print(json.dumps({'type':'track','url':'https://youtube.com/watch?v=test','title':'one'}),flush=True)
 time.sleep(30)
else:
 print(json.dumps({'type':'stream','url':'https://cdn.googlevideo.com/audio'}),flush=True)
""")
        imports = [self.media.import_tracks(self.track.url, 7, 10) for _ in range(2)]
        try:
            for iterator in imports:
                self.assertEqual((await anext(iterator)).title, 'one')
            self.assertEqual(
                await self.media.resolve(self.track), 'https://cdn.googlevideo.com/audio'
            )
        finally:
            for iterator in imports:
                await iterator.aclose()
        self.assertTrue(all(p.returncode is not None for p in self.processes))
        self.assertFalse(self.media._processes)

    async def test_resolve_timeout_kills_and_reaps_worker(self):
        self.media.resolve_timeout = 0.1
        self.worker('time.sleep(30)')
        with self.assertRaisesRegex(MediaError, 'timed out'):
            await self.media.resolve(self.track)
        self.assertTrue(self.processes)
        self.assertTrue(all(p.returncode is not None for p in self.processes))
        self.assertFalse(self.media._processes)

    async def test_cancellation_reaps_worker_and_removes_cookie_directory(self):
        self.worker("""from pathlib import Path
import sys
Path(r['snapshot_root'],'private.txt').write_text('test-only')
print(json.dumps({'type':'track','url':'https://youtube.com/watch?v=test','title':r['snapshot_root']}),flush=True)
time.sleep(30)
""")
        iterator = self.media.import_tracks(self.track.url, 7, 10)
        first = await anext(iterator)
        directory = Path(first.title)
        self.assertTrue(directory.is_dir())
        task = asyncio.create_task(anext(iterator))
        await asyncio.sleep(0.02)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(directory.exists())
        self.assertFalse(self.media._processes)
        self.assertIsNotNone(self.processes[0].returncode)

    async def test_import_inactivity_timeout(self):
        self.media.inactivity_timeout = 0.1
        self.worker('time.sleep(30)')
        with self.assertRaisesRegex(MediaError, 'timed out'):
            async for _ in self.media.import_tracks(self.track.url, 7, 10):
                self.fail('No entry expected')
        self.assertIsNotNone(self.processes[0].returncode)

    async def test_worker_error_is_sanitized(self):
        self.worker("print(json.dumps({'type':'error','detail':'secret-cookie-value'}),flush=True)")
        with self.assertRaises(MediaError) as caught:
            await self.media.resolve(self.track)
        self.assertNotIn('secret-cookie-value', str(caught.exception))

    async def test_invalid_worker_protocol_and_unexpected_exit(self):
        for script in (
            "print('not-json',flush=True)",
            "print('[]',flush=True)",
            'pass',
            "print(json.dumps({'type':'bogus'}),flush=True)",
            "print(json.dumps({'type':'done'}),flush=True)",
        ):
            with self.subTest(script=script):
                self.worker(script)
                with self.assertRaises(MediaError):
                    await self.media.resolve(self.track)
                self.patchers.pop().stop()

    async def test_import_completes_and_is_bounded(self):
        self.worker("""print(json.dumps({'type':'track','url':'https://youtube.com/watch?v=test','title':'one'}),flush=True)
print(json.dumps({'type':'done'}),flush=True)
""")
        tracks = [track async for track in self.media.import_tracks(self.track.url, 7, 1)]
        self.assertEqual([t.title for t in tracks], ['one'])
        self.assertFalse(self.media._processes)

    async def test_overproducing_worker_is_rejected(self):
        self.worker("""for i in range(2):
 print(json.dumps({'type':'track','url':'https://youtube.com/watch?v=test','title':'one'}),flush=True)
""")
        iterator = self.media.import_tracks(self.track.url, 7, 1)
        self.assertEqual((await anext(iterator)).title, 'one')
        with self.assertRaisesRegex(MediaError, 'Invalid playlist response'):
            await anext(iterator)

    async def test_closed_media_rejects_new_extractions(self):
        await self.media.close()
        with self.assertRaisesRegex(MediaError, 'shutting down'):
            await self.media.resolve(self.track)

    async def test_real_worker_does_not_echo_invalid_input_or_credentials(self):
        # Exercises the actual entrypoint without permitting a network operation.
        process = await self.media._spawn()
        self.processes.append(process)
        result, _ = await process.communicate(json.dumps({'url': 'secret-value'}).encode() + b'\n')
        self.assertEqual(json.loads(result), {'type': 'error'})
        self.assertNotEqual(process.returncode, 0)

    async def test_repeated_cancellation_cannot_interrupt_process_cleanup(self):
        self.worker('time.sleep(30)')
        original = self.media._terminate
        cleaning = asyncio.Event()
        release = asyncio.Event()

        async def delayed_cleanup(process):
            cleaning.set()
            await release.wait()
            await original(process)

        with patch.object(self.media, '_terminate', delayed_cleanup):
            task = asyncio.create_task(self.media.resolve(self.track))
            while not self.media._processes:
                await asyncio.sleep(0.005)
            task.cancel()
            await cleaning.wait()
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertFalse(self.media._processes)
        self.assertIsNotNone(self.processes[0].returncode)

    async def test_spawn_does_not_pass_discord_credentials(self):
        from unittest.mock import AsyncMock

        with (
            patch.dict(os.environ, {'DISCORD_TOKEN': 'test-only-token'}),
            patch('musicbot.media.asyncio.create_subprocess_exec', new_callable=AsyncMock) as spawn,
        ):
            await self.media._spawn()
            self.assertNotIn('DISCORD_TOKEN', spawn.call_args.kwargs['env'])

    async def test_close_cancels_active_resolve_without_racing_stdout_reads(self):
        self.worker('time.sleep(30)')
        operation = asyncio.create_task(self.media.resolve(self.track))
        while not self.media._readers:
            await asyncio.sleep(0.005)
        await asyncio.gather(self.media.close(), self.media.close())
        with self.assertRaises(asyncio.CancelledError):
            await operation
        self.assertFalse(self.media._processes)
        self.assertIsNotNone(self.processes[0].returncode)

    async def test_close_handles_import_paused_between_yields(self):
        self.worker("""from pathlib import Path
import sys
Path(r['snapshot_root'],'private.txt').write_text('test-only')
print(json.dumps({'type':'track','url':'https://youtube.com/watch?v=test','title':r['snapshot_root']}),flush=True)
time.sleep(30)
""")
        iterator = self.media.import_tracks(self.track.url, 7, 10)
        directory = Path((await anext(iterator)).title)
        await asyncio.gather(self.media.close(), self.media.close())
        self.assertFalse(directory.exists())
        self.assertIsNotNone(self.processes[0].returncode)
        await iterator.aclose()  # a second cleanup is harmless
        self.assertFalse(self.media._processes)


class WorkerEntrypointTests(unittest.TestCase):
    def test_import_emits_tracks_then_completion(self):
        from io import StringIO

        from musicbot import extractor_worker

        request = {
            'mode': 'import',
            'url': 'https://youtube.com/playlist?list=test',
            'requester': 7,
            'limit': 1,
            'cookie_file': '/nonexistent',
            'snapshot_root': '/tmp',
        }
        output = StringIO()
        with (
            patch.object(extractor_worker, 'Media') as media,
            patch('sys.stdin', StringIO(json.dumps(request))),
            patch('sys.stdout', output),
        ):
            media.return_value.entries.return_value = [
                Track('https://youtube.com/watch?v=test', 'Title', 7)
            ]
            self.assertEqual(extractor_worker.main(), 0)
        messages = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(messages[0]['type'], 'track')
        self.assertEqual(messages[1], {'type': 'done'})

    def test_resolve_emits_stream(self):
        from io import StringIO

        from musicbot import extractor_worker

        request = {
            'mode': 'resolve',
            'url': 'https://youtube.com/watch?v=test',
            'cookie_file': '/nonexistent',
            'snapshot_root': '/tmp',
        }
        output = StringIO()
        with (
            patch.object(extractor_worker, 'Media') as media,
            patch('sys.stdin', StringIO(json.dumps(request))),
            patch('sys.stdout', output),
        ):
            media.return_value.resolve_sync.return_value = 'https://cdn.googlevideo.com/audio'
            self.assertEqual(extractor_worker.main(), 0)
        self.assertEqual(
            json.loads(output.getvalue()),
            {'type': 'stream', 'url': 'https://cdn.googlevideo.com/audio'},
        )

    def test_errors_only_emit_safe_status(self):
        from io import StringIO

        from musicbot import extractor_worker

        request = {
            'mode': 'invalid',
            'url': 'secret-value',
            'cookie_file': '/nonexistent',
            'snapshot_root': '/tmp',
        }
        output = StringIO()
        with patch('sys.stdin', StringIO(json.dumps(request))), patch('sys.stdout', output):
            self.assertEqual(extractor_worker.main(), 1)
        self.assertEqual(json.loads(output.getvalue()), {'type': 'error'})
