"""Consistency tests: device fingerprint must stay stable across requests."""
import pytest

from tiktok_signer import DeviceProfile, TikTokSigner

PARAMS = {
    "aid": "1233",
    "app_name": "musical_ly",
    "device_platform": "android",
    "os_version": "9",
    "device_type": "2203121C",
    "device_brand": "Xiaomi",
    "channel": "googleplay",
    "language": "id",
    "region": "ID",
}


def trace_prefix(headers):
    return headers["x-tt-trace-id"].split("-")[1][:16]


@pytest.fixture(autouse=True)
def reset_device():
    TikTokSigner.set_device(None)
    yield
    TikTokSigner.set_device(None)


def test_trace_id_stable_for_same_device():
    TikTokSigner.set_device(DeviceProfile(device_id="1234567890abcdef"))
    h1 = TikTokSigner.generate_headers(params=PARAMS)
    h2 = TikTokSigner.generate_headers(params=PARAMS)
    assert trace_prefix(h1) == trace_prefix(h2) == "1234567890abcdef"


def test_auto_profile_stable_within_session():
    h1 = TikTokSigner.generate_headers(params=PARAMS)
    h2 = TikTokSigner.generate_headers(params=PARAMS)
    assert trace_prefix(h1) == trace_prefix(h2)


def test_different_profiles_differ():
    TikTokSigner.set_device(DeviceProfile(device_id="aaaaaaaaaaaaaaaa"))
    a = TikTokSigner.generate_headers(params=PARAMS)
    TikTokSigner.set_device(DeviceProfile(device_id="bbbbbbbbbbbbbbbb"))
    b = TikTokSigner.generate_headers(params=PARAMS)
    assert trace_prefix(a) != trace_prefix(b)
