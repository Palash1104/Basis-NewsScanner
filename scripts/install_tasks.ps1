<#
.SYNOPSIS
    Installs (or removes) the Windows Task Scheduler tasks that keep Newsdesk running.

.DESCRIPTION
    Three tasks under the \Newsdesk\ folder, all running scripts\run_task.ps1 from the
    project's virtualenv, through scripts\run_hidden.vbs so that no console window appears:

      Newsdesk-pipeline   newsdesk run           at every pipeline hour
      Newsdesk-digest     newsdesk digest --send at each digest time
      Newsdesk-score      newsdesk score         daily
      Newsdesk-web        newsdesk serve         at logon, with -WithWeb

    The times come from `newsdesk schedule-times`, which reads settings.yaml, so they cannot
    drift from the app's own schedule. Re-run this script after changing those settings.

    The tasks start after a reboot and login without a terminal or any window at all, run as
    soon as possible after a missed start, and may wake the machine. They need no
    administrator rights: they run as the logged-on user, which is also the only account with
    the project's .env. A job that fails is logged as FAILED and, if the Telegram credentials
    are set, reported in one message.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install_tasks.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install_tasks.ps1 -WithWeb

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install_tasks.ps1 -Remove
#>
[CmdletBinding()]
param(
    [switch]$Remove,
    # Also start the web UI at logon, so http://127.0.0.1:8787 is there without a terminal.
    [switch]$WithWeb
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
# Every task runs wscript.exe on this launcher, which starts the PowerShell wrapper with its
# window hidden from the start. Task Scheduler's own "Hidden" setting does not do that for a
# console program - it only hides the task's own window - which is why black windows used to
# flash up on every wake (user, 2026-09-23).
$launcher = Join-Path $root "scripts\run_hidden.vbs"
$exe = Join-Path $root ".venv\Scripts\newsdesk.exe"
$taskPath = "\Newsdesk\"

$existing = Get-ScheduledTask -TaskPath $taskPath -ErrorAction SilentlyContinue
if ($existing) { $existing | Unregister-ScheduledTask -Confirm:$false }

if ($Remove) {
    Write-Output "Removed $(@($existing).Count) Newsdesk task(s)."
    exit 0
}

if (-not (Test-Path $exe)) { throw "newsdesk not found at $exe - run 'uv sync' first." }

$times = & $exe schedule-times | ConvertFrom-Json
Write-Output "Scheduling in $($times.timezone) (this machine: $((Get-TimeZone).Id))"

function New-DailyTrigger {
    param([int]$Hour, [int]$Minute)
    New-ScheduledTaskTrigger -Daily -At (Get-Date -Hour $Hour -Minute $Minute -Second 0)
}

# StartWhenAvailable is the "run as soon as possible after a missed start" box, WakeToRun the
# "wake the computer" one. IgnoreNew leaves a running job alone; the app's own lock covers the
# case of a task started by hand at the same time.
#
# No RestartCount any more: a failed job used to be retried twice, ten minutes apart, which on
# the pipeline meant three helpings of the same quota for a fault that is usually still there
# ten minutes later. A failure now sends one message and waits for the next slot (user,
# 2026-09-23). Catch-up runs for slots missed while the laptop slept are thinned in the app
# itself - `schedule.min_run_gap_minutes` - because Windows has no "only the most recent one".
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -WakeToRun `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2)

$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

function Register-NewsdeskTask {
    param([string]$Name, [string]$Job, [object[]]$Triggers, [string]$Description)
    $action = New-ScheduledTaskAction -Execute "wscript.exe" `
        -Argument "//nologo `"$launcher`" $Job" -WorkingDirectory $root
    Register-ScheduledTask -TaskPath $taskPath -TaskName $Name -Action $action -Trigger $Triggers `
        -Settings $settings -Principal $principal -Description $Description -Force | Out-Null
    Write-Output "  $Name"
}

$pipelineTriggers = @(foreach ($hour in $times.pipeline_hours) { New-DailyTrigger -Hour $hour -Minute 0 })
$digestTriggers = @(foreach ($time in $times.digest_times) {
    $parts = $time.Split(":")
    New-DailyTrigger -Hour ([int]$parts[0]) -Minute ([int]$parts[1])
})
$scoreParts = $times.score_time.Split(":")
$scoreTrigger = New-DailyTrigger -Hour ([int]$scoreParts[0]) -Minute ([int]$scoreParts[1])

Write-Output "Installed:"
Register-NewsdeskTask -Name "Newsdesk-pipeline" -Job "run" -Triggers $pipelineTriggers `
    -Description "Fetch, group, rank, summarize, extract events, apply the playbook, price the calls."
Register-NewsdeskTask -Name "Newsdesk-digest" -Job "digest" -Triggers $digestTriggers `
    -Description "Send the Telegram digest of stories summarized since the last one."
Register-NewsdeskTask -Name "Newsdesk-score" -Job "score" -Triggers $scoreTrigger `
    -Description "Judge every impact whose horizon is complete and update the track record."

if ($WithWeb) {
    # The server runs until logoff: no time limit, restart it if it dies, and only ever one.
    # It is not a WakeToRun job - waking a sleeping laptop to serve a page nobody asked for
    # would be silly - and it does not need to catch up a missed start.
    $webSettings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 1)
    $webTrigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
    # A minute's grace so the server starts after the machine has settled.
    $webTrigger.Delay = "PT1M"
    $action = New-ScheduledTaskAction -Execute "wscript.exe" `
        -Argument "//nologo `"$launcher`" serve" -WorkingDirectory $root
    Register-ScheduledTask -TaskPath $taskPath -TaskName "Newsdesk-web" -Action $action `
        -Trigger $webTrigger -Settings $webSettings -Principal $principal `
        -Description "Serve the BASIS web UI on http://127.0.0.1:8787 for as long as you are logged in." -Force | Out-Null
    Write-Output "  Newsdesk-web (at logon)"
}

Write-Output ""
Get-ScheduledTask -TaskPath $taskPath |
    Select-Object TaskName, State, @{ Name = "NextRun"; Expression = { (Get-ScheduledTaskInfo $_).NextRunTime } } |
    Format-Table -AutoSize
Write-Output "Logs: data\logs\tasks\<job>.log and data\logs\newsdesk.log"
