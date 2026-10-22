#!/usr/bin/env bash
# Remove whisper-dictation from Linux: what install.sh (or get.sh) set up.
#   curl -fsSL https://raw.githubusercontent.com/plastic-eater/whisper-dictation/main/uninstall.sh | bash
# Leaves the apt packages (other software may use them) and any git clone you
# ran install.sh from yourself.

set -euo pipefail

# Everything is inside main, called on the last line, so a download cut short
# runs nothing.
main() {
    local home="${WHISPER_DICT_HOME:-$HOME/.local/share/whisper-dictation}"
    local conf="$HOME/.config/whisper-dictation"
    local units="$HOME/.config/systemd/user"

    echo "==> whisper-dictation uninstall"
    echo "    Stops and removes the dictation services, ydotool, and $home"
    echo "    (engines, models, and the copy get.sh installs)."
    ask "Continue?" || { echo "    Nothing removed."; return; }

    echo "==> Removing user services…"
    systemctl --user disable --now whisper-ptt whisper-server 2>/dev/null || true
    rm -f "$units/whisper-ptt.service" "$units/whisper-server.service"
    systemctl --user daemon-reload

    echo "==> Removing the typing daemon (sudo)…"
    sudo systemctl disable --now ydotoold.service 2>/dev/null || true
    sudo rm -f /etc/systemd/system/ydotoold.service /usr/local/bin/ydotool /usr/local/bin/ydotoold \
        /etc/modules-load.d/uinput.conf
    sudo systemctl daemon-reload

    if id -nG "$USER" | grep -qw input \
        && ask "Remove $USER from the 'input' group? Say no if anything else reads your keyboard or controllers."; then
        sudo gpasswd -d "$USER" input
    fi

    if ask "Also delete your settings ($conf) and tuning_local.py?"; then
        rm -rf "$conf"
    elif [ -f "$home/src/tuning_local.py" ]; then
        mkdir -p "$conf"
        mv "$home/src/tuning_local.py" "$conf/tuning_local.py"
        echo "    Saved your tuning_local.py to $conf/ (after a reinstall, copy it back next to the code)"
    fi

    echo "==> Deleting $home…"
    rm -rf "$home"
    echo "==> Done."
}

# Yes only on an explicit y. Piped from curl, stdin is this script, so read the
# terminal; with no terminal, the answer is no.
ask() {
    local a=""
    if (: </dev/tty) 2>/dev/null; then read -r -p "    $1 [y/N] " a </dev/tty || true; fi
    [[ "$a" =~ ^[Yy] ]]
}

main "$@"
