"""Network write block: requests that could change data are faked, not sent.

GET, HEAD and OPTIONS pass through. Every other HTTP request in Theseus's
browser is answered with a fake success and recorded instead of reaching the
server, and outgoing WebSocket messages from pages are recorded and dropped.
A GET, HEAD or OPTIONS request that carries a body counts as a write. Clicks
still really happen, so the agent can explore; only what would have been sent
is held back.

Theseus always runs its own browser (``explorer.session``), so the block
intercepts at the browser level (CDP ``Fetch`` on the browser target), not
per page. That also catches requests the browser itself sends for a page,
such as the ``sendBeacon`` and ``fetch(keepalive)`` requests current Chrome
sends while a page unloads. Only the request types that can carry a write
are paused (documents, XHR, fetch, pings, CSP reports and "other"): images,
scripts, stylesheets, fonts and media are only ever fetched with GET, and
pausing them would slow every page down. A faked response to a cross-origin
request carries the CORS headers that let the page read it.

Channels whose traffic can't be intercepted are shut off: service workers are
blocked, SharedWorker and WebSocketStream are removed by browser flag in every
frame and worker, and WebTransport and RTCPeerConnection are disabled by a
script in each page frame.

Records hold no values, and are careful with names, but only by shape. A
record has the method, the URL's scheme, host and port (never credentials), the
path (a segment that doesn't have the shape of a route word, such as an ID,
token or code, is shown as ``:param``), the names of query parameters and body
fields (only names that look like field names; anything else is counted, not
shown), and the body's kind (from its Content-Type) and size. A shape rule is
a guess: a lowercase slug, a username or a key that is data can pass for a
route word or a field name. The user can name fields whose values should be
recorded with ``THESEUS_RECORD_FIELDS`` (comma-separated field names);
nothing else is ever recorded.

Policy. The MCP server blocks by default; the command-line runner doesn't
(blocking pauses a page's network while the model thinks there). The user
decides with ``THESEUS_BLOCK_WRITES=1`` or ``THESEUS_ALLOW_WRITES=1``; if both
are set, writes are blocked. ``THESEUS_WRITE_ALLOW`` lets named requests
through: comma-separated URL patterns. In a pattern, the part before any
``?`` is matched against the URL's scheme, host, port and path (``**`` matches
anything, ``*`` one path segment; at most two ``**``); a part after ``?`` is
matched against the query string (``*`` matches anything but ``&``). A
pattern matching a WebSocket URL lets that socket's messages through. Allowed
requests and messages are sent, and recorded as sent once the browser accepted
them to continue (a request the page cancels at the same moment may still be
counted, never the other way round).

Not covered:
- a GET that changes data (``/delete?id=1``);
- WebSocket messages sent from a dedicated Web Worker, or from a sandboxed
  frame (``<iframe sandbox="allow-scripts">``) that Chrome gives none of the
  page script; the block notes that a page started a worker, so a run can
  say so;
- WebTransport and RTCPeerConnection inside workers and sandboxed frames;
- the requests the browser itself makes for FedCM sign-in;
- work the server starts on its own.
"""

from __future__ import annotations

import base64
import html
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, IO, Iterable, List, Optional, Set, Tuple
from urllib.parse import unquote_plus, urlsplit

from theseus.explorer.borrow import is_devtools_endpoint

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

ALLOW_WRITES_ENV = "THESEUS_ALLOW_WRITES"
BLOCK_WRITES_ENV = "THESEUS_BLOCK_WRITES"
WRITE_ALLOW_ENV = "THESEUS_WRITE_ALLOW"
RECORD_FIELDS_ENV = "THESEUS_RECORD_FIELDS"
CAPTURED_WRITES_FILE = "captured_writes.jsonl"

MAX_FIELD_NAMES = 30
MAX_VALUE_CHARS = 200
MAX_MATCH_CHARS = 2048  # longer URLs (query included) never match an allow pattern
MAX_WILDCARDS = 2  # "*" or "**" runs in one part of an allow pattern; more backtrack badly
MAX_PARSE_BYTES = 1_000_000  # larger bodies are recorded by kind and size only
MAX_RECORDS = 5000  # records kept and listed; later writes are only counted
LARGE_BODY_BYTES = 5_000_000

BLOCKED_TITLE = "Blocked by Theseus"
BLOCKED_PAGE = """<!doctype html><title>{title}</title>
<h1>{title}</h1>
<p>This page submitted data ({method} {url}). Theseus blocks writes, so nothing was sent.</p>"""

# Request types that can carry a write. Everything else CDP knows (images,
# scripts, stylesheets, fonts, media, manifests, prefetches, preflights...) is
# fetched with GET or OPTIONS only. A new request type added to Chrome later
# isn't paused until it is listed here; a test checks the installed Playwright's
# list of types against these three sets.
WRITE_RESOURCE_TYPES = ("Document", "XHR", "Fetch", "Ping", "CSPViolationReport", "Other")
READ_ONLY_RESOURCE_TYPES = frozenset({
    "Stylesheet", "Image", "Media", "Font", "Script", "TextTrack", "Prefetch", "EventSource",
    "WebSocket", "Manifest", "SignedExchange", "Preflight",
})
# Sent by the browser itself for an API a page calls (FedCM sign-in posts an ID
# assertion request to the identity provider). Chrome's Fetch filter rejects
# these types, so they can't be paused by type: not covered.
UNFILTERABLE_RESOURCE_TYPES = frozenset({"FedCM"})

# Chromium flags for the browser Theseus launches. SharedWorker and
# WebSocketStream are switched off in every realm, workers and sandboxed frames
# included, which a page script can't do.
LAUNCH_ARGS = ("--disable-blink-features=SharedWorker,WebSocketStream",)

# Channels whose traffic the block can't intercept or attribute are disabled
# in every page frame. Runs before page scripts.
DISABLE_UNCOVERED_CHANNELS_SCRIPT = """(() => {
  for (const name of ['SharedWorker', 'WebSocketStream', 'WebTransport', 'RTCPeerConnection', 'webkitRTCPeerConnection']) {
    if (!(name in window)) continue;
    const blocked = function () {
      throw new DOMException(name + ' is disabled by Theseus: its traffic could not be blocked', 'SecurityError');
    };
    // An accessor, and configurable, so a polyfill's `window.RTCPeerConnection = ... || ...`, a global `let RTCPeerConnection`
    // or a defineProperty doesn't throw. The real constructor is gone from the window either way.
    try { Object.defineProperty(window, name, {get: () => blocked, set: () => {}, configurable: true}); } catch (e) {}
  }
})();"""


TRUE_WORDS = frozenset({"1", "true", "yes", "on", "y", "t", "enabled"})
FALSE_WORDS = frozenset({"", "0", "false", "no", "off", "n", "f", "disabled"})


def parse_switch(raw: Optional[str]) -> Optional[bool]:
    """True or False for a value that says on or off; None for anything else.

    An unset or empty value is off. A value nobody can read (``enable``, ``2``)
    is None, so each setting can choose the safe reading."""
    word = (raw or "").strip().lower()
    if word in TRUE_WORDS:
        return True
    if word in FALSE_WORDS:
        return False
    return None


def env_flag(name: str) -> bool:
    """True for an environment variable that says on (1, true, yes, on...)."""
    return parse_switch(os.environ.get(name)) is True


def _env_list(name: str) -> Tuple[str, ...]:
    return tuple(p.strip() for p in os.environ.get(name, "").split(",") if p.strip())


@dataclass(frozen=True)
class WritePolicy:
    block: bool = True
    allow_patterns: Tuple[str, ...] = ()
    record_fields: Tuple[str, ...] = ()

    @classmethod
    def from_env(cls, *, default_block: bool = True, extra_allow: Iterable[str] = (),
                 live: bool = False) -> "WritePolicy":
        """The policy the user's environment sets.

        ``extra_allow`` and ``live`` come from a site profile (``profiles``), which
        can loosen the policy for one site but never overrule the environment:
        ``THESEUS_BLOCK_WRITES`` blocks even a profile that says ``live``, and the
        profile's patterns come after the environment's.
        """
        if parse_switch(os.environ.get(BLOCK_WRITES_ENV)) is not False:
            block = True  # the safe choice wins a conflict; a value nobody can read counts as on
        elif env_flag(ALLOW_WRITES_ENV) or live:
            block = False
        else:
            block = default_block
        patterns = list(_env_list(WRITE_ALLOW_ENV))
        patterns.extend(p for p in extra_allow if p not in patterns)
        return cls(block=block, allow_patterns=tuple(patterns),
                   record_fields=_env_list(RECORD_FIELDS_ENV))


# -- describing requests -----------------------------------------------------------

def _parts(url: str) -> Optional[Tuple[str, str, str, str]]:
    """(scheme, host[:port], path, query) of a URL, or None if it has no host.

    The host is as Chrome writes it (lowercase, IPv6 literals bracketed) and
    credentials are dropped: ``user:pw@host`` never reaches a record, a message
    or an allow pattern.
    """
    try:
        parts = urlsplit(url)
        host, port = parts.hostname, parts.port
    except ValueError:
        return None
    if not parts.scheme or not host:
        return None
    authority = f"[{host}]" if ":" in host else host
    if port is not None:
        authority += f":{port}"
    return parts.scheme.lower(), authority, parts.path, parts.query


def _bare(url: str) -> Optional[str]:
    """scheme://host[:port]/path, without credentials, query or fragment."""
    found = _parts(url)
    return None if found is None else f"{found[0]}://{found[1]}{found[2]}"


# A path segment is shown as it is only if it has the shape of a route word:
# users, sign-in, getUser, UserService, v2, save.json. An ID, token, code or
# email has another shape and becomes :param.
_ROUTE_WORD = re.compile(
    r"^(?:[a-z][a-z_-]{0,23}"                     # users, sign-in
    r"|[a-z]{3,12}(?:[A-Z][a-z]{2,11}){1,3}"      # getUser
    r"|(?:[A-Z][a-z]{2,11}){1,4}"                 # UserService
    r"|v[0-9]{1,3}(?:(?:alpha|beta)[0-9]{0,2})?"  # v2, v1beta1
    r"|oauth2)"                                   # the one common word with a digit
    r"(?:\.(?:json|xml|html?|php|aspx?|jsp|do|action|cgi|js|css|txt|csv|ics|rss|atom))?\Z"
)


def path_template(path: str) -> str:
    """The path with every segment that isn't shaped like a route word (an ID,
    token, code, email or name with digits or odd characters) replaced by
    ``:param``."""
    return "/".join(s if not s or _ROUTE_WORD.match(s) else ":param" for s in path.split("/"))


def record_url(url: str) -> str:
    """The URL as recorded and reported: origin and templated path, no
    credentials or query."""
    found = _parts(url)
    if found is None:
        return "(unreadable URL)"
    return f"{found[0]}://{found[1]}{path_template(found[2])}"


# A field name is recorded only if it looks like one. Text that isn't (JSON
# posted with a form Content-Type, a token, an email, a number) could be data,
# so it is counted but never shown.
_FIELD_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.\[\]-]{0,39}\Z")
_UUID = re.compile(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\Z")
_HEX = re.compile(r"[0-9A-Fa-f]{16,}\Z")


def _is_field_name(name: Any) -> bool:
    """Best effort: a name has the shape of a field name and not of a token.

    A shape rule can't tell a field called ``jane`` from a user called ``jane``
    used as a key; it keeps out the common token shapes (UUIDs, hashes, base64
    with digits and both cases) and anything that isn't identifier-like.
    """
    if not isinstance(name, str) or not _FIELD_NAME.match(name):
        return False
    if _UUID.match(name) or _HEX.match(name):
        return False
    token_like = (len(name) >= 12 and any(c.isdigit() for c in name)
                  and any(c.islower() for c in name) and any(c.isupper() for c in name))
    return not token_like


def _field_names(names: Iterable[Any], listed: Tuple[str, ...] = ()) -> Tuple[List[str], int]:
    """(distinct names that look like field names, count of the others).

    A name the user listed in ``THESEUS_RECORD_FIELDS`` is shown whatever it
    looks like."""
    kept: Dict[str, None] = {}
    rejected = 0
    for name in names:
        if _is_field_name(name) or (isinstance(name, str) and name in listed):
            kept[name] = None
        else:
            rejected += 1
    return list(kept), rejected


def _pairs(text: str) -> Tuple[List[Tuple[str, str]], int]:
    """``name=value`` segments of a query or form body, and the count of
    segments without ``=`` (whose text could be a value, so it's not kept)."""
    pairs, unnamed = [], 0
    for segment in text.split("&"):
        if not segment:
            continue
        if "=" in segment:
            name, value = segment.split("=", 1)
            pairs.append((unquote_plus(name), unquote_plus(value)))
        else:
            unnamed += 1
    return pairs, unnamed


def _media_type(content_type: Optional[str]) -> str:
    return (content_type or "").split(";", 1)[0].strip().lower()


_BOUNDARY = re.compile(r'boundary\s*=\s*(?:"([^"]{1,200})"|([^;\s]{1,200}))', re.I)
_DISPOSITION = re.compile(rb'^content-disposition:[^\r\n]*', re.I | re.M)
_PART_TYPE = re.compile(rb'^content-type:\s*([^;\r\n]*)(?:;[^\r\n]*?charset\s*=\s*"?([A-Za-z0-9_.:-]+))?', re.I | re.M)
_PART_NAME = re.compile(rb'[;\s]name\s*=\s*"([^"\r\n]*)"', re.I)
_PART_FILENAME = re.compile(rb'[;\s]filename\*?\s*=', re.I)
MAX_PART_HEADER_BYTES = 8192


def _multipart_fields(body: bytes, content_type: Optional[str]) -> List[Tuple[Any, Optional[str]]]:
    """(name, text) of each part of a multipart body, in one pass with no backtracking.

    Names come from the parts' own headers; the text of a part with no filename
    and a text type (the default), decoded as UTF-8 unless the part names a
    charset Python knows; None for files and other types."""
    found = _BOUNDARY.search(content_type or "")
    if not found:
        return []
    delimiter = b"--" + (found.group(1) or found.group(2)).encode("latin-1", "replace")
    fields: List[Tuple[Any, Optional[str]]] = []
    for chunk in body.split(delimiter)[1:]:
        if chunk.startswith(b"--"):  # the closing delimiter
            break
        head, sep, content = chunk.partition(b"\r\n\r\n")
        if not sep:
            head, sep, content = chunk.partition(b"\n\n")
        head = head[:MAX_PART_HEADER_BYTES]
        disposition = _DISPOSITION.search(head)
        name_match = _PART_NAME.search(disposition.group(0)) if disposition else None
        name = name_match.group(1).decode("utf-8", "replace") if name_match else None
        if disposition and _PART_FILENAME.search(disposition.group(0)):
            fields.append((name, None))
            continue
        media = _PART_TYPE.search(head)
        kind = (media.group(1).strip().lower().decode("ascii", "replace") if media else "text/plain")
        if not kind.startswith("text/"):
            fields.append((name, None))
            continue
        charset = media.group(2).decode("ascii", "replace") if media and media.group(2) else "utf-8"
        raw = content[:-2] if content.endswith(b"\r\n") else content[:-1] if content.endswith(b"\n") else content
        try:
            fields.append((name, raw.decode(charset, "replace")))
        except LookupError:
            fields.append((name, raw.decode("utf-8", "replace")))
    return fields


def parse_fields(body: bytes, content_type: Optional[str]) -> Tuple[List[Tuple[Any, Optional[str]]], int]:
    """(name, value) pairs a real parser for the Content-Type finds, and the
    count of unnamed segments. Values are None where they aren't plain text
    (nested JSON, file parts). Unknown types yield nothing."""
    media = _media_type(content_type)
    if media == "application/json" or media.endswith("+json"):
        parsed = json.loads(body.decode("utf-8"))
        if not isinstance(parsed, dict):
            return [], 0
        return [(k, v if isinstance(v, str) else (json.dumps(v) if isinstance(v, (int, float, bool)) else None))
                for k, v in parsed.items()], 0
    if media == "application/x-www-form-urlencoded":
        pairs, unnamed = _pairs(body.decode("utf-8", "replace"))
        return [(k, v) for k, v in pairs], unnamed
    if media == "multipart/form-data":
        return _multipart_fields(body, content_type), 0
    return [], 0


def describe_body(body: Optional[bytes], content_type: Optional[str],
                  record_fields: Tuple[str, ...] = ()) -> Dict[str, Any]:
    """Kind, size and field names of a body, plus the values of fields the
    user named in ``record_fields``. Names come only from a real parser for
    the Content-Type, and only names that look like field names are shown;
    malformed or unknown bodies record kind and size only."""
    if body is None:
        return {"body_kind": "none", "body_bytes": 0}
    info: Dict[str, Any] = {"body_kind": _media_type(content_type) or "unknown", "body_bytes": len(body)}
    if len(body) > MAX_PARSE_BYTES:  # parsing hostile input costs more than it tells
        return info
    try:
        fields, unnamed = parse_fields(body, content_type)
    except Exception:  # malformed or hostile input
        fields, unnamed = [], 0
    names, rejected = _field_names((name for name, _ in fields), record_fields)
    if names:
        info["body_fields"] = names[:MAX_FIELD_NAMES]
        if len(names) > MAX_FIELD_NAMES:
            info["body_fields_more"] = len(names) - MAX_FIELD_NAMES
    if unnamed or rejected:
        info["unnamed_fields"] = unnamed + rejected
    values = {name: value[:MAX_VALUE_CHARS] for name, value in fields
              if isinstance(name, str) and name in record_fields and isinstance(value, str)}
    if values:
        info["values"] = values
    return info


def describe_message(message: Any) -> Dict[str, Any]:
    """A WebSocket message: kind and size; JSON object keys if it parses."""
    if not isinstance(message, str):
        return {"body_kind": "binary", "body_bytes": len(message) if isinstance(message, (bytes, bytearray)) else 0}
    data = message.encode("utf-8", "replace")
    try:
        parsed = json.loads(message)
    except Exception:
        return {"body_kind": "text", "body_bytes": len(data)}
    info: Dict[str, Any] = {"body_kind": "json", "body_bytes": len(data)}
    if isinstance(parsed, dict):
        names, rejected = _field_names(parsed)
        if names:
            info["body_fields"] = names[:MAX_FIELD_NAMES]
        if rejected:
            info["unnamed_fields"] = rejected
    return info


# -- allow patterns ------------------------------------------------------------------

def _compile_query_pattern(pattern: str) -> "re.Pattern[str]":
    # A run of stars is one star: "?**" and "?*" match the same strings.
    collapsed = re.sub(r"\*+", "*", pattern)
    return re.compile("^" + "".join("[^&]*" if c == "*" else re.escape(c) for c in collapsed) + "$")


_WILDCARD_RUN = re.compile(r"\*+")
_SCHEME_AND_AUTHORITY = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://([^/]*)(/.*)?$", re.S)


def _check_pattern(text: str, source: str = WRITE_ALLOW_ENV) -> None:
    """Refuse a pattern that can never match what the user meant, or that
    could take seconds to match. Raises ValueError with the reason; ``source``
    names where the pattern came from (the setting, or a site profile)."""
    def refuse(reason: str) -> None:
        raise ValueError(f"{source} pattern {text!r} {reason}")

    path_part, sep, query_part = text.partition("?")
    for label, part in (("before the '?'", path_part), ("after the '?'", query_part)):
        if len(_WILDCARD_RUN.findall(part)) > MAX_WILDCARDS:
            refuse(f"has more than {MAX_WILDCARDS} wildcards {label}")
    found = _SCHEME_AND_AUTHORITY.match(path_part)
    if not found and not path_part.startswith("*"):
        refuse("can't match any URL: start it with a scheme such as https:// or with '**'")
    if found:
        authority, path = found.group(1), found.group(2)
        if "**" in authority:
            refuse("has '**' in the host or port, where it could match across '/'; use '*'")
        if not path:
            refuse(f"has no path; write {path_part}/** for the whole site")


def validate_allow_pattern(text: str, source: str = WRITE_ALLOW_ENV) -> None:
    """Raise ValueError if ``text`` isn't a usable allow pattern; ``source`` is
    named in the message."""
    _check_pattern(text, source)
    AllowPattern(text)  # it must also compile


@dataclass(frozen=True)
class AllowPattern:
    """A ``THESEUS_WRITE_ALLOW`` entry, matched part by part."""

    text: str

    def __post_init__(self) -> None:
        from theseus.explorer.explorer_tools import compile_url_pattern

        _check_pattern(self.text)
        path_part, sep, query_part = self.text.partition("?")
        object.__setattr__(self, "_path", compile_url_pattern(path_part))
        object.__setattr__(self, "_query", _compile_query_pattern(query_part) if sep else None)

    def matches(self, url: str) -> bool:
        if not isinstance(url, str) or len(url) > MAX_MATCH_CHARS:  # the whole URL, query included
            return False
        found = _parts(url)
        if found is None:
            return False
        bare = f"{found[0]}://{found[1]}{found[2] or '/'}"
        if not self._path.match(bare):
            return False
        return self._query is None or bool(self._query.match(found[3]))


# -- the block ----------------------------------------------------------------------

@dataclass
class WriteBlock:
    """Fakes and records writes for one browser Theseus owns."""

    policy: WritePolicy = field(default_factory=WritePolicy)
    sink_path: Optional[Path] = None
    captured: List[Dict[str, Any]] = field(default_factory=list)
    # The DevTools endpoint of the user's Chrome (THESEUS_CDP_URL). When set, no
    # request from Theseus's browser reaches it, whatever the write policy.
    devtools_url: Optional[str] = None

    def __post_init__(self) -> None:
        self._allow = [AllowPattern(p) for p in self.policy.allow_patterns]
        self._counts = {"blocked": 0, "sent": 0, "aborted": 0, "refused": 0}
        self._overflowed = False
        self._gap_counts: Dict[str, int] = {}
        self._gaps_reported: Set[str] = set()
        self._blocked_navigations: Set[str] = set()  # bare URLs
        self._drained = 0
        self._lock = threading.Lock()
        self._cdp: Any = None
        self._sink: Optional[IO[str]] = None

    @property
    def active(self) -> bool:
        return self.policy.block

    @property
    def launch_args(self) -> Tuple[str, ...]:
        """Chromium flags the browser needs for the block to cover it."""
        return LAUNCH_ARGS if self.active else ()

    @property
    def blocked(self) -> List[Dict[str, Any]]:
        """The listed records of writes that were held back (see MAX_RECORDS)."""
        return [w for w in self.captured if w.get("outcome") == "blocked"]

    @property
    def allowed(self) -> List[Dict[str, Any]]:
        """The listed records of writes that went out: let through by an allow pattern."""
        return [w for w in self.captured if w.get("outcome") == "sent"]

    @property
    def gaps(self) -> List[str]:
        return [f"{gap} (x{n})" if n > 1 else gap for gap, n in self._gap_counts.items()]

    def summary(self) -> Dict[str, Any]:
        """The run-summary fields, shared by the MCP server and the runner.

        ``dry_run`` is true when writes were intercepted and none went out:
        no request that could change data was sent, as far as the block can
        see (``write_block_gaps`` lists what it can't). It is false after a
        request let through by ``THESEUS_WRITE_ALLOW``, and when nothing was
        intercepted, in which case the counts are ``None`` (not observed,
        not zero).
        """
        if not self.active:
            return {"dry_run": False, "blocked_writes": None, "allowed_writes": None,
                    "write_block_gaps": self.gaps}
        with self._lock:
            blocked, sent = self._counts["blocked"], self._counts["sent"]
        return {
            "dry_run": sent == 0,
            "blocked_writes": blocked,
            "allowed_writes": sent,
            "write_block_gaps": self.gaps,
        }

    def is_blocked_document(self, url: str, title: Any) -> bool:
        """Whether the page showing ``url`` is the "Blocked by Theseus" page of
        a faked form post; a later normal visit to the same URL isn't.
        ``title`` is called only for a URL that had a blocked post."""
        bare = _bare(url)
        return bare is not None and bare in self._blocked_navigations and title() == BLOCKED_TITLE

    # -- installation ------------------------------------------------------

    def install(self, browser: Any, context: Any) -> None:
        """Intercept every request in a browser Theseus launched, and route
        page WebSockets of its context. Call before opening pages.

        With writes allowed, only requests to the DevTools endpoint (if there
        is one) are intercepted."""
        if not self.active and not self.devtools_url:
            return
        self._cdp = browser.new_browser_cdp_session()
        self._cdp.on("Fetch.requestPaused", self.handle_paused)
        patterns: List[Dict[str, Any]] = [
            {"urlPattern": pattern, "requestStage": "Request"} for pattern in self._devtools_patterns()]
        if self.active:
            context.add_init_script(DISABLE_UNCOVERED_CHANNELS_SCRIPT)
            context.route_web_socket("**", self.handle_websocket)
            context.on("page", self._page_opened)
            context.on("response", self._response_seen)
            patterns += [{"urlPattern": "*", "resourceType": t, "requestStage": "Request"}
                         for t in WRITE_RESOURCE_TYPES]
        try:
            self._cdp.send("Fetch.enable", {"patterns": patterns})
        except Exception:
            # A browser that rejects the type filter: pause everything.
            self._cdp.send("Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]})

    def _devtools_patterns(self) -> List[str]:
        """URL patterns, for every request type, of the usual spellings of the DevTools endpoint."""
        if not self.devtools_url:
            return []
        try:
            port = urlsplit(self.devtools_url.replace("ws://", "http://", 1).replace("wss://", "https://", 1)).port
        except ValueError:
            return []
        if port is None:
            return []
        hosts = ("127.0.0.1", "localhost", "localhost.", "*.localhost", "*.localhost.", "[::1]", "0.0.0.0")
        return [f"{scheme}://{host}:{port}/*" for host in hosts for scheme in ("http", "https", "ws", "wss")]

    # -- HTTP --------------------------------------------------------------

    def _allowed_by(self, url: str) -> Optional[str]:
        for pattern in self._allow:
            if pattern.matches(url):
                return pattern.text
        return None

    def _describe(self, method: str, url: str) -> Dict[str, Any]:
        entry: Dict[str, Any] = {"method": method, "url": record_url(url)}
        found = _parts(url)
        pairs, unnamed = _pairs(found[3]) if found else ([], 0)
        names, rejected = _field_names((k for k, _ in pairs), self.policy.record_fields)
        if names:
            entry["query_keys"] = names[:MAX_FIELD_NAMES]
            if len(names) > MAX_FIELD_NAMES:
                entry["query_keys_more"] = len(names) - MAX_FIELD_NAMES
        values = {k: v[:MAX_VALUE_CHARS] for k, v in pairs if k in self.policy.record_fields}
        if values:
            entry["query_values"] = values
        if unnamed or rejected:
            entry["query_unnamed"] = unnamed + rejected
        return entry

    @staticmethod
    def _body_of(request: Dict[str, Any]) -> Optional[bytes]:
        entries = request.get("postDataEntries")
        if entries:
            return b"".join(base64.b64decode(e.get("bytes", "")) for e in entries)
        data = request.get("postData")
        return data.encode("utf-8", "replace") if isinstance(data, str) else None

    @staticmethod
    def _has_body(request: Dict[str, Any]) -> bool:
        return bool(request.get("hasPostData") or request.get("postData") or request.get("postDataEntries"))

    def _entry(self, method: str, url: str, request: Dict[str, Any], headers: Dict[str, Any],
               event: Dict[str, Any]) -> Dict[str, Any]:
        """The record for a write. Never raises: if describing it fails, the
        entry says so."""
        try:
            entry = self._describe(method, url)
            body = self._body_of(request)
            entry.update(describe_body(body, headers.get("content-type"), self.policy.record_fields))
            if body is not None and len(body) > LARGE_BODY_BYTES:
                self._note_gap(f"a request body over {LARGE_BODY_BYTES // 1_000_000} MB was held back; "
                               "large bodies make Theseus slow and use a lot of memory")
        except Exception:
            entry = {"method": method, "url": record_url(url), "body_kind": "unreadable"}
        entry.update({"resource_type": str(event.get("resourceType", "")).lower(),
                      "navigation": event.get("resourceType") == "Document"})
        return entry

    def handle_paused(self, event: Dict[str, Any]) -> None:
        """``Fetch.requestPaused``: continue reads, fake and record writes.

        Never raises, and always resolves the request. A write is faked
        whenever anything goes wrong; one is counted as sent only after the
        browser accepted the request to continue, and recording never changes
        how a request is resolved.
        """
        request_id: Any = None
        read = False
        origin = None
        try:
            request_id = event.get("requestId")
            request = event.get("request")
            request = request if isinstance(request, dict) else {}
            method = request.get("method")
            url = str(request.get("url", ""))
            if self.devtools_url and is_devtools_endpoint(url, self.devtools_url):
                self._refuse_devtools(request_id, method, url)
                return
            if not self.active:  # only the DevTools patterns were requested
                self._cdp.send("Fetch.continueRequest", {"requestId": request_id})
                return
            # Only a bare GET, HEAD or OPTIONS is a read; a missing or odd
            # method, or a body on any method, is a write.
            read = isinstance(method, str) and method in SAFE_METHODS and not self._has_body(request)
            if read:
                self._cdp.send("Fetch.continueRequest", {"requestId": request_id})
                return
            method = method if isinstance(method, str) and method else "(unknown method)"
            headers = {str(k).lower(): v for k, v in (request.get("headers") or {}).items()}
            origin = headers.get("origin")
            allowed_by = self._allowed_by(url)
            entry = self._entry(method, url, request, headers, event)
            if allowed_by:
                entry.update({"allowed": True, "allowed_by": allowed_by})
                try:
                    self._cdp.send("Fetch.continueRequest", {"requestId": request_id})
                except Exception:
                    # The browser no longer had the request (the page cancelled
                    # it first): nothing was sent.
                    entry.update({"allowed": False, "outcome": "aborted"})
                else:
                    entry["outcome"] = "sent"
                self._record(entry)
                return
            entry["outcome"] = "blocked"
            try:
                if entry["navigation"]:
                    bare = _bare(url)
                    if bare is not None:
                        self._blocked_navigations.add(bare)
                    page = BLOCKED_PAGE.format(title=BLOCKED_TITLE, method=html.escape(method),
                                               url=html.escape(record_url(url)))
                    self._fulfill(request_id, page.encode(), "text/html", origin)
                else:
                    self._fulfill(request_id, b"{}", "application/json", origin)
            finally:
                self._record(entry)  # held back, even if the page cancelled it
        except Exception:
            # Fail closed: whatever went wrong, a write isn't sent.
            try:
                if read:
                    self._cdp.send("Fetch.continueRequest", {"requestId": request_id})
                else:
                    self._fulfill(request_id, b"{}", "application/json", origin)
            except Exception:
                pass

    def _refuse_devtools(self, request_id: Any, method: Any, url: str) -> None:
        """Answer a request to the user's DevTools endpoint with a refusal, whatever its type."""
        entry = {"method": method if isinstance(method, str) else "(unknown method)",
                 "url": record_url(url), "resource_type": "devtools", "navigation": False,
                 "outcome": "refused"}
        try:
            self._cdp.send("Fetch.fulfillRequest", {
                "requestId": request_id, "responseCode": 403,
                "responseHeaders": [{"name": "Content-Type", "value": "text/plain"}],
                "body": base64.b64encode(b"Blocked by Theseus: this is your Chrome's DevTools endpoint.").decode(),
            })
        finally:
            self._record(entry)
            self._note_gap("a page tried to reach your Chrome's DevTools endpoint; Theseus refused the request")

    def _fulfill(self, request_id: Any, body: bytes, content_type: str, origin: Optional[str]) -> None:
        headers = [{"name": "Content-Type", "value": content_type}]
        if origin:
            # A cross-origin fetch only sees a faked success if the fake allows
            # its origin (the literal "null" of a sandboxed frame included);
            # credentialed requests also need Allow-Credentials.
            headers += [{"name": "Access-Control-Allow-Origin", "value": origin},
                        {"name": "Access-Control-Allow-Credentials", "value": "true"},
                        {"name": "Vary", "value": "Origin"}]
        self._cdp.send("Fetch.fulfillRequest", {
            "requestId": request_id,
            "responseCode": 200,
            "responseHeaders": headers,
            "body": base64.b64encode(body).decode(),
        })

    # -- WebSockets --------------------------------------------------------

    def handle_websocket(self, ws: Any) -> None:
        server = ws.connect_to_server()  # server -> page messages still arrive
        allowed_by = self._allowed_by(ws.url)

        def page_sent(message: Any) -> None:
            try:
                entry = {"method": "WEBSOCKET_SEND", "url": record_url(ws.url), **describe_message(message)}
            except Exception:
                entry = {"method": "WEBSOCKET_SEND", "url": record_url(ws.url)}
            entry.update({"resource_type": "websocket", "navigation": False, "outcome": "blocked"})
            if allowed_by:
                entry.update({"allowed": True, "allowed_by": allowed_by, "outcome": "sent"})
                try:
                    server.send(message)  # allowed: forwarded, and counted once it went
                except Exception:
                    entry.update({"allowed": False, "outcome": "aborted"})
            self._record(entry)  # not allowed: dropped

        def page_closed(code: Any = None, reason: Any = None) -> None:
            # The page chooses a close code and reason; they go to the server only if the socket is allowed.
            try:
                if allowed_by:
                    server.close(code=code, reason=reason)
                else:
                    server.close()
            except Exception:
                pass

        ws.on_message(page_sent)
        ws.on_close(page_closed)

    # -- gaps and records --------------------------------------------------

    def _page_opened(self, page: Any) -> None:
        page.on("worker", self._worker_started)
        page.on("frameattached", self._frame_attached)

    def _response_seen(self, response: Any) -> None:
        """Note a site that asks Chrome to send it reports (Report-To, Reporting-Endpoints, NEL):
        those uploads are POSTs the browser's network service sends itself."""
        try:
            headers = response.headers
            asked = any(name in headers for name in ("report-to", "reporting-endpoints", "nel"))
        except Exception:
            return
        if asked:
            self._note_gap("a site asked Chrome to send it reports (Report-To, Reporting-Endpoints or NEL); "
                           "the browser sends those uploads itself, so Theseus can't hold them back")

    def _worker_started(self, worker: Any) -> None:
        self._note_gap("a page started a Web Worker; WebSocket and WebTransport messages it sends are not blocked")

    def _frame_attached(self, frame: Any) -> None:
        """Note a frame that Chrome may give none of Theseus's page script: a
        sandboxed ``srcdoc`` frame without ``allow-same-origin``."""
        try:
            element = frame.frame_element()
            sandbox, srcdoc = element.get_attribute("sandbox"), element.get_attribute("srcdoc")
        except Exception:
            return  # detached already, or in another process: nothing to tell
        if sandbox is not None and srcdoc is not None and "allow-same-origin" not in sandbox.split():
            self._note_gap("a page embedded a sandboxed frame (srcdoc without allow-same-origin) that Chrome "
                           "may give none of Theseus's page script; WebSocket and WebTransport messages "
                           "it sends may not be blocked")

    def _note_gap(self, gap: str) -> None:
        with self._lock:
            self._gap_counts[gap] = self._gap_counts.get(gap, 0) + 1

    def _record(self, entry: Dict[str, Any]) -> None:
        """Count a write, keep its record and append it to the sink. Never raises.

        Only the first MAX_RECORDS records are kept and listed; later writes
        are still counted, so a page that floods requests can't grow memory
        or the records file without bound."""
        entry = {"ts": round(time.time(), 3), **entry}
        with self._lock:
            outcome = entry.get("outcome")
            if outcome in self._counts:
                self._counts[outcome] += 1
            if len(self.captured) >= MAX_RECORDS:
                if not self._overflowed:
                    self._overflowed = True
                    self._gap_counts[f"more than {MAX_RECORDS} writes: later ones are counted but not listed"] = 1
                return
            self.captured.append(entry)
            if self.sink_path is None:
                return
            try:
                if self._sink is None:
                    self.sink_path.parent.mkdir(parents=True, exist_ok=True)
                    self._sink = self.sink_path.open("a", encoding="utf-8")
                # ASCII-only JSON: no text, a lone surrogate included, can fail to encode.
                self._sink.write(json.dumps(entry) + "\n")
                self._sink.flush()
            except Exception:
                pass

    def close(self) -> None:
        with self._lock:
            if self._sink is not None:
                try:
                    self._sink.close()
                except Exception:
                    pass
                self._sink = None

    def drain(self) -> List[Dict[str, Any]]:
        """Writes captured (blocked, sent or cancelled) since the last drain."""
        with self._lock:
            new = self.captured[self._drained:]
            self._drained = len(self.captured)
        return new

    def drain_gaps(self) -> List[str]:
        """Kinds of gap seen for the first time since the last drain."""
        with self._lock:
            new = [g for g in self._gap_counts if g not in self._gaps_reported]
            self._gaps_reported.update(new)
        return new
