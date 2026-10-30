---
description: Set up what Theseus needs (uv and a browser) one step at a time, then check the result with the doctor
allowed-tools: Bash(uv --version:*), Bash(which uvx:*), Bash(ls -d /Applications/Google Chrome.app:*)
---

Set up Theseus for this user. The plugin carries the MCP configuration and the skill; the server is the Python package `theseus-mcp` from PyPI, which the plugin starts with `uvx theseus-mcp`, and it needs a browser. Go one step at a time, say what each command does before you run it, and let the user's permission prompt decide. Installing is not pre-approved, on purpose.

Arguments: `$ARGUMENTS`. If they contain `upgrade`, add `--refresh` to the uvx command in step 2 (it fetches the newest release instead of the cached one).

1. **uv.** Run `uv --version` and `which uvx`. The plugin starts the server with `uvx`, which fetches the package and a Python 3.12+ for it. If uv is missing, stop and point the user to https://docs.astral.sh/uv/ to install it; installing it is theirs to do. If they would rather not use uv, the README explains adding the server by hand for a Python where they `pip install theseus-mcp`.

2. **The package.** Run this exactly; it downloads `theseus-mcp` into uv's cache and runs a quick check:

       uvx --from theseus-mcp theseus-doctor --quick

   If it fails to download, show the error and what it means; don't try other indexes or other ways to fetch the code. For an upgrade, the same command with `--refresh`:

       uvx --refresh --from theseus-mcp theseus-doctor

3. **A browser.** Theseus runs its own browser, so it needs one of:
   - **The user's Google Chrome** (preferred: nothing to download). Check with `ls -d /Applications/Google Chrome.app` on macOS. Then the user sets `THESEUS_BROWSER_CHANNEL=chrome` in the environment Claude Code starts with: an `env` entry in `~/.claude/settings.json`, or an `export` before starting Claude Code. Show them the line. Don't edit their settings yourself unless they ask you to.
   - **Playwright's Chromium**, a download of about 150 MB: `uvx --from theseus-mcp playwright install chromium`. If the download is blocked by the network sandbox, that is the user's call to allow or not; suggest Chrome instead.

4. **Check.** Run `uvx --from theseus-mcp theseus-doctor` and show the user the report. It checks the packages, the settings, the browser (and proves the write block with a test page) and that the MCP server starts. For each FAIL, give them the one fix it prints.

5. **Finish.** The plugin's MCP server starts when Claude Code does, so if it failed to start earlier, the user should reconnect it with `/mcp` or restart Claude Code. Then mention the optional extra: to start sessions logged in, the doctor explains how to borrow a login from a Chrome (`THESEUS_CDP_URL`).

Don't set `THESEUS_ALLOW_WRITES` or any other write setting, and don't open any website.
