import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import unittest
import zipfile
import xml.etree.ElementTree as ET

spec = importlib.util.spec_from_file_location('mf_core', Path(__file__).parents[1] / 'core.py')
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)


def payload(result):
    return {'code': 'M000_00000', 'result': result}


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.conf = {'download_path': str(self.root / 'books'), 'request_delay': 1,
                     'max_per_title': 10, 'make_epub': True, 'include_author_comment': False}
        self.calls = []
        self.items = [{'id': 10, 'novelId': 1, 'num': 1, 'title': '1화', 'free': True},
                      {'id': 20, 'novelId': 1, 'num': 2, 'title': '유료', 'free': False},
                      {'id': 30, 'novelId': 1, 'num': 10, 'title': '10화', 'free': True}]

    def tearDown(self):
        self.tmp.cleanup()

    def transport(self, path, params):
        self.calls.append((path, params.copy()))
        if path.endswith('/chapters'):
            return payload({'list': self.items, 'total': len(self.items), 'next': False})
        if '/entries/' in path:
            return payload({'entry': {'id': int(path.split('/')[-1]), 'content': '직접 작성한 시험 본문<br>둘째 줄 &amp; 기호', 'authorComment': '시험 작가의 말'}})
        return payload({'novelInfo': {'id': 1, 'title': '시험 책 <제목>', 'authorName': '시험 작가'}})

    def engine(self, transport=None):
        return c.Engine(self.root / 'history.db', client_factory=lambda **kw: c.Client(transport=transport or self.transport, **kw))

    def run_engine(self, engine, selected=None, kind='download'):
        engine.start(kind, ['1'], self.conf, selected)
        engine.thread.join(5)
        self.assertFalse(engine.thread.is_alive())
        return engine.snapshot()

    def test_urls_and_input_safety(self):
        self.assertEqual(c.parse_id('https://m.munpia.com/novel/detail/599040?x=1'), '599040')
        self.assertEqual(c.parse_id('https://www.munpia.com/novel/viewer/599040/12'), '599040')
        for value in ['https://evil.test/novel/detail/1', 'file:///etc/passwd', 'https://m.munpia.com@evil.test/novel/detail/1', '0', '1/../../a']:
            with self.assertRaises((c.MunpiaError, ValueError)):
                c.parse_id(value)

    def test_cursor_pagination_and_stall(self):
        def transport(path, params):
            if not params.get('lastNovelEntryChapterId'):
                return payload({'list': self.items[:2], 'total': 3, 'next': True})
            self.assertEqual(params['lastNovelEntryChapterId'], '20')
            return payload({'list': self.items[2:], 'total': 3, 'next': False})
        self.assertEqual(len(c.Client(transport=transport).chapters('1')), 3)
        looping = c.Client(transport=lambda p, q: payload({'list': self.items[:1], 'total': 3, 'next': True}))
        with self.assertRaisesRegex(c.MunpiaError, '반복'):
            looping.chapters('1')
        partial = c.Client(transport=lambda p, q: payload({'list': [], 'total': 3, 'next': False}))
        with self.assertRaisesRegex(c.MunpiaError, '맞지'):
            partial.chapters('1')

    def test_plain_text_and_html(self):
        self.assertEqual(c.plain_text('A\r\n\r\nB'), 'A\n\nB')
        self.assertEqual(c.plain_text('<p>A &amp; B</p><script>bad()</script><br>C'), 'A & B\n\nC')
        self.assertIn('[삽화]', c.plain_text('before{@PIC:1}after'))
        self.assertEqual(c.plain_text('<상태창>\n기술'), '<상태창>\n기술')

    def test_download_only_free_integrity_resume_and_epub(self):
        engine = self.engine()
        s = self.run_engine(engine)
        self.assertEqual(s['status'], 'completed')
        self.assertEqual(s['completed'], 2)
        self.assertFalse(any(p.endswith('/20') for p, q in self.calls))
        rows = engine.history.rows(nid='1')
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(c.valid_record(r, self.conf['download_path']) for r in rows))
        epub = next((self.root / 'books').rglob('*.epub'))
        with zipfile.ZipFile(epub) as z:
            self.assertIsNone(z.testzip())
            self.assertEqual(z.namelist()[0], 'mimetype')
            self.assertEqual(z.getinfo('mimetype').compress_type, 0)
            for name in z.namelist():
                if name.endswith(('.xml', '.opf', '.ncx', '.xhtml')):
                    ET.fromstring(z.read(name))
            toc = ET.fromstring(z.read('OEBPS/toc.ncx'))
            labels = [e.text for e in toc.findall('.//{*}navLabel/{*}text')]
            self.assertEqual(labels, ['1화', '10화'])
            self.assertIn(b'urn:munpia:novel:1', z.read('OEBPS/content.opf'))
        self.calls.clear()
        self.assertEqual(self.run_engine(engine)['completed'], 0)
        self.assertFalse(any('/entries/' in p for p, q in self.calls))
        Path(rows[0]['path']).write_text('손상', encoding='utf-8')
        self.assertEqual(self.run_engine(engine)['completed'], 1)

    def test_selected_paid_never_requested_and_missing_rejected(self):
        s = self.run_engine(self.engine(), selected=['20'])
        self.assertEqual(s['completed'], 0)
        self.assertFalse(any('/entries/' in p for p, q in self.calls))
        s = self.run_engine(self.engine(), selected=['999'])
        self.assertEqual(s['status'], 'failed')

    def test_empty_body_not_completed(self):
        def transport(path, params):
            if '/entries/' in path:
                return payload({'entry': {'id': int(path.split('/')[-1]), 'content': '   '}})
            return self.transport(path, params)
        engine = self.engine(transport)
        s = self.run_engine(engine)
        self.assertEqual(s['failed'], 2)
        self.assertEqual(engine.history.rows(nid='1'), [])
        self.assertFalse(list((self.root / 'books').rglob('*.txt')))

    def test_cross_engine_lock_and_cancellation(self):
        entered, release = threading.Event(), threading.Event()
        def transport(path, params):
            entered.set()
            release.wait(3)
            return self.transport(path, params)
        first = self.engine(transport)
        first.start('download', ['1'], self.conf)
        self.assertTrue(entered.wait(2))
        with self.assertRaises(c.MunpiaError):
            self.engine().start('download', ['1'], self.conf)
        first.cancel()
        release.set()
        first.thread.join(4)
        self.assertEqual(first.snapshot()['status'], 'canceled')
        self.assertEqual(self.run_engine(self.engine())['status'], 'completed')

    def test_symlink_escape_rejected(self):
        root = Path(self.conf['download_path'])
        root.mkdir()
        outside = self.root / 'outside'
        outside.mkdir()
        (root / (c.safe_name('시험 책 <제목>') + ' [1]')).symlink_to(outside, target_is_directory=True)
        self.assertEqual(self.run_engine(self.engine())['status'], 'failed')
        self.assertEqual(list(outside.iterdir()), [])

    def test_limit_and_analysis(self):
        self.conf['max_per_title'] = 1
        engine = self.engine()
        self.assertEqual(self.run_engine(engine)['completed'], 1)
        self.assertEqual(self.run_engine(engine)['completed'], 1)
        s = self.run_engine(engine, kind='analyze')
        self.assertEqual(len(s['analysis']['episodes']), 3)
        self.assertEqual(sum(e['have'] for e in s['analysis']['episodes']), 2)


if __name__ == '__main__':
    unittest.main()
