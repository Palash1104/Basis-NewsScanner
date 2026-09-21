<#
.SYNOPSIS
    Installs (or removes) the Windows Task Scheduler tasks that keep Newsdesk running.

.DESCRIPTION
    Three tasks under the \Newsdesk\ folder, all running scripts\run_task.ps1 from the
    project's virtualenv:

      Newsdesk-pipeline   newsdesk run           at every pipeline hour
      Newsdesk-digest     newsdesk digest --send at each digest time
      Newsdesk-score      newsdesk score         daily

    The times come from `newsdesk schedule-times`, which reads settings.yaml, so they cannot
    drift from the app's own schedule. Re-run this script after changing those settings.

    The tasks start after a reboot and login without a terminal, run as soon as possible
    after a missed start, and may wake the machine. They need no administrator rights: they
    run as the logged-on user, which is also the only account with the project's .env.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install_tasks.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install_tasks.ps1 -Remove
#>
[CmdletBinding()]
param(
    [switch]$Remove
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$runner = Join-Path $root "scripts\run_task.ps1"
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
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -WakeToRun `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
    -RestartCount 2 `
    -RestartInterval (New-TimeSpan -Minutes 10)

$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

function Register-NewsdeskTask {
    param([string]$Name, [string]$Job, [object[]]$Triggers, [string]$Description)
    $argument = "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass " +
                "-File `"$runner`" -Job $Job"
    $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $argument -WorkingDirectory $root
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

Write-Output ""
Get-ScheduledTask -TaskPath $taskPath |
    Select-Object TaskName, State, @{ Name = "NextRun"; Expression = { (Get-ScheduledTaskInfo $_).NextRunTime } } |
    Format-Table -AutoSize
Write-Output "Logs: data\logs\tasks\<job>.log and data\logs\newsdesk.log"
