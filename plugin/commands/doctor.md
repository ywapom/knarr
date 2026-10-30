---
description: Check that Theseus can run on this machine (Python, uv, packages, browser, write block, Chrome login) and say how to fix what can't
allowed-tools: Bash(uvx --from theseus-mcp theseus-doctor:*)
---

Run `uvx --from theseus-mcp theseus-doctor $ARGUMENTS` with the Bash tool and show the user the report as it is printed. It is short and written for them; don't boil it down to a summary.

Then:
- For each `FAIL` line, tell the user the one fix the report prints under it.
- For each `WARN` line, say in a sentence what it means for their next session.
- Don't run a fix yourself. Installing packages or a browser, setting environment variables, changing file permissions and starting Chrome are the user's to do.

If the command fails with `uvx: command not found`, uv isn't installed. Point them to step 1 of "Use it from Claude Code" in the Theseus README (or `/theseus:install`), then ask them to run `/theseus:doctor` again.

The argument `--quick` skips starting a browser and the MCP server; use it only if the user asks for a fast check.
