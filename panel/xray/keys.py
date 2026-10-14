"""Pure-python x25519 (RFC 7748) for REALITY keypairs.

Stdlib-only: the VPS install must not depend on a cryptography wheel or an
`xray x25519` binary — keys are generated at inbound-create time and the
REALITY seed keys at migration time, so keygen has to work wherever the
panel code works.

Encoding: xray REALITY strings are urlsafe base64 WITHOUT padding of the
32 raw bytes (private = random seed, clamped on use; public = seed * 9).
"""

import base64
import secrets

_P = 2**255 - 19
_A24 = 121665


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _x25519(clamped_scalar: int, u: int) -> int:
    """Montgomery ladder (RFC 7748 §5) — scalar * u over Curve25519."""
    x2, z2, x3, z3, swap = 1, 0, u, 1, 0
    for t in reversed(range(255)):
        k_t = (clamped_scalar >> t) & 1
        swap ^= k_t
        if swap:
            x2, x3 = x3, x2
            z2, z3 = z3, z2
        swap = k_t
        a = (x2 + z2) % _P
        aa = a * a % _P
        b = (x2 - z2) % _P
        bb = b * b % _P
        e = (aa - bb) % _P
        c = (x3 + z3) % _P
        d = (x3 - z3) % _P
        da = d * a % _P
        cb = c * b % _P
        x3 = (da + cb) ** 2 % _P
        z3 = u * (da - cb) ** 2 % _P
        x2 = aa * bb % _P
        z2 = e * (aa + _A24 * e) % _P
    if swap:
        x2, x3 = x3, x2
        z2, z3 = z3, z2
    return x2 * pow(z2, _P - 2, _P) % _P


def public_from_private_hex(private_hex: str) -> str:
    """Public key (hex) for a private seed (hex), per RFC 7748 §5 clamping."""
    k = bytearray(bytes.fromhex(private_hex))
    k[0] &= 248
    k[31] &= 127
    k[31] |= 64
    return _x25519(int.from_bytes(k, "little"), 9).to_bytes(32, "little").hex()


def generate_keypair() -> tuple[str, str]:
    """(private_key, public_key) as xray REALITY strings."""
    private = secrets.token_bytes(32)
    public = bytes.fromhex(public_from_private_hex(private.hex()))
    return _encode(private), _encode(public)


def public_from_private(private: str) -> str:
    """Public REALITY string for a private REALITY string (b64url pair)."""
    return _encode(bytes.fromhex(public_from_private_hex(_decode(private).hex())))
