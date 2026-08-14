# Morning brief runner for Task Scheduler.
# - skips if today's brief already exists (multiple logons per day)
# - starts llama-server if not running, waits for /health
# - runs the researcher (falls back to --no-llm automatically if server never comes up)
# - shows a Windows toast with the Executive Summary; clicking it opens the brief
#
# NOTE: ASCII-only on purpose (PowerShell 5.1 misreads BOM-less UTF-8 sources).
# Czech text from the brief itself is read as UTF-8 data at runtime, which is fine.

param(
    [switch]$Force,       # regenerate even if today's brief exists
    [switch]$KeepServer   # do not stop llama-server if this script started it
)

$ErrorActionPreference = 'Continue'

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python      = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$LlamaExe    = 'C:\llama-cuda\bin\llama-server.exe'
$LlamaArgs   = '-hf meta-models/Muse-Glimmer-30B-GGUF:Q4_K_M --jinja -c 16384 --host 127.0.0.1 --port 8080'
$HealthUrl   = 'http://127.0.0.1:8080/health'
$Today       = Get-Date -Format 'yyyy-MM-dd'
$BriefPath   = Join-Path $ProjectRoot "output\$Today-morning-brief.md"
$LogPath     = Join-Path $ProjectRoot 'logs\scheduler.log'

function Write-Log([string]$Message) {
    "$(Get-Date -Format s) $Message" | Add-Content -Encoding UTF8 $LogPath
}

function Test-LlamaHealth {
    try {
        (Invoke-WebRequest -UseBasicParsing -Uri $HealthUrl -TimeoutSec 3).StatusCode -eq 200
    } catch { $false }
}

function Show-Toast([string]$Title, [string]$Body, [string]$OpenPath) {
    try {
        [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
        [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
        $launchAttr = ''
        if ($OpenPath -and (Test-Path $OpenPath)) {
            $uri = ([System.Uri](Resolve-Path $OpenPath).Path).AbsoluteUri
            $launchAttr = " activationType=`"protocol`" launch=`"$uri`""
        }
        $t = [System.Security.SecurityElement]::Escape($Title)
        $b = [System.Security.SecurityElement]::Escape($Body)
        $xmlText = "<toast$launchAttr><visual><binding template=`"ToastGeneric`"><text>$t</text><text>$b</text></binding></visual></toast>"
        $doc = New-Object Windows.Data.Xml.Dom.XmlDocument
        $doc.LoadXml($xmlText)
        $appId = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
        [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show(
            (New-Object Windows.UI.Notifications.ToastNotification($doc)))
    } catch {
        Write-Log "Toast failed: $_"
    }
}

function Get-ExecutiveSummary([string]$Path, [int]$MaxLines = 4) {
    if (-not (Test-Path $Path)) { return 'Brief soubor nenalezen.' }
    $lines = Get-Content -Encoding UTF8 $Path
    $collect = $false
    $picked = @()
    foreach ($line in $lines) {
        if ($line -match '^##\s+Executive Summary') { $collect = $true; continue }
        if ($collect -and $line -match '^##\s+') { break }
        if ($collect -and $line.Trim()) {
            # strip [[12]](url) references and bold markers for toast readability
            $clean = $line -replace '\[\[(\d+)\]\]\([^)]*\)', '' -replace '\*\*', ''
            $clean = $clean.Trim()
            if ($clean.Length -gt 160) { $clean = $clean.Substring(0, 157) + '...' }
            $picked += $clean
            if ($picked.Count -ge $MaxLines) { break }
        }
    }
    if ($picked.Count -eq 0) { return 'Brief je hotovy (bez Executive Summary - pravdepodobne fallback rezim).' }
    return ($picked -join "`n")
}

# ---------------------------------------------------------------- main flow

Write-Log '=== Scheduler run started ==='

if ((Test-Path $BriefPath) -and -not $Force) {
    Write-Log "Brief for $Today already exists, skipping run."
    Show-Toast "Morning Brief $Today" (Get-ExecutiveSummary $BriefPath) $BriefPath
    exit 0
}

if (-not (Test-Path $Python)) {
    Write-Log "Python venv not found at $Python"
    Show-Toast 'AI Researcher - chyba' "Nenalezen Python venv: $Python"
    exit 1
}

# Start llama-server if it is not already running
$serverProc = $null
if (-not (Test-LlamaHealth)) {
    if (Test-Path $LlamaExe) {
        Write-Log 'Starting llama-server...'
        $serverProc = Start-Process -FilePath $LlamaExe -ArgumentList $LlamaArgs `
            -WindowStyle Minimized -PassThru
        $deadline = (Get-Date).AddMinutes(10)
        while ((Get-Date) -lt $deadline -and -not (Test-LlamaHealth)) {
            Start-Sleep -Seconds 5
        }
        if (Test-LlamaHealth) {
            Write-Log 'llama-server is healthy.'
        } else {
            Write-Log 'llama-server did not become healthy in 10 min - researcher will write a fallback brief.'
        }
    } else {
        Write-Log "llama-server exe not found at $LlamaExe - researcher will write a fallback brief."
    }
}

# Run the researcher (its own health check handles a dead LLM gracefully)
Write-Log 'Running researcher...'
$stdout = Join-Path $ProjectRoot 'logs\scheduler-run.out.log'
$stderr = Join-Path $ProjectRoot 'logs\scheduler-run.err.log'
$proc = Start-Process -FilePath $Python -ArgumentList '-m', 'src.main' `
    -WorkingDirectory $ProjectRoot -WindowStyle Hidden -Wait -PassThru `
    -RedirectStandardOutput $stdout -RedirectStandardError $stderr
Write-Log "Researcher finished with exit code $($proc.ExitCode)."

# On Sundays also produce the weekly digest (skip if it already exists)
if ((Get-Date).DayOfWeek -eq 'Sunday') {
    $WeeklyPath = Join-Path $ProjectRoot "output\$Today-weekly-digest.md"
    if (-not (Test-Path $WeeklyPath)) {
        Write-Log 'Sunday - running weekly digest...'
        $wOut = Join-Path $ProjectRoot 'logs\scheduler-weekly.out.log'
        $wErr = Join-Path $ProjectRoot 'logs\scheduler-weekly.err.log'
        $wProc = Start-Process -FilePath $Python -ArgumentList '-m', 'src.main', '--weekly' `
            -WorkingDirectory $ProjectRoot -WindowStyle Hidden -Wait -PassThru `
            -RedirectStandardOutput $wOut -RedirectStandardError $wErr
        Write-Log "Weekly digest finished with exit code $($wProc.ExitCode)."
    }
}

# Stop the server only if we started it (default; use -KeepServer to keep it)
if ($serverProc -and -not $KeepServer) {
    Write-Log 'Stopping llama-server (started by this script).'
    try { Stop-Process -Id $serverProc.Id -Force -ErrorAction Stop } catch {}
}

if ($proc.ExitCode -eq 0 -and (Test-Path $BriefPath)) {
    Show-Toast "Morning Brief $Today" (Get-ExecutiveSummary $BriefPath) $BriefPath
    exit 0
} else {
    Show-Toast 'AI Researcher - chyba' "Run selhal (exit $($proc.ExitCode)). Detaily v logs\researcher.log"
    exit 1
}
