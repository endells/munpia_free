"""Read-only Telegram channel feed. Never sends messages or changes webhooks."""
import json
import re
import urllib.request
from pathlib import Path

from .core import MunpiaError, NoRedirect, atomic_write, parse_id


def validate_settings(raw):
    token = str(raw.get('token', '')).strip()
    channel = str(raw.get('channel', '')).strip()
    enabled = raw.get('enabled') is True
    if token and not re.fullmatch(r'[0-9]{5,20}:[A-Za-z0-9_-]{20,100}', token):
        raise MunpiaError('텔레그램 봇 토큰 형식을 확인하세요.')
    if channel and not re.fullmatch(r'-100[0-9]+|@[A-Za-z][A-Za-z0-9_]{4,31}', channel):
        raise MunpiaError('채널 ID(-100으로 시작) 또는 @채널사용자명을 입력하세요.')
    if enabled and not (token and channel):
        raise MunpiaError('수신을 켜려면 봇 토큰과 채널을 입력하세요.')
    return {'token': token, 'channel': channel, 'enabled': enabled}


def extract_ids(message):
    texts = [message.get('text', ''), message.get('caption', '')]
    for key in ('entities', 'caption_entities'):
        texts += [e.get('url', '') for e in message.get(key, []) if isinstance(e, dict)]
    result = []
    for text in texts:
        for url in re.findall(r'https?://[^\s<>"\u200b]+', str(text)):
            try:
                nid = parse_id(url.rstrip('.,;!?)\u3002]}>'))
            except (ValueError, MunpiaError):
                continue
            if nid not in result:
                result.append(nid)
    return result


class TelegramFeed:
    def __init__(self, directory, api=None):
        self.directory = Path(directory)
        self.settings_path = self.directory / 'munpia_free_telegram.json'
        self.state_path = self.directory / 'munpia_free_telegram_state.json'
        self.api = api or self.request

    def settings(self):
        try:
            return validate_settings(json.loads(self.settings_path.read_text(encoding='utf-8')))
        except FileNotFoundError:
            return validate_settings({})

    def public_settings(self):
        conf = self.settings()
        return {'enabled': conf['enabled'], 'channel': conf['channel'], 'token_saved': bool(conf['token'])}

    def save(self, raw):
        previous = self.settings()
        raw = dict(raw)
        if not raw.get('token'):
            raw['token'] = previous['token']
        if raw.get('clear_token') is True:
            raw['token'] = ''
        conf = validate_settings(raw)
        # Reset cursor only for a different bot, not on routine saves or channel changes.
        if conf['token'] != previous['token']:
            self.write_state({'offset': 0, 'message': '새 봇 설정 저장'})
        atomic_write(self.settings_path, json.dumps(conf).encode('utf-8'))
        return self.public_settings()

    def state(self):
        try:
            return json.loads(self.state_path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            return {'offset': 0, 'message': '아직 수신하지 않았습니다.'}

    def write_state(self, state):
        atomic_write(self.state_path, json.dumps(state, ensure_ascii=False).encode('utf-8'))

    @staticmethod
    def request(token, method, params):
        if method not in ('getWebhookInfo', 'getChat', 'getUpdates'):
            raise MunpiaError('허용되지 않은 텔레그램 요청입니다.')
        req = urllib.request.Request('https://api.telegram.org/bot' + token + '/' + method,
                                     data=json.dumps(params).encode('utf-8'),
                                     headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.build_opener(NoRedirect()).open(req, timeout=15) as response:
                data = response.read(4 * 1024 * 1024 + 1)
            if len(data) > 4 * 1024 * 1024:
                raise ValueError()
            body = json.loads(data)
            if body.get('ok') is not True:
                raise ValueError()
            return body['result']
        except Exception:
            # URLs contain tokens: never propagate transport exception text.
            raise MunpiaError('텔레그램 요청 실패: 토큰·채널 권한·네트워크와 다른 수신 프로그램의 봇 중복 사용을 확인하세요.') from None

    def check(self, conf):
        if not conf['token'] or not conf['channel']:
            raise MunpiaError('텔레그램 설정을 먼저 저장하세요.')
        webhook = self.api(conf['token'], 'getWebhookInfo', {})
        if webhook.get('url'):
            raise MunpiaError('이 봇은 웹훅을 사용 중입니다. 기존 설정을 변경하지 않았습니다. 전용 봇을 사용하세요.')
        chat = self.api(conf['token'], 'getChat', {'chat_id': conf['channel']})
        if chat.get('type') != 'channel':
            raise MunpiaError('텔레그램 채널만 지원합니다.')
        return str(chat['id'])

    def poll(self, append):
        conf = self.settings()
        if not conf['enabled']:
            return '텔레그램 수신이 꺼져 있습니다.'
        channel_id = self.check(conf)
        state = self.state()
        updates = self.api(conf['token'], 'getUpdates', {
            'offset': int(state.get('offset', 0)), 'limit': 100, 'timeout': 0,
            'allowed_updates': ['channel_post', 'edited_channel_post']})
        if not isinstance(updates, list):
            raise MunpiaError('텔레그램 응답 형식이 변경되었습니다.')
        added = []
        for update in updates:
            uid = int(update['update_id'])
            if uid < int(state.get('offset', 0)):
                continue
            message = update.get('channel_post') or update.get('edited_channel_post') or {}
            if str(message.get('chat', {}).get('id')) == channel_id:
                # Save titles before acknowledging the update. Retrying is idempotent.
                added.extend(append(extract_ids(message)))
            state['offset'] = uid + 1
            state['message'] = '목록 추가: ' + ', '.join(added) if added else '수신 확인 완료 · 새 작품 없음'
            self.write_state(state)
        if not updates:
            state['message'] = '수신 확인 완료 · 새 게시물 없음'
            self.write_state(state)
        return state['message']
