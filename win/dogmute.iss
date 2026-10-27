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
#ifdef CublasUrl
Name: gpu; Description: "Use my NVIDIA graphics card for speech: catches more words (downloads about 1.2 GB)"; Check: HasNvidia
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
Filename: "{app}\DogMute.exe"; Description: "Open DogMute"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/f /t /im DogMute.exe"; Flags: runhidden; RunOnceId: "StopDogMute"
Filename: "{sys}\taskkill.exe"; Parameters: "/f /im dogmute-listen.exe"; Flags: runhidden; RunOnceId: "StopHelper"

[UninstallDelete]
Type: filesandordirs; Name: "{app}\cuda"
Type: filesandordirs; Name: "{app}\_internal"

[Code]
var
  DownloadPage: TDownloadWizardPage;

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
  if (CurPageID = wpReady) and WizardIsTaskSelected('gpu') then begin
    DownloadPage.Clear;
    DownloadPage.Add('{#CublasUrl}', 'cublas.whl', '{#CublasSha}');
    DownloadPage.Add('{#CudnnUrl}', 'cudnn.whl', '{#CudnnSha}');
    DownloadPage.Show;
    try
      try
        DownloadPage.Download;
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

procedure CurStepChanged(CurStep: TSetupStep);
begin
#ifdef CublasUrl
  if (CurStep = ssPostInstall) and WizardIsTaskSelected('gpu') then begin
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
