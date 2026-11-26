; Inno Setup Script for TimeTracker
; Generates TimeTracker_Setup.exe for end-user installation and updates.
;
; Optional code signing:
;   build_installer.bat passes /DSIGNING_ENABLED and /Sbycert="<signtool command>"
;   when SIGN_CERT_PATH and SIGN_CERT_PASSWORD are set in the environment.
;   The same command signs the uninstaller (SignedUninstaller=yes).
;   Without those variables the build is unsigned and nothing is signed.

#define MyAppName "TimeTracker"
; build_installer.bat passes /DMyAppVersion=<config.APP_VERSION>; the literal
; below is only the fallback when the script is compiled manually.
#ifndef MyAppVersion
  #define MyAppVersion "1.3.2"
#endif
#define MyAppPublisher "Bussma"
#define MyAppExeName "TimeTracker.exe"

[Setup]
#ifdef SIGNING_ENABLED
SignTool=bycert
SignedUninstaller=yes
#endif
AppId={{B5C7D910-E22A-4F7C-9C61-54E1A616B4D2}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\{#MyAppName}
DisableProgramGroupPage=yes
OutputDir=dist
OutputBaseFilename=TimeTracker_Setup
SetupIconFile=assets\setup_icon.ico
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
CloseApplications=force
RestartApplications=no
UninstallDisplayIcon={app}\{#MyAppExeName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "autostart"; Description: "Run TimeTracker automatically when Windows starts"; GroupDescription: "Startup Options:"; Flags: checkedonce

[Files]
Source: "dist\TimeTracker\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon
Name: "{userstartup}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: autostart

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "{#MyAppName}"; ValueData: """{app}\{#MyAppExeName}"""; Flags: uninsdeletevalue; Tasks: autostart

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[Code]
function InitializeSetup(): Boolean;
var
  ResultCode: Integer;
begin
  // Terminate any running instance of TimeTracker before installing or updating
  Exec('taskkill.exe', '/F /IM TimeTracker.exe', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Result := True;
end;

function InitializeUninstall(): Boolean;
var
  ResultCode: Integer;
begin
  // Terminate running instance before uninstalling
  Exec('taskkill.exe', '/F /IM TimeTracker.exe', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Result := True;
end;
