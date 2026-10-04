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
from dictation_core import APP_DIR, CONFIG  # noqa: E402

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


def write_config(backend, model):
    with open(CONFIG, "w") as f:
        f.write("# whisper-dictation engine settings. After editing, quit and restart\n"
                "# Whisper Dictation, or rerun \"Whisper Dictation Setup\".\n"
                "# WHISPER_BACKEND: cpu, or cuda (NVIDIA; only if setup downloaded it)\n"
                f"# WHISPER_MODEL:   {' '.join(MODELS)}\n"
                f"WHISPER_BACKEND={backend}\nWHISPER_MODEL={model}\n")


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
    write_config(backend, model)

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


def _restart_app():
    """Stop any running copy (it holds the old config) and start a fresh one."""
    if getattr(sys, "frozen", False):
        exe = os.path.basename(sys.executable)
        subprocess.run(["taskkill", "/F", "/IM", exe, "/FI", f"PID ne {os.getpid()}"],
                       capture_output=True, creationflags=NO_WINDOW)
        subprocess.Popen([sys.executable], creationflags=subprocess.DETACHED_PROCESS)
    else:
        print("    (source run: start the app with  python windows\\dictation.py)")


def run_setup():
    _console()
    print("==> Whisper Dictation setup")
    os.makedirs(APP_DIR, exist_ok=True)
    _seed_tuning()
    if not os.path.exists(CONFIG) or ask("Already set up. Benchmark and pick a model again?"):
        pick()
    with open(CONFIG) as f:
        print("".join(f"    {line}" for line in f if line.startswith("WHISPER_")), end="")
    print(f"    Settings and tuning_local.py live in {APP_DIR}")
    print("    Hold Insert (or the mouse side button) to dictate.")
    _restart_app()
    if sys.stdin.isatty():
        input("    Press Enter to close.")
