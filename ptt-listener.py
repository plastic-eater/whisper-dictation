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
import re
import time
import signal
import threading
import subprocess
import selectors
import urllib.request

import evdev
from evdev import ecodes

SERVER_URL         = "http://127.0.0.1:8910/inference"   # base.en — used for both the final
PREVIEW_SERVER_URL = SERVER_URL                          # typed text and the live preview


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
PTT_KEY    = ecodes.KEY_SYSRQ        # Print Screen — keyboard trigger (laptop/touchpad)
PTT_BTN    = ecodes.BTN_EXTRA        # Bluetooth mouse "forward"/side button — hold-to-talk
PTT_CODES  = (PTT_KEY, PTT_BTN)
TAP_SEC        = 0.25              # a PTT_BTN press shorter than this is a tap, not speech
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
def clean(text):
    text = re.sub(r"\[[^\]]*\]", "", text)   # [BLANK_AUDIO] and similar
    text = re.sub(r"\([^)]*\)", "", text)    # (silence) and similar
    return " ".join(text.split()).strip()


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


_ACRONYMS_BY_LEN = sorted(LOWERCASE_ACRONYMS, key=len, reverse=True)


def _decompose(tok):
    """Return a list of acronyms that exactly tile `tok`, or None."""
    if not tok:
        return []
    for a in _ACRONYMS_BY_LEN:
        if tok.startswith(a):
            rest = _decompose(tok[len(a):])
            if rest is not None:
                return [a] + rest
    return None


def split_merged_acronyms(text):
    """Split a token whisper fused from back-to-back acronyms ('lolty' -> 'lol ty').
    Only fires when the whole token is >= 2 acronyms, so real words (which contain
    non-acronym letters) are never touched."""
    def repl(m):
        pieces = _decompose(m.group(0).lower())
        return " ".join(pieces) if pieces and len(pieces) >= 2 else m.group(0)
    return re.sub(r"[A-Za-z]+", repl, text)


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


# Literal phrase fixes: case-insensitive whole-phrase match -> fixed spelling.
# Keys are lowercase; words may be separated by spaces or hyphens (whisper spells
# the "okie doke" interjection several ways), and the separator matches either.
PHRASE_FIXES = {
    "okie doke": "okiedok",
    "okey doke": "okiedok",
    "okie dokie": "okiedok",
    "okey dokie": "okiedok",
    "clod": "Claude",         # whisper hears the name "Claude" as "clod"
    "[name]": "[Name]",   # whisper mistypes the name "[Name]"
}
_PHRASE_RE = re.compile(
    r"\b(?:" + "|".join(
        r"[\s-]+".join(map(re.escape, re.split(r"[\s-]+", k)))
        for k in sorted(PHRASE_FIXES, key=len, reverse=True)
    ) + r")\b",
    re.IGNORECASE,
)


def fix_phrases(text):
    return _PHRASE_RE.sub(
        lambda m: PHRASE_FIXES[re.sub(r"[\s-]+", " ", m.group(0).lower())], text
    )


# Spoken punctuation: say the name of a mark and get the mark itself.
# "hello comma world" -> "hello, world"; "wow bang" -> "wow!". The mark REPLACES
# whatever punctuation whisper already glued around the spoken word, so saying a
# "?" or "!" overrides the "." whisper guessed at the end of the sentence:
# "are we done. Question mark" -> "are we done?" (not "are we done.?").
# Note: "bang" and "period" are also ordinary words ("big bang", "grace period");
# they'll be turned into marks too. Drop the entry if that bites.
SPOKEN_PUNCT = {
    "comma": ",",
    "period": ".",
    "full stop": ".",
    "question mark": "?",
    "exclamation mark": "!",
    "exclamation point": "!",
    "bang": "!",
    "colon": ":",
    "semicolon": ";",
}
# Whitespace + punctuation whisper may sprinkle around the spoken word; absorbed so
# the spoken mark wins. A *run* of back-to-back spoken marks collapses to the LAST
# one — say "bang" then "period" and you get "." not "!", so you can self-correct a
# mark you didn't mean ("big bang period" -> "big.").
_PUNCT_EDGE = ".,!?;:…"
_PUNCT_WORDS = "|".join(
    r"[\s-]+".join(map(re.escape, re.split(r"[\s-]+", k)))
    for k in sorted(SPOKEN_PUNCT, key=len, reverse=True)
)
_PUNCT_WORD_RE = re.compile(r"\b(?:" + _PUNCT_WORDS + r")\b", re.IGNORECASE)
_edge = r"[\s" + re.escape(_PUNCT_EDGE) + r"]"
_PUNCT_RE = re.compile(
    _edge + r"*"                                      # leading: glue to prev word
    r"\b(?:" + _PUNCT_WORDS + r")\b"                  # first spoken mark
    r"(?:" + _edge + r"+\b(?:" + _PUNCT_WORDS + r")\b)*"   # any adjacent marks
    r"[" + re.escape(_PUNCT_EDGE) + r"]*",            # trailing punct (no spaces)
    re.IGNORECASE,
)


def fix_spoken_punct(text):
    def repl(m):
        last = _PUNCT_WORD_RE.findall(m.group(0))[-1]
        return SPOKEN_PUNCT[re.sub(r"[\s-]+", " ", last.lower())]
    return _PUNCT_RE.sub(repl, text)


# Casual exclamations whisper capitalizes; lowercase them EXCEPT when they open a
# sentence (start of the text, or after . ! ? …). So "oh jesus" -> "jesus", but a
# sentence-initial "Jesus wept." keeps its capital. Add a word here to apply the rule.
SENTENCE_AWARE_LOWER = {"jesus", "christ", "god"}
_SAL_RE = re.compile(
    r"\b(?:" + "|".join(map(re.escape, SENTENCE_AWARE_LOWER)) + r")\b", re.IGNORECASE
)


def lower_unless_sentence_start(text):
    def repl(m):
        prefix = text[:m.start()].rstrip()
        word = m.group(0).lower()
        return word.capitalize() if not prefix or prefix.endswith((".", "!", "?", "…")) else word
    return _SAL_RE.sub(repl, text)


def postprocess(text):
    return lower_unless_sentence_start(fix_spoken_punct(
        fix_phrases(lower_acronyms(fix_sudo(split_merged_acronyms(text))))
    ))


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


# Spoken editing command: an utterance of just delete-words ("backspace" or "delete",
# said one or more times; "back space" spelled either way) deletes that many characters
# instead of typing them. Only fires when the WHOLE utterance is delete-words, so a
# sentence that merely contains "delete"/"backspace" still types normally.
DELETE_WORDS = {"backspace", "delete"}


def spoken_deletes(text):
    words = re.findall(r"[a-z]+", re.sub(r"\bback\s+space\b", "backspace", text.lower()))
    return len(words) if words and all(w in DELETE_WORDS for w in words) else 0


def stop_and_type():
    if not _stop_recording():
        return
    overlay_write("⏳ transcribing…")
    text = postprocess(transcribe(WAV, SERVER_URL, timeout=60))
    overlay_stop()
    if not text:
        return
    n = spoken_deletes(text)
    if n:
        send_keys([ecodes.KEY_BACKSPACE] * n)
        return
    subprocess.run([YDOTOOL, "type", "--key-delay", "4", "--key-hold", "2", "--", text + " "])


def find_devices():
    """Devices that report a PTT trigger — the keyboard (Print Screen) and the
    mouse (forward/side button). The ydotool virtual device is skipped so injected
    events can't trigger a recording."""
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
    last_tap_at = 0.0                    # last quick tap of PTT_BTN, for double-tap detection
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
                        if ev.code == PTT_BTN and held < TAP_SEC:
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


if __name__ == "__main__":
    main()
