# dim-fork-zen-api-proxy

*OpenCode Zen free-tier models as local Ollama models — with server-side
agentic tools.*

An Ollama-API-compatible proxy that exposes OpenCode Zen's free-tier models as
local Ollama models, plus a small set of server-side agentic tools.

It was built for the Caelestia shell "AI Assistant" sidebar panel, which talks
to `localhost:11434` expecting Ollama's API shape (`/api/tags`, `/api/chat`,
`/api/generate`) — with zero changes to the panel. Any Ollama-API-compatible
client can use it the same way.

The AI Assistant sidebar panel is specific to the **dim-ghub fork of
caelestia-shell** — vanilla upstream caelestia-shell may not include it.

Tool-calling runs **inside the proxy** (execute → append result → re-request
the model) because the panel's own tool-calling did not play well with the free
models. The proxy exposes a fixed 7-tool set instead.

## Quick start

```bash
git clone https://github.com/Auroristic/dim-fork-zen-api-proxy.git && cd dim-fork-zen-api-proxy
export ZEN_API_KEY="<your-opencode-zen-key>"   # or add it to ~/.env
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
  - `bash`, `procps` (`free`), `coreutils` (`ls`, `df`) — shell/system info
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

A healthy response lists your local Ollama models plus the Zen free models
(short display names like `V4 Flash`, `MiMo`, `Nemotron`). Warning signs:

- Free models missing → `ZEN_API_KEY` wasn't picked up (check `~/.env` or the export).
- `"models": []` → no key *and* the local Ollama fallback on 11435 isn't reachable.

Default ports: proxy listens on **127.0.0.1:11434**; the local Ollama fallback
is expected on **127.0.0.1:11435** (start it as your own user, e.g.
`ollama serve` with `OLLAMA_HOST=127.0.0.1:11435`).

Point your client at the proxy. For Caelestia shell specifically, the key
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

## Configuration (env vars)

| Variable | Default | Description |
|---|---|---|
| `ZEN_API_KEY` | *(unset)* | OpenCode Zen bearer token; unset → local models only |
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
- Tool execution has **no per-call human confirmation** — models act as
  instructed within the tool loop.
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

- No image search capability yet; planned tools: `web_search`,
  `describe_image`, etc.
- `BROKEN_FREE_MODELS` and the free-model display list are maintained
  manually; Zen's `/models` endpoint is not consulted at runtime.
- Local model fallback expects the Ollama store served by the same user the
  proxy runs as.
