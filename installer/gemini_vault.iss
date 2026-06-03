; Inno Setup script for Gemini Vault (Windows installer)
; ---------------------------------------------------------------------------
; Build the .exe first:   python build\build_windows.py   (produces dist\GeminiVault.exe)
; Then compile this script with Inno Setup (https://jrsoftware.org/isinfo.php):
;     "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer\gemini_vault.iss
; Output: installer\Output\GeminiVault-Setup.exe
;
; The app keeps user data (DB, config, logs) in %LOCALAPPDATA%\GeminiVault, so the
; install folder under Program Files can stay read-only and uninstalling does NOT
; delete the user's archive.

#define MyAppName "Gemini Vault"
#define MyAppVersion "1.1.0"
#define MyAppPublisher "Gemini Vault"
#define MyAppURL "https://github.com/vuhuutam459-max/gemini-vault"
#define MyAppExeName "GeminiVault.exe"

[Setup]
AppId={{8E5A2D31-6C4B-49E2-9C2A-1A2B3C4D5E6F}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
DefaultDirName={autopf}\Gemini Vault
DefaultGroupName=Gemini Vault
DisableProgramGroupPage=yes
OutputDir=Output
OutputBaseFilename=GeminiVault-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; A 64-bit, per-machine install (installer will prompt for elevation).
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
LicenseFile=..\LICENSE

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
; The one-file executable produced by PyInstaller.
Source: "..\dist\GeminiVault.exe"; DestDir: "{app}"; Flags: ignoreversion
; Handy docs alongside the app.
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\LICENSE";   DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\Gemini Vault";        Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall Gemini Vault"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Gemini Vault";  Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
; Offer to launch right after install.
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,Gemini Vault}"; Flags: nowait postinstall skipifsilent
