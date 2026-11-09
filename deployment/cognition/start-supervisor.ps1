$ErrorActionPreference = 'Stop'
$aliceRoot = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$aliceConfig = Join-Path $aliceRoot '.runtime/cognition-supervisor.json'
if (!(Test-Path -LiteralPath $aliceConfig)) { throw 'Cognition supervisor is not configured' }
$aliceSupervisorConfig = Get-Content -LiteralPath $aliceConfig -Raw | ConvertFrom-Json
if (Test-Path -LiteralPath $aliceSupervisorConfig.stop_file_windows) { exit 0 }
& (Join-Path $PSScriptRoot 'ensure-local-dependencies.ps1') -StopFile $aliceSupervisorConfig.stop_file_windows
if (Test-Path -LiteralPath $aliceSupervisorConfig.stop_file_windows) { exit 0 }
$aliceLauncher = (Get-Command py.exe).Source
$aliceSupervisor = Join-Path $PSScriptRoot 'supervise.py'
$aliceArguments = @('-3.12', $aliceSupervisor, '--config', $aliceConfig)
# Keep the registered task alive for the entire supervisor lifetime. A detached
# child can be cleaned up with a short-lived agent's process/job hierarchy.
& $aliceLauncher @aliceArguments
exit $LASTEXITCODE
