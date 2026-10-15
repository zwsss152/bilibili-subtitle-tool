param([Parameter(Mandatory=$true)][string]$Setup)
$ErrorActionPreference = 'Stop'
$installerSetup = (Resolve-Path -LiteralPath $Setup).Path
if (Test-Path -LiteralPath 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\{575AB27D-D4AE-487D-AF0F-CA2B78E02DCB}_is1') {
    throw 'An installed copy already exists. Run installer lifecycle tests in a separate Windows user or disposable VM.'
}
if (Test-Path -LiteralPath (Join-Path ([Environment]::GetFolderPath('Desktop')) '文字稿.lnk')) {
    throw 'A desktop shortcut already exists. Tests will not overwrite it.'
}
$installerRoot = Split-Path $PSScriptRoot -Parent
$installerRunId = [guid]::NewGuid().ToString('N')
$installerTestRoot = Join-Path $installerRoot ('tests\.artifacts\installer-' + $installerRunId)
$installerTarget = Join-Path $installerTestRoot '安装目录 with spaces'
$installerData = Join-Path $env:LOCALAPPDATA 'BilibiliSubtitleTool'
New-Item -ItemType Directory -Path $installerTestRoot, $installerData -Force | Out-Null
$installerSentinel = Join-Path $installerData ('.installer-check-' + $installerRunId)
[System.IO.File]::WriteAllText($installerSentinel, $installerRunId)
function Run-InstallerProcess([string]$File, [string[]]$Arguments) {
    $installProcess = Start-Process -FilePath $File -ArgumentList $Arguments -WindowStyle Hidden -PassThru
    if (-not $installProcess.WaitForExit(60000)) { throw 'Installer process exceeded 60 seconds; inspect setup log before continuing' }
    if ($installProcess.ExitCode -ne 0) { throw "Installer failed with exit $($installProcess.ExitCode)" }
    $installProcess.Dispose()
}
$installerEvidence = [ordered]@{ installed=$false; shortcut=$false; overinstall=$false; uninstalled=$false; data_preserved=$false; clean_vm=$false }
try {
    Run-InstallerProcess $installerSetup @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', ('/DIR="' + $installerTarget + '"'), ('/LOG="' + (Join-Path $installerTestRoot 'install.log') + '"'))
    $installerApp = Join-Path $installerTarget 'BilibiliSubtitleTool.exe'
    if (-not (Test-Path -LiteralPath $installerApp)) { throw 'Installed application missing' }
    $installerEvidence.installed = $true
    $installerShortcut = Join-Path ([Environment]::GetFolderPath('Desktop')) '文字稿.lnk'
    $shortcutShell = New-Object -ComObject WScript.Shell
    if ($shortcutShell.CreateShortcut($installerShortcut).TargetPath -ne $installerApp) { throw 'Desktop shortcut target mismatch' }
    $installerEvidence.shortcut = $true
    & (Join-Path $PSScriptRoot 'test_packaged.ps1') -Exe $installerApp -Reports (Join-Path $installerTestRoot 'checks')
    Run-InstallerProcess $installerSetup @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', ('/DIR="' + $installerTarget + '"'))
    if ([System.IO.File]::ReadAllText($installerSentinel) -ne $installerRunId) { throw 'Overinstall changed user data' }
    $installerEvidence.overinstall = $true
    Run-InstallerProcess (Join-Path $installerTarget 'unins000.exe') @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART')
    if (Test-Path -LiteralPath $installerApp) { throw 'Uninstall did not remove application' }
    $installerEvidence.uninstalled = $true
    if ([System.IO.File]::ReadAllText($installerSentinel) -ne $installerRunId) { throw 'Uninstall deleted user data' }
    $installerEvidence.data_preserved = $true
} finally {
    $installerEvidence | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $installerTestRoot 'installer-result.json') -Encoding utf8
    Remove-Item -LiteralPath $installerSentinel -ErrorAction SilentlyContinue
}
Write-Output "Installer checks passed. Evidence: $installerTestRoot (existing Windows host, not a clean VM)."
