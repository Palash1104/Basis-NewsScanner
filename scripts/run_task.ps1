<#
.SYNOPSIS
    Runs one newsdesk job for Windows Task Scheduler and logs its output.

.DESCRIPTION
    Task Scheduler cannot redirect a task's output, so this wrapper does it: each job appends
    to data\logs\tasks\<job>.log, with a timestamp header and the exit code. The job runs from
    the project's own virtualenv, so nothing depends on uv or PATH.

    Install the tasks with scripts\install_tasks.ps1; this script is not meant to be run by
    hand (though it is safe to: `newsdesk run` skips itself if another run holds the lock).
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("run", "digest", "score")]
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
}

$stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss zzz"
Add-Content -Path $log -Encoding utf8 -Value "=== $stamp  newsdesk $($arguments -join ' ') ==="

if (-not (Test-Path $exe)) {
    Add-Content -Path $log -Encoding utf8 -Value "newsdesk not found at $exe - run 'uv sync'"
    exit 1
}

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
    Add-Content -Path $log -Encoding utf8 -Value "=== exit $($process.ExitCode) ==="
    exit $process.ExitCode
}
finally {
    Remove-Item -Path $out, $err -Force -ErrorAction SilentlyContinue
}
