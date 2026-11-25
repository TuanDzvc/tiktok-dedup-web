"""Local algorithm checks; these do not establish TikTok server acceptance."""
import ctypes
import json
import unittest

import reader
from tiktok_signer.lib.ladon import Ladon
from tiktok_signer.lib.utils.sm3 import SM3


class OfflineChecks(unittest.TestCase):
    def test_sm3_published_abc_vector(self):
        self.assertEqual(SM3().digest(b'abc').hex(),
                         '66c7f0f462eeedd9d1f2d46bdc10e4e24167c4875cf2f7a2297da02b8f4ba8e0')

    def test_ladon_rotate_preserves_both_halves(self):
        self.assertEqual(Ladon._ror(ctypes.c_ulonglong(0x0123456789ABCDEF), 8),
                         0xEF0123456789ABCD)

    def test_body_and_query_affect_gorgon(self):
        common = dict(device=reader.DeviceProfile(device_id='123456789'), unix=1795000000)
        sign = reader.TikTokSigner.generate_headers
        a = sign('aid=1233&count=18', data=b'one', **common)
        b = sign('aid=1233&count=19', data=b'one', **common)
        c = sign('aid=1233&count=18', data=b'two', **common)
        self.assertNotEqual(a['x-gorgon'], b['x-gorgon'])
        self.assertNotEqual(a['x-gorgon'], c['x-gorgon'])
        self.assertNotEqual(a['x-ss-stub'], c['x-ss-stub'])

    def test_ttencrypt_roundtrip_unicode(self):
        payload = {'header': {'aid': 1233}, 'text': 'kiểm tra trùng'}
        wire = reader.TikTokSigner.encrypt(payload)
        self.assertEqual(json.loads(reader.TikTokSigner.decrypt(wire)), payload)

    def test_manager_access_is_rejected(self):
        with self.assertRaises(RuntimeError):
            reader.independence_guard('open', ('C:/fake/tiktokmanager/index.jsc',))


if __name__ == '__main__':
    unittest.main()
