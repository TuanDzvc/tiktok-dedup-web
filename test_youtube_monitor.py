import copy
import unittest
from unittest.mock import Mock, patch

from youtube_monitor import (CloudStore, MonitorError, YouTubeMonitor,
                             YouTubeSource, channel_reference, discord_url)

CID = 'UC' + 'a' * 22


class MemoryStore:
    def __init__(self):
        self.data = {'version': 1, 'settings': {}, 'channels': {}, 'events': []}
        self.fail = False

    def load(self):
        return copy.deepcopy(self.data)

    def save(self, value):
        if self.fail:
            raise MonitorError('Storage unavailable')
        self.data = copy.deepcopy(value)


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()
        self.source = Mock()
        self.source.resolve.return_value = {'id': CID, 'title': 'Test channel', 'playlist': 'UU'+'a'*22}
        self.source.fetch.return_value = ([{'id': 'old00000001', 'title': 'Old', 'published_at': ''}], 'API', '')
        self.deliver = Mock()
        self.monitor = YouTubeMonitor(self.store, self.source, self.deliver)
        self.monitor.action('settings', {'enabled': True, 'telegram_chat_id': '123',
            'telegram_token': '123:'+'x'*30, 'api_keys': ['A'*35]})
        self.monitor.action('add', {'channel': CID})

    def new_video(self):
        self.source.fetch.return_value = ([{'id': 'new00000001', 'title': 'New', 'published_at': ''},
                                           {'id': 'old00000001', 'title': 'Old', 'published_at': ''}], 'RSS', 'API unavailable')
        self.monitor.data['channels'][CID]['next_check'] = 0

    def test_baseline_no_alert_and_new_upload_once(self):
        self.monitor.tick()
        self.deliver.assert_not_called()
        self.new_video()
        self.monitor.tick()
        self.deliver.assert_called_once()
        self.monitor.data['channels'][CID]['next_check'] = 0
        self.monitor.tick()
        self.deliver.assert_called_once()
        self.assertEqual(self.monitor.view()['events'][0]['sent'], ['telegram'])

    def test_restart_retains_seen_and_delivery(self):
        self.monitor.tick()
        self.new_video()
        self.monitor.tick()
        restarted = YouTubeMonitor(self.store, self.source, self.deliver)
        restarted.ensure_loaded()
        restarted.data['channels'][CID]['next_check'] = 0
        restarted.tick()
        self.deliver.assert_called_once()

    def test_notification_failure_retry_without_losing_event(self):
        self.monitor.tick()
        self.new_video()
        self.deliver.side_effect = MonitorError('Telegram unavailable')
        self.monitor.tick()
        event = self.monitor.data['events'][0]
        self.assertEqual(event['pending'], ['telegram'])
        self.assertIn('telegram', event['errors'])
        self.deliver.side_effect = None
        event['retry_at'] = 0
        self.monitor.tick()
        self.assertEqual(self.monitor.data['events'][0]['pending'], [])

    def test_ack_storage_failure_does_not_repeat_send_in_process(self):
        self.monitor.tick()
        self.new_video()
        self.deliver.side_effect = lambda *args: setattr(self.store, 'fail', True)
        with self.assertRaises(MonitorError):
            self.monitor.tick()
        self.assertIsNotNone(self.monitor.pending_ack)
        self.store.fail = False
        self.monitor.tick()
        self.deliver.assert_called_once()

    def test_failed_config_save_keeps_previous_configuration(self):
        self.store.fail = True
        with self.assertRaises(MonitorError):
            self.monitor.action('settings', {'enabled': False})
        self.assertTrue(self.monitor.view()['settings']['enabled'])

    def test_failed_boot_does_not_save_empty_store(self):
        store = Mock()
        store.load.side_effect = MonitorError('Read failed')
        monitor = YouTubeMonitor(store)
        with self.assertRaises(MonitorError):
            monitor.action('settings', {'enabled': True})
        store.save.assert_not_called()

    def test_api_errors_do_not_reset_baseline(self):
        self.monitor.tick()
        self.source.fetch.side_effect = MonitorError('All sources failed')
        self.monitor.data['channels'][CID]['next_check'] = 0
        self.monitor.tick()
        self.assertTrue(self.monitor.data['channels'][CID]['baseline'])
        self.assertEqual(self.monitor.data['channels'][CID]['seen'], ['old00000001'])
        self.assertIn('All sources failed', self.monitor.view()['channels'][0]['error'])

    def test_paused_and_removed_channels_not_sent(self):
        self.monitor.action('toggle', {'id': CID, 'enabled': False})
        self.monitor.tick()
        self.source.fetch.assert_not_called()
        self.monitor.action('delete', {'id': CID})
        self.assertFalse(self.monitor.view()['channels'])

    def test_empty_channel_baseline_and_ignore_later_old_upload(self):
        self.source.fetch.return_value = ([], 'API', '')
        self.monitor.tick()
        self.source.fetch.return_value = ([{'id': 'old00000002', 'title': 'Old resurfaced',
                                            'published_at': '2020-01-01T00:00:00Z'}], 'RSS', '')
        self.monitor.data['channels'][CID]['next_check'] = 0
        self.monitor.tick()
        self.deliver.assert_not_called()
        self.assertFalse(self.monitor.view()['events'])

    def test_secrets_are_not_returned_and_blank_fields_preserve(self):
        self.monitor.action('settings', {'enabled': True, 'telegram_chat_id': '456'})
        text = str(self.monitor.view())
        self.assertNotIn('x'*30, text)
        self.assertNotIn('A'*35, text)
        self.assertEqual(self.monitor.data['settings']['telegram_token'], '123:'+'x'*30)
        self.assertNotIn('seen', self.monitor.view()['channels'][0])

    def test_test_notification_is_explicit(self):
        self.monitor.test('telegram')
        self.deliver.assert_called_once()

    def test_three_second_interval_is_saved_and_reschedules_channels(self):
        self.monitor.tick()
        self.assertGreater(self.monitor.data['channels'][CID]['next_check'], 0)
        result = self.monitor.action('settings', {'enabled': True, 'interval_seconds': 3})
        self.assertEqual(result['settings']['interval_seconds'], 3)
        self.assertEqual(self.store.data['settings']['interval_seconds'], 3)
        self.assertEqual(self.monitor.data['channels'][CID]['next_check'], 0)
        result = self.monitor.action('settings', {'enabled': True, 'interval_seconds': 1})
        self.assertEqual(result['settings']['interval_seconds'], 3)


class SourceTests(unittest.TestCase):
    def test_channel_input_limits_targets_to_youtube(self):
        self.assertEqual(channel_reference('https://youtube.com/channel/'+CID), ('id', CID))
        self.assertEqual(channel_reference('https://www.youtube.com/@example/videos'), ('forHandle', '@example'))
        for value in ('http://127.0.0.1', 'https://evil.test/@example', 'https://youtube.com/watch?v=x'):
            with self.assertRaises(MonitorError):
                channel_reference(value)

    def test_webhook_target_validation(self):
        self.assertTrue(discord_url('https://discord.com/api/webhooks/123/token'))
        for value in ('http://127.0.0.1/api/webhooks/1/x', 'https://discord.com.evil.test/api/webhooks/1/x',
                      'https://discord.com/api/webhooks/1/x?redirect=evil'):
            with self.assertRaises(MonitorError):
                discord_url(value)

    def test_quota_error_falls_back_to_rss_without_keys_in_errors(self):
        session = Mock()
        quota = Mock(status_code=403)
        feed = Mock()
        feed.content = b'<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015"><entry><yt:videoId>new00000001</yt:videoId><title>New</title></entry></feed>'
        session.get.side_effect = [quota, feed]
        source = YouTubeSource(session)
        rows, name, warning = source.fetch({'id': CID, 'playlist': 'UU'+'a'*22}, ['secret-api-key'])
        self.assertEqual(name, 'RSS')
        self.assertEqual(rows[0]['id'], 'new00000001')
        self.assertNotIn('secret-api-key', warning)


class RouteTests(unittest.TestCase):
    def setUp(self):
        import server
        from fastapi.testclient import TestClient
        self.server = server
        self.client = TestClient(server.app)

    def test_guest_and_member_cannot_read_or_write(self):
        for user in (None, {'username': 'member', 'role': 'member'}):
            with patch.object(self.server, '_extract_vip_user', return_value=user):
                self.assertEqual(self.client.get('/api/youtube-monitor/state').status_code, 403)
                self.assertEqual(self.client.post('/api/youtube-monitor/settings', json={}).status_code, 403)

    def test_cloud_admin_can_read_only_sanitized_state(self):
        monitor = YouTubeMonitor(MemoryStore())
        with patch.object(self.server, '_extract_vip_user', return_value={'username':'admin','role':'admin'}), \
             patch.object(self.server, '_is_cloud_runtime', return_value=True), \
             patch.object(self.server, '_YOUTUBE_MONITOR', monitor):
            result = self.client.get('/api/youtube-monitor/state')
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json()['runtime'], 'Render')
            self.assertEqual(self.client.get('/api/youtube-monitor/settings').status_code, 405)
            self.assertEqual(self.client.post('/api/youtube-monitor/settings', json=[]).status_code, 400)

    def test_pc_delegates_without_running_monitor_or_forwarding_browser_token(self):
        response = Mock(status_code=200)
        response.json.return_value = {'success': True, 'runtime': 'Render'}
        with patch.object(self.server, '_extract_vip_user', return_value={'username':'admin','role':'admin'}), \
             patch.object(self.server, '_is_cloud_runtime', return_value=False), \
             patch.object(self.server.requests, 'request', return_value=response) as forward:
            result = self.client.get('/api/youtube-monitor/state', headers={'Authorization':'Bearer browser-token'})
            self.assertEqual(result.json()['runtime'], 'Render')
            self.assertNotIn('Authorization', forward.call_args.kwargs['headers'])
            self.assertEqual(forward.call_args.kwargs['headers']['X-YouTube-Admin'], 'admin')


if __name__ == '__main__':
    unittest.main()
