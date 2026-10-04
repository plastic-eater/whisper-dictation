# Personal tuning overrides.
#
# Copy this file to tuning_local.py (gitignored, so your entries never leave
# your machine) and add whatever whisper keeps getting wrong for *you*.
# Anything defined here merges over the built-in tables in ptt-listener.py;
# restart afterward:  systemctl --user restart whisper-ptt
#
# Available tables (all optional):
#   PHRASE_FIXES         dict  spoken phrase (lowercase) -> replacement text
#   VOCAB                set   names/jargon whisper should expect (sent as its prompt)
#   LOWERCASE_ACRONYMS   set   words to force lowercase ("LOL" -> "lol")
#   SENTENCE_AWARE_LOWER set   lowercase unless the word opens a sentence
#   SPOKEN_PUNCT         dict  spoken word -> punctuation mark
#   SHELL_COMMANDS       set   words that flip a preceding "pseudo" to "sudo"
#   DELETE_WORDS         set   whole-utterance words that mean "backspace"

# The most common use: names whisper mishears or misspells.
PHRASE_FIXES = {
    "jean luc": "Jean-Luc",
}

# Names whisper should hear right in the first place. Spell them exactly as
# you want them typed. Keep the list short (roughly 100 words max).
VOCAB = {"Jean-Luc"}
