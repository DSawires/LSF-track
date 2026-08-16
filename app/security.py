from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

# scrypt ships with the standard library, so the deployment has one less wheel to
# build on a machine that may never see the internet.
_N = 2**14
_R = 8
_P = 1
_DKLEN = 32


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=_DKLEN)
    return "scrypt${}${}${}${}${}".format(
        _N, _R, _P, base64.b64encode(salt).decode(), base64.b64encode(dk).decode()
    )


# Ceilings for parameters parsed out of a stored hash. A corrupted or hostile
# row must not be able to make a login attempt allocate gigabytes (scrypt
# memory is roughly 128 * n * r bytes).
_MAX_N = 2**17
_MAX_R = 32
_MAX_P = 4


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, hash_b64 = stored.split("$")
        n_i, r_i, p_i = int(n), int(r), int(p)
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    if not (0 < n_i <= _MAX_N and 0 < r_i <= _MAX_R and 0 < p_i <= _MAX_P):
        return False
    dk = hashlib.scrypt(
        password.encode(),
        salt=salt,
        n=n_i,
        r=r_i,
        p=p_i,
        dklen=len(expected),
    )
    return hmac.compare_digest(dk, expected)
