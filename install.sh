#!/usr/bin/env bash
# Install whisper-dictation on Ubuntu 24.04 / GNOME Wayland (or similar).
# Builds the engines from source, sets up the typing daemon, and installs the
# systemd services. Run from the repo:  ./install.sh
#
# Security note: the ydotoold socket is world-accessible (0666) so the listener
# can type via it. Fine on a single-user laptop; do not use on a shared machine.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENGINE_DIR="${WHISPER_DICT_HOME:-$HOME/.local/share/whisper-dictation}"

echo "==> whisper-dictation install"
echo "    scripts : $REPO_DIR"
echo "    engines : $ENGINE_DIR"

# --- 1. dependencies --------------------------------------------------------
echo "==> Installing dependencies (sudo)…"
sudo apt-get update
sudo apt-get install -y \
    build-essential cmake git scdoc \
    python3 python3-evdev python3-gi python3-gi-cairo gir1.2-gtk-3.0 \
    pipewire-bin wireplumber

# --- 2. whisper.cpp + models ------------------------------------------------
echo "==> Building whisper.cpp…"
mkdir -p "$ENGINE_DIR"
[ -d "$ENGINE_DIR/whisper.cpp/.git" ] || \
    git clone --depth 1 https://github.com/ggml-org/whisper.cpp "$ENGINE_DIR/whisper.cpp"
cmake -S "$ENGINE_DIR/whisper.cpp" -B "$ENGINE_DIR/whisper.cpp/build" -DCMAKE_BUILD_TYPE=Release
cmake --build "$ENGINE_DIR/whisper.cpp/build" -j

echo "==> Downloading model (base.en)…"
[ -f "$ENGINE_DIR/whisper.cpp/models/ggml-base.en.bin" ] || \
    bash "$ENGINE_DIR/whisper.cpp/models/download-ggml-model.sh" base.en

# --- 3. ydotool (client + daemon; Ubuntu's package omits the daemon) --------
echo "==> Building ydotool…"
[ -d "$ENGINE_DIR/ydotool/.git" ] || \
    git clone --depth 1 https://github.com/ReimuNotMoe/ydotool "$ENGINE_DIR/ydotool"
cmake -S "$ENGINE_DIR/ydotool" -B "$ENGINE_DIR/ydotool/build" -DCMAKE_BUILD_TYPE=Release
cmake --build "$ENGINE_DIR/ydotool/build" -j
sudo install -m 755 "$ENGINE_DIR/ydotool/build/ydotool"  /usr/local/bin/ydotool
sudo install -m 755 "$ENGINE_DIR/ydotool/build/ydotoold" /usr/local/bin/ydotoold

# --- 4. uinput + ydotoold system service ------------------------------------
echo "==> Setting up the typing daemon (sudo)…"
echo uinput | sudo tee /etc/modules-load.d/uinput.conf >/dev/null
sudo modprobe uinput || true
sudo cp "$REPO_DIR/systemd/ydotoold.service" /etc/systemd/system/ydotoold.service
sudo systemctl daemon-reload
sudo systemctl enable --now ydotoold.service

# --- 5. keyboard read access ------------------------------------------------
sudo usermod -aG input "$USER"

# --- 6. user services -------------------------------------------------------
echo "==> Installing user services…"
mkdir -p "$HOME/.config/systemd/user"
for svc in whisper-server whisper-ptt; do
    sed -e "s#@ENGINE_DIR@#$ENGINE_DIR#g" -e "s#@REPO_DIR@#$REPO_DIR#g" \
        "$REPO_DIR/systemd/$svc.service.in" > "$HOME/.config/systemd/user/$svc.service"
done
systemctl --user daemon-reload
systemctl --user enable --now whisper-server whisper-ptt

echo
echo "==> Done."
echo "    The 'input' group was just granted — LOG OUT AND BACK IN once so the"
echo "    keyboard listener can read your keyboard, then hold Print Screen to dictate."
