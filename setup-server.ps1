[CmdletBinding()]
param(
    [string]$RepositoryUrl = "https://github.com/Element-IT/element-it",
    [string]$RunnerName = $env:COMPUTERNAME,
    [string]$RunnerLabel = "deploy",
    [string]$RunnerDirectory = "C:\actions-runner",
    [string]$ServiceUser = $env:USERNAME,
    [string]$WslDistro = "Ubuntu",
    [string]$WslDeployDirectory = "/opt/n8n_whisper_transcriber",
    [string]$RunnerVersion = "2.335.1",
    [string]$RunnerSha256 = "eb65c95277af42bcf3778a799c41359d224ba2a67b4de26b7cea1729b09c803d",
    [switch]$ResumeAfterReboot
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$ResumeTaskName = "N8nWhisperDockerSetupResume"

function Write-Step {
    param([string]$Message)
    Write-Host "`n==> $Message" -ForegroundColor Cyan
}

function Assert-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        if (-not $PSCommandPath) {
            throw "Run this file from PowerShell as Administrator."
        }

        $arguments = @(
            "-NoProfile",
            "-ExecutionPolicy", "Bypass",
            "-File", "`"$PSCommandPath`"",
            "-RepositoryUrl", "`"$RepositoryUrl`"",
            "-RunnerName", "`"$RunnerName`"",
            "-RunnerLabel", "`"$RunnerLabel`"",
            "-RunnerDirectory", "`"$RunnerDirectory`"",
            "-ServiceUser", "`"$ServiceUser`"",
            "-WslDistro", "`"$WslDistro`"",
            "-WslDeployDirectory", "`"$WslDeployDirectory`"",
            "-RunnerVersion", "`"$RunnerVersion`"",
            "-RunnerSha256", "`"$RunnerSha256`""
        )
        if ($ResumeAfterReboot) {
            $arguments += "-ResumeAfterReboot"
        }
        Start-Process powershell.exe -Verb RunAs -ArgumentList $arguments
        exit
    }
}

function Invoke-Checked {
    param(
        [string]$FilePath,
        [string[]]$ArgumentList
    )

    & $FilePath @ArgumentList
    if ($LASTEXITCODE -ne 0) {
        throw "$FilePath failed with exit code $LASTEXITCODE."
    }
}

function Test-ServiceAccountMatches {
    param(
        [string]$StartName,
        [string]$UserName
    )

    if ([string]::IsNullOrWhiteSpace($StartName)) {
        return $false
    }

    $normalized = $StartName.ToLowerInvariant()
    $user = $UserName.ToLowerInvariant()
    return (
        $normalized -eq ".\$user" -or
        $normalized -eq "$($env:COMPUTERNAME.ToLowerInvariant())\$user" -or
        $normalized.EndsWith("\$user")
    )
}

function Install-WingetPackage {
    param(
        [string]$Id,
        [string]$Name
    )

    Write-Host "Installing/checking $Name..."

    $listOutput = & winget list --exact --id $Id --accept-source-agreements 2>&1
    if ($LASTEXITCODE -eq 0 -and (($listOutput -join "`n") -match [regex]::Escape($Id))) {
        Write-Host "$Name is already installed."
        return
    }

    $installOutput = & winget install `
        --exact `
        --id $Id `
        --scope machine `
        --accept-package-agreements `
        --accept-source-agreements `
        --disable-interactivity 2>&1
    $exitCode = $LASTEXITCODE
    $installOutput | ForEach-Object { Write-Host $_ }

    if ($exitCode -notin @(0, -1978335189)) {
        $listOutput = & winget list --exact --id $Id --accept-source-agreements 2>&1
        if ($LASTEXITCODE -eq 0 -and (($listOutput -join "`n") -match [regex]::Escape($Id))) {
            Write-Host "$Name appears to be installed after winget returned $exitCode. Continuing."
            return
        }

        throw "winget could not install $Name ($Id), exit code $exitCode."
    }
}

function Update-ProcessPath {
    $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machinePath;$userPath"
}

function ConvertTo-PlainText {
    param([Security.SecureString]$SecureString)

    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecureString)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
    }
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

function Invoke-WslUpdateBestEffort {
    Write-Host "Updating WSL if Windows can reach the update source..."

    $commands = @(
        @("--update", "--web-download"),
        @("--update")
    )

    foreach ($arguments in $commands) {
        $output = & wsl.exe @arguments 2>&1
        $exitCode = $LASTEXITCODE
        $output | ForEach-Object { Write-Host $_ }

        if ($exitCode -eq 0) {
            return
        }

        $text = $output -join "`n"
        if ($text -match "(?i)restart|reboot|перезагруз") {
            Register-ResumeTaskAndRestart
        }
    }

    Write-Host "WSL update did not complete. Continuing because the installed WSL version may still be usable." -ForegroundColor Yellow
}

function Set-WslDefaultVersionBestEffort {
    $output = & wsl.exe --set-default-version 2 2>&1
    $exitCode = $LASTEXITCODE
    $output | ForEach-Object { Write-Host $_ }

    if ($exitCode -eq 0) {
        return
    }

    $text = $output -join "`n"
    if ($text -match "(?i)restart|reboot|перезагруз") {
        Register-ResumeTaskAndRestart
    }

    Write-Host "Could not set the default WSL version yet. Continuing; distro startup will verify WSL2 readiness." -ForegroundColor Yellow
}

function Get-WslDistros {
    $output = & wsl.exe -l -q 2>$null
    if ($LASTEXITCODE -ne 0) {
        return @()
    }

    return @(
        $output |
            ForEach-Object { ($_ -replace "`0", "").Trim() } |
            Where-Object { $_ }
    )
}

function Test-WslDistroReady {
    $output = & wsl.exe -d $WslDistro -u root -- bash -lc "echo WSL ready" 2>&1
    $exitCode = $LASTEXITCODE
    if ($exitCode -eq 0) {
        return $true
    }

    Write-Host ($output -join "`n")
    return $false
}

function Ensure-WslDistro {
    $distros = Get-WslDistros
    if ($distros -notcontains $WslDistro) {
        Write-Host "Installing WSL distro $WslDistro..."
        $installResults = @()
        $installCommands = @(
            @("--install", "-d", $WslDistro, "--no-launch", "--web-download"),
            @("--install", "-d", $WslDistro, "--no-launch")
        )

        foreach ($arguments in $installCommands) {
            $installOutput = & wsl.exe @arguments 2>&1
            $exitCode = $LASTEXITCODE
            $installText = $installOutput -join "`n"
            $installResults += $installText
            $installOutput | ForEach-Object { Write-Host $_ }

            $distros = Get-WslDistros
            if ($exitCode -eq 0 -or $distros -contains $WslDistro -or $installText -match "(?i)already|exist|уже|существ") {
                break
            }

            if ($installText -match "(?i)restart|reboot|перезагруз") {
                Register-ResumeTaskAndRestart
            }
        }

        $distros = Get-WslDistros
        if (
            $distros -notcontains $WslDistro -and
            (($installResults -join "`n") -notmatch "(?i)already|exist|уже|существ")
        ) {
            if (($installResults -join "`n") -match "(?i)restart|reboot|перезагруз") {
                Register-ResumeTaskAndRestart
            }

            throw "WSL could not install distro '$WslDistro'. Try running 'wsl --install -d $WslDistro' once manually, reboot, then run this setup again."
        }
    }

    for ($attempt = 1; $attempt -le 12; $attempt++) {
        if (Test-WslDistroReady) {
            return
        }

        Write-Host "Waiting for WSL distro initialization attempt $attempt/12..."
        Start-Sleep -Seconds 10
    }

    throw "WSL distro '$WslDistro' was installed but did not become ready. Reboot Windows, then run setup-server.ps1 again."
}

function Register-ResumeTaskAndRestart {
    if (-not $PSCommandPath) {
        throw "Cannot create resume task because script path is unknown."
    }

    $arguments = @(
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File", "`"$PSCommandPath`"",
        "-RepositoryUrl", "`"$RepositoryUrl`"",
        "-RunnerName", "`"$RunnerName`"",
        "-RunnerLabel", "`"$RunnerLabel`"",
        "-RunnerDirectory", "`"$RunnerDirectory`"",
        "-ServiceUser", "`"$ServiceUser`"",
        "-WslDistro", "`"$WslDistro`"",
        "-WslDeployDirectory", "`"$WslDeployDirectory`"",
        "-RunnerVersion", "`"$RunnerVersion`"",
        "-RunnerSha256", "`"$RunnerSha256`"",
        "-ResumeAfterReboot"
    )

    $currentUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument ($arguments -join " ")
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $currentUser
    $principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Interactive -RunLevel Highest
    Register-ScheduledTask -TaskName $ResumeTaskName -Action $action -Trigger $trigger -Principal $principal -Force | Out-Null

    Write-Host "WSL features require a reboot. The setup will resume automatically after you sign in." -ForegroundColor Yellow
    Restart-Computer -Force
    exit
}

Assert-Administrator

if ($ResumeAfterReboot) {
    Unregister-ScheduledTask -TaskName $ResumeTaskName -Confirm:$false -ErrorAction SilentlyContinue
}

if (-not [Environment]::Is64BitOperatingSystem) {
    throw "A 64-bit Windows installation is required."
}

Write-Step "Installing Windows prerequisites"
if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
    throw "winget is not installed. Install Microsoft App Installer and run this file again."
}

$packages = @(
    @{ Id = "Git.Git"; Name = "Git" },
    @{ Id = "Microsoft.DotNet.Runtime.8"; Name = ".NET 8 Runtime" },
    @{ Id = "Microsoft.VCRedist.2015+.x64"; Name = "Visual C++ Runtime" }
)

foreach ($package in $packages) {
    Install-WingetPackage -Id $package.Id -Name $package.Name
}
Update-ProcessPath

if (-not (Get-Command git.exe -ErrorAction SilentlyContinue)) {
    throw "git.exe is not available in PATH after installation."
}

Write-Step "Enabling WSL2"
$featureNames = @("Microsoft-Windows-Subsystem-Linux", "VirtualMachinePlatform")
$needsRestart = $false
foreach ($featureName in $featureNames) {
    $feature = Get-WindowsOptionalFeature -Online -FeatureName $featureName
    if ($feature.State -ne "Enabled") {
        Enable-WindowsOptionalFeature -Online -FeatureName $featureName -All -NoRestart | Out-Null
        $needsRestart = $true
    }
}

if ($needsRestart -and -not $ResumeAfterReboot) {
    Register-ResumeTaskAndRestart
}

Invoke-WslUpdateBestEffort
Set-WslDefaultVersionBestEffort

Ensure-WslDistro

Write-Step "Configuring systemd in WSL"
Invoke-WslRoot -Script @"
set -euo pipefail
cat >/etc/wsl.conf <<'EOF'
[boot]
systemd=true
EOF
"@
& wsl.exe --shutdown
Start-Sleep -Seconds 3

Write-Step "Installing Docker Engine and NVIDIA Container Toolkit in WSL"
$dockerInstallScript = @'
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

apt-get update
apt-get install -y ca-certificates curl gnupg lsb-release apt-transport-https
install -m 0755 -d /etc/apt/keyrings

curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
. /etc/os-release
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" >/etc/apt/sources.list.d/docker.list

curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list |
  sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' >/etc/apt/sources.list.d/nvidia-container-toolkit.list

apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin nvidia-container-toolkit
nvidia-ctk runtime configure --runtime=docker

systemctl enable docker >/dev/null 2>&1 || true
systemctl restart docker >/dev/null 2>&1 || service docker restart
docker version
docker compose version
'@
Invoke-WslRoot -Script $dockerInstallScript

Write-Step "Checking NVIDIA driver and CUDA container runtime"
if (-not (Get-Command nvidia-smi.exe -ErrorAction SilentlyContinue)) {
    throw "nvidia-smi.exe was not found. Install or update the Windows NVIDIA driver, reboot, then run this setup again."
}
nvidia-smi.exe

Invoke-WslRoot -Script @"
set -euo pipefail
docker run --rm --gpus all nvidia/cuda:12.6.3-base-ubuntu22.04 nvidia-smi
"@

Write-Step "Preparing WSL deployment directory"
$encryptionKey = New-RandomKey
$envContent = @"
# Generated by setup-server.ps1. Keep this file private.
TZ=Asia/Yekaterinburg

N8N_IMAGE=n8nio/n8n:2.25.4
N8N_HOST=localhost
N8N_HOST_PORT=5678
N8N_LISTEN_ADDRESS=0.0.0.0
N8N_PROTOCOL=http
N8N_SECURE_COOKIE=false
N8N_ENCRYPTION_KEY=$encryptionKey

TRANSCRIBER_HOST_PORT=7861
TRANSCRIBER_IMAGE=ghcr.io/element-it/element-it/transcriber:latest

PLANFIX_AUDIO_EXTENSIONS=.mp3,.m4a,.wav,.ogg,.opus,.webm,.aac,.flac,.mp4,.mov,.mkv,.avi
PLANFIX_CREATE_TRANSCRIBE_JOBS=true
PLANFIX_ALLOWED_FILE_URL_HOSTS=planfix.ru,.planfix.ru
PLANFIX_FILE_URL_TIMEOUT=120
PLANFIX_RESULT_WEBHOOK_ID=
PLANFIX_RESULT_WEBHOOK_URL=
PLANFIX_RESULT_FILE_FIELD=txt_file
PLANFIX_RESULT_TIMEOUT=120
PLANFIX_ALLOWED_RESULT_HOSTS=planfix.ru,.planfix.ru

NVIDIA_VISIBLE_DEVICES=all
NVIDIA_DRIVER_CAPABILITIES=compute,utility
"@
$encodedEnv = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($envContent))
Invoke-WslRoot -Script @"
set -euo pipefail
DEPLOY_DIR=$(Quote-Bash $WslDeployDirectory)
mkdir -p "`$DEPLOY_DIR"/data "`$DEPLOY_DIR"/models "`$DEPLOY_DIR"/n8n_data
if [ ! -f "`$DEPLOY_DIR/.env" ]; then
  echo '$encodedEnv' | base64 -d >"`$DEPLOY_DIR/.env"
  chmod 600 "`$DEPLOY_DIR/.env"
fi
"@

Write-Step "Opening Windows Firewall ports"
foreach ($rule in @(
    @{ Name = "n8n"; Port = 5678 },
    @{ Name = "Whisper API"; Port = 7861 }
)) {
    if (-not (Get-NetFirewallRule -DisplayName $rule.Name -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule `
            -DisplayName $rule.Name `
            -Direction Inbound `
            -Protocol TCP `
            -LocalPort $rule.Port `
            -Action Allow | Out-Null
    }
}

Write-Step "Creating the GitHub runner service account"
$runnerConfig = Join-Path $RunnerDirectory ".runner"
$runnerService = Get-CimInstance Win32_Service -ErrorAction SilentlyContinue |
    Where-Object {
        $_.Name -like "actions.runner.*" -and
        $_.PathName -like "*$RunnerDirectory*"
    } |
    Select-Object -First 1
$needsRunnerRegistration = -not (Test-Path -LiteralPath $runnerConfig) -or -not $runnerService
$serviceAccountMismatch = $runnerService -and -not (Test-ServiceAccountMatches -StartName $runnerService.StartName -UserName $ServiceUser)

$servicePasswordSecure = $null
$servicePasswordPlain = $null
if ($needsRunnerRegistration -or $serviceAccountMismatch -or -not (Get-LocalUser -Name $ServiceUser -ErrorAction SilentlyContinue)) {
    $servicePasswordSecure = Read-Host "Password to set/use for .\$ServiceUser" -AsSecureString
    $servicePasswordConfirmation = Read-Host "Repeat the password for .\$ServiceUser" -AsSecureString
    $servicePasswordPlain = ConvertTo-PlainText $servicePasswordSecure
    $servicePasswordConfirmationPlain = ConvertTo-PlainText $servicePasswordConfirmation
    if ($servicePasswordPlain -ne $servicePasswordConfirmationPlain) {
        throw "The service account passwords do not match."
    }
    if ([string]::IsNullOrWhiteSpace($servicePasswordPlain)) {
        throw "The service account password cannot be empty."
    }
}

$serviceAccount = Get-LocalUser -Name $ServiceUser -ErrorAction SilentlyContinue
if ($serviceAccount -and $servicePasswordSecure -and $ServiceUser -ne $env:USERNAME) {
    Set-LocalUser -Name $ServiceUser -Password $servicePasswordSecure -AccountNeverExpires -PasswordNeverExpires $true
}
elseif (-not $serviceAccount) {
    New-LocalUser `
        -Name $ServiceUser `
        -Password $servicePasswordSecure `
        -AccountNeverExpires `
        -PasswordNeverExpires `
        -Description "GitHub Actions deployment runner" | Out-Null
}

$administratorsGroup = Get-LocalGroup -SID "S-1-5-32-544"
$isAdministrator = Get-LocalGroupMember -Group $administratorsGroup -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -match "\\$([regex]::Escape($ServiceUser))$" }
if (-not $isAdministrator) {
    Add-LocalGroupMember -Group $administratorsGroup -Member $ServiceUser
}

Write-Step "Downloading GitHub Actions runner $RunnerVersion"
$RunnerDirectory = [IO.Path]::GetFullPath($RunnerDirectory)
New-Item -ItemType Directory -Force -Path $RunnerDirectory | Out-Null

$runnerArchive = Join-Path $env:TEMP "actions-runner-win-x64-$RunnerVersion.zip"
$runnerUrl = "https://github.com/actions/runner/releases/download/v$RunnerVersion/actions-runner-win-x64-$RunnerVersion.zip"

if (-not (Test-Path -LiteralPath (Join-Path $RunnerDirectory "config.cmd"))) {
    Invoke-WebRequest -Uri $runnerUrl -OutFile $runnerArchive
    $actualHash = (Get-FileHash -LiteralPath $runnerArchive -Algorithm SHA256).Hash
    if ($actualHash -ne $RunnerSha256) {
        throw "Runner archive checksum mismatch. Expected $RunnerSha256, got $actualHash."
    }

    Expand-Archive -LiteralPath $runnerArchive -DestinationPath $RunnerDirectory -Force
}

Get-ChildItem -LiteralPath $RunnerDirectory -Recurse -File | Unblock-File

Write-Step "Registering and starting the GitHub Actions runner service"
$configCommand = Join-Path $RunnerDirectory "config.cmd"
if ((Test-Path -LiteralPath $runnerConfig) -and -not $runnerService) {
    Write-Host "An incomplete previous runner configuration was found. Repairing it..."
    foreach ($file in @(".runner", ".credentials", ".credentials_rsaparams", ".service")) {
        Remove-Item -LiteralPath (Join-Path $RunnerDirectory $file) -Force -ErrorAction SilentlyContinue
    }
}

if (-not (Test-Path -LiteralPath $runnerConfig)) {
    Write-Host "Create a fresh token at:"
    Write-Host "$RepositoryUrl/settings/actions/runners/new?arch=x64&os=win" -ForegroundColor Yellow
    $registrationTokenSecure = Read-Host "GitHub runner registration token" -AsSecureString
    $registrationToken = ConvertTo-PlainText $registrationTokenSecure
    if ([string]::IsNullOrWhiteSpace($registrationToken)) {
        throw "The GitHub registration token cannot be empty."
    }

    $env:ACTIONS_RUNNER_INPUT_TOKEN = $registrationToken
    $env:ACTIONS_RUNNER_INPUT_WINDOWSLOGONPASSWORD = $servicePasswordPlain

    try {
        Push-Location $RunnerDirectory
        Invoke-Checked -FilePath $configCommand -ArgumentList @(
            "--unattended",
            "--url", $RepositoryUrl,
            "--name", $RunnerName,
            "--runnergroup", "Default",
            "--labels", $RunnerLabel,
            "--work", "_work",
            "--replace",
            "--runasservice",
            "--windowslogonaccount", ".\$ServiceUser"
        )
    }
    finally {
        Pop-Location
        Remove-Item Env:ACTIONS_RUNNER_INPUT_TOKEN -ErrorAction SilentlyContinue
        Remove-Item Env:ACTIONS_RUNNER_INPUT_WINDOWSLOGONPASSWORD -ErrorAction SilentlyContinue
    }
}
else {
    Write-Host "Runner is already configured; registration was skipped."
}

$runnerService = Get-CimInstance Win32_Service |
    Where-Object {
        $_.Name -like "actions.runner.*" -and
        $_.PathName -like "*$RunnerDirectory*"
    } |
    Select-Object -First 1
if (-not $runnerService) {
    throw "The GitHub Actions runner service was not created."
}

if (-not (Test-ServiceAccountMatches -StartName $runnerService.StartName -UserName $ServiceUser)) {
    if (-not $servicePasswordPlain) {
        throw "Runner service runs as '$($runnerService.StartName)', but WSL was installed for '$ServiceUser'. Re-run setup-server.ps1 and enter the password for .\$ServiceUser."
    }

    Write-Host "Changing runner service account from '$($runnerService.StartName)' to '.\$ServiceUser'..."
    Stop-Service -Name $runnerService.Name -ErrorAction SilentlyContinue
    & sc.exe config $runnerService.Name obj= ".\$ServiceUser" password= $servicePasswordPlain | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw "Could not change runner service account to .\$ServiceUser."
    }
    $runnerService = Get-CimInstance Win32_Service -Filter "Name='$($runnerService.Name)'"
}

if ($runnerService.State -ne "Running") {
    Start-Service -Name $runnerService.Name
}
Set-Service -Name $runnerService.Name -StartupType Automatic

$registrationToken = $null
$servicePasswordPlain = $null

Write-Step "Docker server setup completed"
Write-Host "Runner service:   $($runnerService.Name)" -ForegroundColor Green
Write-Host "Runner label:     $RunnerLabel"
Write-Host "WSL distro:       $WslDistro"
Write-Host "Deploy directory: $WslDeployDirectory"
Write-Host ""
Write-Host "Push these project changes to main. After CI succeeds, CD will run Docker Compose inside WSL automatically." -ForegroundColor Yellow
