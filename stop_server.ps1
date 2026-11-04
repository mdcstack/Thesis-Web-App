# BOOKLETLOG - Bulletproof Server Process Termination Script
# =========================================================

$ErrorActionPreference = 'SilentlyContinue'
$killedPids = [System.Collections.Generic.HashSet[int]]::new()

Write-Host "==============================================================" -ForegroundColor Yellow
Write-Host "  BOOKLETLOG - Terminating Background Servers" -ForegroundColor Yellow
Write-Host "==============================================================" -ForegroundColor Yellow
Write-Host ""

# 1. Kill by active listening or established ports (8000, 8080)
Write-Host "[1/3] Scanning network ports 8000 and 8080..." -ForegroundColor Cyan

# A. Via Get-NetTCPConnection
$tcpConns = Get-NetTCPConnection -LocalPort 8000, 8080 -ErrorAction SilentlyContinue
foreach ($conn in $tcpConns) {
    $pId = [int]$conn.OwningProcess
    if ($pId -gt 4 -and -not $killedPids.Contains($pId)) {
        $killedPids.Add($pId) | Out-Null
    }
}

# B. Via netstat -ano fallback
$netstatLines = netstat -ano
foreach ($line in $netstatLines) {
    if ($line -match ':8000\s+' -or $line -match ':8080\s+') {
        $tokens = ($line.Trim() -split '\s+')
        if ($tokens.Count -ge 5) {
            $lastToken = $tokens[-1]
            if ($lastToken -match '^\d+$') {
                $pId = [int]$lastToken
                if ($pId -gt 4 -and -not $killedPids.Contains($pId)) {
                    $killedPids.Add($pId) | Out-Null
                }
            }
        }
    }
}

# 2. Kill by process commandline pattern
Write-Host "[2/3] Scanning Python process command lines..." -ForegroundColor Cyan
$procs = Get-CimInstance Win32_Process | Where-Object {
    $_.Name -like 'python*' -and (
        $_.CommandLine -like '*run_servers*' -or
        $_.CommandLine -like '*app.main:app*' -or
        $_.CommandLine -like '*launcher.py*' -or
        $_.CommandLine -like '*multiprocessing.spawn*' -or
        $_.CommandLine -like '*spawn_main*'
    )
}

foreach ($p in $procs) {
    $pId = [int]$p.ProcessId
    if ($pId -gt 4 -and -not $killedPids.Contains($pId)) {
        $killedPids.Add($pId) | Out-Null
    }
}

# 3. Read server.pid if it exists
$pidFilePath = Join-Path $PSScriptRoot "server.pid"
if (Test-Path $pidFilePath) {
    try {
        $savedPids = Get-Content $pidFilePath -ErrorAction SilentlyContinue
        foreach ($savedPid in $savedPids) {
            if ($savedPid -match '^\d+$') {
                $pId = [int]$savedPid
                if ($pId -gt 4 -and -not $killedPids.Contains($pId)) {
                    $killedPids.Add($pId) | Out-Null
                }
            }
        }
        Remove-Item -Path $pidFilePath -Force -ErrorAction SilentlyContinue
    } catch {}
}

# 4. Terminate all discovered process trees
if ($killedPids.Count -gt 0) {
    Write-Host "[3/3] Terminating $($killedPids.Count) server process tree(s)..." -ForegroundColor Cyan
    foreach ($pidToKill in $killedPids) {
        Write-Host "  -> Terminating process tree for PID $pidToKill..." -ForegroundColor Yellow
        # Use taskkill /T /F to guarantee child process trees are also killed
        & taskkill /F /T /PID $pidToKill 2>$null | Out-Null
        Stop-Process -Id $pidToKill -Force -ErrorAction SilentlyContinue
    }
} else {
    Write-Host "[3/3] No active BOOKLETLOG processes were running." -ForegroundColor Green
}

# Final verification
Start-Sleep -Milliseconds 600
$remainingPorts = netstat -ano | Where-Object { $_ -match ':8000\s+' -or $_ -match ':8080\s+' }

Write-Host ""
if (-not $remainingPorts) {
    Write-Host "[SUCCESS] All BOOKLETLOG servers have been completely stopped." -ForegroundColor Green
    Write-Host "Ports 8000 (HTTPS) and 8080 (HTTP) are now released." -ForegroundColor Green
} else {
    Write-Host "[NOTICE] Some connections are in closing wait states; processes were terminated." -ForegroundColor Yellow
}
Write-Host ""

