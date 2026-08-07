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
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen
from urllib.error import HTTPError


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


def _execute_tool(name, args, zen_model):
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
                                                            zen_payload["model"])
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
                                                        zen_payload["model"])
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
