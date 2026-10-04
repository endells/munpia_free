"""Optional integration-contract checks; real FF installation still needs testing."""
import importlib.util
import logging
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

try:
    from flask import Flask, request
    from jinja2 import ChoiceLoader, DictLoader, FileSystemLoader
    HAS_FLASK = True
except ImportError:
    HAS_FLASK = False


@unittest.skipUnless(HAS_FLASK, 'Flask/Jinja가 있는 검증 환경에서 실행')
class AdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(__file__).parents[1]
        sys.path.insert(0, str(root.parent))
        cls.app = Flask('ff_contract')
        cls.app.secret_key = 'synthetic-test-only'
        cls.app.jinja_loader = ChoiceLoader([FileSystemLoader(str(root / 'templates')), DictLoader({'base.html': '{% block content %}{% endblock %}'})])
        cls.jobs = set()
        cls.registered_jobs = {}
        cls.registrations = []
        def add_job_instance(job, run=True):
            if job.job_id in cls.jobs:
                return
            cls.jobs.add(job.job_id)
            cls.registered_jobs[job.job_id] = job
            cls.registrations.append((job, run))
            if run:
                job.target_function()
        def remove_job(job_id):
            cls.jobs.discard(job_id)
            cls.registered_jobs.pop(job_id, None)
        class Job:
            def __init__(self, package, job_id, interval, target_function, description):
                self.job_id, self.interval, self.target_function = job_id, interval, target_function
        cls.framework = types.ModuleType('framework')
        cls.framework.F = types.SimpleNamespace(config={'path_data': cls.tmp.name}, scheduler=types.SimpleNamespace(
            is_include=lambda n: n in cls.jobs, add_job_instance=add_job_instance, remove_job=remove_job))
        cls.framework.Job = Job
        cls.old_framework = sys.modules.get('framework')
        cls.old_plugin = sys.modules.get('plugin')
        sys.modules['framework'] = cls.framework
        plugin = types.ModuleType('plugin')
        class Base:
            def __init__(self, P, first_menu=None, name=None, scheduler_desc=None):
                self.P, self.name = P, name
            def get_scheduler_name(self):
                return self.P.package_name + '_' + self.name
            def get_scheduler_desc(self):
                return '문피아 무료 회차 수집'
        plugin.PluginModuleBase = Base
        sys.modules['plugin'] = plugin
        from munpia_free.mod_basic import ModuleBasic
        cls.Module = ModuleBasic
        cls.values = dict(ModuleBasic.db_default)
        class Settings:
            @staticmethod
            def get(k): return cls.values.get(k)
            @staticmethod
            def set(k,v): cls.values[k] = v
        cls.P = types.SimpleNamespace(package_name='munpia_free', ModelSetting=Settings,
                                     logger=logging.getLogger('ff-contract'),
                                     logic=types.SimpleNamespace(scheduler_start=lambda n: cls.jobs.add('munpia_free_'+n), scheduler_stop=lambda n: cls.jobs.discard('munpia_free_'+n)))
        cls.module = ModuleBasic(cls.P)
        cls.module.plugin_load()
        @cls.app.route('/munpia_free/basic/<page>')
        def menu(page): return cls.module.process_menu(page, request)
        @cls.app.route('/munpia_free/ajax/basic/command', methods=['POST'])
        def command(): return cls.module.process_command(request.form.get('command'),request.form.get('arg1'),request.form.get('arg2'),request.form.get('arg3'),request)

    @classmethod
    def tearDownClass(cls):
        cls.module.plugin_unload()
        for name, old in [('framework', cls.old_framework), ('plugin', cls.old_plugin)]:
            if old is None: sys.modules.pop(name, None)
            else: sys.modules[name] = old
        cls.tmp.cleanup()

    def setUp(self):
        self.client = self.app.test_client()
        self.client.get('/munpia_free/basic/setting')
        with self.client.session_transaction() as s: self.token = s['munpia_free_csrf']

    def send(self, command, **kw):
        return self.client.post('/munpia_free/ajax/basic/command',data={'command':command,'csrf_token':self.token,**kw})

    def test_add_auto_preserves_settings_and_does_not_start_work(self):
        previous = dict(self.values)
        try:
            self.values['titles'] = 'https://m.munpia.com/novel/detail/599040'
            self.values['basic_auto_start'] = 'False'
            with patch.object(self.module._engine(), 'start') as start, patch.object(self.module, 'sync_schedule') as schedule:
                first = self.send('add_auto', arg1='594760').get_json()
                self.assertTrue(first['added'])
                self.assertEqual(self.values['titles'], 'https://m.munpia.com/novel/detail/599040\n594760')
                duplicate = self.send('add_auto', arg1='https://www.munpia.com/novel/detail/594760').get_json()
                self.assertFalse(duplicate['added'])
                existing = self.send('add_auto', arg1='599040').get_json()
                self.assertFalse(existing['added'])
                self.assertEqual(self.values['basic_auto_start'], 'False')
                start.assert_not_called()
                schedule.assert_not_called()
        finally:
            self.values.clear()
            self.values.update(previous)

    def test_four_templates_render(self):
        for page in ['setting','manual','status','history']:
            r = self.client.get('/munpia_free/basic/'+page)
            self.assertEqual(r.status_code, 200)
            self.assertIn('문피아 무료편 다운', r.get_data(as_text=True))

    def test_csrf_and_status_history(self):
        self.assertEqual(self.client.post('/munpia_free/ajax/basic/command',data={'command':'run'}).status_code,403)
        self.assertEqual(self.send('status').json['ret'],'success')
        self.assertEqual(self.send('history',arg1='1').json['rows'],[])

    def test_settings_validation_and_schedule(self):
        import json
        conf=self.module.config()
        conf.update(titles='https://m.munpia.com/novel/detail/599040',basic_auto_start=True)
        self.assertEqual(self.send('save',arg1=json.dumps(conf)).json['ret'],'success')
        self.assertEqual(self.values['titles'],'599040')
        self.assertIn('munpia_free_basic',self.jobs)
        conf['download_path']='relative/path'
        self.assertEqual(self.send('save',arg1=json.dumps(conf)).json['ret'],'error')
        conf=self.module.config();conf['basic_auto_start']=False
        self.send('save',arg1=json.dumps(conf))
        self.assertNotIn('munpia_free_basic',self.jobs)

    def test_cookie_storage_redaction_preservation_clear_and_verification(self):
        import json
        secret = 'session=synthetic-private-value'
        conf = self.module.config()
        conf['cookie'] = secret
        try:
            response = self.send('save',arg1=json.dumps(conf))
            self.assertTrue(response.json['cookie_saved'])
            self.assertNotIn(secret,response.get_data(as_text=True))
            self.assertEqual(self.module.read_cookie(),secret)
            self.assertEqual(self.module.cookie_path().stat().st_mode & 0o777,0o600)
            self.assertNotIn('cookie', self.values)
            self.assertNotIn('cookie',self.module.config())
            html=self.client.get('/munpia_free/basic/setting').get_data(as_text=True)
            self.assertNotIn(secret,html)
            self.assertNotIn(secret,self.send('status').get_data(as_text=True))
            conf['cookie']=''
            self.send('save',arg1=json.dumps(conf))
            self.assertEqual(self.module.read_cookie(),secret)
            with patch('munpia_free.mod_basic.Client') as client:
                client.return_value.login_status.return_value=True
                self.assertTrue(self.send('check_login').json['authenticated'])
                client.assert_called_once_with(cookie=secret)
                client.return_value.login_status.return_value=False
                self.assertFalse(self.send('check_login',arg1='session=new-candidate').json['authenticated'])
                self.assertEqual(self.module.read_cookie(),secret)
            self.assertEqual(self.client.post('/munpia_free/ajax/basic/command',data={'command':'check_login'}).status_code,403)
            conf['clear_cookie']=True
            self.assertFalse(self.send('save',arg1=json.dumps(conf)).json['cookie_saved'])
            self.assertEqual(self.module.read_cookie(),'')
        finally:
            if self.module.cookie_path().exists(): self.module.cookie_path().unlink()

    def test_json_cookie_save_and_login_check(self):
        import json
        exported=json.dumps([{'domain':'.munpia.com','name':'session','value':'synthetic-json-secret',
                              'hostOnly':False,'path':'/','session':True}],indent=2)
        conf=self.module.config();conf['cookie']=exported
        try:
            self.assertTrue(self.send('save',arg1=json.dumps(conf)).json['cookie_saved'])
            saved=self.module.read_cookie()
            self.assertEqual(json.loads(saved)[0]['value'],'synthetic-json-secret')
            self.assertNotIn('synthetic-json-secret',self.client.get('/munpia_free/basic/setting').get_data(as_text=True))
            with patch('munpia_free.mod_basic.Client') as client:
                client.return_value.login_status.return_value=True
                self.assertTrue(self.send('check_login',arg1=exported).json['authenticated'])
                client.assert_called_once_with(cookie=saved)
        finally:
            if self.module.cookie_path().exists(): self.module.cookie_path().unlink()

    def test_epub_settings_and_existing_book_refresh_command(self):
        import json
        conf=self.module.config()
        conf.update(epub_line_height=2.0,epub_paragraph_gap=0.7)
        self.assertEqual(self.send('save',arg1=json.dumps(conf)).json['ret'],'success')
        self.assertEqual(self.module.config()['epub_line_height'],2.0)
        with patch.object(self.module._engine(),'start') as start:
            self.assertEqual(self.send('refresh',arg1='https://m.munpia.com/novel/detail/599040').json['ret'],'success')
            args=start.call_args[0]
            self.assertEqual(args[:2],('refresh',['599040']))
            self.assertEqual(args[2]['epub_paragraph_gap'],0.7)
        conf['epub_line_height']=100
        self.assertEqual(self.send('save',arg1=json.dumps(conf)).json['ret'],'error')

    def test_save_does_not_run_or_reset_existing_schedule(self):
        import json
        scheduler = self.framework.F.scheduler
        scheduler.remove_job('munpia_free_basic')
        self.registrations.clear()
        conf = self.module.config()
        conf.update(titles='599040', basic_auto_start=True, basic_interval=180)
        with patch.object(self.module._engine(), 'start') as start:
            self.assertEqual(self.send('save',arg1=json.dumps(conf)).json['ret'],'success')
            start.assert_not_called()
            self.assertFalse(self.registrations[-1][1])
            first = self.registered_jobs['munpia_free_basic']
            conf['titles'] = '599040\n12345'
            self.send('save',arg1=json.dumps(conf))
            self.assertIs(self.registered_jobs['munpia_free_basic'],first)
            self.assertEqual(len(self.registrations),1)
            start.assert_not_called()
            conf['basic_interval'] = 240
            self.send('save',arg1=json.dumps(conf))
            self.assertEqual(len(self.registrations),2)
            self.assertFalse(self.registrations[-1][1])
            start.assert_not_called()
            self.registered_jobs['munpia_free_basic'].target_function()
            self.assertEqual(start.call_args[0][1],['599040','12345'])
            start.reset_mock()
            self.send('run')
            start.assert_called_once()
            start.reset_mock()
            conf['basic_auto_start'] = False
            self.send('save',arg1=json.dumps(conf))
            self.assertNotIn('munpia_free_basic',self.jobs)
            start.assert_not_called()

    def test_plugin_load_registers_without_immediate_download(self):
        scheduler = self.framework.F.scheduler
        scheduler.remove_job('munpia_free_basic')
        self.values['basic_auto_start'] = 'True'
        try:
            with patch.object(self.module._engine(), 'start') as start:
                self.module.plugin_load()
                self.assertFalse(self.registrations[-1][1])
                # FF's subsequent auto-start call sees the existing job and leaves it alone.
                job = self.registered_jobs['munpia_free_basic']
                scheduler.add_job_instance(job)
                start.assert_not_called()
        finally:
            scheduler.remove_job('munpia_free_basic')
            self.values['basic_auto_start'] = 'False'


if __name__ == '__main__': unittest.main()
