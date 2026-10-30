"""Keep credentials that travel in URLs out of the files a run leaves behind.

A sign-in redirect carries ``?code=...`` or ``#access_token=...``, and a link
can carry ``user:password@``. The trace is a file on disk that outlives the
session, so URLs are written to it with those parts taken out. This is by
shape, like the write log: a parameter whose name says it is a credential, or
whose value looks like a random token, loses its value and keeps its name. A
short plain value (``type=RECURRENT``) is kept, because it is what makes the
trace readable. The model's own results are not changed; only the files are.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import unquote

REDACTED = "redacted"  # a plain word: a closing bracket would be taken for punctuation after the URL
USERINFO = "[userinfo]"

# Names that are credentials whatever the value looks like. A name that contains
# one of the words in SENSITIVE_WORDS counts too (``X-Amz-Security-Token``, ``sessionid``).
SENSITIVE_NAMES = frozenset({
    "code", "key", "apikey", "api_key", "api-key", "access_key", "secret_key", "sid", "sig", "otp", "jwt",
    "ticket", "bearer", "pin",
})
SENSITIVE_WORDS = (
    "token", "secret", "passw", "pwd", "auth", "session", "signature", "credential", "assertion", "saml",
    "cookie", "csrf", "xsrf", "nonce",
)

MIN_OPAQUE_CHARS = 20  # a value this long, made of token characters with digits in it, is treated as a secret
_TOKEN_CHARS = re.compile(r"^[A-Za-z0-9_\-.~+/=]+$")

# A URL inside any text: up to white space, a quote or a bracket. Linear: no nested repeats.
_URL = re.compile(r"https?://[^\s'\"<>`]+", re.IGNORECASE)
_TRAILING = ".,;:!?)]}"
MAX_STRING_CHARS = 200_000  # beyond this a string is left alone rather than scanned
MAX_DEPTH = 30  # deeper than any record is; a self-referencing value stops here and the writer drops it


def _is_sensitive_name(name: str) -> bool:
    lowered = unquote(name).strip().lower().strip("[]")
    return lowered in SENSITIVE_NAMES or any(word in lowered for word in SENSITIVE_WORDS)


def _looks_opaque(value: str) -> bool:
    decoded = unquote(value)
    if len(decoded) < MIN_OPAQUE_CHARS or not _TOKEN_CHARS.match(decoded):
        return False
    digits = sum(ch.isdigit() for ch in decoded)
    letters = sum(ch.isalpha() for ch in decoded)
    return digits >= 2 and letters >= 2


def _redact_params(text: str) -> str:
    """``a=1&token=xyz`` -> ``a=1&token=redacted``; a piece without ``=`` is judged as a value."""
    pieces = []
    for piece in text.split("&"):
        name, sep, value = piece.partition("=")
        if sep:
            pieces.append(f"{name}={REDACTED}" if value and (_is_sensitive_name(name) or _looks_opaque(value)) else piece)
        else:
            pieces.append(REDACTED if _looks_opaque(piece) else piece)
    return "&".join(pieces)


def redact_url(url: str) -> str:
    """One URL with its userinfo, its credential parameters and its credential-looking fragment taken out."""
    head, hash_, fragment = url.partition("#")
    base, question, query = head.partition("?")
    scheme, slashes, rest = base.partition("://")
    authority, slash, path = rest.partition("/")
    if "@" in authority:
        authority = f"{USERINFO}@{authority.rpartition('@')[2]}"
    out = f"{scheme}{slashes}{authority}{slash}{path}"
    if question:
        out += "?" + _redact_params(query)
    if hash_:
        out += "#" + (_redact_params(fragment) if "=" in fragment or _looks_opaque(fragment) else fragment)
    return out


def _redact_match(match: "re.Match[str]") -> str:
    found = match.group(0)
    stripped = found.rstrip(_TRAILING)
    return redact_url(stripped) + found[len(stripped):]


def redact_urls_in_text(text: str) -> str:
    """``text`` with every URL in it passed through ``redact_url``."""
    if not isinstance(text, str) or len(text) > MAX_STRING_CHARS or "://" not in text:
        return text
    return _URL.sub(_redact_match, text)


def redact(value: Any, _depth: int = 0) -> Any:
    """``value`` (strings, lists, dicts) with the URLs in every string redacted. Keys are left alone."""
    if isinstance(value, str):
        return redact_urls_in_text(value)
    if _depth >= MAX_DEPTH:
        return value
    if isinstance(value, dict):
        return {key: redact(item, _depth + 1) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item, _depth + 1) for item in value]
    return value
