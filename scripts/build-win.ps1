param([string]$Version)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$root = Split-Path $PSScriptRoot -Parent
if (-not $Version) {
    # Same source as scripts/version.sh and the macOS build.
    $source = Get-Content "$root/src/ai_voice/__init__.py" -Raw
    if ($source -notmatch '(?m)^__version__ = "([^"]+)"') { throw 'Version not found' }
    $Version = $Matches[1]
}
if ($Version -notmatch '^\d+\.\d+\.\d+$') { throw 'Version must be X.Y.Z' }
if (-not [Environment]::Is64BitOperatingSystem -or $env:PROCESSOR_ARCHITECTURE -ne 'AMD64') {
    throw 'Build requires x64 Windows'
}

$build = Join-Path $root 'build/win'
$stage = Join-Path $build 'stage'
$iscc = Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6/ISCC.exe'
if (-not (Test-Path $iscc)) { throw 'Install Inno Setup 6 before building' }
New-Item -ItemType Directory -Force $build | Out-Null

# Reuse the verified Windows uv asset pin; never execute an unchecked download.
$manifest = Get-Content "$root/vc_worker/engine-manifest.json" -Raw | ConvertFrom-Json
$pin = $manifest.platforms.'win-x64'.uv
$archive = Join-Path $build 'uv.zip'
Invoke-WebRequest -Uri $pin.url -OutFile $archive
if ((Get-FileHash $archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $pin.sha256) {
    throw 'uv SHA256 mismatch'
}
$uvDir = Join-Path $build 'uv'
if (Test-Path $uvDir) { Remove-Item $uvDir -Recurse -Force }
Expand-Archive $archive $uvDir
$uv = Join-Path $uvDir 'uv.exe'

# Pinned uv carries SHA256 pins for its managed CPython downloads and checks
# them before making the interpreter available. App Python matches mac PYVER.
$pythonDir = Join-Path $build 'python'
if (Test-Path $pythonDir) { Remove-Item $pythonDir -Recurse -Force }
& $uv python install 3.13 --install-dir $pythonDir --no-config --no-progress
if ($LASTEXITCODE -ne 0) { throw 'Portable Python installation failed' }
$runtime = Get-ChildItem $pythonDir -Directory -Filter 'cpython-3.13.*-windows-x86_64-none' |
    Sort-Object Name | Select-Object -Last 1
if (-not $runtime) { throw 'Portable Python 3.13 x64 not found' }
if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
New-Item -ItemType Directory -Force $stage | Out-Null
Copy-Item $runtime.FullName "$stage/python" -Recurse
$python = Join-Path $stage 'python/python.exe'
Remove-Item "$stage/python/EXTERNALLY-MANAGED", "$stage/python/Lib/EXTERNALLY-MANAGED" -Force -ErrorAction SilentlyContinue
& $uv pip install --python $python --no-config -r "$root/scripts/requirements-app.txt"
if ($LASTEXITCODE -ne 0) { throw 'App dependency installation failed' }

New-Item -ItemType Directory -Force "$stage/src", "$stage/macos" | Out-Null
Copy-Item "$root/src/ai_voice" "$stage/src/ai_voice" -Recurse
Copy-Item "$root/macos/desktop" "$stage/macos/desktop" -Recurse
Copy-Item "$root/vc_worker" "$stage/vc_worker" -Recurse
Copy-Item "$root/config.example.toml", "$root/windows/AppIcon.ico" $stage
Get-ChildItem "$stage/src", "$stage/vc_worker" -Directory -Recurse -Filter '__pycache__' |
    Remove-Item -Recurse -Force

# Relative paths survive installation at any location. import site is needed
# for pywebview/pythonnet and dependency .pth files; no global PYTHONPATH.
@'
python313.zip
.
DLLs
Lib
Lib\site-packages
..\src
import site
'@ | Set-Content "$stage/python/python313._pth" -Encoding ascii
& "$stage/python/python.exe" -c "import socket, ssl, sqlite3, ctypes, ai_voice.winshell"
if ($LASTEXITCODE -ne 0) { throw 'Portable Python runtime import check failed' }
if (-not (Test-Path "$stage/python/pythonw.exe")) { throw 'pythonw.exe missing' }

& $iscc "/DAppVersion=$Version" "/DSourceDir=$stage" "/DOutputDir=$build" "$root/windows/installer.iss"
if ($LASTEXITCODE -ne 0) { throw 'Inno Setup compilation failed' }
$setup = Join-Path $build "AI-Voice-Setup-$Version.exe"
if (-not (Test-Path $setup)) { throw 'Installer output missing' }
Write-Output $setup
