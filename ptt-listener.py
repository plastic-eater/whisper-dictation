#!/usr/bin/env python3
"""Push-to-talk dictation with a live on-screen preview.

Hold Print Screen to record from the default mic; release to transcribe the clip
and type the text into the focused window via ydotool.

Transcription goes to a warm whisper-server (model kept resident in RAM), so each
call is fast with no per-call model load. While held, a background thread sends
snapshots of the audio-so-far for a live preview in a focus-safe overlay; that
preview is throwaway — only the final transcription on release is typed. If the
server is unreachable, it falls back to spawning whisper-cli locally.

Reads keyboard events directly (needs the 'input' group) because Wayland/GNOME
hotkeys only fire on key-press, never release.
"""
import os
import re
import signal
import threading
import subprocess
import selectors
import urllib.request

import evdev
from evdev import ecodes

SERVER_URL         = "http://127.0.0.1:8910/inference"   # base.en, final typed text
PREVIEW_SERVER_URL = "http://127.0.0.1:8911/inference"   # tiny.en, fast throwaway preview


def _engine_dir():
    """Where whisper.cpp is built. Override with $WHISPER_DICT_HOME; otherwise
    autodetect the install layout (~/.local/share) or a manual ~/Documents/Code build."""
    env = os.environ.get("WHISPER_DICT_HOME")
    if env:
        return os.path.expanduser(env)
    for c in ("~/.local/share/whisper-dictation", "~/Documents/Code"):
        p = os.path.expanduser(c)
        if os.path.exists(f"{p}/whisper.cpp/build/bin/whisper-cli"):
            return p
    return os.path.expanduser("~/.local/share/whisper-dictation")


ENGINE_DIR = _engine_dir()
WHISPER    = f"{ENGINE_DIR}/whisper.cpp/build/bin/whisper-cli"             # fallback engine
MODEL      = f"{ENGINE_DIR}/whisper.cpp/models/ggml-base.en.bin"           # fallback model
THREADS    = "12"                                                          # fallback
YDOTOOL    = "/usr/local/bin/ydotool"
OVERLAY    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "preview-overlay.py")
RUNTIME    = os.environ.get("XDG_RUNTIME_DIR", "/tmp")
WAV        = f"{RUNTIME}/whisper-ptt.wav"
SNAP       = f"{RUNTIME}/whisper-ptt.snap.wav"
PTT_KEY    = ecodes.KEY_SYSRQ        # Print Screen
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
def clean(text):
    text = re.sub(r"\[[^\]]*\]", "", text)   # [BLANK_AUDIO] and similar
    text = re.sub(r"\([^)]*\)", "", text)    # (silence) and similar
    return " ".join(text.split()).strip()


def collapse_repeats(text, max_phrase=6):
    """Collapse consecutive repeated word-phrases (tiny.en preview loop artifacts).
    Preview-only — the typed text is never run through this."""
    words = text.split()
    changed = True
    while changed:
        changed = False
        for L in range(1, max_phrase + 1):
            i = 0
            while i + 2 * L <= len(words):
                if [w.lower() for w in words[i:i + L]] == [w.lower() for w in words[i + L:i + 2 * L]]:
                    del words[i + L:i + 2 * L]
                    changed = True
                else:
                    i += 1
    return " ".join(words)


# Texting acronyms whisper tends to capitalize; force them lowercase. Matching is
# whole-word and case-insensitive. Add/remove freely. Ambiguous ones (e.g. "rn"/"RN",
# "ty", "np") are intentionally left out so legitimate capitalized words aren't clobbered.
LOWERCASE_ACRONYMS = {
    "lol", "lmao", "lmfao", "rofl", "ttyl", "brb", "idk", "imo", "imho", "iirc",
    "tbh", "btw", "fyi", "omg", "wtf", "smh", "nvm", "irl", "afaik", "idc", "ikr",
    "jk", "tldr", "fwiw", "ngl", "iykyk", "wyd", "hmu", "istg", "tmi", "afk",
    "rn", "ty", "np",   # "ty" also lowercases the name "Ty" — remove if that bites
}
_ACRONYM_RE = re.compile(
    r"\b(?:" + "|".join(map(re.escape, LOWERCASE_ACRONYMS)) + r")\b", re.IGNORECASE
)


def lower_acronyms(text):
    return _ACRONYM_RE.sub(lambda m: m.group(0).lower(), text)


# whisper mishears "sudo" as the homophone "pseudo". Convert it back ONLY when the
# next word is a shell command, so real uses ("pseudocode", "pseudo-random") survive.
SHELL_COMMANDS = {
    "apt", "apt-get", "dnf", "pacman", "snap", "systemctl", "service", "journalctl",
    "rm", "cp", "mv", "mkdir", "rmdir", "ln", "chmod", "chown", "chgrp", "tee",
    "dd", "mount", "umount", "reboot", "shutdown", "kill", "killall", "ufw",
    "nano", "vim", "vi", "docker", "git", "make", "pip", "pip3", "npm", "modprobe",
    "usermod", "useradd", "groupadd", "passwd", "visudo", "iptables", "fdisk", "su",
}
_PSEUDO_RE = re.compile(r"\bpseudo\b(\s+)(\w[\w-]*)", re.IGNORECASE)


def fix_sudo(text):
    return _PSEUDO_RE.sub(
        lambda m: ("sudo" + m.group(1) + m.group(2))
        if m.group(2).lower() in SHELL_COMMANDS else m.group(0),
        text,
    )


def postprocess(text):
    return lower_acronyms(fix_sudo(text))


def http_transcribe(path, url, timeout=30):
    """POST the audio to a warm whisper-server. Returns text, or None if the
    server can't be reached (so the caller can fall back to whisper-cli)."""
    try:
        with open(path, "rb") as f:
            audio = f.read()
    except OSError:
        return ""
    boundary = "----wptt" + os.urandom(8).hex()
    crlf = "\r\n"
    pre = (
        f"--{boundary}{crlf}"
        f'Content-Disposition: form-data; name="response_format"{crlf}{crlf}text{crlf}'
        f"--{boundary}{crlf}"
        f'Content-Disposition: form-data; name="file"; filename="a.wav"{crlf}'
        f"Content-Type: audio/wav{crlf}{crlf}"
    ).encode()
    body = pre + audio + f"{crlf}--{boundary}--{crlf}".encode()
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return clean(r.read().decode("utf-8", "replace"))
    except Exception:
        return None


def cli_transcribe(path):
    try:
        out = subprocess.run(
            [WHISPER, "-m", MODEL, "-f", path, "-nt", "-np", "-t", THREADS],
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
    while not _preview_stop.is_set():
        try:
            text = postprocess(collapse_repeats(snapshot_transcribe()))
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
    overlay_start()
    _preview_stop.clear()
    _preview_thread = threading.Thread(target=preview_loop, daemon=True)
    _preview_thread.start()


def stop_and_type():
    global _rec, _preview_thread
    if _rec is None:
        return
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

    overlay_write("⏳ transcribing…")
    text = postprocess(transcribe(WAV, SERVER_URL, timeout=60))
    overlay_stop()
    if not text:
        return
    subprocess.run([YDOTOOL, "type", "--key-delay", "4", "--key-hold", "2", "--", text + " "])


def find_keyboards():
    devs = []
    for path in evdev.list_devices():
        try:
            d = evdev.InputDevice(path)
        except Exception:
            continue
        keys = d.capabilities().get(ecodes.EV_KEY, [])
        if PTT_KEY in keys and "ydotool" not in d.name.lower():
            devs.append(d)
    return devs


def main():
    sel = selectors.DefaultSelector()
    kbds = find_keyboards()
    if not kbds:
        raise SystemExit("no readable keyboard with a Print Screen key")
    for d in kbds:
        sel.register(d, selectors.EVENT_READ)
    while True:
        for key, _ in sel.select():
            try:
                for ev in key.fileobj.read():
                    if ev.type == ecodes.EV_KEY and ev.code == PTT_KEY:
                        if ev.value == 1:        # key down
                            start_recording()
                        elif ev.value == 0:      # key up
                            stop_and_type()
                        # value == 2 (autorepeat while held) ignored
            except OSError:
                pass


if __name__ == "__main__":
    main()
