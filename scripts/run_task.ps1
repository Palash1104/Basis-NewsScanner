<#
.SYNOPSIS
    Runs one newsdesk job for Windows Task Scheduler and logs its output.

.DESCRIPTION
    Task Scheduler cannot redirect a task's output, so this wrapper does it: each job appends
    to data\logs\tasks\<job>.log, with a timestamp header and the exit code. The job runs from
    the project's own virtualenv, so nothing depends on uv or PATH.

    A job that exits non-zero is written to the log as FAILED and, if the Telegram
    credentials are set, reported in one short message (`newsdesk notify-failure`). One
    message per failed run, not per error: a run that finishes having recorded a broken feed
    is a normal run.

    Install the tasks with scripts\install_tasks.ps1. They call this through
    scripts\run_hidden.vbs, which is what keeps a console window from appearing. Running it
    by hand is safe (`newsdesk run` skips itself if another run holds the lock); a console
    window then is expected.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("run", "digest", "score", "serve")]
    [string]$Job
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$exe = Join-Path $root ".venv\Scripts\newsdesk.exe"
$logDir = Join-Path $root "data\logs\tasks"
$log = Join-Path $logDir "$Job.log"

if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }

# Keep one previous file rather than growing without limit; the app's own rotating log in
# data\logs\newsdesk.log holds the detail anyway.
if ((Test-Path $log) -and ((Get-Item $log).Length -gt 5MB)) {
    Move-Item -Path $log -Destination "$log.1" -Force
}

$arguments = switch ($Job) {
    "run" { @("run") }
    "digest" { @("digest", "--send") }
    "score" { @("score") }
    "serve" { @("serve") }
}

if (-not (Test-Path $exe)) {
    Add-Content -Path $log -Encoding utf8 -Value "newsdesk not found at $exe - run 'uv sync'"
    exit 1
}

function Send-FailureNotice {
    <#
        One Telegram message for one failed run. The app does the sending, so the bot token
        stays where it is already handled and never appears in a PowerShell string or a log
        line. It reads this log's tail for the message, so it runs after the failure has been
        written; it never throws, and whatever it prints lands in the same log.
    #>
    param([int]$Code)

    $notice = [System.IO.Path]::GetTempFileName()
    try {
        $process = Start-Process -FilePath $exe -WorkingDirectory $root -NoNewWindow -Wait -PassThru `
            -ArgumentList @("notify-failure", "--job", $Job, "--exit", "$Code", "--log", $log) `
            -RedirectStandardOutput $notice -RedirectStandardError "$notice.err"
        foreach ($file in @($notice, "$notice.err")) {
            if ((Test-Path $file) -and ((Get-Item $file).Length -gt 0)) {
                Get-Content -Path $file | Add-Content -Path $log -Encoding utf8
            }
        }
        if ($process.ExitCode -ne 0) {
            Add-Content -Path $log -Encoding utf8 -Value "notify-failure exited $($process.ExitCode)"
        }
    }
    catch {
        Add-Content -Path $log -Encoding utf8 -Value "could not run notify-failure: $_"
    }
    finally {
        Remove-Item -Path $notice, "$notice.err" -Force -ErrorAction SilentlyContinue
    }
}

# The web server runs until logoff, so its output has to stream into the log as it happens
# rather than being collected at exit like the batch jobs below. Start-Process redirection
# truncates, which is what we want here: one file per logon, not one per request forever.
if ($Job -eq "serve") {
    $process = Start-Process -FilePath $exe -ArgumentList $arguments -WorkingDirectory $root `
        -NoNewWindow -Wait -PassThru -RedirectStandardOutput $log -RedirectStandardError "$log.err"
    if ($process.ExitCode -ne 0) {
        Add-Content -Path $log -Encoding utf8 -Value "=== FAILED, exit $($process.ExitCode) ==="
        Send-FailureNotice -Code $process.ExitCode
    }
    exit $process.ExitCode
}

$stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss zzz"
Add-Content -Path $log -Encoding utf8 -Value "=== $stamp  newsdesk $($arguments -join ' ') ==="

# Start-Process with separate redirect files: piping a native command's output inside
# PowerShell 5.1 wraps stderr lines in error records and can fail the whole task.
$out = [System.IO.Path]::GetTempFileName()
$err = [System.IO.Path]::GetTempFileName()
try {
    $process = Start-Process -FilePath $exe -ArgumentList $arguments -WorkingDirectory $root `
        -NoNewWindow -Wait -PassThru -RedirectStandardOutput $out -RedirectStandardError $err
    foreach ($file in @($out, $err)) {
        if ((Get-Item $file).Length -gt 0) {
            Get-Content -Path $file | Add-Content -Path $log -Encoding utf8
        }
    }
    $code = $process.ExitCode
    if ($code -eq 0) {
        Add-Content -Path $log -Encoding utf8 -Value "=== exit 0 ==="
    }
    else {
        Add-Content -Path $log -Encoding utf8 -Value "=== FAILED, exit $code ==="
        Send-FailureNotice -Code $code
    }
    exit $code
}
finally {
    Remove-Item -Path $out, $err -Force -ErrorAction SilentlyContinue
}
