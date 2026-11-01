#define MyAppName "Jev 固定话术客服"
#define MyAppVersion "0.3.5"
#define MyAppExeName "JevReplyTool.exe"

[Setup]
AppId={{21F20212-C5BC-4FDB-87F4-0EA6D859475A}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
DefaultDirName={localappdata}\Programs\JevReplyTool
DefaultGroupName={#MyAppName}
PrivilegesRequired=lowest
OutputDir=..\release
OutputBaseFilename=JevReplyTool-Setup-0.3.5
Compression=lzma
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#MyAppExeName}

[Files]
Source: "..\release\audit-build\JevReplyTool\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "打开首次配置向导"; Flags: nowait postinstall skipifsilent
