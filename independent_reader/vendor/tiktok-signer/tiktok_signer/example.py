"""
TikTok Signer Example

Mirrors a real TikTok Android request captured in ``tiktok-search-all.har``:
full query parameters, app headers and signed headers for the
``aweme/v1/search/sug/stream/`` endpoint.

Usage:
    python3 -m tiktok_signer.example
    python3 tiktok_signer/example.py
"""
import json
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

if __name__ == "__main__" and not __package__:
    sys.path.insert(0, str(Path(__file__).parent.parent))

from tiktok_signer import DeviceProfile, TikTokSigner, __version__

USER_AGENT = (
    "com.zhiliaoapp.musically/2023700040 (Linux; U; Android 9; in_ID; "
    "2203121C; Build/PQ3A.190705.09121607;tt-ok/3.12.13.4-tiktok)"
)


def build_api_params(profile: DeviceProfile, device_id: str, install_id: str) -> dict:
    """Full query parameter set of a real request (from the HAR capture)."""
    now = int(time.time() * 1000)
    return {
        "device_platform": "android",
        "os": "android",
        "ssmix": "a",
        "_rticket": str(now),
        "cdid": profile.cdid,
        "channel": profile.channel,
        "aid": "1233",
        "app_name": "musical_ly",
        "version_code": "370004",
        "version_name": "37.0.4",
        "manifest_version_code": "2023700040",
        "update_version_code": "2023700040",
        "ab_version": "37.0.4",
        "resolution": "720*1280",
        "dpi": "320",
        "device_type": profile.device_type,
        "device_brand": "Xiaomi",
        "language": "id",
        "os_api": "28",
        "os_version": profile.os_version,
        "ac": "wifi",
        "is_pad": "0",
        "current_region": "ID",
        "app_type": "normal",
        "sys_region": "ID",
        "last_install_time": str(now // 1000 - 86400),
        "mcc_mnc": "51000",
        "timezone_name": "Asia/Bangkok",
        "carrier_region_v2": "510",
        "residence": "ID",
        "app_language": "id",
        "carrier_region": "ID",
        "timezone_offset": "25200",
        "host_abi": "arm64-v8a",
        "locale": "id-ID",
        "ac2": "wifi",
        "uoo": "0",
        "op_region": "ID",
        "build_number": "37.0.4",
        "region": "ID",
        "ts": str(now // 1000),
        "iid": install_id,
        "device_id": device_id,
        "openudid": profile.openudid,
    }


def build_app_headers(body: bytes) -> dict:
    """App-level headers (from the HAR capture); signed headers are added by the signer."""
    return {
        "user-agent": USER_AGENT,
        "accept-encoding": "gzip",
        "rpc-persist-pyxis-policy-v-tnc": "1",
        "sdk-version": "2",
        "passport-sdk-version": "6031490",
        "x-vc-bdturing-sdk-version": "2.3.8.i18n",
        "x-metasec-event-source": "native",
        "x-tt-request-tag": "n=0;nr=011;bg=0",
        "x-tt-pba-enable": "1",
        "x-tt-dm-status": "login=0;ct=1;rt=7",
        "x-tt-store-region": "id",
        "x-tt-store-region-src": "did",
        "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
        "content-length": str(len(body)),
    }


def main() -> None:
    print("=" * 60)
    print(f"TikTok Signer v{__version__}")
    print("=" * 60)
    print()

    print("[1] Device Profile (stable fingerprint)")
    print("-" * 60)
    profile = DeviceProfile(device_id="1234567890abcdef")
    TikTokSigner.set_device(profile)
    print(f"device_id : {profile.device_id}")
    print(f"openudid  : {profile.openudid}")
    print(f"cdid      : {profile.cdid}")
    print(f"to_json   : {profile.to_json()}")
    print()

    print("[2] Real-world signed request (per HAR)")
    print("-" * 60)
    device_id = "7671700755837978119"      # from device registration
    install_id = "7671702915996206855"      # from device registration
    params = build_api_params(profile, device_id=device_id, install_id=install_id)
    params_str = urlencode(params)
    body = "keyword=tiktok&source=search_sug".encode()

    headers = TikTokSigner.generate_headers(params=params_str, data=body, device=profile)
    headers.update(build_app_headers(body))

    print("query params: " + str(len(params)) + " keys")
    print("signed headers:")
    for key in ("x-ss-req-ticket", "x-ss-stub", "x-ladon", "x-khronos", "x-argus", "x-gorgon"):
        print(f"  {key}: {headers[key][:44]}...")
    print("app headers:")
    for key in ("user-agent", "sdk-version", "x-tt-dm-status", "x-tt-store-region"):
        print(f"  {key}: {headers[key]}")
    url = "https://search22-normal-c-alisg.tiktokv.com/aweme/v1/search/sug/stream/?" + params_str
    print("url:", url[:100] + "...")
    print()

    print("[3] Stable trace id across requests")
    print("-" * 60)
    h1 = TikTokSigner.generate_headers(params=params_str, data=body, device=profile)
    h2 = TikTokSigner.generate_headers(params=params_str, data=body, device=profile)

    def trace(h):
        return h["x-tt-trace-id"].split("-")[1][:16]

    print(f"trace-id 1: {trace(h1)}")
    print(f"trace-id 2: {trace(h2)}")
    print(f"Consistent: {trace(h1) == trace(h2)}")
    print()

    print("[4] TTEncrypt (device_register payload)")
    print("-" * 60)
    device_info = {
        "magic_tag": "ss_app_log",
        "header": {
            "display_name": "TikTok",
            "aid": 1233,
            "channel": profile.channel,
            "package": "com.zhiliaoapp.musically",
            "version_name": "37.0.4",
            "os": "Android",
            "os_version": profile.os_version,
            "device_model": profile.device_type,
            "device_brand": "Xiaomi",
            "openudid": profile.openudid,
            "clientudid": profile.cdid,
            "language": "id",
        },
        "_gen_time": int(time.time() * 1000),
    }
    print(f"Input: {json.dumps(device_info)}")
    print()
    encrypted = TikTokSigner.encrypt(device_info)
    print(f"Encrypted: {len(encrypted)} bytes")
    print(f"Prefix: {encrypted[:6].hex()} (marker 746305100000)")
    print(f"Roundtrip decrypt: {TikTokSigner.decrypt(encrypted) == json.dumps(device_info, separators=(',', ':'))}")
    print()

    print("[5] Generate Headers with Custom Unix Timestamp")
    print("-" * 60)
    custom_unix = int(time.time()) - 60
    headers_unix = TikTokSigner.generate_headers(params=params_str, unix=custom_unix)
    print(f"x-ss-req-ticket: {headers_unix['x-ss-req-ticket']}")
    print(f"x-khronos      : {headers_unix['x-khronos']}")
    print(f"Note: x-khronos should be {custom_unix}")
    print()

    print("[6] Protobuf Encode/Decode")
    print("-" * 60)
    protobuf_data = {
        1: "string_value",
        2: 12345,
        3: {
            1: "nested_value",
            2: 67890,
        },
    }
    print(f"Input: {protobuf_data}")
    print()
    encoded = TikTokSigner.encode(protobuf_data)
    print(f"Encoded: {len(encoded)} bytes")
    print(f"Hex: {encoded.hex()}")
    print()
    decoded = TikTokSigner.decode(encoded)
    print(f"Decoded: {decoded}")
    print()

    print("=" * 60)
    print("All examples completed successfully!")
    print("=" * 60)


if __name__ == "__main__":
    main()
