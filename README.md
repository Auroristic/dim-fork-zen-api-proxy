# dim-fork-zen-api-proxy

*OpenCode Zen free-tier models as local Ollama models — with server-side
agentic tools.*

An Ollama-API-compatible proxy that exposes OpenCode Zen's free-tier models as
local Ollama models, plus a small set of server-side agentic tools.

Built for the **dim-ghub fork of Caelestia shell** "AI Assistant"
sidebar panel, which talks
to `localhost:11434` expecting Ollama's API shape (`/api/tags`, `/api/chat`,
`/api/generate`), Any Ollama compatible client can use this, no changes to the sidebar.

specific to
**[dim-ghub/caelestia-shell](https://github.com/dim-ghub/caelestia-shell)**
fork, vanilla upstream caelestia doesnt include this.

Tool-calling runs **inside the proxy** (execute → append result → re-request
the model) The proxy exposes a fixed 14-tool set instead.

## Automated Installation

For a guided, interactive install — prerequisites check, API keys saved to
`.env`, optional desktop-notification dependency, systemd user service plus
auto-restart-on-update path unit, and startup verification — just run the
bundled installer:

```bash
git clone https://github.com/Auroristic/dim-fork-zen-api-proxy.git && cd dim-fork-zen-api-proxy
./install.sh
```

To remove the service later, run `./uninstall.sh`.

Run it interactively in a terminal (not as root, not via a non-interactive
pipe). Manual setup instructions continue below.

## Quick start

```bash
git clone https://github.com/Auroristic/dim-fork-zen-api-proxy.git && cd dim-fork-zen-api-proxy
export ZEN_API_KEY="<your-opencode-zen-key>"   # or add it to ~/.env (/home/user/.env)
python3 zen_ollama_proxy.py
```

Full setup details in the sections below.

## Requirements

- Python 3.x (stdlib only — no pip dependencies)
- Optional: a local Ollama instance for the local-model fallback passthrough
  (the proxy's `/api/tags` merges local + Zen models)
- An OpenCode Zen API key (`ZEN_API_KEY`), needed for any Zen model
  (sign up / get a key at https://opencode.ai/auth)
- Linux desktop utilities used by the tools (exact package names):
  - `grim` — screen capture (Wayland)
  - `hyprctl` — Hyprland compositor, used to locate the focused monitor
  - `jq` — parses `hyprctl` output for the capture geometry
  - `imagemagick` — converts/resizes the capture to JPEG (`magick`)
  - `wl-clipboard` (`wl-paste`/`wl-copy`) or `xclip` — clipboard tools
  - `libnotify-bin` (`notify-send`) — desktop notifications
  - `bash`, `procps` (`free`), `coreutils` (`ls`, `df`) — shell/system info
- Optional: a Tavily API key (`TAVILY_API_KEY`) for the `web_search` tool
  (sign up / get a key at https://tavily.com — 1,000 searches/month, no card)
- Note: `take_screenshot` assumes Hyprland (uses `hyprctl` for monitor
  geometry). Other compositors need a modified capture command.

## Installation

```bash
git clone https://github.com/Auroristic/dim-fork-zen-api-proxy.git && cd dim-fork-zen-api-proxy
# set the API key (either way):
export ZEN_API_KEY="your-opencode-zen-key"
# or create ~/.env with:  ZEN_API_KEY="<your-opencode-zen-key>"
python3 zen_ollama_proxy.py                  # stdlib only, nothing to install
```

**Verify it's running:**

```bash
curl http://localhost:11434/api/tags
```

This should list your local Ollama models plus the Zen free models
(short display names like `V4 Flash`, `MiMo`, `Nemotron`). 

Warning signs:

- Free models missing → `ZEN_API_KEY` wasn't picked up (check `~/.env` or the export).
- `"models": []` → no key *and* the local Ollama fallback on 11435 isn't reachable.

Default ports: proxy listens on **127.0.0.1:11434**; the local Ollama fallback
is expected on **127.0.0.1:11435** (start it as your own user, e.g.
`ollama serve` with `OLLAMA_HOST=127.0.0.1:11435`).

Point your client at the proxy. For the dim-ghub Caelestia shell specifically, the key
`ai.ollamaUrl` in `~/.config/caelestia/shell.json` is what the shell's
`AiConfig` reads — defaults already point at `http://localhost:11434`, so
usually nothing needs changing; only set it if you run the proxy on another
port. `ai.ollamaModel` sets the default model; the models dropdown is served
by `/api/tags`.

**Important — disable the panel's own tool-calling.** The proxy runs its own
server-side tool loop, so the panel's built-in tool usage must be switched off
or the two will conflict (duplicate or broken tool calls). In the panel go to
**Settings → Panels → Sidebar → AI Assistant** and disable **"Enable tool
usage"** before using the proxy.

## Running persistently

Keep it alive without a terminal — either background it:

```bash
nohup python3 zen_ollama_proxy.py >/tmp/zen-ollama-proxy.log 2>&1 &
```

or use a user-level systemd unit at `~/.config/systemd/user/zen-ollama-proxy.service`:

```ini
[Unit]
Description=OpenCode Zen to Ollama proxy

[Service]
ExecStart=/usr/bin/python3 %h/dim-fork-zen-api-proxy/zen_ollama_proxy.py
Restart=on-failure

[Install]
WantedBy=default.target
```

(`%h` is your home directory — this `ExecStart` assumes the repo was cloned
directly into `$HOME`; adjust the path if you cloned it elsewhere.)

```bash
systemctl --user daemon-reload
systemctl --user enable --now zen-ollama-proxy
```

The proxy loads `~/.env` itself, so the unit needs no `EnvironmentFile`.

### Optional: auto-restart on updates (systemd path unit)

`Restart=on-failure` in the service unit only restarts on a crash — it does
**not** pick up file changes on its own. To make edits or updates (e.g. after
a `git pull`) apply automatically, add a systemd *path* unit at
`~/.config/systemd/user/zen-ollama-proxy.path`:

```ini
[Path]
PathModified=%h/dim-fork-zen-api-proxy/zen_ollama_proxy.py

[Install]
WantedBy=default.target
```

Enable and start both together:

```bash
systemctl --user daemon-reload
systemctl --user enable --now zen-ollama-proxy.service
systemctl --user enable --now zen-ollama-proxy.path
```

To verify: `touch ~/dim-fork-zen-api-proxy/zen_ollama_proxy.py` (or do a real
`git pull`), then check `systemctl --user status zen-ollama-proxy` — the
"Active" timestamp should update to just now.

## Configuration (env vars)

| Variable | Default | Description |
|---|---|---|
| `ZEN_API_KEY` | *(unset)* | OpenCode Zen bearer token; unset → local models only |
| `TAVILY_API_KEY` | *(unset)* | Optional Tavily key for `web_search`; get a free key at [tavily.com](https://tavily.com) (1,000 searches/month, no card); unset → web_search returns "unavailable" |
| `PROXY_PORT` | `11434` | Port the proxy listens on |
| `LOCAL_OLLAMA_URL` | `http://127.0.0.1:11435` | Local Ollama to merge/fall back to |
| `ZEN_BASE` | `https://opencode.ai/zen/v1` | Zen API base URL |

An optional `~/.env` file is loaded at startup (KEY=VALUE lines, `#` comments,
optional `export ` prefix, optional quotes). **Process environment wins over
`.env`.** The file can hold any of the variables above; typically just
`ZEN_API_KEY`.

## Available tools

The proxy injects these into every Zen chat request and executes them
server-side (up to 4 tool rounds per request; results truncated to 8000 chars):

| Tool | What it does |
|---|---|
| `get_current_date` | Current date/time, answered locally (zero LLM turns) |
| `run_shell_command` | Runs a `bash` command, returns combined output (blocklisted patterns refused) |
| `take_screenshot` | Captures the focused monitor to `/tmp/orion_screenshot.jpg` |
| `get_clipboard` | Returns current clipboard text |
| `set_clipboard` | Replaces clipboard with given text |
| `list_directory` | `ls -la` of a path (`~` expanded) |
| `get_system_info` | CPU %, memory, disk usage |
| `web_search` | Web search (Tavily API), top 5 results with titles/URLs/snippets; requires `TAVILY_API_KEY` |
| `read_file` | Reads a text file (`~` expanded); refuses binary files, truncates at 8000 chars |
| `describe_image` | Loads any image file and attaches it for vision models (same channel as screenshots); 10 MB cap |
| `search_files` | Case-insensitive name search in a directory (depth ≤ 20, max 50 results) |
| `notify` | Desktop notification (title + message) via `notify-send` |
| `write_file` | Writes text to a file — **requires user confirmation** (see below) |
| `open_application` | Launches an app by name (no args) — **requires user confirmation** (see below) |

**Confirmation gate:** `write_file` and `open_application` never execute
directly. The proxy issues a one-time 6-char token; the model asks you to reply
with `confirm <TOKEN>` in chat, and only the next call matching that exact
action (same path/content or app) executes. Tokens expire after 15 minutes.
`write_file` additionally hard-refuses system directories (`/etc`, `/usr`,
`/boot`, ...) and protected files (the proxy script, `.env`).

Vision: **only MiMo (`mimo-v2.5-free`) is vision-capable** among the free
models (verified live against the Zen API). `take_screenshot` attaches the
image only for vision-capable models; for all others the tool result
explicitly tells the model it cannot see the image and must not guess.

## Model listing & naming

- `/api/tags` merges local Ollama models with Zen free models
  (`id.endswith("-free")` filter).
- Free models get short display names (e.g. `deepseek-v4-flash-free` →
  `V4 Flash`); `model` in requests accepts either the real id or the display
  name.
- `BROKEN_FREE_MODELS` excludes known-broken free ids (e.g.
  `ling-3.0-flash-free`, `north-mini-code-free`) from the listing entirely.

## Known limitations

- The shell-command blocklist (recursive `rm`, `mkfs`, `dd if=`, fork bombs,
  remote-script-to-shell piping) is a **basic safety net, not a security
  boundary**. The proxy will happily run any other command the model requests.
- Tool execution has **no per-call human confirmation** for most tools —
  models act as instructed within the tool loop. Exceptions: `write_file` and
  `open_application` require a chat-based confirmation token.
- `web_search` needs a Tavily API key; without one it returns an explicit
  "unavailable" message.
- `read_file` is text-only (binary files are refused); `describe_image` only
  attaches images for vision-capable models (MiMo among the free ones).
- Clipboard contents are sent to the model by design (tool results); they are
  redacted from the proxy's own failure logs.
- `take_screenshot` is Hyprland-specific; other compositors need a modified
  capture command.

## Troubleshooting (walls we hit, so you don't)

- **`image_url` in tool messages → 400 from strict providers (Console).**
  Image content must live in a **user-role** message. The proxy keeps tool
  messages text-only and appends a synthetic `{"role": "user", "content":
  [image_url parts]}` message after the tool result.
- **`messages[N]: invalid type: map, expected a string` on round 2.**
  Replayed `tool_calls[].function.arguments` must be a **JSON string**, not a
  parsed object. The proxy serializes arguments before replaying.
- **`messages[N]: missing field 'type'`.** Strict providers (deepseek's Console
  route) require a `type` discriminator on every message — replay messages
  carry `"type": "message"` / `"type": "tool"`.
- **`403` from Zen.** The proxy sends a browser-like `User-Agent`; plain
  urllib UAs are rejected.
- **401 `CreditsError: Insufficient balance`.** Paid models are out of scope;
  use the `-free` models.
- **Round-2 failures are detailed by design:** the proxy logs the full
  outgoing payload on failure (clipboard values redacted).

## TODO / known rough edges

- `BROKEN_FREE_MODELS` and the free-model display list are maintained
  manually; Zen's `/models` endpoint is not consulted at runtime.
- Local model fallback expects the Ollama store served by the same user the
  proxy runs as.

## Maintainer notes

- When changing `zen_ollama_proxy.py` (port, env vars, dependencies, tools),
  update the CONFIG block and feature menu in `install.sh` and the CONFIG
  block in `uninstall.sh` in the same commit so the installers never drift.

## 🔮 Planned Tools

- `todo`: Persistent task list
- `system_stats`: Dedicated CPU/RAM/battery via /proc
- `clipboard`: Wayland wl-copy/wl-paste integration
- `screenshot`: grim integration
- `hyprctl`: Hyprland window/workspace control
- `weather`: via open-meteo
- `speak`: TTS via NVIDIA API / piper
