import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).parents[2]))
from munpia_free.notifications import ActivityLog, DiscordNotifier, summary_messages, webhook_url
from munpia_free.core import MunpiaError


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sent = []
        self.notifier = DiscordNotifier(self.tmp.name, lambda url, message: self.sent.append(message))
        self.url = 'https://discord.com/api/webhooks/123456/' + 'X' * 40
        self.report = {'source': '자동 수집', 'state': {'status': 'completed', 'completed': 1, 'failed': 0, 'finished': '2026-10-20'},
                       'downloads': [{'novel_id': '123', 'title': '시험 작품', 'no': 1, 'episode_title': '시작'}]}

    def tearDown(self):
        self.tmp.cleanup()

    def test_summary_times_are_korean_without_timezone_suffix(self):
        for source in ('자동 수집', '수동 다운로드'):
            self.report['source'] = source
            for timestamp, expected in (
                ('2026-10-21T00:22:24+00:00', '2026-10-21 09:22:24'),
                ('2026-10-21T18:22:24Z', '2026-10-22 03:22:24'),
                ('2026-10-21T09:22:24+09:00', '2026-10-21 09:22:24'),
                ('2026-10-21T00:22:24', '2026-10-21 09:22:24'),
            ):
                with self.subTest(source=source, timestamp=timestamp):
                    self.report['state']['finished'] = timestamp
                    message = summary_messages(self.report)[-1]
                    self.assertEqual(message.splitlines()[-1], '종료: ' + expected)

    def test_disabled_no_send_save_redaction_and_test(self):
        self.assertFalse(self.notifier.notify(self.report))
        public = self.notifier.save({'enabled': True, 'url': self.url})
        self.assertNotIn(self.url, json.dumps(public))
        self.assertEqual(self.sent, [])
        self.assertEqual(self.notifier.path.stat().st_mode & 0o777, 0o600)
        self.notifier.save({'enabled': True, 'url': ''})
        self.assertTrue(self.notifier.notify(self.report))
        self.assertIn('1. 시작', self.sent[0])
        self.assertIn('자동 수집', self.sent[0])
        self.notifier.test()
        self.assertIn('테스트', self.sent[-1])

    def test_empty_success_silent_errors_and_cancel_reported(self):
        self.notifier.save({'enabled': True, 'url': self.url})
        self.report['state']['completed'] = 0
        self.report['downloads'] = []
        self.assertFalse(self.notifier.notify(self.report))
        self.report['state'].update(status='failed', message='시험 오류')
        self.assertTrue(self.notifier.notify(self.report))
        self.assertIn('시험 오류', self.sent[-1])
        self.report['state']['status'] = 'canceled'
        self.assertTrue(self.notifier.notify(self.report))

    def test_multiple_episodes_are_compact_ranges(self):
        self.report['downloads'] = [{'novel_id': '123', 'title': '시험', 'no': i, 'episode_title': '회차-%s' % i} for i in range(1, 1001)]
        messages = summary_messages(self.report)
        self.assertEqual(len(messages), 1)
        self.assertIn('001-1000화', messages[0])
        self.assertNotIn('회차-1', messages[0])
        self.report['downloads'] = [self.report['downloads'][i-1] for i in (7, 1, 2, 3, 5, 6, 10)]
        self.assertIn('001-003화, 005-007화, 010화', summary_messages(self.report)[0])

    def test_sparse_ranges_are_not_truncated(self):
        self.report['downloads'] = [{'novel_id': '123', 'title': '시험', 'no': i, 'episode_title': '시험'} for i in range(1, 2000, 2)]
        messages = summary_messages(self.report)
        self.assertGreater(len(messages), 1)
        self.assertTrue(all(len(s.encode('utf-16-le')) // 2 <= 2000 for s in messages))
        for i in range(1, 2000, 2):
            self.assertIn('%03d화' % i, '\n'.join(messages))

    def test_reject_non_discord_and_redact_network_error(self):
        for url in ['http://discord.com/api/webhooks/1/x', 'https://discord.com.evil.test/api/webhooks/1/x', 'https://127.0.0.1/x']:
            with self.assertRaises(MunpiaError):
                webhook_url(url)
        with patch('urllib.request.build_opener', side_effect=RuntimeError(self.url)):
            with self.assertRaises(MunpiaError) as ctx:
                DiscordNotifier.send(self.url, 'test')
        self.assertNotIn(self.url, str(ctx.exception))

    def test_sender_payload_has_no_mentions(self):
        class Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *args): pass
        with patch('urllib.request.build_opener') as opener:
            opener.return_value.open.return_value = Response()
            DiscordNotifier.send(self.url, '@everyone')
            request = opener.return_value.open.call_args.args[0]
            self.assertEqual(json.loads(request.data)['allowed_mentions'], {'parse': []})
            self.assertTrue(request.full_url.endswith('?wait=true'))

    def test_activity_is_bounded_and_redacted(self):
        log = ActivityLog(Path(self.tmp.name) / 'log.db')
        log.add('ERROR', self.url + '\nCookie: secret')
        row = log.rows()[0]
        self.assertNotIn(self.url, row['message'])
        self.assertNotIn('secret', row['message'])
        self.assertEqual(row['level'], 'ERROR')

