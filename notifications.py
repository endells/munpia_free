"""Discord download summaries and a bounded, credential-redacted activity log."""
from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .core import MunpiaError, NoRedirect, atomic_write, now


def redact(value):
    text = str(value)
    text = re.sub(r'https://(?:[a-z]+\.)?discord(?:app)?\.com/api/[^\s]+', '[Discord 웹훅 숨김]', text)
    text = re.sub(r'\b[0-9]{5,20}:[A-Za-z0-9_-]{20,}', '[봇 토큰 숨김]', text)
    text = re.sub(r'(?im)(cookie|authorization|set-cookie)\s*[:=].*', r'\1: [숨김]', text)
    return text[:6000]


class ActivityLog(logging.Handler):
    def __init__(self, path):
        super().__init__()
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS activity (id INTEGER PRIMARY KEY, time TEXT, level TEXT, message TEXT)')

    def add(self, level, message):
        with sqlite3.connect(self.path, timeout=10) as db:
            db.execute('INSERT INTO activity(time,level,message) VALUES(?,?,?)', (now(), level, redact(message)))
            db.execute('DELETE FROM activity WHERE id NOT IN (SELECT id FROM activity ORDER BY id DESC LIMIT 1000)')

    def emit(self, record):
        try:
            self.add(record.levelname, self.format(record))
        except Exception:
            pass

    def rows(self):
        with sqlite3.connect(self.path, timeout=10) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute('SELECT * FROM activity ORDER BY id DESC LIMIT 300')]


def webhook_url(value):
    value = str(value or '').strip()
    if not value:
        return ''
    if not re.fullmatch(r'https://(?:discord\.com|discordapp\.com)/api/(?:v[0-9]+/)?webhooks/[0-9]+/[A-Za-z0-9_-]+', value):
        raise MunpiaError('Discord의 일반 텍스트 채널 웹훅 URL을 입력하세요. 초대 링크나 봇 토큰은 사용할 수 없습니다.')
    return value


KOREA_TIME = timezone(timedelta(hours=9))


def notification_time(value):
    """Render engine timestamps in Korean time, independently of the host TZ."""
    try:
        stamp = datetime.fromisoformat(str(value or now()).replace('Z', '+00:00'))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return stamp.astimezone(KOREA_TIME).strftime('%Y-%m-%d %H:%M:%S')
    except (ValueError, TypeError, OverflowError):
        return '시간 확인 불가'


def summary_messages(report):
    state = report['state']
    source = report.get('source', '수동 다운로드')
    lines = ['문피아 알리미 · ' + source,
             '결과: %s | 완료 %s · 실패 %s · 건너뜀 %s' % (
                 {'completed': '완료', 'failed': '오류', 'auth_required': '로그인 필요', 'canceled': '중지'}.get(state['status'], state['status']),
                 state.get('completed', 0), state.get('failed', 0), state.get('skipped', 0))]
    grouped = {}
    for item in report.get('downloads', []):
        group = grouped.setdefault(item['novel_id'], {'title': item['title'], 'episodes': []})
        group['episodes'].append('%s. %s' % (item['no'], item['episode_title']))
    for nid, group in grouped.items():
        lines += ['', '%s — %s회 저장' % (group['title'][:180], len(group['episodes']))]
        lines += ['  ' + name[:240] for name in group['episodes']]
    if state.get('last_error') or state['status'] in ('failed', 'auth_required'):
        lines += ['', '오류: ' + redact(state.get('last_error') or state.get('message', ''))[:600]]
    if state.get('warnings'):
        lines += ['경고: ' + redact(state.get('last_warning', ''))[:500]]
    lines += ['종료: ' + notification_time(state.get('finished'))]
    messages, current = [], ''
    for line in lines:
        line = line[:900]
        if len((current + '\n' + line).encode('utf-16-le')) // 2 > 1850:
            messages.append(current)
            current = '문피아 알리미 · 계속\n'
        current += line + '\n'
    if current:
        messages.append(current)
    return messages


class DiscordNotifier:
    def __init__(self, directory, sender=None):
        self.path = Path(directory) / 'munpia_free_discord.json'
        self.sender = sender or self.send
        self.lock = threading.RLock()

    def settings(self):
        try:
            return json.loads(self.path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            return {'enabled': False, 'url': ''}

    def public_settings(self):
        conf = self.settings()
        return {'enabled': conf['enabled'], 'url_saved': bool(conf['url'])}

    def save(self, raw):
        with self.lock:
            url = webhook_url(raw.get('url') or self.settings()['url'])
            if raw.get('clear_url') is True:
                url = ''
            enabled = raw.get('enabled') is True
            if enabled and not url:
                raise MunpiaError('알림을 켜려면 Discord 웹훅 URL을 입력하세요.')
            atomic_write(self.path, json.dumps({'enabled': enabled, 'url': url}).encode('utf-8'))
            return self.public_settings()

    @staticmethod
    def send(url, content):
        url = webhook_url(url)
        payload = {'username': '문피아 알리미', 'content': content, 'allowed_mentions': {'parse': []}}
        request = urllib.request.Request(url + '?wait=true', data=json.dumps(payload).encode('utf-8'),
                                         headers={'Content-Type': 'application/json', 'User-Agent': 'MunpiaFree/0.5'})
        for attempt in range(2):
            try:
                with urllib.request.build_opener(NoRedirect()).open(request, timeout=10) as response:
                    if response.status not in (200, 204):
                        raise ValueError()
                return
            except urllib.error.HTTPError as exc:
                code = exc.code
                try:
                    retry = float(json.loads(exc.read(8192)).get('retry_after', 1)) if code == 429 else 0
                except (ValueError, TypeError):
                    retry = 1
                finally:
                    exc.close()
                if code == 429 and attempt == 0 and 0 <= retry <= 5:
                    time.sleep(max(1, retry))
                    continue
                raise MunpiaError('Discord 알림 전송 실패 (HTTP %s). 웹훅 설정을 확인하세요.' % code) from None
            except Exception:
                raise MunpiaError('Discord 알림 전송 실패. 네트워크와 웹훅 설정을 확인하세요.') from None

    def notify(self, report):
        with self.lock:
            conf = self.settings()
        if not conf['enabled']:
            return False
        state = report['state']
        if not state.get('completed') and not state.get('failed') and state['status'] == 'completed' and not state.get('warnings'):
            return False
        for message in summary_messages(report):
            self.sender(conf['url'], message)
        return True

    def test(self):
        conf = self.settings()
        if not conf['url']:
            raise MunpiaError('웹훅 URL을 먼저 저장하세요.')
        self.sender(conf['url'], '문피아 알리미 · 테스트 알림\n다운로드 결과가 이 채널로 전송됩니다.')

