; Comfy Planner installer (Inno Setup 6). Build it with scripts\build_release.ps1, which stages the program
; and passes AppVersion and StageDir; do not run this file by hand without them.
;
; Per-user install (no administrator rights) to %LOCALAPPDATA%\Programs\Comfy Planner.
;
; Upgrade: running a newer (or the same) installer over an older install replaces the program files and removes
; every program file of the old version first (RemoveOldProgramFiles), so nothing it had is left behind.
; These are never deleted or overwritten:
;   config\app.toml (created from config\app.example.toml only when it is missing)
;   data\    models\    tools\    and .venv (uv refreshes it on the next start)
; config\prompts is replaced too, but a copy of the old folder is made first in data\backups\prompts-<old version>\.
;
; Uninstall: removes the program and .venv, then asks whether to delete data\, models\, tools\ and config\app.toml
; as well. The default, and the answer in a silent uninstall, is to keep them.
;
; WebView2: the app window needs the Microsoft Edge WebView2 Runtime. When it is missing, Setup runs Microsoft's
; Evergreen Bootstrapper (carried inside this installer, extracted to a temporary folder and deleted again, never
; copied to the install folder). It needs internet access; if it fails, the install still finishes and the app opens
; in the browser instead.

#ifndef AppVersion
  #error Pass /DAppVersion=1.2.3 (scripts\build_release.ps1 does)
#endif
#ifndef StageDir
  #error Pass /DStageDir=<staged program folder> (scripts\build_release.ps1 does)
#endif
#ifndef WebView2Setup
  #error Pass /DWebView2Setup=<path of the signed bootstrapper exe> (scripts\build_release.ps1 does)
#endif

; Fixed, so every version upgrades the one before it. Never change it.
#define AppGuid "4614D8BE-BE51-4AC2-BAB8-AF9F6521B588"
#define AppName "Comfy Planner"
#define AppExe "Comfy Planner.exe"
; The registry key Setup writes its uninstall entry to (read to find the installed version).
#define UninstallKeyPath "Software\Microsoft\Windows\CurrentVersion\Uninstall\{" + AppGuid + "}_is1"

[Setup]
AppId={{{#AppGuid}}
AppName={#AppName}
AppVersion={#AppVersion}
VersionInfoVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\{#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
SetupIconFile=..\launcher\icon.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
OutputBaseFilename=ComfyPlanner-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; A running copy is asked to quit in the code below; this is the fallback for anything else that holds a file.
CloseApplications=yes
RestartApplications=no

[Tasks]
Name: "desktopicon"; Description: "Create a &Desktop shortcut"; Flags: unchecked

[Files]
; Everything that was staged, except what belongs to the user.
Source: "{#StageDir}\*"; DestDir: "{app}"; Excludes: "config\app.toml,data\*,models\*,tools\*,.venv\*"; Flags: recursesubdirs createallsubdirs ignoreversion
; The settings file starts as a copy of the example and is never touched again.
Source: "{#StageDir}\config\app.example.toml"; DestDir: "{app}\config"; DestName: "app.toml"; Flags: onlyifdoesntexist uninsneveruninstall
; The WebView2 bootstrapper stays inside the setup; the code below extracts it to {tmp} only if it is needed.
Source: "{#WebView2Setup}"; DestName: "MicrosoftEdgeWebview2Setup.exe"; Flags: dontcopy

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "Start {#AppName}"; WorkingDir: "{app}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Files the program and Python create after the install (caches, the environment).
Type: filesandordirs; Name: "{app}\.venv"
Type: filesandordirs; Name: "{app}\app"
Type: filesandordirs; Name: "{app}\ui"
Type: filesandordirs; Name: "{app}\scripts"
Type: filesandordirs; Name: "{app}\launcher"
Type: filesandordirs; Name: "{app}\config\prompts"
Type: filesandordirs; Name: "{app}\config\schemas"

[Code]
const
  // The launcher holds this mutex while it runs ("Comfy Planner.exe --quit" asks it to stop).
  AppMutex = 'Local\ComfyPlanner.Mutex';
  UninstallKey = '{#UninstallKeyPath}';
  // Client ID of the Evergreen WebView2 Runtime under ...\EdgeUpdate\Clients (Microsoft documents these keys).
  WebView2Clients = 'Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';

var
  // The version found in the install folder before this setup ran ('' for a new install).
  OldVersion: String;

// Asks a running Comfy Planner to quit and waits until it has (the server is stopped with it).
function CloseRunningApp(AppDir: String): Boolean;
var
  ResultCode, Waited: Integer;
begin
  Result := True;
  if not CheckForMutexes(AppMutex) then
    Exit;
  if FileExists(AppDir + '\{#AppExe}') then
    Exec(AppDir + '\{#AppExe}', '--quit', AppDir, SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Waited := 0;
  while CheckForMutexes(AppMutex) and (Waited < 60000) do
  begin
    Sleep(500);
    Waited := Waited + 500;
  end;
  Result := not CheckForMutexes(AppMutex);
end;

// The version that is installed in AppDir ('' if none): the VERSION file, else the uninstall entry.
function InstalledVersion(AppDir: String): String;
var
  Text: AnsiString;
begin
  Result := '';
  if LoadStringFromFile(AppDir + '\VERSION', Text) then
    Result := Trim(String(Text));
  if Result = '' then
    if not RegQueryStringValue(HKCU, UninstallKey, 'DisplayVersion', Result) then
      Result := '';
end;

// True if Installed is a higher version than the one being installed. False if either cannot be read.
function IsNewerThanThisSetup(Installed: String): Boolean;
var
  Old, This: Int64;
begin
  Result := False;
  if StrToVersion(Installed, Old) and StrToVersion('{#AppVersion}', This) then
    Result := ComparePackedVersion(Old, This) > 0;
end;

// Copies config\prompts to data\backups\prompts-<old version> (a number is added if that folder exists).
// Returns '' when done or when there is nothing to copy, else an error text.
function BackupPrompts(AppDir, OldVersion: String): String;
var
  Source, Target, Name: String;
  ResultCode, Number: Integer;
begin
  Result := '';
  Source := AppDir + '\config\prompts';
  if not DirExists(Source) then
    Exit;
  if OldVersion = '' then
    OldVersion := 'unknown';
  Name := 'prompts-' + OldVersion;
  Target := AppDir + '\data\backups\' + Name;
  Number := 1;
  while DirExists(Target) do
  begin
    Number := Number + 1;
    Target := AppDir + '\data\backups\' + Name + '-' + IntToStr(Number);
  end;
  if not ForceDirectories(AppDir + '\data\backups') then
  begin
    Result := 'Could not create ' + AppDir + '\data\backups to save your prompts.';
    Exit;
  end;
  // xcopy: all files and subfolders, creating the target folder. Exit code 0 is success, 1 means nothing to copy.
  if (not Exec(ExpandConstant('{sys}\xcopy.exe'), '"' + Source + '" "' + Target + '" /E /I /Y /Q /H', '', SW_HIDE, ewWaitUntilTerminated, ResultCode))
     or (ResultCode > 1) then
    Result := 'Could not save a copy of your prompts to ' + Target + '. Nothing was changed.';
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  AppDir, Old: String;
begin
  Result := '';
  AppDir := ExpandConstant('{app}');

  // A source checkout is not an install folder: the upgrade clean-up would delete its app and ui folders.
  if DirExists(AppDir + '\.git') then
  begin
    Result := AppDir + ' is a git checkout, not a Comfy Planner install. Choose another folder.';
    Exit;
  end;

  Old := InstalledVersion(AppDir);
  OldVersion := Old;
  if (Old <> '') and IsNewerThanThisSetup(Old) then
  begin
    if WizardSilent then
    begin
      Result := 'Comfy Planner ' + Old + ' is installed, which is newer than this setup (' + '{#AppVersion}' + '). Nothing was changed.';
      Exit;
    end;
    if MsgBox('Comfy Planner ' + Old + ' is already installed. This setup is the older version {#AppVersion}.' + #13#10#13#10
        + 'Installing it over the newer one can leave data that the older version does not understand. Install it anyway?',
        mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDNO then
    begin
      Result := 'Setup was stopped so that version ' + Old + ' stays installed.';
      Exit;
    end;
  end;

  if not CloseRunningApp(AppDir) then
  begin
    Result := '{#AppName} is still running. Quit it from its tray icon, then run Setup again.';
    Exit;
  end;

  Result := BackupPrompts(AppDir, Old);
end;

// True for what an upgrade keeps: the user's settings, data, models, tools and the Python environment.
function IsKept(Name: String): Boolean;
begin
  Name := Lowercase(Name);
  Result := (Name = 'data') or (Name = 'models') or (Name = 'tools') or (Name = '.venv') or (Name = 'config')
    or (Copy(Name, 1, 5) = 'unins');      // Setup's own uninstaller (unins000.exe/.dat), which it updates itself
end;

// Upgrade: removes every program file of the old version, so nothing it had and the new one does not is left behind.
// Kept: data\, models\, tools\, .venv\ and config\app.toml (the rest of config\ is removed). It only runs when the folder
// holds an install (a VERSION file or the uninstall entry), never on a new install into a folder that has other files.
procedure RemoveOldProgramFiles(AppDir: String);
var
  Found: TFindRec;
  Path: String;
begin
  if FindFirst(AppDir + '\*', Found) then
  try
    repeat
      if (Found.Name <> '.') and (Found.Name <> '..') and not IsKept(Found.Name) then
      begin
        Path := AppDir + '\' + Found.Name;
        if (Found.Attributes and FILE_ATTRIBUTE_DIRECTORY) <> 0 then
          DelTree(Path, True, True, True)
        else
          DeleteFile(Path);
      end;
    until not FindNext(Found);
  finally
    FindClose(Found);
  end;
  if FindFirst(AppDir + '\config\*', Found) then
  try
    repeat
      if (Found.Name <> '.') and (Found.Name <> '..') and (Lowercase(Found.Name) <> 'app.toml') then
      begin
        Path := AppDir + '\config\' + Found.Name;
        if (Found.Attributes and FILE_ATTRIBUTE_DIRECTORY) <> 0 then
          DelTree(Path, True, True, True)
        else
          DeleteFile(Path);
      end;
    until not FindNext(Found);
  finally
    FindClose(Found);
  end;
end;

// True if the key holds a "pv" (version) value that is set: not empty and not 0.0.0.0.
function HasRuntimeVersion(RootKey: Integer; SubKey: String): Boolean;
var
  Version: String;
begin
  Result := RegQueryStringValue(RootKey, SubKey, 'pv', Version) and (Version <> '') and (Version <> '0.0.0.0');
  if Result then
    Log('WebView2 Runtime found: ' + SubKey + ' pv=' + Version);
end;

// True if the WebView2 Runtime is installed (machine-wide, 32-bit view, or for this user).
function WebView2Installed: Boolean;
begin
  Result := HasRuntimeVersion(HKLM, 'SOFTWARE\WOW6432Node\' + WebView2Clients)
    or HasRuntimeVersion(HKLM, 'SOFTWARE\' + WebView2Clients)
    or HasRuntimeVersion(HKCU, 'Software\' + WebView2Clients);
end;

// Installs the WebView2 Runtime with the bootstrapper if it is missing. Setup runs without administrator rights, so
// this is a per-user install. A failure is only logged (and mentioned in an interactive setup): the app also works
// in a browser, and Setup finishes either way.
procedure InstallWebView2IfMissing;
var
  Bootstrapper: String;
  ResultCode: Integer;
  Failed: Boolean;
begin
  if WebView2Installed then
  begin
    Log('WebView2 Runtime is already installed, bootstrapper skipped.');
    Exit;
  end;
  Log('WebView2 Runtime not found, running the bootstrapper.');
  WizardForm.StatusLabel.Caption := 'Installing Microsoft Edge WebView2 Runtime...';
  Failed := True;
  try
    ExtractTemporaryFile('MicrosoftEdgeWebview2Setup.exe');
    Bootstrapper := ExpandConstant('{tmp}\MicrosoftEdgeWebview2Setup.exe');
    if Exec(Bootstrapper, '/silent /install', '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
    begin
      Log('WebView2 bootstrapper exit code: ' + IntToStr(ResultCode));
      // Exit code 0 is success; check the registry too, the bootstrapper can report other codes for a finished install.
      Failed := (ResultCode <> 0) and not WebView2Installed;
    end
    else
      Log('WebView2 bootstrapper could not be started: ' + SysErrorMessage(ResultCode));
  except
    Log('WebView2 bootstrapper failed: ' + GetExceptionMessage);
  end;
  // {tmp} is removed when Setup ends; delete the exe now anyway.
  DeleteFile(ExpandConstant('{tmp}\MicrosoftEdgeWebview2Setup.exe'));
  if Failed and not WizardSilent then
    MsgBox('The Microsoft Edge WebView2 Runtime could not be installed (it needs an internet connection). '
      + 'Comfy Planner is installed and also works in a browser at http://127.0.0.1:8765. '
      + 'To get the app window, install the runtime from microsoft.com/edge/webview2 and start Comfy Planner again.',
      mbInformation, MB_OK);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  // ssInstall comes after PrepareToInstall (the prompts are already backed up) and before the new files are copied.
  if (CurStep = ssInstall) and (OldVersion <> '') then
    RemoveOldProgramFiles(ExpandConstant('{app}'));
  // ssPostInstall: the program is installed; the optional "Start Comfy Planner" run entry comes after this.
  if CurStep = ssPostInstall then
    InstallWebView2IfMissing;
end;

function InitializeUninstall(): Boolean;
begin
  Result := CloseRunningApp(ExpandConstant('{app}'));
  if not Result then
    SuppressibleMsgBox('{#AppName} is still running. Quit it from its tray icon, then run the uninstaller again.', mbError, MB_OK, IDOK);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  AppDir: String;
begin
  if CurUninstallStep <> usPostUninstall then
    Exit;
  // Silent uninstall: keep everything. Otherwise ask, with "No" as the default button.
  if UninstallSilent then
    Exit;
  if MsgBox('Also delete your settings and data?' + #13#10#13#10
      + 'This removes the data folder (database, images, workflow copies, logs and prompt backups), the models and tools folders, '
      + 'and config\app.toml.' + #13#10#13#10
      + 'Choose No to keep them, for example to install again later.', mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
  begin
    AppDir := ExpandConstant('{app}');
    DelTree(AppDir + '\data', True, True, True);
    DelTree(AppDir + '\models', True, True, True);
    DelTree(AppDir + '\tools', True, True, True);
    DeleteFile(AppDir + '\config\app.toml');
    RemoveDir(AppDir + '\config');
    RemoveDir(AppDir);
  end;
end;
