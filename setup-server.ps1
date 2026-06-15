[CmdletBinding()]
param(
    [string]$RepositoryUrl = "https://github.com/Element-IT/element-it",
    [string]$RunnerName = $env:COMPUTERNAME,
    [string]$RunnerLabel = "deploy",
    [string]$RunnerDirectory = "C:\actions-runner",
    [string]$DeployDirectory = "C:\services\n8n_whisper_transcriber",
    [string]$ServiceUser = "github-runner",
    [string]$RunnerVersion = "2.335.1",
    [string]$RunnerSha256 = "eb65c95277af42bcf3778a799c41359d224ba2a67b4de26b7cea1729b09c803d"
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

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
            "-DeployDirectory", "`"$DeployDirectory`"",
            "-ServiceUser", "`"$ServiceUser`"",
            "-RunnerVersion", "`"$RunnerVersion`"",
            "-RunnerSha256", "`"$RunnerSha256`""
        )
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

function Install-WingetPackage {
    param(
        [string]$Id,
        [string]$Name
    )

    Write-Host "Installing/checking $Name..."
    & winget install `
        --exact `
        --id $Id `
        --scope machine `
        --accept-package-agreements `
        --accept-source-agreements `
        --disable-interactivity

    if ($LASTEXITCODE -notin @(0, -1978335189)) {
        throw "winget could not install $Name ($Id), exit code $LASTEXITCODE."
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

Assert-Administrator

if (-not [Environment]::Is64BitOperatingSystem) {
    throw "A 64-bit Windows installation is required."
}

Write-Step "Collecting the two secret values"
Write-Host "Create a fresh token at:"
Write-Host "$RepositoryUrl/settings/actions/runners/new?arch=x64&os=win" -ForegroundColor Yellow
$registrationTokenSecure = Read-Host "GitHub runner registration token" -AsSecureString
$servicePasswordSecure = Read-Host "Password to set for .\$ServiceUser" -AsSecureString
$servicePasswordConfirmation = Read-Host "Repeat the password for .\$ServiceUser" -AsSecureString

$servicePasswordPlain = ConvertTo-PlainText $servicePasswordSecure
$servicePasswordConfirmationPlain = ConvertTo-PlainText $servicePasswordConfirmation
if ($servicePasswordPlain -ne $servicePasswordConfirmationPlain) {
    throw "The service account passwords do not match."
}
if ([string]::IsNullOrWhiteSpace($servicePasswordPlain)) {
    throw "The service account password cannot be empty."
}
$servicePasswordConfirmationPlain = $null

$registrationToken = ConvertTo-PlainText $registrationTokenSecure
if ([string]::IsNullOrWhiteSpace($registrationToken)) {
    throw "The GitHub registration token cannot be empty."
}

Write-Step "Installing system dependencies"
if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
    throw "winget is not installed. Install Microsoft App Installer and run this file again."
}

$packages = @(
    @{ Id = "Git.Git"; Name = "Git" },
    @{ Id = "Python.Python.3.10"; Name = "Python 3.10" },
    @{ Id = "OpenJS.NodeJS.LTS"; Name = "Node.js LTS" },
    @{ Id = "Gyan.FFmpeg"; Name = "FFmpeg" },
    @{ Id = "Microsoft.DotNet.Runtime.8"; Name = ".NET 8 Runtime" },
    @{ Id = "Microsoft.DotNet.DesktopRuntime.8"; Name = ".NET 8 Desktop Runtime" },
    @{ Id = "Microsoft.VCRedist.2015+.x64"; Name = "Visual C++ Runtime" }
)

foreach ($package in $packages) {
    Install-WingetPackage -Id $package.Id -Name $package.Name
}

Update-ProcessPath

$machinePython = Join-Path $env:ProgramFiles "Python310\python.exe"
if (Test-Path -LiteralPath $machinePython) {
    $pythonDirectory = Split-Path -Parent $machinePython
    $pythonScriptsDirectory = Join-Path $pythonDirectory "Scripts"
    $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $pathParts = @($pythonDirectory, $pythonScriptsDirectory) + ($machinePath -split ";")
    $newMachinePath = ($pathParts | Where-Object { $_ } | Select-Object -Unique) -join ";"
    [Environment]::SetEnvironmentVariable("Path", $newMachinePath, "Machine")
    Update-ProcessPath
}

foreach ($command in @("git.exe", "python.exe", "node.exe", "npm.cmd", "ffmpeg.exe")) {
    if (-not (Get-Command $command -ErrorAction SilentlyContinue)) {
        throw "$command is not available in the machine PATH after installation."
    }
}

$pythonVersion = & python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
if ($pythonVersion -notin @("3.10", "3.11")) {
    throw "Python 3.10 or 3.11 is required; found $pythonVersion."
}

$nodeVersionText = (& node --version).TrimStart("v")
$nodeVersionParts = $nodeVersionText.Split(".")
$nodeMajor = [int]$nodeVersionParts[0]
$nodeMinor = [int]$nodeVersionParts[1]
if ($nodeMajor -lt 20 -or $nodeMajor -gt 24 -or ($nodeMajor -eq 20 -and $nodeMinor -lt 19)) {
    throw "Node.js 20.19 through 24 is required; found $nodeVersionText."
}

Write-Step "Creating the Windows service account"
$serviceAccount = Get-LocalUser -Name $ServiceUser -ErrorAction SilentlyContinue
if ($serviceAccount) {
    Set-LocalUser `
        -Name $ServiceUser `
        -Password $servicePasswordSecure `
        -AccountNeverExpires `
        -PasswordNeverExpires $true
}
else {
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

Write-Step "Preparing the permanent deployment directory"
$DeployDirectory = [IO.Path]::GetFullPath($DeployDirectory)
New-Item -ItemType Directory -Force -Path $DeployDirectory | Out-Null
foreach ($directory in @("data", "models", "n8n_data", "service_logs", "temp")) {
    New-Item -ItemType Directory -Force -Path (Join-Path $DeployDirectory $directory) | Out-Null
}

$envFile = Join-Path $DeployDirectory ".env"
if (-not (Test-Path -LiteralPath $envFile)) {
    $encryptionKey = New-RandomKey
    $envContent = @"
# Generated by setup-server.ps1. Keep this file private.
TZ=Asia/Yekaterinburg

N8N_VERSION=2.25.4
N8N_HOST=$env:COMPUTERNAME
N8N_HOST_PORT=5678
N8N_LISTEN_ADDRESS=0.0.0.0
N8N_PROTOCOL=http
N8N_SECURE_COOKIE=false
N8N_ENCRYPTION_KEY=$encryptionKey

TRANSCRIBER_HOST_PORT=7861

PLANFIX_AUDIO_EXTENSIONS=.mp3,.m4a,.wav,.ogg,.opus,.webm,.aac,.flac,.mp4,.mov,.mkv,.avi
PLANFIX_CREATE_TRANSCRIBE_JOBS=true
PLANFIX_ALLOWED_FILE_URL_HOSTS=planfix.ru,.planfix.ru
PLANFIX_FILE_URL_TIMEOUT=120
PLANFIX_RESULT_WEBHOOK_ID=
PLANFIX_RESULT_WEBHOOK_URL=
PLANFIX_RESULT_FILE_FIELD=txt_file
PLANFIX_RESULT_TIMEOUT=120
PLANFIX_ALLOWED_RESULT_HOSTS=planfix.ru,.planfix.ru
"@
    Set-Content -LiteralPath $envFile -Value $envContent -Encoding UTF8
}

Write-Step "Opening the Windows Firewall ports"
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
$runnerConfig = Join-Path $RunnerDirectory ".runner"
$runnerService = Get-CimInstance Win32_Service |
    Where-Object {
        $_.Name -like "actions.runner.*" -and
        $_.PathName -like "*$RunnerDirectory*"
    } |
    Select-Object -First 1

if ((Test-Path -LiteralPath $runnerConfig) -and -not $runnerService) {
    Write-Host "An incomplete previous runner configuration was found. Repairing it..."
    foreach ($file in @(".runner", ".credentials", ".credentials_rsaparams", ".service")) {
        Remove-Item -LiteralPath (Join-Path $RunnerDirectory $file) -Force -ErrorAction SilentlyContinue
    }
}

if (-not (Test-Path -LiteralPath $runnerConfig)) {
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
if ($runnerService.State -ne "Running") {
    Start-Service -Name $runnerService.Name
}
Set-Service -Name $runnerService.Name -StartupType Automatic

$registrationToken = $null
$servicePasswordPlain = $null

Write-Step "Installation completed"
Write-Host "Runner service: $($runnerService.Name)" -ForegroundColor Green
Write-Host "Runner label:   $RunnerLabel"
Write-Host "Deploy folder:  $DeployDirectory"
Write-Host "Environment:    $envFile"
Write-Host ""
Write-Host "Now push these project changes to main." -ForegroundColor Yellow
Write-Host "After CI succeeds, CD will deploy and start n8n and the transcriber automatically."
