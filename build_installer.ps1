$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$distDir = Join-Path $projectRoot "dist"
$tempRoot = Join-Path $env:TEMP "MadrixHueBridgeInstaller"
$buildDir = Join-Path $tempRoot "build"
$payloadDir = Join-Path $buildDir "payload"
$tempSetupExe = Join-Path $tempRoot "MadrixHueBridgeSetup.exe"
$setupExe = Join-Path $distDir "MadrixHueBridgeSetup.exe"
$sedPath = Join-Path $buildDir "MadrixHueBridgeSetup.sed"
$mainExe = Join-Path $distDir "MadrixHueBridgeUI.exe"

if (-not (Test-Path -LiteralPath $mainExe)) {
    throw "Main application EXE not found: $mainExe. Run build_exe.ps1 first."
}

New-Item -ItemType Directory -Force -Path $distDir | Out-Null
if (Test-Path -LiteralPath $tempRoot) {
    Remove-Item -LiteralPath $tempRoot -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $payloadDir | Out-Null

Copy-Item -LiteralPath $mainExe -Destination (Join-Path $payloadDir "MadrixHueBridgeUI.exe") -Force
Copy-Item -LiteralPath (Join-Path $projectRoot "config.example.json") -Destination $payloadDir -Force
Copy-Item -LiteralPath (Join-Path $projectRoot "README.md") -Destination $payloadDir -Force
Copy-Item -LiteralPath (Join-Path $projectRoot "install_remote.cmd") -Destination $payloadDir -Force

$payloadPath = $payloadDir
$setupPath = $tempSetupExe

$sed = @"
[Version]
Class=IEXPRESS
SEDVersion=3
[Options]
PackagePurpose=InstallApp
ShowInstallProgramWindow=1
HideExtractAnimation=0
UseLongFileName=1
InsideCompressed=0
CAB_FixedSize=0
CAB_ResvCodeSigning=0
RebootMode=N
InstallPrompt=
DisplayLicense=
FinishMessage=Madrix Hue Bridge installed successfully.
TargetName=$setupPath
FriendlyName=Madrix Hue Bridge Setup
AppLaunched=cmd.exe /c install_remote.cmd
PostInstallCmd=<None>
AdminQuietInstCmd=
UserQuietInstCmd=
SourceFiles=SourceFiles
[Strings]
FILE0=MadrixHueBridgeUI.exe
FILE1=config.example.json
FILE2=README.md
FILE3=install_remote.cmd
[SourceFiles]
SourceFiles0=$payloadPath
[SourceFiles0]
%FILE0%=
%FILE1%=
%FILE2%=
%FILE3%=
"@

New-Item -ItemType Directory -Force -Path $buildDir | Out-Null
Set-Content -LiteralPath $sedPath -Value $sed -Encoding ASCII

& "$env:SystemRoot\System32\iexpress.exe" /N $sedPath

for ($i = 0; $i -lt 120 -and -not (Test-Path -LiteralPath $tempSetupExe); $i++) {
    Start-Sleep -Milliseconds 500
}

if (-not (Test-Path -LiteralPath $tempSetupExe)) {
    throw "Temporary installer was not created: $tempSetupExe"
}

if (Test-Path -LiteralPath $setupExe) {
    Remove-Item -LiteralPath $setupExe -Force
}

$copied = $false
for ($i = 0; $i -lt 5 -and -not $copied; $i++) {
    try {
        Copy-Item -LiteralPath $tempSetupExe -Destination $setupExe -Force
        $copied = $true
    }
    catch {
        Start-Sleep -Seconds 1
    }
}

if (-not $copied) {
    throw "Installer was created in temp but could not be copied to dist: $tempSetupExe"
}

if (-not (Test-Path -LiteralPath $setupExe)) {
    throw "Installer was not created: $setupExe"
}

Write-Host ""
Write-Host "Installer build complete:"
Write-Host $setupExe
