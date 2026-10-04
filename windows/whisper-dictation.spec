# PyInstaller build: pyinstaller windows/whisper-dictation.spec
# One windowed exe (no console); `--setup` gives itself a console for first-run setup.
import os

root = os.path.join(SPECPATH, "..")

a = Analysis(
    [os.path.join(SPECPATH, "dictation.py")],
    pathex=[root, SPECPATH],
    datas=[(os.path.join(root, "tuning_local.example.py"), ".")],
    # pynput picks its OS backend at runtime, so PyInstaller can't see the import.
    hiddenimports=["first_run", "pynput.keyboard._win32", "pynput.mouse._win32"],
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="whisper-dictation", console=False)
coll = COLLECT(exe, a.binaries, a.datas, name="whisper-dictation")
