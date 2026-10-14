; Per-user Windows installer. User notes/configuration are outside {app} and retained.
#ifndef MyAppVersion
  #error MyAppVersion must be provided by the verified release build
#endif

[Setup]
AppId={{D426FCA6-8DE0-4A69-8E36-12004679E7D4}
AppName=MechaHub
AppVersion={#MyAppVersion}
AppPublisher=Mechaty17027
AppPublisherURL=https://github.com/Mechaty17027/flosty
AppSupportURL=https://github.com/Mechaty17027/flosty/issues
AppUpdatesURL=https://github.com/Mechaty17027/flosty/releases/latest
DefaultDirName={localappdata}\Programs\MechaHub
DefaultGroupName=MechaHub
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir=..\dist
OutputBaseFilename=MechaHubSetup
SetupIconFile=..\build\MechaHub.ico
UninstallDisplayIcon={app}\MechaHub.exe
Compression=lzma2/normal
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "ukrainian"; MessagesFile: "compiler:Languages\Ukrainian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "..\dist\MechaHub.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\MechaHub"; Filename: "{app}\MechaHub.exe"
Name: "{autodesktop}\MechaHub"; Filename: "{app}\MechaHub.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\MechaHub.exe"; Description: "{cm:LaunchProgram,MechaHub}"; Flags: nowait postinstall skipifsilent
