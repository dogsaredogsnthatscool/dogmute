# Builds dist\DogMute-Setup.exe on Windows: the sound helper (MSVC), DogMute.exe (PyInstaller), the installer (Inno Setup).
# Needs Python 3.13, Visual Studio Build Tools with C++ (run it from a "Developer PowerShell", so cl.exe is found),
# and Inno Setup 6. The GitHub workflow (.github/workflows/windows.yml) runs exactly this.
#   powershell -ExecutionPolicy Bypass -File win\build.ps1 -Version 1.0.0
param([string]$Version = "0.0.0", [string]$Python = "py -3.13")
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot)

function Run($exe, [string[]]$a) { & $exe @a; if ($LASTEXITCODE) { throw "$exe $a failed ($LASTEXITCODE)" } }

Write-Host "1/4 sound helper"
New-Item -ItemType Directory -Force bin, build | Out-Null
Run cl @("/nologo", "/O2", "/EHsc", "/std:c++17", "/W3", "win\listen.cpp", "/Fo:build\", "/Fe:bin\dogmute-listen.exe",
         "ole32.lib", "shell32.lib", "version.lib")

Write-Host "2/4 Python and its packages"
if (-not (Test-Path .venv-build)) { Invoke-Expression "$Python -m venv .venv-build"; if ($LASTEXITCODE) { throw "venv" } }
$py = ".venv-build\Scripts\python.exe"
Run $py @("-m", "pip", "install", "-q", "--upgrade", "pip")
Run $py @("-m", "pip", "install", "-q", "-r", "requirements.txt", "pyinstaller==6.22.3")
Run $py @("-c", "from PIL import Image; Image.open('dogmute/static/icon.png').save('build/icon.ico', sizes=[(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)])")

Write-Host "3/4 DogMute.exe"
Run $py @("-m", "PyInstaller", "--noconfirm", "--clean", "--distpath", "dist", "--workpath", "build\pyi", "win\DogMute.spec")

Write-Host "4/4 installer"
# the graphics card's DLLs: the exact wheels from requirements-win-gpu.txt, checked against PyPI's SHA-256
$gpu = & $py -c @"
import json, re, urllib.request
for line in open('requirements-win-gpu.txt'):
    m = re.match(r'([\w-]+)==([\w.]+)', line)
    if m:
        d = json.load(urllib.request.urlopen(f'https://pypi.org/pypi/{m[1]}/{m[2]}/json'))
        f = next(f for f in d['urls'] if f['filename'].endswith('win_amd64.whl'))
        key = 'Cublas' if 'cublas' in m[1] else 'Cudnn'
        print(f'/D{key}Url={f["url"]}')
        print(f'/D{key}Sha={f["digests"]["sha256"]}')
"@
if ($LASTEXITCODE) { throw "PyPI lookup failed" }
$iscc = (Get-Command iscc -ErrorAction SilentlyContinue).Source
if (-not $iscc) { $iscc = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe" }
Run $iscc (@("/Q", "/DVersion=$Version") + $gpu + @("win\dogmute.iss"))
Get-Item dist\DogMute-Setup.exe | ForEach-Object { Write-Host ("dist\DogMute-Setup.exe  {0:N0} MB" -f ($_.Length / 1MB)) }
