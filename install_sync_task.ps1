# Install HealthSync scheduled task (run as Administrator)
$python = (Get-Command python).Source
$projectDir = $PSScriptRoot
$script = Join-Path $projectDir "health_sync.py"
$taskName = "HealthSync_Mobvoi"

Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue

# pythonw.exe — no console window
$pythonw = $python -replace 'python\.exe$', 'pythonw.exe'
if (-not (Test-Path $pythonw)) { $pythonw = $python }

$action = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$script`" --days 14" -WorkingDirectory $projectDir

# Trigger 1: at logon, 2 min delay for WiFi
$triggerLogon = New-ScheduledTaskTrigger -AtLogOn
$triggerLogon.Delay = 'PT2M'

# Trigger 2: when PC idle
$triggerIdle = New-CimInstance -CimClass (
    Get-CimClass -Namespace Root/Microsoft/Windows/TaskScheduler -ClassName MSFT_TaskIdleTrigger
) -ClientOnly
$triggerIdle.Enabled = $true

# Settings: hidden, no console, NOT RunOnlyIfIdle (so logon trigger fires always)
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -DontStopOnIdleEnd `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5) `
    -Hidden

# Set idle settings separately (only affects idle trigger behavior)
$settings.IdleSettings.IdleDuration = 'PT10M'
$settings.IdleSettings.WaitTimeout  = 'PT8H'
$settings.IdleSettings.StopOnIdleEnd = $false

$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -RunLevel Highest -LogonType Interactive

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger @($triggerLogon, $triggerIdle) -Settings $settings -Principal $principal -Force

Write-Host ""
Write-Host "Task '$taskName' installed!" -ForegroundColor Green
Write-Host "  Uses: $pythonw (invisible, no console)"
Write-Host "  Trigger 1: at logon (2 min delay) — always"
Write-Host "  Trigger 2: after 10 min idle"
Write-Host "  Task is hidden in Task Scheduler"
