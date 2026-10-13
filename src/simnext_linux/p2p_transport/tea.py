"""TEA block operations for the legacy wire format.

These implement the standard 32-round TEA algorithm with little endian wire
words. This legacy cipher is a compatibility mechanism, not a recommendation
for securing new protocols.
"""

import struct

_MASK = 0xFFFFFFFF
_DELTA = 0x9E3779B9
_WORDS = struct.Struct("<2I")
_KEY = struct.Struct("<4I")


def encrypt_block(block: bytes, key: bytes) -> bytes:
    left, right = _WORDS.unpack(block)
    a, b, c, d = _KEY.unpack(key)
    total = 0
    for _ in range(32):
        total = (total + _DELTA) & _MASK
        left = (
            left + (((right << 4) + a) ^ (right + total) ^ ((right >> 5) + b))
        ) & _MASK
        right = (
            right + (((left << 4) + c) ^ (left + total) ^ ((left >> 5) + d))
        ) & _MASK
    return _WORDS.pack(left, right)


def decrypt_block(block: bytes, key: bytes) -> bytes:
    left, right = _WORDS.unpack(block)
    a, b, c, d = _KEY.unpack(key)
    total = (_DELTA * 32) & _MASK
    for _ in range(32):
        right = (
            right - (((left << 4) + c) ^ (left + total) ^ ((left >> 5) + d))
        ) & _MASK
        left = (
            left - (((right << 4) + a) ^ (right + total) ^ ((right >> 5) + b))
        ) & _MASK
        total = (total - _DELTA) & _MASK
    return _WORDS.pack(left, right)


def encrypt(data: bytes, key: bytes) -> bytes:
    """Encrypt zero-padded blocks; callers transmit the original length separately."""
    if len(key) != 16:
        raise ValueError("TEA requires a 16-byte key")
    padded = data + b"\0" * (-len(data) % 8)
    return b"".join(
        encrypt_block(padded[i : i + 8], key) for i in range(0, len(padded), 8)
    )


def decrypt(data: bytes, key: bytes) -> bytes:
    """Decrypt complete blocks without guessing the application's padding length."""
    if len(key) != 16 or len(data) % 8:
        raise ValueError("Invalid TEA key or ciphertext length")
    return b"".join(decrypt_block(data[i : i + 8], key) for i in range(0, len(data), 8))
