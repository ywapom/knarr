"""The plugin's slash commands: their frontmatter and the commands they tell Claude to run."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from knarr import doctor

COMMANDS = Path(__file__).resolve().parents[2] / "plugin" / "commands"
README = (Path(__file__).resolve().parents[2] / "README.md").read_text()


def parse(name):
    text = (COMMANDS / f"{name}.md").read_text()
    head, body = re.match(r"---\n(.*?)\n---\n(.*)", text, re.S).groups()
    fields = dict(line.split(": ", 1) for line in head.splitlines())
    return fields, body


@pytest.mark.parametrize("name", ["doctor", "install"])
def test_every_command_has_a_description_and_a_narrow_tool_list(name):
    fields, body = parse(name)
    assert fields["description"].strip() and body.strip()
    tools = [t.strip() for t in re.findall(r"Bash\([^)]*\)", fields["allowed-tools"])]
    assert tools and fields["allowed-tools"].count("Bash(") == len(tools)  # nothing but Bash patterns
    for tool in tools:
        assert "pip" not in tool and "install" not in tool and "*" != tool[5:-1], tool


def test_the_install_command_pre_approves_nothing_that_changes_the_machine():
    fields, _ = parse("install")
    allowed = fields["allowed-tools"]
    for word in ("pip", "playwright", "chmod", "rm ", "export", "sudo", "curl", "git "):
        assert word not in allowed, word


def test_the_install_command_uses_the_same_commands_as_the_doctor_and_the_readme():
    _, body = parse("install")
    one_line = re.sub(r"\\\n\s+", "", README)  # the README may wrap a command
    for command in (doctor.INSTALL_COMMAND, doctor.BROWSER_COMMAND, doctor.DOCTOR_COMMAND):
        assert command in body and command in one_line, command


def test_the_install_command_follows_the_agreed_steps_and_limits():
    _, body = parse("install")
    positions = [body.index(marker) for marker in
                 ("1. **uv.**", "2. **The package.**", "3. **A browser.**", "4. **Check.**", "5. **Finish.**")]
    assert positions == sorted(positions)
    assert doctor.DOCTOR_COMMAND in body and "KNARR_BROWSER_CHANNEL=chrome" in body
    assert "Don't edit their settings yourself unless they ask" in body
    assert "don't open any website" in body and "KNARR_ALLOW_WRITES" in body  # named only to forbid it
    assert "--refresh" in body and "$ARGUMENTS" in body


def test_the_doctor_command_runs_only_the_doctor():
    fields, _ = parse("doctor")
    assert fields["allowed-tools"] == f"Bash({doctor.DOCTOR_COMMAND}:*)"


def test_the_plugin_starts_the_server_the_doctor_names():
    import json

    config = json.loads((COMMANDS.parent / ".mcp.json").read_text())["mcpServers"]["knarr"]
    assert " ".join([config["command"], *config["args"]]) == doctor.SERVER_COMMAND
