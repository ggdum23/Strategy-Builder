; Strategy Builder per-user installer.
; Compile with ISCC and the /D defines the build script passes.
; The default market-data folder is a setting written to JSON. The installer
; does not create that folder and does not copy files into it.

#ifndef AppVersion
  #error AppVersion is required (numeric, from pyproject.toml).
#endif
#ifndef VersionInfoVersion
  #error VersionInfoVersion is required (four numeric parts).
#endif
#ifndef SetupName
  #error SetupName is required (filename version).
#endif
#ifndef PayloadDir
  #error PayloadDir is required (PyInstaller one-folder bundle).
#endif
#ifndef RepoRoot
  #error RepoRoot is required.
#endif
#ifndef OutputDir
  #error OutputDir is required.
#endif
#ifndef DefaultDataRoot
  #define DefaultDataRoot "C:\Users\Jeu\Desktop\Backtester"
#endif

#define AppName "Strategy Builder"
#define AppPublisher "Strategy Builder"

[Setup]
AppId={{B6E2C4A1-9F37-4D58-8C21-7A5E3B9D0F14}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
VersionInfoVersion={#VersionInfoVersion}
VersionInfoProductName={#AppName}
VersionInfoProductVersion={#VersionInfoVersion}
DefaultDirName={autopf}\Strategy Builder
DefaultGroupName=Strategy Builder
DisableProgramGroupPage=no
AllowNoIcons=yes
OutputDir={#OutputDir}
OutputBaseFilename=StrategyBuilder-Setup-{#SetupName}
SetupIconFile={#RepoRoot}\packaging\assets\strategy_builder.ico
UninstallDisplayIcon={app}\Strategy Builder.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=commandline dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
UsedUserAreasWarning=no
RestartIfNeededByRun=no
LicenseFile={#RepoRoot}\packaging\LICENSE.txt
InfoBeforeFile={#RepoRoot}\packaging\INSTALL_BEFORE.txt
CloseApplications=yes

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#PayloadDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#RepoRoot}\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#RepoRoot}\packaging\LICENSE.txt"; DestDir: "{app}"; DestName: "LICENSE.txt"; Flags: ignoreversion
Source: "{#RepoRoot}\docs\INSTALLER.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#RepoRoot}\packaging\INSTALL_BEFORE.txt"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\Strategy Builder"; Filename: "{app}\Strategy Builder.exe"; Comment: "Strategy Builder"
Name: "{group}\Strategy Builder Command Line"; Filename: "{app}\backtester.exe"; Comment: "Strategy Builder command line"
Name: "{group}\{cm:UninstallProgram,Strategy Builder}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Strategy Builder"; Filename: "{app}\Strategy Builder.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Strategy Builder.exe"; Description: "Launch Strategy Builder"; Flags: nowait postinstall skipifsilent

[Code]
var
  DataPage: TInputDirWizardPage;
  DataRootChosen: String;

function CmdParamValue(const Prefix: String): String;
var
  Index: Integer;
  Param: String;
begin
  Result := '';
  for Index := 1 to ParamCount do
  begin
    Param := ParamStr(Index);
    if CompareText(Copy(Param, 1, Length(Prefix)), Prefix) = 0 then
    begin
      Result := Copy(Param, Length(Prefix) + 1, MaxInt);
      Exit;
    end;
  end;
end;

function JsonEscape(const Value: String): String;
var
  Index: Integer;
  Ch: String;
begin
  Result := '';
  for Index := 1 to Length(Value) do
  begin
    Ch := Value[Index];
    if Ch = '\' then
      Result := Result + '\\'
    else if Ch = '"' then
      Result := Result + '\"'
    else
      Result := Result + Ch;
  end;
end;

function SelectedDataRoot: String;
var
  FromCmd: String;
begin
  FromCmd := CmdParamValue('/DATA=');
  if FromCmd <> '' then
    Result := FromCmd
  else if DataRootChosen <> '' then
    Result := DataRootChosen
  else
    Result := '{#DefaultDataRoot}';
end;

procedure InitializeWizard;
begin
  DataRootChosen := '{#DefaultDataRoot}';
  // A silent install has no wizard page. /DATA= and the default still apply.
  if WizardSilent then
    Exit;
  DataPage := CreateInputDirPage(
    wpSelectDir,
    'Market data folder',
    'Which folder already holds the filled 1-minute files?',
    'This path is saved in your settings. Setup does not copy, move, or unzip market data, and it does not create this folder.',
    False,
    ''
  );
  DataPage.Add('Filled-data folder');
  DataPage.Values[0] := DataRootChosen;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if DataPage = nil then
    Exit;
  if CurPageID = DataPage.ID then
  begin
    if DataPage.Values[0] <> '' then
      DataRootChosen := DataPage.Values[0]
    else
      DataRootChosen := '{#DefaultDataRoot}';
  end;
end;

procedure WriteTextFile(const Path, Contents: String);
var
  Lines: TArrayOfString;
begin
  ForceDirectories(ExtractFilePath(Path));
  SetArrayLength(Lines, 1);
  Lines[0] := Contents;
  // UTF-8 without a BOM. The application reads these files as UTF-8.
  if not SaveStringsToUTF8FileWithoutBOM(Path, Lines, False) then
    RaiseException('Could not write ' + Path);
end;

procedure WriteDefaults;
var
  Json: String;
begin
  Json := '{"schema":1,"data_root":"' + JsonEscape(SelectedDataRoot) + '"}' + #13#10;
  WriteTextFile(ExpandConstant('{app}\defaults.json'), Json);
  Log('Strategy Builder defaults data_root=' + SelectedDataRoot);
end;

procedure WriteInitialUserConfig;
var
  Root: String;
  ConfigPath: String;
  RunsPath: String;
  Json: String;
begin
  Root := ExpandConstant('{localappdata}\StrategyBuilder');
  ConfigPath := Root + '\config.json';
  RunsPath := Root + '\runs';
  ForceDirectories(RunsPath);
  if not FileExists(Root + '\README.txt') then
    WriteTextFile(
      Root + '\README.txt',
      'Strategy Builder keeps settings, the series registry, and backtest runs in this folder.' + #13#10 +
      'Uninstall leaves this folder in place unless you ask it to delete saved results.' + #13#10 +
      'Market data is not stored here. data_root in config.json points at the filled files.' + #13#10
    );
  if FileExists(ConfigPath) then
  begin
    Log('Strategy Builder left the existing user config in place');
    Exit;
  end;
  Json :=
    '{"schema":1' +
    ',"data_root":"' + JsonEscape(SelectedDataRoot) + '"' +
    ',"runs_dir":"' + JsonEscape(RunsPath) + '"' +
    ',"registry_path":"' + JsonEscape(Root + '\registry.json') + '"' +
    '}' + #13#10;
  WriteTextFile(ConfigPath, Json);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    WriteDefaults;
    // All-users installs must not write into one person's profile.
    // The application creates that profile's config on first launch.
    if not IsAdminInstallMode then
      WriteInitialUserConfig;
  end;
end;

function DeleteUserDataRequested: Boolean;
var
  Index: Integer;
  Param: String;
begin
  Result := False;
  for Index := 1 to ParamCount do
  begin
    Param := UpperCase(ParamStr(Index));
    if (Param = '/DELETEUSERDATA') or (Param = '/DELETEUSERDATA=1') or (Param = '/DELETEUSERDATA=YES') then
      Result := True;
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
  DoDelete: Boolean;
begin
  if CurUninstallStep <> usPostUninstall then
    Exit;
  DataDir := ExpandConstant('{localappdata}\StrategyBuilder');
  if not DirExists(DataDir) then
    Exit;
  if DeleteUserDataRequested then
    DoDelete := True
  else if UninstallSilent then
    DoDelete := False
  else
    DoDelete := SuppressibleMsgBox(
      'Also delete settings, the registry, and backtest runs in ' + DataDir + '?' + #13#10 + #13#10 +
      'Market data is not in this folder and will not be deleted.',
      mbConfirmation, MB_YESNO, IDNO) = IDYES;
  if DoDelete then
  begin
    Log('Strategy Builder deleting ' + DataDir);
    DelTree(DataDir, True, True, True);
  end
  else
    Log('Strategy Builder keeping ' + DataDir);
end;
