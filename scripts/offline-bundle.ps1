# Build the etl+ image and pack it for a server without internet:
#   dist\etlplus-<version>-offline\ { etlplus-<version>.tar, docker-compose.yml, LOAD_AND_RUN.md }
# and the same folder as a .zip beside it. Run: powershell -ExecutionPolicy Bypass -File scripts\offline-bundle.ps1
# -FromImage REF packs an image built already (tagged as the version) instead of building.
param([string]$FromImage = "")
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$m = Select-String -Path "pyproject.toml" -Pattern '^version = "(.*)"' | Select-Object -First 1
$version = $m.Matches[0].Groups[1].Value
$image = "crestedcrane/etlplus:$version"
$out = Join-Path "dist" "etlplus-$version-offline"

if ($FromImage) {
    docker tag $FromImage $image
    if ($LASTEXITCODE -ne 0) { throw "docker tag failed" }
} else {
    Write-Host "Building $image ..."
    docker build -t $image .
    if ($LASTEXITCODE -ne 0) { throw "docker build failed" }
}

if (Test-Path $out) { Remove-Item -Recurse -Force $out -Confirm:$false }
New-Item -ItemType Directory -Force $out | Out-Null
Write-Host "Saving the image ..."
docker save $image -o (Join-Path $out "etlplus-$version.tar")
if ($LASTEXITCODE -ne 0) { throw "docker save failed" }

# the compose file without the build line: the server only loads the image
$compose = Get-Content "docker-compose.yml" | Where-Object { $_ -notmatch '^    build: \.$' }
[System.IO.File]::WriteAllLines((Join-Path (Resolve-Path $out) "docker-compose.yml"), [string[]]$compose)

$readme = @"
# etl+ $version, offline
1. Copy this folder to the server (Docker with the compose plugin installed).
2. ``docker load -i etlplus-$version.tar``
3. ``docker compose up -d`` in this folder.
4. Open http://localhost:8300 on the server (or ``ssh -L 8300:127.0.0.1:8300 <server>``) and choose demo data or your own data.
"@
[System.IO.File]::WriteAllText((Join-Path (Resolve-Path $out) "LOAD_AND_RUN.md"), $readme.Replace("`r`n", "`n") + "`n")

$zip = "$out.zip"
if (Test-Path $zip) { Remove-Item -Force $zip -Confirm:$false }
Compress-Archive -Path $out -DestinationPath $zip
Get-ChildItem $out, $zip | Select-Object Name, Length
Write-Host "Done: $zip"
