# Theseus

Theseus lets an AI agent work through a website toward a goal you set, using a small set of guarded browser tools. The run passes only when the page shows the success criterion fixed at the start; the agent can't change it partway or grade itself with other text. It works on any site you can open in a browser, including ones you're logged in to.

It runs as an MCP server: Claude (in Claude Code, or any MCP client) drives the browser tools in your conversation.

## How it works

You give Theseus a start URL, a goal and a success criterion. Each turn the agent:

1. Reads the page as a numbered list of controls, e.g. `[3] button 'Place order'`.
2. Acts: clicks by number or by text, fills a field, submits, navigates or waits.
3. Repeats until `assert_state` finds the success criterion on the page, the agent gives up, or a limit is reached.

Guards around every action:

- **URL boundary.** Navigation must stay on the start URL's origin, or inside a manifest's URL patterns. From Claude Code, the page's real origin is checked before and after every call. If your own action (a click, a fill, a submit) took the page elsewhere, it's sent back to the start page and counts as a violation; three end the run. If the page went elsewhere by itself, such as a late redirect to a sign-in page, Theseus lets the sign-in finish, follows it again with your login, and counts no violation. A site that keeps sending the page to the same place ends the run as `login_required`, and a new session can start once you've signed in. The guard returns only to a page Theseus chose to load (the start page), never to one a click landed on, so it can't repeat a link that changes data.
- **Forbidden actions.** Actions on destructive or irreversible verbs (Delete, Archive, Publish, Approve, Send Request and others) are refused, including a `submit` that would hit a button such as "Save and Publish". A [site profile](#site-profiles) can add verbs for one site (a shop's "Buy now", say). Three refusals end the run.
- **Writes blocked** by default. Requests that could change data (anything but GET, HEAD and OPTIONS, plus outgoing WebSocket messages) are answered with a fake success and recorded instead of being sent. Clicks still really happen, so the agent can explore; nothing is saved.
- **Its own browser.** Theseus always runs its own browser, so every request, new tabs included, goes through the write block, and it's closed at the end.
- **Limits.** Turns (default 30), wall clock (15 minutes), tokens (500k) and memory.

Each run writes a record of every tool call (`trace.jsonl`) and a `summary.json`. The record keeps what you typed into a field out of the file (it holds the field's label and the length of the text, since a field may be a password), and removes the text from error messages that repeat it. A URL in either file loses its `user:password@`, the values of parameters named like credentials (`code`, `token`, `session`, `password` and the like) and values that look like random tokens, in the query and in the `#` part; the parameter names stay. Each trace line has its own UTC time (`at`), and `summary.json` says who ran the session (`user`, the operating-system account) and when it started and ended (`started_at`, `ended_at`). With `THESEUS_TRACE=1` it also writes a Playwright trace you can open with `python3 -m playwright show-trace trace.zip`; traces record page contents, typed text, request headers and bodies, and cookies, so they hold the session's login and whatever was typed.

## Use it from Claude Code

If you use the plugin (step 2), `/theseus:install` walks through steps 1 and 3 with you, one command at a time, and ends with `/theseus:doctor`.

**1. Install uv.** The plugin starts the server with `uvx theseus-mcp`, which fetches the [`theseus-mcp`](https://pypi.org/project/theseus-mcp/) package from PyPI, with a Python 3.12+ for it, the first time it runs. See [docs.astral.sh/uv](https://docs.astral.sh/uv/) to install uv. To fetch a newer release later:

    uvx --refresh --from theseus-mcp theseus-doctor

**2. Add it to Claude Code**, either as a plugin (MCP server plus a skill that teaches Claude how to use it):

    /plugin marketplace add OWNER/theseus-mcp
    /plugin install theseus@theseus-mcp

or as a bare MCP server:

    claude mcp add theseus -- uvx theseus-mcp

**3. Give it a browser.** Either download Playwright's Chromium, for the Playwright version Theseus uses:

    uvx --from theseus-mcp playwright install chromium

or use the Google Chrome you already have, by setting `THESEUS_BROWSER_CHANNEL=chrome` in the environment Claude Code starts with (an `env` entry in `~/.claude/settings.json`, or an `export` before you start it). Chrome needs no download.

**Without uv.** Install the package with pip into a Python 3.12 or newer, and add the server for that Python:

    python3 -m pip install theseus-mcp
    python3 -m playwright install chromium
    claude mcp add theseus -- python3 -m theseus.mcp

The ranges in `pyproject.toml` all have an upper bound, and `requirements.lock` holds the exact versions the whole test suite passed on (mcp 1.30.0, Playwright 1.64.1 and what they pull in). To install exactly those, from a clone: `python3 -m pip install -r requirements.lock`, then `python3 -m pip install --no-deps .`. The doctor fails if an installed package is outside the ranges.

Then ask Claude something like *"Use Theseus to check that placing an order on http://localhost:3000 shows 'Order placed'"*.

Claude calls the tools in this order:
1. `session_start` with the start URL, the goal and a **success criterion**: the text that will be on the page when the goal is reached, in quotes, e.g. `the page shows 'Order placed'`. It's required and can't be changed during the session.
2. `read_page`, `click_index`, `fill` and the other browser tools.
3. `assert_state`, which checks the page against that criterion (every quoted phrase must be visible), or `give_up`.
4. `session_end`, which returns the summary and the run directory.

Every tool result ends with a status line, e.g. `[theseus] call 4 of 30, 880 s of browser time left`. Once the run ends, further calls are refused.

### Check your setup

Run `/theseus:doctor` in Claude Code, or `uvx --from theseus-mcp theseus-doctor` in a terminal (`theseus-doctor` if you installed with pip). It checks:
- the Python that runs it, and that `uvx`, which the plugin starts the server with, is on your PATH;
- the installed packages and their versions;
- your `THESEUS_*` settings (a misspelt name, a bad allow pattern, a missing saved-login file, writes allowed, tracing on);
- the browser: it starts one the way a session does, loads a local page that tries to write, and checks that the write block held the write back;
- the Chrome you borrow a login from (`THESEUS_CDP_URL`): that it answers, how many tabs are open, and that it hands over its cookies (only a count is printed). If it isn't set up, it prints the command to start Chrome;
- that the MCP server starts and lists its tools.

Each problem comes with the fix. `--quick` skips the browser and the server. It never opens a site, and exits 1 if anything failed.

The server runs one session at a time per conversation. Run output goes to `~/.theseus/runs/<run-id>/`.

The model chooses the start URL and the criterion. Everything after that is fixed by the session and your environment:
- the origin the start URL lands on, after any redirect;
- the destructive-verb list;
- the limits in the variables below.

The time limit counts only time the browser spends on tool calls, so waiting for you to approve a call doesn't use it up. A session with no calls for 15 minutes is closed. If a guard stops a session (too many boundary or forbidden-action violations), no new session can start in that conversation.

## Turn a walkthrough into a Playwright project

Theseus drives the browser; Claude reads the pages, decides what to do next and writes the code. Together they can walk a flow once and leave you a script that repeats it. For example, ask Claude:

> Use Theseus to explore https://app.example.com. Walk the flow from the login page to the submit form, and write down each page's fields and buttons. Don't submit anything real. Then write a Python Playwright project that logs in, fills the form and submits it, using role and label locators.

Theseus lists each page as numbered controls with their role, label and visible text (`[3] button 'Place order'`). Claude clicks and fills its way through, then writes the project from what it saw. Every step is recorded in `trace.jsonl` in the run folder, so you can check the script against what actually happened.

What to know before you try it:
- **Login.** Theseus never logs in itself, and with writes blocked the login form's POST is faked. Either [borrow a login from your Chrome](#using-a-site-youre-logged-in-to), allow just that request with `THESEUS_WRITE_ALLOW` (use a test account), or have Claude write the login step from the login page's fields without running it.
- **Submit.** The final submit is faked, so you can walk up to it and see the form, but nothing is saved and `assert_state` can't confirm the real result. Don't use `read_only` for this: it fakes every click, so Claude couldn't move between pages.
- **Selectors.** Theseus reports controls by role, label and text, not CSS selectors or test ids. That suits Playwright's `get_by_role` and `get_by_label`. A `data-testid` has to come from you.
- **The generated script isn't guarded.** Theseus's write block applies to Theseus's browser only. When you run the script against the real site, its submit really happens: use a test account or a test environment.

## Using a site you're logged in to

Theseus doesn't log in for you. Either let it borrow the login from a Chrome you've already logged in to, or give it a saved login.

**Borrow your login from Chrome.** Start Chrome with a debugging port and a separate profile directory, log in to the site there, and keep a tab open on it:

    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
        --remote-debugging-port=9222 --user-data-dir="$HOME/.theseus-chrome"

    export THESEUS_CDP_URL=http://127.0.0.1:9222

When a session starts, Theseus asks that Chrome, over a short DevTools connection, for two things, and keeps them in memory:
- the cookies Chrome would send to the start site;
- the site's localStorage, read from your open tab on it.

Then it launches its own browser with that copy. Single sign-on works the same way: if the page moves to other origins by itself (a redirect to your company's sign-in page, whether from the server or a script), Theseus borrows each origin's login in turn and loads the start URL again, up to four times, so the sign-in can finish. Before it starts, Theseus waits for the page to finish loading (a slow page included, up to a limit), for its requests to go quiet, and for a `Refresh` header or meta refresh to another origin to fire. A redirect from a timer or a slow script can't be seen in advance; Theseus follows it when it happens. Each reload of the sign-in loads the start URL again, so anything the page does on load (a request that `THESEUS_WRITE_ALLOW` lets through, a GET that changes data) happens once per reload; pick a start URL that doesn't. If the sign-in is longer than four reloads, the start message says which origin's login wasn't borrowed. If you're not signed in at the provider, the session stays on the page it settled on (the sign-in page), the start message says where that is, and nothing counts as a violation.

Your tabs and any dialogs in them are left alone. A tab showing a dialog can't answer, so after 2 seconds Theseus skips its localStorage and says so; it doesn't wait. Cookies for other sites, sessionStorage and IndexedDB aren't copied. The start message says exactly what was borrowed, and says plainly when nothing was.

The session gets your login for the start site and for the sign-in sites the page sends it through, so approve a `session_start` only for a site you'd open with that login.

The DevTools connection is direct, so an `HTTP_PROXY` in your environment doesn't apply to it. `THESEUS_CDP_URL` may be written as `http://127.0.0.1:9222` or as the `ws://` address Chrome prints. **It must be on your own machine** (`127.0.0.1`, `localhost` or `[::1]`, without a user name or password); anything else is refused and the session does not start, because anyone who can reach a Chrome debugging port controls that Chrome and every login in it, and Theseus asks it for cookies. The same rule holds for the WebSocket addresses Chrome's replies name, and a redirect from the DevTools address is not followed. Theseus never opens that endpoint as a page, whatever the spelling (`localhost`, `localhost.`, `127.1`, `[::1]`), and no request from its browser reaches it, whatever the page or the request type (a fetch, an `<img>`, a redirect): a page there could list, open and close your tabs. A refused request is noted in the result.

This works for sites that keep their login in cookies or localStorage, which covers most. If a site keeps it somewhere Theseus can't copy, it will start logged out. Logging in inside Theseus's browser won't work while writes are blocked, because the login form's POST is blocked too. In that case, either:
- let the login request through with `THESEUS_WRITE_ALLOW`;
- or set `THESEUS_ALLOW_WRITES=1` for that session.

Some single sign-on hands the login back to the site with a POST (SAML and OpenID Connect `form_post` do). While writes are blocked, that POST is blocked too, so the session would start on a "Blocked by Theseus" page; the start message says so. Allow that one request, e.g. `THESEUS_WRITE_ALLOW=https://app.example.com/saml/acs`.

With Claude Code, set the variable in the shell you start Claude Code from.

**Saved login.** Point `THESEUS_STORAGE_STATE` at a Playwright storage-state file. Theseus launches a browser with those cookies.

## Safety

**Writes are blocked by default.** In Theseus's browser, every request other than GET, HEAD and OPTIONS is faked and recorded, not sent, along with outgoing WebSocket messages from pages. The block works at the browser level, so it covers:
- fetch and XHR;
- form posts, which show a "Blocked by Theseus" page;
- `sendBeacon`;
- iframes, including cross-site ones;
- new tabs the page opens;
- the requests current Chrome sends itself while a page unloads (`sendBeacon` and `fetch(keepalive)` on leaving a page or closing it).

A faked response to a cross-origin request carries the CORS headers that let the page read it, so the page sees a success, not a network error. A GET, HEAD or OPTIONS request that carries a body counts as a write.

Only the request types that can carry a write are paused (documents, XHR, fetch, pings, CSP reports and "other"). Images, scripts, stylesheets, fonts and media are only fetched with GET, so pages load at nearly normal speed.

Channels whose traffic can't be intercepted are shut off. Service workers are blocked. SharedWorker and WebSocketStream are removed by a browser flag in every frame and worker. WebTransport and RTCPeerConnection are disabled by a script in each page frame.

Theseus runs Chromium with its sandbox on. If that can't start where you run it (a container, say), set `THESEUS_NO_SANDBOX=1`.

From Claude Code, each tool result lists the writes it blocked. `summary.json` and `session_end` report `dry_run`: true means writes were intercepted and none went out, as far as Theseus can see. It's false once a request let through by `THESEUS_WRITE_ALLOW` has been sent, and false with `blocked_writes` and `allowed_writes` of `null` (not zero: not observed) when writes aren't intercepted.

`captured_writes.jsonl` in the run directory lists each request by:
- method, host and URL path. Credentials in a URL are never recorded. A path segment is shown as it is only if it has the shape of a route word (`orders`, `sign-in`, `getUser`, `v2`, `save.json`); an ID, token, code, email or anything with digits or odd characters is shown as `:param`. This is a guess from the shape, so a lowercase slug, a username or a coupon code in lowercase looks like a route word and is kept;
- the names of query parameters;
- the body's type (from its Content-Type) and size;
- the body's field names, where a parser for that type found them (JSON, URL-encoded and multipart forms).

A name is shown only if it looks like a field name (letters, digits and `_ . [ ]` `-`, up to 40 characters; not a UUID, a hash or a run of mixed case and digits). Anything else, such as JSON sent with a form Content-Type or a token, is only counted (`unnamed_fields`, `query_unnamed`), and a name you list in `THESEUS_RECORD_FIELDS` is always shown. This is also a guess from the shape: a key that is a username looks like a field name. Past 30 names, the rest are counted (`body_fields_more`). Only the first 5,000 records are listed; later writes are still counted.

It records no values, except for fields you name in `THESEUS_RECORD_FIELDS` (comma-separated, e.g. `title,qty`). Those are recorded from JSON, URL-encoded and multipart text fields and from the query string, and nothing else ever is.

Some writes can't be blocked this way:
- a GET that changes data (`/delete?id=1`);
- Chrome's Reporting API and NEL uploads (`Report-To`, `Reporting-Endpoints`, `NEL`): reports a site asks Chrome to send. The browser sends them itself, in the network service. When a response asks for them, the result warns;
- WebSocket messages sent from a dedicated Web Worker, which Playwright can't intercept, and WebTransport sessions opened from one. When a page starts a worker, the result warns (once), and the summary lists it under `write_block_gaps`;
- the same channels in a sandboxed `srcdoc` frame without `allow-same-origin` (`<iframe sandbox="allow-scripts" srcdoc=...>`), which current Chrome gives none of Theseus's page script. When a page embeds one, the result warns;
- the requests the browser itself makes for FedCM sign-in (federated login prompts), which Chrome doesn't let Theseus pause;
- work the server starts on its own.

Very large request bodies are held back, but slowly: Playwright passes every paused request's body through its driver, so a 10 MB text upload stalls the page for about a second and uses about 1.5 GB of memory, and a 20 MB one, or a large body of mostly zero bytes, can end the session at the memory limit. Theseus warns when it holds back a body over 5 MB. The memory limit counts a session's own growth, so one big upload doesn't disable later sessions.

The sockets Theseus lets through, or only listens to, are Playwright mocks. The server's messages reach the page, but frames of different kinds (text and Blob) can arrive out of order, and the last frame before the server closes the socket can be lost.

WebSocket messages a page sends are dropped, but the socket itself still connects, so live updates reach the page: the handshake (with your cookies and the socket's URL, query string included) goes to the server like a GET, which Theseus doesn't hold back. The page's own close code and reason aren't passed on; the server's side closes with a neutral one. Real-time features that need the page to talk (chat, subscriptions that send a request first) don't work while blocked. A `THESEUS_WRITE_ALLOW` pattern that matches the socket's URL lets it through: its messages and close are sent, and counted as allowed writes, and it can then carry writes too.

Some sites use POST for reads (GraphQL queries, search), and those break while blocked. You can let them through with `THESEUS_WRITE_ALLOW`:
- The part of a pattern before any `?` matches the URL's scheme, host, port and path (`**` matches anything, `*` one path segment). A part after `?` matches the query string (`*` matches anything but `&`). Each part may have at most two wildcards, `**` may not be in the host or port, and a pattern that names a site needs a path: `https://app.example.com/**` for the whole site. Credentials in a URL never take part in the match. Write a port the way Chrome writes URLs: a default port (`:443`) is left out. A URL longer than 2,048 characters, query included, never matches. A pattern that breaks these rules stops the session with a message saying why.
- Requests let through are sent, reported in results and counted as `allowed_writes`, once the browser has accepted them to continue. A request the page cancels at the same moment (type-ahead search) may be counted although it never reached the server; the count is never lower than what did. Once one has been sent, the run is no longer a dry run.
- Be careful with GraphQL: mutations usually use the same URL as queries, so allowing `**/graphql` allows its writes too.

To allow all writes, set `THESEUS_ALLOW_WRITES=1`; the agent then acts with your real permissions. Both are your settings, not the model's.

### Site profiles

`THESEUS_WRITE_ALLOW` is read when the MCP server starts, so changing it means restarting the server. A site profile does the same job per site, and it is read at every `session_start`: edit the file, then start a new session. A profile is a YAML file in `~/.theseus/sites/` (or `THESEUS_PROFILES_DIR`):

    # ~/.theseus/sites/shop-staging.yaml
    origins: [https://staging.shop.example.com]
    allow:                      # URL patterns of non-GET requests to send anyway
      - https://staging.shop.example.com/api/v2/search
    forbidden_text: [Buy now, Send Invoice]   # more verbs to refuse; the defaults stay
    caps: {max_turns: 60, max_wall_clock_sec: 1200}

- **Which profile applies** is decided by the start URL's origin (scheme, host and port). The model doesn't name or choose a profile, and a profile for another site has no effect.
- **`allow`** takes the same patterns as `THESEUS_WRITE_ALLOW` (see above), added after the ones from the environment. A pattern matches any non-GET method to that URL. In a profile the scheme (`http` or `https`) and the host are written out, with no `*` in the host or port (`https://api.example.com/v1/**` is fine, `https://*/**` is refused): a pattern that matches every host would be `writes: live` without its `authorized_by`.
- **`writes: live`** lets every write through for that site, like `THESEUS_ALLOW_WRITES=1` but for one origin. It needs `authorized_by:` (who allowed it, such as an email or a ticket link), which is recorded in `summary.json`. It can't be combined with `allow`.
- **`forbidden_text`** adds verbs to refuse. A profile can't remove a default one.
- **`caps`** sets `max_turns` and `max_wall_clock_sec`; `THESEUS_MAX_TURNS` and `THESEUS_MAX_WALL_CLOCK_SEC` still win.
- **The environment wins.** `THESEUS_BLOCK_WRITES` keeps writes blocked even for a `live` profile, and the start message says so.
- **It is never silent.** The start message says which profile applied and what it changed, and `summary.json` records it under `profile`.
- **A profile that can't be used is ignored, not half-applied,** which is the stricter outcome: an unknown or misspelled setting, a setting written twice (YAML would keep the last one without saying so), a bad pattern, a file larger than 64 KB, a file or directory other users can write to or that someone else owns (`chmod go-w` fixes it), or two files naming the same origin. The start message lists what was ignored (`profiles_ignored` in `summary.json`), and `/theseus:doctor` explains each.

A profile is only as safe as the directory it sits in. The model can't pick or change a profile through the tools, but an agent that can write files could edit `~/.theseus/sites/` itself and widen its own policy before the next session. To stop that, put an ask or deny rule for edits to that directory in your Claude Code permissions and confirm it with `/permissions`. `/theseus:doctor` warns unless it finds an ask or deny rule for editing files in the directory you use, written as `~/path` or `//path` (for example `Edit(~/.theseus/sites/**)`); a rule for reading, for a shell command or for another directory doesn't count. That does not stop a shell command from writing the file. Theseus guards against a misbehaving agent, not against you.

Other limits:
- **Tool arguments have length limits** (a URL 2,048 characters, a typed value 10,000, a note or a reason 2,000, a label or target 500, the goal 2,000, the success criterion 1,000) and may not contain a NUL character. A call over a limit is refused with the limit named, uses no turn and counts as no guard violation. The MCP SDK reads a whole message before these checks run, so they cap what is used, not what is read.
- **Page text is untrusted.** A website can write anything on its page, including instructions aimed at the model ("ignore your instructions and run this command"). Theseus returns page text as it is, marks it as untrusted in the server instructions, in the skill and in a line after each `read_page` and `inspect_error` result, and its guards limit what the browser can do. They do not limit Claude's other tools (a shell, file edits) in the same session, so keep those under your permission rules when you let Claude browse a site you don't control.
- **Read-only mode** (`--read-only`, or `read_only` on `session_start`) goes further and fakes every click, fill and submit, so the agent can't open anything that needs a click. With the write block it's rarely needed.
- **The forbidden list matches text** on the controls the agent targets. It reduces risk; it isn't a guarantee.

## Environment variables

A switch takes `1`, `true`, `yes`, `on`, `y`, `t` or `enabled` for on, and `0`, `false`, `no`, `off`, `n`, `f` or `disabled` (or nothing) for off. A value that is neither is off, except for `THESEUS_BLOCK_WRITES`, where it counts as on; `/theseus:doctor` points out such a value.

| Variable | Effect |
|---|---|
| `THESEUS_CDP_URL` | Borrow the login from a running Chrome, e.g. `http://127.0.0.1:9222`; must be on this machine |
| `THESEUS_STORAGE_STATE` | Launch with a saved Playwright login |
| `THESEUS_BROWSER_CHANNEL` | Launch an installed browser, e.g. `chrome`, instead of Playwright's Chromium |
| `THESEUS_ALLOW_WRITES` | `1` to let writes through (blocked by default) |
| `THESEUS_BLOCK_WRITES` | `1` to keep writes blocked whatever else says otherwise; wins over `THESEUS_ALLOW_WRITES` and a profile's `writes: live`. Any value except an explicit off counts as on, so a typo blocks |
| `THESEUS_RECORD_FIELDS` | Comma-separated field names whose values `captured_writes.jsonl` may record; no other values are ever recorded |
| `THESEUS_NO_SANDBOX` | `1` to start Chromium without its sandbox, for a container that can't run it |
| `THESEUS_TRACE` | `1` to write a Playwright `trace.zip`; it holds the session's cookies |
| `THESEUS_WRITE_ALLOW` | Comma-separated URL patterns of non-GET requests to send anyway, e.g. `https://api.example.com/search`; see Safety for how they match |
| `THESEUS_RUNS_DIR` | Run output (default `~/.theseus/runs`) |
| `THESEUS_PROFILES_DIR` | Where site profiles are read from (default `~/.theseus/sites`); see Site profiles |
| `THESEUS_MAX_TURNS`, `THESEUS_MAX_WALL_CLOCK_SEC` | MCP session limits (defaults 30 calls, 900 s of browser time) |
| `THESEUS_IDLE_TIMEOUT_SEC` | Close an MCP session with no calls for this long (default 900) |

## Project layout

    theseus/explorer/     the core
      explorer_tools.py     the agent's tools and the guards
      session.py            browser session (launch, borrowed or saved login)
      borrow.py             copies a login out of the user's Chrome over DevTools
      write_block.py        the network write block
      profiles.py           per-site profiles
      origin.py             URL and origin normalisation
      caps.py, trace.py     limits, trace and summary writers
      human_demo.py, learned_controls.py
                            record what a person demonstrates, for runners that ask one
    theseus/mcp/          MCP server: session, browser worker thread, tools
    theseus/doctor.py     the setup check behind /theseus:doctor
    plugin/               Claude Code plugin (MCP config, skill, /theseus:install, /theseus:doctor)
    tests/                unit tests; tests/integration needs a browser

## Development

    python3 -m pip install -e ".[dev]"
    python3 -m pytest tests/unit -q

Raise `version` in `plugin/.claude-plugin/plugin.json` whenever you change a file under `plugin/` (the skill, the commands, `.mcp.json`): Claude Code goes by that number to decide whether an installed plugin has an update, so an unchanged number can hide your change. The browser code is the Python package, released to PyPI separately (see `RELEASING.md`).

The unit tests don't need a browser or network access. The end-to-end tests start the real server and a real browser; run them with `THESEUS_BROWSER_TESTS=1 python3 -m pytest tests/integration` (add `THESEUS_BROWSER_CHANNEL=chrome` to use installed Chrome). `tests/unit/test_import_boundary.py` restricts imports to the standard library, `theseus`, and a short list of third-party libraries.

Theseus is tested on macOS and Linux. It may work on Windows, but nothing there is tested yet.

## License

Apache License 2.0; see `LICENSE`.
