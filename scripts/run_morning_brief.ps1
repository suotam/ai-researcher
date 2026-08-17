# Morning brief runner for Task Scheduler.
# - skips if today's brief already exists (multiple logons per day)
# - starts llama-server if not running, waits for /health
# - runs the researcher (falls back to --no-llm automatically if server never comes up)
# - shows a Windows toast AND a persistent corner popup (scripts\show_popup.ps1)
#   with the Executive Summary; clicking either opens the brief
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
# Router mode: models are defined in config\llama-models.ini and loaded on
# demand by name; --models-max 1 because only one fits the 8 GB GPU at a time.
$LlamaPreset = Join-Path $ProjectRoot 'config\llama-models.ini'
$LlamaArgs   = "--models-preset `"$LlamaPreset`" --models-max 1 --host 127.0.0.1 --port 8080"
$HealthUrl   = 'http://127.0.0.1:8080/health'
$Today       = Get-Date -Format 'yyyy-MM-dd'
$BriefPath   = Join-Path $ProjectRoot "output\$Today-morning-brief.md"
$LogPath     = Join-Path $ProjectRoot 'logs\scheduler.log'
$PowerShellExe = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
$PopupScript = Join-Path $PSScriptRoot 'show_popup.ps1'

function Write-Log([string]$Message) {
    "$(Get-Date -Format s) $Message" | Add-Content -Encoding UTF8 $LogPath
}

function Stop-ProcessTree([int]$ParentId) {
    # The router spawns one child llama-server per loaded model; stopping
    # only the parent would leave the model (and its VRAM) behind.
    Get-CimInstance Win32_Process -Filter "ParentProcessId=$ParentId" |
        ForEach-Object { Stop-ProcessTree $_.ProcessId }
    Stop-Process -Id $ParentId -Force -ErrorAction SilentlyContinue
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

# Persistent always-on-top card in the bottom-right corner; stays until clicked.
# Runs detached so this script (and the scheduled task) can finish immediately.
function Show-Popup([string]$Title, [string]$Body, [string]$OpenPath, [int]$Slot = 0) {
    if (-not (Test-Path $PopupScript)) { Write-Log "Popup script not found: $PopupScript"; return }
    try {
        $bodyFile = Join-Path $ProjectRoot "logs\popup-body-$Slot.txt"
        [System.IO.File]::WriteAllText($bodyFile, $Body, (New-Object System.Text.UTF8Encoding($true)))
        $argList = "-NoProfile -STA -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$PopupScript`" " +
                   "-Title `"$Title`" -BodyFile `"$bodyFile`" -Slot $Slot"
        if ($OpenPath) { $argList += " -OpenPath `"$OpenPath`"" }
        Start-Process -FilePath $PowerShellExe -ArgumentList $argList -WindowStyle Hidden | Out-Null
        Write-Log "Popup shown: $Title"
    } catch {
        Write-Log "Popup failed: $_"
    }
}

function Notify([string]$Title, [string]$Body, [string]$OpenPath, [int]$Slot = 0) {
    Show-Toast $Title $Body $OpenPath
    Show-Popup $Title $Body $OpenPath $Slot
}

function Get-ExecutiveSummary([string]$Path, [int]$MaxLines = 4, [string]$Heading = 'Executive Summary') {
    if (-not (Test-Path $Path)) { return 'Brief soubor nenalezen.' }
    $lines = Get-Content -Encoding UTF8 $Path
    $collect = $false
    $picked = @()
    foreach ($line in $lines) {
        if ($line -match "^##\s+$Heading") { $collect = $true; continue }
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
    Notify "Morning Brief $Today" (Get-ExecutiveSummary $BriefPath) $BriefPath
    exit 0
}

if (-not (Test-Path $Python)) {
    Write-Log "Python venv not found at $Python"
    Notify 'AI Researcher - chyba' "Nenalezen Python venv: $Python"
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

# Notify right away (before the Sunday digest, which takes another ~30 min)
$briefOk = ($proc.ExitCode -eq 0 -and (Test-Path $BriefPath))
if ($briefOk) {
    Notify "Morning Brief $Today" (Get-ExecutiveSummary $BriefPath) $BriefPath 0
} else {
    Notify 'AI Researcher - chyba' "Run selhal (exit $($proc.ExitCode)). Detaily v logs\researcher.log"
}

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
        if ($wProc.ExitCode -eq 0 -and (Test-Path $WeeklyPath)) {
            Notify "Weekly Digest $Today" (Get-ExecutiveSummary $WeeklyPath 4 'The Week in Brief') $WeeklyPath 1
        } else {
            Notify 'AI Researcher - weekly digest chyba' "Digest selhal (exit $($wProc.ExitCode)). Detaily v logs\researcher.log" '' 1
        }
    }
}

# Stop the server only if we started it (default; use -KeepServer to keep it)
if ($serverProc -and -not $KeepServer) {
    Write-Log 'Stopping llama-server (started by this script).'
    Stop-ProcessTree $serverProc.Id
}

if ($briefOk) { exit 0 } else { exit 1 }
