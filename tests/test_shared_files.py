import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).parents[2]))
from munpia_free import core as c, publication
from munpia_free.shared_settings import publish_settings, read_settings


class SharedFilesTests(unittest.TestCase):
    def test_peer_files_integrity_root_and_refresh(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / 'books'
            root.mkdir()
            own = c.History(base / 'munpia_free_history.db')
            peer = c.History(base / 'munpia_gold_history.db')
            path = root / 'chapter.txt'
            path.write_bytes(b'original')
            peer.record('1', {'id': 10, 'num': 1, 'title': 'one'}, 'book', 'completed',
                        path, hashlib.sha256(path.read_bytes()).hexdigest())
            own.sync_peer('1', root)
            self.assertTrue(own.complete('1', '10', root))
            self.assertFalse(own.complete('1', '10', base / 'elsewhere'))
            path.write_bytes(b'changed')
            self.assertFalse(own.complete('1', '10', root))
            peer.record('1', {'id': 10, 'num': 1}, 'book', 'completed',
                        path, hashlib.sha256(path.read_bytes()).hexdigest())
            own.sync_peer('1', root)
            self.assertTrue(own.complete('1', '10', root))
            path.unlink()
            own.sync_peer('1', root)
            self.assertFalse(own.complete('1', '10', root))

    def test_sibling_cache_matches_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            path = folder / '.munpia_gold' / 'entries' / '10.json'
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({'version': 1, 'txt_sha256': 'expected',
                                       'blocks': [{'type': 'text', 'text': 'synthetic'}]}))
            self.assertIsNotNone(publication.load_cache(folder, {'entry_id': '10', 'sha256': 'expected'}))
            self.assertIsNone(publication.load_cache(folder, {'entry_id': '10', 'sha256': 'wrong'}))

    def test_download_reuses_peer_folder_and_skips_existing_episode(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / 'books'
            folder = root / 'Old title [1]'
            folder.mkdir(parents=True)
            path = folder / '001_old [10].txt'
            path.write_text('1. one\n\nsynthetic original')
            peer = c.History(base / 'munpia_gold_history.db')
            peer.record('1', {'id': 10, 'num': 1, 'title': 'one'}, 'Old title', 'completed',
                        path, hashlib.sha256(path.read_bytes()).hexdigest())
            called = []
            chapters = [{'id': 10, 'num': 1, 'title': 'one', 'free': True},
                        {'id': 20, 'num': 2, 'title': 'two', 'free': True}]
            def transport(url, params):
                if url.endswith('my-info-simple'):
                    return {'login': True}
                if url.endswith('/chapters'):
                    data = {'total': len(chapters), 'next': False, 'list': chapters}
                elif '/entries/' in url:
                    called.append(url.rsplit('/', 1)[-1])
                    data = {'entry': {'id': 20, 'content': 'synthetic new'}}
                else:
                    data = {'novelInfo': {'id': 1, 'title': 'New title'}}
                return {'code': 'M000_00000', 'result': data}
            engine = c.Engine(base / 'munpia_free_history.db',
                              client_factory=lambda **kw: c.Client(transport=transport, **kw))
            conf = {'cookie': 'test=synthetic', 'download_path': str(root), 'request_delay': 1,
                    'max_per_title': 10, 'make_epub': True, 'include_author_comment': False,
                    'episode_digits': 4}
            engine.start('download', ['1'], conf)
            engine.thread.join(5)
            self.assertFalse(engine.thread.is_alive())
            self.assertEqual(engine.snapshot()['status'], 'completed')
            self.assertEqual(called, ['20'])
            self.assertEqual(len(engine.history.rows(nid='1')), 2)
            self.assertTrue(all(Path(row['path']).parent == folder for row in engine.history.rows(nid='1')))
            self.assertTrue(list(folder.glob('*.epub')))
            self.assertTrue((folder / '0002_two.txt').is_file())
            self.assertFalse((folder / '0002_two [20].txt').exists())
            self.assertTrue(path.is_file())  # Legacy names remain recognized.
            called.clear()
            engine.start('download', ['1'], conf)
            engine.thread.join(5)
            self.assertFalse(engine.thread.is_alive())
            self.assertEqual(called, [])
            self.assertEqual(engine.snapshot()['skipped'], 2)
            original = (folder / '0002_two.txt').read_bytes()
            chapters.append({'id': 30, 'num': 2, 'title': 'two', 'free': True})
            engine.start('download', ['1'], conf)
            engine.thread.join(5)
            self.assertFalse(engine.thread.is_alive())
            self.assertEqual(engine.snapshot()['failed'], 1)
            self.assertEqual((folder / '0002_two.txt').read_bytes(), original)
            other = c.Engine(base / 'munpia_gold_history.db')
            self.assertEqual(engine.lock_path, other.lock_path)

    def test_settings_snapshot_excludes_lists_and_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            conf = dict(download_path='/books', episode_digits=3, max_per_title=10,
                        request_delay=2, make_epub=True, include_author_comment=False,
                        epub_line_height=1.8, epub_paragraph_gap=.55, titles='123', cookie='secret')
            publish_settings(directory, conf, c.atomic_write)
            loaded = read_settings(directory)
            self.assertEqual(loaded['download_path'], '/books')
            self.assertNotIn('cookie', loaded)
            self.assertNotIn('titles', loaded)
