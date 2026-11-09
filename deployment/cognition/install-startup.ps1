param([switch]$StartNow)
$ErrorActionPreference = 'Stop'
$aliceStart = Join-Path $PSScriptRoot 'start-supervisor.ps1'
$aliceRoot = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$aliceExecutable = Join-Path $env:WINDIR 'System32/WindowsPowerShell/v1.0/powershell.exe'
$aliceArguments = '-NoProfile -WindowStyle Hidden -File "' + $aliceStart + '"'
$aliceMethod = 'scheduled_task_at_logon'
try {
    $aliceUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $aliceAction = New-ScheduledTaskAction -Execute $aliceExecutable -Argument $aliceArguments
    $aliceTrigger = New-ScheduledTaskTrigger -AtLogOn -User $aliceUser
    $alicePrincipal = New-ScheduledTaskPrincipal -UserId $aliceUser -LogonType Interactive -RunLevel Limited
    $aliceSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    Register-ScheduledTask -TaskName 'AliceCognition' -Action $aliceAction -Trigger $aliceTrigger -Principal $alicePrincipal -Settings $aliceSettings -Description 'Keep the authorized Alice WSL runtime alive; decision and budget remain inside Alice.' -Force | Out-Null
} catch {
    # Current-user login startup works without requesting administrator rights.
    $aliceMethod = 'current_user_startup_shortcut'
    $aliceShortcutPath = Join-Path ([Environment]::GetFolderPath('Startup')) 'AliceCognition.lnk'
    $aliceShell = New-Object -ComObject WScript.Shell
    $aliceShortcut = $aliceShell.CreateShortcut($aliceShortcutPath)
    $aliceShortcut.TargetPath = $aliceExecutable
    $aliceShortcut.Arguments = $aliceArguments
    $aliceShortcut.WindowStyle = 7
    $aliceShortcut.Save()
}
@{method=$aliceMethod; installed_at=(Get-Date).ToString('o'); launcher=$aliceStart} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $aliceRoot '.runtime/cognition-startup.json') -Encoding utf8
if ($StartNow) {
    if ($aliceMethod -eq 'scheduled_task_at_logon') {
        Start-ScheduledTask -TaskName 'AliceCognition'
    } else {
        Start-Process -FilePath $aliceExecutable -ArgumentList $aliceArguments -WindowStyle Hidden
    }
}
Write-Output ('Installed Alice login startup: ' + $aliceMethod)
