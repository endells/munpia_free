"""Mobile free-episode client and storage engine. Python 3.8+; media uses Pillow."""
import copy
from contextlib import contextmanager
import hashlib
import html
from html.parser import HTMLParser
import json
import math
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
from datetime import datetime, timezone

from . import publication


class MunpiaError(Exception):
    pass


class Stopped(MunpiaError):
    pass


class LoginRequired(MunpiaError):
    pass


def normalize_cookie(value):
    if not isinstance(value, str) or len(value.encode('utf-8')) > 262144:
        raise MunpiaError('쿠키 JSON은 최대 256KB까지 입력할 수 있습니다.')
    value = value.lstrip('\ufeff').strip()
    if value.startswith(('[', '{')):
        try:
            data = json.loads(value)
        except (ValueError, RecursionError):
            raise MunpiaError('쿠키 JSON을 읽을 수 없습니다. JSON 내보내기 결과 전체를 붙여넣으세요.') from None
        if isinstance(data, dict):
            data = data.get('cookies')
        if not isinstance(data, list) or not data or len(data) > 1000:
            raise MunpiaError('쿠키 JSON은 쿠키 배열 또는 cookies 배열을 가진 객체여야 합니다. 최대 1000개입니다.')
        accepted = {}
        for item in data:
            if not isinstance(item, dict) or not isinstance(item.get('domain'), str):
                raise MunpiaError('JSON 쿠키에 domain, name, value 항목이 필요합니다.')
            domain = item['domain'].lower().lstrip('.')
            host_only = item.get('hostOnly', not item['domain'].startswith('.'))
            if type(host_only) is not bool:
                raise MunpiaError('JSON 쿠키의 hostOnly 형식이 잘못되었습니다.')
            # Match the mobile HTTPS host as a browser would; never promote www/nssl cookies.
            if domain not in ('munpia.com', 'm.munpia.com') or (host_only and domain != 'm.munpia.com'):
                continue
            if item.get('partitionKey') is not None:
                continue
            name, val, path = item.get('name'), item.get('value'), item.get('path', '/')
            if not isinstance(name, str) or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name):
                raise MunpiaError('JSON 쿠키의 이름 형식이 잘못되었습니다.')
            if not isinstance(val, str) or re.search(r'[^\x20-\x7e]|;', val):
                raise MunpiaError('JSON 쿠키 값에 사용할 수 없는 문자가 있습니다.')
            if not isinstance(path, str) or not path.startswith('/') or re.search(r'[\x00-\x20\x7f]', path):
                raise MunpiaError('JSON 쿠키의 path 형식이 잘못되었습니다.')
            expiry = item.get('expirationDate', item.get('expires'))
            if item.get('session') is True or expiry is None or expiry == -1:
                expiry = None
            elif type(expiry) not in (int, float) or not math.isfinite(expiry):
                raise MunpiaError('JSON 쿠키 만료일은 숫자 형식이어야 합니다.')
            elif expiry <= time.time():
                continue
            cookie = {'name': name, 'value': val, 'domain': domain,
                      'hostOnly': host_only, 'path': path, 'expirationDate': expiry}
            accepted[(name, domain, path)] = cookie
        if not accepted:
            raise MunpiaError('모바일 문피아에 사용할 쿠키가 없습니다. m.munpia.com에 로그인한 뒤 다시 내보내세요. 다른 도메인·만료 쿠키는 제외됩니다.')
        result = json.dumps(list(accepted.values()), ensure_ascii=True, separators=(',', ':'))
        if len(result) > 262144:
            raise MunpiaError('변환된 쿠키 JSON이 너무 큽니다. 문피아 사이트의 쿠키만 내보내세요.')
        return result
    return normalize_cookie_header(value)


def normalize_cookie_header(value):
    if len(value) > 16384 or re.search(r'[^\x20-\x7e]', value):
        raise MunpiaError('일반 Cookie 헤더는 줄바꿈 없이 최대 16KB입니다. 여러 줄은 JSON 형식으로 입력하세요.')
    value = value.strip()
    if value.lower().startswith('cookie:'):
        value = value[7:].strip()
    if value:
        for part in value.split(';'):
            if not part.strip():
                continue
            name, sep, _ = part.strip().partition('=')
            if not sep or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name):
                raise MunpiaError('쿠키 형식은 이름=값; 이름=값 입니다. Cookie 요청 헤더 값을 복사하세요.')
        if not any(part.strip() for part in value.split(';')):
            raise MunpiaError('쿠키 이름과 값이 없습니다.')
    return value


def cookie_header(normalized, request_path):
    if not normalized.startswith('['):
        return normalized
    # Keep domain/path/expiry metadata on disk instead of flattening an export forever.
    matches = []
    for item in json.loads(normalized):
        path, expiry = item['path'], item['expirationDate']
        if expiry is not None and expiry <= time.time():
            continue
        if request_path == path or (request_path.startswith(path) and (path.endswith('/') or request_path[len(path):].startswith('/'))):
            matches.append(item)
    matches.sort(key=lambda item: -len(item['path']))
    return normalize_cookie_header('; '.join(item['name'] + '=' + item['value'] for item in matches))


def response_error(payload, http_status=None):
    code = payload.get('code') if isinstance(payload, dict) else None
    code = code if isinstance(code, str) and re.fullmatch(r'[A-Z][0-9]{3}_[0-9]{5}', code) else '?'
    if code in ('A001_11004', 'A002_21006'):
        return LoginRequired('문피아 로그인이 필요하거나 쿠키가 만료되었습니다. 설정에서 쿠키를 저장하고 로그인 확인 후 다시 실행하세요. (' + code + ')')
    suffix = ' HTTP %s' % http_status if http_status is not None else ''
    return MunpiaError('문피아 접근 제한/응답 오류:%s 코드 %s' % (suffix, code))


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

    def __init__(self, stop=None, delay=1.5, transport=None, cookie=''):
        self.stop = stop or threading.Event()
        self.delay = max(1.0, float(delay))
        self.transport = transport
        self.last_request = 0
        self.cookie = normalize_cookie(cookie)
        # Cookies only go to the fixed mobile HTTPS origin. Never follow redirects.
        self.opener = urllib.request.build_opener(NoRedirect())

    def check(self):
        if self.stop.is_set():
            raise Stopped('중지했습니다.')

    def _request(self, path, params=None):
        self.check()
        if not re.fullmatch(r'/api/(?:member/my-info-simple|v1/mobile/novel-detail/[1-9][0-9]*(?:/chapters|/entries/[1-9][0-9]*)?)', path):
            raise MunpiaError('허용되지 않은 문피아 요청 경로입니다.')
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
                headers = {
                    'User-Agent': 'Mozilla/5.0 (compatible; MunpiaFreeFF/0.2)',
                    'Accept': 'application/json', 'Referer': self.BASE + '/',
                }
                header = cookie_header(self.cookie, path)
                if header:
                    headers['Cookie'] = header
                req = urllib.request.Request(url, headers=headers)
                try:
                    with self.opener.open(req, timeout=20) as response:
                        raw = response.read(8 * 1024 * 1024 + 1)
                    if len(raw) > 8 * 1024 * 1024:
                        raise MunpiaError('응답이 너무 큽니다.')
                    payload = json.loads(raw.decode('utf-8'))
                    break
                except urllib.error.HTTPError as exc:
                    try:
                        error_payload = json.loads(exc.read(8192).decode('utf-8'))
                    except (ValueError, OSError):
                        error_payload = {}
                    finally:
                        exc.close()
                    error = response_error(error_payload, exc.code)
                    if isinstance(error, LoginRequired):
                        raise error from None
                    if exc.code in (429, 500, 502, 503, 504) and attempt < 2:
                        try:
                            retry = float(exc.headers.get('Retry-After', '5'))
                        except ValueError:
                            retry = 5
                        if self.stop.wait(min(60, max(5, retry)) * (attempt + 1)):
                            raise Stopped('중지했습니다.')
                        continue
                    raise error from None
                except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
                    if attempt < 2:
                        if self.stop.wait(3 * (attempt + 1)):
                            raise Stopped('중지했습니다.')
                        continue
                    raise MunpiaError('문피아 연결 또는 응답 오류: %s' % type(exc).__name__)
        self.check()
        return payload

    def login_status(self):
        payload = self._request('/api/member/my-info-simple')
        if not isinstance(payload, dict) or type(payload.get('login')) is not bool:
            raise response_error(payload)
        return payload['login']

    def get(self, path, params=None):
        payload = self._request(path, params)
        if not isinstance(payload, dict) or payload.get('code') != 'M000_00000':
            raise response_error(payload)
        if not isinstance(payload.get('result'), dict):
            raise MunpiaError('문피아 응답에 result가 없습니다.')
        return payload['result']

    def detail(self, nid):
        d = self.get('/api/v1/mobile/novel-detail/' + parse_id(nid))
        n = d.get('novelInfo') or {}
        if str(n.get('id')) != str(nid) or not n.get('title'):
            raise MunpiaError('작품 정보가 일치하지 않습니다.')
        intro = d.get('introductionInfo') or {}
        n['introduction'] = plain_text(intro.get('introduction') or '')
        n['tags'] = [x.get('title', '') for x in intro.get('tags', []) if isinstance(x, dict)]
        return n

    def image(self, url):
        """Download site-supplied image URLs without sharing account cookies with a CDN."""
        url = urllib.parse.urljoin(self.BASE + '/', str(url))
        u = urllib.parse.urlsplit(url)
        if (u.scheme != 'https' or not u.hostname or
                not (u.hostname == 'munpia.com' or u.hostname.endswith('.munpia.com')) or
                u.username or u.password or u.port not in (None, 443)):
            raise ValueError('문피아 HTTPS 이미지 주소만 지원합니다.')
        self.check()
        if self.stop.wait(max(0, self.last_request + self.delay - time.monotonic())):
            raise Stopped('중지했습니다.')
        self.last_request = time.monotonic()
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0',
                                     'Accept': 'image/jpeg,image/png,image/gif,image/webp', 'Referer': self.BASE + '/'})
        try:
            with self.opener.open(req, timeout=20) as response:
                data = response.read(20 * 1024 * 1024 + 1)
        except (urllib.error.URLError, OSError) as exc:
            if isinstance(exc, urllib.error.HTTPError):
                exc.close()
            raise ValueError('문피아 이미지 요청에 실패했습니다. 기존 파일 보완으로 재시도할 수 있습니다.') from None
        self.check()
        if len(data) > 20 * 1024 * 1024:
            raise ValueError('이미지 크기가 20MB를 초과했습니다.')
        return data

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
        if (not text or len(text.replace('[삽화]', '').strip()) == 0) and not any(b['type'] == 'image' for b in publication.body_blocks(e)):
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

    def update_digest(self, row, new_digest):
        with self.connect() as db:
            db.execute('UPDATE entries SET sha256=?, updated=? WHERE novel_id=? AND entry_id=? AND sha256=?',
                       (new_digest, now(), row['novel_id'], row['entry_id'], row['sha256']))
        row['sha256'] = new_digest


def valid_record(row, root):
    try:
        p = Path(row['path']).resolve()
        p.relative_to(Path(root).resolve())
        return p.is_file() and p.stat().st_size > 0 and hashlib.sha256(p.read_bytes()).hexdigest() == row['sha256']
    except (OSError, ValueError):
        return False


def build_epub(folder, novel, records, stop=None, warn=None, line_height=1.8, paragraph_gap=0.55):
    def check():
        if stop and stop.is_set():
            raise Stopped('EPUB 생성을 중지했습니다.')
    output_name = '%s [%s].epub' % (safe_name(novel['title']), novel['id'])
    return publication.build_epub(folder, novel, records, output_name, check, warn or (lambda message: None),
                                  line_height, paragraph_gap)


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

    def warn(self, message):
        with self.guard:
            self.state['warnings'] = self.state.get('warnings', 0) + 1
            self.state['last_warning'] = message

    def prepare_book(self, folder, novel, client):
        publication.write_info(folder, novel, atomic_write)
        atomic_write(publication.safe_path(folder, 'metadata.json'), json.dumps(novel, ensure_ascii=False, indent=2).encode('utf-8'))
        cover = publication.safe_path(folder, 'cover.jpg')
        if cover.is_file() and cover.stat().st_size:
            return
        url = novel.get('coverUrl') or novel.get('originCoverUrl')
        if url:
            try:
                data, _ = publication.decode_image(client.image(url), cover=True)
                atomic_write(cover, data)
            except (ValueError, OSError) as exc:
                self.warn('표지 저장 실패: ' + str(exc))

    def upgrade_headings(self, records, client):
        for row in records:
            client.check()
            path = Path(row['path'])
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != row['sha256']:
                raise MunpiaError('기존 TXT가 변경되었습니다. 다시 실행하세요.')
            title = publication.heading(row['seq'], row['episode_title'])
            text = raw.decode('utf-8')
            if text.startswith(title + '\n'):
                continue
            cache = publication.load_cache(path.parent, row)
            data = (title + '\n\n' + text).encode('utf-8')
            atomic_write(path, data)
            self.history.update_digest(row, hashlib.sha256(data).hexdigest())
            if cache:
                cache.update(txt_sha256=row['sha256'], title=title)
                atomic_write(publication.cache_path(path.parent, row['entry_id']), json.dumps(cache, ensure_ascii=False).encode('utf-8'))

    def start(self, kind, ids, config, selected=None):
        if kind not in ('analyze', 'download', 'refresh'):
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
                          'failed': 0, 'skipped': 0, 'warnings': 0, 'done': 0, 'total': 0, 'started': now(), 'cancel_requested': False}
        self.thread = threading.Thread(target=self._run, args=(kind, ids, dict(config), selected, handle), daemon=True)
        try:
            self.thread.start()
        except Exception:
            handle.close()
            self.update(status='failed', message='작업 스레드를 시작하지 못했습니다.')
            raise

    def _run(self, kind, ids, config, selected, handle):
        completed = failed = skipped = 0
        login_required = False
        try:
            root = Path(config['download_path']).expanduser()
            if not root.is_absolute():
                raise MunpiaError('다운로드 경로는 컨테이너 안의 절대 경로여야 합니다.')
            root = root.resolve()
            client = self.client_factory(stop=self.stop, delay=config['request_delay'], cookie=config.get('cookie', ''))
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
                records = [r for r in self.history.rows(nid=nid) if valid_record(r, root)]
                self.upgrade_headings(records, client)
                completed_rows = {r['entry_id']: r for r in records}
                candidates = []
                for item in chapters:
                    if wanted is not None and str(item['id']) not in wanted:
                        continue
                    if item.get('free') is not True:
                        skipped += 1
                        continue
                    existing = completed_rows.get(str(item['id']))
                    if kind == 'refresh' and not existing:
                        continue
                    if existing and (kind != 'refresh' or publication.media_complete(Path(existing['path']).parent, existing)):
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
                self.prepare_book(folder, novel, client)
                for index, item in enumerate(candidates):
                    client.check()
                    self.update(message='받는 중: ' + item.get('title', ''), current_episode=item.get('title', ''))
                    existing = completed_rows.get(str(item['id']))
                    try:
                        target_folder = Path(existing['path']).parent if existing else folder
                        previous = publication.load_cache(target_folder, existing) if existing else None
                        if previous and previous.get('source'):
                            entry = previous['source']
                        else:
                            entry, _ = client.entry(nid, item['id'])
                        data = publication.save_episode(target_folder, item, entry, config['include_author_comment'],
                                                        client.image, atomic_write, client.check, self.warn, previous)
                        filename = '%05d_%s [%s].txt' % (int(item.get('num') or 0), safe_name(item.get('title', '')), item['id'])
                        path = Path(existing['path']) if existing else folder / filename
                        path.resolve().relative_to(root)
                        client.check()
                        atomic_write(path, data)
                        self.history.record(nid, item, novel['title'], 'completed', path, hashlib.sha256(data).hexdigest())
                        completed += 1
                    except Stopped:
                        raise
                    except LoginRequired as exc:
                        failed += 1
                        login_required = True
                        if not existing:
                            self.history.record(nid, item, novel['title'], 'failed', error=str(exc))
                        self.update(last_error=str(exc))
                    except Exception as exc:
                        failed += 1
                        if not existing:
                            self.history.record(nid, item, novel['title'], 'failed', error=str(exc))
                        self.update(last_error=str(exc))
                    self.update(done=index + 1, completed=completed, failed=failed, skipped=skipped)
                    if login_required:
                        break
                if config['make_epub']:
                    client.check()
                    self.update(message='EPUB 합본을 만듭니다.')
                    rows = [r for r in self.history.rows(nid=nid) if valid_record(r, root)]
                    build_epub(folder, novel, rows, self.stop, self.warn,
                               config.get('epub_line_height', 1.8), config.get('epub_paragraph_gap', 0.55))
                if login_required:
                    break
            if login_required:
                self.update(status='auth_required', message='로그인이 필요해 중단했습니다. 쿠키 저장·로그인 확인 후 다시 실행하세요. 완료된 회차는 유지됩니다.', finished=now())
            else:
                self.update(status='completed', message='완료 (실패/이미지 경고를 확인하세요)' if failed or self.snapshot().get('warnings') else '완료', finished=now())
        except Stopped:
            self.update(status='canceled', message='중지했습니다. 완료된 파일은 보존됩니다.', finished=now())
        except Exception as exc:
            self.update(status='failed', message=str(exc), finished=now())
        finally:
            handle.close()
