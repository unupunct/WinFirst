; WinFirst installer definition for Inno Setup 6.3+.
;
; Build the portable executable first, then compile this script:
;   powershell -ExecutionPolicy Bypass -File scripts\build_installer.ps1
;
; The installer deliberately does not delete user data on uninstall. WinFirst
; stores logs, reports, profiles and the database in %LOCALAPPDATA%\WinFirst.

#define MyAppName "WinFirst"
#define MyAppVersion "1.1.0"
#define MyAppPublisher "WinFirst Contributors"
#define MyAppExeName "WinFirst.exe"
#define MyAppId "{{6F1D7C2E-9A4B-4E3D-8C5A-2B7E1F0D4A93}"

[Setup]
AppId={#MyAppId}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\WinFirst
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
LicenseFile=..\LICENSE
OutputDir=Output
OutputBaseFilename=WinFirst-Setup-{#MyAppVersion}
SetupIconFile=..\src\winfirst\resources\icons\winfirst.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "..\dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion isreadme
Source: "..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Intentionally empty: never remove logs, reports, profiles or the database.
