; Autopilot installer (Phase D.9) — Inno Setup 6 script.
;
; Input:  the PyInstaller ONEDIR build at dist\Autopilot\ (run
;         scripts\build_exe.bat first). Output: dist\installer\
;         AutopilotSetup-3.1.0.exe.
; ffmpeg/ffprobe (+ optional piper) are deliberately NOT bundled
; (license + size); the user copies them post-install — the app runs
; every non-render feature without them and reports exactly what is
; missing. AFTER_INSTALL.txt explains this on the final wizard page.

#define AppName "Autopilot"
#define AppVersion "3.1.0"
#define AppPublisher "Autopilot"
#define AppExeName "Autopilot.exe"
#define SourceDir "..\dist\Autopilot"

[Setup]
AppId={{8F2E6C4A-3B1D-4E7F-9A52-3C1B7D9E5A40}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=..\dist\installer
OutputBaseFilename=AutopilotSetup-{#AppVersion}
Compression=lzma2/normal
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallMode=x64compatible
PrivilegesRequiredOverridesAllowed=dialog
WizardStyle=modern
InfoAfterFile=AFTER_INSTALL.txt
UninstallDisplayIcon={app}\{#AppExeName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; \
  GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; \
  Flags: ignoreversion recursesubdirs createallsubdirs

[Dirs]
; writable runtime folders (render state, logs, project folders)
Name: "{app}\projects"
Name: "{app}\logs"
Name: "{app}\cache"
Name: "{app}\temp"
Name: "{app}\projects"

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"; \
  Parameters: "ui"
Name: "{group}\{#AppName} (console)"; Filename: "{app}\{#AppExeName}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; \
  Parameters: "ui"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Parameters: "ui"; \
  Description: "{cm:LaunchProgram,{#StringChange(AppName, '&', '&&')}}"; \
  Flags: nowait postinstall skipifsilent
