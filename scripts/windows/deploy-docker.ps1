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

function Invoke-WslRoot {
    param([string]$Script)

    $encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($Script))
    & wsl.exe -d $WslDistro -u root -- bash -lc "set -euo pipefail; echo $encoded | base64 -d | bash"
    if ($LASTEXITCODE -ne 0) {
        throw "WSL command failed with exit code $LASTEXITCODE."
    }
}

function ConvertTo-WslUncPath {
    param([string]$LinuxPath)

    $relative = $LinuxPath.TrimStart("/") -replace "/", "\"
    return "\\wsl.localhost\$WslDistro\$relative"
}

Write-Host "Deploying $Repository@$DeploySha to ${WslDistro}:$WslDeployDir"

$nativeTask = Get-ScheduledTask -TaskName "n8n-whisper-transcriber" -ErrorAction SilentlyContinue
if ($nativeTask) {
    Stop-ScheduledTask -TaskName "n8n-whisper-transcriber" -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName "n8n-whisper-transcriber" -Confirm:$false
}

Invoke-WslRoot -Script @"
set -euo pipefail
DEPLOY_DIR=$(Quote-Bash $WslDeployDir)
mkdir -p "`$DEPLOY_DIR"/data "`$DEPLOY_DIR"/models "`$DEPLOY_DIR"/n8n_data
"@

$deployUnc = ConvertTo-WslUncPath -LinuxPath $WslDeployDir
if (-not (Test-Path -LiteralPath $deployUnc)) {
    throw "Could not access WSL deploy directory through UNC path: $deployUnc"
}

Copy-Item -LiteralPath (Join-Path $SourceDir "docker-compose.prod.yml") -Destination (Join-Path $deployUnc "docker-compose.prod.yml") -Force

$envPath = Join-Path $deployUnc ".env"
if (-not (Test-Path -LiteralPath $envPath)) {
    Copy-Item -LiteralPath (Join-Path $SourceDir ".env.example") -Destination $envPath
    $key = [Convert]::ToBase64String([Security.Cryptography.RandomNumberGenerator]::GetBytes(48))
    $content = Get-Content -LiteralPath $envPath
    $content = $content -replace "^TZ=.*", "TZ=Asia/Yekaterinburg"
    $content = $content -replace "^N8N_HOST=.*", "N8N_HOST=localhost"
    $content = $content -replace "^N8N_ENCRYPTION_KEY=.*", "N8N_ENCRYPTION_KEY=$key"
    Set-Content -LiteralPath $envPath -Value $content -Encoding UTF8
}

$repositoryLower = $Repository.ToLowerInvariant()
$image = "ghcr.io/$repositoryLower/transcriber:sha-$DeploySha"

$script = @"
set -euo pipefail

DEPLOY_DIR=$(Quote-Bash $WslDeployDir)
IMAGE=$(Quote-Bash $image)
GHCR_USERNAME=$(Quote-Bash $GhcrUsername)
GHCR_TOKEN=$(Quote-Bash $GhcrToken)

if [ -n "`$GHCR_TOKEN" ]; then
  printf '%s' "`$GHCR_TOKEN" | docker login ghcr.io -u "`$GHCR_USERNAME" --password-stdin
fi

export TRANSCRIBER_IMAGE="`$IMAGE"
cd "`$DEPLOY_DIR"

docker compose --env-file .env -f docker-compose.prod.yml pull
docker compose --env-file .env -f docker-compose.prod.yml up -d --remove-orphans
docker compose --env-file .env -f docker-compose.prod.yml ps

healthy=0
for attempt in `$(seq 1 60); do
  if docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' whisper_transcriber_api 2>/dev/null | grep -q '^healthy`$'; then
    healthy=1
    break
  fi
  sleep 5
done

if [ "`$healthy" != "1" ]; then
  docker logs --tail 150 whisper_transcriber_api || true
  exit 20
fi
"@

Invoke-WslRoot -Script $script
Write-Host "Docker deployment is healthy: http://localhost:7861/health"
