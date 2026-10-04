import importlib.util
import io
import json
from pathlib import Path
from unittest.mock import patch
import tempfile
import threading
import unittest
import urllib.error
import zipfile
import xml.etree.ElementTree as ET

import sys
sys.path.insert(0, str(Path(__file__).parents[2]))
from munpia_free import core as c


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
            self.assertEqual(labels, ['1. 1화', '10. 10화'])
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

    def test_cookie_transport_login_and_error_redaction(self):
        secret = 'session=synthetic-secret; another=value'
        self.assertEqual(c.normalize_cookie('Cookie: ' + secret), secret)
        for bad in ['session=secret\r\nX-Test: injected', 'Set-Cookie: session=secret', '한글=값', '; ;']:
            with self.assertRaises(c.MunpiaError) as ctx:
                c.normalize_cookie(bad)
            self.assertNotIn(bad, str(ctx.exception))
        requests = []
        class Opener:
            def open(self, req, timeout):
                requests.append(req)
                return io.BytesIO(b'{"login":true}')
        client = c.Client(cookie=secret)
        client.opener = Opener()
        self.assertTrue(client.login_status())
        self.assertEqual(requests[0].get_header('Cookie'), secret)
        self.assertEqual(requests[0].full_url, 'https://m.munpia.com/api/member/my-info-simple')
        with self.assertRaises(c.MunpiaError):
            client.get('//example.com/steal')
        self.assertEqual(len(requests), 1)
        self.assertIsNone(c.NoRedirect().redirect_request(requests[0],None,302,'',{},'https://example.com/'))
        self.assertFalse(c.Client(transport=lambda p,q: {'login':False}).login_status())
        class ErrorOpener:
            def open(self, req, timeout):
                data = json.dumps({'code':'A002_21006','message':secret}).encode()
                raise urllib.error.HTTPError(req.full_url,400,'Bad Request',{},io.BytesIO(data))
        client = c.Client(cookie=secret)
        client.opener = ErrorOpener()
        with self.assertRaises(c.LoginRequired) as ctx:
            client.entry('1','30')
        self.assertIn('A002_21006', str(ctx.exception))
        self.assertNotIn(secret, str(ctx.exception))

    def test_login_required_stops_remaining_preserves_epub_and_resumes(self):
        self.items.append({'id':40,'novelId':1,'num':11,'title':'11화','free':True})
        def limited(path,params):
            if path.endswith('/entries/30'):
                self.calls.append((path,params))
                return {'code':'A002_21006','message':'로그인 후 이용해주세요.'}
            return self.transport(path,params)
        engine = self.engine(limited)
        state = self.run_engine(engine)
        self.assertEqual((state['status'],state['completed'],state['failed']), ('auth_required',1,1))
        self.assertFalse(any(p.endswith('/40') for p,q in self.calls))
        self.assertTrue(list((self.root/'books').rglob('*.epub')))
        self.calls.clear()
        state = self.run_engine(self.engine())
        self.assertEqual(state['completed'],2)
        self.assertFalse(any(p.endswith('/10') or p.endswith('/20') for p,q in self.calls))

    def test_multiline_json_cookies_domain_path_and_expiry(self):
        base={'domain':'.munpia.com','hostOnly':False,'path':'/','name':'sid','value':'root-token','expirationDate':2000}
        items=[base, dict(base,path='/api/member',value='member-token'),
               dict(base,domain='m.munpia.com',hostOnly=True,name='mobile',value='yes',expirationDate=-1),
               dict(base,domain='www.munpia.com',name='www'),
               dict(base,domain='munpia.com',hostOnly=True,name='hostonly'),
               dict(base,domain='.munpia.com.evil.test',name='other'),
               dict(base,name='expired',expirationDate=500),
               dict(base,name='partition',partitionKey={'topLevelSite':'https://other.test'})]
        with patch.object(c.time,'time',return_value=1000):
            normalized=c.normalize_cookie(json.dumps(items,indent=2))
            self.assertEqual(c.normalize_cookie(json.dumps({'cookies':items})),normalized)
            self.assertEqual(c.normalize_cookie(normalized),normalized)
            self.assertEqual(c.cookie_header(normalized,'/api/member/my-info-simple'),'sid=member-token; sid=root-token; mobile=yes')
            self.assertEqual(c.cookie_header(normalized,'/api/membership'),'sid=root-token; mobile=yes')
            client=c.Client(cookie=json.dumps(items,indent=2))
            requests=[]
            class Opener:
                def open(self,req,timeout):
                    requests.append(req)
                    return io.BytesIO(b'{"login":true}')
            client.opener=Opener()
            self.assertTrue(client.login_status())
            self.assertEqual(requests[0].get_header('Cookie'),'sid=member-token; sid=root-token; mobile=yes')
        with patch.object(c.time,'time',return_value=3000):
            self.assertEqual(c.cookie_header(normalized,'/api/member/my-info-simple'),'mobile=yes')

    def test_json_cookie_invalid_inputs_do_not_expose_values(self):
        base={'domain':'.munpia.com','name':'sid','value':'synthetic-private-value','path':'/'}
        bad=['[{"value":"synthetic-private-value"', json.dumps({'wrong':[base]}),
             json.dumps([dict(base,value='synthetic-private-value\r\nInjected: yes')]),
             json.dumps([dict(base,value='synthetic-private-value; extra=value')]),
             json.dumps([dict(base,domain='unrelated.test')]),json.dumps([dict(base,hostOnly='false')]),
             json.dumps([dict(base,expirationDate='bad')]),json.dumps([dict(base,path='relative')]),
             json.dumps([{'name':'sid','value':'synthetic-private-value'}])]
        for value in bad:
            with self.assertRaises(c.MunpiaError) as ctx:
                c.normalize_cookie(value)
            self.assertNotIn('synthetic-private-value',str(ctx.exception))


if __name__ == '__main__':
    unittest.main()
