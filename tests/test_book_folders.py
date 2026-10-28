import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).parents[2]))
from munpia_free import core as c


class BookFolderTests(unittest.TestCase):
    def test_move_assets_and_both_histories(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'books'
            folder = root / 'Old [1]'
            folder.mkdir(parents=True)
            path = folder / '001_test.txt'
            path.write_bytes(b'content')
            (folder / 'images').mkdir()
            (folder / 'images' / 'test.jpg').write_bytes(b'image')
            own = c.History(Path(tmp) / 'munpia_free_history.db')
            peer = c.History(Path(tmp) / 'munpia_gold_history.db')
            for history in (own, peer):
                history.record('1', {'id': 10, 'num': 1}, 'Old', 'completed',
                               path, hashlib.sha256(b'content').hexdigest())
            warnings = []
            self.assertTrue(own.rename_book_folder('1', 'New', root, warnings.append))
            self.assertFalse(folder.exists())
            self.assertEqual((root / 'New [1]' / 'images' / 'test.jpg').read_bytes(), b'image')
            for history in (own, peer):
                self.assertTrue(history.complete('1', 10, root))
                self.assertEqual(history.rows(nid='1')[0]['title'], 'New')
            self.assertFalse(own.rename_book_folder('1', 'New', root, warnings.append))
            self.assertEqual(warnings, [])
            (root / 'Other [1]').mkdir()
            self.assertFalse(own.rename_book_folder('1', 'Third', root, warnings.append))
            self.assertTrue((root / 'New [1]').exists())
            self.assertEqual(len(warnings), 1)

    def test_failed_database_commit_rolls_back_folder_move(self):
        from contextlib import contextmanager
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / 'Old [1]'
            folder.mkdir()
            (folder / 'keep.txt').write_bytes(b'keep')
            history = c.History(root / 'test.db')
            original = history.connect
            @contextmanager
            def failing_commit():
                with original() as db:
                    yield db
                    raise OSError('commit failed')
            warnings = []
            with patch.object(history, 'connect', failing_commit):
                self.assertFalse(history.rename_book_folder('1', 'New', root, warnings.append))
            self.assertTrue((folder / 'keep.txt').is_file())
            self.assertFalse((root / 'New [1]').exists())
            self.assertEqual(len(warnings), 1)

    def test_refresh_cleans_all_files_before_remote_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'books'
            history_path = Path(tmp) / 'munpia_free_history.db'
            history = c.History(history_path)
            for nid in ('1', '2'):
                folder = root / ('Old [%s]' % nid)
                folder.mkdir(parents=True)
                path = folder / '0035_35화 [9025825].txt'
                path.write_bytes(b'original')
                history.record(nid, {'id': 9025825, 'num': 35}, 'Old', 'completed',
                               path, hashlib.sha256(b'original').hexdigest())
            calls = []
            def transport(url, params):
                if url.endswith('my-info-simple'):
                    return {'login': True}
                calls.append(url)
                raise c.MunpiaError('HTTP 400 코드 A002_12002')
            engine = c.Engine(history_path, client_factory=lambda **kw: c.Client(transport=transport, **kw))
            conf = {'download_path': str(root), 'request_delay': 1, 'cookie': 'test=value',
                    'max_per_title': 1, 'make_epub': False, 'include_author_comment': False}
            engine.start('refresh', ['1', '2'], conf)
            engine.thread.join(5)
            self.assertFalse(engine.thread.is_alive())
            self.assertEqual(engine.snapshot()['renamed'], 2)
            self.assertEqual(engine.snapshot()['failed'], 2)
            self.assertEqual(len(calls), 2)
            for nid in ('1', '2'):
                folder = root / ('Old [%s]' % nid)
                self.assertTrue((folder / '0035_35화.txt').exists())
                self.assertFalse((folder / '0035_35화 [9025825].txt').exists())
                self.assertTrue(history.complete(nid, 9025825, root))


    def test_unavailable_removed_and_next_title_processed(self):
        with tempfile.TemporaryDirectory() as tmp:
            removed = []
            calls = []
            def transport(url, params):
                if url.endswith('my-info-simple'):
                    return {'login': True}
                calls.append(url)
                if '/1' in url:
                    return {'code': 'A002_12002'}
                raise c.MunpiaError('temporary failure')
            engine = c.Engine(Path(tmp) / 'test.db', client_factory=lambda **kw: c.Client(transport=transport, **kw))
            conf = dict(download_path=tmp, request_delay=1, cookie='test=value', max_per_title=1,
                        make_epub=False, include_author_comment=False, remove_unavailable_title=removed.append)
            engine.start('refresh', ['1', '2'], conf)
            engine.thread.join(5)
            self.assertFalse(engine.thread.is_alive())
            self.assertEqual(removed, ['1'])
            self.assertEqual(len(calls), 2)
            self.assertEqual(engine.snapshot()['failed'], 1)
            self.assertEqual(engine.snapshot()['skipped'], 1)

    def test_scan_without_saved_list_adopts_only_matching_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'books'
            good = root / 'Book [1]'
            bad = root / 'Wrong [2]'
            good.mkdir(parents=True)
            bad.mkdir()
            (good / '0001_one [10].txt').write_text('1. one\n\nExisting text')
            (bad / '0001_one [10].txt').write_text('Existing text')
            def transport(url, params):
                if url.endswith('my-info-simple'):
                    return {'login': True}
                if url.endswith('/chapters'):
                    self.assertNotIn('/2/', url)
                    data = {'total': 1, 'next': False, 'list': [{'id': 10, 'num': 1, 'title': 'one', 'free': True}]}
                elif '/entries/' in url:
                    data = {'entry': {'id': 10, 'content': 'Existing text'}}
                else:
                    data = {'novelInfo': {'id': int(url.rsplit('/', 1)[1]), 'title': 'Book'}}
                return {'code': 'M000_00000', 'result': data}
            engine = c.Engine(Path(tmp) / 'test.db', client_factory=lambda **kw: c.Client(transport=transport, **kw))
            conf = dict(download_path=str(root), request_delay=1, cookie='test=value', max_per_title=10,
                        make_epub=True, include_author_comment=False, scan_folders=True)
            engine.start('refresh', [], conf)
            engine.thread.join(5)
            self.assertFalse(engine.thread.is_alive())
            self.assertEqual(engine.snapshot()['status'], 'completed')
            self.assertEqual(engine.snapshot()['adopted'], 1)
            self.assertEqual(engine.snapshot()['renamed'], 1)
            self.assertTrue(engine.history.complete('1', '10', root))
            self.assertTrue((good / '0001_one.txt').exists())
            self.assertTrue(list(good.glob('*.epub')))
            self.assertTrue((bad / '0001_one [10].txt').exists())
            self.assertFalse(engine.history.rows(nid='2'))
