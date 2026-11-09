param(
    [string]$DockerDesktopPath = (Join-Path $env:ProgramFiles 'Docker/Docker/Docker Desktop.exe'),
    [string]$StopFile,
    [ValidateRange(1, 120)][int]$WaitSeconds = 60
)
$ErrorActionPreference = 'Stop'

function Test-AlicePersonaPort {
    $aliceClient = [System.Net.Sockets.TcpClient]::new()
    try {
        $aliceConnect = $aliceClient.ConnectAsync('127.0.0.1', 15432)
        if (!$aliceConnect.Wait(500)) { return $false }
        return $aliceClient.Connected
    } catch {
        return $false
    } finally {
        $aliceClient.Dispose()
    }
}

if ($StopFile -and (Test-Path -LiteralPath $StopFile)) { return }
if (Test-AlicePersonaPort) { return }
if (!(Test-Path -LiteralPath $DockerDesktopPath)) {
    throw 'Local persona database unavailable and Docker Desktop executable not found'
}
if (!(Get-Process -Name 'Docker Desktop' -ErrorAction SilentlyContinue)) {
    Start-Process -FilePath $DockerDesktopPath -WindowStyle Hidden
}
$aliceDependencyDeadline = [DateTime]::UtcNow.AddSeconds($WaitSeconds)
while ([DateTime]::UtcNow -lt $aliceDependencyDeadline) {
    if ($StopFile -and (Test-Path -LiteralPath $StopFile)) { return }
    if (Test-AlicePersonaPort) { return }
    Start-Sleep -Seconds 1
}
throw 'Local persona database did not become reachable before the startup deadline'
