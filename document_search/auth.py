from __future__ import annotations

import hashlib
import hmac
import os
from dataclasses import dataclass


@dataclass(slots=True)
class UserRecord:
    username: str
    password_hash: str
    salt: str


def hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 120_000).hex()


def verify_password(password: str, salt: str, expected_hash: str) -> bool:
    actual = hash_password(password, salt)
    return hmac.compare_digest(actual, expected_hash)


def new_salt() -> str:
    return os.urandom(16).hex()
