; Inno Setup script: wraps the PyInstaller build into whisper-dictation-setup.exe.
; Build: iscc /DAppVersion=1.2.3 windows\installer.iss  (after pyinstaller)
; Per-user install, so no admin prompt. The engine and models are downloaded by
; first-run setup into %LOCALAPPDATA%\whisper-dictation, not shipped here.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{6C1F4B52-8E0B-4E52-9B8A-3D9C2F1A7E41}
AppName=Whisper Dictation
AppVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\Whisper Dictation
PrivilegesRequired=lowest
DisableProgramGroupPage=yes
DisableDirPage=yes
OutputDir=dist
OutputBaseFilename=whisper-dictation-setup
Compression=lzma2
SolidCompression=yes
CloseApplications=force
UninstallDisplayName=Whisper Dictation

[Files]
Source: "dist\whisper-dictation\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{userprograms}\Whisper Dictation"; Filename: "{app}\whisper-dictation.exe"
Name: "{userprograms}\Whisper Dictation Setup"; Filename: "{app}\whisper-dictation.exe"; Parameters: "--setup"
Name: "{userprograms}\Whisper Dictation Settings"; Filename: "{localappdata}\whisper-dictation"

[Registry]
; Start at login.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "WhisperDictation"; ValueData: """{app}\whisper-dictation.exe"""; Flags: uninsdeletevalue

[Run]
; First-run setup: downloads the engine, benchmarks, picks a model, then starts the app.
Filename: "{app}\whisper-dictation.exe"; Parameters: "--setup"; Description: "Set up the speech engine"; Flags: postinstall nowait

[UninstallRun]
Filename: "taskkill"; Parameters: "/F /IM whisper-dictation.exe"; Flags: runhidden; RunOnceId: "StopApp"

[UninstallDelete]
; Engine, models and log go; config and tuning_local.py stay for a reinstall.
Type: filesandordirs; Name: "{localappdata}\whisper-dictation\engine"
Type: filesandordirs; Name: "{localappdata}\whisper-dictation\models"
Type: files; Name: "{localappdata}\whisper-dictation\jfk.wav"
Type: files; Name: "{localappdata}\whisper-dictation\dictation.log"
