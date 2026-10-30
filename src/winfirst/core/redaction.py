"""Secret and identifier redaction helpers.

Used before writing command output to the audit log, the rotating log file,
reports, or diagnostic bundles.  Redaction is pattern based: it reduces the
chance of leaking credentials but must not be treated as a guarantee.
"""
from __future__ import annotations

import re
from typing import Iterable

_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"(?i)\b(authorization)\b(\s*[:=]\s*)(?:bearer|basic)\s+[^\s,;]+"
        ),
        r"\1\2***REDACTED***",
    ),
    (
        re.compile(
            r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|"
            r"client[_-]?secret|credential)\b(\s*[:=]\s*)([^\s,;]+)"
        ),
        r"\1\2***REDACTED***",
    ),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/\-]+=*"), "Bearer ***REDACTED***"),
    (
        re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)([^/\s:@]+):([^/\s@]+)@"),
        r"\1\2:***REDACTED***@",
    ),
    (
        re.compile(
            r"(?i)\b(ProductKey|ProductId|DigitalProductId|BackupProductKeyDefault)\b"
            r"(\s*[:=]\s*)([^\s,;]+)"
        ),
        r"\1\2***REDACTED***",
    ),
    (
        re.compile(r"(?i)(--password|--token|--api-key)(\s+)(\S+)"),
        r"\1\2***REDACTED***",
    ),
)

_SENSITIVE_ENV_NAMES = re.compile(
    r"(?i)(password|passwd|passphrase|secret|token|api[_-]?key|access[_-]?key|"
    r"private[_-]?key|credential|connection[_-]?string|"
    r"(?:^|[_-])(?:auth|session|cookie|sas)(?:[_-]|$))"
)

_MAC_RE = re.compile(r"\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b")
_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


def redact_secrets(text: str | None) -> str:
    if not text:
        return ""
    result = text
    for pattern, replacement in _SECRET_PATTERNS:
        result = pattern.sub(replacement, result)
    return result


def should_redact_env_name(name: str) -> bool:
    return bool(_SENSITIVE_ENV_NAMES.search(name))


def redact_identifiers(text: str, *, hostname: str = "", username: str = "") -> str:
    """Additional identifier redaction for exported support bundles."""
    if not text:
        return ""
    result = text
    if hostname and len(hostname) >= 3:
        result = re.sub(re.escape(hostname), "<HOSTNAME>", result, flags=re.IGNORECASE)
    if username and len(username) >= 2:
        result = re.sub(re.escape(username), "<USER>", result, flags=re.IGNORECASE)
    result = _MAC_RE.sub("<MAC>", result)
    result = _IPV4_RE.sub("<IP>", result)
    return result


def redact_mapping(rows: Iterable[tuple[str, str]]) -> dict[str, str]:
    """Redact values whose key looks sensitive (environment variables etc.)."""
    out: dict[str, str] = {}
    for key, value in rows:
        out[key] = "***REDACTED***" if should_redact_env_name(key) else redact_secrets(value)
    return out
