; Inno Setup 6 script for Subzeo (run build.bat first)
[Setup]
AppName=Subzeo Download Manager
AppVersion=1.0.0
AppPublisher=Subzeo
DefaultDirName={autopf}\Subzeo
DefaultGroupName=Subzeo
OutputBaseFilename=Subzeo-Setup
SetupIconFile=assets\icon.ico
UninstallDisplayIcon={app}\Subzeo.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"
Name: "autostart"; Description: "Start Subzeo with Windows (minimized to tray)"

[Files]
Source: "dist\Subzeo\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion
Source: "extension\*"; DestDir: "{app}\browser-extension"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\Subzeo"; Filename: "{app}\Subzeo.exe"
Name: "{autodesktop}\Subzeo"; Filename: "{app}\Subzeo.exe"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; \
  ValueName: "Subzeo"; ValueData: """{app}\Subzeo.exe"" --minimized"; Tasks: autostart; \
  Flags: uninsdeletevalue

[Run]
Filename: "{app}\Subzeo.exe"; Description: "Launch Subzeo"; Flags: nowait postinstall skipifsilent
