"""Merge folders only after the server has confirmed the novel ID and title."""
import shutil
import uuid
from pathlib import Path


def merge(history, nid, title, root, target, peer_name, own_name, timestamp, warn):
    sources = sorted(p for p in root.iterdir() if p.is_dir() and not p.is_symlink()
                     and p.name.endswith(' [%s]' % nid) and p != target)
    if not sources:
        return False
    if target.is_symlink() or (target.exists() and not target.is_dir()):
        warn('병합 대상이 정상 폴더가 아닙니다: ' + str(target))
        return False
    # Do not traverse links into unrelated storage.
    if any(p.is_symlink() for folder in sources + ([target] if target.exists() else [])
           for p in folder.rglob('*')):
        warn('작품 %s 폴더 내부에 심볼릭 링크가 있어 병합을 건너뜁니다.' % nid)
        return False
    created_files, created_dirs, archives = [], [], []
    mapping = {}
    def mkdir(path):
        if path.exists():
            if not path.is_dir():
                raise ValueError('파일과 폴더 이름 충돌: ' + str(path))
            return
        mkdir(path.parent)
        path.mkdir()
        created_dirs.append(path)
    try:
        mkdir(target)
        with history.connect() as db:
            schemas = ['main']
            own = Path(history.path)
            peer = own.with_name(peer_name)
            if own.name == own_name and peer.is_file():
                db.execute('ATTACH DATABASE ? AS peer', (str(peer),))
                schemas.append('peer')
            db.execute('BEGIN IMMEDIATE')
            for source in sources:
                files = sorted(p for p in source.rglob('*') if p.is_file())
                conflicts = [p for p in files if (target / p.relative_to(source)).exists()
                             and (not (target / p.relative_to(source)).is_file()
                                  or p.read_bytes() != (target / p.relative_to(source)).read_bytes())]
                backup = None
                if conflicts:
                    archive = target / '.duplicates'
                    mkdir(archive)
                    backup = archive / ('folder_merge_' + uuid.uuid4().hex)
                    archives.append(backup)
                    shutil.copytree(source, backup)
                for path in files:
                    dest = target / path.relative_to(source)
                    if path in conflicts:
                        mapping[str(path)] = str(backup / path.relative_to(source))
                    else:
                        if not dest.exists():
                            mkdir(dest.parent)
                            created_files.append(dest)
                            shutil.copy2(path, dest)
                        mapping[str(path)] = str(dest)
                for schema in schemas:
                    for row in db.execute('SELECT entry_id,path FROM ' + schema + '.entries WHERE novel_id=?', (str(nid),)).fetchall():
                        if row['path'] in mapping:
                            db.execute('UPDATE ' + schema + '.entries SET path=?,title=?,updated=? WHERE novel_id=? AND entry_id=?',
                                       (mapping[row['path']], title, timestamp(), str(nid), row['entry_id']))
        for source in sources:
            try:
                shutil.rmtree(source)
            except OSError as exc:
                warn('병합 완료 후 이전 폴더 삭제 실패: %s (%s)' % (source.name, exc))
        return True
    except Exception as exc:
        for path in reversed(created_files):
            if path.exists():
                path.unlink()
        for path in archives:
            if path.exists():
                shutil.rmtree(path)
        for path in reversed(created_dirs):
            if path.exists():
                path.rmdir()
        warn('작품 %s 폴더 병합 실패, 원본 유지: %s' % (nid, exc))
        return False
