"""Public mobile API client and storage engine. Python 3.8+, standard library only."""
import copy
from contextlib import contextmanager
import hashlib
import html
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone
from xml.sax.saxutils import escape


class MunpiaError(Exception):
    pass


class Stopped(MunpiaError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def parse_id(value):
    value = str(value or '').strip()
    if re.fullmatch(r'[1-9][0-9]{0,14}', value):
        return value
    u = urllib.parse.urlsplit(value)
    if u.scheme not in ('https', 'http') or u.hostname not in ('munpia.com', 'www.munpia.com', 'm.munpia.com') or u.username or u.password or u.port:
        raise MunpiaError('문피아 작품 URL 또는 숫자 작품 번호를 입력하세요.')
    m = re.fullmatch(r'/novel/(?:detail|viewer)/([1-9][0-9]*)(?:/[1-9][0-9]*)?/?', u.path)
    if not m:
        raise MunpiaError('지원하는 주소는 /novel/detail/작품번호 형식입니다.')
    return m.group(1)


def title_ids(raw):
    result = []
    for part in re.split(r'[\n|]+', raw or ''):
        if part.strip():
            nid = parse_id(part)
            if nid not in result:
                result.append(nid)
    if len(result) > 100:
        raise MunpiaError('한 번에 최대 100개 작품을 등록할 수 있습니다.')
    return result


def safe_name(value, max_length=70):
    s = re.sub(r'[\x00-\x1f\\/*?:"<>|]', '_', str(value)).strip(' .')
    return (s[:max_length].rstrip(' .') or '제목없음')


class TextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.hidden += 1
        if self.hidden:
            return
        if tag in ('br', 'p', 'div', 'li', 'h1', 'h2', 'h3'):
            self.parts.append('\n')
        if tag == 'img':
            alt = dict(attrs).get('alt') or '삽화'
            self.parts.append('\n[' + alt + ']\n')

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.hidden = max(0, self.hidden - 1)
        elif not self.hidden and tag in ('p', 'div', 'li', 'h1', 'h2', 'h3'):
            self.parts.append('\n')

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def plain_text(content):
    if not isinstance(content, str):
        raise MunpiaError('본문 응답 형식이 변경되었습니다.')
    content = content.replace('\r\n', '\n').replace('\r', '\n')
    content = re.sub(r'\{@PIC:[^}]+\}', '\n[삽화]\n', content)
    # Preserve literal <status> etc. in plain-text novels; only parse actual HTML.
    if re.search(r'</?(?:p|div|br|span|img|script|style|b|i|strong|em|h[1-6])(?:\s|/?>)', content, re.I):
        parser = TextParser()
        parser.feed(content)
        parser.close()
        content = ''.join(parser.parts)
    else:
        content = html.unescape(content)
    content = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', content)
    return re.sub(r'\n{4,}', '\n\n\n', content).strip()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Client:
    BASE = 'https://m.munpia.com'

    def __init__(self, stop=None, delay=1.5, transport=None):
        self.stop = stop or threading.Event()
        self.delay = max(1.0, float(delay))
        self.transport = transport
        self.last_request = 0
        # No CookieJar, account cookie, login, payment, or mobile-app impersonation.
        self.opener = urllib.request.build_opener(NoRedirect())

    def check(self):
        if self.stop.is_set():
            raise Stopped('중지했습니다.')

    def get(self, path, params=None):
        self.check()
        if self.transport:
            payload = self.transport(path, params or {})
        else:
            url = self.BASE + path
            if params:
                url += '?' + urllib.parse.urlencode(params)
            for attempt in range(3):
                remaining = max(0, self.last_request + self.delay - time.monotonic())
                if self.stop.wait(remaining):
                    raise Stopped('중지했습니다.')
                self.last_request = time.monotonic()
                req = urllib.request.Request(url, headers={
                    'User-Agent': 'Mozilla/5.0 (compatible; MunpiaFreeFF/0.1)',
                    'Accept': 'application/json', 'Referer': self.BASE + '/',
                })
                try:
                    with self.opener.open(req, timeout=20) as response:
                        raw = response.read(8 * 1024 * 1024 + 1)
                    if len(raw) > 8 * 1024 * 1024:
                        raise MunpiaError('응답이 너무 큽니다.')
                    payload = json.loads(raw.decode('utf-8'))
                    break
                except urllib.error.HTTPError as exc:
                    if exc.code in (429, 500, 502, 503, 504) and attempt < 2:
                        try:
                            retry = float(exc.headers.get('Retry-After', '5'))
                        except ValueError:
                            retry = 5
                        if self.stop.wait(min(60, max(5, retry)) * (attempt + 1)):
                            raise Stopped('중지했습니다.')
                        continue
                    raise MunpiaError('문피아 HTTP 오류: %s (로그인/앱 제한은 건너뜁니다)' % exc.code)
                except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
                    if attempt < 2:
                        if self.stop.wait(3 * (attempt + 1)):
                            raise Stopped('중지했습니다.')
                        continue
                    raise MunpiaError('문피아 연결 또는 응답 오류: %s' % type(exc).__name__)
        self.check()
        if not isinstance(payload, dict) or payload.get('code') != 'M000_00000':
            code = payload.get('code', '?') if isinstance(payload, dict) else '?'
            raise MunpiaError('문피아 접근 제한/응답 오류: ' + str(code))
        if not isinstance(payload.get('result'), dict):
            raise MunpiaError('문피아 응답에 result가 없습니다.')
        return payload['result']

    def detail(self, nid):
        d = self.get('/api/v1/mobile/novel-detail/' + parse_id(nid))
        n = d.get('novelInfo') or {}
        if str(n.get('id')) != str(nid) or not n.get('title'):
            raise MunpiaError('작품 정보가 일치하지 않습니다.')
        return n

    def chapters(self, nid):
        path = '/api/v1/mobile/novel-detail/%s/chapters' % parse_id(nid)
        params = {'order': 'ENTRY_FIRST', 'bookmark': 'false'}
        found, seen, cursors = [], set(), set()
        for _ in range(1000):
            d = self.get(path, params)
            items = d.get('list')
            if not isinstance(items, list):
                raise MunpiaError('회차 목록 형식이 변경되었습니다.')
            for item in items:
                eid = str(item.get('id', ''))
                if not eid.isdigit() or str(item.get('novelId', nid)) != str(nid):
                    raise MunpiaError('회차 ID가 잘못되었습니다.')
                if eid not in seen:
                    seen.add(eid)
                    found.append(item)
            if not d.get('next'):
                if 'total' in d and len(found) != int(d['total']):
                    raise MunpiaError('전체 회차 수가 맞지 않습니다. 목록을 다시 분석하세요.')
                return found
            if not items:
                raise MunpiaError('다음 회차 목록이 비어 있습니다.')
            cursor = str(items[-1]['id'])
            if cursor in cursors:
                raise MunpiaError('목록 페이지가 반복되어 중단했습니다.')
            cursors.add(cursor)
            params['lastNovelEntryChapterId'] = cursor
        raise MunpiaError('회차 목록이 너무 많습니다.')

    def entry(self, nid, eid):
        if not re.fullmatch(r'[1-9][0-9]*', str(eid)):
            raise MunpiaError('회차 번호가 잘못되었습니다.')
        d = self.get('/api/v1/mobile/novel-detail/%s/entries/%s' % (parse_id(nid), eid))
        e = d.get('entry') or {}
        if str(e.get('id')) != str(eid):
            raise MunpiaError('회차 응답이 일치하지 않습니다.')
        text = plain_text(e.get('content'))
        if not text or len(text.replace('[삽화]', '').strip()) == 0:
            raise MunpiaError('본문이 비어 있어 완료로 기록하지 않았습니다.')
        return e, text


def atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.munpia-', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, str(path))
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class History:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS entries (
                novel_id TEXT, entry_id TEXT, title TEXT, episode_title TEXT,
                seq INTEGER, status TEXT, path TEXT, sha256 TEXT, error TEXT,
                updated TEXT, PRIMARY KEY(novel_id,entry_id))''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def record(self, nid, item, title, status, path='', digest='', error=''):
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO entries VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (str(nid), str(item['id']), title, item.get('title', ''),
                        int(item.get('num') or 0), status, str(path), digest, error, now()))

    def rows(self, limit=100, offset=0, nid=None):
        with self.connect() as db:
            if nid is not None:
                return [dict(x) for x in db.execute('SELECT * FROM entries WHERE novel_id=? AND status=? ORDER BY seq,entry_id', (str(nid), 'completed'))]
            return [dict(x) for x in db.execute('SELECT * FROM entries ORDER BY updated DESC LIMIT ? OFFSET ?', (limit, offset))]

    def complete(self, nid, eid, root):
        with self.connect() as db:
            row = db.execute('SELECT * FROM entries WHERE novel_id=? AND entry_id=?', (str(nid), str(eid))).fetchone()
        if not row or row['status'] != 'completed':
            return False
        return valid_record(dict(row), root)


def valid_record(row, root):
    try:
        p = Path(row['path']).resolve()
        p.relative_to(Path(root).resolve())
        return p.is_file() and p.stat().st_size > 0 and hashlib.sha256(p.read_bytes()).hexdigest() == row['sha256']
    except (OSError, ValueError):
        return False


def build_epub(folder, novel, records, stop=None):
    """EPUB 2, stable book UID, explicit episode order, escaped XML, atomic replace."""
    if not records:
        return None
    folder = Path(folder)
    title, author = str(novel['title']), str(novel.get('authorName') or '')
    uid = 'urn:munpia:novel:' + str(novel['id'])
    out = folder / ('%s [%s].epub' % (safe_name(title), novel['id']))
    fd, temp = tempfile.mkstemp(prefix='.epub-', dir=str(folder))
    os.close(fd)
    try:
        with zipfile.ZipFile(temp, 'w', zipfile.ZIP_DEFLATED) as z:
            z.writestr('mimetype', 'application/epub+zip', compress_type=zipfile.ZIP_STORED)
            z.writestr('META-INF/container.xml', '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
            manifest, spine, toc = [], [], []
            for i, rec in enumerate(sorted(records, key=lambda r: (r['seq'], r['entry_id']))):
                if stop and stop.is_set():
                    raise Stopped('EPUB 생성을 중지했습니다.')
                cid = 'ch%d' % i
                label = escape(rec['episode_title'])
                body = Path(rec['path']).read_text(encoding='utf-8')
                paragraphs = ''.join('<p>%s</p>' % (escape(line) or '&#160;') for line in body.splitlines())
                z.writestr('OEBPS/' + cid + '.xhtml', '<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml"><head><title>%s</title></head><body><h2>%s</h2>%s</body></html>' % (label, label, paragraphs))
                manifest.append('<item id="%s" href="%s.xhtml" media-type="application/xhtml+xml"/>' % (cid, cid))
                spine.append('<itemref idref="%s"/>' % cid)
                toc.append('<navPoint id="%s" playOrder="%d"><navLabel><text>%s</text></navLabel><content src="%s.xhtml"/></navPoint>' % (cid, i + 1, label, cid))
            z.writestr('OEBPS/content.opf', '<?xml version="1.0" encoding="utf-8"?><package version="2.0" unique-identifier="BookId" xmlns="http://www.idpf.org/2007/opf"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>%s</dc:title><dc:creator>%s</dc:creator><dc:language>ko</dc:language><dc:identifier id="BookId">%s</dc:identifier></metadata><manifest><item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>%s</manifest><spine toc="ncx">%s</spine></package>' % (escape(title), escape(author), uid, ''.join(manifest), ''.join(spine)))
            z.writestr('OEBPS/toc.ncx', '<?xml version="1.0" encoding="utf-8"?><ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1"><head><meta name="dtb:uid" content="%s"/><meta name="dtb:depth" content="1"/><meta name="dtb:totalPageCount" content="0"/><meta name="dtb:maxPageNumber" content="0"/></head><docTitle><text>%s</text></docTitle><navMap>%s</navMap></ncx>' % (uid, escape(title), ''.join(toc)))
        os.replace(temp, str(out))
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    return str(out)


class Engine:
    def __init__(self, db_path, client_factory=Client):
        self.history = History(db_path)
        self.lock_path = str(db_path) + '.lock'
        self.client_factory = client_factory
        self.stop = threading.Event()
        self.guard = threading.Lock()
        self.state = {'status': 'idle', 'message': '대기 중', 'completed': 0, 'failed': 0, 'skipped': 0}
        self.thread = None

    def update(self, **values):
        with self.guard:
            self.state.update(values)

    def snapshot(self):
        with self.guard:
            return copy.deepcopy(self.state)

    def cancel(self):
        self.stop.set()
        self.update(cancel_requested=True)

    def start(self, kind, ids, config, selected=None):
        if kind not in ('analyze', 'download'):
            raise MunpiaError('지원하지 않는 작업입니다.')
        if not ids:
            raise MunpiaError('작품을 등록하거나 URL을 입력하세요.')
        import fcntl
        with self.guard:
            if self.state.get('status') in ('running', 'stopping'):
                raise MunpiaError('다른 작업이 진행 중입니다. 완료 후 다시 실행하세요.')
            handle = open(self.lock_path, 'a')
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                handle.close()
                raise MunpiaError('다른 FF 프로세스에서 작업 중입니다.')
            self.stop.clear()
            self.state = {'status': 'running', 'kind': kind, 'message': '시작 중', 'completed': 0,
                          'failed': 0, 'skipped': 0, 'done': 0, 'total': 0, 'started': now(), 'cancel_requested': False}
        self.thread = threading.Thread(target=self._run, args=(kind, ids, dict(config), selected, handle), daemon=True)
        try:
            self.thread.start()
        except Exception:
            handle.close()
            self.update(status='failed', message='작업 스레드를 시작하지 못했습니다.')
            raise

    def _run(self, kind, ids, config, selected, handle):
        completed = failed = skipped = 0
        try:
            root = Path(config['download_path']).expanduser()
            if not root.is_absolute():
                raise MunpiaError('다운로드 경로는 컨테이너 안의 절대 경로여야 합니다.')
            root = root.resolve()
            client = self.client_factory(stop=self.stop, delay=config['request_delay'])
            for nid in ids:
                client.check()
                self.update(message='작품 %s 목록을 확인합니다.' % nid)
                novel = client.detail(nid)
                chapters = client.chapters(nid)
                if kind == 'analyze':
                    episodes = [{'id': str(c['id']), 'no': c.get('num'), 'title': c.get('title', ''),
                                 'free': c.get('free') is True, 'have': self.history.complete(nid, c['id'], root)} for c in chapters]
                    self.update(analysis={'novel_id': nid, 'title': novel['title'], 'episodes': episodes})
                    continue
                # Resolve selection against a freshly fetched list. Never trust browser free flags.
                wanted = set(map(str, selected)) if selected is not None else None
                if wanted is not None and not wanted.issubset({str(c['id']) for c in chapters}):
                    raise MunpiaError('선택 회차가 현재 목록에 없습니다. 다시 분석하세요.')
                candidates = []
                for item in chapters:
                    if wanted is not None and str(item['id']) not in wanted:
                        continue
                    if item.get('free') is not True:
                        skipped += 1
                        continue
                    if self.history.complete(nid, item['id'], root):
                        skipped += 1
                        continue
                    candidates.append(item)
                maximum = int(config['max_per_title'])
                candidates = candidates[:maximum]
                self.update(total=len(candidates), done=0, current_title=novel['title'], skipped=skipped)
                folder = root / ('%s [%s]' % (safe_name(novel['title']), nid))
                # Only plugin-created names are used; guard against symlink escapes too.
                folder.resolve().relative_to(root)
                folder.mkdir(parents=True, exist_ok=True)
                atomic_write(folder / 'metadata.json', json.dumps({k: novel.get(k) for k in ('id', 'title', 'authorName', 'genres', 'coverUrl')}, ensure_ascii=False, indent=2).encode('utf-8'))
                for index, item in enumerate(candidates):
                    client.check()
                    self.update(message='받는 중: ' + item.get('title', ''), current_episode=item.get('title', ''))
                    try:
                        entry, text = client.entry(nid, item['id'])
                        if config['include_author_comment'] and entry.get('authorComment'):
                            text += '\n\n[작가의 말]\n' + plain_text(entry['authorComment'])
                        data = (text + '\n').encode('utf-8')
                        filename = '%05d_%s [%s].txt' % (int(item.get('num') or 0), safe_name(item.get('title', '')), item['id'])
                        path = folder / filename
                        path.resolve().relative_to(root)
                        client.check()
                        atomic_write(path, data)
                        self.history.record(nid, item, novel['title'], 'completed', path, hashlib.sha256(data).hexdigest())
                        completed += 1
                    except Stopped:
                        raise
                    except Exception as exc:
                        failed += 1
                        self.history.record(nid, item, novel['title'], 'failed', error=str(exc))
                    self.update(done=index + 1, completed=completed, failed=failed, skipped=skipped)
                if config['make_epub']:
                    client.check()
                    self.update(message='EPUB 합본을 만듭니다.')
                    rows = [r for r in self.history.rows(nid=nid) if valid_record(r, root)]
                    build_epub(folder, novel, rows, self.stop)
            self.update(status='completed', message='완료' if not failed else '완료 (실패 회차는 이력에서 확인하세요)', finished=now())
        except Stopped:
            self.update(status='canceled', message='중지했습니다. 완료된 파일은 보존됩니다.', finished=now())
        except Exception as exc:
            self.update(status='failed', message=str(exc), finished=now())
        finally:
            handle.close()
