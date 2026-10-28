"""Synthetic book fixtures only; no Munpia novel text or account cookies."""
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
import xml.etree.ElementTree as ET
import zipfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[2]))
from munpia_free import core as c
from munpia_free import publication as p

try:
    from PIL import Image
except ImportError:
    Image = None


@unittest.skipIf(Image is None, 'FF의 Pillow 설치 환경에서 실행')
class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        buf = io.BytesIO()
        Image.new('RGBA', (12, 20), (90, 140, 180, 120)).save(buf, format='PNG')
        self.png = buf.getvalue()
        self.config = {'download_path': str(self.root / 'books'), 'request_delay': 1,
                       'max_per_title': 50, 'make_epub': True, 'include_author_comment': False,
                       'epub_line_height': 1.8, 'epub_paragraph_gap': 0.55}
        self.image_calls, self.entry_calls = [], []
        self.fail_images = False
        test = self
        class Client(c.Client):
            def detail(self, nid):
                return {'id':1,'title':'합성 시험 작품','authorName':'시험 작가','genres':['판타지'],
                        'introduction':'직접 작성한 시험 소개 & 설명','chapterCount':1,
                        'createdAt':'2026-10-04T00:00:00','coverUrl':'https://cdn1.munpia.com/cover'}
            def chapters(self, nid):
                return [{'id':10,'novelId':1,'num':34,'title':'유령선','free':True}]
            def entry(self, nid, eid):
                test.entry_calls.append(eid)
                return {'id':10,'content':'첫 번째 합성 문단\n\n{@PIC:abc}\n\n두 번째 합성 문단 <상태창>',
                        'attachments':[{'id':'picture-abc-original','imageUrl':'https://cdn1.munpia.com/inside','alt':'시험 삽화'}]}, 'unused'
            def image(self, url):
                test.image_calls.append(url)
                if test.fail_images and url.endswith('/inside'):
                    raise ValueError('합성 이미지 오류')
                return test.png
        self.engine = c.Engine(self.root/'history.db',client_factory=Client)

    def tearDown(self):
        self.tmp.cleanup()

    def run_book(self, kind='download'):
        self.engine.start(kind,['1'],self.config)
        self.engine.thread.join(5)
        self.assertFalse(self.engine.thread.is_alive())
        return self.engine.snapshot()

    def test_named_images_and_pc_text_survive_media_failure(self):
        item = {'id': 10, 'num': 3, 'title': '시험 제목'}
        entry = {'content': '합성 본문', '_source': 'pc', 'attachments': [
            {'imageUrl': 'https://cdn1.munpia.com/a'}, {'imageUrl': 'https://cdn1.munpia.com/b'}]}
        warnings = []
        data = p.save_episode(self.root, item, entry, False, lambda url: self.png,
                              c.atomic_write, lambda: None, warnings.append, episode_digits=4)
        self.assertIn('합성 본문', data.decode())
        for number in (1, 2):
            name = 'images/0003_시험 제목_이미지_%03d.png' % number
            self.assertTrue((self.root / name).is_file())
            self.assertIn(name, data.decode())
        self.assertTrue(warnings)  # PC image positions are explicitly reported.
        def failed_fetch(url):
            raise ValueError('image unavailable')
        def failed_cache(path, data):
            if str(path).endswith('.json'):
                raise OSError('cache unavailable')
            c.atomic_write(path, data)
        data = p.save_episode(self.root, item, entry, False, failed_fetch,
                              failed_cache, lambda: None, warnings.append, episode_digits=4)
        self.assertIn('합성 본문', data.decode())
        self.assertIn('[삽화: 받지 못함]', data.decode())

    def test_txt_references_restore_epub_without_cache_or_network(self):
        self.run_book()
        row = self.engine.history.rows(nid='1')[0]
        folder = Path(row['path']).parent
        p.cache_path(folder, row['entry_id']).unlink()
        self.assertTrue(p.media_complete(folder, row))
        self.entry_calls.clear(); self.image_calls.clear()
        self.run_book('refresh')
        self.assertEqual(self.entry_calls, [])
        self.assertEqual(self.image_calls, [])
        with zipfile.ZipFile(next(folder.glob('*.epub'))) as z:
            chapter = ET.fromstring(z.read('OEBPS/ch0.xhtml'))
            body = list(chapter.find('{*}body'))
            self.assertEqual([n.tag.split('}')[-1] for n in body], ['h2', 'p', 'div', 'p'])
            src = chapter.find('.//{*}img').get('src')
            self.assertEqual(z.read('OEBPS/' + src), self.png)

    def test_txt_references_reject_escapes_and_report_missing_files(self):
        outside = self.root / 'outside.png'; outside.write_bytes(self.png)
        folder = self.root / 'book'; folder.mkdir()
        (folder / 'escape.png').symlink_to(outside)
        text = '앞\n[삽화: ../outside.png]\n[삽화: escape.png]\n[삽화: missing.png]\n뒤'
        blocks = p.txt_blocks(folder, text)
        self.assertEqual(sum(b['type'] == 'image' for b in blocks), 3)
        self.assertFalse(any(b.get('src') for b in blocks))
        path = folder / '001_test.txt'; path.write_text(text)
        row = {'entry_id': '10', 'seq': 1, 'episode_title': 'test',
               'path': str(path), 'sha256': p.digest(path.read_bytes())}
        warnings = []
        p.build_epub(folder, {'id': 1, 'title': 'test'}, [row], 'test.epub', lambda: None, warnings.append)
        self.assertEqual(len(warnings), 3)
        self.assertIn('missing.png', warnings[-1])
        with zipfile.ZipFile(folder / 'test.epub') as z:
            self.assertFalse(any(n.endswith('.png') for n in z.namelist()))

    def test_attachment_aliases_and_unplaced_images_are_kept(self):
        blocks = p.body_blocks({'content': '앞\n{@PIC:one}\n뒤', 'attachments': [
            {'id': 'one', 'url': 'https://cdn1.munpia.com/one'},
            {'id': 'two', 'src': 'https://cdn1.munpia.com/two'}]})
        self.assertEqual([b['type'] for b in blocks], ['text', 'image', 'text', 'image'])
        self.assertEqual(blocks[1]['url'], 'https://cdn1.munpia.com/one')
        self.assertTrue(blocks[-1]['unplaced'])
        self.assertEqual(blocks[-1]['url'], 'https://cdn1.munpia.com/two')

    def test_author_comment_does_not_duplicate_attachments(self):
        blocks = p.body_blocks({'content': '본문 {@PIC:one}', 'authorComment': '후기 {@PIC:two}',
                                'attachments': [{'id': 'one', 'url': 'https://cdn1.munpia.com/one'},
                                                {'id': 'two', 'url': 'https://cdn1.munpia.com/two'}]}, True)
        images = [b for b in blocks if b['type'] == 'image']
        self.assertEqual(len(images), 2)
        self.assertFalse(any(b.get('unplaced') for b in images))

    def test_filename_with_brackets_and_webp_embeds_as_png(self):
        buf = io.BytesIO()
        Image.new('RGB', (4, 4), 'red').save(buf, format='WEBP')
        relative = 'images/0001_제목 [특별편]_이미지_001.webp'
        path = self.root / relative; path.parent.mkdir(); path.write_bytes(buf.getvalue())
        txt = self.root / 'one.txt'
        txt.write_text('1. 시험\n\n앞\n[삽화: %s]\n뒤\n' % relative)
        row = {'entry_id': '10', 'seq': 1, 'episode_title': '시험',
               'path': str(txt), 'sha256': p.digest(txt.read_bytes())}
        warnings = []
        p.build_epub(self.root, {'id': 1, 'title': '시험'}, [row], 'test.epub', lambda: None, warnings.append)
        self.assertEqual(warnings, [])
        with zipfile.ZipFile(self.root / 'test.epub') as z:
            chapter = ET.fromstring(z.read('OEBPS/ch0.xhtml'))
            src = chapter.find('.//{*}img').get('src')
            self.assertTrue(src.endswith('.png'))
            self.assertTrue(z.read('OEBPS/' + src).startswith(b'\x89PNG'))

    def test_cover_info_txt_and_embedded_epub(self):
        state = self.run_book()
        self.assertEqual(state['completed'],1)
        row=self.engine.history.rows(nid='1')[0]
        folder=Path(row['path']).parent
        text=Path(row['path']).read_text()
        self.assertTrue(text.startswith('34. 유령선\n\n'))
        self.assertEqual(text.count('34. 유령선'),1)
        self.assertIn('[삽화: images/',text)
        self.assertIn('<상태창>',text)
        with Image.open(folder/'cover.jpg') as img:
            self.assertEqual(img.format,'JPEG')
        info=ET.parse(folder/'info.xml').getroot()
        self.assertEqual(info.tag,'ComicInfo')
        self.assertEqual(info.findtext('Writer'),'시험 작가')
        self.assertIn('&',info.findtext('Summary'))
        epub=next(folder.glob('*.epub'))
        with zipfile.ZipFile(epub) as z:
            self.assertEqual(z.namelist()[0],'mimetype')
            self.assertEqual(z.getinfo('mimetype').compress_type,zipfile.ZIP_STORED)
            for name in z.namelist():
                if name.endswith(('.xml','.opf','.ncx','.xhtml')): ET.fromstring(z.read(name))
            opf=ET.fromstring(z.read('OEBPS/content.opf'))
            self.assertEqual(opf.find('{*}spine/{*}itemref').get('idref'),'cover-page')
            for item in opf.findall('{*}manifest/{*}item'):
                self.assertIn('OEBPS/'+item.get('href'),z.namelist())
            chapter=ET.fromstring(z.read('OEBPS/ch0.xhtml'))
            content=list(chapter.find('{*}body'))
            self.assertEqual([n.tag.split('}')[-1] for n in content],['h2','p','div','p'])
            self.assertEqual(content[0].text,'34. 유령선')
            for name in ['OEBPS/cover.xhtml','OEBPS/ch0.xhtml']:
                for image in ET.fromstring(z.read(name)).findall('.//{*}img'):
                    self.assertIn('OEBPS/'+image.get('src'),z.namelist())
            self.assertIn(b'line-height: 1.80',z.read('OEBPS/style.css'))
            self.assertNotIn('34. 유령선\n', ''.join(chapter.itertext()))
        self.entry_calls.clear();self.image_calls.clear()
        self.config['epub_line_height']=2.0
        self.run_book('refresh')
        self.assertEqual(self.entry_calls,[])
        self.assertEqual(self.image_calls,[])
        with zipfile.ZipFile(epub) as z:self.assertIn(b'line-height: 2.00',z.read('OEBPS/style.css'))

    def test_old_txt_migration_and_image_refresh_are_idempotent(self):
        folder=Path(self.config['download_path'])/'합성 시험 작품 [1]';folder.mkdir(parents=True)
        path=folder/'old.txt';data='옛 합성 본문\n[삽화]\n'.encode();path.write_bytes(data)
        self.engine.history.record('1',{'id':10,'num':34,'title':'유령선'},'합성 시험 작품','completed',path,hashlib.sha256(data).hexdigest())
        self.run_book()
        self.assertEqual(self.entry_calls,[])
        self.assertTrue(path.read_text().startswith('34. 유령선\n\n옛 합성 본문'))
        self.run_book('refresh')
        self.assertEqual(self.entry_calls,[10])
        self.assertEqual(path.read_text().count('34. 유령선'),1)
        self.assertIn('images/',path.read_text())
        self.run_book('refresh')
        self.assertEqual(self.entry_calls,[10])
        self.assertTrue(self.engine.history.complete('1','10',self.config['download_path']))

    def test_missing_image_retry_keeps_text_and_reuses_cached_source(self):
        self.fail_images=True
        state=self.run_book()
        self.assertEqual((state['completed'],state['failed']),(1,0))
        self.assertGreater(state['warnings'],0)
        row=self.engine.history.rows(nid='1')[0];folder=Path(row['path']).parent
        self.assertFalse(p.media_complete(folder,row))
        self.fail_images=False;self.entry_calls.clear()
        state=self.run_book('refresh')
        self.assertEqual(state['completed'],1)
        self.assertEqual(self.entry_calls,[])
        self.assertTrue(p.media_complete(folder,self.engine.history.rows(nid='1')[0]))

    def test_html_image_order_and_safe_escaping(self):
        blocks=p.body_blocks({'content':'<p>앞 &amp; 글</p><script>나쁜 코드</script><img src="https://cdn1.munpia.com/pic" alt="그림 &amp; 설명"/><p>뒤</p>'})
        self.assertEqual([x['type'] for x in blocks],['text','image','text'])
        self.assertEqual(blocks[0]['text'],'앞 & 글')
        self.assertNotIn('나쁜 코드',str(blocks))
        self.assertEqual(p.heading(34,'34. 유령선'),'34. 유령선')
        self.assertEqual(p.heading(34,'34화 유령선'),'34. 유령선')
        self.assertEqual(p.heading(34,'유령선'),'34. 유령선')

    def test_image_http_never_sends_cookie_and_rejects_external_urls(self):
        requests=[];data=self.png
        class Opener:
            def open(self, req, timeout):
                requests.append(req)
                return io.BytesIO(data)
        client=c.Client(cookie='session=synthetic-private')
        client.opener=Opener()
        self.assertEqual(client.image('https://cdn1.munpia.com/test'),self.png)
        self.assertIsNone(requests[0].get_header('Cookie'))
        for url in ['http://cdn1.munpia.com/test','https://evil.test/pic','https://munpia.com.evil.test/pic','file:///etc/passwd','https://user@cdn1.munpia.com/pic']:
            with self.assertRaises(ValueError):client.image(url)
        self.assertEqual(len(requests),1)
        with self.assertRaises(ValueError):p.decode_image(b'<html>login page</html>')

    def test_symlink_asset_escape_and_cancel_keep_existing_epub(self):
        self.run_book()
        row=self.engine.history.rows(nid='1')[0];folder=Path(row['path']).parent
        epub=next(folder.glob('*.epub'));before=epub.read_bytes()
        event=threading.Event();event.set()
        with self.assertRaises(c.Stopped):c.build_epub(folder,{'id':1,'title':'합성 시험 작품'},[row],event)
        self.assertEqual(epub.read_bytes(),before)
        outside=self.root/'outside';outside.mkdir()
        (folder/'escape').symlink_to(outside,target_is_directory=True)
        with self.assertRaises(ValueError):p.safe_path(folder,'escape/file.jpg')


if __name__ == '__main__': unittest.main()

