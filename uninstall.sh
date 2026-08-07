#!/usr/bin/env bash
set -euo pipefail

# ── CONFIG ── keep in sync with zen_ollama_proxy.py (port, env vars, deps) ──
SERVICE="zen-ollama-proxy"
REPO_DIR="$HOME/dim-fork-zen-api-proxy"
SYSTEMD_DIR="$HOME/.config/systemd/user"
FEAT_REPO=0
FEAT_LINGER=0
# ── /CONFIG ──

PUR="\033[1;35m"; YEL="\033[1;33m"; GRN="\033[1;32m"; CYN="\033[1;36m"; R="\033[1;31m"; RST="\033[0m"
if [ ! -t 1 ]; then PUR=""; YEL=""; GRN=""; CYN=""; R=""; RST=""; fi

banner() {
    printf "${PUR}╭───────────────────────────────────────╮${RST}\n"
    printf "${PUR}│${RST}      ${PUR}🗑 ZEN PROXY UNINSTALLER 🗑${RST}      ${PUR}│${RST}\n"
    printf "${PUR}╰───────────────────────────────────────╯${RST}\n\n"
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
        printf "${PUR}┌ ◈ Optional removals — type a number to toggle, d when done${RST}\n"
        printf "│ ${CYN}[1]${RST} [%s] Delete repo directory (contains .env API keys + reminders.json)\n" "$(feat_mark "$FEAT_REPO")"
        printf "│ ${CYN}[2]${RST} [%s] Disable linger for this user\n" "$(feat_mark "$FEAT_LINGER")"
        printf "└ ${CYN}Choice: ${RST}"
        read -r choice || choice="d"
        case "$choice" in
            1) [ "$FEAT_REPO" = 1 ] && FEAT_REPO=0 || FEAT_REPO=1 ;;
            2) [ "$FEAT_LINGER" = 1 ] && FEAT_LINGER=0 || FEAT_LINGER=1 ;;
            d|D) break ;;
            *) printf "│ ${YEL}Invalid choice — enter 1-2 to toggle, d when done${RST}\n" ;;
        esac
    done
}

[ -t 0 ] || sec_fail "Run this script interactively (needs a TTY for prompts)."
[ "$EUID" -eq 0 ] && sec_fail "Do not run as root — this targets systemd --user."

banner
sec_open "Detecting installation..."
if systemctl --user is-active --quiet "$SERVICE.service"; then
    sec_line "service '$SERVICE' is active"
else
    sec_line "service '$SERVICE' is not active"
fi
if [ -f "$SYSTEMD_DIR/$SERVICE.service" ]; then
    sec_line "unit file present: $SYSTEMD_DIR/$SERVICE.service"
else
    sec_line "unit file absent: $SYSTEMD_DIR/$SERVICE.service"
fi
if [ -f "$SYSTEMD_DIR/$SERVICE.path" ]; then
    sec_line "unit file present: $SYSTEMD_DIR/$SERVICE.path"
else
    sec_line "unit file absent: $SYSTEMD_DIR/$SERVICE.path"
fi
if [ -d "$REPO_DIR" ]; then
    sec_line "repo directory present: $REPO_DIR"
else
    sec_line "repo directory absent: $REPO_DIR"
fi
if [ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null || true)" = "1" ] || [ -f "/var/lib/systemd/linger/$USER" ]; then
    sec_line "linger is enabled for $USER"
else
    sec_line "linger is not enabled for $USER"
fi
sec_ok "Detection complete"

checkbox_menu
printf "│ ${YEL}(use d to confirm the selection above)${RST}\n\n"

sec_open "Final confirmation"
if [ "$FEAT_REPO" = 1 ]; then
    sec_note "WARNING: $REPO_DIR will be DELETED (contains .env API keys and reminders.json)."
fi
printf "│ ${YEL}Type ${RST}${PUR}uninstall${RST}${YEL} to confirm removal:${RST} "
read -r confirm || confirm=""
if [ "$(trim "$confirm")" != "uninstall" ]; then
    sec_note "Aborted — nothing was removed. Re-run and type 'uninstall' to proceed."
    exit 1
fi
sec_ok "Confirmed"

sec_open "Removing..."
systemctl --user disable --now "$SERVICE.service" || true
sec_line "disabled $SERVICE.service"
systemctl --user disable --now "$SERVICE.path" || true
sec_line "disabled $SERVICE.path"
rm -f "$SYSTEMD_DIR/$SERVICE.service" "$SYSTEMD_DIR/$SERVICE.path" || true
sec_line "removed unit files"
systemctl --user daemon-reload || true
systemctl --user reset-failed "$SERVICE.service" || true
if [ "$FEAT_REPO" = 1 ]; then
    rm -rf "$REPO_DIR" || true
    sec_line "Removed $REPO_DIR (.env API keys and reminders.json deleted)."
fi
if [ "$FEAT_LINGER" = 1 ]; then
    if loginctl disable-linger "$USER" 2>/dev/null; then
        sec_line "Linger disabled for $USER."
    else
        sec_note "Could not disable linger — remove /var/lib/systemd/linger/$USER manually."
    fi
fi
sec_ok "Removal done"

sec_open "Verifying..."
if systemctl --user is-active --quiet "$SERVICE.service"; then
    sec_note "Service is still active — investigate manually."
else
    sec_line "Service is stopped."
fi
if [ -f "$SYSTEMD_DIR/$SERVICE.service" ] || [ -f "$SYSTEMD_DIR/$SERVICE.path" ]; then
    sec_note "Unit file(s) still present — remove them manually."
else
    sec_line "Unit files removed."
fi
sec_ok "Uninstall complete"
printf "❯ ${CYN}Note:${RST} If you were also running the proxy manually from a terminal, stop that instance yourself (Ctrl+C).\n"
