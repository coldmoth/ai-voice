#ifndef AppVersion
  #error AppVersion must be supplied by build-win.ps1
#endif
#ifndef SourceDir
  #error SourceDir must be supplied by build-win.ps1
#endif
#ifndef OutputDir
  #error OutputDir must be supplied by build-win.ps1
#endif

[Setup]
AppId={{204B761F-C651-4C94-87CE-677049646A41}
AppName=AI Voice
AppVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\AI Voice
DefaultGroupName=AI Voice
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=commandline
ArchitecturesAllowed=x64os
ArchitecturesInstallIn64BitMode=x64os
MinVersion=10.0.17763
OutputDir={#OutputDir}
OutputBaseFilename=AI-Voice-Setup-{#AppVersion}
SetupIconFile={#SourceDir}\AppIcon.ico
UninstallDisplayIcon={app}\AppIcon.ico
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\AI Voice"; Filename: "{app}\python\pythonw.exe"; Parameters: "-X utf8 -m ai_voice.winshell"; WorkingDir: "{app}"; IconFilename: "{app}\AppIcon.ico"

[UninstallDelete]
Type: filesandordirs; Name: "{app}"

[Run]
Filename: "{app}\python\pythonw.exe"; Parameters: "-X utf8 -m ai_voice.winshell"; WorkingDir: "{app}"; Flags: nowait postinstall skipifsilent; Description: "Launch AI Voice"
; In-app update: the silent installer started by AI Voice passes /RELAUNCH=1.
Filename: "{app}\python\pythonw.exe"; Parameters: "-X utf8 -m ai_voice.winshell"; WorkingDir: "{app}"; Flags: nowait; Check: RelaunchRequested

; User data and the VC runtime live outside {app} and survive uninstall.
[CustomMessages]
WebView2Required=AI Voice requires Microsoft Edge WebView2 Evergreen Runtime. Download and install it from:%nhttps://developer.microsoft.com/microsoft-edge/webview2/%nThen run AI Voice setup again.

[Code]
function HasWebView2: Boolean;
var
  Version: String;
  Key: String;
begin
  // Microsoft documents HKLM's 32-bit view and HKCU for Evergreen Runtime.
  Key := 'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';
  Result := RegQueryStringValue(HKLM32, Key, 'pv', Version);
  Result := Result and (Version <> '') and (Version <> '0.0.0.0');
  if not Result then begin
    Result := RegQueryStringValue(HKCU32, Key, 'pv', Version);
    Result := Result and (Version <> '') and (Version <> '0.0.0.0');
  end;
end;

function RelaunchRequested: Boolean;
begin
  Result := ExpandConstant('{param:RELAUNCH|0}') = '1';
end;

function InitializeSetup: Boolean;
begin
  Result := HasWebView2;
  if not Result then begin
    Log(CustomMessage('WebView2Required'));
    if not WizardSilent then
      MsgBox(CustomMessage('WebView2Required'), mbError, MB_OK);
  end;
end;
