# Installs dist\DogMute-Setup.exe silently, runs it like a sign-in start would, checks the installed app works
# (page, engine restart, the sound helper hearing this PC), then uninstalls. The GitHub workflow runs it.
$ErrorActionPreference = "Stop"
$app = "$env:LOCALAPPDATA\Programs\DogMute"
$api = "http://127.0.0.1:8853/api"
function Wait-For($what, $secs, [scriptblock]$ok) {
    for ($i = 0; $i -lt $secs; $i++) {
        try { $s = Invoke-RestMethod "$api/state" -TimeoutSec 5; if (& $ok $s) { Write-Host "ok: $what"; return $s } } catch {}
        Start-Sleep 1
    }
    throw "timed out: $what (last: $($s | ConvertTo-Json -Depth 2 -Compress))"
}
function Post($path, $body) {
    Invoke-RestMethod -Method Post "$api/$path" -ContentType "application/json" -Body ($body | ConvertTo-Json -Compress)
}
try {
    Start-Process dist\DogMute-Setup.exe -ArgumentList "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/LOG=setup.log" -Wait
    if (-not (Test-Path "$app\DogMute.exe")) { throw "not installed" }
    if (-not (Test-Path "$app\_internal\bin\dogmute-listen.exe")) { throw "the sound helper wasn't installed" }
    $run = (Get-ItemProperty "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run").DogMute
    if ($run -notlike "*DogMute.exe*--background") { throw "no sign-in start: $run" }
    Write-Host "ok: installed, starts at sign-in ($run)"

    Start-Process "$app\DogMute.exe" -ArgumentList "--background"
    $s = Wait-For "the page is up" 120 { param($s) $s.platform -eq "windows" }
    $n = @(Get-Process DogMute).Count
    if ($n -lt 2) { throw "expected the tray and the engine, found $n DogMute processes" }
    Write-Host "ok: tray + engine running"
    Wait-For "speech model loaded" 900 { param($s) $s.stt -like "ready*" } | Out-Null

    Post "config" @{ path = "audio.target"; value = "all" } | Out-Null
    Wait-For "hearing every app's sound" 60 { param($s) $s.audio -eq "listening" } | Out-Null

    $engine = (Get-CimInstance Win32_Process -Filter "Name = 'DogMute.exe'" | Where-Object CommandLine -like "*--engine*").ProcessId
    Post "restart" @{} | Out-Null
    Start-Sleep 3
    Wait-For "the tray started the engine again" 120 { param($s) $s.platform -eq "windows" } | Out-Null
    $again = (Get-CimInstance Win32_Process -Filter "Name = 'DogMute.exe'" | Where-Object CommandLine -like "*--engine*").ProcessId
    if ($again -eq $engine) { throw "the engine didn't restart" }

    # a second start only shows the running one
    Start-Process "$app\DogMute.exe" -ArgumentList "--background" -Wait
    $n = @(Get-Process DogMute).Count
    if ($n -ne 2) { throw "a second DogMute started ($n processes)" }
    Write-Host "ok: one DogMute at a time"
} finally {
    Write-Host "---- dogmute.log"; Get-Content "$env:LOCALAPPDATA\DogMute\dogmute.log" -Tail 40 -ErrorAction SilentlyContinue
    Write-Host "---- tray.log"; Get-Content "$env:LOCALAPPDATA\DogMute\tray.log" -Tail 20 -ErrorAction SilentlyContinue
}
Start-Process "$app\unins000.exe" -ArgumentList "/VERYSILENT", "/SUPPRESSMSGBOXES" -Wait
Start-Sleep 3
if (Test-Path "$app\DogMute.exe") { throw "uninstall left DogMute.exe" }
if (Get-Process DogMute -ErrorAction SilentlyContinue) { throw "uninstall left DogMute running" }
if ((Get-ItemProperty "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run").DogMute) { throw "uninstall left the sign-in start" }
Write-Host "ok: uninstalled cleanly"
