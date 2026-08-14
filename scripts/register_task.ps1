# Registers the morning-brief run as a Windows Task Scheduler task.
# Trigger: at logon of the current user, 2 minutes after login.
# Runs only when the user is logged on (interactive), so toast notifications work.
#
# Usage:      powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1
# Remove:     Unregister-ScheduledTask -TaskName 'AI Researcher Morning Brief' -Confirm:$false

$TaskName   = 'AI Researcher Morning Brief'
$ScriptPath = Join-Path $PSScriptRoot 'run_morning_brief.ps1'

# Full path — Task Scheduler may fail with 0x80070002 (file not found) when
# the action uses a bare 'powershell.exe'.
$PowerShellExe = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"

$action = New-ScheduledTaskAction -Execute $PowerShellExe `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$ScriptPath`"" `
    -WorkingDirectory (Split-Path -Parent $PSScriptRoot)

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$trigger.Delay = 'PT2M'   # give the desktop 2 minutes to settle after login

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Force

Write-Host "Task '$TaskName' registered. It will run 2 minutes after each logon."
Write-Host "Test it manually with: Start-ScheduledTask -TaskName '$TaskName'"
