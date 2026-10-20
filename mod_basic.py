import json
import os
import secrets
import threading
from pathlib import Path

from flask import jsonify, render_template, session
from framework import F, Job
from plugin import PluginModuleBase

from .core import Client, Engine, MunpiaError, atomic_write, normalize_cookie, parse_id, title_ids
from .telegram_feed import TelegramFeed
from .notifications import ActivityLog, DiscordNotifier


class ModuleBasic(PluginModuleBase):
    db_default = {
        'titles': '', 'download_path': '', 'max_per_title': '10', 'episode_digits': '5',
        'request_delay': '1.5', 'make_epub': 'True',
        'include_author_comment': 'False',
        'epub_line_height': '1.8', 'epub_paragraph_gap': '0.55',
        'basic_interval': '180', 'basic_auto_start': 'False',
    }

    def __init__(self, P):
        super().__init__(P, first_menu='setting', name='basic', scheduler_desc='문피아 무료 회차 수집')
        self.engine = None
        self.settings_lock = threading.RLock()
        self.telegram = TelegramFeed(Path(F.config['path_data']) / 'db')
        self.telegram_lock = threading.Lock()
        self.activity = ActivityLog(Path(F.config['path_data']) / 'db' / 'munpia_free_activity.db')
        self.discord = DiscordNotifier(Path(F.config['path_data']) / 'db')

    def plugin_load(self):
        if not self.P.ModelSetting.get('download_path'):
            self.P.ModelSetting.set('download_path', os.path.join(F.config['path_data'], 'downloads', 'munpia_free'))
        self._engine()
        self.sync_schedule(self.config())
        self.sync_telegram_schedule()
        if hasattr(self.P.logger, 'addHandler') and self.activity not in self.P.logger.handlers:
            self.P.logger.addHandler(self.activity)
        self.activity.add('INFO', '문피아 무료편 다운 시작')

    def download_finished(self, report):
        state = report['state']
        self.activity.add('ERROR' if state.get('failed') or state['status'] in ('failed', 'auth_required') else 'INFO',
                          '%s: %s · 완료 %s · 실패 %s · 건너뜀 %s' % (report['source'], state['message'], state.get('completed', 0), state.get('failed', 0), state.get('skipped', 0)))
        for item in report['downloads']:
            self.activity.add('INFO', '%s [%s] / %s. %s 저장 완료' % (item['title'], item['novel_id'], item['no'], item['episode_title']))
        if state.get('last_error'):
            self.activity.add('ERROR', state['last_error'])
        if state.get('last_warning'):
            self.activity.add('WARNING', state['last_warning'])
        try:
            if self.discord.notify(report):
                self.activity.add('INFO', 'Discord 다운로드 결과 알림 전송 완료')
        except MunpiaError as exc:
            self.activity.add('ERROR', str(exc))
            self._engine().warn(str(exc))

    def sync_telegram_schedule(self):
        job_id = self.get_scheduler_name() + '_telegram'
        enabled = self.telegram.settings()['enabled']
        if not enabled:
            if F.scheduler.is_include(job_id):
                F.scheduler.remove_job(job_id)
        elif not F.scheduler.is_include(job_id):
            job = Job(self.P.package_name, job_id, '1', self.telegram_poll, '문피아 텔레그램 링크 수신')
            F.scheduler.add_job_instance(job, run=False)

    def telegram_append(self, ids):
        if not ids:
            return []
        with F.app.app_context(), self.settings_lock:
            current = title_ids(self.P.ModelSetting.get('titles') or '')
            added = [nid for nid in dict.fromkeys(ids) if nid not in current]
            if len(current) + len(added) > 100:
                raise MunpiaError('자동 수집 목록이 100개를 초과합니다. 목록 정리 후 다시 수신하세요.')
            if added:
                self.P.ModelSetting.set('titles', '\n'.join(current + added))
                self.activity.add('INFO', '텔레그램 자동 목록 추가: ' + ', '.join(added))
            return added

    def telegram_poll(self):
        import fcntl
        if not self.telegram_lock.acquire(blocking=False):
            return '다른 수신 작업이 진행 중입니다.'
        try:
            self.telegram.directory.mkdir(parents=True, exist_ok=True)
            with open(self.telegram.directory / 'munpia_free_telegram.lock', 'a') as handle:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return '다른 수신 작업이 진행 중입니다.'
                try:
                    return self.telegram.poll(self.telegram_append)
                except MunpiaError as exc:
                    self.activity.add('ERROR', str(exc))
                    state = self.telegram.state()
                    state['message'] = str(exc)
                    self.telegram.write_state(state)
                    return str(exc)
        finally:
            self.telegram_lock.release()

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
        conf['remove_paid_title'] = self.remove_paid_title
        conf['on_finished'] = self.download_finished
        return conf

    def remove_paid_title(self, nid):
        # Worker threads need their own Flask context for FF's settings database.
        with F.app.app_context(), self.settings_lock:
            current = title_ids(self.P.ModelSetting.get('titles') or '')
            if str(nid) in current:
                self.P.ModelSetting.set('titles', '\n'.join(x for x in current if x != str(nid)))
                self.P.logger.info('유료 전환 작품 자동 목록 제거: %s (기존 파일 보존)', nid)

    @staticmethod
    def validate(raw):
        ids = title_ids(raw.get('titles', ''))
        path = str(raw.get('download_path', '')).strip()
        if not Path(path).is_absolute():
            raise MunpiaError('다운로드 경로는 컨테이너 안의 절대 경로를 입력하세요.')
        digits = int(raw.get('episode_digits', 5))
        if not 1 <= digits <= 10:
            raise MunpiaError('파일명 회차 번호 자릿수는 1~10 범위입니다.')
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
        return {'titles': '\n'.join(ids), 'download_path': path, 'max_per_title': maximum, 'episode_digits': digits,
                'request_delay': delay, 'basic_interval': interval,
                'epub_line_height': line_height, 'epub_paragraph_gap': paragraph_gap,
                'make_epub': flag('make_epub'), 'include_author_comment': flag('include_author_comment'),
                'basic_auto_start': flag('basic_auto_start')}

    def process_menu(self, page, req):
        if page not in ('setting', 'integration', 'cookie', 'manual', 'status', 'history', 'log'):
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
                               msg='문피아 로그인 확인 완료. 새 쿠키를 입력했다면 쿠키 저장을 눌러 적용하세요.' if logged_in else '로그인되지 않았습니다. 문피아에 다시 로그인한 뒤 모바일 웹 요청의 Cookie 값을 복사하세요.')
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
            if command == 'activity':
                return jsonify(ret='success', rows=self.activity.rows())
            if command == 'discord_settings':
                return jsonify(ret='success', config=self.discord.public_settings())
            if command == 'discord_save':
                result = self.discord.save(json.loads(arg1 or '{}'))
                return jsonify(ret='success', config=result, msg='문피아 알리미 설정을 저장했습니다.')
            if command == 'discord_test':
                self.discord.test()
                self.activity.add('INFO', 'Discord 테스트 알림 전송 완료')
                return jsonify(ret='success', msg='Discord 채널에 테스트 알림을 보냈습니다.')
            if command == 'cookie_save':
                raw = json.loads(arg1 or '{}')
                cookie = normalize_cookie(raw.get('cookie', ''))
                if raw.get('clear_cookie') is True:
                    try:
                        self.cookie_path().unlink()
                    except FileNotFoundError:
                        pass
                elif cookie:
                    atomic_write(self.cookie_path(), cookie.encode('utf-8'))
                return jsonify(ret='success', cookie_saved=self.cookie_path().is_file(), msg='쿠키 설정을 저장했습니다.')
            if command == 'telegram_settings':
                return jsonify(ret='success', config=self.telegram.public_settings(), state=self.telegram.state()['message'])
            if command == 'telegram_save':
                with self.telegram_lock:
                    result = self.telegram.save(json.loads(arg1 or '{}'))
                    self.sync_telegram_schedule()
                return jsonify(ret='success', config=result, msg='텔레그램 설정 저장 완료. 수신은 매분 확인하며 다운로드는 시작하지 않습니다.')
            if command == 'telegram_check':
                channel = self.telegram.check(self.telegram.settings())
                return jsonify(ret='success', msg='채널 연결 확인: ' + channel + ' · 봇을 채널 관리자로 추가하고 새 링크를 게시하세요.')
            if command == 'telegram_poll':
                message = self.telegram_poll()
                return jsonify(ret='success', msg=message, titles=self.P.ModelSetting.get('titles') or '')
            if command == 'status':
                state = engine.snapshot()
                state['scheduler_enabled'] = F.scheduler.is_include(self.get_scheduler_name())
                return jsonify(ret='success', state=state)
            if command == 'history':
                page = max(1, min(100000, int(arg1 or 1)))
                rows = engine.history.rows(51, (page - 1) * 50)
                return jsonify(ret='success', rows=rows[:50], more=len(rows) > 50, page=page)
            if command == 'add_auto':
                nid = parse_id(arg1)
                with self.settings_lock:
                    current = self.P.ModelSetting.get('titles') or ''
                    if nid in title_ids(current):
                        return jsonify(ret='success', added=False, novel_id=nid,
                                       msg='이미 자동 수집 목록에 등록된 작품입니다. (' + nid + ')')
                    updated = current.rstrip() + '\n' + nid if current.strip() else nid
                    self.P.ModelSetting.set('titles', updated)
                # Only append the title: do not start work, enable or reset a schedule.
                return jsonify(ret='success', added=True, novel_id=nid,
                               msg='자동 수집 목록에 저장했습니다. (' + nid + ') 즉시 다운로드하지 않으며 자동 수집 사용 여부와 예약은 기존 설정을 따릅니다.')
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
                with self.settings_lock:
                    for key, value in conf.items():
                        if key == 'titles' and 'titles_base' in raw:
                            latest = title_ids(self.P.ModelSetting.get('titles') or '')
                            if latest != title_ids(raw['titles_base']):
                                raise MunpiaError('자동 수집 목록이 다른 작업에서 변경되었습니다. 새로고침 후 설정을 저장하세요.')
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
                conf['run_source'] = '기존 파일 보완'
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
            conf['run_source'] = '자동 수집'
            self._engine().start('download', title_ids(conf['titles']), conf)
        except Exception as exc:
            self.P.logger.warning('문피아 무료 수집 시작 실패: %s', exc)

    def plugin_unload(self):
        if hasattr(self.P.logger, 'removeHandler'):
            self.P.logger.removeHandler(self.activity)
        job_id = self.get_scheduler_name() + '_telegram'
        if F.scheduler.is_include(job_id):
            F.scheduler.remove_job(job_id)
        if self.engine:
            self.engine.cancel()

