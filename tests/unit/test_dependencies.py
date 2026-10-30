"""The dependency ranges in pyproject.toml and the exact versions in requirements.lock agree."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _version(text: str) -> tuple:
    return tuple(int(part) for part in text.split("."))


def _ranges() -> dict:
    """name -> (minimum, ceiling) for every dependency in pyproject.toml."""
    out = {}
    for dep in tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())["project"]["dependencies"]:
        name = re.match(r"[A-Za-z0-9_.-]+", dep).group(0).lower()
        minimum = re.search(r">=\s*([0-9.]+)", dep)
        ceiling = re.search(r"<\s*([0-9.]+)", dep)
        out[name] = (_version(minimum.group(1)) if minimum else None, _version(ceiling.group(1)) if ceiling else None)
    return out


def _lock() -> dict:
    pins = {}
    for line in (REPO_ROOT / "requirements.lock").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        found = re.fullmatch(r"([A-Za-z0-9_.-]+)==([0-9][0-9A-Za-z.+!_-]*)", line)
        assert found, f"not an exact pin: {line!r}"
        name = found.group(1).lower().replace("_", "-")
        assert name not in pins, f"{name} is pinned twice"
        pins[name] = found.group(2)
    return pins


def test_every_dependency_has_a_lower_and_an_upper_bound():
    for name, (minimum, ceiling) in _ranges().items():
        assert minimum is not None and ceiling is not None, f"{name} needs both a floor and a ceiling"


def test_the_lock_pins_every_line_exactly_with_no_duplicates():
    assert len(_lock()) > 10  # the dependencies and what they pull in


def test_each_direct_dependency_is_locked_inside_its_range():
    pins = _lock()
    for name, (minimum, ceiling) in _ranges().items():
        assert name in pins, f"{name} is in pyproject.toml but not in requirements.lock"
        # the numeric part only: 15.0.1 -> (15, 0, 1)
        locked = _version(re.match(r"[0-9]+(\.[0-9]+)*", pins[name]).group(0))
        assert minimum <= locked < ceiling, f"{name}=={pins[name]} is outside its range in pyproject.toml"


def test_the_lock_holds_no_test_tools_and_not_theseus_itself():
    pins = _lock()
    for name in ("pytest", "pluggy", "iniconfig", "theseus-mcp"):
        assert name not in pins, name
