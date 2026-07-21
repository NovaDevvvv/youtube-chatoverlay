#define AppName "YouTube Live Chat Overlay"
#define AppVersion "1.0.0"
#define AppPublisher "NovaDev"

[Setup]
AppId={{A73BE4A7-6E39-486B-86FA-22E9634B2E81}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={localappdata}\Programs\YouTubeChatOverlay
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
OutputDir={#ProjectRoot}\dist
OutputBaseFilename=Installer
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
SetupLogging=yes
UninstallDisplayName={#AppName}
VersionInfoVersion={#AppVersion}
VersionInfoCompany={#AppPublisher}
VersionInfoDescription={#AppName} Installer

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: checkedonce

[Files]
Source: "{#ProjectRoot}\src\*"; DestDir: "{app}\src"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#ProjectRoot}\requirements.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#ProjectRoot}\installer\launch.cmd"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#RuntimeRoot}\*"; DestDir: "{app}\runtime"; Flags: ignoreversion recursesubdirs createallsubdirs

[Run]
Filename: "{app}\launch.cmd"; Description: "Launch {#AppName}"; WorkingDir: "{app}"; Flags: nowait postinstall skipifsilent

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\launch.cmd"; WorkingDir: "{app}"; IconFilename: "{sys}\shell32.dll"; IconIndex: 167
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\launch.cmd"; WorkingDir: "{app}"; IconFilename: "{sys}\shell32.dll"; IconIndex: 167; Tasks: desktopicon

[UninstallDelete]
Type: filesandordirs; Name: "{app}"
