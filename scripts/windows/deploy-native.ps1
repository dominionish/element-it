param(
    [Parameter(Mandatory = $true)]
    [string]$SourceDir,

    [Parameter(Mandatory = $true)]
    [string]$DeployDir,

    [string]$TaskName = "n8n-whisper-transcriber"
)

$ErrorActionPreference = "Stop"

function Assert-Command {
    param(
        [string]$Name,
        [string]$InstallHint
    )

    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "$Name is not installed or is not available in PATH. $InstallHint"
    }
}

function Invoke-Checked {
    param(
        [string]$FilePath,
        [string[]]$ArgumentList
    )

    & $FilePath @ArgumentList
    if ($LASTEXITCODE -ne 0) {
        throw "$FilePath failed with exit code $LASTEXITCODE"
    }
}

function Import-DotEnv {
    param([string]$Path)

    $values = @{}
    foreach ($line in Get-Content -LiteralPath $Path) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith("#")) {
            continue
        }

        $separator = $trimmed.IndexOf("=")
        if ($separator -lt 1) {
            continue
        }

        $name = $trimmed.Substring(0, $separator).Trim()
        $value = $trimmed.Substring($separator + 1).Trim().Trim('"').Trim("'")
        $values[$name] = $value
    }
    return $values
}

$currentIdentity = [Security.Principal.WindowsIdentity]::GetCurrent()
$currentPrincipal = [Security.Principal.WindowsPrincipal]::new($currentIdentity)
if (-not $currentPrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "The GitHub runner service account must have local administrator rights."
}

Assert-Command -Name "python" -InstallHint "Install Python 3.10 or 3.11."
Assert-Command -Name "ffmpeg" -InstallHint "Install FFmpeg and restart the runner service."
Assert-Command -Name "node" -InstallHint "Install Node.js 20.19 through 24."
Assert-Command -Name "npm.cmd" -InstallHint "Install Node.js with npm."

$pythonVersion = & python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
if ($pythonVersion -notin @("3.10", "3.11")) {
    throw "Python 3.10 or 3.11 is required; found $pythonVersion."
}

$nodeVersion = (& node --version).TrimStart("v").Split(".")
$nodeMajor = [int]$nodeVersion[0]
$nodeMinor = [int]$nodeVersion[1]
if ($nodeMajor -lt 20 -or $nodeMajor -gt 24 -or ($nodeMajor -eq 20 -and $nodeMinor -lt 19)) {
    throw "Node.js 20.19 through 24 is required; found $(& node --version)."
}

$SourceDir = [System.IO.Path]::GetFullPath($SourceDir)
$DeployDir = [System.IO.Path]::GetFullPath($DeployDir)
$sourcePrefix = $SourceDir.TrimEnd("\") + "\"
$deployPrefix = $DeployDir.TrimEnd("\") + "\"
if ($deployPrefix.StartsWith($sourcePrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "DeployDir must be outside SourceDir."
}

New-Item -ItemType Directory -Force -Path $DeployDir | Out-Null
foreach ($directory in @("data", "models", "n8n_data", "service_logs", "temp")) {
    New-Item -ItemType Directory -Force -Path (Join-Path $DeployDir $directory) | Out-Null
}

$envFile = Join-Path $DeployDir ".env"
if (-not (Test-Path -LiteralPath $envFile)) {
    Copy-Item -LiteralPath (Join-Path $SourceDir ".env.example") -Destination $envFile
    throw "Created $envFile. Fill in its production values, then re-run deployment."
}

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($task) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 3
}

foreach ($pidFile in @("transcriber.pid", "n8n.pid")) {
    $path = Join-Path $DeployDir $pidFile
    if (Test-Path -LiteralPath $path) {
        $processId = Get-Content -LiteralPath $path -ErrorAction SilentlyContinue
        if ($processId) {
            & taskkill.exe /PID $processId /T /F 2>$null | Out-Null
        }
        Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
    }
}

$robocopyArgs = @(
    (Join-Path $SourceDir "transcriber"),
    (Join-Path $DeployDir "transcriber"),
    "/MIR",
    "/R:2",
    "/W:2",
    "/NFL",
    "/NDL",
    "/NJH",
    "/NJS",
    "/NP",
    "/XD",
    "__pycache__",
    "/XF",
    "*.pyc"
)
& robocopy.exe @robocopyArgs
if ($LASTEXITCODE -ge 8) {
    throw "robocopy transcriber failed with exit code $LASTEXITCODE"
}

$scriptsDestination = Join-Path $DeployDir "scripts\windows"
New-Item -ItemType Directory -Force -Path $scriptsDestination | Out-Null
Copy-Item -LiteralPath (Join-Path $SourceDir "scripts\windows\start-server.ps1") -Destination $scriptsDestination -Force

$venvDir = Join-Path $DeployDir ".venv"
$python = Join-Path $venvDir "Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    Invoke-Checked -FilePath "python" -ArgumentList @("-m", "venv", $venvDir)
}

$requirements = Join-Path $DeployDir "transcriber\requirements.txt"
$requirementsHash = (Get-FileHash -LiteralPath $requirements -Algorithm SHA256).Hash
$requirementsStamp = Join-Path $venvDir ".requirements.sha256"
$installedHash = if (Test-Path -LiteralPath $requirementsStamp) {
    (Get-Content -LiteralPath $requirementsStamp -Raw).Trim()
} else {
    ""
}

if ($requirementsHash -ne $installedHash) {
    Invoke-Checked -FilePath $python -ArgumentList @("-m", "pip", "install", "--upgrade", "pip")
    Invoke-Checked -FilePath $python -ArgumentList @(
        "-m", "pip", "install",
        "torch==2.7.1+cu126",
        "--index-url", "https://download.pytorch.org/whl/cu126"
    )
    Invoke-Checked -FilePath $python -ArgumentList @("-m", "pip", "install", "-r", $requirements)
    Set-Content -LiteralPath $requirementsStamp -Value $requirementsHash
}

$settings = Import-DotEnv -Path $envFile
$n8nVersion = if ($settings.N8N_VERSION) { $settings.N8N_VERSION } else { "2.25.4" }
$n8nRuntime = Join-Path $DeployDir "n8n_runtime"
$n8nStamp = Join-Path $n8nRuntime ".version"
$installedN8nVersion = if (Test-Path -LiteralPath $n8nStamp) {
    (Get-Content -LiteralPath $n8nStamp -Raw).Trim()
} else {
    ""
}

if ($installedN8nVersion -ne $n8nVersion) {
    New-Item -ItemType Directory -Force -Path $n8nRuntime | Out-Null
    Invoke-Checked -FilePath "npm.cmd" -ArgumentList @(
        "install",
        "--prefix", $n8nRuntime,
        "--omit=dev",
        "n8n@$n8nVersion"
    )
    Set-Content -LiteralPath $n8nStamp -Value $n8nVersion
}

$launcher = Join-Path $DeployDir "scripts\windows\start-server.ps1"
$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$launcher`" -DeployDir `"$DeployDir`""
$trigger = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal `
    -UserId "SYSTEM" `
    -LogonType ServiceAccount `
    -RunLevel Highest
$taskSettings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -RestartCount 5 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $taskSettings `
    -Force | Out-Null

Start-ScheduledTask -TaskName $TaskName

$transcriberPort = if ($settings.TRANSCRIBER_HOST_PORT) { $settings.TRANSCRIBER_HOST_PORT } else { "7861" }
$n8nPort = if ($settings.N8N_HOST_PORT) { $settings.N8N_HOST_PORT } else { "5678" }
$transcriberHealthUrl = "http://127.0.0.1:$transcriberPort/health"
$n8nHealthUrl = "http://127.0.0.1:$n8nPort/"
$transcriberHealthy = $false
$n8nHealthy = $false
for ($attempt = 1; $attempt -le 60; $attempt++) {
    try {
        $response = Invoke-RestMethod -Uri $transcriberHealthUrl -TimeoutSec 5
        if ($response.status -eq "ok") {
            $transcriberHealthy = $true
        }
    }
    catch {}

    try {
        $response = Invoke-WebRequest -Uri $n8nHealthUrl -UseBasicParsing -TimeoutSec 5
        if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 500) {
            $n8nHealthy = $true
        }
    }
    catch {}

    if ($transcriberHealthy -and $n8nHealthy) {
        break
    }

    Start-Sleep -Seconds 5
}

if (-not $transcriberHealthy -or -not $n8nHealthy) {
    Get-Content -LiteralPath (Join-Path $DeployDir "service_logs\transcriber.stderr.log") -Tail 100 -ErrorAction SilentlyContinue
    Get-Content -LiteralPath (Join-Path $DeployDir "service_logs\n8n.stderr.log") -Tail 100 -ErrorAction SilentlyContinue
    throw "Services did not become healthy within 300 seconds. transcriber=$transcriberHealthy n8n=$n8nHealthy"
}

Write-Host "Native Windows deployment is healthy: $transcriberHealthUrl and $n8nHealthUrl"
