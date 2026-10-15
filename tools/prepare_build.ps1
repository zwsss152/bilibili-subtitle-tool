$ErrorActionPreference = 'Stop'
$buildRoot = Split-Path $PSScriptRoot -Parent
$buildTools = Join-Path $buildRoot '.build-tools'
$redistDir = Join-Path $buildRoot 'installer\redist'
New-Item -ItemType Directory -Path $buildTools, $redistDir -Force | Out-Null

function Get-VerifiedDownload([string]$Url, [string]$File, [string]$Publisher, [long]$MinimumSize) {
    if (-not (Test-Path -LiteralPath $File)) { Invoke-WebRequest -Uri $Url -OutFile $File }
    $downloadItem = Get-Item -LiteralPath $File
    $downloadSignature = Get-AuthenticodeSignature -LiteralPath $File
    if ($downloadItem.Length -lt $MinimumSize -or $downloadSignature.Status -ne 'Valid' -or
        $downloadSignature.SignerCertificate.Subject -notmatch $Publisher) {
        throw "Unexpected size or publisher signature: $File"
    }
}

$compilerInstaller = Join-Path $buildTools 'innosetup-6.7.3.exe'
Get-VerifiedDownload 'https://github.com/jrsoftware/issrc/releases/download/is-6_7_3/innosetup-6.7.3.exe' $compilerInstaller 'Pyrsys' 1MB
$compilerDir = Join-Path $buildTools 'InnoSetup'
if (-not (Test-Path -LiteralPath (Join-Path $compilerDir 'ISCC.exe'))) {
    $compilerProcess = Start-Process -FilePath $compilerInstaller -ArgumentList @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/CURRENTUSER', ('/DIR="' + $compilerDir + '"')) -WindowStyle Hidden -PassThru
    $compilerProcess.WaitForExit()
    if ($compilerProcess.ExitCode -ne 0) { throw "Inno installation failed: $($compilerProcess.ExitCode)" }
}
$webviewInstaller = Join-Path $redistDir 'MicrosoftEdgeWebView2RuntimeInstallerX64.exe'
Get-VerifiedDownload 'https://go.microsoft.com/fwlink/?linkid=2124701' $webviewInstaller 'Microsoft Corporation' 50MB
Get-FileHash -LiteralPath $compilerInstaller, $webviewInstaller -Algorithm SHA256 |
    Select-Object @{Name='File'; Expression={Split-Path $_.Path -Leaf}}, Hash |
    ConvertTo-Json | Set-Content -LiteralPath (Join-Path $redistDir 'download-manifest.json') -Encoding utf8
