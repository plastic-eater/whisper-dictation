"""Push-to-talk dictation for Windows: the counterpart of ptt-listener.py.

Hold the push-to-talk key (Insert by default) or mouse button (forward/side by
default; both chosen in setup) to record from the default mic; release to
transcribe and type the text into the focused window. The key is swallowed so
it never does its normal job (Insert never toggles overwrite mode); the mouse
button is read passively, as on Linux, so it still does "forward" too.

The app starts its own whisper-server (there's no systemd here) inside a job
object, so the server dies with the app. While a key is held, a background
thread transcribes snapshots of the audio for a focus-safe preview overlay.

Run with --setup for first-run setup (engine download, benchmark, model pick).
"""
import ctypes
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import wave
from ctypes import wintypes

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # source runs: core is at the repo root
from dictation_core import (  # noqa: E402
    APP_DIR, CONFIG, SERVER_URL, clean, engine_config, http_transcribe, postprocess, PROMPT,
    spoken_deletes,
)

TAP_SEC          = 0.25    # a mouse-button press shorter than this is a tap, not speech
DOUBLE_TAP_SEC   = 0.40    # two taps within this window send Enter (mouse-only convenience)
PREVIEW_STEP     = 0.15    # seconds between preview passes (near back-to-back)
PREVIEW_TAIL_SEC = 30      # preview transcribes the last N seconds (whisper's own window)
RATE             = 16000
THREADS          = str(min(8, os.cpu_count() or 4))
LLKHF_INJECTED   = 0x10
WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP = 0x100, 0x101, 0x104, 0x105

LOG  = os.path.join(APP_DIR, "dictation.log")
WAV  = os.path.join(tempfile.gettempdir(), "whisper-ptt.wav")
SNAP = os.path.join(tempfile.gettempdir(), "whisper-ptt.snap.wav")

_CFG   = engine_config()
ENGINE = os.path.join(APP_DIR, "engine", _CFG["WHISPER_BACKEND"])
MODEL  = os.path.join(APP_DIR, "models", f"ggml-{_CFG['WHISPER_MODEL']}.bin")

# Push-to-talk triggers from the config: "vk:0x2D" is a key by virtual-key code,
# "mouse:x2" a pynput mouse button name, "none" turns the secondary off.
PTT       = [_CFG.get("PTT_PRIMARY", "vk:0x2d"), _CFG.get("PTT_SECONDARY", "mouse:x2")]
PTT_VKS   = {int(p[3:], 16) for p in PTT if p.startswith("vk:")}
PTT_MOUSE = {p[6:] for p in PTT if p.startswith("mouse:")}

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_u32 = ctypes.WinDLL("user32", use_last_error=True)


# --- whisper-server, tied to our lifetime -----------------------------------
class _JobLimits(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD)]


class _JobLimitsEx(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _JobLimits), ("IoInfo", ctypes.c_uint64 * 6),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


def _kill_on_close_job():
    """A job object that kills its processes when our handle closes, i.e. when
    this app exits or is killed, so no orphaned server keeps the port."""
    _k32.CreateJobObjectW.restype = wintypes.HANDLE
    job = _k32.CreateJobObjectW(None, None)
    info = _JobLimitsEx()
    info.BasicLimitInformation.LimitFlags = 0x2000          # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    _k32.SetInformationJobObject(wintypes.HANDLE(job), 9, ctypes.byref(info), ctypes.sizeof(info))
    return job


_job = None
_server = None
_server_lock = threading.Lock()


def start_server():
    global _job, _server
    if _job is None:
        _job = _kill_on_close_job()
    _server = subprocess.Popen(
        [os.path.join(ENGINE, "whisper-server.exe"), "-m", MODEL, "--host", "127.0.0.1",
         "--port", "8910", "-t", THREADS, "-nt"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    _k32.AssignProcessToJobObject(wintypes.HANDLE(_job), wintypes.HANDLE(int(_server._handle)))


def cli_transcribe(path):
    try:
        out = subprocess.run(
            [os.path.join(ENGINE, "whisper-cli.exe"), "-m", MODEL, "-f", path, "-nt", "-np",
             "-t", THREADS, "--prompt", PROMPT],
            capture_output=True, text=True, encoding="utf-8", timeout=120,
            creationflags=subprocess.CREATE_NO_WINDOW,
        ).stdout
    except Exception:
        return ""
    return clean(out)


def transcribe(path, timeout=30):
    t = http_transcribe(path, SERVER_URL, timeout)
    if t is None:                       # server down -> local fallback, and bring it back
        with _server_lock:
            if _server is not None and _server.poll() is not None:
                start_server()
        t = cli_transcribe(path)
    return t


# --- audio ------------------------------------------------------------------
class Recorder:
    """16 kHz mono int16 capture into memory; written out as WAV on demand."""

    def __init__(self):
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._stream = None

    def start(self):
        import sounddevice as sd
        with self._lock:
            self._buf = bytearray()
        self._stream = sd.RawInputStream(samplerate=RATE, channels=1, dtype="int16",
                                         callback=self._on_audio)
        self._stream.start()

    def _on_audio(self, data, frames, t, status):
        with self._lock:
            self._buf.extend(data)

    def stop(self):
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    def write(self, path, tail_sec=None):
        with self._lock:
            pcm = bytes(self._buf)
        if tail_sec is not None:
            pcm = pcm[-tail_sec * RATE * 2:]
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(pcm)
        return len(pcm)


# --- overlay ----------------------------------------------------------------
class Overlay:
    """Topmost, click-through, never-focused preview box (tkinter + Win32 styles).
    Lives on the main thread; other threads talk to it through a queue. Hidden by
    alpha 0 rather than unmapping, since re-showing a Tk window can activate it."""

    ALPHA     = 0.80     # whole-window opacity; Tk can't fade the background alone
    MAX_CHARS = 360

    def __init__(self):
        import tkinter as tk
        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.0)
        self.root.configure(bg="black")
        sw = self.root.winfo_screenwidth()
        self.width = min(1200, sw - 80)
        self.x = (sw - self.width) // 2
        self.label = tk.Label(self.root, fg="white", bg="black", font=("Segoe UI", 18, "bold"),
                              wraplength=self.width - 40, justify="left", anchor="nw",
                              padx=20, pady=20)
        self.label.pack(fill="both", expand=True)
        self.root.update_idletasks()
        _u32.GetParent.restype = wintypes.HWND
        hwnd = _u32.GetParent(self.root.winfo_id())
        GWL_EXSTYLE = -20
        style = _u32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        # NOACTIVATE: never takes focus. TOOLWINDOW: no taskbar button. TRANSPARENT: clicks pass through.
        _u32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | 0x08000000 | 0x80 | 0x20)
        self.q = queue.Queue()
        self.root.after(30, self._poll)

    def show(self, text):
        self.q.put(text)

    def hide(self):
        self.q.put(None)

    def _poll(self):
        try:
            while True:
                text = self.q.get_nowait()
                if text is None:
                    self.root.attributes("-alpha", 0.0)
                    continue
                if not text:
                    text = "listening…"
                elif len(text) > self.MAX_CHARS:
                    text = "…" + text[-self.MAX_CHARS:]
                self.label.configure(text="🎙  " + text)
                self.root.update_idletasks()
                self.root.geometry(f"{self.width}x{self.label.winfo_reqheight()}+{self.x}+56")
                self.root.attributes("-alpha", self.ALPHA)
        except queue.Empty:
            pass
        self.root.after(30, self._poll)


# --- typing -----------------------------------------------------------------
def _kb():
    from pynput.keyboard import Controller
    return Controller()


def type_text(text):
    _kb().type(text)


def send_key(name, n=1):
    from pynput.keyboard import Key
    kb = _kb()
    for _ in range(n):
        kb.press(getattr(Key, name))
        kb.release(getattr(Key, name))


# --- push-to-talk -----------------------------------------------------------
class Dictation:
    def __init__(self, overlay):
        self.overlay = overlay
        self.rec = Recorder()
        self.recording = False
        self.preview_stop = threading.Event()
        self.preview_thread = None
        self.events = queue.Queue()       # (kind, source, time) from the input hooks
        self.press_at = {}                # source -> press time
        self.last_tap_at = 0.0            # last quick mouse tap, for double-tap Enter

    def start_recording(self):
        if self.recording:
            return
        for f in (WAV, SNAP):
            try:
                os.remove(f)
            except OSError:
                pass
        self.rec.start()
        self.recording = True
        self.preview_stop.clear()
        self.preview_thread = threading.Thread(target=self.preview_loop, daemon=True)
        self.preview_thread.start()

    def _stop_recording(self):
        if not self.recording:
            return False
        self.preview_stop.set()
        self.rec.stop()
        self.recording = False
        if self.preview_thread:
            self.preview_thread.join(timeout=2)
            self.preview_thread = None
        return True

    def abort_recording(self):
        """A quick mouse tap is the Enter gesture, not speech: discard it."""
        if self._stop_recording():
            self.overlay.hide()

    def preview_loop(self):
        # Hold off until the press outlasts a tap, so the double-tap Enter gesture
        # never flashes the overlay.
        if self.preview_stop.wait(TAP_SEC):
            return
        self.overlay.show("")
        while not self.preview_stop.is_set():
            try:
                if self.rec.write(SNAP, PREVIEW_TAIL_SEC):
                    text = postprocess(transcribe(SNAP, timeout=8))
                    if not self.preview_stop.is_set() and text:
                        self.overlay.show(text)
            except Exception:
                pass
            self.preview_stop.wait(PREVIEW_STEP)

    def stop_and_type(self):
        if not self._stop_recording():
            return
        self.overlay.show("⏳ transcribing…")
        self.rec.write(WAV)
        raw = transcribe(WAV, timeout=60)
        text = postprocess(raw)
        if text != raw:                 # to the log, to see which fixes still fire
            print(f"raw:   {raw}\ntyped: {text}", flush=True)
        self.overlay.hide()
        if not text:
            return
        n = spoken_deletes(text)
        if n:
            send_key("backspace", n)
            return
        type_text(text + " ")

    def run(self):
        """Handle hook events off the hook threads: low-level hooks must return
        fast or Windows silently removes them."""
        while True:
            kind, src, t = self.events.get()
            try:
                self.handle(kind, src, t)
            except Exception:
                import traceback
                traceback.print_exc()

    def handle(self, kind, src, t):
        if kind == "down":
            if src in self.press_at:          # key autorepeat while held
                return
            self.press_at[src] = t
            self.start_recording()
            return
        held = t - self.press_at.pop(src, t)
        if src.startswith("mouse:") and held < TAP_SEC:
            self.abort_recording()
            if t - self.last_tap_at < DOUBLE_TAP_SEC:
                send_key("enter")
                self.last_tap_at = 0.0        # consume; a 3rd tap starts fresh
            else:
                self.last_tap_at = t
        else:
            self.stop_and_type()


def start_hooks(d):
    from pynput import keyboard, mouse
    kb_listener = None

    def kb_filter(msg, data):
        if data.vkCode not in PTT_VKS or data.flags & LLKHF_INJECTED:
            return True
        src = f"vk:{data.vkCode:#04x}"
        if msg in (WM_KEYDOWN, WM_SYSKEYDOWN):
            d.events.put(("down", src, time.time()))
        elif msg in (WM_KEYUP, WM_SYSKEYUP):
            d.events.put(("up", src, time.time()))
        kb_listener.suppress_event()          # swallow the key so it never does its normal job

    def on_click(x, y, button, pressed):
        if button.name in PTT_MOUSE:
            d.events.put(("down" if pressed else "up", f"mouse:{button.name}", time.time()))

    kb_listener = keyboard.Listener(win32_event_filter=kb_filter)
    kb_listener.start()
    mouse.Listener(on_click=on_click).start()


# --- app --------------------------------------------------------------------
def _single_instance():
    """Hold a named mutex for our lifetime; False if another copy holds it."""
    _k32.CreateMutexW.restype = wintypes.HANDLE
    _k32.CreateMutexW(None, False, "Local\\whisper-dictation")
    return ctypes.get_last_error() != 183           # ERROR_ALREADY_EXISTS


def main():
    os.makedirs(APP_DIR, exist_ok=True)
    try:
        if os.path.getsize(LOG) > 1_000_000:
            os.remove(LOG)
    except OSError:
        pass
    # A windowed exe has no stdout; send prints and tracebacks to the log.
    sys.stdout = sys.stderr = open(LOG, "a", buffering=1, encoding="utf-8")
    if not _single_instance():
        return
    if not os.path.exists(CONFIG):
        # Never set up (or setup was cancelled): run it in a console, then stop here.
        args = [sys.executable, "--setup"] if getattr(sys, "frozen", False) else \
               [sys.executable, os.path.abspath(__file__), "--setup"]
        subprocess.Popen(args, creationflags=subprocess.CREATE_NEW_CONSOLE)
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)   # crisp overlay text on scaled displays
    except Exception:
        pass
    print(f"start: backend={_CFG['WHISPER_BACKEND']} model={_CFG['WHISPER_MODEL']}", flush=True)
    start_server()
    overlay = Overlay()
    d = Dictation(overlay)
    threading.Thread(target=d.run, daemon=True).start()
    start_hooks(d)
    overlay.root.mainloop()


if __name__ == "__main__":
    if "--selftest" in sys.argv:      # CI: prove the bundled exe can load everything it needs
        import pynput.keyboard, pynput.mouse, sounddevice, tkinter, first_run  # noqa: E401,F401
        sys.exit(0)
    if "--setup" in sys.argv:
        from first_run import run_setup
        run_setup()
    else:
        main()
