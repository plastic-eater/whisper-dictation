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
stays tiny — engines and models are fetched/built, not committed), downloads the
`base.en` model, sets up the typing daemon, and enables the user services. **Log out and back in once** afterward (so the new `input` group takes
effect), then hold Print Screen to dictate.

Tested on Ubuntu 24.04 / GNOME Wayland, CPU-only.

## How it works

On release the clip is transcribed and typed into the focused window. While you
hold, a background thread shows a live, throwaway preview in a translucent
overlay; only the final transcription on release is actually typed.

- A listener (`ptt-listener.py`) reads the keyboard directly via evdev — Wayland
  hotkeys only fire on key *press*, never release, so this is the only way to get
  hold-to-talk. Needs the `input` group.
- Transcription hits a warm **whisper-server** (base.en, model resident in RAM,
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
| `whisper-server` (user) | base.en :8910 — typed text + live preview |
| `ydotoold` (system) | virtual keyboard for typing |

```bash
systemctl --user restart whisper-ptt          # after editing ptt-listener.py
journalctl --user -u whisper-ptt -f            # listener logs
```

## Tuning

- **Preview transparency / look:** `BG_ALPHA`, font, position, `MAX_CHARS` — top
  of `preview-overlay.py`. `BG_ALPHA` only affects the background; text stays solid.
- **Preview cadence / window:** `PREVIEW_STEP`, `PREVIEW_TAIL_SEC` in `ptt-listener.py`.
- **Hotkey:** `PTT_KEY` (`KEY_SYSRQ` = Print Screen) in `ptt-listener.py`.
- **Typing speed:** the `--key-delay 4 --key-hold 2` args in `stop_and_type()`.
- **Accuracy vs speed:** swap the model in `~/.config/systemd/user/whisper-server.service`
  (base.en → small.en), then `systemctl --user restart whisper-server`.

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
