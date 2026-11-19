"""Borrowing a login from the user's Chrome, without attaching Playwright to it.

The user starts Chrome with ``--remote-debugging-port`` and logs in. To start
a session logged in, Knarr copies two things from that Chrome:

- the cookies Chrome would send to the start site, read with the browser-level
  DevTools call ``Storage.getCookies``, which no page can block;
- the site's localStorage, read from a tab the user has open on that exact
  origin, with a short time limit: while a tab shows a dialog its page answers
  no DevTools calls, so a busy tab is skipped and reported, never waited on.

Nothing else is copied: sessionStorage and IndexedDB stay behind, so a site
that keeps its login there starts logged out.

It talks to Chrome over a short raw DevTools connection instead of Playwright's
``connect_over_cdp``, so Playwright never attaches to the user's browser: it
can't dismiss dialogs in the user's tabs, or hang connecting while one is open.
The connection is direct: an ``HTTP_PROXY`` in the environment doesn't apply to
it.
"""

from __future__ import annotations

import inspect
import ipaddress
import json
import re
import socket
import time
import unicodedata
import urllib.request
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote, urlsplit

from knarr.explorer.origin import normalize_origin

TAB_TIMEOUT_SEC = 2.0
_SAME_SITE = {"Strict": "Strict", "Lax": "Lax", "None": "None"}
_DEFAULT_PORT = {"http": 80, "https": 443, "ws": 80, "wss": 443}

# localStorage through the Storage API, called on the prototype so a stored
# item named like a method ("key", "length", "getItem"...) can't shadow it.
_LOCAL_STORAGE_JS = """(() => {
  const s = window.localStorage, P = Storage.prototype;
  const length = Object.getOwnPropertyDescriptor(P, 'length').get.call(s);
  const items = [];
  for (let i = 0; i < length; i++) { const k = P.key.call(s, i); items.push([k, P.getItem.call(s, k)]); }
  return JSON.stringify({length: length, items: items});
})()"""

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Chrome's DevTools endpoint never redirects; anything that does is not it, and must not send Knarr elsewhere."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


# Direct connections: DevTools is local, and a proxy in the environment (a
# sandbox's, say) must not sit between Knarr and the user's Chrome.
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


class BorrowError(RuntimeError):
    """The user's Chrome couldn't be reached."""


class DevToolsEndpointError(BorrowError):
    """The URL points at the user's Chrome DevTools endpoint itself."""


def devtools_endpoint_error(url: str) -> DevToolsEndpointError:
    return DevToolsEndpointError(
        f"{url} is the DevTools endpoint of your Chrome (KNARR_CDP_URL): Knarr doesn't open "
        "it, because a page there could list, open and close your tabs"
    )


def _is_this_machine(host: str) -> bool:
    """Whether ``host`` is this machine: ``localhost`` or a loopback address.

    Stricter than ``_is_loopback``, which also matches for comparing hosts: not a
    name that merely resolves there (``a.localhost``, a DNS name), and not 0.0.0.0.
    """
    host = canonical_host(host).strip("[]")
    if host == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        try:  # spellings browsers accept for IPv4: 127.1, 2130706433, 0x7f.0.0.1
            ip = ipaddress.ip_address(socket.inet_aton(host))
        except OSError:
            return False
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_loopback


def _require_local(url: str, what: str) -> None:
    """Raise ``BorrowError`` unless ``url`` is on this machine.

    A Chrome debugging port gives whoever reaches it control of that Chrome and
    every login in it, and Knarr asks it for cookies. It is never sent over a
    network, and an address a reply names (a redirect, the WebSocket address in
    ``/json/version``) is held to the same rule as the one the user set.
    """
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        host = ""
    if not _is_this_machine(host):
        shown = host if len(host) <= 60 else host[:57] + "..."
        raise BorrowError(
            f"{what} must be on this machine (127.0.0.1, localhost or [::1]), not {shown!r}: "
            "anyone who can reach a Chrome debugging port controls that Chrome and every login in it"
        )


def _http_base(cdp_url: str) -> str:
    """The http(s) base of the DevTools endpoint, written either as
    ``http://host:9222`` or as a ``ws://host:9222/devtools/browser/<id>`` URL.
    It must be on this machine and carry no user name or password."""
    try:
        parts = urlsplit((cdp_url or "").strip())
        scheme = {"ws": "http", "wss": "https"}.get(parts.scheme.lower(), parts.scheme.lower())
        valid = scheme in ("http", "https") and bool(parts.netloc)
        credentials = parts.username is not None or parts.password is not None
    except ValueError:
        valid, credentials = False, False
    if not valid:
        raise BorrowError(f"KNARR_CDP_URL must look like http://127.0.0.1:9222 (got {cdp_url!r})")
    if credentials:
        raise BorrowError("KNARR_CDP_URL must not contain a user name or password")
    _require_local(cdp_url.strip(), "KNARR_CDP_URL")
    return f"{scheme}://{parts.netloc}"


def _get_json(base: str, path: str, timeout: float) -> Any:
    _require_local(base, "The DevTools address")
    try:
        with _DIRECT.open(base + path, timeout=timeout) as r:
            return json.load(r)
    except Exception as exc:
        raise BorrowError(f"could not reach Chrome at {base} ({type(exc).__name__}: {exc})") from exc


def _connect(ws_url: str, timeout: float) -> Any:
    from websockets.sync.client import connect

    _require_local(ws_url, "A DevTools WebSocket address")  # one that Chrome's reply named is held to the rule too
    kwargs: Dict[str, Any] = {"max_size": None, "open_timeout": timeout}
    if "proxy" in inspect.signature(connect).parameters:  # websockets 15+ reads the proxy environment
        kwargs["proxy"] = None
    return connect(ws_url, **kwargs)


def _call(ws_url: str, method: str, params: Dict[str, Any], timeout: float) -> Optional[Dict[str, Any]]:
    """One DevTools call over its own connection; None if no answer in time."""
    deadline = time.monotonic() + timeout
    with _connect(ws_url, timeout) as ws:
        ws.send(json.dumps({"id": 1, "method": method, "params": params}))
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            try:
                message = json.loads(ws.recv(timeout=left))
            except TimeoutError:
                return None
            if message.get("id") == 1:
                return message


# -- hosts -------------------------------------------------------------------------

_IPV4_LEGACY = re.compile(r"(?:0x[0-9a-f]+|[0-9]+)(?:\.(?:0x[0-9a-f]+|[0-9]+)){0,3}\Z")


def canonical_host(host: str) -> str:
    """The host as Chrome writes it in cookie domains and URLs: lowercase,
    percent-escapes decoded, no trailing dot, an IPv6 literal compressed and in
    brackets, an IPv4 address in dotted form (``2130706433`` and ``127.1`` too),
    an IDN as punycode the way UTS 46 does it (``straße`` keeps its ß; the
    ``idna`` codec of the standard library would turn it into ``strasse``)."""
    host = unquote((host or "").strip()).strip("[]")
    host = unicodedata.normalize("NFKC", host).lower()
    host = re.sub("[\u3002\uff0e\uff61]", ".", host).rstrip(".")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return f"[{ip.compressed}]" if ip.version == 6 else str(ip)
    if _IPV4_LEGACY.match(host):
        try:
            return str(ipaddress.IPv4Address(socket.inet_aton(host)))
        except (OSError, ValueError):
            return host
    labels = []
    for label in host.split("."):
        try:
            labels.append(label if label.isascii() else "xn--" + label.encode("punycode").decode("ascii"))
        except UnicodeError:
            labels.append(label)
    return ".".join(labels)


def _sent_to(cookie: Dict[str, Any], host: str) -> bool:
    """Whether a browser sends this cookie to ``host``: a domain cookie
    (".example.com") goes to that domain and its subdomains, a host-only
    cookie ("app.example.com") only to that exact host."""
    domain = str(cookie.get("domain", "")).lower()
    host = canonical_host(host)
    if domain.startswith("."):
        bare = domain[1:]
        return host == bare or host.endswith("." + bare)
    return host == canonical_host(domain)


def _is_loopback(host: str) -> bool:
    host = canonical_host(host).strip("[]")
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        try:  # spellings browsers accept for IPv4: 127.1, 2130706433, 0x7f.0.0.1
            ip = ipaddress.ip_address(socket.inet_aton(host))
        except OSError:
            return False
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_loopback or ip.is_unspecified


# ``http://[::1].:9222/``: recent Python releases refuse a bracketed host with
# anything after the ``]`` but a port, so the check would see no host at all.
_DOTS_AFTER_BRACKETS = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*://(?:[^@/?#]*@)?\[[^\]/?#]*\])\.+")


def _drop_dots_after_brackets(url: str) -> str:
    return _DOTS_AFTER_BRACKETS.sub(r"\1", url, count=1)


def is_devtools_endpoint(url: str, cdp_url: str) -> bool:
    """Whether ``url`` points at the DevTools endpoint named by ``cdp_url``:
    the same port on the same host, or on any loopback spelling when the
    endpoint is local (``localhost``, ``127.1``, ``2130706433``, ``[::1]``,
    ``0.0.0.0``). A session on that endpoint could list, close and open the
    user's tabs."""
    try:
        target, cdp = urlsplit(_drop_dots_after_brackets((url or "").strip())), urlsplit(_http_base(cdp_url))
        host, cdp_host = target.hostname, cdp.hostname
        port = target.port or _DEFAULT_PORT.get(target.scheme.lower())
        cdp_port = cdp.port or _DEFAULT_PORT[cdp.scheme]
    except (ValueError, BorrowError):
        return False
    if not host or not cdp_host or port != cdp_port:
        return False
    return (_is_loopback(host) and _is_loopback(cdp_host)) or canonical_host(host) == canonical_host(cdp_host)


def is_devtools_address(address: Optional[Dict[str, Any]], cdp_url: str) -> bool:
    """Same check for the address a page was actually served from (Playwright's
    ``Response.server_addr()``), which sees through DNS names for the endpoint."""
    if not address or not address.get("ipAddress") or not address.get("port"):
        return False
    ip = str(address["ipAddress"])
    return is_devtools_endpoint(f"http://{f'[{ip}]' if ':' in ip else ip}:{address['port']}/", cdp_url)


# -- cookies and storage -------------------------------------------------------------

def _in_first_party_partition(cookie: Dict[str, Any], origin: Optional[str]) -> bool:
    """Whether Chrome would send this cookie on a top-level visit to ``origin``.

    An unpartitioned cookie always qualifies. A partitioned (CHIPS) cookie
    qualifies only if its partition's top-level site is the start site with no
    cross-site ancestor; the same name set by an embedded frame under another
    site must not replace the first-party cookie."""
    key = cookie.get("partitionKey")
    if not key:
        return True
    if isinstance(key, dict):
        top, cross = key.get("topLevelSite"), bool(key.get("hasCrossSiteAncestor"))
    else:
        top, cross = key, False  # older Chrome: just the top-level site
    if cross or not isinstance(top, str):
        return False
    wanted = urlsplit(origin or "").hostname or ""
    site = urlsplit(top).hostname or top
    return canonical_host(site) == canonical_host(wanted) or canonical_host(wanted).endswith("." + canonical_host(site))


def _playwright_cookie(cookie: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "name": cookie["name"],
        "value": cookie.get("value", ""),
        "domain": cookie.get("domain", ""),
        "path": cookie.get("path", "/"),
        "expires": cookie.get("expires", -1) if not cookie.get("session") else -1,
        "httpOnly": bool(cookie.get("httpOnly")),
        "secure": bool(cookie.get("secure")),
        "sameSite": _SAME_SITE.get(cookie.get("sameSite", ""), "Lax"),
    }


def _read_local_storage(reply: Dict[str, Any]) -> Tuple[List[Dict[str, str]], str]:
    """(items, status) from a ``Runtime.evaluate`` reply of ``_LOCAL_STORAGE_JS``."""
    try:
        data = json.loads(reply["result"]["result"]["value"])
        items = [{"name": k, "value": v} for k, v in data["items"] if isinstance(k, str) and isinstance(v, str)]
        total = int(data["length"])
    except Exception:
        return [], "unreadable"  # e.g. storage blocked for that page
    if total == 0:
        return [], "empty"
    return items, "copied" if len(items) == total else "partial"


def borrow_login(cdp_url: str, url: str, *, tab_timeout: float = TAB_TIMEOUT_SEC) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """A Playwright storage state for ``url``, copied from the user's Chrome.

    Returns the state and a description for messages: the host, the number of
    cookies, and what happened with localStorage (``copied``, ``partial``,
    ``empty``, ``unreadable``, ``busy`` when the tab didn't answer in time, or
    ``no_tab``) with the number of items copied.
    """
    host = urlsplit(url).hostname or ""
    origin = normalize_origin(url)
    base = _http_base(cdp_url)
    version = _get_json(base, "/json/version", timeout=5)
    try:
        answer = _call(version["webSocketDebuggerUrl"], "Storage.getCookies", {}, timeout=5)
    except Exception as exc:
        raise BorrowError(f"could not read cookies from Chrome ({type(exc).__name__}: {exc})") from exc
    if answer is None or "result" not in answer:
        raise BorrowError("Chrome didn't return its cookies")
    cookies = [_playwright_cookie(c) for c in answer["result"].get("cookies", [])
               if _sent_to(c, host) and _in_first_party_partition(c, origin or url)]

    local: List[Dict[str, str]] = []
    status = "no_tab"
    tabs = [t for t in _get_json(base, "/json/list", timeout=5) if t.get("type") == "page"]
    for tab in tabs:
        if not origin or normalize_origin(tab.get("url", "")) != origin:
            continue
        try:
            reply = _call(tab["webSocketDebuggerUrl"], "Runtime.evaluate",
                          {"expression": _LOCAL_STORAGE_JS, "returnByValue": True}, timeout=tab_timeout)
        except Exception:
            reply = None
        if reply is None:
            status = "busy"  # e.g. a dialog is open in that tab
            continue
        local, status = _read_local_storage(reply)
        if status != "unreadable":
            break

    state = {"cookies": cookies,
             "origins": [{"origin": origin, "localStorage": local}] if local else []}
    return state, {"host": host, "origin": origin, "cookies": len(cookies),
                   "local_storage": status, "local_storage_items": len(local)}


# -- telling the user ------------------------------------------------------------------------------

_STORAGE_NOTES = {
    "copied": "and its localStorage",
    "partial": "and part of its localStorage",
    "empty": "(your open tab had no localStorage to copy)",
    "unreadable": "(no localStorage: the page wouldn't give it up)",
    "busy": "(no localStorage: your tab on it didn't answer, maybe a dialog is open)",
    "no_tab": "(no localStorage: no tab of yours was open on it)",
}
_STORAGE_REASONS = {
    "copied": "its localStorage was copied",
    "partial": "part of its localStorage was copied",
    "empty": "your open tab had no localStorage",
    "unreadable": "its page wouldn't give up its localStorage",
    "busy": "your tab on it didn't answer, maybe a dialog is open",
    "no_tab": "no tab of yours was open on it, and localStorage can only be read from an open tab",
}


def login_sentence(copies: Optional[List[Dict[str, Any]]]) -> str:
    """What was borrowed from the user's Chrome, origin by origin, in one sentence
    (empty when nothing was asked for). Says why, per origin, when nothing was
    borrowed at all."""
    if not copies:
        return ""
    cookies = sum(info.get("cookies", 0) for info in copies)
    storage = any(info.get("local_storage") in ("copied", "partial") for info in copies)
    if not cookies and not storage:
        reasons = "; ".join(
            f"{info.get('origin') or info.get('host')}: no cookies, and "
            f"{_STORAGE_REASONS.get(info.get('local_storage'), 'no localStorage was copied')}"
            for info in copies)
        return (f" Nothing was borrowed from your Chrome ({reasons}), so the session starts logged out "
                "(a login kept in sessionStorage or IndexedDB is never copied).")
    parts = [f"{info.get('cookies', 0)} cookie(s) for {info.get('origin') or info.get('host')} "
             f"{_STORAGE_NOTES.get(info.get('local_storage'), '')}".rstrip() for info in copies]
    return " Borrowed your login from your Chrome: " + "; ".join(parts) + "."
