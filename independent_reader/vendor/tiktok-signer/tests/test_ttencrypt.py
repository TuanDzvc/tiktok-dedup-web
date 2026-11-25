"""TTEncrypt roundtrip test (guards the gzip-compression fix)."""
from tiktok_signer import TikTokSigner


def test_encrypt_decrypt_roundtrip():
    data = {"magic_tag": "ss_app_log", "header": {"aid": 1233}}
    encrypted = TikTokSigner.encrypt(data)
    assert encrypted[:6] == bytes.fromhex("746305100000")
    assert TikTokSigner.decrypt(encrypted) == '{"magic_tag":"ss_app_log","header":{"aid":1233}}'


def test_encrypt_accepts_str_and_bytes():
    for payload in ('{"a":1}', b'{"a":1}'):
        encrypted = TikTokSigner.encrypt(payload)
        assert TikTokSigner.decrypt(encrypted) == '{"a":1}'
