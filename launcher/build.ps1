# Builds Comfy Planner.exe and the "Comfy Planner" Desktop shortcut.
#
# Uses the .NET Framework C# compiler that ships with Windows (C# 5) and one pinned NuGet package,
# Microsoft.Web.WebView2 (the SDK for the app window). The package is downloaded from api.nuget.org on
# the first build, checked against the SHA-256 below, and only two DLLs are taken out of it. Nothing
# in the package is run.
#
#   launcher\build.ps1
#
# Compiles launcher\Launcher.cs and launcher\AppWindow.cs with launcher\icon.ico (the brand mark, see
# launcher\make_icons.py) and launcher\app.manifest (per-monitor DPI awareness), copies the WebView2 DLLs
# next to the exe, and creates the Desktop shortcut that points at the exe.
#
# Stop Comfy Planner first (tray menu > Quit, or:  "Comfy Planner.exe" --quit): a running exe cannot be replaced.

$ErrorActionPreference = 'Stop'

$launcherDir = $PSScriptRoot
$root = Split-Path -Parent $launcherDir
$iconPath = Join-Path $launcherDir 'icon.ico'
$manifestPath = Join-Path $launcherDir 'app.manifest'
$exePath = Join-Path $root 'Comfy Planner.exe'
$sourcePaths = @((Join-Path $launcherDir 'Launcher.cs'), (Join-Path $launcherDir 'AppWindow.cs'))
$csc = 'C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe'

# The WebView2 SDK. To update: change the version and the hash (the hash is NuGet's .nupkg, sha256).
$webView2Version = '1.0.4258.31'
$webView2Sha256 = '56F7F4B8BF9AEE4B8EFEFBBDD4F67D5F74EBD1B100ED0806DA71BF76AF481AA9'
$webView2Url = "https://api.nuget.org/v3-flatcontainer/microsoft.web.webview2/$webView2Version/microsoft.web.webview2.$webView2Version.nupkg"
$sdkDir = Join-Path $launcherDir "webview2\$webView2Version"       # git-ignored
$packagePath = Join-Path $sdkDir "Microsoft.Web.WebView2.$webView2Version.nupkg"
# package entry -> file name. The x64 loader goes with the x64 build below.
$sdkFiles = [ordered]@{
    'lib/net462/Microsoft.Web.WebView2.Core.dll'   = 'Microsoft.Web.WebView2.Core.dll'
    'runtimes/win-x64/native/WebView2Loader.dll'   = 'WebView2Loader.dll'
}

if (-not (Test-Path $csc)) {
    throw "C# compiler not found: $csc (is .NET Framework 4.x installed?)"
}

# --- icon ------------------------------------------------------------------
# launcher\icon.ico (16 to 256 px) is generated with the brand mark by launcher\make_icons.py, which also writes the
# web icons in ui\public. Run it first when the mark changes:
#   uv run --with pillow python launcher\make_icons.py
if (-not (Test-Path $iconPath)) {
    throw "icon not found: $iconPath (run launcher\make_icons.py)"
}
if (-not (Test-Path $manifestPath)) {
    throw "manifest not found: $manifestPath"
}

# --- WebView2 SDK ----------------------------------------------------------

function Test-PackageHash {
    $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $packagePath).Hash
    if ($actual -ne $webView2Sha256) {
        Remove-Item -LiteralPath $packagePath -Force
        throw "WebView2 package hash mismatch: expected $webView2Sha256 but got $actual. The file was deleted. Not using it."
    }
}

$missing = @($sdkFiles.Values | Where-Object { -not (Test-Path (Join-Path $sdkDir "lib\$_")) })
if ($missing.Count -gt 0) {
    New-Item -ItemType Directory -Force -Path $sdkDir | Out-Null

    if (-not (Test-Path $packagePath)) {
        Write-Host "Downloading Microsoft.Web.WebView2 $webView2Version from api.nuget.org ..."
        [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
        $part = "$packagePath.part"
        $savedProgress = $ProgressPreference
        $ProgressPreference = 'SilentlyContinue'
        try {
            Invoke-WebRequest -Uri $webView2Url -OutFile $part -UseBasicParsing
        }
        finally {
            $ProgressPreference = $savedProgress
        }
        Move-Item -LiteralPath $part -Destination $packagePath -Force
    }
    Test-PackageHash

    # A .nupkg is a zip. Read the wanted entries by exact name; nothing else is extracted or run.
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $libDir = Join-Path $sdkDir 'lib'
    New-Item -ItemType Directory -Force -Path $libDir | Out-Null
    $zip = [System.IO.Compression.ZipFile]::OpenRead($packagePath)
    try {
        foreach ($entryName in $sdkFiles.Keys) {
            $entry = $zip.GetEntry($entryName)
            if ($null -eq $entry) {
                throw "WebView2 package has no entry $entryName"
            }
            [System.IO.Compression.ZipFileExtensions]::ExtractToFile($entry, (Join-Path $libDir $sdkFiles[$entryName]), $true)
        }
    }
    finally {
        $zip.Dispose()
    }
}
elseif (Test-Path $packagePath) {
    Test-PackageHash
}
$coreDll = Join-Path $sdkDir 'lib\Microsoft.Web.WebView2.Core.dll'

# --- compile ---------------------------------------------------------------
# x64, to match the x64 WebView2Loader.dll.

& $csc /nologo /target:winexe /platform:x64 "/win32icon:$iconPath" "/win32manifest:$manifestPath" `
    /r:System.Windows.Forms.dll /r:System.Drawing.dll "/r:$coreDll" "/out:$exePath" $sourcePaths
if ($LASTEXITCODE -ne 0) {
    throw "csc failed with exit code $LASTEXITCODE"
}
if (-not (Test-Path $exePath)) {
    throw "the compiler did not produce $exePath"
}

# The exe finds the WebView2 DLLs in its own folder.
foreach ($name in $sdkFiles.Values) {
    $from = Join-Path $sdkDir "lib\$name"
    $to = Join-Path $root $name
    if (-not (Test-Path $to) -or ((Get-FileHash -LiteralPath $from).Hash -ne (Get-FileHash -LiteralPath $to).Hash)) {
        Copy-Item -LiteralPath $from -Destination $to -Force
    }
}

# --- Desktop shortcut ------------------------------------------------------

$desktop = [Environment]::GetFolderPath('Desktop')
$lnkPath = Join-Path $desktop 'Comfy Planner.lnk'
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($lnkPath)
$shortcut.TargetPath = $exePath
$shortcut.WorkingDirectory = $root
$shortcut.IconLocation = "$exePath,0"
$shortcut.Description = 'Comfy Planner'
$shortcut.Save()
[void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($shortcut)
[void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($shell)

Write-Host "Icon:     $iconPath"
Write-Host "WebView2: $webView2Version"
Write-Host "Exe:      $exePath"
Write-Host "Shortcut: $lnkPath"
