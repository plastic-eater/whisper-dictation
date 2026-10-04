# whisper-dictation

System-wide **push-to-talk** voice-to-text for Ubuntu / GNOME Wayland, fully
local and offline. Hold **Print Screen**, talk, release — the text types into
whatever window is focused, with a live translucent preview while you hold.

Uses [whisper.cpp](https://github.com/ggml-org/whisper.cpp) for transcription and
[ydotool](https://github.com/ReimuNotMoe/ydotool) to type into any app.

## Install

```bash
git clone <this-repo> whisper-dictation
cd whisper-dictation
./install.sh
```

`install.sh` installs deps, builds whisper.cpp + ydotool from source (the repo
stays tiny: engines and models are fetched/built, not committed), benchmarks CPU vs
Vulkan (GPU), then benchmarks bigger models until one gets too slow for a live
preview and lets you pick (recommending the biggest that keeps up), sets up the
typing daemon, and enables the user services. **Log out and back in once** afterward (so the new `input` group takes
effect), then hold your push-to-talk key to dictate. The installer asks you to
press the key (and optionally a second key or mouse button) you want; Enter
keeps the defaults, Print Screen and the mouse forward/side button.

Tested on Ubuntu 24.04 / GNOME Wayland, CPU and Vulkan (Intel Iris Xe).

## Windows

Download `whisper-dictation-setup.exe` from the latest GitHub release and run
it. It installs per-user (no admin prompt), then opens first-run setup: it
downloads the speech engine (the NVIDIA GPU build too if you have one, after
asking, since it's ~640 MB), benchmarks it, lets you pick a model, and starts
dictation. It also starts at login.

- Setup asks you to press your push-to-talk key, plus an optional second key or
  mouse button; Enter keeps the defaults, **Insert** and the mouse
  forward/side button. The key is blocked while the app runs, so it never does
  its normal job (Insert never toggles overwrite mode). Change them later by
  rerunning "Whisper Dictation Setup".
- Settings, `tuning_local.py` and `dictation.log` live in
  `%LOCALAPPDATA%\whisper-dictation` (Start menu: "Whisper Dictation Settings").
  After editing, rerun "Whisper Dictation Setup", which restarts the app.
- The installer isn't code-signed yet, so Windows shows "Windows protected
  your PC": click **More info**, then **Run anyway**.
- Dictation can't type into windows running as administrator; Windows blocks it.
- Uninstall from Settings > Apps. Your config and `tuning_local.py` are kept.

## How it works

On release the clip is transcribed and typed into the focused window. While you
hold, a background thread shows a live, throwaway preview in a translucent
overlay; only the final transcription on release is actually typed.

- A listener (`ptt-listener.py`) reads the keyboard directly via evdev — Wayland
  hotkeys only fire on key *press*, never release, so this is the only way to get
  hold-to-talk. Needs the `input` group.
- Transcription hits a warm **whisper-server** (model resident in RAM,
  no per-call load) on :8910 — used for both the typed text and the live preview,
  so the preview shows exactly what will be typed. Falls back to `whisper-cli` if
  the server is down.
- `preview-overlay.py` is a GTK override-redirect window with an RGBA visual —
  translucent background, opaque text, and it never steals keyboard focus.
- **ydotool** types via a kernel-level uinput virtual keyboard (the one way to
  inject into any window on Wayland). Ubuntu's package omits the `ydotoold`
  daemon, so it's built from source.

## Services

| Service | Role |
|---|---|
| `whisper-ptt` (user) | the keyboard listener |
| `whisper-server` (user) | :8910, typed text + live preview |
| `ydotoold` (system) | virtual keyboard for typing |

```bash
systemctl --user restart whisper-ptt          # after editing ptt-listener.py
journalctl --user -u whisper-ptt -f            # listener logs
```

## Tuning

- **Preview transparency / look:** `BG_ALPHA`, font, position, `MAX_CHARS` — top
  of `preview-overlay.py`. `BG_ALPHA` only affects the background; text stays solid.
- **Preview cadence / window:** `PREVIEW_STEP`, `PREVIEW_TAIL_SEC` in `ptt-listener.py`.
- **Push-to-talk buttons:** `PTT_PRIMARY` / `PTT_SECONDARY` (evdev names like
  `KEY_SYSRQ`, `BTN_EXTRA`, or `none`) in `~/.config/whisper-dictation/config`.
  To pick by pressing again, delete those lines and rerun `install.sh`. Linux
  can't block the key, so it still does its normal job; pick one you don't use.
- **Typing speed:** the `--key-delay 4 --key-hold 2` args in `stop_and_type()`.
- **Accuracy vs speed / CPU vs GPU:** `WHISPER_MODEL` and `WHISPER_BACKEND` in
  `~/.config/whisper-dictation/config` (written once by `install.sh` from a
  benchmark), then `systemctl --user restart whisper-server whisper-ptt`.

## Teaching it your words

A postprocessing pipeline fixes what whisper reliably gets wrong: texting
acronyms forced lowercase ("LOL" → "lol"), "pseudo apt" → "sudo apt", spoken
punctuation ("question mark" → "?"), spoken deletes (say "backspace backspace"
to delete two characters), and literal phrase fixes ("clod" → "Claude").

Names and jargon go one step earlier: `VOCAB` is sent to whisper as its prompt,
so it expects those spellings while transcribing instead of being corrected
afterward. It's capped at about 100 words, so it's for the names you use most;
`PHRASE_FIXES` stays as the backstop.

To add your own — the names whisper misspells, your own capitalizations — copy
`tuning_local.example.py` to `tuning_local.py` (gitignored, so your entries
never leave your machine) and add entries; they merge over the built-ins at
startup. Then `systemctl --user restart whisper-ptt`.

## Notes

- The ydotoold socket is world-accessible (`0666`) so the listener can type via
  it — fine on a single-user laptop, not on a shared machine.
- The mic follows the GNOME default input (Settings → Sound).

## Troubleshooting

- **Nothing happens on hold:** `systemctl --user status whisper-ptt` (and check it
  found a keyboard: `journalctl --user -u whisper-ptt`). Did you log out/in after install?
- **Records but doesn't type:** the ydotool daemon — `systemctl status ydotoold`.
- **No preview:** `systemctl --user status whisper-preview`.
- **Wrong/silent mic:** set the default input in GNOME Settings → Sound.
