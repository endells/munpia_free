"""Download preferences published by the free plugin, read by the gold plugin."""
import json
from pathlib import Path

SHARED_KEYS = ('download_path', 'episode_digits', 'max_per_title', 'request_delay',
               'make_epub', 'include_author_comment', 'epub_line_height', 'epub_paragraph_gap')


def settings_path(data_root):
    return Path(data_root) / 'db' / 'munpia_shared_settings.json'


def read_settings(data_root):
    try:
        data = json.loads(settings_path(data_root).read_text(encoding='utf-8'))
    except FileNotFoundError:
        return None
    if not isinstance(data, dict) or data.get('version') != 1 or not isinstance(data.get('settings'), dict):
        raise ValueError('무료판 공유 설정을 읽을 수 없습니다. 무료판에서 설정을 다시 저장하세요.')
    settings = data['settings']
    if any(key not in settings for key in SHARED_KEYS):
        raise ValueError('무료판 공유 설정이 누락되었습니다. 무료판에서 설정을 다시 저장하세요.')
    return {key: settings[key] for key in SHARED_KEYS}


def publish_settings(data_root, conf, write):
    data = json.dumps({'version': 1, 'settings': {key: conf[key] for key in SHARED_KEYS}},
                      ensure_ascii=False, sort_keys=True).encode('utf-8')
    path = settings_path(data_root)
    try:
        if path.read_bytes() == data:
            return
    except FileNotFoundError:
        pass
    write(path, data)
