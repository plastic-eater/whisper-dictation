"""Built-in postprocess behavior, run on Linux and in Windows CI so both
platforms produce the same text. Cases avoid anything a personal
tuning_local.py is likely to override."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dictation_core import clean, postprocess, spoken_deletes  # noqa: E402

CASES = {
    "LOL that was funny BTW": "lol that was funny btw",
    "lolty for the help": "lol ty for the help",
    "run pseudo apt update": "run sudo apt update",
    "pseudocode is fine and pseudo-random too": "pseudocode is fine and pseudo-random too",
    "ask clod about it": "ask Claude about it",
    "hello comma world": "hello, world",
    "are we done. Question mark": "are we done?",
    "wow bang": "wow!",
    "big bang period": "big.",
    "oh Jesus that hurt. Jesus wept.": "oh jesus that hurt. Jesus wept.",
    "God, that's great. oh God": "God, that's great. oh god",
    "[BLANK_AUDIO] something (silence) here": "something here",
}


def test_postprocess():
    for raw, want in CASES.items():
        assert postprocess(clean(raw)) == want, raw


def test_spoken_deletes():
    assert spoken_deletes("backspace backspace") == 2
    assert spoken_deletes("Back space.") == 1
    assert spoken_deletes("delete the file") == 0
