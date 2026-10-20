import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import sys
sys.path.insert(0, str(Path(__file__).parents[2]))
from munpia_free.telegram_feed import TelegramFeed, extract_ids, validate_settings
from munpia_free.core import MunpiaError, parse_id


class TelegramTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.calls = []
        self.updates = []
        self.feed = TelegramFeed(self.tmp.name, self.api)
        self.token = '123456:' + 'A' * 30
        self.feed.save({'token': self.token, 'channel': '-100123', 'enabled': True})

    def tearDown(self):
        self.tmp.cleanup()

    def api(self, token, method, params):
        self.calls.append((method, params))
        if method == 'getWebhookInfo':
            return {'url': ''}
        if method == 'getChat':
            return {'id': -100123, 'type': 'channel'}
        return self.updates

    def update(self, uid, channel=-100123, key='channel_post'):
        return {'update_id': uid, key: {'chat': {'id': channel}, 'text': 'https://nlink.munpia.com/link/munpia/novel/598028'}}

    def test_links_captions_entities_and_domain_safety(self):
        self.assertEqual(parse_id('https://nlink.munpia.com/link/munpia/novel/598028?x=1'), '598028')
        message = {'text': '읽어보세요 (https://nlink.munpia.com/link/munpia/novel/598028).',
                   'caption': 'https://m.munpia.com/novel/detail/123',
                   'entities': [{'url': 'https://www.munpia.com/novel/detail/456'}],
                   'caption_entities': [{'url': 'https://nlink.munpia.com/link/munpia/novel/598028'}]}
        self.assertEqual(extract_ids(message), ['598028', '123', '456'])
        for host in ['nlink.munpia.com.evil.test', 'evil.test', 'nlink.munpia.com@evil.test']:
            self.assertEqual(extract_ids({'text': 'https://' + host + '/link/munpia/novel/598028'}), [])

    def test_channel_filter_cursor_restart_and_edits(self):
        self.updates = [self.update(1, -100999), self.update(2), self.update(3, key='edited_channel_post')]
        titles = []
        def append(ids):
            added = [x for x in ids if x not in titles]
            titles.extend(added)
            return added
        self.feed.poll(append)
        self.assertEqual(titles, ['598028'])
        self.assertEqual(self.feed.state()['offset'], 4)
        restarted = TelegramFeed(self.tmp.name, self.api)
        restarted.poll(lambda ids: self.fail('already processed'))
        self.assertEqual(self.calls[-1][1]['offset'], 4)

    def test_failure_does_not_acknowledge(self):
        self.updates = [self.update(5)]
        def full(ids):
            raise MunpiaError('full')
        with self.assertRaises(MunpiaError):
            self.feed.poll(full)
        self.assertEqual(self.feed.state()['offset'], 0)

    def test_settings_preserve_token_and_cursor_and_disable(self):
        self.feed.write_state({'offset': 17, 'message': 'test'})
        public = self.feed.save({'token': '', 'channel': '-100123', 'enabled': False})
        self.assertNotIn(self.token, json.dumps(public))
        self.assertEqual(self.feed.settings()['token'], self.token)
        self.assertEqual(self.feed.state()['offset'], 17)
        self.assertEqual(self.feed.settings_path.stat().st_mode & 0o777, 0o600)
        self.feed.poll(lambda ids: self.fail('disabled'))
        self.assertEqual(self.calls, [])

    def test_webhook_never_deleted(self):
        self.feed.api = lambda token, method, params: {'url': 'https://example.test/webhook'}
        with self.assertRaisesRegex(MunpiaError, '웹훅'):
            self.feed.poll(lambda ids: [])

    def test_token_validation_and_error_redaction(self):
        with self.assertRaises(MunpiaError):
            validate_settings({'token': 'bad/token'})
        with patch('urllib.request.build_opener', side_effect=RuntimeError(self.token)):
            with self.assertRaises(MunpiaError) as ctx:
                TelegramFeed.request(self.token, 'getUpdates', {})
        self.assertNotIn(self.token, str(ctx.exception))

    def test_interval_validation_migration_and_cursor_preservation(self):
        self.assertEqual(validate_settings({})['interval_minutes'], 1)
        for value in [0, -1, 1441, '1.5', '', True, None]:
            with self.assertRaises(MunpiaError):
                validate_settings({'interval_minutes': value})
        for value in [1, '5', 1440]:
            self.assertEqual(validate_settings({'interval_minutes': value})['interval_minutes'], int(value))
        self.feed.write_state({'offset': 123, 'message': 'test'})
        self.feed.save({'channel': '-100123', 'enabled': True, 'interval_minutes': '5'})
        restarted = TelegramFeed(self.tmp.name, self.api)
        self.assertEqual(restarted.public_settings()['interval_minutes'], 5)
        self.assertEqual(restarted.state()['offset'], 123)
        restarted.save({'channel': '-100123', 'enabled': True})
        self.assertEqual(restarted.settings()['interval_minutes'], 5)


if __name__ == '__main__':
    unittest.main()
