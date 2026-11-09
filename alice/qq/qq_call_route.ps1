param(
  [Parameter(Mandatory=$true)][ValidateSet('arm','restore','watchdog')][string]$Mode,
  [Parameter(Mandatory=$true)][string]$PolicyPath,
  [Parameter(Mandatory=$true)][string]$StateDirectory,
  [int]$Seconds=240,
  [string]$Generation=''
)
$ErrorActionPreference='Stop'
$policy=(Resolve-Path -LiteralPath $PolicyPath).Path
$state=(Resolve-Path -LiteralPath $StateDirectory).Path
$leasePath=Join-Path $state 'qq-call-routing-lease.json'
function Invoke-Policy([string[]]$PolicyArgs) {
  $quoted=@($PolicyArgs | ForEach-Object { '"'+($_ -replace '"','')+'"' })
  $p=Start-Process -FilePath $policy -ArgumentList $quoted -WindowStyle Hidden -Wait -PassThru
  if($p.ExitCode -ne 0){throw 'app_audio_policy_failed'}
}
function Snapshot([string]$Name){
  $path=Join-Path $state $Name
  Invoke-Policy @('/SaveFileEncoding','3','/sjson',$path)
  return @(Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json)
}
function Defaults($Rows){
  return (($Rows | Where-Object { $_.Type -eq 'Device' -and ($_.'Default' -or $_.'Default Multimedia' -or $_.'Default Communications') } |
    ForEach-Object { $_.'Item ID'+'|'+$_.'Default'+'|'+$_.'Default Multimedia'+'|'+$_.'Default Communications' } | Sort-Object) -join "`n")
}
function Restore-QQ {
  # Application policy only. There is deliberately no /SetDefault command.
  Invoke-Policy @('/SetAppDefault','DefaultRenderDevice','all','QQ.exe')
  Invoke-Policy @('/SetAppDefault','DefaultCaptureDevice','all','QQ.exe')
  foreach($qqProcess in @(Get-Process -Name QQ -ErrorAction SilentlyContinue)){
    Invoke-Policy @('/SetAppDefault','DefaultRenderDevice','all',[string]$qqProcess.Id)
    Invoke-Policy @('/SetAppDefault','DefaultCaptureDevice','all',[string]$qqProcess.Id)
  }
}
if($Mode -eq 'watchdog'){
  $end=(Get-Date).AddSeconds([Math]::Min(300,[Math]::Max(30,$Seconds)))
  $seen=@{};$applied=@()
  while((Get-Date) -lt $end){
    if(-not(Test-Path -LiteralPath $leasePath)){exit}
    $current=Get-Content -LiteralPath $leasePath -Raw -Encoding UTF8 | ConvertFrom-Json
    if($current.generation -ne $Generation){exit}
    foreach($qqProcess in @(Get-Process -Name QQ -ErrorAction SilentlyContinue)){
      $pidText=[string]$qqProcess.Id
      if(-not $seen.ContainsKey($pidText)){
        Invoke-Policy @('/SetAppDefault','{0.0.0.00000000}.{d61007f2-c6c1-47db-80d1-675aa440f605}','all',$pidText)
        Invoke-Policy @('/SetAppDefault','{0.0.1.00000000}.{b099b2bd-7510-4b96-9c5a-86952c319132}','all',$pidText)
        $seen[$pidText]=$true
        $applied+=@{processId=$qqProcess.Id;at=(Get-Date).ToString('o')}
        ConvertTo-Json -InputObject $applied | Set-Content -LiteralPath (Join-Path $state 'qq-call-process-routing.json') -Encoding UTF8
      }
    }
    Start-Sleep -Milliseconds 250
  }
  if(Test-Path -LiteralPath $leasePath){
    $lease=Get-Content -LiteralPath $leasePath -Raw -Encoding UTF8 | ConvertFrom-Json
    if($lease.generation -eq $Generation){Restore-QQ;Remove-Item -LiteralPath $leasePath;Write-Output 'qq_app_routing_expired'}
  }
  exit
}
if($Mode -eq 'restore'){
  Restore-QQ
  if(Test-Path -LiteralPath $leasePath){Remove-Item -LiteralPath $leasePath}
  $null=Snapshot 'qq-app-routing-restored.json'
  Write-Output 'QQ follows the system devices again.'
  exit
}
if($Seconds -lt 30 -or $Seconds -gt 300){throw 'routing_window_must_be_30_to_300_seconds'}
if(-not(Get-Process -Name QQ -ErrorAction SilentlyContinue)){throw 'qq_not_running'}
$before=Snapshot 'qq-app-routing-before.json'
foreach($id in @('{0.0.0.00000000}.{d61007f2-c6c1-47db-80d1-675aa440f605}','{0.0.1.00000000}.{b099b2bd-7510-4b96-9c5a-86952c319132}')){
  if(-not($before | Where-Object {$_.'Item ID' -eq $id -and $_.'Device State' -eq 'Active'})){throw 'required_virtual_endpoint_missing'}
}
$generation=[Guid]::NewGuid().ToString('N')
@{generation=$generation;expires=(Get-Date).AddSeconds($Seconds).ToString('o');process='QQ.exe';changesGlobalDefaults=$false} |
  ConvertTo-Json | Set-Content -LiteralPath $leasePath -Encoding UTF8
# Watchdog exists before any mutation, including if this caller is terminated.
$watchArgs=@('-NoProfile','-ExecutionPolicy','Bypass','-File',('"'+$PSCommandPath+'"'),'-Mode','watchdog',
  '-PolicyPath',('"'+$policy+'"'),'-StateDirectory',('"'+$state+'"'),'-Seconds',$Seconds,'-Generation',$generation)
$null=Start-Process -FilePath powershell.exe -ArgumentList $watchArgs -WindowStyle Hidden -PassThru
try {
  Invoke-Policy @('/SetAppDefault','{0.0.0.00000000}.{d61007f2-c6c1-47db-80d1-675aa440f605}','all','QQ.exe')
  Invoke-Policy @('/SetAppDefault','{0.0.1.00000000}.{b099b2bd-7510-4b96-9c5a-86952c319132}','all','QQ.exe')
  $after=Snapshot 'qq-app-routing-after.json'
  if((Defaults $before) -cne (Defaults $after)){throw 'global_defaults_changed_during_app_policy_test'}
  Write-Output ('QQ-only routing armed for '+$Seconds+'s; global defaults verified unchanged. Actual call routing remains unverified.')
} catch {
  Restore-QQ
  if(Test-Path -LiteralPath $leasePath){Remove-Item -LiteralPath $leasePath}
  throw
}
