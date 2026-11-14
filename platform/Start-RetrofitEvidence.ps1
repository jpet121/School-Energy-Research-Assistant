[CmdletBinding()]
param([switch]$Stop, [switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
$name = 'retrofit-evidence-desktop'
$image = 'retrofit-evidence:0.2.0'
$root = if (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'Dockerfile')) {
    $PSScriptRoot
} else {
    Split-Path -Parent $PSScriptRoot
}
$port = if ($env:RETROFIT_LOCAL_PORT) { [int]$env:RETROFIT_LOCAL_PORT } else { 8765 }
$origin = "http://127.0.0.1:$port"
function Invoke-Docker {
    & docker @args
    if ($LASTEXITCODE -ne 0) { throw 'Docker could not complete the Retrofit Evidence action.' }
}
$engine = Invoke-Docker info --format '{{.OSType}}'
if ($engine -ne 'linux') { throw 'Switch Docker to Linux containers.' }
if ($Stop) {
    Invoke-Docker stop $name | Out-Null
    exit 0
}
& docker image inspect $image 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    $target = Invoke-Docker info --format '{{.Architecture}}'
    switch -Regex ($target) {
        '^(x86_64|amd64)$' { $arch = 'amd64' }
        '^(aarch64|arm64)$' { $arch = 'arm64' }
        default { throw "Unsupported Docker engine architecture $target." }
    }
    $archive = Join-Path $root "retrofit-evidence-linux-$arch.tar.gz"
    if (Test-Path -LiteralPath $archive) {
        Invoke-Docker load --input $archive
    } else {
        Invoke-Docker build --tag $image $root
    }
}
& docker container inspect $name 2>$null | Out-Null
if ($LASTEXITCODE -eq 0) {
    Invoke-Docker start $name | Out-Null
} else {
    $run = @('run', '-d', '--name', $name, '--env', "RETROFIT_LOCAL_PORT=$port", '--publish', "127.0.0.1:${port}:8765",
        '--mount', 'type=volume,source=retrofit-evidence-desktop,target=/data',
        '--read-only', '--tmpfs', '/tmp:rw,noexec,nosuid,size=256m', '--cap-drop', 'ALL',
        '--security-opt', 'no-new-privileges', '--pids-limit', '128', $image)
    Invoke-Docker @run | Out-Null
}
for ($i = 0; $i -lt 60; $i++) {
    $page = $null
    try { $page = Invoke-WebRequest -Uri "$origin/" -UseBasicParsing -TimeoutSec 1 }
    catch { Start-Sleep -Seconds 1 }
    if ($page -and $page.Content -match '<title>Retrofit Evidence') {
        Write-Output $origin
        if (-not $NoBrowser) { Start-Process -FilePath $origin }
        exit 0
    }
}
Invoke-Docker logs --tail 25 $name
throw 'Retrofit Evidence did not become ready.'
