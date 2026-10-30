"""URL and origin normalization shared by the runner and the MCP server.

Origins compare equal the way browsers compare them: lowercase scheme and
host, no default port, no credentials. Only http and https have origins here;
anything else (``file:``, ``data:``, ``about:``) has none.
"""

from __future__ import annotations

from typing import Optional
from urllib.parse import urljoin, urlsplit, urlunsplit

DEFAULT_PORTS = {"http": 80, "https": 443}


def _host_and_port(url: str):
    """``(scheme, host, port)`` for an http(s) URL, else ``None``. Port is
    ``None`` when absent or the scheme's default."""
    try:
        parts = urlsplit(url.strip())
        scheme = parts.scheme.lower()
        host = parts.hostname  # lowercased, credentials dropped
        port = parts.port
    except (ValueError, AttributeError):  # bad port, malformed IPv6 literal, non-str
        return None
    if scheme not in DEFAULT_PORTS or not host:
        return None
    # Whitespace or control characters can't be in a real host, and port 0
    # can't be connected to.
    if any(ch.isspace() or ord(ch) < 33 for ch in host) or port == 0:
        return None
    if port == DEFAULT_PORTS[scheme]:
        port = None
    if ":" in host:  # IPv6 literal
        host = f"[{host}]"
    return scheme, host, port


def normalize_origin(url: str) -> Optional[str]:
    """``scheme://host[:port]`` for an http(s) URL, or ``None``."""
    found = _host_and_port(url or "")
    if found is None:
        return None
    scheme, host, port = found
    return f"{scheme}://{host}" + (f":{port}" if port is not None else "")


def normalize_url(url: str, base: str = "") -> str:
    """Resolve ``url`` against ``base`` and normalize its origin part.

    Relative URLs (``/orders``, ``?tab=2``) resolve against ``base``. An
    http(s) result gets a lowercase scheme and host and loses a default port
    and credentials; the path, query and fragment are kept. Anything else is
    returned resolved but otherwise unchanged.
    """
    url = (url or "").strip()
    resolved = urljoin(base, url) if base else url
    origin = normalize_origin(resolved)
    if origin is None:
        return resolved
    parts = urlsplit(resolved)
    path = parts.path or "/"
    return origin + urlunsplit(("", "", path, parts.query, parts.fragment))
