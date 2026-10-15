#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#define Root AddBackslash(SourcePath) + ".."

[Setup]
AppId={{575AB27D-D4AE-487D-AF0F-CA2B78E02DCB}
AppName=文字稿
AppVersion={#AppVersion}
AppPublisher=zwsss152
AppPublisherURL=https://github.com/zwsss152/bilibili-subtitle-tool
DefaultDirName={localappdata}\Programs\BilibiliSubtitleTool
DefaultGroupName=文字稿
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.19045
OutputDir={#Root}\release
OutputBaseFilename=BilibiliSubtitleTool-Setup-{#AppVersion}-x64
SetupIconFile={#Root}\assets\视频列表.ico
UninstallDisplayIcon={app}\BilibiliSubtitleTool.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
DisableProgramGroupPage=yes
SetupLogging=yes

[Languages]
Name: "chinesesimp"; MessagesFile: "{#Root}\installer\ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "快捷方式："

[Files]
Source: "{#Root}\dist\BilibiliSubtitleTool\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#Root}\installer\redist\MicrosoftEdgeWebView2RuntimeInstallerX64.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall; Check: NeedWebView2

[Icons]
Name: "{group}\文字稿"; Filename: "{app}\BilibiliSubtitleTool.exe"
Name: "{group}\文字稿 · 环境诊断"; Filename: "{app}\BilibiliSubtitleTool.exe"; Parameters: "--diagnostics"
Name: "{group}\卸载文字稿"; Filename: "{uninstallexe}"
Name: "{autodesktop}\文字稿"; Filename: "{app}\BilibiliSubtitleTool.exe"; Tasks: desktopicon

[Run]
Filename: "{tmp}\MicrosoftEdgeWebView2RuntimeInstallerX64.exe"; Parameters: "/silent /install"; StatusMsg: "正在安装抖音访问所需的浏览器组件…"; Flags: waituntilterminated; Check: NeedWebView2; AfterInstall: VerifyWebView2
Filename: "{app}\BilibiliSubtitleTool.exe"; Description: "启动文字稿"; Flags: nowait postinstall skipifsilent

[Code]
function HasWebView2(RootKey: Integer; Key: String): Boolean;
var V: String;
begin
  Result := RegQueryStringValue(RootKey, Key, 'pv', V) and (V <> '') and (V <> '0.0.0.0');
end;

function NeedWebView2: Boolean;
var K: String;
begin
  K := 'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';
  Result := not (HasWebView2(HKCU, K) or HasWebView2(HKLM32, K) or HasWebView2(HKLM64, K));
end;

procedure VerifyWebView2;
begin
  if NeedWebView2 then
    RaiseException('浏览器组件安装未完成。请重新运行安装程序，或安装 Microsoft WebView2 后再使用抖音访问。');
end;
