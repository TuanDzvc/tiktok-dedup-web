import copy
import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from outlook_client_worker import ClientMailWorker, launcher


class FakeService:
    def __init__(self):
        self._lock = threading.RLock()
        self._state = {'folders': {'Default': [{'row_id': 'row1', 'email': 'fixture@example.com',
            'password': 'fixture-password', 'checked': True}]}, 'current_folder': 'Default', 'settings': {'threads': 1}}
        self._running = False
        self.logs = []
        self.saved = None
    def _save_unlocked(self): self.saved = copy.deepcopy(self._state)
    def _find_row_unlocked(self, folder, rid):
        return next((r for r in self._state['folders'].get(folder, []) if r['row_id'] == rid), None)
    def _parsed_proxies(self, value): return value.splitlines()
    def _add_log(self, value, kind): self.logs.append(value)
    def get_snapshot(self):
        self._client_worker.refresh_status()
        return {'workers': self._client_worker.public_workers()}


class ClientWorkerTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000
        self.s = FakeService()
        self.w = ClientMailWorker(self.s, clock=lambda: self.now, verify_token=lambda *a: (True, 'OK'))
        self.wid, self.key = self.w.pair()
        self.w.poll(self.wid, self.key, {'name': 'fixture VM'})

    def claim(self):
        self.w.start({'worker_id': self.wid})
        return self.w.poll(self.wid, self.key, {})['jobs'][0]

    def result(self, job, **extra):
        return self.w.event(self.wid, self.key, {'job_id': job['id'], 'lease': job['lease'],
            'kind': 'result', 'ok': True, 'refresh_token': 'fixture-refresh-token-long-enough', **extra})

    def test_client_browser_respects_hidden_setting(self):
        self.s._state['settings']['show_browser'] = False
        job = self.claim()
        self.assertFalse(job['show_browser'])

    def test_client_browser_visible_by_default(self):
        self.assertTrue(self.claim()['show_browser'])

    def test_worker_stage_is_translated_before_display(self):
        job = self.claim()
        self.w.event(self.wid, self.key, {'job_id': job['id'], 'lease': job['lease'],
            'kind': 'log', 'text': 'Opening Microsoft sign-in on this client'})
        from mail_worker_messages import localize_mail_message
        expected = localize_mail_message('Opening Microsoft sign-in on this client')
        self.assertNotEqual(expected, 'Opening Microsoft sign-in on this client')
        self.assertEqual(self.s.logs[-1], expected)

    def test_pair_stores_only_hash(self):
        self.assertNotIn(self.key, json.dumps(self.s.saved))
        self.assertEqual(self.s.saved['client_workers'][self.wid]['hash'], hashlib.sha256(self.key.encode()).hexdigest())
        self.assertNotIn('hash', self.w.public_workers()[0])

    def test_wrong_key_rejected(self):
        with self.assertRaises(PermissionError): self.w.poll(self.wid, 'wrong', {})

    def test_jobs_pinned_to_one_worker(self):
        job = self.claim()
        other, key = self.w.pair()
        self.assertFalse(self.w.poll(other, key, {})['jobs'])
        with self.assertRaises(PermissionError):
            self.w.event(other, key, {'job_id': job['id'], 'lease': job['lease']})
        self.assertNotIn('fixture-password', json.dumps(self.s.saved['client_jobs']))

    def test_result_idempotent_and_saved(self):
        job = self.claim()
        self.result(job)
        self.assertEqual(self.s.saved['folders']['Default'][0]['status'], 'OK')
        self.assertTrue(self.result(job)['already_done'])
        self.assertEqual(self.s._done_count, 1)

    def test_changed_account_rejects_token(self):
        job = self.claim()
        self.s._state['folders']['Default'][0]['password'] = 'new-password'
        with self.assertRaises(ValueError): self.result(job)
        self.assertNotIn('oauth2_token', self.s._state['folders']['Default'][0])

    def test_revoke_cancels_and_blocks(self):
        job = self.claim()
        self.w.revoke(self.wid)
        self.assertEqual(self.s.saved['client_jobs'][job['id']]['status'], 'cancelled')
        with self.assertRaises(PermissionError): self.w.poll(self.wid, self.key, {})

    def test_stop_notifies_child(self):
        job = self.claim()
        self.w.stop()
        self.assertIn(job['id'], self.w.poll(self.wid, self.key, {'active': [job['id']]})['cancel'])
        self.assertTrue(self.result(job)['cancel'])

    def test_lost_is_not_reassigned(self):
        job = self.claim()
        self.now += 100
        self.w.refresh_status()
        self.assertEqual(self.s._state['client_jobs'][job['id']]['status'], 'lost')
        self.assertFalse(self.w.poll(self.wid, self.key, {})['jobs'])

    def test_restore_keys_and_pending_queue(self):
        self.w.start({'worker_id': self.wid})
        restored = FakeService()
        restored._state = copy.deepcopy(self.s.saved)
        controller = ClientMailWorker(restored, clock=lambda: self.now)
        self.assertEqual(len(controller.poll(self.wid, self.key, {})['jobs']), 1)

    def test_cloud_failure_rolls_back_result(self):
        job = self.claim()
        def fail(): raise OSError('fixture cloud unavailable')
        self.s._save_unlocked = fail
        with self.assertRaises(OSError): self.result(job)
        self.assertNotIn('oauth2_token', self.s._state['folders']['Default'][0])

    def test_log_redacts_password(self):
        job = self.claim()
        self.w.event(self.wid, self.key, {'job_id': job['id'], 'lease': job['lease'], 'kind': 'log', 'text': 'fixture-password'})
        self.assertNotIn('fixture-password', self.s.logs[-1])

    def test_browser_failure_is_not_misreported_as_account_verification(self):
        job=self.claim()
        self.result(job,ok=False,failure_code='cdp_connect',failure_line=203,
                    failure_message='fixture-password should never be logged')
        row=self.s._state['folders']['Default'][0]
        self.assertIn('Chromium',row['status'])
        self.assertIn('203',row['status'])
        self.assertNotIn('fixture-password',' '.join(self.s.logs))
        self.assertNotIn('xác minh',row['status'])

    def test_browser_exit_diagnostic_survives_save(self):
        job=self.claim()
        self.result(job,ok=False,failure_code='browser_exited',browser_exit=-1073741515)
        self.assertIn('-1073741515',self.s.saved['folders']['Default'][0]['status'])

    def test_launcher_contains_only_scoped_key(self):
        bat = launcher('https://checktiktok.site', self.wid, self.key, Path(__file__).parent)
        self.assertIn(self.key, bat)
        self.assertNotIn('fixture-password', bat)
        self.assertIn('ProtectedData', bat)
        self.assertIn('Get-FileHash', bat)

    def test_package_uses_same_cloud_origin_as_its_hashes(self):
        bat=launcher('https://checktiktok.site',self.wid,self.key,Path(__file__).parent,
                     asset_origin='https://tiktok-dedup-web.onrender.com')
        self.assertIn('TF_MAIL_ORIGIN=https://checktiktok.site',bat)
        self.assertIn('TF_MAIL_ASSETS=https://tiktok-dedup-web.onrender.com',bat)
        self.assertIn("$env:TF_MAIL_ASSETS+'/static/'",bat)

    def test_launcher_detaches_hidden_setup_without_console_pause(self):
        bat=launcher('https://checktiktok.site',self.wid,self.key,Path(__file__).parent)
        self.assertIn('start "" powershell.exe -WindowStyle Hidden',bat)
        self.assertNotIn('pause',bat.lower())
        self.assertIn('setup-error.log',bat)

    def test_api_worker_key_and_owner_boundary(self):
        import server
        from fastapi.testclient import TestClient
        with patch.object(server, '_is_cloud_runtime', return_value=True), \
             patch.object(server, '_prepare_client_mail_service', return_value=self.s), \
             patch.object(server, '_extract_vip_user', return_value=None):
            client = TestClient(server.app)
            self.assertEqual(client.get('/api/outlook-oauth/state').status_code, 403)
            self.assertEqual(client.post('/api/outlook-oauth/worker/download').status_code, 403)
            self.assertEqual(client.post('/api/outlook-oauth/worker/poll', json={}).status_code, 403)
            headers={'X-Mail-Worker-Id': self.wid, 'X-Mail-Worker-Token': self.key}
            response=client.post('/api/outlook-oauth/worker/poll',json={},headers=headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['jobs'], [])

    def test_api_download_is_admin_only_and_uncacheable(self):
        import server
        from fastapi.testclient import TestClient
        with patch.object(server, '_is_cloud_runtime', return_value=True), \
             patch.object(server, '_prepare_client_mail_service', return_value=self.s), \
             patch.object(server, '_extract_vip_user', return_value={'username':'tuandzvcc','role':'admin'}):
            response=TestClient(server.app).post('/api/outlook-oauth/worker/download')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers['Cache-Control'], 'no-store')
            self.assertIn('tikflow_mail_worker.bat',response.headers['Content-Disposition'])
            self.assertNotIn('fixture-password',response.text)


if __name__ == '__main__': unittest.main()
