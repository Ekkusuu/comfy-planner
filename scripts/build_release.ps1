# Builds the Windows installer: dist\ComfyPlanner-Setup-<version>.exe (and a .sha256 file next to it).
#
#   scripts\build_release.ps1                  version from pyproject.toml
#   scripts\build_release.ps1 -Version 0.1.1   a different version number (used to test upgrades)
#
# What it does:
#   1. builds the UI (npm ci, npm run build) and the launcher (launcher\build.ps1 -NoShortcut)
#   2. downloads a pinned uv release, checks its SHA-256 against the hash below, and takes uv.exe out of it
#   3. downloads Microsoft's Evergreen Bootstrapper for the WebView2 Runtime and checks that it carries a valid
#      Microsoft signature (the file changes over time, so there is no pinned hash)
#   4. stages the program in dist\ComfyPlanner-<version>\ : the files git tracks (working-tree copies, so
#      uncommitted edits are included) minus tests, docs and sources that are not needed at runtime, plus
#      ui\dist, the exe and its DLLs, uv.exe and a VERSION file
#   5. runs Inno Setup (installer\comfy-planner.iss) on that folder; the bootstrapper is passed in separately
#      (/DWebView2Setup), so it is inside the installer but never copied to the install folder
#
# The installer runs the bootstrapper only when the WebView2 Runtime is missing on the PC.
#
# The installed app creates its Python environment on the first start (uv sync from uv.lock), so the
# installer carries no Python and no packages.
#
# Needs: Node.js and npm, git, Inno Setup 6 (ISCC.exe), and internet access the first time (uv, WebView2 SDK,
# WebView2 bootstrapper).
# Stop Comfy Planner first (a running exe cannot be rebuilt).

param(
    [string]$Version
)

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$distDir = Join-Path $root 'dist'                           # git-ignored
$issPath = Join-Path $root 'installer\comfy-planner.iss'

# The uv release that goes into the installer. To update: change the version and the hash. The hash is the
# SHA-256 that the release publishes next to the zip (uv-x86_64-pc-windows-msvc.zip.sha256 on its GitHub page).
$uvVersion = '0.13.1'
$uvSha256 = '3B2661A463A5BAEA7F5076EA2B25AB71D77F36A6C0D809E5350651792F80A672'
$uvZipName = 'uv-x86_64-pc-windows-msvc.zip'
$uvUrl = "https://github.com/astral-sh/uv/releases/download/$uvVersion/$uvZipName"

# Files git tracks that do not go into the installer (regular expressions on the path, with / separators).
# ui\ is left out because the built ui\dist is added below; launcher\icon.ico is the tray icon the exe loads.
$excluded = @(
    '^tests/', '^\.github/', '^\.impeccable/', '^\.gitignore$',
    '^DESIGN\.md$', '^start\.ps1$',
    '^installer/', '^scripts/build_release\.ps1$',
    '^launcher/(?!icon\.ico$)',
    '^ui/'
)

function Invoke-Native {
    # Runs a program and stops the build at once if it fails.
    param([string]$What, [scriptblock]$Command)
    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "$What failed with exit code $LASTEXITCODE"
    }
}

# --- version ---------------------------------------------------------------

if (-not $Version) {
    $match = [regex]::Match((Get-Content -Raw -LiteralPath (Join-Path $root 'pyproject.toml')), '(?m)^version\s*=\s*"([^"]+)"')
    if (-not $match.Success) {
        throw 'no version found in pyproject.toml'
    }
    $Version = $match.Groups[1].Value
}
if ($Version -notmatch '^\d+\.\d+\.\d+$') {
    throw "version '$Version' is not in the form 1.2.3"
}
Write-Host "Version: $Version"

# --- Inno Setup ------------------------------------------------------------

$iscc = @(
    (Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe'),
    (Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe'),
    (Join-Path $env:ProgramFiles 'Inno Setup 6\ISCC.exe')
) | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $iscc) {
    throw 'Inno Setup 6 (ISCC.exe) was not found. Install it from https://jrsoftware.org/isinfo.php'
}

# --- UI and launcher -------------------------------------------------------

Write-Host 'Building the UI ...'
Push-Location (Join-Path $root 'ui')
try {
    Invoke-Native 'npm ci' { npm ci }
    Invoke-Native 'npm run build' { npm run build }
}
finally {
    Pop-Location
}
if (-not (Test-Path (Join-Path $root 'ui\dist\index.html'))) {
    throw 'ui\dist\index.html is missing after the UI build'
}

Write-Host 'Building the launcher ...'
& (Join-Path $root 'launcher\build.ps1') -NoShortcut -Version $Version

# --- uv.exe ----------------------------------------------------------------

$uvDir = Join-Path $distDir "downloads\uv-$uvVersion"
$uvZip = Join-Path $uvDir $uvZipName
$uvExe = Join-Path $uvDir 'uv.exe'
New-Item -ItemType Directory -Force -Path $uvDir | Out-Null

if (-not (Test-Path $uvZip)) {
    Write-Host "Downloading uv $uvVersion from github.com/astral-sh/uv ..."
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    $part = "$uvZip.part"
    $savedProgress = $ProgressPreference
    $ProgressPreference = 'SilentlyContinue'
    try {
        Invoke-WebRequest -Uri $uvUrl -OutFile $part -UseBasicParsing
    }
    finally {
        $ProgressPreference = $savedProgress
    }
    Move-Item -LiteralPath $part -Destination $uvZip -Force
}
$actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $uvZip).Hash
if ($actual -ne $uvSha256) {
    Remove-Item -LiteralPath $uvZip -Force
    throw "uv download hash mismatch: expected $uvSha256 but got $actual. The file was deleted. Not using it."
}

# Read only uv.exe from the zip, by exact name; nothing else is extracted or run.
Add-Type -AssemblyName System.IO.Compression.FileSystem
$zip = [System.IO.Compression.ZipFile]::OpenRead($uvZip)
try {
    $entry = $zip.GetEntry('uv.exe')
    if ($null -eq $entry) {
        throw 'the uv zip has no uv.exe'
    }
    [System.IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $uvExe, $true)
}
finally {
    $zip.Dispose()
}

# --- WebView2 bootstrapper -------------------------------------------------

# Microsoft's Evergreen Bootstrapper: a small exe that downloads and installs the WebView2 Runtime. The link always
# serves the current file, so it is checked by its signature instead of a hash: it must be validly signed by Microsoft.
$webView2Url = 'https://go.microsoft.com/fwlink/p/?LinkId=2124703'
$webView2Dir = Join-Path $distDir 'downloads\webview2'
$webView2Setup = Join-Path $webView2Dir 'MicrosoftEdgeWebview2Setup.exe'
New-Item -ItemType Directory -Force -Path $webView2Dir | Out-Null

if (-not (Test-Path $webView2Setup)) {
    Write-Host 'Downloading the WebView2 bootstrapper from go.microsoft.com ...'
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    $part = "$webView2Setup.part"
    $savedProgress = $ProgressPreference
    $ProgressPreference = 'SilentlyContinue'
    try {
        Invoke-WebRequest -Uri $webView2Url -OutFile $part -UseBasicParsing
    }
    finally {
        $ProgressPreference = $savedProgress
    }
    Move-Item -LiteralPath $part -Destination $webView2Setup -Force
}
$signature = Get-AuthenticodeSignature -LiteralPath $webView2Setup
$signer = if ($signature.SignerCertificate) { $signature.SignerCertificate.Subject } else { '' }
if ($signature.Status -ne 'Valid' -or $signer -notlike '*O=Microsoft Corporation*') {
    Remove-Item -LiteralPath $webView2Setup -Force
    throw "WebView2 bootstrapper signature check failed (status $($signature.Status), signer '$signer'). The file was deleted. Not using it."
}
Write-Host "WebView2 bootstrapper: signature valid ($signer)"

# --- stage the program -----------------------------------------------------

$stage = Join-Path $distDir "ComfyPlanner-$Version"
if (Test-Path $stage) {
    Remove-Item -LiteralPath $stage -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $stage | Out-Null

$tracked = @(git -C $root -c core.quotepath=off ls-files)
if ($LASTEXITCODE -ne 0 -or $tracked.Count -eq 0) {
    throw 'git ls-files failed'
}
$count = 0
foreach ($path in $tracked) {
    if ($excluded | Where-Object { $path -match $_ }) {
        continue
    }
    $from = Join-Path $root $path
    if (-not (Test-Path -LiteralPath $from)) {
        continue    # tracked but deleted in the working tree (not committed yet)
    }
    $to = Join-Path $stage $path
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $to) | Out-Null
    Copy-Item -LiteralPath $from -Destination $to
    $count++
}
Write-Host "Staged $count tracked files"

Copy-Item -LiteralPath (Join-Path $root 'ui\dist') -Destination (Join-Path $stage 'ui\dist') -Recurse
foreach ($name in @('Comfy Planner.exe', 'Microsoft.Web.WebView2.Core.dll', 'WebView2Loader.dll')) {
    Copy-Item -LiteralPath (Join-Path $root $name) -Destination $stage
}
Copy-Item -LiteralPath $uvExe -Destination $stage
# The installer reads this file to know which version is installed (for the prompts backup and the downgrade warning).
[IO.File]::WriteAllText((Join-Path $stage 'VERSION'), $Version, (New-Object Text.UTF8Encoding($false)))

# --- installer -------------------------------------------------------------

Write-Host 'Running Inno Setup ...'
Invoke-Native 'Inno Setup' { & $iscc /Qp "/DAppVersion=$Version" "/DStageDir=$stage" "/DWebView2Setup=$webView2Setup" "/O$distDir" $issPath }

$setup = Join-Path $distDir "ComfyPlanner-Setup-$Version.exe"
if (-not (Test-Path $setup)) {
    throw "Inno Setup did not produce $setup"
}
$hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $setup).Hash.ToLower()
[IO.File]::WriteAllText("$setup.sha256", "$hash  $(Split-Path -Leaf $setup)`n", (New-Object Text.UTF8Encoding($false)))

Write-Host "Installer: $setup"
Write-Host ("Size:      {0:N1} MB" -f ((Get-Item $setup).Length / 1MB))
Write-Host "SHA-256:   $hash"
