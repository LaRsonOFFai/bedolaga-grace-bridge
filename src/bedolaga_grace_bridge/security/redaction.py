from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable

UUID_PATTERN = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}\b"
)
SECRET_ASSIGNMENT = re.compile(
    r"(?im)^(\s*(?:[A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|API_KEY)|DATABASE_DSN)\s*[=:]\s*)([^\s]+)"
)
BEARER = re.compile(r"(?i)\b(Bearer\s+)[A-Za-z0-9._~+/=-]+")
POSTGRES_PASSWORD = re.compile(r"(?i)(postgres(?:ql)?://[^:\s/]+:)([^@\s]+)(@)")


def fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def redact(text: str, explicit_secrets: Iterable[str] = (), *, hash_uuids: bool = True) -> str:
    result = text
    for secret in sorted({item for item in explicit_secrets if item}, key=len, reverse=True):
        result = result.replace(secret, "<REDACTED>")
    result = SECRET_ASSIGNMENT.sub(r"\1<REDACTED>", result)
    result = BEARER.sub(r"\1<REDACTED>", result)
    result = POSTGRES_PASSWORD.sub(r"\1<REDACTED>\3", result)
    if hash_uuids:
        result = UUID_PATTERN.sub(lambda match: f"uuid:{fingerprint(match.group(0).lower())}", result)
    return result
