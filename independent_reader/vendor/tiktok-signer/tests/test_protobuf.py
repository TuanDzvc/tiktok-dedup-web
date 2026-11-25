"""Protobuf varint encode/decode tests (covers the 0x80 boundary bug)."""
import pytest

from tiktok_signer.lib.utils.protobuf import ProtoBuf, ProtoError, protobuf_decode, protobuf_encode

VALUES = [0, 1, 127, 128, 129, 255, 256, 16383, 16384, 300, 2**32 - 1, 2**63 - 1]


@pytest.mark.parametrize("value", VALUES)
def test_varint_roundtrip(value):
    encoded = protobuf_encode({1: value})
    decoded = protobuf_decode(encoded)
    assert decoded[1] == value


def test_varint_128_boundary():
    encoded = protobuf_encode({1: 128})
    assert encoded == b"\x08\x80\x01"


def test_malformed_varint_raises():
    with pytest.raises(ProtoError):
        ProtoBuf(b"\x08" + b"\xff" * 20)
