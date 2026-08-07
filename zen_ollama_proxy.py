#!/usr/bin/env python3
"""Ollama-API-compatible proxy that merges a local Ollama instance with OpenCode Zen.

Listens on 127.0.0.1:11434 (stdlib only: http.server + urllib, no pip deps).

  GET  /api/tags      -> merged model list: local models + "opencode/<id>" Zen models
  POST /api/chat      -> local models forwarded untouched (NDJSON passthrough);
                         Zen models: OpenAI SSE -> Ollama NDJSON translation
  POST /api/generate  -> local passthrough; Zen: "prompt" wrapped as a user message
                         (used by the panel's non-streamed chat-title generation)

Env vars:
  ZEN_API_KEY         OpenCode Zen bearer token (unset = local models only)
  LOCAL_OLLAMA_URL    local Ollama to merge with / fall back to (default http://127.0.0.1:11435)
  PROXY_PORT          port to listen on (default 11434)
  ZEN_BASE            Zen API base (default https://opencode.ai/zen/v1)
"""

import base64
import copy
import json
import os
import random
import re
import shutil
import string
import subprocess
import sys
import threading
import time
import argparse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen
from urllib.error import HTTPError

try:
    import spotipy
    from spotipy.oauth2 import SpotifyOAuth
except ImportError:
    spotipy = None


def _load_dotenv(path=None):
    """Load KEY=VALUE pairs from ~/.env if present. Existing env vars win."""
    path = path or os.path.join(os.path.expanduser("~"), ".env")
    try:
        with open(path, encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                if line.startswith("export "):
                    line = line[len("export "):]
                k, _, v = line.partition("=")
                k, v = k.strip(), v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                if k and k not in os.environ:
                    os.environ[k] = v
    except OSError:
        pass


_load_dotenv()

LOCAL_OLLAMA_URL = os.environ.get("LOCAL_OLLAMA_URL", "http://127.0.0.1:11435")
PROXY_PORT = int(os.environ.get("PROXY_PORT", "11434"))
ZEN_BASE = os.environ.get("ZEN_BASE", "https://opencode.ai/zen/v1")
ZEN_API_KEY = os.environ.get("ZEN_API_KEY", "")
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")
SPOTIFY_CLIENT_ID = os.environ.get("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET", "")
UPSTREAM_TIMEOUT = 600

VISION_MODEL_HINTS = ("vision", "gemini", "claude", "gpt-5", "gpt-4", "mimo",
                      "llava", "qwen-vl", "minicpm", "moondream", "phi-3-v", "internvl")

FREE_MODEL_NAMES = {
    "deepseek-v4-flash-free": "V4 Flash",
    "mimo-v2.5-free": "MiMo",
    "nemotron-3-ultra-free": "Nemotron",
    "laguna-s-2.1-free": "Laguna",
    "longcat-2.0-free": "Longcat",
}
DISPLAY_TO_REAL = {v: k for k, v in FREE_MODEL_NAMES.items()}

BROKEN_FREE_MODELS = {"ling-3.0-flash-free", "ling-3.0-tiny-free", "north-mini-code-free"}


def is_free_model(model_id):
    return model_id.endswith("-free") and model_id not in BROKEN_FREE_MODELS


OUR_TOOLS = [
    {"type": "function", "function": {
        "name": "get_current_date",
        "description": "Get the current date and time of the user's system (day of week, date, and local time). Use this whenever the user asks what day, date, or time it is.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "run_shell_command",
        "description": "Run a shell command on the user's Linux system and return its combined stdout/stderr output. The command runs non-interactively via bash. Prefer read-only commands for information queries.",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string",
                        "description": "The exact bash command to execute, e.g. 'date', 'ls -la ~/Documents', 'free -h'."}},
            "required": ["command"]}}},
    {"type": "function", "function": {
        "name": "take_screenshot",
        "description": "Capture the user's screen (focused monitor) and save it to a temp file. Returns the path of the saved image. If the active model is vision-capable the image is also attached for visual analysis; otherwise only the path is returned.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "get_clipboard",
        "description": "Return the current clipboard text content of the user's desktop session.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "set_clipboard",
        "description": "Replace the clipboard content with the given text.",
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string", "description": "The text to place on the clipboard."}},
            "required": ["text"]}}},
    {"type": "function", "function": {
        "name": "list_directory",
        "description": "List the contents of a directory on the user's system (ls -la equivalent). Use this for 'what's in X' queries instead of a generic shell command.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Absolute path of the directory to list, e.g. '/home/retro/Documents'."}},
            "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "get_system_info",
        "description": "Return a read-only summary of the user's system: CPU usage, memory usage, and disk usage.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "web_search",
        "description": "Search the web (Tavily API) and return the top results with titles, URLs and snippets. Requires TAVILY_API_KEY to be configured on the proxy.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "The search query"}},
            "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "read_file",
        "description": "Read the contents of a text file. Refuses binary files and truncates very long files.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Path of the text file to read (supports ~)"}},
            "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "describe_image",
        "description": "Load an image file and attach it for visual analysis if the current model is vision-capable. Use for describing or OCR-ing any existing image file, not just fresh screenshots.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Path of the image file (jpg, jpeg, png, gif, webp, bmp; supports ~)"}},
            "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "search_files",
        "description": "Search for files whose names contain a pattern, within a directory (depth-limited, max 50 results).",
        "parameters": {"type": "object", "properties": {
            "directory": {"type": "string", "description": "Directory to search (defaults to home; supports ~)"},
            "pattern": {"type": "string", "description": "Case-insensitive substring to match against file names"}},
            "required": ["pattern"]}}},
    {"type": "function", "function": {
        "name": "notify",
        "description": "Send a desktop notification (title + message) via notify-send.",
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string", "description": "Notification title (optional)"},
            "message": {"type": "string", "description": "Notification body text"}},
            "required": ["message"]}}},
    {"type": "function", "function": {
        "name": "write_file",
        "description": "Write text content to a file. NEVER executes without explicit user confirmation — ask the user to confirm first, then use the tool.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Destination path (supports ~)"},
            "content": {"type": "string", "description": "Full text content to write"}},
            "required": ["path", "content"]}}},
    {"type": "function", "function": {
        "name": "open_application",
        "description": "Launch an application by name (no arguments). NEVER executes without explicit user confirmation — ask the user to confirm first, then use the tool.",
        "parameters": {"type": "object", "properties": {
            "app": {"type": "string", "description": "Plain application name, e.g. 'firefox' or 'code'"}},
            "required": ["app"]}}},
    {"type": "function", "function": {
        "name": "set_reminder",
        "description": "Set a desktop notification reminder that fires later via notify-send. "
                       "Provide EXACTLY ONE of the two time params: 'delay_seconds' (number of "
                       "seconds from now, for 'in X minutes' phrasings) or 'at_time' (full local "
                       "datetime in ISO 8601, e.g. '2026-08-08T22:00:00', for 'at 10pm' phrasings; "
                       "compute the date from the current local date in the system prompt). Never "
                       "provide both or neither. Example mappings: 'send me nyah notification in 1 "
                       "minute' -> delay_seconds=60, message='nyah'; 'say meow in 5 minutes' -> "
                       "delay_seconds=300, message='meow'; 'remind me to sleep at 10pm' -> "
                       "at_time='<today>T22:00:00', message='sleep'; 'ping me to stretch in 30 "
                       "minutes' -> delay_seconds=1800, message='stretch'.",
        "parameters": {"type": "object", "properties": {
            "message": {"type": "string", "description": "Notification text to show when the reminder fires."},
            "delay_seconds": {"type": "number", "description": "Seconds from now until the reminder fires (for 'in X minutes/hours' requests)."},
            "at_time": {"type": "string", "description": "Full local datetime (ISO 8601, e.g. 2026-08-08T22:00:00) at which to fire (for 'at 10pm' requests)."}},
            "required": ["message"]}}},
    {"type": "function", "function": {
        "name": "list_reminders",
        "description": "List all active reminders with their id, fire time, and message. "
                       "Use when the user asks things like 'what reminders do I have', "
                       "'list my reminders', 'show my pending reminders'.",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "cancel_reminder",
        "description": "Cancel a pending reminder by id (get ids from list_reminders). The "
                       "reminder will not fire. Use when the user asks things like 'cancel my "
                       "sleep reminder', 'delete that reminder', 'never mind about the 10pm "
                       "reminder'. Call list_reminders first if you don't have the id.",
        "parameters": {"type": "object", "properties": {
            "id": {"type": "string", "description": "Reminder id, e.g. '1754612345678-ab12'."}},
            "required": ["id"]}}},
    {"type": "function", "function": {
        "name": "save_memory",
        "description": "Save a fact about the user to long-term memory (persisted in "
                       "memories.json, injected into every future request). Use when the user "
                       "says things like 'remember that I like dark roast', 'my name is retro', "
                       "'always respond sarcastically', or 'keep in mind that I use Hyprland'.",
        "parameters": {"type": "object", "properties": {
            "content": {"type": "string", "description": "The fact or preference to remember."},
            "category": {"type": "string", "description": "Optional category, e.g. 'preferences', 'identity' (defaults to 'general')."}},
            "required": ["content"]}}},
    {"type": "function", "function": {
        "name": "list_memories",
        "description": "List all saved long-term memories with their id, category, content, and creation time.",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "delete_memory",
        "description": "Delete a long-term memory by id (get ids from list_memories).",
        "parameters": {"type": "object", "properties": {
            "id": {"type": "string", "description": "Memory id, e.g. '1754612345678-ab12'."}},
            "required": ["id"]}}},
    {"type": "function", "function": {
        "name": "media_control",
        "description": "Control the active media player via playerctl: play, pause, next, prev, or query status.",
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["play", "pause", "next", "prev", "status"],
                       "description": "Media action to perform."}},
            "required": ["action"]}}},
    {"type": "function", "function": {
        "name": "play_song",
        "description": "Search Spotify for a track and play it on the active device. "
                       "Use when the user asks things like 'play hate me on spotify', "
                       "'put on some jazz', 'play the song hate me'.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Track or song to search for, e.g. 'hate me'."}},
            "required": ["query"]}}},
]
EXECUTABLE_TOOLS = {t["function"]["name"] for t in OUR_TOOLS}

BLOCKED_COMMAND_PATTERNS = [
    (r"\brm\s+-r[f]?\b", "recursive rm"),
    (r"\bmkfs(?:\s|\.|\b)", "mkfs"),
    (r"\bdd\s+if=", "dd write"),
    (r":\(\s*\)\s*\{[^}]*\}", "fork bomb"),
    (r"\b(?:curl|wget)\b[^|;&]*\|\s*(?:sudo\s+)?(?:sh|bash)\b", "pipe remote script to shell"),
]

MAX_TOOL_ROUNDS = 4
TOOL_OUTPUT_LIMIT = 8000

APPROVAL_TTL = 15 * 60
APPROVAL_TOKEN_RE = re.compile(r"\bconfirm\s+([A-Z0-9]{6})\b", re.IGNORECASE)
_APPROVAL_TOKENS = {}
SYSTEM_DIR_PREFIXES = ("/etc", "/usr", "/boot", "/bin", "/sbin", "/lib",
                       "/lib64", "/var", "/proc", "/sys", "/dev", "/run", "/root")
FORBIDDEN_WRITE_FILES = ("zen_ollama_proxy.py", ".env")
IMAGE_EXT_MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                  ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp"}
MAX_IMAGE_BYTES = 10 * 1024 * 1024


def _is_vision(model):
    return any(h in (model or "").lower() for h in VISION_MODEL_HINTS)


_REDACTED = "[REDACTED clipboard content]"
_CLIPBOARD_SECRETS = []


def _note_clipboard_value(value):
    if isinstance(value, str) and value and value != _REDACTED:
        _CLIPBOARD_SECRETS[:] = _CLIPBOARD_SECRETS[-3:] + [value]


def _redact_secrets(text):
    if not isinstance(text, str):
        text = str(text)
    for secret in _CLIPBOARD_SECRETS:
        if len(secret) >= 8:
            text = text.replace(secret, _REDACTED)
            text = text.replace(secret[:32], _REDACTED)
    return text


def _redact_payload(payload):
    red = copy.deepcopy(payload)
    clip_ids = set()
    for m in red.get("messages", []):
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function") or {}
            if fn.get("name") in ("get_clipboard", "set_clipboard"):
                clip_ids.add(tc.get("id", ""))
                args = fn.get("arguments")
                if isinstance(args, dict) and "text" in args:
                    args["text"] = _REDACTED
    for m in red.get("messages", []):
        if m.get("role") == "tool" and m.get("tool_call_id") in clip_ids:
            m["content"] = _REDACTED
    return red


def _run_cmd(argv, timeout=60, input_text=None):
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                              input=input_text, cwd=os.path.expanduser("~"))
    except subprocess.TimeoutExpired:
        return False, "ERROR: command timed out after {}s".format(timeout)
    except Exception as e:
        return False, "ERROR: failed to run command: {}".format(e)
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if not out:
        out = "(command completed with no output)"
    return proc.returncode == 0, out[:TOOL_OUTPUT_LIMIT]


def _cpu_usage():
    try:
        def _read_stat():
            with open("/proc/stat") as f:
                vals = f.readline().split()
            return sum(int(v) for v in vals[1:]), int(vals[4])
        t1, idle1 = _read_stat()
        time.sleep(0.25)
        t2, idle2 = _read_stat()
        total, idle = t2 - t1, idle2 - idle1
        return "{}%".format(int(100.0 * (1 - idle / total))) if total else "unknown"
    except Exception as e:
        log_err("failed to read CPU usage: {}".format(e))
        return "unknown"


def _image_content(text, image_path):
    try:
        with open(image_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        return [{"type": "text", "text": text},
                {"type": "image_url",
                 "image_url": {"url": "data:image/jpeg;base64," + b64}}]
    except Exception as e:
        log_err("failed to attach image {}: {}".format(image_path, e))
        return text


def _replay_tool_calls(calls):
    """Replay tool_calls in message history. OpenAI requires function.arguments as a JSON string."""
    out = []
    for c in calls:
        fn = c.get("function") or {}
        args = fn.get("arguments") or {}
        if not isinstance(args, str):
            args = json.dumps(args)
        out.append({"id": c.get("id", ""),
                    "function": {"name": fn.get("name", ""), "arguments": args}})
    return out


def _issue_approval_token(tool, args):
    token = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
    _APPROVAL_TOKENS[token] = {"tool": tool, "args": json.dumps(args, sort_keys=True),
                               "expires": time.time() + APPROVAL_TTL}
    return token


def _check_approval(tool, args, messages):
    """True only if the user's LAST chat message confirms this exact action
    with a token we previously issued."""
    token = None
    for m in reversed(messages or []):
        content = m.get("content")
        if isinstance(content, str) and content.strip():
            mch = APPROVAL_TOKEN_RE.search(content)
            token = mch.group(1).upper() if mch else None
            break
    if not token:
        return False
    entry = _APPROVAL_TOKENS.pop(token, None)
    if not entry:
        return False
    if time.time() > entry["expires"]:
        return False
    if entry["tool"] != tool or entry["args"] != json.dumps(args, sort_keys=True):
        return False
    return True


_REMINDER_LOCK = threading.Lock()
_ACTIVE_TIMERS = {}
_MEMORIES_LOCK = threading.Lock()


def _memories_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "memories.json")


def _read_memories():
    try:
        with open(_memories_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _write_memories(items):
    try:
        with open(_memories_path(), "w", encoding="utf-8") as f:
            json.dump(items, f, indent=2)
    except OSError as e:
        log_err("failed to write memories.json: {}".format(e))


def _add_memory(entry):
    with _MEMORIES_LOCK:
        items = _read_memories()
        items.append(entry)
        _write_memories(items)


def _remove_memory(mid):
    with _MEMORIES_LOCK:
        items = _read_memories()
        remaining = [i for i in items if i.get("id") != mid]
        if len(remaining) == len(items):
            return False
        _write_memories(remaining)
        return True


SPOTIFY_SCOPE = "user-modify-playback-state user-read-playback-state user-read-currently-playing"
SPOTIFY_REDIRECT_URI = "http://127.0.0.1:8888/callback"


def _spotify_cache_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), ".spotify_cache")


def _spotify_oauth():
    return SpotifyOAuth(client_id=SPOTIFY_CLIENT_ID, client_secret=SPOTIFY_CLIENT_SECRET,
                        redirect_uri=SPOTIFY_REDIRECT_URI, scope=SPOTIFY_SCOPE,
                        open_browser=False, cache_path=_spotify_cache_path())


def _spotify_client():
    if spotipy is None or not (SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET):
        return None
    auth = _spotify_oauth()
    if auth.get_cached_token() is None:
        return None
    return spotipy.Spotify(auth_manager=auth)


def _is_no_device_error(e):
    msg = str(e).lower()
    return "no active device" in msg or "no_active_device" in msg or "active device" in msg


def _launch_spotify():
    for argv in (["spotify"], ["spotify-launcher"], ["flatpak", "run", "com.spotify.Client"]):
        try:
            subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
            return True
        except OSError:
            continue
    return False


def _play_on_device(sp, device_id, uris):
    try:
        sp.start_playback(device_id=device_id, uris=uris)
        return True, None
    except Exception as e:
        log_err("spotify error: {}".format(e))
        if not _is_no_device_error(e):
            return False, "ERROR: Spotify playback failed: {}".format(e)
    try:
        sp.transfer_playback(device_id, force_transfer=True)
    except Exception as e:
        log_err("spotify transfer_playback error: {}".format(e))
    try:
        sp.start_playback(device_id=device_id, uris=uris)
        return True, None
    except Exception as e:
        log_err("spotify error: {}".format(e))
        return False, "ERROR: Spotify playback failed: {}".format(e)


def _reminders_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "reminders.json")


def _read_reminders():
    try:
        with open(_reminders_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _write_reminders(items):
    try:
        with open(_reminders_path(), "w", encoding="utf-8") as f:
            json.dump(items, f, indent=2)
    except OSError as e:
        log_err("failed to write reminders.json: {}".format(e))


def _add_reminder(entry):
    with _REMINDER_LOCK:
        items = _read_reminders()
        items.append(entry)
        _write_reminders(items)


def _remove_reminder(rid):
    with _REMINDER_LOCK:
        items = [i for i in _read_reminders() if i.get("id") != rid]
        _write_reminders(items)


def _fire_reminder(rid, message):
    log_err("reminder fired: {}".format(_redact_secrets(repr(message[:120]))))
    if shutil.which("notify-send"):
        _run_cmd(["notify-send", "--app-name=Zen proxy", "Reminder", message], timeout=15)
    _remove_reminder(rid)
    with _REMINDER_LOCK:
        _ACTIVE_TIMERS.pop(rid, None)


def _reschedule_reminders():
    """On startup: re-arm future reminders at their exact fire time; silently
    drop entries missed while the proxy was off (never fire them late)."""
    now = time.time()
    pending = []
    for item in _read_reminders():
        rid = item.get("id")
        message = item.get("message")
        try:
            fire_at = float(item.get("fire_at"))
        except (TypeError, ValueError):
            continue
        if fire_at > now:
            timer = threading.Timer(fire_at - now, _fire_reminder, args=(rid, message))
            timer.daemon = True
            timer.start()
            with _REMINDER_LOCK:
                _ACTIVE_TIMERS[rid] = timer
            pending.append(item)
    with _REMINDER_LOCK:
        _write_reminders(pending)
    return len(pending)


def _execute_tool(name, args, zen_model, messages=None):
    args = args or {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            args = {}
    try:
        if name == "get_current_date":
            return datetime.now().strftime("%A, %B %-d, %Y"), None
        if name == "run_shell_command":
            cmd = str(args.get("command", ""))
            if not cmd.strip():
                return "ERROR: empty command", None
            for pattern, label in BLOCKED_COMMAND_PATTERNS:
                if re.search(pattern, cmd):
                    log_err("blocked shell command ({}): {}".format(label, cmd[:200]))
                    return ("ERROR: command blocked by proxy safety blocklist "
                            "(matched pattern: '{}').").format(label), None
            ok, out = _run_cmd(["bash", "-c", cmd])
            return out, None
        if name == "take_screenshot":
            screen_cmd = ('grim -g "$(hyprctl monitors -j | jq -r \'.[] | select(.focused) | "\\(.x),\\(.y) \\(.width)x\\(.height)"\')" '
                          '/tmp/orion_screenshot.png')
            ok, out = _run_cmd(["bash", "-c", screen_cmd], timeout=30)
            if not ok:
                return "ERROR: screenshot capture failed: {}".format(out), None
            ok, out = _run_cmd(["bash", "-c",
                                "magick /tmp/orion_screenshot.png -resize '1024x1024>' -quality 85 /tmp/orion_screenshot.jpg"],
                               timeout=60)
            if not ok:
                return "ERROR: image conversion failed: {}".format(out), None
            path = "/tmp/orion_screenshot.jpg"
            if _is_vision(zen_model):
                return "Screenshot saved to {}".format(path), path
            return ("Screenshot saved to {}. NOTE: You are not vision-capable in this session; "
                    "you cannot see the image contents. Do not describe or guess what's in it. "
                    "Tell the user you cannot view images and suggest a vision-capable model "
                    "(MiMo is the only free model with image support) if they need image description."
                    ).format(path), None
        if name == "get_clipboard":
            if shutil.which("wl-paste"):
                ok, out = _run_cmd(["wl-paste", "-n"], timeout=10)
            elif shutil.which("xclip"):
                ok, out = _run_cmd(["xclip", "-o", "-selection", "clipboard"], timeout=10)
            else:
                return "ERROR: no clipboard tool (wl-paste/xclip) found", None
            if not ok:
                return out, None
            out = out if out != "(command completed with no output)" else "(clipboard is empty)"
            _note_clipboard_value(out)
            return out, None
        if name == "set_clipboard":
            text = str(args.get("text", ""))
            _note_clipboard_value(text)
            if shutil.which("wl-copy"):
                try:
                    proc = subprocess.Popen(["wl-copy"], stdin=subprocess.PIPE,
                                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                            start_new_session=True)
                    proc.stdin.write(text.encode())
                    proc.stdin.close()
                except Exception as e:
                    return "ERROR: wl-copy failed: {}".format(e), None
            elif shutil.which("xclip"):
                ok, out = _run_cmd(["xclip", "-i", "-selection", "clipboard"], input_text=text)
                if not ok:
                    return "ERROR: xclip failed: {}".format(out), None
            else:
                return "ERROR: no clipboard tool (wl-copy/xclip) found", None
            return "Clipboard updated.", None
        if name == "list_directory":
            path = str(args.get("path", "")) or "."
            path = os.path.expanduser(path)
            ok, out = _run_cmd(["ls", "-la", "--", path], timeout=30)
            return out, None
        if name == "get_system_info":
            parts = ["CPU usage: " + _cpu_usage()]
            _, mem = _run_cmd(["free", "-h"], timeout=15)
            _, disk = _run_cmd(["df", "-h"], timeout=15)
            parts.append("Memory:\n" + mem)
            parts.append("Disk:\n" + disk)
            return "\n".join(parts), None
        if name == "web_search":
            query = str(args.get("query", ""))
            if not query.strip():
                return "ERROR: web_search requires a 'query'", None
            if not TAVILY_API_KEY:
                return ("ERROR: search unavailable — no TAVILY_API_KEY configured. "
                        "Get a free key at tavily.com (1,000 searches/month), add "
                        "TAVILY_API_KEY to ~/.env, then restart the proxy."), None
            try:
                req = Request("https://api.tavily.com/search",
                              data=json.dumps({"query": query, "max_results": 5,
                                               "search_depth": "basic"}).encode(),
                              method="POST",
                              headers={"Content-Type": "application/json",
                                       "Authorization": "Bearer " + TAVILY_API_KEY,
                                       "User-Agent": "curl/8.0.0"})
                with urlopen(req, timeout=30) as resp:
                    data = json.loads(resp.read())
                results = (data.get("results") or [])[:5]
                if not results:
                    return "No search results for: " + query, None
                lines = ["Search results for: " + query]
                for i, r in enumerate(results, 1):
                    lines.append("{}. {} — {} ({})".format(
                        i, r.get("title", ""), r.get("url", ""),
                        (r.get("content") or "").replace("\n", " ")))
                return "\n".join(lines)[:TOOL_OUTPUT_LIMIT], None
            except HTTPError as e:
                return "ERROR: search API error: {}".format(e.code), None
            except Exception as e:
                return "ERROR: search failed: {}".format(e), None
        if name == "read_file":
            path = os.path.expanduser(str(args.get("path", "")))
            if not path.strip():
                return "ERROR: read_file requires a 'path'", None
            if not os.path.isfile(path):
                return "ERROR: no such file: {}".format(path), None
            try:
                with open(path, "rb") as f:
                    raw = f.read(TOOL_OUTPUT_LIMIT + 1)
            except OSError as e:
                return "ERROR: cannot read {}: {}".format(path, e), None
            if b"\x00" in raw:
                return ("ERROR: {} appears to be a binary file — read_file only "
                        "handles text files".format(path)), None
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                return ("ERROR: {} is not valid UTF-8 text — refusing to dump "
                        "binary content".format(path)), None
            truncated = len(raw) > TOOL_OUTPUT_LIMIT
            text = text[:TOOL_OUTPUT_LIMIT]
            if truncated:
                text += "\n...[truncated at {} chars]".format(TOOL_OUTPUT_LIMIT)
            return text, None
        if name == "describe_image":
            path = os.path.expanduser(str(args.get("path", "")))
            if not path.strip():
                return "ERROR: describe_image requires a 'path'", None
            if not os.path.isfile(path):
                return "ERROR: no such image: {}".format(path), None
            ext = os.path.splitext(path)[1].lower()
            if ext not in IMAGE_EXT_MIME:
                return ("ERROR: unsupported image type '{}' (supported: jpg, jpeg, "
                        "png, gif, webp, bmp)".format(ext or "(none)")), None
            try:
                size = os.path.getsize(path)
            except OSError as e:
                return "ERROR: cannot stat {}: {}".format(path, e), None
            if size > MAX_IMAGE_BYTES:
                return "ERROR: image too large ({} MB, max 10 MB)".format(size // (1024 * 1024)), None
            if not _is_vision(zen_model):
                return ("Image loaded from {}. NOTE: you are not vision-capable in "
                        "this session; you cannot see its contents. Do not describe "
                        "or guess what's in it — tell the user to use a "
                        "vision-capable model (MiMo).").format(path), None
            return "Image loaded from {}".format(path), path
        if name == "search_files":
            directory = os.path.expanduser(str(args.get("directory", "")) or "~")
            pattern = str(args.get("pattern", ""))
            try:
                max_depth = min(int(args.get("max_depth", 6) or 6), 20)
            except (TypeError, ValueError):
                max_depth = 6
            if not pattern.strip():
                return "ERROR: search_files requires a 'pattern'", None
            if not os.path.isdir(directory):
                return "ERROR: no such directory: {}".format(directory), None
            needle = pattern.lower()
            hits = []

            def _walk(d, depth):
                if depth > max_depth or len(hits) >= 50:
                    return
                try:
                    entries = sorted(os.listdir(d))
                except OSError:
                    return
                for e in entries:
                    if len(hits) >= 50:
                        return
                    full = os.path.join(d, e)
                    if needle in e.lower():
                        hits.append(full)
                    if os.path.isdir(full) and not os.path.islink(full):
                        _walk(full, depth + 1)

            _walk(directory, 1)
            if not hits:
                return "No files matching '{}' under {}".format(pattern, directory), None
            return ("{} match(es) under {} (depth <= {}):\n{}".format(
                len(hits), directory, max_depth, "\n".join(hits[:50])))[:TOOL_OUTPUT_LIMIT], None
        if name == "notify":
            title = str(args.get("title", "")) or "Zen proxy"
            message = str(args.get("message", ""))
            if not message.strip():
                return "ERROR: notify requires a 'message'", None
            if not shutil.which("notify-send"):
                return "ERROR: notify-send not found (install libnotify-bin)", None
            ok, out = _run_cmd(["notify-send", "--app-name=Zen proxy", title, message], timeout=15)
            if not ok:
                return "ERROR: notification failed: {}".format(out), None
            return "Notification sent.", None
        if name == "set_reminder":
            message = str(args.get("message", "")).strip()
            if not message:
                return "ERROR: set_reminder requires a 'message'", None
            delay = args.get("delay_seconds")
            at_time = args.get("at_time")
            if (delay is None) == (at_time is None):
                return ("ERROR: set_reminder requires exactly ONE of 'delay_seconds' "
                        "or 'at_time' (got delay_seconds={!r}, at_time={!r})").format(delay, at_time), None
            now = datetime.now()
            if at_time is not None:
                try:
                    dt = datetime.fromisoformat(str(at_time).strip())
                except ValueError:
                    return ("ERROR: cannot parse at_time {!r}; use a full local ISO 8601 "
                            "datetime like '2026-08-08T22:00:00'").format(at_time), None
                if dt.tzinfo is not None:
                    dt = dt.astimezone().replace(tzinfo=None)
                fire_at = dt.timestamp()
                if fire_at <= now.timestamp():
                    return "ERROR: at_time {} is in the past (now is {:%H:%M:%S})".format(at_time, now), None
            else:
                try:
                    delay = float(delay)
                except (TypeError, ValueError):
                    return "ERROR: delay_seconds must be a number, got {!r}".format(delay), None
                if delay <= 0:
                    return "ERROR: delay_seconds must be positive, got {}".format(delay), None
                fire_at = now.timestamp() + delay
            rid = "{}-{:04x}".format(int(time.time() * 1000), random.getrandbits(16))
            entry = {"id": rid, "message": message, "fire_at": fire_at,
                     "created_at": time.time()}
            _add_reminder(entry)
            timer = threading.Timer(max(0.0, fire_at - time.time()),
                                    _fire_reminder, args=(rid, message))
            timer.daemon = True
            timer.start()
            with _REMINDER_LOCK:
                _ACTIVE_TIMERS[rid] = timer
            fire_dt = datetime.fromtimestamp(fire_at)
            if fire_dt.date() == now.date():
                when = "today at {:%H:%M:%S}".format(fire_dt)
            else:
                when = "{:%Y-%m-%d %H:%M:%S}".format(fire_dt)
            return 'Reminder set for {} — "{}"'.format(when, message), None
        if name == "list_reminders":
            with _REMINDER_LOCK:
                items = sorted(_read_reminders(), key=lambda i: i.get("fire_at", 0.0))
            if not items:
                return "No active reminders.", None
            lines = ["Active reminders:"]
            for n, item in enumerate(items, 1):
                fire_dt = datetime.fromtimestamp(float(item.get("fire_at", 0)))
                lines.append("[{}] {} | {:%Y-%m-%d %H:%M:%S} | {}".format(
                    n, item.get("id", "?"), fire_dt, item.get("message", "")))
            return "\n".join(lines), None
        if name == "cancel_reminder":
            rid = str(args.get("id", "")).strip()
            if not rid:
                return "ERROR: cancel_reminder requires an 'id'", None
            with _REMINDER_LOCK:
                timer = _ACTIVE_TIMERS.pop(rid, None)
            if timer is None:
                return "Reminder not found.", None
            timer.cancel()
            _remove_reminder(rid)
            return "Cancelled.", None
        if name == "save_memory":
            content = str(args.get("content", "")).strip()
            if not content:
                return "ERROR: save_memory requires a 'content'", None
            category = str(args.get("category", "general")).strip() or "general"
            entry = {"id": "{}-{:04x}".format(int(time.time() * 1000), random.getrandbits(16)),
                     "category": category, "content": content, "created_at": time.time()}
            _add_memory(entry)
            return 'Saved to long-term memory: [{}] {}'.format(category, content), None
        if name == "list_memories":
            with _MEMORIES_LOCK:
                memories = _read_memories()
            if not memories:
                return "No memories saved yet.", None
            lines = ["Saved memories:"]
            for n, m in enumerate(memories, 1):
                dt = datetime.fromtimestamp(float(m.get("created_at", 0)))
                lines.append("[{}] {} | [{}] | {} | {:%Y-%m-%d %H:%M:%S}".format(
                    n, m.get("id", "?"), m.get("category", "?"), m.get("content", ""), dt))
            return "\n".join(lines), None
        if name == "delete_memory":
            mid = str(args.get("id", "")).strip()
            if not mid:
                return "ERROR: delete_memory requires an 'id'", None
            if _remove_memory(mid):
                return "Deleted.", None
            return "Not found.", None
        if name == "media_control":
            action = str(args.get("action", "")).strip()
            if action not in ("play", "pause", "next", "prev", "status"):
                return "ERROR: media_control action must be one of play/pause/next/prev/status", None
            if action == "status":
                ok, out = _run_cmd(["playerctl", "metadata", "--format", "{{artist}} | {{title}}"], timeout=10)
                if not ok or "no players" in out.lower():
                    return "No player is running.", None
                artist, _, title = out.partition("|")
                return "Now playing: {} by {}".format(title.strip(), artist.strip()), None
            ok, out = _run_cmd(["playerctl", "previous" if action == "prev" else action], timeout=10)
            if not ok:
                return "ERROR: {}".format(out), None
            return {"play": "Playing", "pause": "Paused",
                    "next": "Skipped", "prev": "Skipped"}[action], None
        if name == "play_song":
            query = str(args.get("query", "")).strip()
            if not query:
                return "ERROR: play_song requires a 'query'", None
            sp = _spotify_client()
            if sp is None:
                return ("Spotify not authorized. Please run 'python3 zen_ollama_proxy.py "
                        "--spotify-auth' in your terminal first."), None
            try:
                results = sp.search(query, type="track", limit=1)
                items = (results.get("tracks") or {}).get("items") or []
                if not items:
                    return 'No Spotify results for "{}".'.format(query), None
                track = items[0]
                artist = track["artists"][0]["name"] if track.get("artists") else "unknown"
                label = 'Playing {} by {}'.format(track["name"], artist)
                uri = track["uri"]
                devices = (sp.devices() or {}).get("devices", [])
                target = next((d for d in devices if d.get("type") == "Computer"), None)
                if target is None and devices:
                    target = devices[0]
                if target is None:
                    _launch_spotify()
                    try:
                        for _ in range(12):
                            time.sleep(0.5)
                            if (sp.devices() or {}).get("devices"):
                                break
                    except Exception:
                        pass
                    devices = (sp.devices() or {}).get("devices", [])
                    target = next((d for d in devices if d.get("type") == "Computer"), None)
                    if target is None and devices:
                        target = devices[0]
                    if target is None:
                        return ("No Spotify devices found — open the Spotify desktop "
                                "client (logged in) and try again."), None
                ok, err = _play_on_device(sp, target["id"], [uri])
                if not ok:
                    return err, None
                return label, None
            except Exception as e:
                log_err("spotify error: {}".format(e))
                return "ERROR: Spotify playback failed: {}".format(e), None
        if name == "write_file":
            path = os.path.expanduser(str(args.get("path", "")))
            content = str(args.get("content", ""))
            if not path.strip():
                return "ERROR: write_file requires a 'path'", None
            real = os.path.realpath(path)
            if real == os.path.realpath(__file__) or os.path.basename(real) in FORBIDDEN_WRITE_FILES:
                return "ERROR: refusing to write to protected file {}".format(real), None
            if any(real == p or real.startswith(p + "/") for p in SYSTEM_DIR_PREFIXES):
                return "ERROR: refusing to write to a system directory ({})".format(real), None
            if not _check_approval("write_file", args, messages):
                token = _issue_approval_token("write_file", args)
                return ("ACTION NOT CONFIRMED — I did NOT write '{}'. Ask the user "
                        "to confirm this exact write by replying with: "
                        "confirm {}".format(path, token)), None
            try:
                os.makedirs(os.path.dirname(real), exist_ok=True)
                with open(real, "w", encoding="utf-8") as f:
                    f.write(content)
                return "Wrote {} bytes to {}".format(len(content.encode("utf-8")), path), None
            except OSError as e:
                return "ERROR: failed to write {}: {}".format(path, e), None
        if name == "open_application":
            app = str(args.get("app", "")).strip()
            if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$", app):
                return ("ERROR: open_application takes a plain application name only "
                        "(no arguments, no shell metacharacters)"), None
            if not _check_approval("open_application", args, messages):
                token = _issue_approval_token("open_application", args)
                return ("ACTION NOT CONFIRMED — I did NOT launch '{}'. Ask the user "
                        "to confirm by replying with: confirm {}".format(app, token)), None
            exe = shutil.which(app)
            if not exe:
                return "ERROR: no executable '{}' found in PATH".format(app), None
            try:
                subprocess.Popen([exe], start_new_session=True,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return "Launched {}".format(app), None
            except OSError as e:
                return "ERROR: failed to launch {}: {}".format(app, e), None
    except Exception as e:
        log_err("tool '{}' crashed: {}".format(name, e))
        return "ERROR: tool execution failed: {}".format(e), None
    return "ERROR: unknown tool '{}'".format(name), None

_local_models_cache = {"t": 0.0, "names": []}


def log_err(msg):
    print(f"[zen_ollama_proxy] {msg}", file=sys.stderr)


def local_models():
    """Cached (30s) list of model names present in the local Ollama."""
    now = time.time()
    if now - _local_models_cache["t"] < 30 and _local_models_cache["names"]:
        return _local_models_cache["names"]
    names = []
    try:
        req = Request(LOCAL_OLLAMA_URL + "/api/tags", method="GET")
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        names = [m.get("name") for m in data.get("models", []) if m.get("name")]
        _local_models_cache.update(t=now, names=names)
    except Exception as e:
        log_err(f"failed to fetch local models from {LOCAL_OLLAMA_URL}: {e}")
    return names


def zen_models():
    if not ZEN_API_KEY:
        log_err("ZEN_API_KEY not set - skipping Zen models")
        return []
    try:
        req = Request(ZEN_BASE + "/models",
                      headers={"Authorization": "Bearer " + ZEN_API_KEY,
                               "User-Agent": "curl/8.0.0"})
        with urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        return [m.get("id") or m.get("name") for m in data.get("data", []) if (m.get("id") or m.get("name"))]
    except Exception as e:
        log_err(f"failed to fetch Zen models: {e}")
        return []


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("[zen_ollama_proxy] " + fmt % args + "\n")

    # ---- response helpers ---------------------------------------------------

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        return self.rfile.read(length) if length else b""

    def _send_json(self, status, obj):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _start_chunked(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

    def _chunk(self, data):
        if not data:
            return
        self.wfile.write(b"%x\r\n" % len(data))
        self.wfile.write(data)
        self.wfile.write(b"\r\n")
        self.wfile.flush()

    def _end_chunked(self):
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def _forward_upstream_error(self, err):
        try:
            detail = err.read().decode(errors="replace")
        except Exception:
            detail = ""
        self.log_message("upstream error %s: %s", err.code, detail[:2000])
        self._send_json(err.code if err.code else 502, {"error": detail or str(err)})

    # ---- routing -------------------------------------------------------------

    def do_GET(self):
        if self.path == "/api/tags":
            return self._handle_tags()
        self._send_json(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/api/chat":
            return self._handle_chat()
        if self.path == "/api/generate":
            return self._handle_generate()
        self._send_json(404, {"error": "not found"})

    def _handle_tags(self):
        merged = []
        try:
            req = Request(LOCAL_OLLAMA_URL + "/api/tags", method="GET")
            with urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            merged.extend(data.get("models", []))
        except Exception as e:
            log_err(f"failed to fetch local models for /api/tags: {e}")
        for m in zen_models():
            if not is_free_model(m):
                continue
            display = FREE_MODEL_NAMES.get(m, m)
            merged.append({
                "name": display,
                "model": m,
                "size": 0,
                "modified_at": "",
                "digest": "",
                "details": {"parameter_size": "", "quantization_level": ""},
            })
        self._send_json(200, {"models": merged})

    def _relay_stream(self, url, payload):
        """Forward a POST body to upstream and stream its response straight through."""
        req = Request(url, data=json.dumps(payload).encode(), method="POST",
                      headers={"Content-Type": "application/json"})
        try:
            upstream = urlopen(req, timeout=None)
        except HTTPError as e:
            return self._forward_upstream_error(e)
        except Exception as e:
            log_err(f"local upstream request failed: {e}")
            return self._send_json(502, {"error": str(e)})
        self._start_chunked()
        try:
            while True:
                line = upstream.readline()
                if not line:
                    break
                self._chunk(line)
        except Exception as e:
            log_err(f"error while relaying stream: {e}")
        finally:
            upstream.close()
        self._end_chunked()

    def _zen_messages(self, messages, model):
        """Translate Ollama messages (incl. base64 'images') to OpenAI format."""
        is_vision = any(h in (model or "").lower() for h in VISION_MODEL_HINTS)
        out = []
        for m in messages:
            m = dict(m)
            m.setdefault("type", "tool" if m.get("role") == "tool" else "message")
            images = m.pop("images", None) or []
            if images:
                if is_vision:
                    content = [{"type": "text", "text": m.get("content", "")}]
                    for b64 in images:
                        content.append({"type": "image_url",
                                        "image_url": {"url": "data:image/jpeg;base64," + b64}})
                    m["content"] = content
                else:
                    log_err(f"dropping {len(images)} image(s): model '{model}' not in vision allowlist")
                    note = "[Image attached but model is text-only; description unavailable]"
                    text = (m.get("content") or "").strip()
                    m["content"] = note if not text else text + "\n\n" + note
            out.append(m)
        return out

    def _handle_chat(self):
        try:
            payload = json.loads(self._read_body() or b"{}")
        except ValueError:
            return self._send_json(400, {"error": "invalid JSON body"})

        model = payload.get("model", "")
        if model in local_models():
            self.log_message("chat: model '%s' -> local Ollama", model)
            return self._relay_stream(LOCAL_OLLAMA_URL + "/api/chat", payload)

        if not ZEN_API_KEY:
            return self._send_json(502, {"error": f"model '{model}' not local and ZEN_API_KEY not set"})

        zen_model = model[len("opencode/"):] if model.startswith("opencode/") else model
        zen_model = DISPLAY_TO_REAL.get(zen_model, zen_model)
        if zen_model in BROKEN_FREE_MODELS:
            return self._send_json(422, {"error": f"model '{model}' is unavailable "
                                                  "(known broken upstream model)"})
        self.log_message("chat: model '%s' -> Zen (%s)", model, zen_model)
        zen_payload = dict(payload)
        zen_payload["model"] = zen_model
        zen_payload["messages"] = self._zen_messages(payload.get("messages", []), zen_model)
        zen_payload["messages"].insert(0, {
            "role": "system", "type": "message",
            "content": "Current local date and time: {:%Y-%m-%d %H:%M:%S %A} — use this to "
                       "compute 'at_time' and 'delay_seconds' for set_reminder.".format(datetime.now())})
        with _MEMORIES_LOCK:
            memories = _read_memories()
        if memories:
            lines = ["[LONG-TERM MEMORY]",
                     "You know the following about the user:"]
            lines += ["- [{}]: {}".format(m.get("category", "general"),
                                          m.get("content", "")) for m in memories]
            zen_payload["messages"].insert(1, {
                "role": "system", "type": "message",
                "content": "\n".join(lines)})
        zen_payload["tools"] = OUR_TOOLS
        if payload.get("stream", True):
            self._zen_chat_stream(zen_payload)
        else:
            self._zen_chat_once(zen_payload)

    def _zen_stream_round(self, zen_payload):
        """Open one upstream Zen request. Returns ('ok', upstream) or ('error', (status, detail))."""
        req = Request(ZEN_BASE + "/chat/completions",
                      data=json.dumps(zen_payload).encode(), method="POST",
                      headers={"Content-Type": "application/json",
                               "Authorization": "Bearer " + ZEN_API_KEY,
                               "User-Agent": "curl/8.0.0"})
        try:
            upstream = urlopen(req, timeout=None)
        except HTTPError as e:
            return ("error", self._read_upstream_error(e))
        except Exception as e:
            log_err(f"Zen request failed: {e}")
            return ("error", (502, str(e)))
        return ("ok", upstream)

    def _read_upstream_error(self, err):
        try:
            detail = err.read().decode(errors="replace")
        except Exception:
            detail = ""
        return err.code if err.code else 502, detail or str(err)

    def _read_round(self, upstream):
        """Read one SSE round from an open upstream, relaying content to the panel.
        Returns the completed tool_calls list ([] if the round has none)."""
        tool_acc = {}
        try:
            for raw in upstream:
                line = raw.decode(errors="replace").rstrip("\r\n")
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data or data == "[DONE]":
                    continue
                try:
                    evt = json.loads(data)
                except ValueError:
                    continue
                delta = (evt.get("choices") or [{}])[0].get("delta") or {}
                msg = {"role": "assistant", "content": ""}
                content = delta.get("content") or ""
                thinking = delta.get("reasoning_content") or delta.get("reasoning") or ""
                if thinking:
                    msg["thinking"] = thinking
                if content:
                    msg["content"] = content
                for tc in delta.get("tool_calls") or []:
                    idx = tc.get("index", 0)
                    acc = tool_acc.setdefault(idx, {"function": {"name": "", "arguments": ""}})
                    if tc.get("id"):
                        acc["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        acc["function"]["name"] = fn["name"]
                    if fn.get("arguments"):
                        acc["function"]["arguments"] += fn["arguments"]
                if content or thinking:
                    self._chunk(json.dumps({"message": msg, "done": False}).encode() + b"\n")
        except Exception as e:
            log_err(f"error while streaming from Zen: {e}")
        finally:
            upstream.close()
        calls = []
        for idx in sorted(tool_acc):
            acc = tool_acc[idx]
            try:
                args = json.loads(acc["function"]["arguments"]) if acc["function"]["arguments"] else {}
            except ValueError:
                args = {}
            calls.append({"id": acc.get("id", ""),
                          "function": {"name": acc["function"]["name"], "arguments": args}})
        return calls

    def _zen_chat_stream(self, zen_payload):
        zen_payload["stream"] = True
        status, res = self._zen_stream_round(zen_payload)
        if status == "error":
            self._send_json(res[0], {"error": res[1]})
            return
        self._start_chunked()
        rounds = 1
        try:
            while True:
                tool_calls = self._read_round(res)
                if not tool_calls:
                    break
                unknown = [t for t in tool_calls if t["function"]["name"] not in EXECUTABLE_TOOLS]
                if unknown:
                    log_err("forwarded unknown tool call(s) to panel: %s",
                            [t["function"]["name"] for t in unknown])
                    self._chunk(json.dumps({"message": {"role": "assistant", "content": "",
                                                        "tool_calls": tool_calls},
                                            "done": False}).encode() + b"\n")
                    break
                if all(t["function"]["name"] == "get_current_date" for t in tool_calls):
                    answer = datetime.now().strftime("%A, %B %-d, %Y")
                    log_err(f"answered get_current_date locally: {answer}")
                    self._chunk(json.dumps({"message": {"role": "assistant", "content": answer},
                                            "done": False}).encode() + b"\n")
                    break
                if rounds >= MAX_TOOL_ROUNDS:
                    log_err("tool loop reached MAX_TOOL_ROUNDS")
                    self._chunk(json.dumps({"message": {"role": "assistant",
                                                        "content": "*(Tool-call limit reached; stopped.)*"},
                                            "done": False}).encode() + b"\n")
                    break
                zen_payload["messages"] = zen_payload["messages"] + [
                    {"role": "assistant", "content": "", "type": "message",
                     "tool_calls": _replay_tool_calls(tool_calls)}]
                for tc in tool_calls:
                    tname = tc["function"]["name"]
                    result_text, image_path = _execute_tool(tname, tc["function"]["arguments"],
                                                            zen_payload["model"],
                                                            zen_payload["messages"])
                    log_err(f"tool '{tname}' -> {_redact_secrets(result_text[:120])!r}")
                    content = result_text if isinstance(result_text, str) else json.dumps(result_text)
                    zen_payload["messages"].append(
                        {"role": "tool", "tool_call_id": tc.get("id", ""), "type": "tool",
                         "content": content})
                    if image_path and _is_vision(zen_payload["model"]):
                        zen_payload["messages"].append(
                            {"role": "user", "type": "message",
                             "content": _image_content(result_text, image_path)})
                rounds += 1
                status, res = self._zen_stream_round(zen_payload)
                if status == "error":
                    log_err(f"zen round {rounds} failed: {_redact_secrets(res[1][:4000])}")
                    log_err(f"zen round {rounds} failing request payload: "
                            f"{json.dumps(_redact_payload(zen_payload))[:10000]}")
                    self._chunk(json.dumps({"message": {"role": "assistant",
                                                        "content": f"*(Upstream error: {res[1][:300]})*"},
                                            "done": False}).encode() + b"\n")
                    break
            self._chunk(json.dumps({"message": {"role": "assistant", "content": ""}, "done": True}).encode() + b"\n")
        except Exception as e:
            log_err(f"error while streaming from Zen: {e}")
        self._end_chunked()

    def _zen_chat_once(self, zen_payload):
        zen_payload["stream"] = False
        rounds = 0
        while True:
            req = Request(ZEN_BASE + "/chat/completions",
                          data=json.dumps(zen_payload).encode(), method="POST",
                          headers={"Content-Type": "application/json",
                                   "Authorization": "Bearer " + ZEN_API_KEY,
                                   "User-Agent": "curl/8.0.0"})
            try:
                with urlopen(req, timeout=UPSTREAM_TIMEOUT) as resp:
                    data = json.loads(resp.read())
            except HTTPError as e:
                return self._forward_upstream_error(e)
            except Exception as e:
                log_err(f"Zen request failed: {e}")
                return self._send_json(502, {"error": str(e)})
            msg = (data.get("choices") or [{}])[0].get("message") or {}
            tool_calls = msg.get("tool_calls") or []
            if not tool_calls:
                return self._send_json(200, {"message": {"role": "assistant",
                                                         "content": msg.get("content") or ""}, "done": True})
            calls = []
            for tc in tool_calls:
                fn = tc.get("function") or {}
                calls.append({"id": tc.get("id", ""),
                              "function": {"name": fn.get("name", ""),
                                           "arguments": fn.get("arguments") or {}}})
            unknown = [c for c in calls if c["function"]["name"] not in EXECUTABLE_TOOLS]
            if unknown:
                note = ("\n\n*(proxy: requested unknown tool {} — not executed)*"
                        .format([c["function"]["name"] for c in unknown]))
                return self._send_json(200, {"message": {"role": "assistant",
                                                         "content": (msg.get("content") or "") + note},
                                             "done": True})
            if all(c["function"]["name"] == "get_current_date" for c in calls):
                answer = datetime.now().strftime("%A, %B %-d, %Y")
                log_err(f"answered get_current_date locally: {answer}")
                return self._send_json(200, {"message": {"role": "assistant", "content": answer}, "done": True})
            rounds += 1
            if rounds >= MAX_TOOL_ROUNDS:
                log_err("tool loop reached MAX_TOOL_ROUNDS")
                return self._send_json(200, {"message": {"role": "assistant",
                                                         "content": "*(Tool-call limit reached; stopped.)*"},
                                             "done": True})
            zen_payload["messages"] = zen_payload["messages"] + [
                {"role": "assistant", "content": msg.get("content") or "", "type": "message",
                 "tool_calls": _replay_tool_calls(calls)}]
            for c in calls:
                tname = c["function"]["name"]
                result_text, image_path = _execute_tool(tname, c["function"]["arguments"],
                                                        zen_payload["model"],
                                                        zen_payload["messages"])
                log_err(f"tool '{tname}' -> {_redact_secrets(result_text[:120])!r}")
                content = result_text if isinstance(result_text, str) else json.dumps(result_text)
                zen_payload["messages"].append(
                    {"role": "tool", "tool_call_id": c.get("id", ""), "type": "tool",
                     "content": content})
                if image_path and _is_vision(zen_payload["model"]):
                    zen_payload["messages"].append(
                        {"role": "user", "type": "message",
                         "content": _image_content(result_text, image_path)})

    def _handle_generate(self):
        try:
            payload = json.loads(self._read_body() or b"{}")
        except ValueError:
            return self._send_json(400, {"error": "invalid JSON body"})

        model = payload.get("model", "")
        if model in local_models():
            self.log_message("generate: model '%s' -> local Ollama", model)
            return self._relay_stream(LOCAL_OLLAMA_URL + "/api/generate", payload)

        if not ZEN_API_KEY:
            return self._send_json(502, {"error": f"model '{model}' not local and ZEN_API_KEY not set"})

        zen_model = model[len("opencode/"):] if model.startswith("opencode/") else model
        zen_model = DISPLAY_TO_REAL.get(zen_model, zen_model)
        if zen_model in BROKEN_FREE_MODELS:
            return self._send_json(422, {"error": f"model '{model}' is unavailable "
                                                  "(known broken upstream model)"})
        self.log_message("generate: model '%s' -> Zen (%s)", model, zen_model)
        messages = []
        if payload.get("system"):
            messages.append({"role": "system", "content": payload["system"]})
        messages.append({"role": "user", "content": payload.get("prompt", "")})
        zen_payload = {"model": zen_model, "messages": messages, "stream": False}
        req = Request(ZEN_BASE + "/chat/completions",
                      data=json.dumps(zen_payload).encode(), method="POST",
                      headers={"Content-Type": "application/json",
                               "Authorization": "Bearer " + ZEN_API_KEY,
                               "User-Agent": "curl/8.0.0"})
        try:
            with urlopen(req, timeout=UPSTREAM_TIMEOUT) as resp:
                data = json.loads(resp.read())
        except HTTPError as e:
            return self._forward_upstream_error(e)
        except Exception as e:
            log_err(f"Zen request failed: {e}")
            return self._send_json(502, {"error": str(e)})
        content = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        self._send_json(200, {"response": content, "done": True})


def main():
    parser = argparse.ArgumentParser(description="OpenCode Zen to Ollama proxy")
    parser.add_argument("--spotify-auth", action="store_true",
                        help="Run the Spotify OAuth flow in this terminal, save the token, and exit")
    args = parser.parse_args()
    if args.spotify_auth:
        if spotipy is None:
            print("spotipy is not installed — re-run install.sh with the Spotify feature enabled.")
            sys.exit(1)
        if not (SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET):
            print("SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET not set in ~/.env — add them first.")
            sys.exit(1)
        _spotify_oauth().get_access_token()
        print("Auth successful, token saved")
        sys.exit(0)
    n = _reschedule_reminders()
    if n:
        print(f"[zen_ollama_proxy] re-armed {n} pending reminder(s) from reminders.json")
    server = ThreadingHTTPServer(("127.0.0.1", PROXY_PORT), ProxyHandler)
    print(f"[zen_ollama_proxy] listening on 127.0.0.1:{PROXY_PORT}")
    print(f"[zen_ollama_proxy] local Ollama fallback: {LOCAL_OLLAMA_URL}")
    print(f"[zen_ollama_proxy] Zen API: {ZEN_BASE} | key {'set' if ZEN_API_KEY else 'NOT SET'}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[zen_ollama_proxy] shutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
