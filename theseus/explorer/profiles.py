"""Site profiles: the user's settings for one site, read when a session starts.

A profile is a YAML file in ``~/.theseus/sites`` (``THESEUS_PROFILES_DIR``
changes the directory). It belongs to the user, like the ``THESEUS_*``
settings, and the model has no way to name or pick one: the session finds the
profile whose ``origins`` include the start URL's origin. Because it is read at
``session_start``, an edit applies to the next session with no server restart.

    origins: [https://staging.example.com]
    writes: block                   # block (the default) or live
    allow:                          # URL patterns of non-GET requests to send anyway
      - https://staging.example.com/api/v2/search
    authorized_by: you@example.com  # required with writes: live, recorded in summary.json
    forbidden_text: [Send Invoice]  # more verbs to refuse; the defaults stay
    caps: {max_turns: 60, max_wall_clock_sec: 1200}

An ``allow`` entry is a URL pattern, the same grammar as ``THESEUS_WRITE_ALLOW``
(see ``write_block``), and matches any non-GET method to that URL. In a profile
the scheme (http or https) and the host are written out, with no ``*`` in them:
a pattern that matches every host would be ``writes: live`` without its
``authorized_by``, so that is spelled ``writes: live``.

What a profile can do is bounded. It can add allow patterns, switch writes to
``live`` (with ``authorized_by``), add forbidden verbs and set the limits. It
can't remove a default forbidden verb, and it can't overrule the environment:
``THESEUS_BLOCK_WRITES`` still blocks writes, and ``THESEUS_MAX_TURNS`` and
``THESEUS_MAX_WALL_CLOCK_SEC`` still win over its limits.

A profile that can't be used is ignored, never half-applied, which is the
stricter outcome, and the session says so in its start message: a file with an
unknown key, a key written twice or a bad pattern, a file or directory other users can write to (or
that someone else owns), a file that is too large, and two files that name the
same origin.

This protects against the model's tool arguments, not against its file tools:
a client that can write to the profiles directory can widen its own policy. The
README says to keep an ask or deny rule on that directory in Claude Code's
permissions, and ``/theseus:doctor`` checks for one.
"""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple
from urllib.parse import urlsplit

import yaml

from theseus.explorer.origin import normalize_origin
from theseus.explorer.write_block import validate_allow_pattern

PROFILES_DIR_ENV = "THESEUS_PROFILES_DIR"
PROFILE_SUFFIXES = (".yaml", ".yml")

MAX_PROFILE_BYTES = 64 * 1024
MAX_PROFILE_FILES = 100
MAX_ORIGINS = 20
MAX_ALLOW_PATTERNS = 50
MAX_PATTERN_CHARS = 500
MAX_FORBIDDEN_TEXT = 50
MAX_FORBIDDEN_CHARS = 64
MAX_AUTHORIZED_CHARS = 200
MAX_TURNS_LIMIT = 1000
MAX_WALL_CLOCK_LIMIT = 86_400
MAX_PROBLEMS_SHOWN = 3
# A profile is at most three levels deep. PyYAML's parser takes seconds on a few
# thousand nested brackets (a 60 KB file: nearly a minute), so the shape is
# checked first, in a single pass.
MAX_NESTING = 8
MAX_INDENT = 16

KEYS = ("name", "origins", "writes", "allow", "authorized_by", "forbidden_text", "caps")
CAPS_KEYS = ("max_turns", "max_wall_clock_sec")
WRITES_MODES = ("block", "live")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,63}$")
# Before the first "?": http or https, then a host and port with no "*" in them, then a "/".
_LITERAL_HOST = re.compile(r"^https?://[^/*]+/", re.I)


class ProfileError(ValueError):
    """A profile that can't be used; the message says why, without the file name."""


@dataclass(frozen=True)
class SiteProfile:
    name: str
    path: Path
    origins: Tuple[str, ...]
    live: bool = False
    authorized_by: str = ""
    allow: Tuple[str, ...] = ()
    forbidden_text: Tuple[str, ...] = ()
    max_turns: Optional[int] = None
    max_wall_clock_sec: Optional[int] = None

    def caps(self) -> Dict[str, int]:
        """The limits the profile sets, as ``RunCaps.for_explore`` keywords."""
        found: Dict[str, int] = {}
        if self.max_turns:
            found["max_turns"] = self.max_turns
        if self.max_wall_clock_sec:
            found["max_wall_clock_sec"] = self.max_wall_clock_sec
        return found

    def describe_changes(self) -> str:
        parts = []
        if self.live:
            parts.append(f"writes are live (authorized by {self.authorized_by})")
        if self.allow:
            parts.append(f"{len(self.allow)} request pattern(s) are let through")
        if self.forbidden_text:
            parts.append(f"{len(self.forbidden_text)} forbidden verb(s) added")
        if self.caps():
            parts.append("limits set")
        return "; ".join(parts) or "no changes to the defaults"

    def summary(self) -> Dict[str, Any]:
        """What ``summary.json`` records, so a loosened policy is never silent."""
        return {
            "name": self.name,
            "path": str(self.path),
            "origins": list(self.origins),
            "writes": "live" if self.live else "block",
            "allow": list(self.allow),
            "authorized_by": self.authorized_by or None,
            "forbidden_text": list(self.forbidden_text),
            "caps": self.caps(),
        }


@dataclass(frozen=True)
class ProfileLookup:
    profile: Optional[SiteProfile] = None
    problems: Tuple[str, ...] = ()
    directory: Optional[Path] = None


# -- parsing -------------------------------------------------------------------------

def profiles_dir(environ: Optional[Mapping[str, str]] = None) -> Path:
    environ = os.environ if environ is None else environ
    raw = (environ.get(PROFILES_DIR_ENV) or "").strip()
    return Path(raw).expanduser() if raw else Path.home() / ".theseus" / "sites"


def _shown(value: Any, limit: int = 60) -> str:
    """``repr(value)`` cut short, for a message: the file chooses the value."""
    text = repr(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _text(value: Any, label: str, limit: int) -> str:
    """A one-line string: not empty, not too long, no control characters."""
    if not isinstance(value, str) or not value.strip():
        raise ProfileError(f"{label} must be a non-empty string")
    value = value.strip()
    if len(value) > limit:
        raise ProfileError(f"{label} is longer than {limit} characters")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ProfileError(f"{label} must be on one line, with no control characters")
    return value


def _string_list(value: Any, label: str, *, limit: int, item_limit: int) -> List[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ProfileError(f"{label} must be a list")
    if len(value) > limit:
        raise ProfileError(f"{label} has more than {limit} entries")
    return [_text(item, f"each entry of {label}", item_limit) for item in value]


def _origin(item: Any) -> str:
    text = _text(item, "each entry of origins", 200)
    origin = normalize_origin(text)
    if origin is None:
        raise ProfileError(f"origins: {_shown(text)} isn't an http or https origin such as https://app.example.com")
    parts = urlsplit(text)
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ProfileError(f"origins: {_shown(text)} has a path; give only scheme://host[:port]")
    return origin


def _cap(value: Any, label: str, limit: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= limit:
        raise ProfileError(f"caps.{label} must be a whole number from 1 to {limit}")
    return value


def _check_shape(text: str) -> None:
    """Refuse nesting no profile needs, before the YAML parser sees it. One pass over the text."""
    depth = 0
    for line in text.splitlines():
        if len(line) - len(line.lstrip(" ")) > MAX_INDENT:
            raise ProfileError(f"is indented more than {MAX_INDENT} spaces; a profile is not nested that deeply")
        for ch in line:
            if ch in "[{":
                depth += 1
                if depth > MAX_NESTING:
                    raise ProfileError(f"nests brackets more than {MAX_NESTING} deep; a profile does not need that")
            elif ch in "]}" and depth:
                depth -= 1


def _check_literal_host(pattern: str) -> None:
    """An ``allow`` pattern names its scheme and host; a wildcard there is ``writes: live``."""
    if not _LITERAL_HOST.match(pattern.partition("?")[0]):
        raise ProfileError(
            f"allow pattern {_shown(pattern, 80)} must start with http:// or https:// and a host written out, "
            "with no '*' in the host or port; to let every request through, use writes: live with authorized_by"
        )


class _StrictLoader(yaml.SafeLoader):
    """``safe_load`` that refuses a key written twice in one mapping, which would
    otherwise keep the last one silently (``writes: block`` ... ``writes: live``)."""

    def construct_mapping(self, node, deep=False):
        self.flatten_mapping(node)
        seen = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=True)
            if isinstance(key, (list, dict, set)):
                raise ProfileError("a setting name must be a plain word, not a list or a mapping")
            if key in seen:
                raise ProfileError(f"{_shown(key, 40)} is written twice; keep one")
            seen.add(key)
        return super().construct_mapping(node, deep)


def parse_profile(text: str, path: Path) -> SiteProfile:
    """Parse one profile file's text. Raises ``ProfileError``."""
    _check_shape(text)
    try:
        # Aliases are refused before anything is built: a few lines of them expand to gigabytes.
        for event in yaml.parse(text, Loader=yaml.SafeLoader):
            if isinstance(event, yaml.AliasEvent):
                raise ProfileError("must not use YAML aliases (*name); write the value out")
        data = yaml.load(text, Loader=_StrictLoader)
    except ProfileError:
        raise
    except Exception as exc:  # YAMLError, a date such as 2024-13-45 (ValueError), deep nesting (RecursionError)
        first = str(exc).strip().splitlines()[0][:100] if str(exc).strip() else type(exc).__name__
        raise ProfileError(f"isn't valid YAML ({first})") from None
    if not isinstance(data, dict):
        raise ProfileError("must be a mapping of settings, such as origins: [https://app.example.com]")
    unknown = sorted(str(key) for key in data if key not in KEYS)
    if unknown:
        raise ProfileError(f"unknown setting(s) {', '.join(_shown(k, 40) for k in unknown[:5])}"
                           f"{' and more' if len(unknown) > 5 else ''}; known: {', '.join(KEYS)}")

    raw_name = data.get("name")
    if raw_name is None:
        name = re.sub(r"[^A-Za-z0-9._ -]", "_", path.stem).strip("._ -")[:64] or "profile"
    else:
        name = _text(raw_name, "name", 64)
        if not _NAME.match(name):
            raise ProfileError("name may hold only letters, digits, spaces and . _ -")

    raw_origins = data.get("origins")
    if not isinstance(raw_origins, list) or not raw_origins:
        raise ProfileError("origins must be a list with at least one origin, such as https://app.example.com")
    if len(raw_origins) > MAX_ORIGINS:
        raise ProfileError(f"origins has more than {MAX_ORIGINS} entries")
    origins = tuple(dict.fromkeys(_origin(item) for item in raw_origins))

    mode = data.get("writes", "block")
    if mode not in WRITES_MODES:
        raise ProfileError(f"writes must be 'block' or 'live', not {_shown(mode)}")
    live = mode == "live"

    allow = tuple(dict.fromkeys(_string_list(data.get("allow"), "allow", limit=MAX_ALLOW_PATTERNS,
                                             item_limit=MAX_PATTERN_CHARS)))
    for pattern in allow:
        try:
            validate_allow_pattern(pattern, "allow")
        except ValueError as exc:
            raise ProfileError(str(exc)) from None
        _check_literal_host(pattern)
    if live and allow:
        raise ProfileError("allow has no effect with writes: live, which lets every request through; remove one of them")

    authorized_by = ""
    if "authorized_by" in data:
        authorized_by = _text(data["authorized_by"], "authorized_by", MAX_AUTHORIZED_CHARS)
        if not live:
            raise ProfileError("authorized_by only applies to writes: live")
    if live and not authorized_by:
        raise ProfileError("writes: live needs authorized_by: who allowed it (an email or a ticket link); "
                           "it is recorded in summary.json")

    seen = set()
    forbidden = []
    for verb in _string_list(data.get("forbidden_text"), "forbidden_text", limit=MAX_FORBIDDEN_TEXT,
                             item_limit=MAX_FORBIDDEN_CHARS):
        if verb.lower() not in seen:
            seen.add(verb.lower())
            forbidden.append(verb)

    caps = data.get("caps")
    max_turns = max_wall = None
    if caps is not None:
        if not isinstance(caps, dict):
            raise ProfileError("caps must be a mapping, such as caps: {max_turns: 60}")
        extra = sorted(str(key) for key in caps if key not in CAPS_KEYS)
        if extra:
            raise ProfileError(f"unknown caps setting(s) {', '.join(_shown(k, 40) for k in extra[:5])}"
                               f"{' and more' if len(extra) > 5 else ''}; known: {', '.join(CAPS_KEYS)}")
        if "max_turns" in caps:
            max_turns = _cap(caps["max_turns"], "max_turns", MAX_TURNS_LIMIT)
        if "max_wall_clock_sec" in caps:
            max_wall = _cap(caps["max_wall_clock_sec"], "max_wall_clock_sec", MAX_WALL_CLOCK_LIMIT)

    return SiteProfile(name=name, path=path, origins=origins, live=live, authorized_by=authorized_by,
                       allow=allow, forbidden_text=tuple(forbidden), max_turns=max_turns,
                       max_wall_clock_sec=max_wall)


# -- loading -------------------------------------------------------------------------

def _untrusted(info: os.stat_result) -> Optional[str]:
    """Why a file or directory can't stand for the user's own policy (POSIX only)."""
    if os.name != "posix":
        return None
    if info.st_uid != os.getuid():
        return "isn't owned by you"
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        return "can be written by other users (run chmod go-w on it)"
    return None


def load_profiles(directory: Path) -> Tuple[List[SiteProfile], List[str]]:
    """Every usable profile in ``directory``, and a sentence for each file that was ignored."""
    directory = Path(directory)
    try:
        info = directory.stat()
    except FileNotFoundError:
        return [], []
    except OSError as exc:
        return [], [f"{directory} can't be read ({exc.strerror or type(exc).__name__})"]
    reason = _untrusted(info)
    if reason:
        return [], [f"{directory} {reason}, so every profile in it was ignored"]
    try:
        entries = sorted(p for p in directory.iterdir() if p.suffix.lower() in PROFILE_SUFFIXES)
    except OSError as exc:
        return [], [f"{directory} can't be read ({exc.strerror or type(exc).__name__})"]

    profiles: List[SiteProfile] = []
    problems: List[str] = []
    for count, path in enumerate(entries):
        if count >= MAX_PROFILE_FILES:
            problems.append(f"{directory} holds more than {MAX_PROFILE_FILES} profile files; the rest were ignored")
            break
        try:
            if not path.is_file():
                continue
            file_info = path.stat()
            reason = _untrusted(file_info)
            if reason:
                raise ProfileError(f"{reason}, so it was ignored")
            if file_info.st_size > MAX_PROFILE_BYTES:
                raise ProfileError(f"is larger than {MAX_PROFILE_BYTES} bytes, so it was ignored")
            profiles.append(parse_profile(path.read_text(encoding="utf-8"), path))
        except ProfileError as exc:
            problems.append(f"{path.name}: {exc}")
        except (OSError, UnicodeDecodeError) as exc:
            problems.append(f"{path.name}: can't be read ({getattr(exc, 'strerror', None) or type(exc).__name__})")
    return profiles, problems


def ambiguous_origins(profiles: List[SiteProfile]) -> Dict[str, List[SiteProfile]]:
    """Origins that more than one profile names."""
    by_origin: Dict[str, List[SiteProfile]] = {}
    for profile in profiles:
        for origin in profile.origins:
            by_origin.setdefault(origin, []).append(profile)
    return {origin: found for origin, found in by_origin.items() if len(found) > 1}


def find_profile(start_url: str, directory: Optional[Path] = None) -> ProfileLookup:
    """The profile for ``start_url``'s origin, if there is exactly one."""
    directory = Path(directory) if directory is not None else profiles_dir()
    profiles, problems = load_profiles(directory)
    origin = normalize_origin(start_url if isinstance(start_url, str) else "")
    matches = [p for p in profiles if origin in p.origins] if origin else []
    if len(matches) > 1:
        problems.append(f"{', '.join(p.path.name for p in matches)} all name {origin}, so none of them applies")
        matches = []
    return ProfileLookup(profile=matches[0] if matches else None, problems=tuple(problems), directory=directory)


def problems_sentence(problems: Tuple[str, ...]) -> str:
    """The start-message sentence for ignored profiles; empty when there are none."""
    if not problems:
        return ""
    shown = "; ".join(problems[:MAX_PROBLEMS_SHOWN])
    more = f"; and {len(problems) - MAX_PROBLEMS_SHOWN} more" if len(problems) > MAX_PROBLEMS_SHOWN else ""
    return f"Ignored site profile(s): {shown}{more}. Run /theseus:doctor for details."
