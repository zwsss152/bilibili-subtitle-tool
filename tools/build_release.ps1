param([string]$Python = 'python')
$ErrorActionPreference = 'Stop'
$releaseRoot = Split-Path $PSScriptRoot -Parent
Push-Location $releaseRoot
try {
    $releaseVersion = (& $Python -c 'from src.version import VERSION; print(VERSION)').Trim()
    if ($LASTEXITCODE -ne 0 -or $releaseVersion -notmatch '^\d+\.\d+\.\d+$') { throw 'Invalid application version' }
    & $Python -m pip check
    if ($LASTEXITCODE -ne 0) { throw 'Build environment dependencies are inconsistent' }
    & $Python -B -m unittest discover -s tests
    if ($LASTEXITCODE -ne 0) { throw 'Unit tests failed' }
    & $Python -m PyInstaller --noconfirm installer\app.spec
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed' }
    & (Join-Path $PSScriptRoot 'prepare_build.ps1')
    & (Join-Path $PSScriptRoot 'test_packaged.ps1') -Exe 'dist\BilibiliSubtitleTool\BilibiliSubtitleTool.exe' -Reports 'release\checks\bundle' -SkipBrowser
    & '.\.build-tools\InnoSetup\ISCC.exe' ('/DAppVersion=' + $releaseVersion) 'installer\setup.iss'
    if ($LASTEXITCODE -ne 0) { throw 'Installer compilation failed' }
    $releaseInstaller = Join-Path $releaseRoot "release\BilibiliSubtitleTool-Setup-$releaseVersion-x64.exe"
    $releaseHash = (Get-FileHash -LiteralPath $releaseInstaller -Algorithm SHA256).Hash.ToLowerInvariant()
    "$releaseHash  $([System.IO.Path]::GetFileName($releaseInstaller))" |
        Set-Content -LiteralPath ($releaseInstaller + '.sha256') -Encoding ascii
    Copy-Item -LiteralPath 'installer\redist\download-manifest.json' -Destination 'release\download-manifest.json'
    Write-Output "Installer ready: $releaseInstaller"
} finally {
    Pop-Location
}
