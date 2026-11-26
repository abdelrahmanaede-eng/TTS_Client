<#
.SYNOPSIS
    Closes any running TimeTracker instance launched from this project's dist\ folder.

.DESCRIPTION
    PyInstaller's final step replaces dist\TimeTracker\TimeTracker.exe. Windows
    refuses to delete/replace a file that a running process has locked, which
    surfaces as:

        PermissionError: [WinError 5] Access is denied: '...\dist\TimeTracker\TimeTracker.exe'

    This script terminates ONLY processes whose image path (or command line)
    points at this project's dist\TimeTracker\TimeTracker.exe. An installed
    copy elsewhere (e.g. under Program Files or %LOCALAPPDATA%) is never
    touched. It then waits until the file handle is actually released.

    Note: terminating the app is abrupt; any in-progress UI state is lost. The
    tracked time itself is already persisted to the SQLite database.

.EXIT CODES
    0 = no conflicting process, or the lock was released
    1 = the exe is still locked (caller should abort the build)
#>

$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$target = Join-Path $projectRoot 'dist\TimeTracker\TimeTracker.exe'

if (-not (Test-Path -LiteralPath $target)) {
    # Nothing has been built yet, so nothing can be locked.
    exit 0
}
$target = (Resolve-Path -LiteralPath $target).Path

function Get-ConflictingProcesses {
    $all = @(Get-CimInstance Win32_Process -Filter "Name = 'TimeTracker.exe'")
    return @($all | Where-Object {
        $imagePath = $_.ExecutablePath
        $commandLine = $_.CommandLine
        ($imagePath -and $imagePath -ieq $target) -or
        ($commandLine -and $commandLine -like "*$target*")
    })
}

$processes = Get-ConflictingProcesses
if ($processes.Count -gt 0) {
    Write-Host ("[INFO] Closing {0} running TimeTracker instance(s) launched from dist\ ..." -f $processes.Count)
    foreach ($proc in $processes) {
        try {
            Stop-Process -Id $proc.ProcessId -Force -ErrorAction Stop
        }
        catch [System.Management.Automation.ItemNotFoundException] {
            # Already exited between enumeration and stop - fine.
        }
        catch {
            Write-Warning ("Could not stop PID {0}: {1}" -f $proc.ProcessId, $_.Exception.Message)
        }
    }
}
else {
    Write-Host "[INFO] No running TimeTracker instance from dist\ found."
}

# Wait for both the processes AND the file handle to go away. Up to ~5 seconds.
for ($attempt = 0; $attempt -lt 10; $attempt++) {
    $remaining = Get-ConflictingProcesses
    foreach ($proc in $remaining) {
        Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
    }

    if ($remaining.Count -eq 0) {
        try {
            $handle = [System.IO.File]::Open(
                $target,
                [System.IO.FileMode]::Open,
                [System.IO.FileAccess]::ReadWrite,
                [System.IO.FileShare]::None
            )
            $handle.Dispose()
            Write-Host "[OK] dist\TimeTracker\TimeTracker.exe is unlocked and ready to rebuild."
            exit 0
        }
        catch {
            # Locked by something outside our process match (e.g. a file scanner).
        }
    }

    Start-Sleep -Milliseconds 500
}

Write-Host ""
Write-Host "[ERROR] dist\TimeTracker\TimeTracker.exe is still locked." -ForegroundColor Red
Write-Host "        Close TimeTracker from the system tray, pause any antivirus scanning" -ForegroundColor Red
Write-Host "        this folder, then run the build again." -ForegroundColor Red
exit 1
