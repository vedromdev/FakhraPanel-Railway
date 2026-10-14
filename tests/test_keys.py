"""Pure-python x25519 keygen (RFC 7748) — no cryptography libs, no binaries."""

import base64

from panel.xray import keys


def _b64d(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def test_rfc7748_vector():
    # RFC 7748 §6.1 DH test vector (X25519)
    priv = "77076d0a7318a57d3c16c17251b26645df4c2f87ebc0992ab177fba51db92c2a"
    pub = "8520f0098930a754748b7ddcb43ef75a0dbf3a0d26381af4eba4a98eaa9b4e6a"
    assert keys.public_from_private_hex(priv) == pub


def test_generate_keypair_shape_and_roundtrip():
    priv, pub = keys.generate_keypair()
    for value in (priv, pub):
        assert "=" not in value  # urlsafe base64 WITHOUT padding
        assert len(_b64d(value)) == 32
    # the public key recomputes from the private key's raw bytes
    priv_hex = _b64d(priv).hex()
    assert keys.public_from_private_hex(priv_hex) == _b64d(pub).hex()
    # b64-pair convenience (validation derives a missing public_key from it)
    assert keys.public_from_private(priv) == pub


def test_keypair_is_random():
    assert keys.generate_keypair() != keys.generate_keypair()
