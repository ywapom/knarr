"""``knarr-doctor``: check that Knarr can run here, and say how to fix what can't.

It checks, in order: the Python that runs it, the installed packages, your
``KNARR_*`` settings, your site profiles, the browser (it starts one, loads a
local test page that tries to write, and checks that the write block held the
write back), the Chrome you borrow a login from, and that the MCP server starts
and lists its tools. Nothing here opens a site, and no cookie value is read or printed.

    uvx --from knarr knarr-doctor           everything
    uvx --from knarr knarr-doctor --quick   settings, packages and Chrome only (no browser, no server)

Installed with pip, run it as ``knarr-doctor`` or ``python3 -m knarr.doctor``.

Exit status 1 if anything failed; warnings don't change it.
"""

from __future__ import annotations

import asyncio
import difflib
import importlib.metadata
import json
import os
import platform
import re
import shutil
import signal
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, TextIO, Tuple

MIN_PYTHON = (3, 12)

# Minimum versions of what the package needs (pyproject.toml is the source; a test keeps these equal).
REQUIRED: Dict[str, Tuple[Tuple[int, ...], Optional[Tuple[int, ...]]]] = {
    "mcp": ((1, 13), (2,)),          # (minimum, first version that is too new)
    "playwright": ((1, 48), (2,)),
    "pyyaml": ((6, 0), (7,)),
    "websockets": ((12,), (16,)),
}

DIST_NAME = "knarr"
# The plugin starts the server with uvx, which fetches the package from PyPI on first use.
SERVER_COMMAND = f"uvx {DIST_NAME}"
DOCTOR_COMMAND = f"uvx --from {DIST_NAME} knarr-doctor"
# What fixes a missing or mismatched package: a fresh copy through uvx, or pip for a pip install.
INSTALL_COMMAND = f"uvx --refresh --from {DIST_NAME} knarr-doctor"
PIP_INSTALL_COMMAND = f"python3 -m pip install --upgrade {DIST_NAME}"
PACKAGE_FIX = f"{INSTALL_COMMAND}\n(installed with pip instead: {PIP_INSTALL_COMMAND})"
BROWSER_COMMAND = f"uvx --from {DIST_NAME} playwright install chromium"

RUNS_DIR_ENV = "KNARR_RUNS_DIR"
PROFILES_DIR_ENV = "KNARR_PROFILES_DIR"  # the profiles module owns it; imported lazily, a test keeps them equal
KNOWN_SETTINGS = (
    "KNARR_ALLOW_WRITES", "KNARR_BLOCK_WRITES", "KNARR_WRITE_ALLOW", "KNARR_RECORD_FIELDS",
    "KNARR_CDP_URL", "KNARR_STORAGE_STATE", "KNARR_BROWSER_CHANNEL", "KNARR_NO_SANDBOX",
    "KNARR_TRACE", RUNS_DIR_ENV, "KNARR_MAX_TURNS", "KNARR_MAX_WALL_CLOCK_SEC",
    "KNARR_IDLE_TIMEOUT_SEC", PROFILES_DIR_ENV,
)
# Set by the tests that drive real browsers; not a setting for users.
TEST_ONLY_SETTINGS = ("KNARR_BROWSER_TESTS",)

BROWSER_TEST_SECONDS = 90
SERVER_TEST_SECONDS = 30


@dataclass
class Result:
    status: str          # "ok", "warn", "fail" or "info"
    title: str
    detail: str = ""
    fix: str = ""


def ok(title: str, detail: str = "") -> Result:
    return Result("ok", title, detail)


def warn(title: str, detail: str = "", fix: str = "") -> Result:
    return Result("warn", title, detail, fix)


def fail(title: str, detail: str = "", fix: str = "") -> Result:
    return Result("fail", title, detail, fix)


def info(title: str, detail: str = "", fix: str = "") -> Result:
    return Result("info", title, detail, fix)


def _version_tuple(text: str) -> Tuple[int, ...]:
    parts: List[int] = []
    for piece in text.split("."):
        digits = ""
        for ch in piece:
            if not ch.isdigit():
                break
            digits += ch
        if not digits:
            break
        parts.append(int(digits))
        if digits != piece:
            break
    return tuple(parts)


# -- python and packages ------------------------------------------------------------------------

def check_python(version: Sequence[int] = tuple(sys.version_info[:3]), executable: str = sys.executable,
                 uvx_path: Optional[str] = None) -> List[Result]:
    results: List[Result] = []
    shown = ".".join(str(v) for v in version[:3])
    if tuple(version[:2]) < MIN_PYTHON:
        results.append(fail(f"Python {shown} is too old", f"Knarr needs Python {'.'.join(map(str, MIN_PYTHON))} or newer.",
                            "Install a newer Python and install Knarr for it."))
    else:
        results.append(ok(f"Python {shown}", executable))
    if uvx_path is None:
        uvx_path = shutil.which("uvx")
    if uvx_path:
        results.append(ok("uvx", uvx_path))
    else:
        results.append(warn("No uvx on your PATH", f"The Claude Code plugin starts the server with `{SERVER_COMMAND}`.",
                            "Install uv (https://docs.astral.sh/uv/), or skip the plugin and add the server "
                            "for the Python that has Knarr: claude mcp add knarr -- python3 -m knarr.mcp"))
    return results


def _installed_versions() -> Dict[str, Optional[str]]:
    found: Dict[str, Optional[str]] = {}
    for name in REQUIRED:
        try:
            found[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            found[name] = None
    return found


def check_packages(versions: Optional[Mapping[str, Optional[str]]] = None) -> List[Result]:
    versions = _installed_versions() if versions is None else versions
    results: List[Result] = []
    try:
        own = importlib.metadata.version(DIST_NAME)
        results.append(ok(f"{DIST_NAME} {own}"))
    except importlib.metadata.PackageNotFoundError:
        results.append(info(f"{DIST_NAME} is run from source, not installed",
                            "That is fine for development; the plugin runs the released package with uvx."))
    for name, (minimum, too_new) in REQUIRED.items():
        have = versions.get(name)
        wanted = ".".join(map(str, minimum)) + (f" up to but not including {'.'.join(map(str, too_new))}" if too_new else " or newer")
        if have is None:
            results.append(fail(f"{name} is not installed", f"Needs {wanted}.", PACKAGE_FIX))
            continue
        parsed = _version_tuple(have)
        if parsed < minimum or (too_new is not None and parsed >= too_new):
            results.append(fail(f"{name} {have} doesn't fit", f"Needs {wanted}.", PACKAGE_FIX))
        else:
            results.append(ok(f"{name} {have}"))
    return results


# -- settings -------------------------------------------------------------------------------------

def check_settings(env: Mapping[str, str]) -> List[Result]:
    from knarr.explorer.write_block import WriteBlock, WritePolicy, parse_switch

    results: List[Result] = []
    known = set(KNOWN_SETTINGS) | set(TEST_ONLY_SETTINGS)
    for name in sorted(k for k in env if k.startswith("KNARR_") and k not in known):
        close = difflib.get_close_matches(name, KNOWN_SETTINGS, n=1, cutoff=0.7)
        results.append(warn(f"{name} is not a Knarr setting", "It has no effect.",
                            f"Did you mean {close[0]}?" if close else "See the environment variables table in the README."))

    def flag(name: str) -> bool:
        return parse_switch(env.get(name)) is True

    # A switch that nobody can read is off, except KNARR_BLOCK_WRITES, where it counts as on. Say so.
    for name in ("KNARR_ALLOW_WRITES", "KNARR_BLOCK_WRITES", "KNARR_TRACE", "KNARR_NO_SANDBOX"):
        raw = (env.get(name) or "").strip()
        if parse_switch(raw) is None:
            safe = name == "KNARR_BLOCK_WRITES"
            results.append(warn(f"{name}={raw!r} is not understood, so " + ("writes are blocked" if safe else "it is off"),
                                "Say 1 or 0 (also true/false, yes/no, on/off).",
                                f"Set {name}=1" if safe else f"Set {name}=1 to turn it on."))
    # The write policy, built the way the server builds it.
    block = parse_switch(env.get("KNARR_BLOCK_WRITES")) is not False
    allow = flag("KNARR_ALLOW_WRITES")
    patterns = tuple(p.strip() for p in (env.get("KNARR_WRITE_ALLOW") or "").split(",") if p.strip())
    try:
        WriteBlock(WritePolicy(block=block or not allow, allow_patterns=patterns))
    except ValueError as exc:
        results.append(fail("KNARR_WRITE_ALLOW is not valid", str(exc), "Fix or unset KNARR_WRITE_ALLOW."))
        patterns = ()
    if allow and not block:
        results.append(warn("Writes are ALLOWED (KNARR_ALLOW_WRITES)",
                            "Clicks act with your real permissions and can change data.",
                            "Unset KNARR_ALLOW_WRITES to block writes again."))
    else:
        note = f"; {len(patterns)} request pattern(s) in KNARR_WRITE_ALLOW are let through" if patterns else ""
        results.append(ok("Writes are blocked" + (" (KNARR_BLOCK_WRITES wins over KNARR_ALLOW_WRITES)" if allow else ""), note.lstrip("; ")))
    if flag("KNARR_TRACE"):
        results.append(warn("KNARR_TRACE is on", "trace.zip holds page contents, typed text and cookies, so the session's login.",
                            "Unset it unless you are debugging, and delete the traces after."))
    if flag("KNARR_NO_SANDBOX"):
        results.append(warn("Chromium's sandbox is off (KNARR_NO_SANDBOX)",
                            "A page that exploits the browser then runs with your account's permissions.",
                            "Unset it unless you are in a container that can't run the sandbox."))
    channel = (env.get("KNARR_BROWSER_CHANNEL") or "").strip()
    results.append(info("Browser: " + (f"installed {channel}" if channel else "Playwright's Chromium"),
                        "" if channel else f"Set KNARR_BROWSER_CHANNEL=chrome to use the Chrome you have instead of `{BROWSER_COMMAND}`."))
    state = (env.get("KNARR_STORAGE_STATE") or "").strip()
    if state and not Path(state).expanduser().is_file():
        results.append(fail("KNARR_STORAGE_STATE doesn't name a file", state,
                            "Point it at a Playwright storage-state file, or unset it."))
    elif state:
        results.append(ok("KNARR_STORAGE_STATE", "a saved login will be used"))
    for name in ("KNARR_MAX_TURNS", "KNARR_MAX_WALL_CLOCK_SEC", "KNARR_IDLE_TIMEOUT_SEC"):
        raw = (env.get(name) or "").strip()
        if raw and not (raw.isascii() and raw.isdigit() and int(raw) > 0):
            results.append(warn(f"{name}={raw!r} is not a positive whole number", "It is ignored; the default applies."))
    results.append(_check_runs_dir(env))
    return results


def _check_runs_dir(env: Mapping[str, str]) -> Result:
    raw = (env.get(RUNS_DIR_ENV) or "").strip()
    root = Path(raw).expanduser() if raw else Path.home() / ".knarr" / "runs"
    try:
        from knarr.explorer.files import make_private_dirs

        make_private_dirs(root)
        with tempfile.NamedTemporaryFile(dir=root):
            pass
    except OSError as exc:
        return fail("Can't write the runs directory", f"{root}: {exc}", f"Set {RUNS_DIR_ENV} to a directory you can write.")
    mode = root.stat().st_mode & 0o077
    if mode:
        return warn("The runs directory is readable by others", f"{root}: run output holds logged-in page content.",
                    f"chmod 700 {root}")
    return ok("Runs directory", str(root))


# -- site profiles ------------------------------------------------------------------------------------

def _claude_rules(home: Path, cwd: Path) -> List[Tuple[Path, str]]:
    """The ask and deny rules in the Claude Code settings files that can be read here."""
    found: List[Tuple[Path, str]] = []
    for path in (home / ".claude" / "settings.json", home / ".claude" / "settings.local.json",
                 cwd / ".claude" / "settings.json", cwd / ".claude" / "settings.local.json"):
        try:
            permissions = json.loads(path.read_text(encoding="utf-8")).get("permissions") or {}
        except (OSError, ValueError, AttributeError):
            continue
        for kind in ("ask", "deny"):
            rules = permissions.get(kind) if isinstance(permissions, dict) else None
            found.extend((path, rule) for rule in (rules if isinstance(rules, list) else []) if isinstance(rule, str))
    return found


EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
_RULE = re.compile(r"^\s*([A-Za-z_]+)\s*(?:\((.*)\))?\s*$", re.S)


def _rule_path(specifier: str, home: Path) -> Optional[str]:
    """The absolute path a Claude Code rule names, or None when it can't be told from here.

    ``~/x`` is under the home directory and ``//x`` is absolute. A path that is
    relative (to the settings file or to the working directory) is not resolved:
    the doctor can't tell which directory it means, so it doesn't count it.
    """
    specifier = specifier.strip()
    if specifier == "~" or specifier.startswith("~/"):
        return str(home) + specifier[1:]
    if specifier.startswith("//"):
        return specifier[1:]
    return None


def _glob_regex(pattern: str) -> "re.Pattern[str]":
    """A gitignore-style path pattern: ``*`` stays inside one directory, ``**`` crosses them."""
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z", re.S)


def _rule_covers(rule: str, directory: Path, home: Path) -> bool:
    """Whether ``rule`` (``Edit(~/.knarr/sites/**)``) is about editing files in ``directory``.

    A rule for another tool (Read, Bash, WebFetch) or for another directory doesn't count,
    however much of ``.knarr`` its text mentions.
    """
    found = _RULE.match(rule)
    if not found or found.group(1) not in EDIT_TOOLS:
        return False
    specifier = found.group(2)
    if specifier is None or not specifier.strip():
        return True  # the whole tool: every edit asks, or is refused
    path = _rule_path(specifier, home)
    if path is None:
        return False
    regex = _glob_regex(path.rstrip("/") or "/")
    # Both spellings the profile loader reads; a rule for *.yaml alone leaves .yml files open.
    # A pattern for a parent directory covers the file too, so the parents are tried as well.
    for suffix in (".yaml", ".yml"):
        names = set()
        for base in {os.path.abspath(directory), os.path.realpath(directory)}:
            probe = Path(base) / f"profile{suffix}"
            names |= {str(probe), *(str(parent) for parent in probe.parents)}
        if not any(regex.match(name) for name in names):
            return False
    return True


def _suggested_rule(directory: Path, home: Path) -> str:
    try:
        relative = Path(os.path.abspath(directory)).relative_to(home)
    except ValueError:
        return "an Edit rule that covers that directory"
    return f"Edit(~/{relative.as_posix()}/**)"


def check_profiles(env: Mapping[str, str], home: Optional[Path] = None, cwd: Optional[Path] = None) -> List[Result]:
    """Parse the site profiles, and say whether anything keeps the model from editing them."""
    try:
        from knarr.explorer import profiles
    except Exception as exc:  # most likely PyYAML is missing; the packages check says so
        return [info("Skipped: the profile code can't be loaded", f"{type(exc).__name__}: {exc}")]
    home = home or Path.home()
    directory = profiles.profiles_dir(env)
    if not directory.exists():
        return [info("No site profiles", f"{directory} doesn't exist.",
                     "A profile there lets a site's POST reads through, or sets its limits, without restarting "
                     "the server; see the README, 'Site profiles'.")]
    loaded, problems = profiles.load_profiles(directory)
    results: List[Result] = [warn("A site profile is ignored", problem,
                                  "Fix it; until then it doesn't apply, and writes stay blocked for that site.")
                             for problem in problems]
    for origin, found in sorted(profiles.ambiguous_origins(loaded).items()):
        results.append(warn("Two site profiles name the same origin",
                            f"{', '.join(p.path.name for p in found)}: {origin}; neither applies.",
                            "Remove the origin from one of them."))
    for profile in loaded:
        detail = f"{profile.path.name}: {', '.join(profile.origins)}; {profile.describe_changes()}"
        if profile.live:
            results.append(warn(f"Site profile {profile.name!r} lets every write through", detail,
                                "Remove 'writes: live' to block writes again."))
        else:
            results.append(ok(f"Site profile {profile.name!r}", detail))
    if not loaded and not problems:
        results.append(info("No site profiles", f"{directory} holds none."))
    rules = _claude_rules(home, cwd or Path.cwd())
    covering = [(path, rule) for path, rule in rules if _rule_covers(rule, directory, home)]
    if covering:
        results.append(ok("Claude Code is set to ask or refuse edits to your site profiles",
                          f"{covering[0][1]} in {covering[0][0]}"))
    else:
        near = [rule for _, rule in rules if ".knarr" in rule or str(directory) in rule]
        results.append(warn(
            "Nothing makes Claude Code ask before editing your site profiles",
            f"A model that can write files could widen its own policy in {directory} before the next session."
            + (f" A rule mentions it ({near[0]}) but is not an ask or deny rule for editing files there."
               if near else ""),
            f"Add an ask or deny rule for edits to that directory to your Claude Code permissions "
            f"(for example {_suggested_rule(directory, home)} in ~/.claude/settings.json), and confirm it with "
            "/permissions. Only rules written as ~/path or //path are recognised here. "
            "A shell command may still be able to write there."))
    return results


# -- Chrome you borrow a login from ---------------------------------------------------------------------

def chrome_launch_command(system: Optional[str] = None) -> str:
    system = system or platform.system()
    binary = {"Darwin": '"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"',
              "Linux": "google-chrome"}.get(system, "chrome")
    return (f'{binary} --remote-debugging-port=9222 --user-data-dir="$HOME/.knarr-chrome"')


def check_devtools(env: Mapping[str, str]) -> List[Result]:
    from knarr.explorer import borrow

    url = (env.get("KNARR_CDP_URL") or "").strip()
    results: List[Result] = []
    launch = ("Start Chrome with a debugging port and a separate profile, log in to the site there and keep a tab open on it:\n"
              f"    {chrome_launch_command()}\n    export KNARR_CDP_URL=http://127.0.0.1:9222\n"
              "(Chrome 136 and later ignore the debugging port on your normal profile, hence the separate one.)")
    if not url:
        try:
            borrow._get_json("http://127.0.0.1:9222", "/json/version", timeout=1)
            results.append(info("A Chrome with a debugging port answers on http://127.0.0.1:9222",
                                "KNARR_CDP_URL isn't set, so sessions start logged out.",
                                "export KNARR_CDP_URL=http://127.0.0.1:9222 to borrow its login."))
        except borrow.BorrowError:
            results.append(info("KNARR_CDP_URL isn't set: sessions start logged out",
                                "Borrowing your login from a Chrome is optional (KNARR_STORAGE_STATE is the other way).", launch))
        return results
    try:
        base = borrow._http_base(url)
    except borrow.BorrowError as exc:
        return [fail("KNARR_CDP_URL isn't valid", str(exc),
                     "Use http://127.0.0.1:9222 (or the ws:// address Chrome prints). It must be on this machine.")]
    try:
        version = borrow._get_json(base, "/json/version", timeout=5)
    except borrow.BorrowError as exc:
        return [fail("Can't reach your Chrome", str(exc), launch)]
    results.append(ok(f"Chrome answers at {base}", str(version.get("Browser", "unknown version"))))
    try:
        tabs = [t for t in borrow._get_json(base, "/json/list", timeout=5) if t.get("type") == "page"]
        results.append(ok(f"{len(tabs)} tab(s) open", "localStorage is read from a tab you have open on the site"))
    except borrow.BorrowError as exc:
        results.append(warn("Couldn't list the tabs", str(exc)))
    try:
        answer = borrow._call(version["webSocketDebuggerUrl"], "Storage.getCookies", {}, timeout=5)
        if answer is None or "result" not in answer:
            results.append(fail("Chrome didn't answer the cookie request", "", launch))
        else:
            results.append(ok("Chrome gives up its cookies over DevTools", f"{len(answer['result'].get('cookies', []))} in total; none are printed"))
    except Exception as exc:  # a refused or closed websocket
        results.append(fail("The DevTools connection failed", f"{type(exc).__name__}: {exc}",
                            "Start Chrome with --remote-debugging-port, and no other tool holding it."))
    return results


# -- the browser and the write block ---------------------------------------------------------------------

class _ProbeServer:
    """A local page that reads, then tries to write; records what reaches it."""

    PAGE = ("<!doctype html><title>Knarr doctor</title><h1>Knarr doctor</h1><script>"
            "fetch('/read').then(() => fetch('/write', {method: 'POST', body: 'x=1'}))"
            ".then(() => { window.__probed = true; });</script>")

    def __init__(self) -> None:
        self.hits: List[Tuple[str, str]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _answer(self) -> None:
                outer.hits.append((self.command, self.path))
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                body = (outer.PAGE if self.path == "/" else "{}").encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html" if self.path == "/" else "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = _answer

            def log_message(self, *args: Any) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_port}/"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    @property
    def writes(self) -> List[Tuple[str, str]]:
        return [h for h in self.hits if h[0] != "GET"]


def _explain_browser_failure(exc: BaseException, channel: Optional[str]) -> Result:
    text = str(exc)
    first = next((line for line in text.splitlines() if line.strip()), type(exc).__name__)
    low = text.lower()
    if "executable doesn't exist" in low or "playwright install" in low or ("distribution" in low and "is not found" in low):
        if channel:
            return fail(f"The browser {channel!r} isn't installed", first,
                        f"Install it, or unset KNARR_BROWSER_CHANNEL and run `{BROWSER_COMMAND}`.")
        return fail("Playwright's Chromium isn't installed", first,
                    f"{BROWSER_COMMAND}   (or set KNARR_BROWSER_CHANNEL=chrome to use the Chrome you have)")
    if "sandbox" in low or "has been closed" in low:
        return fail("The browser started and stopped at once", first,
                    "If Chromium's sandbox can't run here (a container, or another sandbox), set KNARR_NO_SANDBOX=1.")
    return fail("The browser couldn't start or load the test page", first)


class _Timeout(Exception):
    pass


def check_browser(env: Mapping[str, str],
                  session: Optional[Callable[..., Any]] = None) -> List[Result]:
    """Start the browser the way a session does, with the write block on, and load a page that writes."""
    from knarr.explorer.write_block import WriteBlock, WritePolicy

    if session is None:
        from knarr.explorer.session import plain_session as session
    channel = (env.get("KNARR_BROWSER_CHANNEL") or "").strip() or None
    server = _ProbeServer()
    block = WriteBlock(WritePolicy(block=True))
    started = time.monotonic()
    use_alarm = hasattr(signal, "SIGALRM") and threading.current_thread() is threading.main_thread()
    if use_alarm:
        def on_alarm(signum: int, frame: Any) -> None:
            raise _Timeout()

        previous = signal.signal(signal.SIGALRM, on_alarm)
        signal.alarm(BROWSER_TEST_SECONDS)
    try:
        with session(server.url, channel=channel, write_block=block) as s:
            version = s.context.browser.version
            s.page.wait_for_function("window.__probed === true", timeout=15000)
            s.page.wait_for_timeout(300)
    except _Timeout:
        return [fail("The browser test timed out", f"No result after {BROWSER_TEST_SECONDS} s.",
                     "Run it again; if it hangs, try KNARR_BROWSER_CHANNEL=chrome.")]
    except Exception as exc:
        return [_explain_browser_failure(exc, channel)]
    finally:
        if use_alarm:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, previous)
        server.close()
    took = time.monotonic() - started
    results = [ok(f"Browser starts: {'installed ' + channel if channel else 'Chromium'} {version}", f"{took:.1f} s with the sandbox on")]
    summary = block.summary()
    if server.writes:
        results.append(fail("The write block let a write through",
                            f"The test page's POST reached the local server: {server.writes}",
                            "Don't use Knarr on a real site until this passes. Update Playwright and the browser, and run this again."))
    elif summary["blocked_writes"] != 1:
        results.append(fail("The write block didn't see the test page's POST",
                            f"blocked_writes={summary['blocked_writes']}", "Update Playwright and the browser, and run this again."))
    else:
        results.append(ok("Write block self-test", "a page's POST was held back, its GET went through"))
    if ("GET", "/read") not in server.hits:
        results.append(fail("The write block stopped a plain GET", str(server.hits)))
    return results


# -- the MCP server ----------------------------------------------------------------------------------------

async def _server_tools() -> List[str]:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(command=sys.executable, args=["-m", "knarr.mcp"], env=dict(os.environ))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()
            return sorted(tool.name for tool in (await client.list_tools()).tools)


def check_server(list_tools: Optional[Callable[[], Any]] = None) -> List[Result]:
    list_tools = list_tools or _server_tools
    try:
        names = asyncio.run(asyncio.wait_for(list_tools(), SERVER_TEST_SECONDS))
    except asyncio.TimeoutError:
        return [fail("The MCP server didn't answer", f"No tool list after {SERVER_TEST_SECONDS} s.",
                     f"Run `{_server_by_hand()}` by hand and read what it prints on stderr.")]
    except Exception as exc:
        return [fail("The MCP server won't start", f"{type(exc).__name__}: {exc}",
                     f"Run `{_server_by_hand()}` by hand and read what it prints on stderr.")]
    needed = {"session_start", "session_end", "read_page", "click_index", "assert_state"}
    missing = needed - set(names)
    if missing:
        return [fail("The MCP server is missing tools", f"Missing: {sorted(missing)}", PACKAGE_FIX)]
    return [ok(f"MCP server starts and lists {len(names)} tools", f"{_server_by_hand()} over stdio")]


def _server_by_hand() -> str:
    """The command that starts the same server this check started: the Python running the doctor."""
    return f"{sys.executable} -m knarr.mcp"


# -- running them -----------------------------------------------------------------------------------------

SYMBOLS = {"ok": "ok  ", "warn": "WARN", "fail": "FAIL", "info": "info"}


def run_checks(env: Mapping[str, str], *, quick: bool = False) -> List[Tuple[str, List[Result]]]:
    sections: List[Tuple[str, List[Result]]] = [
        ("Python and uv", check_python()),
        ("Packages", check_packages()),
        ("Settings", check_settings(env)),
        ("Site profiles", check_profiles(env)),
        ("Chrome login", check_devtools(env)),
    ]
    packages_ok = all(r.status != "fail" for _, rs in sections[:2] for r in rs)
    if quick:
        return sections
    if not packages_ok:
        sections.append(("Browser and write block", [info("Skipped: fix the packages first")]))
        sections.append(("MCP server", [info("Skipped: fix the packages first")]))
        return sections
    sections.append(("Browser and write block", check_browser(env)))
    sections.append(("MCP server", check_server()))
    return sections


def render(sections: List[Tuple[str, List[Result]]]) -> str:
    lines: List[str] = []
    counts = {"ok": 0, "warn": 0, "fail": 0, "info": 0}
    for title, results in sections:
        lines.append(f"{title}")
        for r in results:
            counts[r.status] += 1
            lines.append(f"  {SYMBOLS[r.status]}  {r.title}" + (f" - {r.detail}" if r.detail and "\n" not in r.detail else ""))
            if r.detail and "\n" in r.detail:
                lines.extend(f"        {line}" for line in r.detail.splitlines())
            if r.fix:
                head, *rest = r.fix.splitlines()
                lines.append(f"        fix: {head}")
                lines.extend(f"        {line}" for line in rest)
        lines.append("")
    verdict = ("Everything Knarr needs works here." if not counts["fail"] and not counts["warn"] else
               "Knarr can run here; read the warnings." if not counts["fail"] else
               "Knarr can't run here yet: fix the FAIL lines above and run this again.")
    lines.append(f"{counts['ok']} ok, {counts['warn']} warning(s), {counts['fail']} failure(s). {verdict}")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None, env: Optional[Mapping[str, str]] = None, out: Optional[TextIO] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    out = out or sys.stdout
    if any(a in ("-h", "--help") for a in args):
        out.write(__doc__ or "")
        return 0
    unknown = [a for a in args if a != "--quick"]
    if unknown:
        out.write(f"Unknown option(s): {' '.join(unknown)}. Use --quick or nothing.\n")
        return 2
    sections = run_checks(os.environ if env is None else env, quick="--quick" in args)
    out.write(render(sections) + "\n")
    return 1 if any(r.status == "fail" for _, rs in sections for r in rs) else 0


if __name__ == "__main__":
    sys.exit(main())
