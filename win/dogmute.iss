; DogMute's Windows installer (Inno Setup 6). win\build.ps1 compiles it after PyInstaller:
;   ISCC /DVersion=1.0.0 [/DCublasUrl=... /DCublasSha=... /DCudnnUrl=... /DCudnnSha=...] win\dogmute.iss
; Per-user: no admin rights, installs into %LOCALAPPDATA%\Programs\DogMute. Your settings live in %APPDATA%\DogMute
; and stay when you uninstall.
#ifndef Version
  #define Version "0.0.0"
#endif

[Setup]
AppId={{6E0C4F2B-8D1A-4B7E-9F3C-2A5D7B9E1C40}
AppName=DogMute
AppVersion={#Version}
AppVerName=DogMute {#Version}
AppPublisher=DogMute
AppPublisherURL=https://github.com/dogsaredogsnthatscool/dogmute
AppSupportURL=https://github.com/dogsaredogsnthatscool/dogmute#troubleshooting
DefaultDirName={localappdata}\Programs\DogMute
DisableProgramGroupPage=yes
DisableDirPage=auto
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir=..\dist
OutputBaseFilename=DogMute-Setup
SetupIconFile=..\build\icon.ico
UninstallDisplayIcon={app}\DogMute.exe
UninstallDisplayName=DogMute
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=no

[Tasks]
Name: startup; Description: "Start DogMute when I sign in (it waits quietly for Discord)"
Name: desktopicon; Description: "Put DogMute on the Desktop"; Flags: unchecked
Name: obs; Description: "Add the quickscope overlay to OBS Studio (it plays over your webcam scene on each mute)"; Check: HasObs
#ifdef CublasUrl
Name: gpu; Description: "Use my NVIDIA graphics card for speech: faster, and lets you pick the most accurate model (downloads about 1.2 GB)"; Check: HasNvidia
#endif

[Files]
Source: "..\dist\DogMute\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
Name: "{userprograms}\DogMute"; Filename: "{app}\DogMute.exe"; AppUserModelID: "DogMute"; Comment: "Auto local-mute for slurs and mic spam in your Discord calls"
Name: "{userdesktop}\DogMute"; Filename: "{app}\DogMute.exe"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "DogMute"; ValueData: """{app}\DogMute.exe"" --background"; Flags: uninsdeletevalue; Tasks: startup

[Run]
Filename: "{app}\DogMute.exe"; Parameters: "--obs-setup"; StatusMsg: "Adding the overlay to OBS..."; Flags: runhidden waituntilterminated; Tasks: obs
Filename: "{app}\DogMute.exe"; Parameters: "--obs-setup off"; Flags: runhidden waituntilterminated; Tasks: not obs; Check: HasObs
Filename: "{app}\DogMute.exe"; Description: "Open DogMute"; Flags: nowait postinstall skipifsilent; Check: not Updating
; an update opened by hand: no finish page, DogMute's window just opens again
Filename: "{app}\DogMute.exe"; Flags: nowait; Check: Updating and not WizardSilent
; the self-update (silent): DogMute goes back to the tray
Filename: "{app}\DogMute.exe"; Parameters: "--background"; Flags: nowait; Check: WizardSilent

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/f /t /im DogMute.exe"; Flags: runhidden; RunOnceId: "StopDogMute"
Filename: "{sys}\taskkill.exe"; Parameters: "/f /im dogmute-listen.exe"; Flags: runhidden; RunOnceId: "StopHelper"

[UninstallDelete]
Type: filesandordirs; Name: "{app}\cuda"
Type: filesandordirs; Name: "{app}\_internal"

[Code]
var
  DownloadPage: TDownloadWizardPage;
  GotCuda: Boolean;
  OldVersion: String;

const
  UninstKey = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{6E0C4F2B-8D1A-4B7E-9F3C-2A5D7B9E1C40}_is1';

var
  WasInstalled: Boolean;
  ClickTimer: LongWord;

function SetTimer(Wnd: LongWord; IDEvent, Elapse: LongWord; TimerFunc: LongWord): LongWord;
  external 'SetTimer@user32.dll stdcall';
function KillTimer(Wnd: LongWord; IDEvent: LongWord): BOOL;
  external 'KillTimer@user32.dll stdcall';

function InitializeSetup: Boolean;
begin
  // DogMute is already installed and someone opened the new DogMute-Setup.exe: no questions, it just updates
  WasInstalled := RegKeyExists(HKCU, UninstKey) or RegKeyExists(HKLM, UninstKey);
  if not RegQueryStringValue(HKCU, UninstKey, 'DisplayVersion', OldVersion) then
    RegQueryStringValue(HKLM, UninstKey, 'DisplayVersion', OldVersion);
  Result := True;
end;

function Updating: Boolean;
begin
  Result := WasInstalled;
end;

function HasObs: Boolean;
begin
  Result := DirExists(ExpandConstant('{userappdata}\obs-studio'))
    or FileExists(ExpandConstant('{commonpf64}\obs-studio\bin\64bit\obs64.exe'));
end;

function HaveCuda: Boolean;
begin
  // an update over a graphics card install: the files are already there, so they aren't downloaded again
  Result := DirExists(ExpandConstant('{app}\cuda\nvidia\cudnn\bin')) and DirExists(ExpandConstant('{app}\cuda\nvidia\cublas\bin'));
end;

function HasNvidia: Boolean;
begin
  Result := FileExists(ExpandConstant('{sys}\nvidia-smi.exe'))
    or FileExists(ExpandConstant('{commonpf64}\NVIDIA Corporation\NVSMI\nvidia-smi.exe'));
end;

procedure Stop;
var
  Code: Integer;
begin
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/f /t /im DogMute.exe', '', SW_HIDE, ewWaitUntilTerminated, Code);
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/f /im dogmute-listen.exe', '', SW_HIDE, ewWaitUntilTerminated, Code);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Stop;   // an update: the running DogMute holds its files
  Result := '';
end;

#ifdef CublasUrl
procedure InitializeWizard;
begin
  DownloadPage := CreateDownloadPage('Getting the graphics card''s speech files',
    'NVIDIA''s cuBLAS and cuDNN libraries, from PyPI. This happens once.', nil);
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if (CurPageID = wpReady) and WizardIsTaskSelected('gpu') and not HaveCuda then begin
    DownloadPage.Clear;
    DownloadPage.Add('{#CublasUrl}', 'cublas.whl', '{#CublasSha}');
    DownloadPage.Add('{#CudnnUrl}', 'cudnn.whl', '{#CudnnSha}');
    DownloadPage.Show;
    try
      try
        DownloadPage.Download;
        GotCuda := True;
      except
        if not DownloadPage.AbortedByUser then
          SuppressibleMsgBox('The graphics card files didn''t download: ' + GetExceptionMessage + #13#10#13#10 +
            'Go Back and untick the graphics card box to install without them (speech then runs on the processor).',
            mbError, MB_OK, IDOK);
        Result := False;
      end;
    finally
      DownloadPage.Hide;
    end;
  end;
end;

procedure Unpack(Wheel, Folder: String);
var
  Code: Integer;
begin
  // a wheel is a zip; Windows 10's own tar.exe unpacks it
  if not Exec(ExpandConstant('{sys}\tar.exe'), '-xf "' + ExpandConstant('{tmp}\') + Wheel + '" -C "' +
              ExpandConstant('{app}\cuda') + '" ' + Folder, '', SW_HIDE, ewWaitUntilTerminated, Code) or (Code <> 0) then
    Log('unpacking ' + Wheel + ' failed: ' + IntToStr(Code));
end;
#endif

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  // on an update the boxes ticked last time stay ticked (new ones start as on a new install), and there's no finish page
  Result := WasInstalled and ((PageID = wpSelectTasks) or (PageID = wpFinished));
end;

procedure PressInstall(Wnd: LongWord; Msg: LongWord; IDEvent: LongWord; Time: LongWord);
begin
  KillTimer(0, ClickTimer);
  if WizardForm.CurPageID = wpReady then
    WizardForm.NextButton.OnClick(WizardForm.NextButton);
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  // Inno Setup always shows one page before installing (Ready to Install). On an update, Install is pressed for you
  // a moment after it appears (not from inside this function: the page isn't fully shown yet).
  if WasInstalled and (CurPageID = wpReady) and not WizardSilent then
    ClickTimer := SetTimer(0, 0, 300, CreateCallback(@PressInstall));
  if WasInstalled and (CurPageID = wpInstalling) then begin
    WizardForm.PageNameLabel.Caption := 'Updating DogMute';
    if OldVersion <> '' then
      WizardForm.PageDescriptionLabel.Caption := OldVersion + ' to {#Version}. Your settings stay. DogMute opens again when it''s done.'
    else
      WizardForm.PageDescriptionLabel.Caption := 'To {#Version}. Your settings stay. DogMute opens again when it''s done.';
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
#ifdef CublasUrl
  if (CurStep = ssPostInstall) and WizardIsTaskSelected('gpu') and GotCuda then begin
    WizardForm.StatusLabel.Caption := 'Unpacking the graphics card''s speech files...';
    ForceDirectories(ExpandConstant('{app}\cuda'));
    Unpack('cublas.whl', 'nvidia/cublas/bin');
    Unpack('cudnn.whl', 'nvidia/cudnn/bin');
  end;
#endif
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
    Stop;
end;
