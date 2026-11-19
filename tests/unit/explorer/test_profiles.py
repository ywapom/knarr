"""Unit tests for knarr/explorer/profiles.py — the user's per-site settings."""

from __future__ import annotations

import os
import textwrap
from pathlib import Path

import pytest

from knarr.explorer.profiles import (
    MAX_PROFILE_BYTES,
    PROFILES_DIR_ENV,
    ProfileError,
    ambiguous_origins,
    find_profile,
    load_profiles,
    parse_profile,
    problems_sentence,
    profiles_dir,
)
from knarr.explorer.write_block import (
    ALLOW_WRITES_ENV,
    BLOCK_WRITES_ENV,
    WRITE_ALLOW_ENV,
    WritePolicy,
    validate_allow_pattern,
)

SANDBOX = "https://staging.example.com"
posix_only = pytest.mark.skipif(os.name != "posix", reason="file modes")


def _profile(text: str, name: str = "staging.yaml"):
    return parse_profile(textwrap.dedent(text), Path(name))


def _write(directory: Path, name: str, text: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(textwrap.dedent(text))
    return path


# -- parsing ---------------------------------------------------------------------

def test_a_full_profile_parses():
    p = _profile(f"""
        name: Staging site
        origins: [{SANDBOX}/, HTTPS://Other.Example.com:443]
        allow:
          - {SANDBOX}/api/v2/search/**
        forbidden_text: [Send Invoice, send invoice, Wire Money]
        caps: {{max_turns: 60, max_wall_clock_sec: 1200}}
    """)
    assert p.name == "Staging site"
    assert p.origins == (SANDBOX, "https://other.example.com")
    assert p.allow == (f"{SANDBOX}/api/v2/search/**",)
    assert p.forbidden_text == ("Send Invoice", "Wire Money")  # a repeat in other case is dropped
    assert p.caps() == {"max_turns": 60, "max_wall_clock_sec": 1200}
    assert p.live is False and p.authorized_by == ""


def test_only_origins_is_enough_and_the_name_defaults_to_the_file_name():
    p = parse_profile(f"origins: [{SANDBOX}]", Path("/x/my site!.yaml"))
    assert p.name == "my site" and p.allow == () and p.caps() == {}
    assert p.describe_changes() == "no changes to the defaults"


def test_a_live_profile_records_who_authorized_it():
    p = _profile(f"""
        origins: [{SANDBOX}]
        writes: live
        authorized_by: ron@example.com
    """)
    assert p.live and p.authorized_by == "ron@example.com"
    assert "authorized by ron@example.com" in p.describe_changes()
    assert p.summary()["writes"] == "live" and p.summary()["authorized_by"] == "ron@example.com"


@pytest.mark.parametrize("text, message", [
    ("origins: [", "isn't valid YAML"),
    ("- a\n- b\n", "must be a mapping"),
    ("", "must be a mapping"),
    ("origins: [https://a.example.com]\nalow: []\n", r"unknown setting\(s\) 'alow'"),
    ("writes: block\n", "origins must be a list"),
    ("origins: []\n", "origins must be a list"),
    ("origins: https://a.example.com\n", "origins must be a list"),
    ("origins: [a.example.com]\n", "isn't an http or https origin"),
    ("origins: [ftp://a.example.com]\n", "isn't an http or https origin"),
    ("origins: [https://a.example.com/app]\n", "has a path"),
    ("origins: ['https://a.example.com/?x=1']\n", "has a path"),
    ("origins: [42]\n", "non-empty string"),
    ("origins: [https://a.example.com]\nwrites: allow\n", "writes must be 'block' or 'live'"),
    ("origins: [https://a.example.com]\nwrites: on\n", "writes must be 'block' or 'live'"),
    ("origins: [https://a.example.com]\nwrites: true\n", "writes must be 'block' or 'live'"),
    ("origins: [https://a.example.com]\nwrites: live\n", "needs authorized_by"),
    ("origins: [https://a.example.com]\nwrites: live\nauthorized_by: '  '\n", "non-empty string"),
    ("origins: [https://a.example.com]\nauthorized_by: me\n", "only applies to writes: live"),
    ("origins: [https://a.example.com]\nwrites: live\nauthorized_by: me\nallow: ['https://a.example.com/x']\n", "no effect with writes: live"),
    ("origins: [https://a.example.com]\nauthorized_by: [me]\nwrites: live\n", "non-empty string"),
    ("origins: [https://a.example.com]\nallow: '**/x'\n", "allow must be a list"),
    ("origins: [https://a.example.com]\nallow: [7]\n", "non-empty string"),
    ("origins: [https://a.example.com]\nallow: ['']\n", "non-empty string"),
    ("origins: [https://a.example.com]\nallow: ['example.com/x']\n", "allow pattern 'example.com/x' can't match any URL"),
    ("origins: [https://a.example.com]\nallow: ['**/a/**/b/**/c']\n", "allow pattern .* more than 2 wildcards"),
    ("origins: [https://a.example.com]\nallow: ['https://a.example.com']\n", "has no path"),
    ("origins: [https://a.example.com]\nforbidden_text: Delete\n", "forbidden_text must be a list"),
    ("origins: [https://a.example.com]\nforbidden_text: ['']\n", "non-empty string"),
    ("origins: [https://a.example.com]\nforbidden_text: [{a: b}]\n", "non-empty string"),
    ("origins: [https://a.example.com]\ncaps: 5\n", "caps must be a mapping"),
    ("origins: [https://a.example.com]\ncaps: {max_tokens: 5}\n", r"unknown caps setting\(s\) 'max_tokens'"),
    ("origins: [https://a.example.com]\ncaps: {max_turns: 0}\n", "caps.max_turns must be a whole number"),
    ("origins: [https://a.example.com]\ncaps: {max_turns: true}\n", "caps.max_turns must be a whole number"),
    ("origins: [https://a.example.com]\ncaps: {max_turns: 2.5}\n", "caps.max_turns must be a whole number"),
    ("origins: [https://a.example.com]\ncaps: {max_turns: 100000}\n", "caps.max_turns must be a whole number"),
    ("origins: [https://a.example.com]\ncaps: {max_wall_clock_sec: -1}\n", "caps.max_wall_clock_sec"),
    ("origins: [https://a.example.com]\nname: 'a/b'\n", "name may hold only"),
    ("origins: [https://a.example.com]\nname: ''\n", "non-empty string"),
])
def test_an_unusable_profile_is_refused_with_the_reason(text, message):
    with pytest.raises(ProfileError, match=message):
        parse_profile(text, Path("p.yaml"))


def test_text_with_control_characters_or_a_line_break_is_refused():
    with pytest.raises(ProfileError, match="one line"):
        parse_profile('origins: [https://a.example.com]\nwrites: live\nauthorized_by: "me\\nand you"\n', Path("p.yaml"))
    with pytest.raises(ProfileError, match="one line"):
        parse_profile('origins: [https://a.example.com]\nforbidden_text: ["a\\u0007b"]\n', Path("p.yaml"))


def test_too_many_entries_are_refused():
    many = "\n".join(f"  - https://a.example.com/p{i}/*" for i in range(51))
    with pytest.raises(ProfileError, match="more than 50 entries"):
        parse_profile(f"origins: [https://a.example.com]\nallow:\n{many}\n", Path("p.yaml"))
    with pytest.raises(ProfileError, match="longer than"):
        parse_profile(f"origins: [https://a.example.com]\nallow: ['https://a.example.com/{'x' * 600}']\n", Path("p.yaml"))


def test_a_profile_cant_take_a_default_forbidden_verb_away_because_it_has_no_such_setting():
    with pytest.raises(ProfileError, match="unknown setting"):
        _profile(f"origins: [{SANDBOX}]\nforbidden_text_remove: [Delete]\n")


def test_an_allow_pattern_error_names_the_profile_not_the_environment():
    with pytest.raises(ValueError, match=r"^KNARR_WRITE_ALLOW pattern"):
        validate_allow_pattern("nope")
    with pytest.raises(ValueError, match=r"^allow pattern 'nope'"):
        validate_allow_pattern("nope", "allow")


# A pattern that matches every host is "writes: live" without its authorized_by.
EVERY_HOST = ["**", "*", "**/x", "*/x", "https://*/**", "https://*/*", "http://*/x", "*://a.example.com/x",
              "https://*.example.com/x", "https://a.example.com:*/x", "https://a.*.com/x", "https://*a.example.com/x",
              "https://*/x?a=1", "https://*:8443/x"]


@pytest.mark.parametrize("pattern", EVERY_HOST)
def test_an_allow_pattern_must_write_the_scheme_and_host_out(pattern):
    with pytest.raises(ProfileError, match=r"must start with http:// or https:// and a host written out"):
        _profile(f"origins: [{SANDBOX}]\nallow: [{pattern!r}]\n")


@pytest.mark.parametrize("pattern", ["https://*/**", "https://*.example.com/x", "https://a.example.com:*/x"])
def test_the_rule_is_the_profiles_the_environments_patterns_keep_their_wildcards(pattern):
    validate_allow_pattern(pattern)  # KNARR_WRITE_ALLOW is the user's own server setting, unchanged


@pytest.mark.parametrize("pattern", [
    f"{SANDBOX}/api/v2/search/journeys",
    f"{SANDBOX}/api/**",
    f"{SANDBOX}/api/*/search?q=*",
    "https://api.other.example.com/v1/**",  # another host is fine when it is written out
    "http://localhost:8080/graphql",
    "https://[::1]:8443/x",
    "HTTPS://A.Example.com/x",
])
def test_an_allow_pattern_with_a_host_written_out_is_accepted(pattern):
    assert _profile(f"origins: [{SANDBOX}]\nallow: [{pattern!r}]\n").allow == (pattern,)


# -- a setting written twice is an error, not "the last one wins" ---------------------

@pytest.mark.parametrize("text, key", [
    (f"origins: [{SANDBOX}]\nwrites: block\nwrites: live\nauthorized_by: me\n", "writes"),
    (f"origins: [{SANDBOX}]\nallow: ['{SANDBOX}/a']\nallow: ['{SANDBOX}/b']\n", "allow"),
    (f"origins: [{SANDBOX}]\norigins: [https://b.example.com]\n", "origins"),
    (f"origins: [{SANDBOX}]\ncaps: {{max_turns: 5, max_turns: 500}}\n", "max_turns"),
    (f"origins: [{SANDBOX}]\ncaps:\n  max_turns: 5\n  max_turns: 6\n", "max_turns"),
    (f"origins: [{SANDBOX}]\ncaps: {{<<: {{max_turns: 5}}, max_turns: 6}}\n", "max_turns"),
    (f"origins: [{SANDBOX}]\n'writes': block\nwrites: live\nauthorized_by: me\n", "writes"),
])
def test_a_setting_written_twice_makes_the_profile_unusable(text, key):
    with pytest.raises(ProfileError, match=rf"'{key}' is written twice"):
        parse_profile(text, Path("p.yaml"))


def test_a_setting_name_that_is_a_list_is_refused_not_a_crash():
    with pytest.raises(ProfileError, match="setting name must be a plain word"):
        parse_profile(f"origins: [{SANDBOX}]\n? [writes]\n: live\n", Path("p.yaml"))


def test_a_profile_with_a_duplicate_is_ignored_when_loaded(tmp_path):
    d = tmp_path / "sites"
    _write(d, "twice.yaml", f"origins: [{SANDBOX}]\nwrites: block\nwrites: live\nauthorized_by: me\n")
    d.chmod(0o700)
    profiles, problems = load_profiles(d)
    assert profiles == [] and problems == ["twice.yaml: 'writes' is written twice; keep one"]


# -- loading ---------------------------------------------------------------------

def test_a_missing_directory_has_no_profiles_and_no_problems(tmp_path):
    assert load_profiles(tmp_path / "nope") == ([], [])


def test_a_directory_that_is_a_file_is_a_problem_not_a_crash(tmp_path):
    (tmp_path / "sites").write_text("x")
    profiles, problems = load_profiles(tmp_path / "sites")
    assert profiles == [] and len(problems) == 1 and "can't be read" in problems[0]


def test_only_yaml_files_are_read_and_a_broken_one_does_not_hide_a_good_one(tmp_path):
    d = tmp_path / "sites"
    _write(d, "good.yaml", f"origins: [{SANDBOX}]\n")
    _write(d, "also.yml", "origins: [https://b.example.com]\n")
    _write(d, "bad.yaml", "origins: [https://c.example.com]\ntypo: 1\n")
    _write(d, "notes.txt", "not a profile")
    (d / "folder.yaml").mkdir()
    profiles, problems = load_profiles(d)
    assert sorted(p.path.name for p in profiles) == ["also.yml", "good.yaml"]
    assert len(problems) == 1 and problems[0].startswith("bad.yaml: unknown setting")


def test_a_file_that_isnt_utf8_is_a_problem(tmp_path):
    d = tmp_path / "sites"
    d.mkdir()
    (d / "x.yaml").write_bytes(b"origins: [\xff\xfe]\n")
    profiles, problems = load_profiles(d)
    assert profiles == [] and problems[0].startswith("x.yaml: ")


def test_a_huge_file_is_ignored_without_being_read(tmp_path):
    d = tmp_path / "sites"
    _write(d, "big.yaml", "origins: [https://a.example.com]\n# " + "x" * MAX_PROFILE_BYTES + "\n")
    profiles, problems = load_profiles(d)
    assert profiles == [] and "is larger than" in problems[0]


@posix_only
def test_a_file_other_users_can_write_is_ignored(tmp_path):
    d = tmp_path / "sites"
    path = _write(d, "shared.yaml", f"origins: [{SANDBOX}]\nallow: ['{SANDBOX}/x']\n")
    d.chmod(0o700)
    path.chmod(0o664)
    profiles, problems = load_profiles(d)
    assert profiles == [] and "can be written by other users" in problems[0] and problems[0].startswith("shared.yaml")
    path.chmod(0o666)
    assert load_profiles(d)[0] == []
    path.chmod(0o644)
    assert [p.path.name for p in load_profiles(d)[0]] == ["shared.yaml"]


@posix_only
def test_a_directory_other_users_can_write_ignores_every_profile_in_it(tmp_path):
    d = tmp_path / "sites"
    _write(d, "a.yaml", f"origins: [{SANDBOX}]\n")
    d.chmod(0o777)
    profiles, problems = load_profiles(d)
    assert profiles == [] and "can be written by other users" in problems[0] and "every profile" in problems[0]


@posix_only
def test_a_file_owned_by_someone_else_is_ignored(tmp_path, monkeypatch):
    d = tmp_path / "sites"
    _write(d, "a.yaml", f"origins: [{SANDBOX}]\n")
    d.chmod(0o700)
    monkeypatch.setattr(os, "getuid", lambda: os.stat(d).st_uid + 1)
    profiles, problems = load_profiles(d)
    assert profiles == [] and "isn't owned by you" in problems[0]


# -- finding the profile for a start URL -----------------------------------------------

def test_the_profile_is_found_by_the_start_urls_origin_not_by_a_name(tmp_path):
    d = tmp_path / "sites"
    _write(d, "staging.yaml", f"origins: [{SANDBOX}]\n")
    _write(d, "other.yaml", "origins: [https://other.example.com]\n")
    for url in (f"{SANDBOX}/anything?x=1", "HTTPS://STAGING.example.com:443/", f"  {SANDBOX}  "):
        assert find_profile(url, d).profile.path.name == "staging.yaml"
    assert find_profile("https://staging.example.com:8443/", d).profile is None  # another port
    assert find_profile("http://staging.example.com/", d).profile is None  # another scheme
    assert find_profile("https://evil.example.com/", d).profile is None
    assert find_profile("not a url", d).profile is None
    assert find_profile(None, d).profile is None


def test_two_profiles_for_one_origin_means_neither_applies(tmp_path):
    d = tmp_path / "sites"
    _write(d, "a.yaml", f"origins: [{SANDBOX}]\nallow: ['{SANDBOX}/x']\n")
    _write(d, "b.yaml", f"origins: [{SANDBOX}]\n")
    found = find_profile(SANDBOX, d)
    assert found.profile is None
    assert "a.yaml, b.yaml all name" in found.problems[0] and "none of them applies" in found.problems[0]
    assert set(ambiguous_origins(load_profiles(d)[0])) == {SANDBOX}


def test_the_directory_comes_from_the_environment(tmp_path, monkeypatch):
    d = tmp_path / "mine"
    _write(d, "a.yaml", f"origins: [{SANDBOX}]\n")
    monkeypatch.setenv(PROFILES_DIR_ENV, str(d))
    assert profiles_dir() == d
    assert find_profile(SANDBOX).profile is not None
    monkeypatch.setenv(PROFILES_DIR_ENV, "  ")
    assert profiles_dir() == Path.home() / ".knarr" / "sites"


def test_the_problems_sentence_is_short():
    assert problems_sentence(()) == ""
    one = problems_sentence(("a.yaml: bad",))
    assert one.startswith("Ignored site profile(s): a.yaml: bad.") and "/knarr:doctor" in one
    many = problems_sentence(tuple(f"{i}.yaml: bad" for i in range(7)))
    assert "0.yaml" in many and "2.yaml" in many and "3.yaml" not in many and "and 4 more" in many


# -- the write policy a profile feeds ---------------------------------------------------

def test_a_profile_adds_patterns_after_the_environments_without_repeats(monkeypatch):
    monkeypatch.setenv(WRITE_ALLOW_ENV, "**/a, **/b")
    policy = WritePolicy.from_env(extra_allow=("**/b", "**/c"))
    assert policy.allow_patterns == ("**/a", "**/b", "**/c") and policy.block is True


def test_a_live_profile_lets_writes_through_unless_the_environment_blocks_them(monkeypatch):
    assert WritePolicy.from_env(live=True).block is False
    assert WritePolicy.from_env().block is True
    monkeypatch.setenv(BLOCK_WRITES_ENV, "1")
    assert WritePolicy.from_env(live=True).block is True
    monkeypatch.setenv(BLOCK_WRITES_ENV, "garbled")  # a value nobody can read counts as on
    assert WritePolicy.from_env(live=True).block is True
    monkeypatch.setenv(BLOCK_WRITES_ENV, "0")
    monkeypatch.setenv(ALLOW_WRITES_ENV, "1")
    assert WritePolicy.from_env().block is False  # the environment alone still works as before


# -- files that are hostile or just odd ---------------------------------------------------

def test_yaml_that_raises_something_other_than_a_yaml_error_is_still_a_profile_error():
    with pytest.raises(ProfileError, match="isn't valid YAML"):
        parse_profile("origins: [https://a.example.com]\nname: 2024-13-45\n", Path("p.yaml"))  # a date that isn't one


@pytest.mark.parametrize("text, message", [
    ("[" * 20000, "nests brackets more than 8 deep"),
    ("{a: " * 20000, "nests brackets more than 8 deep"),
    ("origins: [" + "[" * 60000 + "\n", "nests brackets more than 8 deep"),
    ("a:\n" + "".join(" " * (2 * i) + f"k{i}:\n" for i in range(1, 60)), "is indented more than 16 spaces"),
])
def test_deeply_nested_yaml_is_refused_at_once_not_after_the_parser_chokes(text, message):
    import time

    started = time.monotonic()
    with pytest.raises(ProfileError, match=message):
        parse_profile(text, Path("p.yaml"))
    assert time.monotonic() - started < 1.0  # the parser itself takes ~15 s on 20,000 brackets


def test_ordinary_nesting_and_brackets_inside_a_pattern_are_fine():
    p = _profile("""
        origins: [https://a.example.com]
        allow:
          - https://a.example.com/api/items[1]/**
        caps: {max_turns: 3}
    """)
    assert p.allow == ("https://a.example.com/api/items[1]/**",) and p.max_turns == 3


def test_aliases_are_refused_before_they_can_expand():
    bomb = ("a: &a [x, x, x, x, x, x, x, x, x]\nb: &b [*a, *a, *a, *a, *a, *a, *a, *a, *a]\n"
            "c: &c [*b, *b, *b, *b, *b, *b, *b, *b, *b]\norigins: [https://a.example.com]\n")
    with pytest.raises(ProfileError, match="must not use YAML aliases"):
        parse_profile(bomb, Path("p.yaml"))
    with pytest.raises(ProfileError, match="must not use YAML aliases"):
        parse_profile("origins: &o [https://a.example.com]\nallow: *o\n", Path("p.yaml"))


def test_one_odd_file_never_breaks_loading_the_others(tmp_path):
    d = tmp_path / "sites"
    _write(d, "good.yaml", f"origins: [{SANDBOX}]\n")
    _write(d, "date.yaml", "origins: [https://b.example.com]\nname: 2024-13-45\n")
    _write(d, "deep.yaml", "[" * 20000)  # refused by the shape check; the parser would take 15 s
    _write(d, "alias.yaml", "a: &a [x]\nb: *a\n")
    _write(d, "empty.yaml", "")
    _write(d, "scalar.yaml", "just text")
    profiles, problems = load_profiles(d)
    assert [p.path.name for p in profiles] == ["good.yaml"]
    assert sorted(m.split(":")[0] for m in problems) == ["alias.yaml", "date.yaml", "deep.yaml", "empty.yaml", "scalar.yaml"]


def test_messages_do_not_echo_unbounded_values():
    huge = "[" + ", ".join(f"'v{i}'" for i in range(5000)) + "]"
    with pytest.raises(ProfileError) as info:
        parse_profile(f"origins: [https://a.example.com]\nwrites: {huge}\n", Path("p.yaml"))
    assert len(str(info.value)) < 200
    keys = "\n".join(f"k{i}{'x' * 200}: 1" for i in range(50))
    with pytest.raises(ProfileError) as info:
        parse_profile(f"origins: [https://a.example.com]\n{keys}\n", Path("p.yaml"))
    assert len(str(info.value)) < 500 and "and more" in str(info.value)
