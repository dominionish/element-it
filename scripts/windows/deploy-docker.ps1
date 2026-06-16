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

Write-Host "Deploying $Repository@$DeploySha to ${WslDistro}:$WslDeployDir"

$nativeTask = Get-ScheduledTask -TaskName "n8n-whisper-transcriber" -ErrorAction SilentlyContinue
if ($nativeTask) {
    Stop-ScheduledTask -TaskName "n8n-whisper-transcriber" -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName "n8n-whisper-transcriber" -Confirm:$false
}

$sourceLinux = (& wsl.exe -d $WslDistro -u root -- wslpath -a "$SourceDir").Trim()
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($sourceLinux)) {
    throw "Could not map source directory into WSL: $SourceDir"
}

$repositoryLower = $Repository.ToLowerInvariant()
$image = "ghcr.io/$repositoryLower/transcriber:sha-$DeploySha"

$script = @"
set -euo pipefail

DEPLOY_DIR=$(Quote-Bash $WslDeployDir)
SOURCE_DIR=$(Quote-Bash $sourceLinux)
IMAGE=$(Quote-Bash $image)
GHCR_USERNAME=$(Quote-Bash $GhcrUsername)
GHCR_TOKEN=$(Quote-Bash $GhcrToken)

mkdir -p "`$DEPLOY_DIR"/data "`$DEPLOY_DIR"/models "`$DEPLOY_DIR"/n8n_data
cp "`$SOURCE_DIR/docker-compose.prod.yml" "`$DEPLOY_DIR/docker-compose.prod.yml"

if [ ! -f "`$DEPLOY_DIR/.env" ]; then
  cp "`$SOURCE_DIR/.env.example" "`$DEPLOY_DIR/.env"
  key="`$(head -c 48 /dev/urandom | base64)"
  sed -i "s#^TZ=.*#TZ=Asia/Yekaterinburg#" "`$DEPLOY_DIR/.env"
  sed -i "s#^N8N_HOST=.*#N8N_HOST=localhost#" "`$DEPLOY_DIR/.env"
  sed -i "s#^N8N_ENCRYPTION_KEY=.*#N8N_ENCRYPTION_KEY=`$key#" "`$DEPLOY_DIR/.env"
fi

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
