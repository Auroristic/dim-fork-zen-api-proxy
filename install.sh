#!/usr/bin/env bash
set -euo pipefail

# ── CONFIG ── keep in sync with zen_ollama_proxy.py (port, env vars, deps) ──
REPO_URL="https://github.com/Auroristic/dim-fork-zen-api-proxy.git"
REPO_DIR="$HOME/dim-fork-zen-api-proxy"
SCRIPT="$REPO_DIR/zen_ollama_proxy.py"
ENV_FILE="$REPO_DIR/.env"
SYSTEMD_DIR="$HOME/.config/systemd/user"
SERVICE="zen-ollama-proxy"
RESTARTER="${SERVICE}-restart"
PORT=11434
PKG_NOTIFY_APT="libnotify-bin"
PKG_NOTIFY_DNF="libnotify"
PKG_NOTIFY_PACMAN="libnotify"
FEAT_NOTIFY=1
FEAT_TAVILY=0
FEAT_PATH=1
FEAT_SPOTIFY=0
# ── /CONFIG ──

PUR="\033[1;35m"; YEL="\033[1;33m"; GRN="\033[1;32m"; CYN="\033[1;36m"; R="\033[1;31m"; RST="\033[0m"
if [ ! -t 1 ]; then PUR=""; YEL=""; GRN=""; CYN=""; R=""; RST=""; fi

banner() {
    printf "${PUR}╭─────────────────────────────────────╮${RST}\n"
    printf "${PUR}│${RST}      ${PUR}🚀 ZEN PROXY INSTALLER 🚀${RST}      ${PUR}│${RST}\n"
    printf "${PUR}╰─────────────────────────────────────╯${RST}\n\n"
}

sec_open() { printf "${PUR}┌ ◈ %s${RST}\n" "$*"; }
sec_line() { printf "│ ❯ %s\n" "$*"; }
sec_note() { printf "│ ${YEL}%s${RST}\n" "$*"; }
sec_ok()   { printf "└ ✔ ${GRN}%s${RST}\n\n" "$*"; }
sec_fail() { printf "└ ✘ ${R}%s${RST}\n" "$*" >&2; exit 1; }

feat_mark() { [ "$1" = 1 ] && printf "✔" || printf " "; }

cleanup() { printf "\n│ ${YEL}Aborted by user.${RST}\n"; exit 130; }
trap cleanup INT TERM

trim() { sed -e 's/^[[:space:]"'"'"']*//' -e 's/[[:space:]"'"'"']*$//' <<< "$1"; }

checkbox_menu() {
    while true; do
        printf "${PUR}┌ ◈ Optional features — type a number to toggle, d when done${RST}\n"
        printf "│ ${CYN}[1]${RST} [%s] Desktop notifications (install libnotify-bin if missing)\n" "$(feat_mark "$FEAT_NOTIFY")"
        printf "│ ${CYN}[2]${RST} [%s] Tavily API key for web_search\n" "$(feat_mark "$FEAT_TAVILY")"
        printf "│ ${CYN}[3]${RST} [%s] Auto-restart on code changes (.path unit)\n" "$(feat_mark "$FEAT_PATH")"
        printf "│ ${CYN}[4]${RST} [%s] Spotify integration (installs spotipy, requires manual API key setup)\n" "$(feat_mark "$FEAT_SPOTIFY")"
        printf "└ ${CYN}Choice: ${RST}"
        read -r choice || choice="d"
        case "$choice" in
            1) [ "$FEAT_NOTIFY" = 1 ] && FEAT_NOTIFY=0 || FEAT_NOTIFY=1 ;;
            2) [ "$FEAT_TAVILY" = 1 ] && FEAT_TAVILY=0 || FEAT_TAVILY=1 ;;
            3) [ "$FEAT_PATH" = 1 ] && FEAT_PATH=0 || FEAT_PATH=1 ;;
            4) [ "$FEAT_SPOTIFY" = 1 ] && FEAT_SPOTIFY=0 || FEAT_SPOTIFY=1 ;;
            d|D) break ;;
            *) printf "│ ${YEL}Invalid choice — enter 1-4 to toggle, d when done${RST}\n" ;;
        esac
    done
}

port_in_use() {
    if command -v ss >/dev/null 2>&1; then
        ss -ltnH "sport = :$PORT" 2>/dev/null | grep -q .
    elif command -v netstat >/dev/null 2>&1; then
        netstat -ltn 2>/dev/null | grep -qE "[:.]$PORT[[:space:]]"
    else
        curl -sf -m 2 "http://127.0.0.1:$PORT/api/tags" >/dev/null 2>&1
    fi
}

banner
sec_open "Checking dependencies..."
[ -t 0 ] || sec_fail "Run this script interactively (needs a TTY for prompts)."
[ "$EUID" -eq 0 ] && sec_fail "Do not run as root — this uses systemd --user."
for c in python3 git curl systemctl; do
    if command -v "$c" >/dev/null 2>&1; then
        sec_line "$c found"
    else
        sec_fail "Missing required command: $c"
    fi
done
if command -v sudo >/dev/null 2>&1; then
    sec_note "Note: sudo may prompt for your password"
else
    sec_line "sudo not found — package installs will be skipped."
fi
if port_in_use; then
    sec_fail "Port $PORT is already in use — stop whatever is listening and re-run."
else
    sec_line "port $PORT is free"
fi
PYTHON="$(command -v python3)"
sec_ok "Dependencies verified"

checkbox_menu
printf "│ ${YEL}(use d to confirm the selection above)${RST}\n\n"

sec_open "API keys"
keep_env=0
if [ -f "$ENV_FILE" ]; then
    existing_zen="$(grep -E '^[[:space:]]*(export[[:space:]]+)?ZEN_API_KEY=' "$ENV_FILE" | tail -1 || true)"
fi
if [ -n "$existing_zen" ]; then
    zen_val="$(trim "$(sed -E 's/^[[:space:]]*(export[[:space:]]+)?ZEN_API_KEY=[[:space:]]*//' <<< "$existing_zen")")"
    if [ -n "$zen_val" ]; then
        prefix="${zen_val:0:6}"
        [ "${#zen_val}" -gt 6 ] && prefix="$prefix…"
        sec_line "Found $ENV_FILE — ZEN_API_KEY starts with ${GRN}$prefix${RST}"
        printf "│ ${YEL}Keep existing keys in .env?${RST} [Y/n]: "
        read -r keep_yn || keep_yn="y"
        case "$(trim "$keep_yn")" in
            ""|y|Y) keep_env=1 ;;
            *) sec_line "Re-entering keys — .env will be rewritten." ;;
        esac
    fi
fi
if [ "$keep_env" = 1 ]; then
    sec_line "Keeping existing keys — skipping ZEN_API_KEY/TAVILY_API_KEY prompts."
else
    zen_key=""
    while [ -z "$zen_key" ]; do
        printf "│ ${CYN}ZEN_API_KEY${RST} (required — shown so you can double-check it): "
        read -r zen_key; printf "\n"
        zen_key="$(trim "$zen_key")"
        [ -z "$zen_key" ] && sec_note "ZEN_API_KEY cannot be empty."
    done
    tavily=""
    if [ "$FEAT_TAVILY" = 1 ]; then
        printf "│ ${CYN}TAVILY_API_KEY${RST} (optional — Enter to skip): "
        read -r tavily; printf "\n"
        tavily="$(trim "$tavily")"
    else
        sec_line "Skipping TAVILY_API_KEY (web_search will report unavailable)."
    fi
fi
sec_ok "API keys collected"

sec_open "Desktop notifications"
if command -v notify-send >/dev/null 2>&1; then
    sec_line "notify-send already present — skipping package install."
elif [ "$FEAT_NOTIFY" = 1 ]; then
    if command -v apt-get >/dev/null 2>&1; then
        sec_note "sudo may prompt for your password"
        sudo apt-get update -qq && sudo apt-get install -y "$PKG_NOTIFY_APT"
    elif command -v dnf >/dev/null 2>&1; then
        sudo dnf install -y "$PKG_NOTIFY_DNF"
    elif command -v pacman >/dev/null 2>&1; then
        sudo pacman -S --noconfirm "$PKG_NOTIFY_PACMAN"
    else
        sec_note "No apt/dnf/pacman found — install libnotify-bin manually for desktop notifications."
    fi
else
    sec_note "Skipped — desktop notifications/reminders will not work without notify-send."
fi
sec_ok "Notifications setup"

sec_open "Spotify integration"
if [ "$FEAT_SPOTIFY" = 1 ]; then
    if python3 -c "import spotipy" >/dev/null 2>&1; then
        sec_line "spotipy already installed — skipping."
    elif command -v paru >/dev/null 2>&1; then
        paru -S --noconfirm python-spotipy || sec_note "paru install failed — try manually: paru -S python-spotipy"
        sec_line "Installed python-spotipy via paru."
    elif command -v yay >/dev/null 2>&1; then
        yay -S --noconfirm python-spotipy || sec_note "yay install failed — try manually: yay -S python-spotipy"
        sec_line "Installed python-spotipy via yay."
    else
        sec_note "No AUR helper (paru/yay) found — install manually: paru -S python-spotipy"
    fi
else
    sec_line "Skipped — no Spotify integration."
fi
sec_ok "Spotify setup"

sec_open "Ollama port setup"
if systemctl is-enabled ollama >/dev/null 2>&1 || [ -f /etc/systemd/system/ollama.service ]; then
    sec_line "Found system ollama.service — moving it off the proxy port (11434 → 11435)."
    sec_note "sudo may prompt for your password"
    if sudo mkdir -p /etc/systemd/system/ollama.service.d \
        && printf '%s\n' \
            "# Managed by zen-ollama-proxy install.sh — keeps ollama off the proxy's port" \
            "[Service]" \
            "Environment=OLLAMA_HOST=127.0.0.1:11435" \
        | sudo tee /etc/systemd/system/ollama.service.d/override.conf >/dev/null \
        && sudo systemctl daemon-reload \
        && sudo systemctl restart ollama; then
        sec_line "ollama now listens on 127.0.0.1:11435 — the proxy's port 11434 is free."
    else
        sec_note "Could not update ollama automatically. After the install, run:"
        sec_note "  sudo mkdir -p /etc/systemd/system/ollama.service.d"
        sec_note "  sudo sh -c 'printf \"[Service]\\nEnvironment=OLLAMA_HOST=127.0.0.1:11435\\n\" > /etc/systemd/system/ollama.service.d/override.conf'"
        sec_note "  sudo systemctl daemon-reload && sudo systemctl restart ollama"
        sec_note "Continuing anyway — ollama on 11434 will block the proxy until fixed."
    fi
elif systemctl --user is-enabled ollama >/dev/null 2>&1 || [ -f "$HOME/.config/systemd/user/ollama.service" ]; then
    sec_line "Found user ollama.service — moving it off the proxy port (11434 → 11435)."
    if mkdir -p "$HOME/.config/systemd/user/ollama.service.d" \
        && printf '%s\n' \
            "# Managed by zen-ollama-proxy install.sh — keeps ollama off the proxy's port" \
            "[Service]" \
            "Environment=OLLAMA_HOST=127.0.0.1:11435" \
        > "$HOME/.config/systemd/user/ollama.service.d/override.conf" \
        && systemctl --user daemon-reload \
        && systemctl --user restart ollama; then
        sec_line "ollama now listens on 127.0.0.1:11435 — the proxy's port 11434 is free."
    else
        sec_note "Could not update user ollama unit — fix manually: systemctl --user edit ollama"
    fi
else
    sec_line "No ollama service found — skipping."
fi
sec_ok "Ollama port setup"

sec_open "Repository setup"
if [ ! -d "$REPO_DIR" ]; then
    sec_line "Cloning $REPO_URL ..."
    git clone "$REPO_URL" "$REPO_DIR" || sec_fail "Clone failed — check network/URL and re-run."
else
    if git -C "$REPO_DIR" rev-parse --git-dir >/dev/null 2>&1; then
        sec_line "$REPO_DIR exists (git repository)."
        printf "│ ${CYN}git pull to update?${RST} [y/N]: "
        read -r yn || yn="n"
        case "$(trim "$yn")" in
            y|Y) git -C "$REPO_DIR" pull || sec_note "git pull failed — continuing with existing code." ;;
            *)   sec_line "Keeping existing code." ;;
        esac
    else
        sec_fail "$REPO_DIR exists but is not a git repository — move it away and re-run."
    fi
fi
[ -f "$SCRIPT" ] || sec_fail "zen_ollama_proxy.py not found in $REPO_DIR."
sec_ok "Repository ready"

sec_open "Environment (.env)"
if [ "$keep_env" = 1 ]; then
    sec_line "Left $ENV_FILE untouched — existing keys preserved."
else
    tmp_env="$(mktemp)"
    if [ -f "$ENV_FILE" ]; then
        grep -Ev '^[[:space:]]*(export[[:space:]]+)?(ZEN_API_KEY|TAVILY_API_KEY)=' "$ENV_FILE" > "$tmp_env" || true
    fi
    printf 'ZEN_API_KEY="%s"\n' "$zen_key" >> "$tmp_env"
    if [ -n "$tavily" ]; then
        printf 'TAVILY_API_KEY="%s"\n' "$tavily" >> "$tmp_env"
    fi
    mv "$tmp_env" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    sec_line "Wrote API keys to $ENV_FILE (chmod 600)."
fi
sec_ok ".env ready"

sec_open "Systemd user units"
install -d "$SYSTEMD_DIR"
cat > "$SYSTEMD_DIR/$SERVICE.service" <<EOF
[Unit]
Description=OpenCode Zen to Ollama proxy
StartLimitIntervalSec=0

[Service]
Environment="DISPLAY=:0"
Environment="XDG_RUNTIME_DIR=/run/user/%U"
ExecStart=$PYTHON $SCRIPT
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target
EOF
sec_line "Wrote $SYSTEMD_DIR/$SERVICE.service"
if [ "$FEAT_PATH" = 1 ]; then
    cat > "$SYSTEMD_DIR/$RESTARTER.service" <<EOF
[Unit]
Description=Restart zen-ollama-proxy on code change

[Service]
Type=oneshot
Environment="XDG_RUNTIME_DIR=/run/user/%U"
ExecStart=/usr/bin/systemctl --user restart $SERVICE.service
EOF
    sec_line "Wrote $SYSTEMD_DIR/$RESTARTER.service"
    cat > "$SYSTEMD_DIR/$SERVICE.path" <<EOF
[Path]
PathModified=$SCRIPT
Unit=$RESTARTER.service

[Install]
WantedBy=default.target
EOF
    sec_line "Wrote $SYSTEMD_DIR/$SERVICE.path"
else
    rm -f "$SYSTEMD_DIR/$SERVICE.path"
    systemctl --user disable "$SERVICE.path" >/dev/null 2>&1 || true
    rm -f "$SYSTEMD_DIR/$RESTARTER.service"
    systemctl --user disable "$RESTARTER.service" >/dev/null 2>&1 || true
    sec_note "Skipped .path unit — auto-restart on updates disabled."
fi
systemctl --user daemon-reload
systemctl --user enable --now "$SERVICE.service" || sec_fail "Failed to start service — see: journalctl --user -u $SERVICE -n 20"
if [ "$FEAT_PATH" = 1 ]; then
    systemctl --user enable --now "$SERVICE.path" || sec_note "Failed to enable path unit — auto-restart on updates disabled."
fi
sec_ok "Units enabled and started"

sec_open "Verifying..."
active=""
for _ in $(seq 1 15); do
    if systemctl --user is-active --quiet "$SERVICE.service"; then active=1; break; fi
    sleep 1
done
if [ -n "$active" ]; then
    sec_line "Service is active."
else
    sec_note "Service is NOT active — hint: journalctl --user -u $SERVICE -n 50 --no-pager"
fi
tags=""
for _ in $(seq 1 15); do
    tags="$(curl -sf -m 3 "http://127.0.0.1:$PORT/api/tags" 2>/dev/null || true)"
    [ -n "$tags" ] && break
    sleep 1
done
if [ -n "$tags" ]; then
    models="$(printf '%s' "$tags" | grep -o '"name"' | wc -l || true)"
    sec_line "Proxy responds on http://127.0.0.1:$PORT with $models model(s)."
else
    sec_note "Proxy not reachable yet — check the journal output above."
fi
sec_ok "Install complete"
printf "❯ ${CYN}Status:${RST}  systemctl --user status $SERVICE\n"
printf "❯ ${CYN}Logs:${RST}    journalctl --user -u $SERVICE -f\n"
printf "❯ ${CYN}Restart after editing .env:${RST}  systemctl --user restart $SERVICE\n"
printf "❯ ${CYN}Auto-restarts on code changes via the .path unit.${RST}\n"
