import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import dev_mail_worker as dev


class DevMailWorkerTests(unittest.TestCase):
    def test_reuses_existing_local_pair_and_process(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {'LOCALAPPDATA': temporary}):
            wid = 'a' * 24
            root = Path(temporary) / 'TikFlow' / 'MailWorker' / wid
            root.mkdir(parents=True)
            (root / 'config.bin').write_bytes(b'fixture-sealed')
            metadata = dev._metadata_path()
            metadata.parent.mkdir(parents=True)
            metadata.write_text(json.dumps({'id': wid}))
            source = Path(temporary) / 'repo' / 'static'
            source.mkdir(parents=True)
            for name in ('mail_worker.ps1', 'mail_cdp_worker.cs'):
                (source / name).write_text('fixture')
            state = {'workers': [{'id': wid, 'online': True, 'revoked': False}]}
            with patch.object(dev, '_snapshot', return_value=state), patch.object(dev, '_running', return_value=True), \
                 patch.object(dev.requests, 'post') as pair, patch.object(dev.subprocess, 'Popen') as launch:
                result = dev.ensure_dev_worker(source.parent, 'https://fixture.invalid', 'Bearer fixture')
                self.assertEqual(result['worker_id'], wid)
                pair.assert_not_called()
                launch.assert_not_called()

    def test_bootstrap_protects_key_and_does_not_execute_bat(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {'LOCALAPPDATA': temporary}):
            source = Path(temporary) / 'repo' / 'static'
            source.mkdir(parents=True)
            for name in ('mail_worker.ps1', 'mail_cdp_worker.cs'):
                (source / name).write_text('fixture')
            wid, key = 'b' * 24, 'fixture-key-' * 4
            response = SimpleNamespace(text=f'set "TF_MAIL_ID={wid}"\nset "TF_MAIL_TOKEN={key}"', raise_for_status=lambda: None)
            states = [{'workers': []}, {'workers': [{'id': wid, 'online': True}]}]
            with patch.object(dev, '_snapshot', side_effect=states), patch.object(dev, '_running', return_value=False), \
                 patch.object(dev.requests, 'post', return_value=response), \
                 patch.object(dev.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as protect, \
                 patch.object(dev.subprocess, 'Popen') as launch:
                result = dev.ensure_dev_worker(source.parent, 'https://fixture.invalid', 'Bearer fixture')
                self.assertEqual(result['worker_id'], wid)
                self.assertNotIn(key, repr(protect.call_args.args))
                self.assertIn('ProtectedData', str(protect.call_args.args))
                self.assertNotIn(key, dev._metadata_path().read_text())
                self.assertNotIn('.bat', repr(launch.call_args))
                self.assertIn('mail_worker.ps1', repr(launch.call_args))

    def test_dev_endpoint_requires_owner_and_local_windows_runtime(self):
        import server
        from fastapi.testclient import TestClient
        client = TestClient(server.app)
        with patch.object(server, '_extract_vip_user', return_value=None):
            self.assertEqual(client.post('/api/outlook-oauth/dev-worker/ensure').status_code, 403)
        with patch.object(server, '_extract_vip_user', return_value={'username': 'tuandzvcc', 'role': 'admin'}), \
             patch.object(server, '_is_cloud_runtime', return_value=True):
            self.assertEqual(client.post('/api/outlook-oauth/dev-worker/ensure').status_code, 409)


if __name__ == '__main__':
    unittest.main()
