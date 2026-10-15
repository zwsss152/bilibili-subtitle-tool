param(
    [Parameter(Mandatory=$true)][string]$Exe,
    [Parameter(Mandatory=$true)][string]$Reports,
    [switch]$SkipBrowser
)
$ErrorActionPreference = 'Stop'
$packageExe = (Resolve-Path -LiteralPath $Exe).Path
$packageReports = [System.IO.Path]::GetFullPath($Reports)
New-Item -ItemType Directory -Path $packageReports -Force | Out-Null
foreach ($packageMode in @('self-test', 'smoke-test', 'browser-self-test')) {
    if ($SkipBrowser -and $packageMode -eq 'browser-self-test') { continue }
    $packageReport = Join-Path $packageReports ($packageMode + '.json')
    if (Test-Path -LiteralPath $packageReport) { Remove-Item -LiteralPath $packageReport }
    $packageStart = [System.Diagnostics.ProcessStartInfo]::new()
    $packageStart.FileName = $packageExe
    $packageStart.UseShellExecute = $false
    $packageStart.CreateNoWindow = $true
    $packageStart.ArgumentList.Add('--' + $packageMode)
    $packageStart.ArgumentList.Add($packageReport)
    $packageStart.Environment['PATH'] = "$env:WINDIR\System32;$env:WINDIR"
    $packageStart.Environment.Remove('PYTHONHOME') | Out-Null
    $packageStart.Environment.Remove('PYTHONPATH') | Out-Null
    $packageStart.Environment['BILIBILI_SUBTITLE_DATA_DIR'] = Join-Path $packageReports 'user-data'
    $packageProcess = [System.Diagnostics.Process]::Start($packageStart)
    if (-not $packageProcess.WaitForExit(60000)) {
        $packageProcess.Kill($true)
        throw "Packaged $packageMode timed out"
    }
    if (-not (Test-Path -LiteralPath $packageReport)) { throw "Missing report: $packageMode (exit $($packageProcess.ExitCode))" }
    $packageResult = Get-Content -LiteralPath $packageReport -Raw | ConvertFrom-Json
    if ($packageProcess.ExitCode -ne 0 -or -not $packageResult.ok -or -not $packageResult.frozen) {
        throw "Packaged $packageMode failed: $($packageResult.error)"
    }
    Write-Output "$packageMode passed (bundled Python $($packageResult.python), system Python removed from PATH)"
    $packageProcess.Dispose()
}
