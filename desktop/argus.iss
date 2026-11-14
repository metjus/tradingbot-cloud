; Inno Setup script for ARGUS
#define AppName "ARGUS"
#define AppVersion "0.12.0"

[Setup]
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=ARGUS
DefaultDirName={autopf}\ARGUS
DefaultGroupName=ARGUS
OutputDir=..\dist
OutputBaseFilename=ARGUS-setup
Compression=lzma2
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\ARGUS.exe

[Tasks]
Name: "autostart"; Description: "Spustiť ARGUS automaticky po štarte Windows"; Flags: unchecked

[Files]
Source: "..\dist\ARGUS\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\ARGUS"; Filename: "{app}\ARGUS.exe"
Name: "{autodesktop}\ARGUS"; Filename: "{app}\ARGUS.exe"
Name: "{userstartup}\ARGUS"; Filename: "{app}\ARGUS.exe"; Tasks: autostart

[Run]
Filename: "{app}\ARGUS.exe"; Description: "Spustiť ARGUS"; Flags: nowait postinstall skipifsilent
