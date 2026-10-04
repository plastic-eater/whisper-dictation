"""Platform-independent half of the dictation listener: the engine config,
the text cleanup and postprocess pipeline (with the user's tuning_local.py
merged in), the vocabulary prompt, and the HTTP call to whisper-server.
"""
import os
import re
import urllib.request

SERVER_URL = "http://127.0.0.1:8910/inference"

# Where the engine config and the user's tuning_local.py live. On Windows the
# app is a bundled exe, so both sit in the per-user data folder next to the
# engine and models instead of beside the code.
if os.name == "nt":
    APP_DIR    = os.path.join(os.environ["LOCALAPPDATA"], "whisper-dictation")
    CONFIG     = os.path.join(APP_DIR, "config")
    TUNING_DIR = APP_DIR
else:
    CONFIG     = os.path.expanduser("~/.config/whisper-dictation/config")
    TUNING_DIR = os.path.dirname(os.path.abspath(__file__))


def engine_config():
    """WHISPER_BACKEND / WHISPER_MODEL from the config install.sh writes; the
    whisper-server unit reads the same file, so the fallback matches the server."""
    cfg = {"WHISPER_BACKEND": "cpu", "WHISPER_MODEL": "base.en"}
    try:
        for line in open(CONFIG):
            k, sep, v = line.strip().partition("=")
            if sep and not k.startswith("#"):
                cfg[k] = v
    except OSError:
        pass
    return cfg


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
    "rn", "ty", "np",   # "ty" also lowercases the name "Ty"; remove if that bites
}


def lower_acronyms(text):
    return _ACRONYM_RE.sub(lambda m: m.group(0).lower(), text)


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
# Keys are lowercase; words may be separated by spaces or hyphens, and the
# separator matches either. This is the place to teach it the names and words
# it reliably mishears; add yours via tuning_local.py (see below).
PHRASE_FIXES = {
    "clod": "Claude",         # whisper hears the name "Claude" as "clod"
}


# Words whisper should expect (names, jargon). Sent as its initial prompt, so it
# leans toward these spellings while decoding, where PHRASE_FIXES patches the
# text afterward. Keep it short: whisper reads at most ~224 tokens of it (about
# 100 words), and a longer list makes it likelier to hallucinate a listed word
# into silence. Formatting rules (punctuation, lowercasing) belong in the tables.
VOCAB = {"Claude", "sudo"}


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
# one: say "bang" then "period" and you get "." not "!", so you can self-correct a
# mark you didn't mean ("big bang period" -> "big.").
_PUNCT_EDGE = ".,!?;:…"


def fix_spoken_punct(text):
    def repl(m):
        last = _PUNCT_WORD_RE.findall(m.group(0))[-1]
        return SPOKEN_PUNCT[re.sub(r"[\s-]+", " ", last.lower())]
    return _PUNCT_RE.sub(repl, text)


# Casual exclamations whisper capitalizes; lowercase them EXCEPT when they open a
# sentence (start of the text, or after . ! ? …). So "oh jesus" -> "jesus", but a
# sentence-initial "Jesus wept." keeps its capital. Add a word here to apply the rule.
SENTENCE_AWARE_LOWER = {"jesus", "christ", "god"}


def lower_unless_sentence_start(text):
    def repl(m):
        prefix = text[:m.start()].rstrip()
        word = m.group(0).lower()
        return word.capitalize() if not prefix or prefix.endswith((".", "!", "?", "…")) else word
    return _SAL_RE.sub(repl, text)


# Delete-words for the spoken-delete command (see spoken_deletes below).
DELETE_WORDS = {"backspace", "delete"}


# --- personal tuning ---------------------------------------------------------
# Every table above (PHRASE_FIXES, VOCAB, LOWERCASE_ACRONYMS, SPOKEN_PUNCT,
# SENTENCE_AWARE_LOWER, SHELL_COMMANDS, DELETE_WORDS) can be extended without
# editing this file: copy tuning_local.example.py to tuning_local.py (gitignored,
# so it never leaves your machine) and add entries there. They merge over the
# built-ins at startup.
def _load_local_tuning():
    path = os.path.join(TUNING_DIR, "tuning_local.py")
    if not os.path.exists(path):
        return
    import importlib.util
    spec = importlib.util.spec_from_file_location("tuning_local", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for name in ("PHRASE_FIXES", "SPOKEN_PUNCT"):
        globals()[name].update(getattr(mod, name, {}))
    for name in ("VOCAB", "LOWERCASE_ACRONYMS", "SENTENCE_AWARE_LOWER", "SHELL_COMMANDS", "DELETE_WORDS"):
        globals()[name].update(getattr(mod, name, ()))


def _phrase_pattern(keys):
    """Alternation matching each key with spaces/hyphens interchangeable."""
    return "|".join(
        r"[\s-]+".join(map(re.escape, re.split(r"[\s-]+", k)))
        for k in sorted(keys, key=len, reverse=True)
    )


def _compile_tables():
    """Build the regexes derived from the tuning tables, after local overrides."""
    global _ACRONYM_RE, _ACRONYMS_BY_LEN, _PHRASE_RE, _PUNCT_WORD_RE, _PUNCT_RE, _SAL_RE, PROMPT
    PROMPT = ", ".join(sorted(VOCAB, key=str.lower)) + "." if VOCAB else ""
    _ACRONYM_RE = re.compile(
        r"\b(?:" + "|".join(map(re.escape, LOWERCASE_ACRONYMS)) + r")\b", re.IGNORECASE
    )
    _ACRONYMS_BY_LEN = sorted(LOWERCASE_ACRONYMS, key=len, reverse=True)
    _PHRASE_RE = re.compile(r"\b(?:" + _phrase_pattern(PHRASE_FIXES) + r")\b", re.IGNORECASE)
    punct_words = _phrase_pattern(SPOKEN_PUNCT)
    _PUNCT_WORD_RE = re.compile(r"\b(?:" + punct_words + r")\b", re.IGNORECASE)
    edge = r"[\s" + re.escape(_PUNCT_EDGE) + r"]"
    _PUNCT_RE = re.compile(
        edge + r"*"                                      # leading: glue to prev word
        r"\b(?:" + punct_words + r")\b"                  # first spoken mark
        r"(?:" + edge + r"+\b(?:" + punct_words + r")\b)*"   # any adjacent marks
        r"[" + re.escape(_PUNCT_EDGE) + r"]*",           # trailing punct (no spaces)
        re.IGNORECASE,
    )
    _SAL_RE = re.compile(
        r"\b(?:" + "|".join(map(re.escape, SENTENCE_AWARE_LOWER)) + r")\b", re.IGNORECASE
    )


_load_local_tuning()
_compile_tables()


# Postprocessing pipeline, applied in order. Add a step (text -> text) to extend it.
_POSTPROCESS_STEPS = (
    split_merged_acronyms,        # "lolty" -> "lol ty"
    fix_sudo,                     # "pseudo apt" -> "sudo apt"
    lower_acronyms,               # "LOL" -> "lol"
    fix_phrases,                  # "clod" -> "Claude"
    fix_spoken_punct,             # "comma" -> ","
    lower_unless_sentence_start,  # "oh Jesus" -> "oh jesus"
)


def postprocess(text):
    for step in _POSTPROCESS_STEPS:
        text = step(text)
    return text


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
        f'Content-Disposition: form-data; name="prompt"{crlf}{crlf}{PROMPT}{crlf}'
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


# Spoken editing command: an utterance of just delete-words ("backspace" or "delete",
# said one or more times; "back space" spelled either way) deletes that many characters
# instead of typing them. Only fires when the WHOLE utterance is delete-words, so a
# sentence that merely contains "delete"/"backspace" still types normally.
def spoken_deletes(text):
    words = re.findall(r"[a-z]+", re.sub(r"\bback\s+space\b", "backspace", text.lower()))
    return len(words) if words and all(w in DELETE_WORDS for w in words) else 0
