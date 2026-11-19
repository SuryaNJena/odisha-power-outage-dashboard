#Requires -Version 5.1
<#
.SYNOPSIS
  Build and deploy the Odisha outage platform to the self-hosted server.

.DESCRIPTION
  Runs on the Windows dev machine and drives the Linux server over SSH:
    1. builds a static linux/amd64 Go backend binary locally
    2. packs the repository (no node_modules/.next/.git/out) and uploads it
    3. uploads the binary and the remote deploy script
    4. runs scripts/deploy_remote.sh on the server, which builds the Next.js
       static export inside a node:20-alpine container (the Windows sandbox
       cannot build it reliably), stages the app image and brings the compose
       stack up.

  Prerequisites: ssh host alias `ssh.356189.xyz` in ~/.ssh/config, Docker on
  the server, Go + tar on the local machine.

.PARAMETER NoUp
  Upload and build images but do not start the stack.

.PARAMETER ForceEnv
  Regenerate platform/.env (only do this before the postgres volume exists).

.EXAMPLE
  powershell -File scripts\deploy.ps1
#>
param(
  [switch]$NoUp,
  [switch]$ForceEnv
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$remote = 'ssh.356189.xyz'
$remoteDir = '~/odisha-platform'
$logDir = Join-Path $repo '.tmp'

function Step([string]$message) { Write-Host "==> $message" }

Push-Location $repo
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

try {
  if (-not (Test-Path platform\.env) -or $ForceEnv) {
    Step 'generating platform/.env'
    $pw = [guid]::NewGuid().ToString('N')
    Set-Content -Path platform\.env -Value "POSTGRES_PASSWORD=$pw`nAIRFLOW_ADMIN_PASSWORD=admin" -NoNewline
  }

  Step 'building Go backend (linux/amd64)'
  cmd /c ".tmp\go-build.cmd"
  if ($LASTEXITCODE -ne 0) { throw 'go build failed (see .tmp/go-build.txt)' }

  Step 'staging app data'
  New-Item -ItemType Directory -Force -Path platform\app\data\mart, platform\app\data\mappings | Out-Null
  Copy-Item data\mappings\* platform\app\data\mappings -Recurse -Force
  Copy-Item data\mart\outages.json platform\app\data\mart\outages.json -Force

  Step 'packing repository (git tree, ignores scratch/build dirs)'
  $pack = @"
@echo off
cd /d $repo
git add -A > .tmp\pack.log 2>&1
git write-tree > .tmp\tree.txt 2>&1
set /p TREE=<.tmp\tree.txt
git archive --format=tar.gz -o .tmp\src.tgz %TREE% >> .tmp\pack.log 2>&1
git reset > .tmp\pack-reset.log 2>&1
"@
  Set-Content -Path .tmp\pack.cmd -Value $pack -Encoding ASCII
  if (Test-Path .tmp\src.tgz) { Remove-Item .tmp\src.tgz -Force }
  cmd /c .tmp\pack.cmd
  if ($LASTEXITCODE -ne 0 -or -not (Test-Path .tmp\src.tgz)) {
    Get-Content .tmp\pack.log -Tail 30 -ErrorAction SilentlyContinue
    throw 'git archive pack failed'
  }

  Step 'creating remote directory'
  cmd /c "ssh -o BatchMode=yes $remote `"mkdir -p $remoteDir/platform/app`" > .tmp\remote-mkdir.log 2>&1"
  if ($LASTEXITCODE -ne 0) { Get-Content .tmp\remote-mkdir.log -Tail 20; throw 'remote mkdir failed' }

  Step 'uploading source'
  cmd /c "scp -o BatchMode=yes .tmp\src.tgz $remote`:/tmp/odisha-src.tgz > .tmp\upload.log 2>&1"
  if ($LASTEXITCODE -ne 0) { Get-Content .tmp\upload.log -Tail 20; throw 'scp source failed' }

  Step 'uploading binary + deploy script'
  cmd /c "scp -o BatchMode=yes .tmp\odisha-backend-linux $remote`:$remoteDir/platform/app/odisha-backend > .tmp\upload2.log 2>&1"
  if ($LASTEXITCODE -ne 0) { Get-Content .tmp\upload2.log -Tail 20; throw 'scp binary failed' }
  cmd /c "scp -o BatchMode=yes scripts\deploy_remote.sh $remote`:/tmp/deploy_remote.sh > .tmp\upload3.log 2>&1"
  if ($LASTEXITCODE -ne 0) { Get-Content .tmp\upload3.log -Tail 20; throw 'scp script failed' }
  cmd /c "scp -o BatchMode=yes platform\.env $remote`:$remoteDir/platform/.env > .tmp\upload4.log 2>&1"
  if ($LASTEXITCODE -ne 0) { Get-Content .tmp\upload4.log -Tail 20; throw 'scp env failed' }

  Step 'extracting source on server'
  cmd /c "ssh -o BatchMode=yes $remote `"tar -xzf /tmp/odisha-src.tgz -C $remoteDir --strip-components=1 && chmod +x /tmp/deploy_remote.sh`" > .tmp\extract.log 2>&1"
  if ($LASTEXITCODE -ne 0) { Get-Content .tmp\extract.log -Tail 30; throw 'extract failed' }

  if (-not $NoUp) {
    Step 'remote build + compose up (10-20 min on first run)'
    cmd /c "ssh -o BatchMode=yes $remote `"bash /tmp/deploy_remote.sh`" > .tmp\deploy.log 2>&1"
    if ($LASTEXITCODE -ne 0) { Get-Content .tmp\deploy.log -Tail 120; throw 'remote deploy failed' }
    Get-Content .tmp\deploy.log -Tail 40
  }

  Step 'health'
  cmd /c "ssh -o BatchMode=yes $remote `"curl -fsS localhost:3001/api/health; echo; curl -fsS localhost:3001/api/mapdata | head -c 300; echo`" 2>&1"
}
finally {
  Pop-Location
}
