; Inno Setup script for ARGUS (compiled by CI on windows-latest)
#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#define AppName "ARGUS CFE"

[Setup]
AppId={{7E2C9D4A-ARGU-SCFE-TRAD-ING212000001}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=ARGUS
DefaultDirName={autopf}\ARGUS CFE
DefaultGroupName=ARGUS CFE
OutputDir=..\dist
OutputBaseFilename=ARGUS-CFE-setup
SetupIconFile=argus.ico
Compression=lzma2
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\ARGUS-CFE.exe
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
WizardStyle=modern
; an existing installation is reinstalled in place (same folder, no uninstall needed)
UsePreviousAppDir=yes
UsePreviousTasks=yes
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "sk"; MessagesFile: "compiler:Languages\Slovak.isl"
Name: "en"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Vytvoriť odkaz na ploche"
Name: "autostart"; Description: "Spustiť ARGUS CFE automaticky po štarte Windows"; Flags: unchecked

[InstallDelete]
; clean reinstall: remove the old program files first (user data in %APPDATA%\ARGUS-CFE is never touched)
Type: filesandordirs; Name: "{app}\_internal"; Check: CleanReinstall
Type: files; Name: "{app}\ARGUS-CFE.exe"; Check: CleanReinstall

[Files]
Source: "..\dist\ARGUS-CFE\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\ARGUS CFE"; Filename: "{app}\ARGUS-CFE.exe"
Name: "{autodesktop}\ARGUS CFE"; Filename: "{app}\ARGUS-CFE.exe"; Tasks: desktopicon
Name: "{userstartup}\ARGUS CFE"; Filename: "{app}\ARGUS-CFE.exe"; Tasks: autostart

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/F /T /IM ARGUS-CFE.exe"; Flags: runhidden; RunOnceId: "StopArgusCfe"

[Run]
Filename: "{app}\ARGUS-CFE.exe"; Description: "Spustiť ARGUS CFE"; Flags: nowait postinstall skipifsilent

[Code]
var
  ModePage: TInputOptionWizardPage;
  PrevVersion: String;

function UninstKey(): String;
begin
  // AppId is written with a doubled opening brace in [Setup]; Windows keys
  // use a single one. ExpandConstant turns the doubled brace into one.
  Result := ExpandConstant('Software\Microsoft\Windows\CurrentVersion\Uninstall\{#emit SetupSetting("AppId")}_is1');
end;

function FindPrevious(): Boolean;
begin
  PrevVersion := '';
  if not RegQueryStringValue(HKCU, UninstKey(), 'DisplayVersion', PrevVersion) then
    RegQueryStringValue(HKLM, UninstKey(), 'DisplayVersion', PrevVersion);
  if (PrevVersion = '') and (RegKeyExists(HKCU, UninstKey()) or RegKeyExists(HKLM, UninstKey())) then
    PrevVersion := '?';
  Result := PrevVersion <> '';
  if Result then
    Log('ARGUS: predchadzajuca instalacia najdena, verzia ' + PrevVersion)
  else
    Log('ARGUS: ziadna predchadzajuca instalacia (' + UninstKey() + ')');
end;

function InitializeSetup(): Boolean;
begin
  // always runs (also for silent installs), before the wizard exists
  FindPrevious();
  Result := True;
end;

procedure InitializeWizard();
begin
  if PrevVersion <> '' then
  begin
    ModePage := CreateInputOptionPage(wpWelcome,
      'ARGUS CFE je už nainštalovaný',
      'Nainštalovaná verzia: ' + PrevVersion + '   Nová verzia: {#AppVersion}',
      'Vyber, ako nainštalovať. Obchody, nastavenia, naučené modely a spárovanie ' +
      'mobilu (priečinok %APPDATA%\ARGUS-CFE) ostanú v oboch prípadoch zachované.',
      True, False);
    ModePage.Add('Preinštalovať načisto (odporúčané) - odstráni staré súbory programu a nainštaluje nové');
    ModePage.Add('Iba aktualizovať - prepíše súbory programu');
    ModePage.SelectedValueIndex := 0;
  end;
end;

function CleanReinstall(): Boolean;
begin
  Result := (ModePage <> nil) and (ModePage.SelectedValueIndex = 0);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Code: Integer;
begin
  { ARGUS (window + background engine) must not hold files open during install }
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /T /IM ARGUS-CFE.exe', '', SW_HIDE,
       ewWaitUntilTerminated, Code);
  Sleep(800);
  Result := '';
end;
