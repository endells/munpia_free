import json
import os
import secrets
from pathlib import Path

from flask import jsonify, render_template, session
from framework import F, Job
from plugin import PluginModuleBase

from .core import Client, Engine, MunpiaError, atomic_write, normalize_cookie, parse_id, title_ids


class ModuleBasic(PluginModuleBase):
    db_default = {
        'titles': '', 'download_path': '', 'max_per_title': '10',
        'request_delay': '1.5', 'make_epub': 'True',
        'include_author_comment': 'False',
        'epub_line_height': '1.8', 'epub_paragraph_gap': '0.55',
        'basic_interval': '180', 'basic_auto_start': 'False',
    }

    def __init__(self, P):
        super().__init__(P, first_menu='setting', name='basic', scheduler_desc='문피아 무료 회차 수집')
        self.engine = None

    def plugin_load(self):
        if not self.P.ModelSetting.get('download_path'):
            self.P.ModelSetting.set('download_path', os.path.join(F.config['path_data'], 'downloads', 'munpia_free'))
        self._engine()
        self.sync_schedule(self.config())

    def sync_schedule(self, conf, previous_interval=None):
        job_id = self.get_scheduler_name()
        included = F.scheduler.is_include(job_id)
        if not conf['basic_auto_start']:
            if included:
                F.scheduler.remove_job(job_id)
            return
        if included and (previous_interval is None or previous_interval == conf['basic_interval']):
            # Changing titles, paths or cookies must not reset the next scheduled run.
            return
        if included:
            F.scheduler.remove_job(job_id)
        job = Job(self.P.package_name, job_id, str(conf['basic_interval']),
                  self.scheduler_function, self.get_scheduler_desc())
        # FF defaults to an immediate first run (5-20 seconds). Explicitly opt out.
        F.scheduler.add_job_instance(job, run=False)

    def _engine(self):
        if self.engine is None:
            self.engine = Engine(os.path.join(F.config['path_data'], 'db', 'munpia_free_history.db'))
        return self.engine

    def config(self):
        raw = {k: self.P.ModelSetting.get(k) or v for k, v in self.db_default.items()}
        if not raw['download_path']:
            raw['download_path'] = os.path.join(F.config['path_data'], 'downloads', 'munpia_free')
        return self.validate(raw)

    def cookie_path(self):
        return Path(F.config['path_data']) / 'db' / 'munpia_free_cookie.txt'

    def read_cookie(self):
        try:
            return normalize_cookie(self.cookie_path().read_text(encoding='utf-8'))
        except FileNotFoundError:
            return ''

    def runtime_config(self):
        conf = self.config()
        conf['cookie'] = self.read_cookie()
        return conf

    @staticmethod
    def validate(raw):
        ids = title_ids(raw.get('titles', ''))
        path = str(raw.get('download_path', '')).strip()
        if not Path(path).is_absolute():
            raise MunpiaError('다운로드 경로는 컨테이너 안의 절대 경로를 입력하세요.')
        maximum = int(raw.get('max_per_title', 10))
        delay = float(raw.get('request_delay', 1.5))
        interval = int(raw.get('basic_interval', 180))
        line_height = float(raw.get('epub_line_height', 1.8))
        paragraph_gap = float(raw.get('epub_paragraph_gap', 0.55))
        if not 1 <= maximum <= 1000:
            raise MunpiaError('작품당 회차 수는 1~1000 범위입니다.')
        if not 1 <= delay <= 60:
            raise MunpiaError('요청 간격은 1~60초 범위입니다.')
        if not 10 <= interval <= 10080:
            raise MunpiaError('자동 수집 간격은 10~10080분 범위입니다.')
        if not 1.2 <= line_height <= 2.5 or not 0 <= paragraph_gap <= 1.5:
            raise MunpiaError('EPUB 줄간격은 1.2~2.5, 문단 간격은 0~1.5 범위입니다.')
        flag = lambda k: raw.get(k) in (True, 'true', 'True', '1')
        return {'titles': '\n'.join(ids), 'download_path': path, 'max_per_title': maximum,
                'request_delay': delay, 'basic_interval': interval,
                'epub_line_height': line_height, 'epub_paragraph_gap': paragraph_gap,
                'make_epub': flag('make_epub'), 'include_author_comment': flag('include_author_comment'),
                'basic_auto_start': flag('basic_auto_start')}

    def process_menu(self, page, req):
        if page not in ('setting', 'manual', 'status', 'history'):
            page = 'setting'
        token_key = 'munpia_free_csrf'
        if token_key not in session:
            session[token_key] = secrets.token_urlsafe(32)
        return render_template('munpia_free_basic.html', page=page,
                               package_name=self.P.package_name, config=self.config(),
                               csrf_token=session[token_key], cookie_saved=self.cookie_path().is_file())

    def process_command(self, command, arg1, arg2, arg3, req):
        try:
            expected = session.get('munpia_free_csrf', '')
            given = req.form.get('csrf_token', '')
            if not expected or not secrets.compare_digest(expected, given):
                return jsonify(ret='error', msg='페이지를 새로고침한 후 다시 시도하세요.'), 403
            if command == 'check_login':
                cookie = normalize_cookie(arg1 or '') or self.read_cookie()
                if not cookie:
                    raise MunpiaError('로그인한 문피아의 쿠키를 먼저 입력하세요.')
                logged_in = Client(cookie=cookie).login_status()
                return jsonify(ret='success', authenticated=logged_in,
                               msg='문피아 로그인 확인 완료. 새 쿠키를 입력했다면 설정 저장을 눌러 적용하세요.' if logged_in else '로그인되지 않았습니다. 문피아에 다시 로그인한 뒤 모바일 웹 요청의 Cookie 값을 복사하세요.')
            if command == 'browse_path':
                path = Path(os.path.normpath(str(arg1 or '/').strip() or '/'))
                if not path.is_absolute():
                    raise MunpiaError('폴더 선택은 /로 시작하는 컨테이너 내부 경로를 입력하세요.')
                # FF's folder dialog requires an existing, readable directory.
                # New download folders may not exist until the first download.
                while not (path.is_dir() and os.access(str(path), os.R_OK | os.X_OK)):
                    if path == path.parent:
                        raise MunpiaError('열 수 있는 상위 폴더가 없습니다. FF 폴더 접근 권한을 확인하세요.')
                    path = path.parent
                return jsonify(ret='success', path=str(path))
            engine = self._engine()
            if command == 'status':
                state = engine.snapshot()
                state['scheduler_enabled'] = F.scheduler.is_include(self.get_scheduler_name())
                return jsonify(ret='success', state=state)
            if command == 'history':
                page = max(1, min(100000, int(arg1 or 1)))
                rows = engine.history.rows(51, (page - 1) * 50)
                return jsonify(ret='success', rows=rows[:50], more=len(rows) > 50, page=page)
            if command == 'save':
                raw = json.loads(arg1 or '{}')
                conf = self.validate(raw)
                previous_interval = self.config()['basic_interval']
                cookie = normalize_cookie(raw.get('cookie', ''))
                if raw.get('clear_cookie') is True:
                    try:
                        self.cookie_path().unlink()
                    except FileNotFoundError:
                        pass
                elif cookie:
                    # Separate from FF's generic settings responses; atomic_write uses mode 0600.
                    atomic_write(self.cookie_path(), cookie.encode('utf-8'))
                for key, value in conf.items():
                    self.P.ModelSetting.set(key, str(value))
                self.sync_schedule(conf, previous_interval)
                return jsonify(ret='success', msg='설정을 저장했습니다.', cookie_saved=self.cookie_path().is_file())
            if command == 'analyze':
                nid = parse_id(arg1)
                engine.start('analyze', [nid], self.runtime_config())
            elif command == 'download':
                nid = parse_id(arg1)
                selected = json.loads(arg2 or '[]')
                if not isinstance(selected, list) or not selected or len(selected) > 10000 or any(not str(x).isdigit() for x in selected):
                    raise MunpiaError('다운로드할 회차를 선택하세요.')
                engine.start('download', [nid], self.runtime_config(), selected=selected)
            elif command == 'run':
                conf = self.runtime_config()
                engine.start('download', title_ids(conf['titles']), conf)
            elif command == 'refresh':
                conf = self.runtime_config()
                ids = [parse_id(arg1)] if arg1 else title_ids(conf['titles'])
                engine.start('refresh', ids, conf)
            elif command == 'stop':
                engine.cancel()
                return jsonify(ret='success', msg='중지 요청을 보냈습니다. 진행 중 요청의 응답을 기다릴 수 있습니다.')
            else:
                raise MunpiaError('지원하지 않는 명령입니다.')
            return jsonify(ret='success', msg='작업을 시작했습니다.')
        except (ValueError, TypeError, MunpiaError) as exc:
            return jsonify(ret='error', msg=str(exc))
        except Exception:
            self.P.logger.exception('munpia_free command failed')
            return jsonify(ret='error', msg='처리에 실패했습니다. FF 로그를 확인하세요.')

    def scheduler_function(self):
        try:
            conf = self.runtime_config()
            self._engine().start('download', title_ids(conf['titles']), conf)
        except Exception as exc:
            self.P.logger.warning('문피아 무료 수집 시작 실패: %s', exc)

    def plugin_unload(self):
        if self.engine:
            self.engine.cancel()
