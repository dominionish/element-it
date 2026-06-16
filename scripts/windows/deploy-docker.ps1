param(
    [Parameter(Mandatory = $true)]
    [string]$SourceDir,

    [string]$WslDistro = "Ubuntu",

    [string]$WslDeployDir = "/opt/n8n_whisper_transcriber",

    [Parameter(Mandatory = $true)]
    [string]$DeploySha,

    [Parameter(Mandatory = $true)]
    [string]$Repository,

    [Parameter(Mandatory = $true)]
    [string]$GhcrUsername,

    [Parameter(Mandatory = $true)]
    [string]$GhcrToken
)

$ErrorActionPreference = "Stop"

function Quote-Bash {
    param([string]$Value)
    return "'" + $Value.Replace("'", "'\''") + "'"
}

function New-RandomKey {
    $bytes = [byte[]]::new(48)
    $generator = [Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $generator.GetBytes($bytes)
    }
    finally {
        $generator.Dispose()
    }
    return [Convert]::ToBase64String($bytes)
}

function Invoke-WslRoot {
    param([string]$Script)

    $Script = $Script -replace "`r`n", "`n" -replace "`r", "`n"
    $encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($Script))
    & wsl.exe -d $WslDistro -u root -- bash -lc "set -euo pipefail; printf '%s' '$encoded' | base64 -d | bash"
    if ($LASTEXITCODE -ne 0) {
        throw "WSL command failed with exit code $LASTEXITCODE."
    }
}

function ConvertTo-WslUncPath {
    param([string]$LinuxPath)

    $relative = $LinuxPath.TrimStart("/") -replace "/", "\"
    return "\\wsl.localhost\$WslDistro\$relative"
}

function Get-WslIpAddress {
    $output = & wsl.exe -d $WslDistro -u root -- bash -lc "hostname -I" 2>&1
    if ($LASTEXITCODE -ne 0) {
        $output | ForEach-Object { Write-Host $_ }
        throw "Could not get WSL IP address."
    }

    $text = ($output -join " ") -replace "`0", ""
    $ip = [regex]::Match($text, "\b(?:\d{1,3}\.){3}\d{1,3}\b").Value
    if ([string]::IsNullOrWhiteSpace($ip)) {
        throw "Could not parse WSL IP address from: $text"
    }

    return $ip
}

function Set-WindowsPortProxy {
    param(
        [string]$WslIpAddress,
        [int[]]$Ports
    )

    Write-Host "Configuring Windows localhost port proxy to WSL IP $WslIpAddress..."
    foreach ($port in $Ports) {
        & netsh.exe interface portproxy delete v4tov4 listenaddress=0.0.0.0 listenport=$port | Out-Null
        & netsh.exe interface portproxy add v4tov4 listenaddress=0.0.0.0 listenport=$port connectaddress=$WslIpAddress connectport=$port | Out-Host
        if ($LASTEXITCODE -ne 0) {
            throw "Could not configure Windows IPv4 port proxy for port $port."
        }

        & netsh.exe interface portproxy delete v6tov4 listenaddress=::1 listenport=$port | Out-Null
        & netsh.exe interface portproxy add v6tov4 listenaddress=::1 listenport=$port connectaddress=$WslIpAddress connectport=$port | Out-Host
        if ($LASTEXITCODE -ne 0) {
            throw "Could not configure Windows IPv6 localhost port proxy for port $port."
        }

        $ruleName = "n8n whisper WSL port $port"
        if (-not (Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue)) {
            New-NetFirewallRule `
                -DisplayName $ruleName `
                -Direction Inbound `
                -Protocol TCP `
                -LocalPort $port `
                -Action Allow | Out-Null
        }
    }
}

function Start-WslKeepAlive {
    Invoke-WslRoot -Script @'
set -euo pipefail
if ! pgrep -f n8n-whisper-wsl-keepalive >/dev/null 2>&1; then
  nohup bash -c 'exec -a n8n-whisper-wsl-keepalive sleep infinity' >/dev/null 2>&1 &
fi
'@

    Write-Host "WSL keep-alive process is running for $WslDistro."
}

Write-Host "Deploying $Repository@$DeploySha to ${WslDistro}:$WslDeployDir"
Start-WslKeepAlive

$nativeTask = Get-ScheduledTask -TaskName "n8n-whisper-transcriber" -ErrorAction SilentlyContinue
if ($nativeTask) {
    Stop-ScheduledTask -TaskName "n8n-whisper-transcriber" -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName "n8n-whisper-transcriber" -Confirm:$false
}

Invoke-WslRoot -Script @"
set -euo pipefail
DEPLOY_DIR=$(Quote-Bash $WslDeployDir)
mkdir -p "`$DEPLOY_DIR"/data "`$DEPLOY_DIR"/models "`$DEPLOY_DIR"/n8n_data
chown -R 1000:1000 "`$DEPLOY_DIR"/n8n_data
"@

$composePath = Join-Path $SourceDir "docker-compose.prod.yml"
if (-not (Test-Path -LiteralPath $composePath)) {
    throw "Missing docker-compose.prod.yml at $composePath"
}

$envExamplePath = Join-Path $SourceDir ".env.example"
if (-not (Test-Path -LiteralPath $envExamplePath)) {
    throw "Missing .env.example at $envExamplePath"
}

$encodedCompose = [Convert]::ToBase64String([IO.File]::ReadAllBytes($composePath))
$key = New-RandomKey
$envContent = Get-Content -LiteralPath $envExamplePath -Raw
$envContent = $envContent -replace "(?m)^TZ=.*", "TZ=Asia/Yekaterinburg"
$envContent = $envContent -replace "(?m)^N8N_HOST=.*", "N8N_HOST=localhost"
$envContent = $envContent -replace "(?m)^N8N_ENCRYPTION_KEY=.*", "N8N_ENCRYPTION_KEY=$key"
$envContent = $envContent -replace "`r`n", "`n" -replace "`r", "`n"
$encodedEnv = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($envContent))

Invoke-WslRoot -Script @"
set -euo pipefail
DEPLOY_DIR=$(Quote-Bash $WslDeployDir)
printf '%s' '$encodedCompose' | base64 -d >"`$DEPLOY_DIR/docker-compose.prod.yml"
chmod 644 "`$DEPLOY_DIR/docker-compose.prod.yml"
if [ ! -f "`$DEPLOY_DIR/.env" ]; then
  printf '%s' '$encodedEnv' | base64 -d >"`$DEPLOY_DIR/.env"
  chmod 600 "`$DEPLOY_DIR/.env"
fi
"@

$repositoryLower = $Repository.ToLowerInvariant()
$image = "ghcr.io/$repositoryLower/transcriber:sha-$DeploySha"

$script = @"
set -euo pipefail

log() {
  printf '\n[%s] %s\n' "`$(date -Is)" "`$*"
}

retry() {
  max_attempts="`$1"
  delay_seconds="`$2"
  shift 2
  attempt=1

  while true; do
    if "`$@"; then
      return 0
    fi

    exit_code=`$?
    if [ "`$attempt" -ge "`$max_attempts" ]; then
      log "Command failed after `$attempt attempts with exit code `$exit_code: `$*"
      return "`$exit_code"
    fi

    log "Command failed with exit code `$exit_code; retrying in `${delay_seconds}s (`$attempt/`$max_attempts): `$*"
    sleep "`$delay_seconds"
    attempt=`$((attempt + 1))
    delay_seconds=`$((delay_seconds * 2))
    if [ "`$delay_seconds" -gt 300 ]; then
      delay_seconds=300
    fi
  done
}

DEPLOY_DIR=$(Quote-Bash $WslDeployDir)
IMAGE=$(Quote-Bash $image)
GHCR_USERNAME=$(Quote-Bash $GhcrUsername)
GHCR_TOKEN=$(Quote-Bash $GhcrToken)

export DOCKER_CLIENT_TIMEOUT=3600
export COMPOSE_HTTP_TIMEOUT=3600
export COMPOSE_PROGRESS=plain
export TRANSCRIBER_IMAGE="`$IMAGE"

log "Docker version"
docker version
docker compose version

if [ -n "`$GHCR_TOKEN" ]; then
  log "Logging in to ghcr.io as `$GHCR_USERNAME"
  printf '%s' "`$GHCR_TOKEN" | docker login ghcr.io -u "`$GHCR_USERNAME" --password-stdin
fi

cd "`$DEPLOY_DIR"

log "Pulling n8n image"
retry 4 15 docker compose --env-file .env -f docker-compose.prod.yml pull n8n

log "Pulling transcriber image: `$IMAGE"
retry 8 30 docker compose --env-file .env -f docker-compose.prod.yml pull transcriber

log "Starting containers"
docker compose --env-file .env -f docker-compose.prod.yml up -d --remove-orphans

log "Current compose state"
docker compose --env-file .env -f docker-compose.prod.yml ps

log "Waiting for whisper_transcriber_api healthcheck"
healthy=0
for attempt in `$(seq 1 60); do
  status=`$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' whisper_transcriber_api 2>/dev/null || true)
  if [ "`$status" = "healthy" ]; then
    healthy=1
    break
  fi
  if [ "`$attempt" = "1" ] || [ `$((attempt % 6)) -eq 0 ]; then
    log "Health attempt `$attempt/60: `${status:-container not ready}"
  fi
  sleep 5
done

if [ "`$healthy" != "1" ]; then
  log "Transcriber did not become healthy; recent logs follow"
  docker logs --tail 150 whisper_transcriber_api || true
  exit 20
fi

log "Deployment completed"
"@

Invoke-WslRoot -Script $script
$wslIpAddress = Get-WslIpAddress
Set-WindowsPortProxy -WslIpAddress $wslIpAddress -Ports @(5678, 7861)
Write-Host "Docker deployment is healthy: http://localhost:7861/health"
