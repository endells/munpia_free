from plugin import create_plugin_instance

setting = {
    'filepath': __file__, 'use_db': True, 'use_default_setting': True,
    'home_module': 'basic', 'setting_menu': None, 'default_route': 'normal',
    'menu': {'uri': __package__, 'name': '문피아 무료편 다운', 'list': [
        {'uri': 'basic/setting', 'name': '설정'},
        {'uri': 'basic/manual', 'name': '수동 다운로드'},
        {'uri': 'basic/status', 'name': '진행 상황'},
        {'uri': 'basic/history', 'name': '다운로드 이력'},
        {'uri': 'log', 'name': '로그'},
    ]},
}
P = create_plugin_instance(setting)
from .mod_basic import ModuleBasic
P.set_module_list([ModuleBasic])
