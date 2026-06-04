; Inno Setup script for Gemini Vault (Windows installer)
; ---------------------------------------------------------------------------
; Build the app first:   python build\build_windows.py   (produces dist\GeminiVault\)
; Then compile this script with Inno Setup (https://jrsoftware.org/isinfo.php):
;     "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer\gemini_vault.iss
; Output: installer\Output\GeminiVault-Setup.exe
;
; The app keeps user data (DB, config, logs) in %LOCALAPPDATA%\GeminiVault, so the
; install folder under Program Files can stay read-only and uninstalling does NOT
; delete the user's archive.

#define MyAppName "Gemini Vault"
; Version is injected from the git tag in CI via: iscc /dMyAppVersion=<tag> ...
; The fallback below keeps manual local builds working without that flag.
#ifndef MyAppVersion
#define MyAppVersion "1.0.0"
#endif
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
; Branded wizard artwork (HiDPI variants listed too).
SetupIconFile=..\build\icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
WizardImageFile=assets\wizard-large.bmp,assets\wizard-large-2x.bmp
WizardSmallImageFile=assets\wizard-small.bmp,assets\wizard-small-2x.bmp
; A 64-bit, per-machine install (installer will prompt for elevation).
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
LicenseFile=..\LICENSE

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

; ── Install types map to component presets (the user can still pick Custom) ──
[Types]
Name: "full";    Description: "Full installation (recommended)"
Name: "compact"; Description: "Viewer only"
Name: "custom";  Description: "Custom installation"; Flags: iscustom

; ── The checkboxes the user sees on the 'Select Components' page ──
[Components]
Name: "core"; Description: "Gemini Vault viewer — your chat archive"; \
    Types: full compact custom; Flags: fixed
Name: "ai";   Description: "Smart Librarian — local AI: auto-tags, summaries & ask-the-archive (needs Ollama)"; \
    Types: full
Name: "tools"; Description: "Management tools — scheduled backup helper"; \
    Types: full

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Components: core

[Files]
; Core: the one-DIR app (GeminiVault.exe + _internal\) plus docs. Always installed.
Source: "..\dist\GeminiVault\*"; DestDir: "{app}"; Components: core; \
    Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\README.md"; DestDir: "{app}"; Components: core; Flags: ignoreversion
Source: "..\LICENSE";   DestDir: "{app}"; Components: core; Flags: ignoreversion
; Tools: scheduled-backup helper (optional).
Source: "..\backup.bat"; DestDir: "{app}"; Components: tools; Flags: ignoreversion

[Icons]
Name: "{group}\Gemini Vault";           Filename: "{app}\{#MyAppExeName}"; Components: core
Name: "{group}\Backup Gemini Vault";    Filename: "{app}\backup.bat";      Components: tools
Name: "{group}\Uninstall Gemini Vault"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Gemini Vault";     Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon; Components: core

[Run]
; Offer to launch right after install.
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,Gemini Vault}"; Flags: nowait postinstall skipifsilent

[Code]
{ When the user does NOT select the AI module, drop a config flag so the app
  starts with the Smart Librarian disabled by default. We never overwrite an
  existing user config — only seed the opt-out when none is present. }
procedure CurStepChanged(CurStep: TSetupStep);
var
  DataDir, CfgFile: string;
begin
  if CurStep = ssPostInstall then
  begin
    if not IsComponentSelected('ai') then
    begin
      DataDir := ExpandConstant('{localappdata}\GeminiVault');
      ForceDirectories(DataDir);
      CfgFile := DataDir + '\librarian_config.json';
      if not FileExists(CfgFile) then
        SaveStringToFile(CfgFile, '{"ai_enabled": false}' + #13#10, False);
    end;
  end;
end;
