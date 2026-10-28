"""Discover locally stored novels and reconcile TXT files with a verified chapter list."""
import hashlib
import re
import unicodedata
from collections import defaultdict
from pathlib import Path


def normalized(value):
    return ' '.join(unicodedata.normalize('NFC', value).split())


def discover(root):
    result = defaultdict(list)
    if Path(root).is_dir():
        for folder in sorted(Path(root).iterdir()):
            match = re.fullmatch(r'(.+) \[(\d+)\]', folder.name)
            if match and folder.is_dir() and not folder.is_symlink():
                result[match[2]].append(folder)
    return dict(result)


def adopt(history, nid, novel, folder, chapters, root, check, warn):
    by_number = defaultdict(list)
    files = defaultdict(list)
    for chapter in chapters:
        by_number[int(chapter.get('num') or 0)].append(chapter)
    for path in folder.glob('*.txt'):
        match = re.match(r'^(\d+)_', path.name)
        if match and path.is_file() and not path.is_symlink():
            files[int(match[1])].append(path)
    count = 0
    for number, paths in files.items():
        check()
        if len(paths) != 1 or len(by_number[number]) != 1:
            warn('작품 %s %s화: 회차 또는 파일이 중복되거나 일치하지 않아 건너뜁니다.' % (nid, number))
            continue
        path, item = paths[0], by_number[number][0]
        if history.complete(nid, item['id'], root):
            continue
        suffix = re.search(r' \[(\d+)\]\.txt$', path.name)
        if suffix and suffix[1] != str(item['id']):
            warn('회차 ID가 다른 파일을 건너뜁니다: ' + path.name)
            continue
        try:
            path.resolve().relative_to(Path(root).resolve())
            data = path.read_bytes()
            text = data.decode('utf-8')
            if not text.strip() or '\x00' in text:
                raise ValueError('빈 파일 또는 텍스트 형식 오류')
            history.record(nid, item, novel['title'], 'completed', path, hashlib.sha256(data).hexdigest())
            count += 1
        except (OSError, ValueError) as exc:
            warn('기존 TXT 확인 실패: %s (%s)' % (path.name, exc))
    return count
