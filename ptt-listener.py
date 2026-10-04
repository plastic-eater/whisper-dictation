#!/usr/bin/env python3
"""Push-to-talk dictation with a live on-screen preview.

Hold Print Screen (or the Bluetooth mouse's forward/side button) to record from the
default mic; release to transcribe the clip and type the text into the focused window
via ydotool. The mouse button is read passively (no device grab), so it still performs
its normal "forward" navigation too — harmless, since that's a no-op on most pages.

Transcription goes to a warm whisper-server (model kept resident in RAM), so each
call is fast with no per-call model load. While held, a background thread sends
snapshots of the audio-so-far for a live preview in a focus-safe overlay; that
preview is throwaway — only the final transcription on release is typed. If the
server is unreachable, it falls back to spawning whisper-cli locally.

Reads keyboard events directly (needs the 'input' group) because Wayland/GNOME
hotkeys only fire on key-press, never release.
"""
import os
import time
import signal
import threading
import subprocess
import selectors

import evdev
from evdev import ecodes

from dictation_core import (
    SERVER_URL, clean, engine_config, http_transcribe, postprocess, PROMPT, spoken_deletes,
)

PREVIEW_SERVER_URL = SERVER_URL                          # one model: typed text and live preview


def _engine_dir():
    """Where whisper.cpp is built. Override with $WHISPER_DICT_HOME; otherwise
    autodetect the install layout (~/.local/share) or a manual ~/Documents/Code build."""
    env = os.environ.get("WHISPER_DICT_HOME")
    if env:
        return os.path.expanduser(env)
    for c in ("~/.local/share/whisper-dictation", "~/Documents/Code"):
        p = os.path.expanduser(c)
        if os.path.exists(f"{p}/whisper.cpp/models"):
            return p
    return os.path.expanduser("~/.local/share/whisper-dictation")


ENGINE_DIR = _engine_dir()
_CFG       = engine_config()
WHISPER    = f"{ENGINE_DIR}/whisper.cpp/build-{_CFG['WHISPER_BACKEND']}/bin/whisper-cli"  # fallback engine
MODEL      = f"{ENGINE_DIR}/whisper.cpp/models/ggml-{_CFG['WHISPER_MODEL']}.bin"          # fallback model
THREADS    = "12"                                                          # fallback
YDOTOOL    = "/usr/local/bin/ydotool"
OVERLAY    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "preview-overlay.py")
RUNTIME    = os.environ.get("XDG_RUNTIME_DIR", "/tmp")
WAV        = f"{RUNTIME}/whisper-ptt.wav"
SNAP       = f"{RUNTIME}/whisper-ptt.snap.wav"
# Push-to-talk triggers: evdev names from the config install.sh writes (pick them
# with --pick-ptt). Defaults are Print Screen and the mouse forward/side button;
# a SECONDARY of "none" turns it off. Mouse buttons (BTN_*) also get the tap and
# double-tap-Enter gestures; keyboard keys don't.
PTT_NAMES  = [_CFG.get("PTT_PRIMARY", "KEY_SYSRQ"), _CFG.get("PTT_SECONDARY", "BTN_EXTRA")]
PTT_CODES  = tuple(ecodes.ecodes[n] for n in PTT_NAMES if n in ecodes.ecodes)
PTT_MOUSE  = {ecodes.ecodes[n] for n in PTT_NAMES if n.startswith("BTN_") and n in ecodes.ecodes}
TAP_SEC        = 0.25              # a mouse-button press shorter than this is a tap, not speech
DOUBLE_TAP_SEC = 0.40             # two taps within this window send Enter (mouse-only convenience)
PREVIEW_STEP     = 0.15             # seconds between preview passes (near back-to-back)
PREVIEW_TAIL_SEC = 30               # preview transcribes the last N seconds (≈ whisper's own
                                    # window): accumulates for normal holds, caps runaway cost

os.environ.setdefault("YDOTOOL_SOCKET", "/run/ydotool/socket")

_rec = None
_overlay = None
_preview_thread = None
_preview_stop = threading.Event()


# --- overlay (focus-safe on-screen preview) ---------------------------------
def overlay_start():
    global _overlay
    try:
        _overlay = subprocess.Popen(["python3", OVERLAY], stdin=subprocess.PIPE, text=True)
    except Exception:
        _overlay = None


def overlay_write(text):
    if _overlay and _overlay.stdin:
        try:
            _overlay.stdin.write(text + "\n")
            _overlay.stdin.flush()
        except Exception:
            pass


def overlay_stop():
    global _overlay
    if _overlay:
        try:
            if _overlay.stdin:
                _overlay.stdin.close()      # EOF -> overlay exits
        except Exception:
            pass
        try:
            _overlay.wait(timeout=1)
        except Exception:
            try:
                _overlay.terminate()
            except Exception:
                pass
        _overlay = None


# --- transcription ----------------------------------------------------------
def cli_transcribe(path):
    try:
        out = subprocess.run(
            [WHISPER, "-m", MODEL, "-f", path, "-nt", "-np", "-t", THREADS, "--prompt", PROMPT],
            capture_output=True, text=True, timeout=120,
        ).stdout
    except Exception:
        return ""
    return clean(out)


def transcribe(path, url, timeout=30):
    t = http_transcribe(path, url, timeout)
    if t is None:                       # server down -> local fallback
        t = cli_transcribe(path)
    return t


def snapshot_transcribe():
    """Transcribe the recent tail of the audio so far (so long holds don't make
    each preview pass progressively slower) by reading + size-patching a copy."""
    try:
        data = bytearray(open(WAV, "rb").read())
    except OSError:
        return ""
    if len(data) <= 44:
        return ""
    max_audio = PREVIEW_TAIL_SEC * 16000 * 2              # 16 kHz, 16-bit, mono
    if len(data) - 44 > max_audio:
        data = data[:44] + data[len(data) - max_audio:]   # header + last N seconds
    data[4:8]   = (len(data) - 8).to_bytes(4, "little")   # RIFF chunk size
    data[40:44] = (len(data) - 44).to_bytes(4, "little")  # data chunk size
    try:
        with open(SNAP, "wb") as f:
            f.write(data)
    except OSError:
        return ""
    return transcribe(SNAP, PREVIEW_SERVER_URL, timeout=8)


def preview_loop():
    # Hold off on the overlay until the press outlasts a tap, so the double-tap Enter
    # gesture (quick taps) never flashes it; only genuine holds show the live preview.
    if _preview_stop.wait(TAP_SEC):
        return
    overlay_start()
    while not _preview_stop.is_set():
        try:
            text = postprocess(snapshot_transcribe())
            if not _preview_stop.is_set() and text:
                overlay_write(text)
        except Exception:
            pass
        _preview_stop.wait(PREVIEW_STEP)


# --- push-to-talk -----------------------------------------------------------
def start_recording():
    global _rec, _preview_thread
    if _rec is not None:
        return
    for f in (WAV, SNAP):               # clear stale audio so the preview can't
        try:                            # transcribe the previous recording
            os.remove(f)
        except OSError:
            pass
    _rec = subprocess.Popen(
        ["pw-record", "--rate", "16000", "--channels", "1", "--format", "s16", WAV]
    )
    _preview_stop.clear()               # preview_loop spawns the overlay once past TAP_SEC
    _preview_thread = threading.Thread(target=preview_loop, daemon=True)
    _preview_thread.start()


def _stop_recording():
    """Tear down pw-record and the preview thread. Returns True if one was running."""
    global _rec, _preview_thread
    if _rec is None:
        return False
    _preview_stop.set()
    _rec.send_signal(signal.SIGINT)          # let pw-record flush the WAV header
    try:
        _rec.wait(timeout=2)
    except Exception:
        _rec.kill()
    _rec = None
    if _preview_thread:
        _preview_thread.join(timeout=2)
        _preview_thread = None
    return True


def abort_recording():
    """Discard an in-progress recording without transcribing — used for a quick tap,
    which is the Enter gesture, not speech."""
    if _stop_recording():
        overlay_stop()                       # no-op if the overlay never appeared


def send_keys(codes):
    """Press+release each keycode in turn via ydotool."""
    args = [YDOTOOL, "key"]
    for c in codes:
        args += [f"{c}:1", f"{c}:0"]
    subprocess.run(args)


def send_enter():
    send_keys([ecodes.KEY_ENTER])


def stop_and_type():
    if not _stop_recording():
        return
    overlay_write("⏳ transcribing…")
    raw = transcribe(WAV, SERVER_URL, timeout=60)
    text = postprocess(raw)
    if text != raw:                     # to the journal, to see which fixes still fire
        print(f"raw:   {raw}\ntyped: {text}", flush=True)
    overlay_stop()
    if not text:
        return
    n = spoken_deletes(text)
    if n:
        send_keys([ecodes.KEY_BACKSPACE] * n)
        return
    subprocess.run([YDOTOOL, "type", "--key-delay", "4", "--key-hold", "2", "--", text + " "])


def find_devices():
    """Devices that report a PTT trigger (keyboard and/or mouse). The ydotool
    virtual device is skipped so injected events can't trigger a recording."""
    devs = []
    for path in evdev.list_devices():
        try:
            d = evdev.InputDevice(path)
        except Exception:
            continue
        keys = d.capabilities().get(ecodes.EV_KEY, [])
        if any(c in keys for c in PTT_CODES) and "ydotool" not in d.name.lower():
            devs.append(d)
    return devs


def main():
    sel = selectors.DefaultSelector()
    registered = {}                      # device path -> InputDevice

    def rescan():
        # The keyboard is always present; the Bluetooth mouse's device node appears
        # only once it connects, which can be after this process started — so we keep
        # rescanning to pick it up (and its forward/side PTT button) when it shows up.
        for d in find_devices():
            if d.path not in registered:
                try:
                    sel.register(d, selectors.EVENT_READ)
                    registered[d.path] = d
                except Exception:
                    pass

    def drop(d):                         # device vanished (evsieve stopped, mouse unplugged)
        try:
            sel.unregister(d)
        except Exception:
            pass
        registered.pop(d.path, None)
        try:
            d.close()
        except Exception:
            pass

    press_at = {}                        # PTT code -> press timestamp
    last_tap_at = 0.0                    # last quick mouse-button tap, for double-tap detection
    rescan()
    while True:
        events = sel.select(timeout=4)
        if not events:
            rescan()                     # idle: pick up hotplugged devices
            continue
        for key, _ in events:
            d = key.fileobj
            try:
                for ev in d.read():
                    if ev.type != ecodes.EV_KEY or ev.code not in PTT_CODES:
                        continue
                    if ev.value == 1:                          # key down
                        press_at[ev.code] = time.time()
                        start_recording()
                    elif ev.value == 0:                        # key up
                        held = time.time() - press_at.pop(ev.code, time.time())
                        if ev.code in PTT_MOUSE and held < TAP_SEC:
                            abort_recording()                  # a tap isn't speech
                            now = time.time()
                            if now - last_tap_at < DOUBLE_TAP_SEC:
                                send_enter()                   # second quick tap -> Enter
                                last_tap_at = 0.0              # consume; a 3rd tap starts fresh
                            else:
                                last_tap_at = now
                        else:
                            stop_and_type()
                    # value == 2 (autorepeat while held) ignored
            except OSError:
                drop(d)


# --- choosing the buttons (install.sh) --------------------------------------
# Typing keys (Esc through Caps Lock: letters, digits, punctuation, Enter, Space,
# Shift, Ctrl, Alt), arrows, Super and the numpad are refused: the listener can't
# block a key, so it would fire on ordinary typing.
_REFUSED = set(range(ecodes.KEY_ESC, ecodes.KEY_CAPSLOCK + 1)) | set(range(ecodes.KEY_KP7, ecodes.KEY_KPDOT + 1)) | {
    ecodes.KEY_UP, ecodes.KEY_DOWN, ecodes.KEY_LEFT, ecodes.KEY_RIGHT, ecodes.KEY_LEFTMETA,
    ecodes.KEY_RIGHTMETA, ecodes.KEY_KPENTER, ecodes.KEY_KPSLASH, ecodes.KEY_KPASTERISK,
}
# Mouse buttons that make sense to hold. Everything else in the button range
# (left/right click, touchpad touches and tool events) is ignored, so brushing
# the touchpad doesn't count as a pick.
_MOUSE_OK = {ecodes.BTN_MIDDLE, ecodes.BTN_SIDE, ecodes.BTN_EXTRA, ecodes.BTN_FORWARD,
             ecodes.BTN_BACK, ecodes.BTN_TASK}


def _code_name(code):
    n = ecodes.KEY.get(code) or ecodes.BTN.get(code)
    if isinstance(n, list):
        n = next((x for x in n if "MIN_INTERESTING" not in x), n[0])
    return n


def pick_ptt():
    """Ask for the primary and secondary trigger by having the user press them.
    Prompts go to stderr; prints "PRIMARY SECONDARY" (evdev names) to stdout."""
    import select
    import sys
    import termios
    devs = []
    for path in evdev.list_devices():
        try:
            d = evdev.InputDevice(path)
        except Exception:
            continue
        if ecodes.EV_KEY in d.capabilities() and "ydotool" not in d.name.lower():
            devs.append(d)
    tty = sys.stdin.fileno()
    saved = termios.tcgetattr(tty)
    quiet = termios.tcgetattr(tty)
    quiet[3] &= ~(termios.ECHO | termios.ICANON)       # keep presses off the screen
    termios.tcsetattr(tty, termios.TCSANOW, quiet)

    def capture(prompt, default, allow_none):
        print(prompt, file=sys.stderr, flush=True)
        while True:
            for d in select.select(devs, [], [])[0]:
                for ev in d.read():
                    if ev.type != ecodes.EV_KEY or ev.value != 1:
                        continue
                    if ev.code == ecodes.KEY_ENTER:
                        return default
                    if ev.code == ecodes.KEY_ESC and allow_none:
                        return "none"
                    if ev.code in _MOUSE_OK or (ev.code not in _REFUSED and not
                                                ecodes.BTN_MISC <= ev.code < ecodes.KEY_OK):
                        return _code_name(ev.code)
                    if ev.code in _REFUSED:
                        print(f"    {_code_name(ev.code)} is a typing key; pick another.",
                              file=sys.stderr, flush=True)

    try:
        primary = capture("    Press the key or mouse button to hold for dictation "
                          "(Enter keeps Print Screen).", "KEY_SYSRQ", False)
        print(f"    primary: {primary}", file=sys.stderr)
        secondary = capture("    Press a second one, Enter for the mouse forward/side "
                            "button, or Esc for none.", "BTN_EXTRA", True)
        if secondary == primary:
            secondary = "none"
        print(f"    secondary: {secondary}", file=sys.stderr)
    finally:
        termios.tcflush(tty, termios.TCIFLUSH)          # drop the keystrokes the terminal also got
        termios.tcsetattr(tty, termios.TCSANOW, saved)
    print(primary, secondary)


if __name__ == "__main__":
    import sys
    if "--pick-ptt" in sys.argv:
        pick_ptt()
    else:
        main()
