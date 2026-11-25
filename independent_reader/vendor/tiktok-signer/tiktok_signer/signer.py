"""TikTok Signer - Main module for TikTok API authentication."""
import gzip
import json
import threading
from random import choice, getrandbits
from time import time
from typing import Any, Dict, Optional, Union
from uuid import uuid4

from tiktok_signer.lib.argus import Argus
from tiktok_signer.lib.gorgon import Gorgon
from tiktok_signer.lib.ladon import Ladon
from tiktok_signer.lib.stub import generate_stub
from tiktok_signer.lib.ttencrypt import TTEncrypt
from tiktok_signer.lib.utils.protobuf import ProtoBuf, protobuf_decode, protobuf_encode


class SignedHeader:
    """Header names produced by :meth:`TikTokSigner.generate_headers`.

    ``STUB`` and ``COOKIE`` are only present when a body or cookie is supplied.
    """

    REQ_TICKET = "x-ss-req-ticket"
    TRACE_ID = "x-tt-trace-id"
    STUB = "x-ss-stub"
    LADON = "x-ladon"
    GORGON = "x-gorgon"
    KHRONOS = "x-khronos"
    ARGUS = "x-argus"
    COOKIE = "cookie"


class DeviceProfile:
    """Stable per-device identity used to keep TikTok fingerprints consistent.

    TikTok tracks requests via several identifiers derived from the device
    (trace id, Argus device_id, device_type, os_version, openudid, cdid, ...).
    Generating these fresh on every call makes the account look like a
    new/rotating device, which triggers risk checks. A DeviceProfile is
    generated once and reused for the whole session so every signed request
    carries the same device fingerprint.
    """

    DEFAULT_DEVICE_TYPE: str = "2203121C"
    DEFAULT_OS_VERSION: str = "9"
    DEFAULT_CHANNEL: str = "googleplay"

    def __init__(
        self,
        device_id: Optional[str] = None,
        device_type: str = DEFAULT_DEVICE_TYPE,
        os_version: str = DEFAULT_OS_VERSION,
        channel: str = DEFAULT_CHANNEL,
        openudid: Optional[str] = None,
        cdid: Optional[str] = None,
    ) -> None:
        if device_id is None:
            device_id = "%016x" % getrandbits(64)
        if openudid is None:
            openudid = "%016x" % getrandbits(64)
        if cdid is None:
            cdid = str(uuid4())
        self.device_id = device_id
        self.device_type = device_type
        self.os_version = os_version
        self.channel = channel
        self.openudid = openudid
        self.cdid = cdid

    @classmethod
    def load(cls, data: Union[str, Dict[str, Any]]) -> "DeviceProfile":
        """Build a profile from a JSON string or dict (e.g. persisted session)."""
        payload = json.loads(data) if isinstance(data, str) else data
        return cls(
            device_id=payload.get("device_id"),
            device_type=payload.get("device_type", cls.DEFAULT_DEVICE_TYPE),
            os_version=payload.get("os_version", cls.DEFAULT_OS_VERSION),
            channel=payload.get("channel", cls.DEFAULT_CHANNEL),
            openudid=payload.get("openudid"),
            cdid=payload.get("cdid"),
        )

    def to_dict(self) -> Dict[str, str]:
        return {
            "device_id": self.device_id,
            "device_type": self.device_type,
            "os_version": self.os_version,
            "channel": self.channel,
            "openudid": self.openudid,
            "cdid": self.cdid,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), separators=(",", ":"))


class TikTokSigner:
    """TikTok API authentication signer with encryption support.

    This class provides complete TikTok Android API authentication including:
    - Request signing (Ladon, Gorgon, Argus)
    - Data encryption/decryption (TTEncrypt)
    """

    DEFAULT_AID: int = 1233
    DEFAULT_LC_ID: int = 2142840551
    DEFAULT_SDK_VER: str = "v05.01.02-alpha.7-ov-android"
    DEFAULT_SDK_VER_CODE: int = 83952160
    DEFAULT_APP_VER: str = "37.0.4"
    DEFAULT_VERSION_CODE: int = 2023700040

    _encryptor: Optional[TTEncrypt] = None
    _device: Optional[DeviceProfile] = None
    _device_lock: "threading.Lock" = threading.Lock()

    @classmethod
    def _get_encryptor(cls) -> TTEncrypt:
        """Get or create TTEncrypt instance."""
        if cls._encryptor is None:
            cls._encryptor = TTEncrypt()
        return cls._encryptor

    @classmethod
    def set_device(cls, device: Union[DeviceProfile, Dict, str, None]) -> DeviceProfile:
        """Set the device profile used for all subsequent signed requests.

        Pass a DeviceProfile, a dict/JSON of one, or None to generate a fresh
        random profile. The same profile must be reused across a session so the
        device fingerprint stays consistent.
        """
        with cls._device_lock:
            if device is None:
                cls._device = DeviceProfile()
            elif isinstance(device, DeviceProfile):
                cls._device = device
            else:
                cls._device = DeviceProfile.load(device)
            return cls._device

    @classmethod
    def get_device(cls) -> DeviceProfile:
        """Return the current device profile, creating one if unset."""
        with cls._device_lock:
            if cls._device is None:
                cls._device = DeviceProfile()
            return cls._device

    @classmethod
    def generate_headers(
        cls,
        params: Union[str, Dict],
        data: Optional[Union[str, Dict, bytes]] = None,
        device_id: Optional[str] = None,
        aid: Union[str, int] = 1233,
        lc_id: Union[str, int] = 2142840551,
        sdk_ver: str = "v05.01.02-alpha.7-ov-android",
        sdk_ver_code: Union[str, int] = 83952160,
        version_name: str = "37.0.4",
        version_code: Union[str, int] = 2023700040,
        cookie: Optional[str] = None,
        unix: Optional[int] = None,
        device: Union[DeviceProfile, Dict, str, None] = None
    ) -> Dict[str, str]:
        """Generate authentication headers for a TikTok API request.

        All signature headers (Ladon, Gorgon, Argus) are computed from the same
        parameters and timestamp so TikTok accepts the request. The device
        fingerprint is taken from the active :class:`DeviceProfile` (set via
        :meth:`set_device` or passed as ``device``) and stays stable across
        calls.

        Args:
            params: URL query parameters as a dict or query string.
            data: Request body for POST requests (str, bytes, or dict).
            device_id: Device identifier. Prefer the ``device`` profile so the
                fingerprint stays consistent; kept for backward compatibility.
            aid: Application ID.
            lc_id: License ID.
            sdk_ver: SDK version string.
            sdk_ver_code: SDK version code.
            version_name: App version name (e.g., "37.0.4").
            version_code: App version code.
            cookie: Cookie string to sign and include in the headers.
            unix: Unix timestamp in seconds. If None, uses the current time.
            device: Device profile to use for this call. If None, the active
                profile from :meth:`set_device` is used.

        Returns:
            A dict of authentication headers with the :class:`SignedHeader`
            names: ``x-ss-req-ticket``, ``x-tt-trace-id``, ``x-ladon``,
            ``x-gorgon``, ``x-khronos`` and ``x-argus``, plus ``x-ss-stub``
            when ``data`` is given and ``cookie`` when one was provided.
        """
        aid = int(aid) if isinstance(aid, str) else aid
        lc_id = int(lc_id) if isinstance(lc_id, str) else lc_id
        sdk_ver_code = int(sdk_ver_code) if isinstance(sdk_ver_code, str) else sdk_ver_code
        version_code = int(version_code) if isinstance(version_code, str) else version_code

        profile = cls.get_device() if device is None else (
            device if isinstance(device, DeviceProfile) else DeviceProfile.load(device)
        )
        if device_id is None:
            device_id = profile.device_id

        if unix is None:
            ticket = time()
            unix = int(ticket)
        else:
            ticket = float(unix)

        if device_id:
            try:
                device_hex = hex(int(device_id))[2:]
            except ValueError:
                device_hex = device_id.lower()
            trace = (
                device_hex
                + "".join(choice("0123456789abcdef") for _ in range(2))
                + "0"
                + hex(aid)[2:]
            )
        else:
            trace = (
                device_id.lower()
                if device_id
                else str("%x" % (round(ticket * 1000) & 0xffffffff))
            ) + "10" + "".join(choice("0123456789abcdef") for _ in range(16))

        headers: Dict[str, str] = {
            "x-ss-req-ticket": str(int(ticket * 1000)),
            "x-tt-trace-id": f"00-{trace}-{trace[:16]}-01",
        }

        if data is not None:
            headers["x-ss-stub"] = generate_stub(data=data)

        headers.update(Ladon.encrypt(
            aid=aid,
            lc_id=lc_id,
            unix=unix,
        ))

        headers.update(Gorgon.encrypt(
            params=params,
            headers=headers,
            cookie=cookie,
            unix=unix,
        ))

        headers.update(Argus.encrypt(
            params=params,
            data=data,
            unix=unix,
            aid=aid,
            lc_id=lc_id,
            device_id=device_id,
            sdk_ver=sdk_ver,
            sdk_ver_code=sdk_ver_code,
            version_name=version_name,
            version_code=version_code,
            device_type=profile.device_type,
            os_version=profile.os_version,
            channel=profile.channel,
        ))

        if cookie is not None:
            headers["cookie"] = cookie

        return headers

    @classmethod
    def encrypt(cls, data: Union[str, bytes, Dict]) -> bytes:
        """Encrypt data using the TikTok TTEncrypt algorithm.

        The input is gzip-compressed (matching the Android app) before the
        TTEncrypt payload envelope is applied.

        Args:
            data: Data to encrypt (dict, str, or bytes).

        Returns:
            Encrypted bytes.
        """
        if isinstance(data, dict):
            data = json.dumps(data, separators=(",", ":")).encode()
        elif isinstance(data, str):
            data = data.encode()
        data = gzip.compress(data, compresslevel=9, mtime=0)
        return cls._get_encryptor().encrypt(data)

    @classmethod
    def decrypt(cls, data: bytes) -> str:
        """Decrypt TikTok TTEncrypt encrypted data.

        Args:
            data: Encrypted bytes to decrypt.

        Returns:
            Decrypted string.
        """
        return cls._get_encryptor().decrypt(data)

    @classmethod
    def encode(cls, data: Dict) -> bytes:
        """Encode a dict into protobuf bytes.

        Args:
            data: Request body with integer keys.

        Returns:
            Protobuf-encoded bytes.
        """
        return protobuf_encode(data=data)

    @classmethod
    def decode(cls, data: Union[bytes, ProtoBuf]) -> Dict:
        """Decode protobuf data into a dict.

        Args:
            data: Protobuf bytes or a :class:`ProtoBuf` instance.

        Returns:
            Dict with field numbers as keys.
        """
        return protobuf_decode(data=data)
