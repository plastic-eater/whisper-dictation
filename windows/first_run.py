"""First-run setup for Windows: the counterpart of install.sh's engine section.

Downloads whisper.cpp's prebuilt engine (CPU, plus the CUDA build when an
NVIDIA GPU is present), benchmarks it, climbs the model ladder until a model
is too slow for a live preview, lets the user pick, offers to delete what
isn't used, writes the config, and (re)starts the app.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # source runs: core is at the repo root
from dictation_core import APP_DIR, CONFIG, engine_config  # noqa: E402

ENGINE_TAG = "b5130"      # whisper.cpp release whose prebuilt Windows binaries we use
RELEASE    = f"https://github.com/ggml-org/whisper.cpp/releases/download/{ENGINE_TAG}"
ENGINES    = {"cpu": ("whisper-bin-x64.zip", 8),                 # (asset, MB)
              "cuda": ("whisper-cublas-12.4.0-bin-x64.zip", 643)}  # bundles its CUDA libraries
SAMPLE_URL = f"https://raw.githubusercontent.com/ggml-org/whisper.cpp/{ENGINE_TAG}/samples/jfk.wav"
MODEL_URL  = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-{}.bin"

# Same ladder and limit as install.sh.
MODELS = ["base.en", "small.en", "medium.en-q5_0", "large-v3-turbo-q5_0"]
MB     = [141, 465, 514, 547]
LIMIT  = 3000
THREADS = str(min(8, os.cpu_count() or 4))

ENGINE_DIR = os.path.join(APP_DIR, "engine")
MODEL_DIR  = os.path.join(APP_DIR, "models")
SAMPLE     = os.path.join(APP_DIR, "jfk.wav")
NO_WINDOW  = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _console():
    """The bundled app is a windowed exe; give setup a console to talk in."""
    import ctypes
    if getattr(sys, "frozen", False) and not ctypes.windll.kernel32.GetConsoleWindow():
        ctypes.windll.kernel32.AllocConsole()
        sys.stdin = open("CONIN$", "r")
        sys.stdout = sys.stderr = open("CONOUT$", "w", buffering=1)


def ask(question, default=False):
    """y/n question; Enter takes the default. No console means no, so an
    unattended run never starts a big download, same as install.sh."""
    if not sys.stdin.isatty():
        return False
    hint = "[Y/n]" if default else "[y/N]"
    a = input(f"    {question} {hint} ").strip().lower()
    return a.startswith("y") if a else default


def download(url, dest):
    tmp = dest + ".part"
    with urllib.request.urlopen(url) as r, open(tmp, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done, shown = 0, -1
        while chunk := r.read(1 << 20):
            f.write(chunk)
            done += len(chunk)
            pct = done * 100 // total if total else 0
            if pct // 10 != shown:
                shown = pct // 10
                print(f"\r    {os.path.basename(dest)}: {done >> 20} / {total >> 20} MB", end="", flush=True)
    print()
    os.replace(tmp, dest)


def fetch_engine(name):
    dest = os.path.join(ENGINE_DIR, name)
    if os.path.exists(os.path.join(dest, "whisper-server.exe")):
        return
    asset, _ = ENGINES[name]
    print(f"==> Downloading the {name} engine…")
    zpath = os.path.join(tempfile.gettempdir(), asset)
    download(f"{RELEASE}/{asset}", zpath)
    os.makedirs(dest, exist_ok=True)
    with zipfile.ZipFile(zpath) as z:
        for m in z.infolist():
            if m.is_dir() or not m.filename.startswith("Release/"):
                continue
            with z.open(m) as src, open(os.path.join(dest, os.path.basename(m.filename)), "wb") as out:
                shutil.copyfileobj(src, out)
    os.remove(zpath)


def fetch_model(m):
    path = os.path.join(MODEL_DIR, f"ggml-{m}.bin")
    if not os.path.exists(path):
        os.makedirs(MODEL_DIR, exist_ok=True)
        download(MODEL_URL.format(m), path)


def has_nvidia():
    try:
        r = subprocess.run(["nvidia-smi", "-L"], capture_output=True, text=True, timeout=20,
                           creationflags=NO_WINDOW)
        return r.returncode == 0 and "GPU" in r.stdout
    except (OSError, subprocess.SubprocessError):
        return False


def bench(backend, model):
    """Best-of-3 transcription time (ms, model load excluded) on the 11s sample."""
    cmd = [os.path.join(ENGINE_DIR, backend, "whisper-cli.exe"), "-m",
           os.path.join(MODEL_DIR, f"ggml-{model}.bin"), "-f", SAMPLE, "-nt", "-t", THREADS]

    def once():
        try:
            err = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                                 errors="replace", timeout=600, creationflags=NO_WINDOW).stderr
        except (OSError, subprocess.SubprocessError):
            return 999999
        times = {}
        for line in err.splitlines():
            for key in ("load time", "total time"):
                if key in line and "=" in line:
                    times[key] = float(line.split("=")[1].split()[0])
        if "total time" not in times:
            return 999999
        return int(times["total time"] - times.get("load time", 0))

    once()                                   # warm-up: GPU backends compile kernels on first run
    return min(once() for _ in range(3))


CONFIG_KEYS = ("WHISPER_BACKEND", "WHISPER_MODEL", "PTT_PRIMARY", "PTT_SECONDARY")


def write_config(**updates):
    """Set the given keys, keeping the others already in the config."""
    cfg = engine_config() if os.path.exists(CONFIG) else {}
    cfg.update(updates)
    with open(CONFIG, "w") as f:
        f.write("# whisper-dictation settings. After editing, rerun \"Whisper Dictation\n"
                "# Setup\" (or quit and restart Whisper Dictation).\n"
                "# WHISPER_BACKEND: cpu, or cuda (NVIDIA; only if setup downloaded it)\n"
                f"# WHISPER_MODEL:   {' '.join(MODELS)}\n"
                "# PTT_PRIMARY / PTT_SECONDARY: vk:0x2D style key codes, mouse:x1, mouse:x2,\n"
                "#                  mouse:middle, or none (secondary only)\n")
        f.writelines(f"{k}={cfg[k]}\n" for k in CONFIG_KEYS if k in cfg)


def pick():
    print("==> Benchmarking to pick backend + model…")
    fetch_engine("cpu")
    if not os.path.exists(SAMPLE):
        download(SAMPLE_URL, SAMPLE)
    fetch_model("base.en")
    backend, best = "cpu", bench("cpu", "base.en")
    print(f"    cpu  base.en : {best} ms")
    mb = ENGINES["cuda"][1]
    if has_nvidia() and ask(f"NVIDIA GPU found. Download the GPU engine? It's a ~{mb} MB download.", True):
        fetch_engine("cuda")
        t = bench("cuda", "base.en")
        print(f"    cuda base.en : {t} ms")
        if t < best:
            backend, best = "cuda", t

    # Climb until a model is too slow; past small.en, ask before each download.
    times, rec = [best], 0
    if best < LIMIT:
        for i in range(1, len(MODELS)):
            m = MODELS[i]
            if i >= 2 and not os.path.exists(os.path.join(MODEL_DIR, f"ggml-{m}.bin")) \
                    and not ask(f"Benchmark {m}? It's a ~{MB[i]} MB download."):
                break
            fetch_model(m)
            t = bench(backend, m)
            times.append(t)
            print(f"    {backend} {m} : {t} ms")
            if t >= LIMIT:
                break
            rec = i

    print(f"==> Pick a model ({backend}; time per transcription on this machine):")
    for i, m in enumerate(MODELS):
        note = f"{times[i]} ms" if i < len(times) else f"not tested (~{MB[i]} MB download)"
        if i == rec:
            note += "  <- recommended"
        elif i < len(times) and times[i] >= LIMIT:
            note += "  (preview will lag)"
        print(f"    {i + 1}) {m:20} {note}")
    choice = input(f"    Choice [{rec + 1}]: ").strip() if sys.stdin.isatty() else ""
    model = MODELS[int(choice) - 1] if choice.isdigit() and 1 <= int(choice) <= len(MODELS) else MODELS[rec]
    fetch_model(model)
    write_config(WHISPER_BACKEND=backend, WHISPER_MODEL=model)

    # Offer to reclaim the disk the benchmark (or earlier setups) used.
    unused = [os.path.join(MODEL_DIR, f) for f in os.listdir(MODEL_DIR)
              if f.startswith("ggml-") and f.endswith(".bin") and f != f"ggml-{model}.bin"]
    unused += [os.path.join(ENGINE_DIR, e) for e in ENGINES
               if e != backend and e != "cpu" and os.path.isdir(os.path.join(ENGINE_DIR, e))]
    if unused:
        print("    Unused files on disk:")
        for p in unused:
            size = sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(p) for f in fs) \
                if os.path.isdir(p) else os.path.getsize(p)
            print(f"      {os.path.basename(p)} ({size >> 20} MB)")
        if ask("Delete them? (switching to one later re-downloads it)"):
            for p in unused:
                shutil.rmtree(p) if os.path.isdir(p) else os.remove(p)


def _seed_tuning():
    """Drop the example tuning file where the app looks for tuning_local.py."""
    dest = os.path.join(APP_DIR, "tuning_local.py")
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    src = os.path.join(base, "tuning_local.example.py")
    if not os.path.exists(dest) and os.path.exists(src):
        shutil.copy(src, dest)


# --- push-to-talk buttons ---------------------------------------------------
# Keys refused as triggers, since the app swallows its key and typing would
# break: Backspace, Tab, Enter, Esc, Shift/Ctrl/Alt (right Ctrl and right Alt are
# allowed), Caps Lock, Space, arrows, Windows keys, letters, digits, numpad,
# punctuation.
_REFUSED = ({0x08, 0x09, 0x0D, 0x1B, 0x10, 0x11, 0x12, 0x14, 0x20, 0x25, 0x26, 0x27, 0x28, 0x5B, 0x5C,
             0xA0, 0xA1, 0xA2, 0xA4, 0xE2}
            | set(range(0x30, 0x5B)) | set(range(0x60, 0x70))
            | set(range(0xBA, 0xC1)) | set(range(0xDB, 0xE0)))
_MOUSE_LABELS = {"x1": "mouse back button", "x2": "mouse forward/side button",
                 "middle": "middle mouse button"}


def ptt_label(p):
    if p == "none":
        return "none"
    if p.startswith("mouse:"):
        return _MOUSE_LABELS.get(p[6:], p)
    from pynput.keyboard import Key
    vk = int(p[3:], 16)
    if 0x30 <= vk <= 0x5A:
        return f"'{chr(vk)}'"
    for k in Key:
        if getattr(k.value, "vk", None) == vk:
            return k.name.replace("_", " ").title()
    return f"key {p[3:]}"


def pick_ptt():
    """Ask for the primary and secondary trigger by having the user press them.
    While picking, every keypress is swallowed so none reach the console."""
    import queue
    from pynput import keyboard, mouse
    presses = queue.Queue()

    def kb_filter(msg, data):
        if msg in (0x100, 0x104) and not data.flags & 0x10:      # key down, not injected
            presses.put(f"vk:{data.vkCode:#04x}")
        kl.suppress_event()

    def mouse_filter(msg, data):
        if msg in (0x207, 0x20B):                                 # middle / X button down
            presses.put("mouse:middle" if msg == 0x207 else f"mouse:x{data.mouseData >> 16}")
        if msg in (0x207, 0x208, 0x20B, 0x20C):
            ml.suppress_event()
        return True

    kl = keyboard.Listener(win32_event_filter=kb_filter)
    ml = mouse.Listener(win32_event_filter=mouse_filter)
    kl.start()
    ml.start()

    def capture(prompt, default, allow_none):
        print(f"    {prompt}", flush=True)
        while True:
            p = presses.get()
            if p == "vk:0x0d":
                return default
            if p == "vk:0x1b" and allow_none:
                return "none"
            if p.startswith("mouse:") or int(p[3:], 16) not in _REFUSED:
                return p
            print(f"    {ptt_label(p)} is a typing key; pick another.", flush=True)

    try:
        primary = capture("Press the key or mouse button to hold for dictation "
                          "(Enter keeps Insert).", "vk:0x2d", False)
        print(f"      primary: {ptt_label(primary)}")
        secondary = capture("Press a second one, Enter for the mouse forward/side button, "
                            "or Esc for none.", "mouse:x2", True)
        if secondary == primary:
            secondary = "none"
        print(f"      secondary: {ptt_label(secondary)}")
    finally:
        kl.stop()
        ml.stop()
    write_config(PTT_PRIMARY=primary, PTT_SECONDARY=secondary)


def _stop_app():
    """Stop a running copy: it holds the old config, and its key hook would
    swallow presses meant for the picker."""
    if getattr(sys, "frozen", False):
        exe = os.path.basename(sys.executable)
        subprocess.run(["taskkill", "/F", "/IM", exe, "/FI", f"PID ne {os.getpid()}"],
                       capture_output=True, creationflags=NO_WINDOW)


def _start_app():
    if getattr(sys, "frozen", False):
        subprocess.Popen([sys.executable], creationflags=subprocess.DETACHED_PROCESS)
    else:
        print("    (source run: start the app with  python windows\\dictation.py)")


def run_setup():
    _console()
    print("==> Whisper Dictation setup")
    _stop_app()
    os.makedirs(APP_DIR, exist_ok=True)
    _seed_tuning()
    if not os.path.exists(CONFIG) or ask("Already set up. Benchmark and pick a model again?"):
        pick()
    cfg = engine_config()
    if "PTT_PRIMARY" not in cfg or ask(
            f"Push-to-talk is {ptt_label(cfg['PTT_PRIMARY'])} + {ptt_label(cfg.get('PTT_SECONDARY', 'none'))}. "
            "Change it?"):
        print("==> Push-to-talk buttons")
        if sys.stdin.isatty():
            pick_ptt()
        else:
            write_config(PTT_PRIMARY="vk:0x2d", PTT_SECONDARY="mouse:x2")
    cfg = engine_config()
    for k in CONFIG_KEYS:
        print(f"    {k}={cfg.get(k, '')}")
    print(f"    Settings and tuning_local.py live in {APP_DIR}")
    print(f"    Hold {ptt_label(cfg['PTT_PRIMARY'])} to dictate"
          + ("." if cfg.get("PTT_SECONDARY", "none") == "none" else f" (or the {ptt_label(cfg['PTT_SECONDARY'])})."))
    _start_app()
    if sys.stdin.isatty():
        input("    Press Enter to close.")
