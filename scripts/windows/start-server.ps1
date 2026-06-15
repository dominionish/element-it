param(
    [Parameter(Mandatory = $true)]
    [string]$DeployDir
)

$ErrorActionPreference = "Stop"

function Import-DotEnv {
    param([string]$Path)

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
        $value = $trimmed.Substring($separator + 1).Trim()
        if (
            $value.Length -ge 2 -and
            (($value.StartsWith('"') -and $value.EndsWith('"')) -or
             ($value.StartsWith("'") -and $value.EndsWith("'")))
        ) {
            $value = $value.Substring(1, $value.Length - 2)
        }

        [Environment]::SetEnvironmentVariable($name, $value, "Process")
    }
}

function Start-ManagedProcess {
    param(
        [string]$Name,
        [string]$FilePath,
        [string[]]$ArgumentList,
        [string]$WorkingDirectory,
        [string]$LogDir
    )

    $stdout = Join-Path $LogDir "$Name.stdout.log"
    $stderr = Join-Path $LogDir "$Name.stderr.log"
    return Start-Process `
        -FilePath $FilePath `
        -ArgumentList $ArgumentList `
        -WorkingDirectory $WorkingDirectory `
        -RedirectStandardOutput $stdout `
        -RedirectStandardError $stderr `
        -PassThru `
        -WindowStyle Hidden
}

$DeployDir = [System.IO.Path]::GetFullPath($DeployDir)
$envFile = Join-Path $DeployDir ".env"
if (-not (Test-Path -LiteralPath $envFile)) {
    throw "Environment file not found: $envFile"
}

Import-DotEnv -Path $envFile

$env:DATA_DIR = Join-Path $DeployDir "data"
$env:MODEL_DIR = Join-Path $DeployDir "models"
$env:WORK_DIR = Join-Path $DeployDir "temp"
$env:N8N_USER_FOLDER = Join-Path $DeployDir "n8n_data"
$env:N8N_PORT = if ($env:N8N_HOST_PORT) { $env:N8N_HOST_PORT } else { "5678" }
if (-not $env:N8N_LISTEN_ADDRESS) {
    $env:N8N_LISTEN_ADDRESS = "0.0.0.0"
}

$python = Join-Path $DeployDir ".venv\Scripts\python.exe"
$appDir = Join-Path $DeployDir "transcriber\app"
$n8nCmd = Join-Path $DeployDir "n8n_runtime\node_modules\.bin\n8n.cmd"
$logDir = Join-Path $DeployDir "service_logs"

foreach ($directory in @($env:DATA_DIR, $env:MODEL_DIR, $env:WORK_DIR, $env:N8N_USER_FOLDER, $logDir)) {
    New-Item -ItemType Directory -Force -Path $directory | Out-Null
}

if (-not (Test-Path -LiteralPath $python)) {
    throw "Python virtual environment not found: $python"
}
if (-not (Test-Path -LiteralPath $n8nCmd)) {
    throw "n8n executable not found: $n8nCmd"
}

$transcriberPort = if ($env:TRANSCRIBER_HOST_PORT) { $env:TRANSCRIBER_HOST_PORT } else { "7861" }
$supervisorLog = Join-Path $logDir "supervisor.log"
$processes = @{}

try {
    while ($true) {
        if (-not $processes.transcriber -or $processes.transcriber.HasExited) {
            Add-Content -LiteralPath $supervisorLog -Value "$(Get-Date -Format s) starting transcriber"
            $processes.transcriber = Start-ManagedProcess `
                -Name "transcriber" `
                -FilePath $python `
                -ArgumentList @("-m", "uvicorn", "api:app", "--host", "0.0.0.0", "--port", $transcriberPort) `
                -WorkingDirectory $appDir `
                -LogDir $logDir
            Set-Content -LiteralPath (Join-Path $DeployDir "transcriber.pid") -Value $processes.transcriber.Id
        }

        if (-not $processes.n8n -or $processes.n8n.HasExited) {
            Add-Content -LiteralPath $supervisorLog -Value "$(Get-Date -Format s) starting n8n"
            $processes.n8n = Start-ManagedProcess `
                -Name "n8n" `
                -FilePath "cmd.exe" `
                -ArgumentList @("/d", "/s", "/c", "`"$n8nCmd`" start") `
                -WorkingDirectory $DeployDir `
                -LogDir $logDir
            Set-Content -LiteralPath (Join-Path $DeployDir "n8n.pid") -Value $processes.n8n.Id
        }

        Start-Sleep -Seconds 5
    }
}
finally {
    foreach ($process in $processes.Values) {
        if ($process -and -not $process.HasExited) {
            & taskkill.exe /PID $process.Id /T /F 2>$null | Out-Null
        }
    }
    Remove-Item -LiteralPath (Join-Path $DeployDir "transcriber.pid") -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath (Join-Path $DeployDir "n8n.pid") -Force -ErrorAction SilentlyContinue
}
