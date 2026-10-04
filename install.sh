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
# Separate so a system without these packages still installs, CPU-only.
sudo apt-get install -y libvulkan-dev glslc spirv-headers \
    || echo "    Vulkan packages unavailable; continuing CPU-only."

# --- 2. whisper.cpp + models ------------------------------------------------
W="$ENGINE_DIR/whisper.cpp"
echo "==> Building whisper.cpp (CPU)…"
mkdir -p "$ENGINE_DIR"
[ -d "$W/.git" ] || git clone --depth 1 https://github.com/ggml-org/whisper.cpp "$W"
cmake -S "$W" -B "$W/build-cpu" -DCMAKE_BUILD_TYPE=Release
cmake --build "$W/build-cpu" -j

echo "==> Building whisper.cpp (Vulkan GPU, optional)…"
if cmake -S "$W" -B "$W/build-vulkan" -DCMAKE_BUILD_TYPE=Release -DGGML_VULKAN=1 >/dev/null \
    && cmake --build "$W/build-vulkan" -j >/dev/null; then
    HAVE_VULKAN=1
else
    HAVE_VULKAN=0
    echo "    Vulkan build failed; CPU only."
fi

# Smallest to largest. The big two are 5-bit quantized, so benchmarking one costs
# a ~0.5 GB download instead of ~1.5 GB.
MODELS=(base.en small.en medium.en-q5_0 large-v3-turbo-q5_0)
MB=(141 465 514 547)
fetch() { [ -f "$W/models/ggml-$1.bin" ] || bash "$W/models/download-ggml-model.sh" "$1"; }
ask() {  # ask "<question>": yes only on an explicit y; no terminal means no
    local a=""
    if [ -t 0 ]; then read -r -p "    $1 [y/N] " a || true; fi
    [[ "$a" =~ ^[Yy] ]]
}

# Best-of-3 transcription time (ms, model load excluded) on whisper.cpp's 11s
# sample clip. Whisper pads to 30s windows, so a short dictation costs about the same.
bench() {  # bench <cpu|vulkan> <model>
    local bin="$W/build-$1/bin/whisper-cli" best=999999 t
    "$bin" -m "$W/models/ggml-$2.bin" -f "$W/samples/jfk.wav" -nt -t 8 >/dev/null 2>&1 || true  # warm-up: Vulkan compiles shaders on first run
    for _ in 1 2 3; do
        t=$("$bin" -m "$W/models/ggml-$2.bin" -f "$W/samples/jfk.wav" -nt -t 8 2>&1 >/dev/null \
            | awk '/load time/{l=$(NF-1)} /total time/{t=$(NF-1)} END{if (t) printf "%d", t-l; else print 999999}' || true)
        [ "$t" -lt "$best" ] && best=$t
    done
    echo "$best"
}

# Picked once, then left alone so hand edits survive a reinstall.
CONFIG="$HOME/.config/whisper-dictation/config"
if [ -f "$CONFIG" ]; then
    echo "==> Keeping existing engine config ($CONFIG; delete it and rerun to pick again)"
else
    echo "==> Benchmarking to pick backend + model…"
    fetch base.en
    BACKEND=cpu
    best=$(bench cpu base.en)
    echo "    cpu    base.en : ${best} ms"
    if [ "$HAVE_VULKAN" = 1 ]; then
        # Benchmark rather than trust device detection: a machine with no GPU can
        # still expose a software Vulkan device that is slower than the CPU build.
        t=$(bench vulkan base.en)
        echo "    vulkan base.en : ${t} ms"
        if [ "$t" -lt "$best" ]; then BACKEND=vulkan; best=$t; fi
    fi

    # Climb the ladder until a model gets too slow; anything bigger would be slower
    # still, so it isn't downloaded. Past small.en, ask before each download, for
    # slow connections. The preview re-transcribes back-to-back, so this time is
    # roughly how often it updates; past ~3s it stops feeling live.
    LIMIT=3000
    TIMES=("$best")
    REC=0
    if [ "$best" -lt "$LIMIT" ]; then
        for i in $(seq 1 $((${#MODELS[@]} - 1))); do
            if [ "$i" -ge 2 ] && [ ! -f "$W/models/ggml-${MODELS[$i]}.bin" ] \
                && ! ask "Benchmark ${MODELS[$i]}? It's a ~${MB[$i]} MB download."; then break; fi
            fetch "${MODELS[$i]}"
            t=$(bench "$BACKEND" "${MODELS[$i]}")
            TIMES[$i]=$t
            echo "    $BACKEND ${MODELS[$i]} : ${t} ms"
            if [ "$t" -ge "$LIMIT" ]; then break; fi
            REC=$i
        done
    fi

    echo "==> Pick a model ($BACKEND; time per transcription on this machine):"
    for i in "${!MODELS[@]}"; do
        if [ -n "${TIMES[$i]:-}" ]; then note="${TIMES[$i]} ms"; else note="not tested (~${MB[$i]} MB download)"; fi
        if [ "$i" = "$REC" ]; then note="$note  <- recommended"
        elif [ -n "${TIMES[$i]:-}" ] && [ "${TIMES[$i]}" -ge "$LIMIT" ]; then note="$note  (preview will lag)"; fi
        printf "    %d) %-20s %s\n" $((i + 1)) "${MODELS[$i]}" "$note"
    done
    choice=""
    if [ -t 0 ]; then read -r -p "    Choice [$((REC + 1))]: " choice || true; fi
    if [[ "$choice" =~ ^[0-9]+$ ]] && [ "$choice" -ge 1 ] && [ "$choice" -le "${#MODELS[@]}" ]; then
        MODEL=${MODELS[$((choice - 1))]}
    else
        MODEL=${MODELS[$REC]}
    fi
    fetch "$MODEL"

    mkdir -p "$(dirname "$CONFIG")"
    cat > "$CONFIG" <<EOF
# whisper-dictation engine settings. After editing:
#   systemctl --user restart whisper-server whisper-ptt
# WHISPER_BACKEND: cpu, or vulkan (GPU; only if install.sh built build-vulkan)
# WHISPER_MODEL:   ${MODELS[*]}, ... (models/ggml-<name>.bin;
#                  fetch others with whisper.cpp/models/download-ggml-model.sh <name>)
WHISPER_BACKEND=$BACKEND
WHISPER_MODEL=$MODEL
EOF

    # Offer to reclaim the disk the benchmark (or earlier installs) used.
    unused=()
    for f in "$W"/models/ggml-*.bin; do
        if [ -f "$f" ] && [ "$f" != "$W/models/ggml-$MODEL.bin" ]; then unused+=("$f"); fi
    done
    if [ ${#unused[@]} -gt 0 ]; then
        echo "    Unused models on disk:"
        for f in "${unused[@]}"; do printf "      %s (%s)\n" "$(basename "$f")" "$(du -h "$f" | cut -f1)"; done
        if ask "Delete them? (switching to one later re-downloads it)"; then rm -f "${unused[@]}"; fi
    fi
fi

# Push-to-talk buttons, asked once: a config without them (including one from an
# older install) keeps the defaults until answered here. Reading input devices
# needs root until the new 'input' group takes effect at next login.
if ! grep -q '^PTT_PRIMARY=' "$CONFIG"; then
    echo "==> Push-to-talk buttons"
    p=KEY_SYSRQ s=BTN_EXTRA
    if [ -t 0 ] && picks=$(sudo python3 "$REPO_DIR/ptt-listener.py" --pick-ptt); then
        read -r p s <<< "$picks"
    fi
    cat >> "$CONFIG" <<EOF
# PTT_PRIMARY / PTT_SECONDARY: evdev key or button names (PTT_SECONDARY may be
# none). To pick by pressing again, delete these lines and rerun install.sh.
PTT_PRIMARY=$p
PTT_SECONDARY=$s
EOF
fi
grep -E '^(WHISPER|PTT)_' "$CONFIG" | sed 's/^/    /'

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
systemctl --user enable whisper-server whisper-ptt
systemctl --user restart whisper-server whisper-ptt   # restart, not --now: a reinstall must pick up config changes

echo
echo "==> Done."
echo "    The 'input' group was just granted — LOG OUT AND BACK IN once so the"
echo "    keyboard listener can read your keyboard, then hold Print Screen to dictate."
