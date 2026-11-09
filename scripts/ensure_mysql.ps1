# Ensure the local MySQL/MariaDB server is running before 循迹（Trace） starts.
#
# Reads DATABASE_URL from .env and only acts when it points at a local MySQL
# (127.0.0.1 / localhost). If the server's port is already reachable it does
# nothing; otherwise it starts the Windows service, requesting elevation (UAC)
# when the current process is not an administrator.
#
# Exit codes:
#   0 = nothing to do, already running, or successfully started
#   2 = could not start the service
#   3 = service started but the port never became reachable
#
# All output is ASCII on purpose: Windows PowerShell 5.1 reads BOM-less .ps1
# files with the system ANSI code page, so non-ASCII here would turn to mojibake.

param(
    [string]$EnvFile = (Join-Path $PSScriptRoot '..\.env'),
    [int]$Port = 3306,
    [int]$WaitSeconds = 45
)

$ErrorActionPreference = 'Stop'

function Log([string]$m) { Write-Host "[mysql] $m" }

function Test-Port([int]$p) {
    try {
        $c = New-Object Net.Sockets.TcpClient
        $c.Connect('127.0.0.1', $p)
        $c.Close()
        return $true
    } catch {
        return $false
    }
}

# 1. Read DATABASE_URL from .env (last match wins).
$url = $null
if (Test-Path -LiteralPath $EnvFile) {
    foreach ($line in Get-Content -LiteralPath $EnvFile) {
        if ($line -match '^\s*DATABASE_URL\s*=\s*(.*)$') {
            $url = $Matches[1].Trim().Trim('"').Trim("'")
        }
    }
}

if (-not $url) { Log 'no DATABASE_URL configured - skip'; exit 0 }
if ($url -notmatch 'mysql' -or $url -notmatch '127\.0\.0\.1|localhost') {
    Log 'DATABASE_URL is not a local MySQL - skip'; exit 0
}

# 2. Already listening? Nothing to do.
if (Test-Port $Port) { Log "MySQL already running (port $Port reachable)"; exit 0 }

# 3. Locate the service.
$svc = @(Get-Service | Where-Object { $_.Name -match '(?i)mysql|maria' })
if ($svc.Count -eq 0) {
    Log 'no MySQL/MariaDB service found - skip (MySQL may not be installed as a service)'
    exit 0
}

$running = $svc | Where-Object { $_.Status -eq 'Running' } | Select-Object -First 1
$target = if ($running) { $running } else { $svc | Select-Object -First 1 }

if ($running) {
    Log ("service '{0}' is running; waiting for port {1}..." -f $target.Name, $Port)
} else {
    Log ("MySQL not running, starting service '{0}'..." -f $target.Name)
    try {
        Start-Service -Name $target.Name -ErrorAction Stop
    } catch {
        # Likely not elevated: the service DACL only lets Administrators start it.
        Log 'administrator rights required, requesting elevation (UAC prompt)...'
        try {
            $ps = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
            $inner = "Start-Service -Name '$($target.Name)' -ErrorAction Stop"
            $p = Start-Process -FilePath $ps -Verb RunAs -Wait -WindowStyle Hidden `
                -ArgumentList @('-NoProfile', '-Command', $inner) -PassThru
            if ($null -ne $p.ExitCode -and $p.ExitCode -ne 0) {
                throw "elevated Start-Service failed (exit $($p.ExitCode))"
            }
        } catch {
            Log ("could not start MySQL: {0}" -f $_.Exception.Message)
            Log ("start it manually from an elevated prompt:  net start {0}" -f $target.Name)
            exit 2
        }
    }
}

# 4. Wait for the port to accept connections.
$deadline = (Get-Date).AddSeconds($WaitSeconds)
while ((Get-Date) -lt $deadline) {
    if (Test-Port $Port) { Log "MySQL is up (port $Port reachable)"; exit 0 }
    Start-Sleep -Milliseconds 500
}
Log "service started but port $Port still not reachable (check my.ini port / firewall)"
exit 3
