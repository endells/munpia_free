"""Optional integration-contract checks; real FF installation still needs testing."""
import importlib.util
import logging
from pathlib import Path
import sys
import tempfile
import types
import unittest

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
        cls.framework = types.ModuleType('framework')
        cls.framework.F = types.SimpleNamespace(config={'path_data': cls.tmp.name}, scheduler=types.SimpleNamespace(is_include=lambda n: n in cls.jobs))
        cls.old_framework = sys.modules.get('framework')
        cls.old_plugin = sys.modules.get('plugin')
        sys.modules['framework'] = cls.framework
        plugin = types.ModuleType('plugin')
        class Base:
            def __init__(self, P, first_menu=None, name=None, scheduler_desc=None):
                self.P, self.name = P, name
            def get_scheduler_name(self):
                return self.P.package_name + '_' + self.name
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

    def test_four_templates_render(self):
        for page in ['setting','manual','status','history']:
            r = self.client.get('/munpia_free/basic/'+page)
            self.assertEqual(r.status_code, 200)
            self.assertIn('문피아 무료회차', r.get_data(as_text=True))

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


if __name__ == '__main__': unittest.main()
