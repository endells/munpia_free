import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).parents[2]))
from munpia_free import core as c


class FilenameCleanupTests(unittest.TestCase):
    def test_rename_updates_both_histories_and_preserves_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            own = c.History(root / 'munpia_free_history.db')
            peer = c.History(root / 'munpia_gold_history.db')
            old = root / '0003_003화 - IA [7355995].txt'
            old.write_bytes(b'original content')
            digest = hashlib.sha256(old.read_bytes()).hexdigest()
            for history in (own, peer):
                history.record('1', {'id': 7355995, 'num': 3}, 'book',
                               'completed', old, digest)
            warnings = []
            self.assertEqual(own.clean_filenames('1', root, lambda: None, warnings.append), 1)
            new = root / '0003_003화 - IA.txt'
            self.assertEqual(new.read_bytes(), b'original content')
            self.assertFalse(old.exists())
            for history in (own, peer):
                self.assertEqual(history.rows(nid='1')[0]['path'], str(new))
                self.assertTrue(history.complete('1', '7355995', root))
            self.assertEqual(own.clean_filenames('1', root, lambda: None, warnings.append), 0)
            self.assertEqual(warnings, [])

    def test_collision_and_invalid_files_are_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            history = c.History(root / 'test.db')
            for eid, name in [(1, '001_title [1].txt'), (2, '002_title [2].txt'),
                              (3, '003_title [999].txt')]:
                path = root / name
                path.write_bytes(b'original')
                history.record('1', {'id': eid, 'num': eid}, 'book', 'completed', path,
                               hashlib.sha256(b'original').hexdigest())
            (root / '001_title.txt').write_bytes(b'different')
            (root / '002_title [2].txt').write_bytes(b'damaged')
            warnings = []
            self.assertEqual(history.clean_filenames('1', root, lambda: None, warnings.append), 0)
            self.assertEqual((root / '001_title.txt').read_bytes(), b'different')
            self.assertTrue((root / '001_title [1].txt').exists())
            self.assertTrue((root / '002_title [2].txt').exists())
            self.assertTrue((root / '003_title [999].txt').exists())
            self.assertEqual(len(warnings), 1)

    def test_database_failure_keeps_original_and_removes_new_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            history = c.History(root / 'test.db')
            old = root / '001_title [1].txt'
            old.write_bytes(b'original')
            history.record('1', {'id': 1, 'num': 1}, 'book', 'completed', old,
                           hashlib.sha256(b'original').hexdigest())
            rows = history.rows(nid='1')
            warnings = []
            with patch.object(history, 'rows', return_value=rows), patch.object(
                    history, 'connect', side_effect=OSError('database unavailable')):
                self.assertEqual(history.clean_filenames('1', root, lambda: None, warnings.append), 0)
            self.assertEqual(old.read_bytes(), b'original')
            self.assertFalse((root / '001_title.txt').exists())
            self.assertTrue(history.complete('1', '1', root))
            self.assertEqual(len(warnings), 1)

