"""End-to-end: the write block, through the MCP server, in a real browser.

A local site offers every write path (fetch, XHR, sendBeacon, a form post, a
cross-origin iframe, a WebSocket send, a service worker that forwards a
request, popups). The test server records every request that reaches it, so
"blocked" means the server never saw it. Opt-in: KNARR_BROWSER_TESTS=1.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from tests.integration.test_mcp_stdio import (  # noqa: F401  (fixture import)
    CHROME,
    _client,
    _server_env,
    _text,
    pytestmark,
    throwaway_chrome,
)

CRITERION = "the page shows 'all done'"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Site:
    """Two origins and a WebSocket server, recording what reaches them."""

    def __init__(self) -> None:
        self.hits = []
        self.ws_messages = []
        self.ws_closes = []        # (path, code, reason) as the server saw each socket end
        self.extra_pages = {}      # a test can serve more pages
        self.extra_headers = {}    # path -> {header: value}
        self.port_a, self.port_b, self.port_ws = _free_port(), _free_port(), _free_port()
        self.a = f"http://127.0.0.1:{self.port_a}"
        # "localhost" and "127.0.0.1" are different sites, so the iframe is cross-site.
        self.b = f"http://localhost:{self.port_b}"
        self._ws_server = None
        self._servers = [ThreadingHTTPServer(("127.0.0.1", port), self._handler(port))
                         for port in (self.port_a, self.port_b)]
        for server in self._servers:
            threading.Thread(target=server.serve_forever, daemon=True).start()
        threading.Thread(target=self._serve_ws, daemon=True).start()
        time.sleep(0.3)

    def writes(self):
        return [(port, method, path) for port, method, path in self.hits if method != "GET"]

    def close(self) -> None:
        for server in self._servers:
            server.shutdown()
        if self._ws_server is not None:
            self._ws_server.shutdown()

    def _serve_ws(self) -> None:
        import websockets.sync.server as wss

        def record(conn):
            try:
                for message in conn:
                    self.ws_messages.append(message)
            finally:
                self.ws_closes.append((conn.request.path, getattr(conn, "close_code", None),
                                       getattr(conn, "close_reason", None)))

        with wss.serve(record, "127.0.0.1", self.port_ws) as server:
            self._ws_server = server
            server.serve_forever()

    def _pages(self):
        main = f"""<!doctype html><title>Writes</title><body><h1>Write paths</h1>
<iframe src="{self.b}/frame" title="partner frame"></iframe>
<button onclick="writes()">Run writes</button>
<form method=post action="/api/form"><input name=password value=hunter2 aria-label=password>
<button>Send form</button></form>
<form method=post action="/api/form-popup" target="_blank"><button>Form in new tab</button></form>
<form method=post action="/api/formtarget-popup"><button formtarget="_blank">Button new tab</button></form>
<a href="/popup" target="_blank">Popup link</a>
<button onclick="window.open('/popup')">Open window</button>
<button onclick="const a = document.createElement('a'); a.href = '/popup'; a.target = '_blank'; a.click()">Detached link</button>
<x-host id=host></x-host>
<button onclick="document.querySelector('#host').shadowRoot.querySelector('a').click()">Shadow link</button>
<a href="/base">Base target page</a>
<button onclick="startWorker()">Start worker</button>
<button onclick="try {{ new SharedWorker('/shared.js'); document.getElementById('sw').textContent = 'shared worker started'; }} catch (e) {{ document.getElementById('sw').textContent = 'shared worker refused: ' + e.name; }}">Start shared worker</button>
<h3 id=sw>no shared worker</h3>
<form method=post action="/api/plain" enctype="text/plain"><input name=password value=hunter2 aria-label="plain password"><button>Send plain form</button></form>
<h4 id=tok>token: unknown</h4>
<h4 id=ls>ls items: unknown</h4>
<a href="/unload">Unload page</a>
<button onclick="window.open('/selfclose')">Self-closing popup</button>
<form method=post action="/logout"><button>Log out</button></form>
<button onclick="fetch('/api/xml', {{method: 'POST', headers: {{'Content-Type': 'application/xml'}}, body: '<login><password>hunter2</password></login>'}})">Send XML</button>
<button onclick="const f = new FormData(); f.append('doc', new Blob(['x']), 'passport_john_doe.pdf'); f.append('note', 'see name=\\'secret-anchor\\''); fetch('/api/upload', {{method: 'POST', body: f}})">Upload</button>
<button onclick="const s = new WebSocket('ws://127.0.0.1:{self.port_ws}/live'); s.onopen = () => s.send('live-hello');">Open live socket</button>
<button onclick="fetch('{self.b}/api/xo', {{method: 'POST', credentials: 'include', headers: {{'Content-Type': 'application/json'}}, body: '{{}}'}}).then(r => r.json()).then(() => {{ document.getElementById('xo').textContent = 'cross-origin save: ok'; }}).catch(e => {{ document.getElementById('xo').textContent = 'cross-origin save failed: ' + e; }})">Cross-origin save</button>
<h5 id=xo>no cross-origin save</h5>
<h2 id=data>loading</h2>
<p id=s>idle</p>
<script>
const S = Storage.prototype;  // items named like Storage methods shadow localStorage.getItem
document.getElementById('tok').textContent = 'token: ' + (S.getItem.call(localStorage, 'authToken') ? 'present' : 'missing');
document.getElementById('ls').textContent = 'ls items: ' + Object.getOwnPropertyDescriptor(S, 'length').get.call(localStorage);
document.getElementById('host').attachShadow({{mode: 'open'}}).innerHTML = '<a href="/popup" target="_blank">in shadow</a>';
setTimeout(() => fetch('/api/data').then(() => {{ document.getElementById('data').textContent = 'data loaded'; }}), 1500);
function startWorker() {{
  new Worker(URL.createObjectURL(new Blob([
    "const ws = new WebSocket('ws://127.0.0.1:{self.port_ws}/'); ws.onopen = () => ws.send('worker-ws');"])));
}}
async function writes() {{
  await fetch('/api/fetch-post', {{method: 'POST', headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{qty: 1, token: 'secret-token'}})}}).catch(() => {{}});
  await new Promise(r => {{ const x = new XMLHttpRequest(); x.open('PUT', '/api/xhr-put'); x.onloadend = r; x.send('x=1'); }});
  navigator.sendBeacon('/api/beacon', 'b=1');
  try {{ const ws = new WebSocket('ws://127.0.0.1:{self.port_ws}/'); ws.onopen = () => ws.send('ws-write'); }} catch (e) {{}}
  await fetch('/api/sw-save', {{method: 'POST', body: 'sw=1'}}).catch(() => {{}});
  await fetch('/api/read').catch(() => {{}});
  document.getElementById('s').textContent = 'all done';
}}
</script></body>"""
        return {
            "/": main,
            "/frame": "<script>fetch('/api/iframe-post', {method: 'POST', body: 'i=1'}).catch(() => {});</script>frame",
            "/popup": "<script>fetch('/api/popup-post', {method: 'POST', body: 'p=1'}).catch(() => {});</script>popup",
            "/shared.js": "fetch('/api/shared-post', {method: 'POST', body: 's=1'});",
            "/unload": "<script>addEventListener('pagehide', () => { navigator.sendBeacon('/api/unload-beacon', 'b');"
                       " fetch('/api/unload-keepalive', {method: 'POST', body: 'k', keepalive: true}); });</script>"
                       "<h1>Unload page</h1><a href='/'>Back home</a>",
            "/selfclose": "<script>addEventListener('pagehide', () => { navigator.sendBeacon('/api/popup-beacon', 'b');"
                          " fetch('/api/popup-keepalive', {method: 'POST', body: 'k', keepalive: true}); });"
                          " setTimeout(() => window.close(), 300);</script>closing",
            "/login": "<script>for (const k of ['authToken', 'key', 'length', 'getItem', 'setItem', 'removeItem', 'clear', 'constructor'])"
                      " Storage.prototype.setItem.call(localStorage, k, 'v-' + k);"
                      " document.cookie = 'sid=user-session; path=/';</script>logged in",
            "/base": '<head><base target="_blank"></head><h1>Base target</h1>'
                     '<form method=post action="/api/base-form"><button>Base form</button></form>',
            "/streams": f"""<!doctype html><title>Streams</title><h1>Channel probes</h1>
<h2 id=r-main>main: probing</h2><h2 id=r-worker>worker: probing</h2><h2 id=r-sandboxed>sandboxed: probing</h2>
<h2 id=r-ctor>stream constructor: probing</h2>
<script>
const probe = () => 'WebSocketStream=' + typeof WebSocketStream + ' SharedWorker=' + typeof SharedWorker;
const say = (id, text) => {{ document.getElementById(id).textContent = text; }};
say('r-main', 'main: ' + probe());
const worker = new Worker(URL.createObjectURL(new Blob(['postMessage("WebSocketStream=" + typeof WebSocketStream + " SharedWorker=" + typeof SharedWorker)'])));
worker.onmessage = e => say('r-worker', 'worker: ' + e.data);
addEventListener('message', e => say('r-sandboxed', 'sandboxed: ' + e.data));
try {{
  const s = new WebSocketStream('ws://127.0.0.1:{self.port_ws}/stream');
  s.opened.then(({{writable}}) => writable.getWriter().write('stream-hello'));
  say('r-ctor', 'stream constructor: allowed');
}} catch (e) {{ say('r-ctor', 'stream constructor: refused ' + e.name); }}
</script>
<iframe sandbox="allow-scripts" title="sandboxed" srcdoc="&lt;script&gt;parent.postMessage('WebSocketStream=' + typeof WebSocketStream + ' SharedWorker=' + typeof SharedWorker, '*')&lt;/script&gt;"></iframe>""",
            "/null-origin": f"""<!doctype html><title>Opaque</title><h1>Opaque origin</h1><h2 id=r>null-origin save: waiting</h2>
<script>addEventListener('message', e => {{ document.getElementById('r').textContent = e.data; }});</script>
<iframe sandbox="allow-scripts" title="opaque" srcdoc="&lt;script&gt;fetch('{self.a}/api/null-origin', {{method: 'POST', credentials: 'include', body: 'x=1'}}).then(r =&gt; r.text()).then(() =&gt; parent.postMessage('null-origin save: ok', '*')).catch(e =&gt; parent.postMessage('null-origin save failed: ' + e, '*'))&lt;/script&gt;"></iframe>""",
            "/typeahead": "<!doctype html><title>Typeahead</title><h1>Typeahead</h1><h2 id=t>idle</h2>"
                          "<button onclick=\"for (let i = 0; i < 20; i++) { const c = new AbortController();"
                          " fetch('/api/typeahead', {method: 'POST', body: 'q=' + i, signal: c.signal}).catch(() => {});"
                          " setTimeout(() => c.abort(), 5); } document.getElementById('t').textContent = 'typed'\">Type fast</button>",
            "/saml": "<!doctype html><title>Signing in</title><form id=f method=post action='/acs'>"
                     "<input type=hidden name=SAMLResponse value=abc></form><script>document.getElementById('f').submit()</script>",
            "/sso": f"<h1>Dashboard</h1><script>if (!document.cookie.includes('app=ok')) location.replace('{self.b}/idp-login');</script>",
            "/idp-login": f"<h1>Please sign in</h1><script>if (document.cookie.includes('idp=1')) location.replace('{self.a}/callback');</script>",
            "/callback": "<h1>Signing in</h1><script>document.cookie = 'app=ok; path=/'; location.replace('/home');</script>",
            "/home": "<h1>Dashboard</h1>",
            "/late-sso": f"<h1>Dashboard</h1><script>if (!document.cookie.includes('app=ok')) setTimeout(() => location.replace('{self.b}/idp-login'), 1800);</script>",
            "/links": f"<!doctype html><title>Links</title><h1>Links</h1><a href='/cancel?id=7'>Cancel order</a> <a href='{self.b}/landing'>Partner site</a>",
            "/cancel?id=7": f"<h1>Order 7 cancelled</h1><a href='{self.b}/landing'>Partner site</a>",
            "/reporting": "<!doctype html><title>Reports</title><h1>A site that wants reports</h1>",
            "/chat": f"<!doctype html><title>Chat</title><h1>Chat</h1><button onclick=\"const s = new WebSocket('ws://127.0.0.1:{self.port_ws}/chat'); s.onopen = () => s.close(4001, 'page-chosen-reason');\">Open and close</button>",
            "/register": "<script>navigator.serviceWorker.register('/sw.js')"
                         ".then(() => navigator.serviceWorker.ready).then(() => { document.title = 'sw-ready'; });</script>",
            "/sw.js": "self.addEventListener('install', e => self.skipWaiting());"
                      "self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));"
                      "self.addEventListener('fetch', e => { if (e.request.url.endsWith('/api/sw-save'))"
                      " e.respondWith(fetch('/api/sw-forwarded', {method: 'POST', body: 'from-sw'})); });",
        }

    def _handler(self, port):
        site = self

        class Handler(BaseHTTPRequestHandler):
            def _record(self):
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                site.hits.append((port, self.command, self.path))

            def _send(self, body, ctype):
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                for name, value in site.extra_headers.get(self.path.split("?")[0], {}).items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(body.encode())

            def do_GET(self):
                self._record()
                if self.path == "/slow.png":
                    time.sleep(8)  # past the session's 4 s wait for the load event and its 2 s wait for quiet
                pages = {**site._pages(), **site.extra_pages}
                if self.path in pages:
                    ctype = "application/javascript" if self.path.endswith(".js") else "text/html"
                    self._send(pages[self.path], ctype)
                else:
                    self._send("{}", "application/json")

            def do_POST(self):
                self._record()
                if self.path == "/acs":
                    self._send("<h1>Dashboard</h1>", "text/html")
                else:
                    self._send("{}", "application/json")

            do_PUT = do_POST

            def do_OPTIONS(self):  # CORS preflight, answered like a real API
                site.hits.append((port, "OPTIONS", self.path))
                self.send_response(204)
                self.send_header("Access-Control-Allow-Origin", self.headers.get("Origin", "*"))
                self.send_header("Access-Control-Allow-Credentials", "true")
                self.send_header("Access-Control-Allow-Methods", "POST, PUT")
                self.send_header("Access-Control-Allow-Headers", "content-type")
                self.end_headers()

            def log_message(self, *args):
                pass

        return Handler


@pytest.fixture
def site():
    s = Site()
    yield s
    s.close()


def _captured(run_dir: Path):
    path = run_dir / "captured_writes.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


async def _run_writes(client, start_url, *, extra_clicks=()):
    """Start, click "Run writes" (and any extra controls), end; return texts and summary."""
    started = _text(await client.call_tool(
        "session_start", {"start_url": start_url, "success_criterion": CRITERION}))
    clicked = _text(await client.call_tool("click", {"target": "Run writes"}))
    waited = _text(await client.call_tool("wait_for", {"description": "'all done'"}))
    others = []
    for target in extra_clicks:
        others.append(_text(await client.call_tool("click", {"target": target})))
        await asyncio.sleep(1.0)
    page = _text(await client.call_tool("read_page", {}))
    summary = json.loads(_text(await client.call_tool("session_end", {})))
    return started, clicked + "\n" + waited, others, page, summary


EXPECTED_PATHS = {"/api/fetch-post", "/api/xhr-put", "/api/beacon", "/api/iframe-post", "/api/sw-save"}


def _check_blocked(site, summary):
    assert site.writes() == [], f"writes reached the server: {site.writes()}"
    assert site.ws_messages == []
    captured = _captured(Path(summary["run_dir"]))
    paths = {urlsplit(c["url"]).path for c in captured if c["method"] != "WEBSOCKET_SEND"}
    assert EXPECTED_PATHS <= paths, paths
    iframe = next(c for c in captured if c["url"].endswith("/api/iframe-post"))
    assert iframe["url"].startswith(site.b)  # the cross-site iframe's write
    assert any(c["method"] == "WEBSOCKET_SEND" for c in captured)
    fetch_post = next(c for c in captured if c["url"].endswith("/api/fetch-post"))
    assert fetch_post["body_fields"] == ["qty", "token"]
    assert "secret-token" not in json.dumps(captured)
    assert summary["extra"]["dry_run"] is True
    return captured


def test_launched_browser_blocks_every_write_path(site, tmp_path):
    async def scenario():
        async with _client(_server_env(tmp_path), tmp_path) as client:
            return await _run_writes(client, site.a + "/", extra_clicks=("Send form",))

    started, clicked, (form,), page, summary = asyncio.run(scenario())
    assert "Writes are blocked" in started
    assert "blocked" in clicked and "nothing was saved" in clicked, clicked
    assert "blocked 1 write(s), not sent: POST /api/form" in form, form
    assert "Blocked by Knarr" in page
    captured = _check_blocked(site, summary)
    form_post = next(c for c in captured if c["url"].endswith("/api/form"))
    assert form_post["navigation"] is True and form_post["body_fields"] == ["password"]
    assert "hunter2" not in json.dumps(captured)


def test_new_tabs_open_in_knarr_context_and_their_writes_are_blocked(site, tmp_path):
    env = _server_env(tmp_path)
    controls = ("Popup link", "Open window", "Detached link", "Shadow link", "Form in new tab", "Button new tab")

    async def scenario():
        async with _client(env, tmp_path) as client:
            await client.call_tool("session_start", {"start_url": site.a + "/", "success_criterion": CRITERION})
            results = {}
            for target in controls:
                results[target] = _text(await client.call_tool("click", {"target": target}))
                await asyncio.sleep(1.0)
                results[target] += "\n" + _text(await client.call_tool("note", {"text": "after " + target}))
            await client.call_tool("navigate", {"url": "/base"})
            results["Base form"] = _text(await client.call_tool("click", {"target": "Base form"}))
            await asyncio.sleep(1.0)
            results["Base form"] += "\n" + _text(await client.call_tool("note", {"text": "after base"}))
            summary = json.loads(_text(await client.call_tool("session_end", {})))
            return results, summary

    results, summary = asyncio.run(scenario())
    assert site.writes() == [], f"writes reached the server: {site.writes()}"
    for target in controls[:4]:
        assert "opened a new tab" in results[target], (target, results[target])
        assert "POST /api/popup-post" in results[target], (target, results[target])
    urls = [c["url"] for c in _captured(Path(summary["run_dir"]))]
    assert sum(u.endswith("/api/popup-post") for u in urls) == 4, urls
    for path in ("/api/form-popup", "/api/formtarget-popup", "/api/base-form"):
        assert any(u.endswith(path) for u in urls), (path, urls)


def test_a_worker_websocket_is_reported_as_a_gap(site, tmp_path):
    async def scenario():
        async with _client(_server_env(tmp_path), tmp_path) as client:
            await client.call_tool("session_start", {"start_url": site.a + "/", "success_criterion": CRITERION})
            out = _text(await client.call_tool("click", {"target": "Start worker"}))
            await asyncio.sleep(1.0)
            out += "\n" + _text(await client.call_tool("note", {"text": "after worker"}))
            summary = json.loads(_text(await client.call_tool("session_end", {})))
            return out, summary

    out, summary = asyncio.run(scenario())
    assert "warning: a page started a Web Worker" in out, out
    assert summary["extra"]["write_block_gaps"]


def test_the_page_keeps_loading_between_tool_calls(site, tmp_path):
    """Routed requests run while the agent is thinking, not only during a call."""
    async def scenario():
        async with _client(_server_env(tmp_path), tmp_path) as client:
            await client.call_tool("session_start", {"start_url": site.a + "/", "success_criterion": CRITERION})
            await asyncio.sleep(3.0)  # no tool calls: the fetch fires 1.5 s after load, later than the start's own wait
            page = _text(await client.call_tool("read_page", {}))
            await client.call_tool("session_end", {})
            return page

    assert "data loaded" in asyncio.run(scenario())


def test_allow_writes_lets_everything_through(site, tmp_path):
    async def scenario():
        async with _client(_server_env(tmp_path, KNARR_ALLOW_WRITES="1"), tmp_path) as client:
            return await _run_writes(client, site.a + "/")

    started, clicked, _, _, summary = asyncio.run(scenario())
    assert "Writes are ALLOWED" in started
    assert "blocked" not in clicked
    assert (site.port_a, "POST", "/api/fetch-post") in site.writes()
    assert summary["extra"]["dry_run"] is False
    assert summary["extra"]["blocked_writes"] is None and summary["extra"]["allowed_writes"] is None


def test_allow_patterns_let_named_requests_through(site, tmp_path):
    env = _server_env(tmp_path, KNARR_WRITE_ALLOW="**/api/fetch-post")

    async def scenario():
        async with _client(env, tmp_path) as client:
            return await _run_writes(client, site.a + "/")

    _, clicked, _, _, summary = asyncio.run(scenario())
    assert [w[2] for w in site.writes()] == ["/api/fetch-post"]
    captured = _captured(Path(summary["run_dir"]))
    sent = [c for c in captured if c.get("allowed")]
    assert [urlsplit(c["url"]).path for c in sent] == ["/api/fetch-post"]
    assert any(c["url"].endswith("/api/xhr-put") and not c.get("allowed") for c in captured)
    assert "sent 1 write(s) allowed by KNARR_WRITE_ALLOW: POST /api/fetch-post" in clicked
    # One write really went out, so this is not a dry run; everything else was faked.
    assert summary["extra"]["dry_run"] is False and summary["extra"]["allowed_writes"] == 1
    assert summary["extra"]["blocked_writes"] >= 1


def test_shared_workers_are_refused_and_their_requests_never_sent(site, tmp_path):
    async def scenario():
        async with _client(_server_env(tmp_path), tmp_path) as client:
            await client.call_tool("session_start", {"start_url": site.a + "/", "success_criterion": CRITERION})
            await client.call_tool("click", {"target": "Start shared worker"})
            await asyncio.sleep(1.0)
            page = _text(await client.call_tool("read_page", {}))
            await client.call_tool("session_end", {})
            return page

    page = asyncio.run(scenario())
    assert "shared worker refused: ReferenceError" in page, page  # the browser flag removes it everywhere
    assert (site.port_a, "POST", "/api/shared-post") not in site.writes()


def test_plain_text_form_values_are_never_recorded(site, tmp_path):
    async def scenario():
        async with _client(_server_env(tmp_path), tmp_path) as client:
            await client.call_tool("session_start", {"start_url": site.a + "/", "success_criterion": CRITERION})
            await client.call_tool("click", {"target": "Send plain form"})
            await asyncio.sleep(0.8)
            return json.loads(_text(await client.call_tool("session_end", {})))

    summary = asyncio.run(scenario())
    assert site.writes() == []
    run_dir = Path(summary["run_dir"])
    plain = [c for c in _captured(run_dir) if c["url"].endswith("/api/plain")]
    # text/plain has no reliable parser, so not even field names are kept.
    assert plain and plain[0]["body_kind"] == "text/plain" and "body_fields" not in plain[0]
    assert "hunter2" not in (run_dir / "captured_writes.jsonl").read_text()


def test_attach_copies_the_sites_login_from_the_users_open_tab(site, tmp_path, throwaway_chrome):
    from playwright.sync_api import sync_playwright

    # The user logged in before Knarr connected, and the tab is still open.
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(throwaway_chrome)
        context = browser.contexts[0]
        context.new_page().goto(site.a + "/login")
        context.add_cookies([{"name": "bank", "value": "bank-secret", "domain": "bank.example.org", "path": "/"}])

    env = _server_env(tmp_path, KNARR_CDP_URL=throwaway_chrome, KNARR_TRACE="1")

    async def scenario():
        async with _client(env, tmp_path) as client:
            started = _text(await client.call_tool(
                "session_start", {"start_url": site.a + "/", "success_criterion": CRITERION}))
            page = _text(await client.call_tool("read_page", {}))
            summary = json.loads(_text(await client.call_tool("session_end", {})))
            return started, page, summary

    started, page, summary = asyncio.run(scenario())
    assert f"1 cookie(s) for {site.a} and its localStorage" in started, started
    assert "token: present" in page, page
    trace = Path(summary["run_dir"]) / "trace.zip"
    assert trace.is_file()  # only because KNARR_TRACE=1
    import zipfile
    with zipfile.ZipFile(trace) as z:
        content = b"".join(z.read(n) for n in z.namelist())
    assert b"bank-secret" not in content  # other sites' cookies are never copied


def test_no_trace_is_written_by_default(site, tmp_path):
    async def scenario():
        async with _client(_server_env(tmp_path), tmp_path) as client:
            await client.call_tool("session_start", {"start_url": site.a + "/", "success_criterion": CRITERION})
            return json.loads(_text(await client.call_tool("session_end", {})))

    summary = asyncio.run(scenario())
    assert not (Path(summary["run_dir"]) / "trace.zip").exists()


UNLOAD_PATHS = {"/api/unload-beacon", "/api/unload-keepalive"}


@pytest.mark.parametrize("channel", [None, "chrome"], ids=["bundled", "chrome"])
def test_unload_beacons_and_keepalive_are_blocked(site, tmp_path, channel):
    """Current Chrome sends these from the browser process while a page
    unloads; per-page interception misses them, browser-level doesn't."""
    import os
    from playwright.sync_api import sync_playwright

    if channel and not os.path.exists(CHROME):
        pytest.skip("Google Chrome not installed")
    if not channel:
        with sync_playwright() as pw:
            if not os.path.exists(pw.chromium.executable_path):
                pytest.skip("Playwright's bundled Chromium isn't installed")
    env = _server_env(tmp_path, **({"KNARR_BROWSER_CHANNEL": channel} if channel else {}))
    env.pop("KNARR_BROWSER_CHANNEL", None) if not channel else None

    async def scenario():
        async with _client(env, tmp_path) as client:
            await client.call_tool("session_start", {"start_url": site.a + "/unload", "success_criterion": CRITERION})
            await client.call_tool("click", {"target": "Back home"})   # unload mid-session
            await asyncio.sleep(1.0)
            await client.call_tool("click", {"target": "Unload page"})
            await asyncio.sleep(0.5)
            await client.call_tool("click", {"target": "Back home"})     # unload again
            await asyncio.sleep(0.5)
            popup = _text(await client.call_tool("click", {"target": "Self-closing popup"}))
            assert popup.startswith("clicked"), popup
            await asyncio.sleep(1.5)
            return json.loads(_text(await client.call_tool("session_end", {})))  # unload at the end

    summary = asyncio.run(scenario())
    time.sleep(1.0)
    leaked = [w for w in site.writes() if w[2] in UNLOAD_PATHS | {"/api/popup-beacon", "/api/popup-keepalive"}]
    assert leaked == [], f"unload writes reached the server: {leaked}"
    captured = _captured(Path(summary["run_dir"]))
    paths = [urlsplit(c["url"]).path for c in captured]
    assert UNLOAD_PATHS <= set(paths), paths
    assert {"/api/popup-beacon", "/api/popup-keepalive"} <= set(paths), paths
    # Requests released while the browser closed are in the summary, too.
    assert summary["extra"]["blocked_writes"] == len(captured), (summary["extra"], paths)


def test_bodies_without_a_real_parser_record_kind_and_size_only(site, tmp_path):
    async def scenario():
        async with _client(_server_env(tmp_path), tmp_path) as client:
            await client.call_tool("session_start", {"start_url": site.a + "/", "success_criterion": CRITERION})
            await client.call_tool("click", {"target": "Send XML"})
            await client.call_tool("click", {"target": "Upload"})
            await asyncio.sleep(0.8)
            return json.loads(_text(await client.call_tool("session_end", {})))

    summary = asyncio.run(scenario())
    run_dir = Path(summary["run_dir"])
    text = (run_dir / "captured_writes.jsonl").read_text()
    for secret in ("hunter2", "passport_john_doe", "secret-anchor"):
        assert secret not in text, secret
    captured = {urlsplit(c["url"]).path: c for c in _captured(run_dir)}
    assert captured["/api/xml"]["body_kind"] == "application/xml"
    assert "body_fields" not in captured["/api/xml"]
    assert captured["/api/upload"]["body_fields"] == ["doc", "note"]


def test_an_allowed_websocket_carries_its_messages(site, tmp_path):
    env = _server_env(tmp_path, KNARR_WRITE_ALLOW=f"ws://127.0.0.1:{site.port_ws}/live")

    async def scenario():
        async with _client(env, tmp_path) as client:
            await client.call_tool("session_start", {"start_url": site.a + "/", "success_criterion": CRITERION})
            await client.call_tool("click", {"target": "Open live socket"})
            await asyncio.sleep(1.0)
            await client.call_tool("click", {"target": "Run writes"})  # its socket isn't allowed
            await asyncio.sleep(1.0)
            await client.call_tool("session_end", {})

    asyncio.run(scenario())
    assert "live-hello" in site.ws_messages
    assert "ws-write" not in site.ws_messages


def test_a_blocked_cross_origin_save_looks_successful_to_the_page(site, tmp_path):
    async def scenario():
        async with _client(_server_env(tmp_path), tmp_path) as client:
            await client.call_tool("session_start", {"start_url": site.a + "/", "success_criterion": CRITERION})
            await client.call_tool("click", {"target": "Cross-origin save"})
            await asyncio.sleep(1.0)
            page = _text(await client.call_tool("read_page", {}))
            await client.call_tool("session_end", {})
            return page

    page = asyncio.run(scenario())
    assert "cross-origin save: ok" in page, page
    assert (site.port_b, "POST", "/api/xo") not in site.writes()


def test_a_service_worker_cannot_register_in_knarr_browser(site, tmp_path):
    async def scenario():
        async with _client(_server_env(tmp_path), tmp_path) as client:
            await client.call_tool("session_start", {"start_url": site.a + "/register", "success_criterion": CRITERION})
            await asyncio.sleep(1.5)  # the page tries to register its service worker
            await client.call_tool("navigate", {"url": "/"})
            await client.call_tool("click", {"target": "Run writes"})
            await asyncio.sleep(1.0)
            return json.loads(_text(await client.call_tool("session_end", {})))

    summary = asyncio.run(scenario())
    assert (site.port_a, "POST", "/api/sw-forwarded") not in site.writes()
    assert site.writes() == []
    paths = [urlsplit(c["url"]).path for c in _captured(Path(summary["run_dir"]))]
    assert "/api/sw-save" in paths  # sent by the page itself, and blocked


def test_a_dialog_in_the_users_tab_does_not_hang_the_borrow_and_stays_open(site, tmp_path, throwaway_chrome):
    import urllib.request
    from websockets.sync.client import connect
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(throwaway_chrome)
        browser.contexts[0].new_page().goto(site.a + "/login")
    tab = next(t for t in json.load(urllib.request.urlopen(throwaway_chrome + "/json/list"))
               if t["type"] == "page" and t["url"].startswith(site.a))
    with connect(tab["webSocketDebuggerUrl"], max_size=None) as ws:
        ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
                            "params": {"expression": "setTimeout(() => confirm('Session timing out'), 10); 1"}}))
        ws.recv()
    time.sleep(0.5)  # the user's tab now shows a dialog

    async def scenario():
        async with _client(_server_env(tmp_path, KNARR_CDP_URL=throwaway_chrome), tmp_path) as client:
            started = _text(await asyncio.wait_for(client.call_tool(
                "session_start", {"start_url": site.a + "/", "success_criterion": CRITERION}), timeout=30))
            await client.call_tool("session_end", {})
            return started

    started = asyncio.run(scenario())
    assert started.startswith("Session started"), started
    assert "didn't answer, maybe a dialog is open" in started, started
    # The dialog is still there for the user: the tab still can't answer.
    from knarr.explorer.borrow import _call

    assert _call(tab["webSocketDebuggerUrl"], "Runtime.evaluate", {"expression": "1"}, timeout=1.0) is None


# -- channels the browser itself switches off ----------------------------------------------

def test_websocketstream_and_shared_workers_are_gone_in_every_realm(site, tmp_path):
    async def scenario():
        async with _client(_server_env(tmp_path), tmp_path) as client:
            await client.call_tool("session_start", {"start_url": site.a + "/streams", "success_criterion": CRITERION})
            await asyncio.sleep(1.5)
            page = _text(await client.call_tool("read_page", {}))
            await client.call_tool("session_end", {})
            return page

    page = asyncio.run(scenario())
    for realm in ("main", "worker", "sandboxed"):
        assert f"{realm}: WebSocketStream=undefined SharedWorker=undefined" in page, page
    assert "stream constructor: refused ReferenceError" in page, page
    assert site.ws_messages == []


def test_a_sandboxed_srcdoc_frame_is_reported_as_a_gap(site, tmp_path):
    async def scenario():
        async with _client(_server_env(tmp_path), tmp_path) as client:
            started = _text(await client.call_tool(
                "session_start", {"start_url": site.a + "/streams", "success_criterion": CRITERION}))
            await asyncio.sleep(1.0)
            note = _text(await client.call_tool("note", {"text": "after the frame"}))
            summary = json.loads(_text(await client.call_tool("session_end", {})))
            return started + "\n" + note, summary

    text, summary = asyncio.run(scenario())
    assert "a page embedded a sandboxed frame" in text, text
    assert any("sandboxed frame" in gap for gap in summary["extra"]["write_block_gaps"])


# -- what a blocked write looks like from the page -------------------------------------------

def test_a_blocked_write_from_a_sandboxed_frame_looks_successful_to_the_page(site, tmp_path):
    async def scenario():
        async with _client(_server_env(tmp_path), tmp_path) as client:
            await client.call_tool("session_start", {"start_url": site.a + "/null-origin", "success_criterion": CRITERION})
            await asyncio.sleep(1.5)
            page = _text(await client.call_tool("read_page", {}))
            await client.call_tool("session_end", {})
            return page

    page = asyncio.run(scenario())
    assert "null-origin save: ok" in page, page  # its Origin is the literal "null"
    assert (site.port_a, "POST", "/api/null-origin") not in site.writes()


def test_text_on_the_blocked_page_never_counts_as_success(site, tmp_path):
    criterion = "the page shows 'submitted'"

    async def scenario():
        async with _client(_server_env(tmp_path), tmp_path) as client:
            await client.call_tool("session_start", {"start_url": site.a + "/", "success_criterion": criterion})
            clicked = _text(await client.call_tool("click", {"target": "Send form"}))
            await asyncio.sleep(0.8)
            page = _text(await client.call_tool("read_page", {}))
            asserted = _text(await client.call_tool("assert_state", {}))
            waited = _text(await client.call_tool("wait_for", {"description": "'submitted'"}))
            status = json.loads(_text(await client.call_tool("session_status", {})))
            await client.call_tool("session_end", {})
            return page, asserted, waited, status

    page, asserted, waited, status = asyncio.run(scenario())
    assert "Blocked by Knarr" in page
    assert asserted.startswith("not_matched") and "stand-in for a form post" in asserted, asserted
    assert "stand-in for a form post" in waited and "appeared" not in waited, waited
    assert status["success_asserted"] is False and status["ended"] is False


def test_a_blocked_sign_in_handoff_is_explained_and_allowing_it_signs_in(site, tmp_path):
    async def scenario(**extra):
        async with _client(_server_env(tmp_path, **extra), tmp_path) as client:
            started = _text(await client.call_tool(
                "session_start", {"start_url": site.a + "/saml", "success_criterion": CRITERION}))
            page = _text(await client.call_tool("read_page", {}))
            moved = _text(await client.call_tool("navigate", {"url": "/home"}))
            await client.call_tool("session_end", {})
            return started, page, moved

    started, page, moved = asyncio.run(scenario())
    assert "stand-in for a form post it blocked" in started and "KNARR_WRITE_ALLOW" in started, started
    assert "Blocked by Knarr" in page
    assert moved.startswith(f"navigated: {site.a}/home"), moved  # the blocked page didn't become the way back
    assert (site.port_a, "POST", "/acs") not in site.writes()

    started, page, _ = asyncio.run(scenario(KNARR_WRITE_ALLOW="**/acs"))
    assert "stand-in" not in started and "Dashboard" in page, (started, page)
    assert (site.port_a, "POST", "/acs") in site.writes()


def test_aborted_allowed_requests_are_not_counted_as_sent(site, tmp_path):
    env = _server_env(tmp_path, KNARR_WRITE_ALLOW="**/api/typeahead")

    async def scenario():
        async with _client(env, tmp_path) as client:
            await client.call_tool("session_start", {"start_url": site.a + "/typeahead", "success_criterion": CRITERION})
            await client.call_tool("click", {"target": "Type fast"})
            await asyncio.sleep(1.5)
            return json.loads(_text(await client.call_tool("session_end", {})))

    summary = asyncio.run(scenario())
    reached = [w for w in site.writes() if w[2] == "/api/typeahead"]
    outcomes = [c["outcome"] for c in _captured(Path(summary["run_dir"])) if c["url"].endswith("/api/typeahead")]
    assert len(outcomes) <= 20, outcomes  # a request cancelled before the browser paused it is never seen
    assert set(outcomes) <= {"sent", "aborted"}, outcomes
    # Whether the browser refuses to continue a request the page already cancelled depends on its
    # version (Chromium 130 does, Chrome 156 accepts it silently), so a cancelled request can still
    # count as sent: the count never falls below what really reached the server.
    assert summary["extra"]["allowed_writes"] == outcomes.count("sent") >= len(reached)
    assert summary["extra"]["dry_run"] is (outcomes.count("sent") == 0)


# -- the guard, sockets and reports ----------------------------------------------------------------------------------------

def test_the_guard_never_requests_a_page_a_click_landed_on_a_second_time(site, tmp_path):
    async def scenario():
        async with _client(_server_env(tmp_path), tmp_path) as client:
            await client.call_tool("session_start", {"start_url": site.a + "/links", "success_criterion": CRITERION})
            await client.call_tool("click", {"target": "Cancel order"})  # a GET that changes data: not blocked
            out = _text(await client.call_tool("click", {"target": "Partner site"}))  # on the page it landed on
            await client.call_tool("session_end", {})
            return out

    out = asyncio.run(scenario())
    assert "rejected: left_origin" in out and f"returned to {site.a}/links" in out, out
    cancels = [h for h in site.hits if h[2] == "/cancel?id=7"]
    assert len(cancels) == 1, cancels  # the click, and nothing the guard added


def test_a_blocked_socket_does_not_pass_the_pages_close_code_to_the_server(site, tmp_path):
    async def scenario(**extra):
        async with _client(_server_env(tmp_path, **extra), tmp_path) as client:
            await client.call_tool("session_start", {"start_url": site.a + "/chat", "success_criterion": CRITERION})
            await client.call_tool("click", {"target": "Open and close"})
            await asyncio.sleep(1.5)
            await client.call_tool("session_end", {})

    asyncio.run(scenario())
    [(path, code, reason)] = [c for c in site.ws_closes if c[0] == "/chat"]
    assert code != 4001 and reason != "page-chosen-reason", (code, reason)

    site.ws_closes.clear()
    asyncio.run(scenario(KNARR_WRITE_ALLOW=f"ws://127.0.0.1:{site.port_ws}/chat"))
    [(path, code, reason)] = [c for c in site.ws_closes if c[0] == "/chat"]
    assert (code, reason) == (4001, "page-chosen-reason")  # an allowed socket carries it


def test_a_site_that_asks_for_reports_is_reported_as_a_gap(site, tmp_path):
    site.extra_headers["/reporting"] = {"Reporting-Endpoints": 'rep="/reports-api"'}

    async def scenario():
        async with _client(_server_env(tmp_path), tmp_path) as client:
            started = _text(await client.call_tool(
                "session_start", {"start_url": site.a + "/reporting", "success_criterion": CRITERION}))
            note = _text(await client.call_tool("note", {"text": "after the page"}))
            summary = json.loads(_text(await client.call_tool("session_end", {})))
            return started + "\n" + note, summary

    text, summary = asyncio.run(scenario())
    assert "asked Chrome to send it reports" in text, text
    assert any("send it reports" in gap for gap in summary["extra"]["write_block_gaps"])


def test_a_polyfill_that_assigns_webrtc_does_not_break_the_page(site, tmp_path):
    site.extra_pages["/polyfill"] = (
        "<!doctype html><title>Poly</title><h1>Poly</h1><h2 id=r>before</h2>"
        "<script type='module'>window.RTCPeerConnection = window.RTCPeerConnection || window.webkitRTCPeerConnection;"
        "document.getElementById('r').textContent = 'assigned without throwing';</script>")

    async def scenario():
        async with _client(_server_env(tmp_path), tmp_path) as client:
            await client.call_tool("session_start", {"start_url": site.a + "/polyfill", "success_criterion": CRITERION})
            page = _text(await client.call_tool("read_page", {}))
            await client.call_tool("session_end", {})
            return page

    assert "assigned without throwing" in asyncio.run(scenario())


def test_a_page_may_declare_or_redefine_the_webrtc_name_without_breaking(site, tmp_path):
    site.extra_pages["/global-let"] = (
        "<!doctype html><title>Global</title><h1>Global</h1><h2 id=r>before</h2>"
        "<script>let RTCPeerConnection = 1; document.getElementById('r').textContent = 'declared without throwing';</script>")
    site.extra_pages["/redefine"] = (
        "<!doctype html><title>Redefine</title><h1>Redefine</h1><h2 id=r>before</h2>"
        "<script>Object.defineProperty(window, 'webkitRTCPeerConnection', {value: 2, configurable: true});"
        "document.getElementById('r').textContent = 'redefined without throwing';</script>")

    async def scenario():
        async with _client(_server_env(tmp_path), tmp_path) as client:
            pages = []
            await client.call_tool("session_start", {"start_url": site.a + "/global-let", "success_criterion": CRITERION})
            pages.append(_text(await client.call_tool("read_page", {})))
            await client.call_tool("navigate", {"url": "/redefine"})
            pages.append(_text(await client.call_tool("read_page", {})))
            await client.call_tool("session_end", {})
            return pages

    first, second = asyncio.run(scenario())
    assert "declared without throwing" in first and "redefined without throwing" in second


# -- what a real run showed: a sticky header, and a screenshot too tall to read --------------------------------------------

def _png_size(path):
    import struct

    data = Path(path).read_bytes()
    return struct.unpack(">II", data[16:24])


def test_a_link_hidden_under_a_fixed_header_is_clicked_in_seconds_with_a_note(site, tmp_path):
    """Two fixed elements overlap, so no scrolling uncovers the link (a shop's Buy button under its header)."""
    site.extra_pages["/sticky"] = (
        "<!doctype html><title>Sticky</title>"
        "<a href='/landed' style='position:fixed;top:30px;left:20px'>Buy now</a>"
        "<div style='position:fixed;top:0;left:0;right:0;height:100px;background:#ccc;z-index:10'>Header</div>")
    site.extra_pages["/landed"] = "<!doctype html><title>Landed</title><h1>Landed</h1>"

    async def scenario():
        async with _client(_server_env(tmp_path), tmp_path) as client:
            await client.call_tool("session_start", {"start_url": site.a + "/sticky", "success_criterion": CRITERION})
            page = _text(await client.call_tool("read_page", {}))
            index = re.search(r"\[(\d+)\] link 'Buy now'", page).group(1)
            started = time.monotonic()
            clicked = _text(await client.call_tool("click_index", {"index": int(index)}))
            took = time.monotonic() - started
            landed = _text(await client.call_tool("read_page", {}))
            await client.call_tool("session_end", {})
            return clicked, took, landed

    clicked, took, landed = asyncio.run(scenario())
    assert clicked.startswith("clicked") and "another element covered it, so Knarr sent the click to it directly" in clicked
    assert took < 4.5 and "Landed" in landed, (took, landed)


def test_a_screenshot_is_the_visible_window_unless_the_whole_page_is_asked_for(site, tmp_path):
    site.extra_pages["/tall"] = "<!doctype html><title>Tall</title><h1>Tall</h1><div style='height:6000px'>long</div>"

    async def scenario():
        async with _client(_server_env(tmp_path), tmp_path) as client:
            await client.call_tool("session_start", {"start_url": site.a + "/tall", "success_criterion": CRITERION})
            window = _text(await client.call_tool("screenshot", {}))
            whole = _text(await client.call_tool("screenshot", {"full_page": True}))
            await client.call_tool("session_end", {})
            return window, whole

    window, whole = asyncio.run(scenario())
    window_path, whole_path = [t.splitlines()[0].removeprefix("saved: ") for t in (window, whole)]
    assert _png_size(window_path)[1] <= 1100        # the 1000-pixel-high window
    assert _png_size(whole_path)[1] >= 6000          # the whole page
