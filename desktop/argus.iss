; Inno Setup script for ARGUS (compiled by CI on windows-latest)
#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#define AppName "ARGUS"

[Setup]
AppId={{7E2C9D4A-ARGU-S001-TRAD-ING212000000}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=ARGUS
DefaultDirName={autopf}\ARGUS
DefaultGroupName=ARGUS
OutputDir=..\dist
OutputBaseFilename=ARGUS-setup
SetupIconFile=argus.ico
Compression=lzma2
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\ARGUS.exe
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
WizardStyle=modern

[Languages]
Name: "sk"; MessagesFile: "compiler:Languages\Slovak.isl"
Name: "en"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Vytvoriť odkaz na ploche"
Name: "autostart"; Description: "Spustiť ARGUS automaticky po štarte Windows"; Flags: unchecked

[Files]
Source: "..\dist\ARGUS\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\ARGUS"; Filename: "{app}\ARGUS.exe"
Name: "{autodesktop}\ARGUS"; Filename: "{app}\ARGUS.exe"; Tasks: desktopicon
Name: "{userstartup}\ARGUS"; Filename: "{app}\ARGUS.exe"; Tasks: autostart

[Run]
Filename: "{app}\ARGUS.exe"; Description: "Spustiť ARGUS"; Flags: nowait postinstall skipifsilent
