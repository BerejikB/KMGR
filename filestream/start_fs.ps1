# Start File Stream Server
param(
    [int]$Port = 17777,
    [string]$Roots = "K:\GOOSE",
    [int]$MaxChunk = 65536,
    [int]$MaxLines = 400
)

$ErrorActionPreference = 'Stop'
$root = "K:\GOOSE"
$scratch = Join-Path $root 'scratch'
if (-not (Test-Path $scratch)) { New-Item -ItemType Directory -Force -Path $scratch | Out-Null }

$env:FILE_STREAM_PORT = $Port
$env:FILE_STREAM_ROOTS = $Roots
$env:FILE_STREAM_MAX_CHUNK = $MaxChunk
$env:FILE_STREAM_MAX_LINES = $MaxLines

$node = 'node'
$script = "K:\GOOSE\KMGR\filestream\file-stream-server.mjs"
$logOut = Join-Path $scratch 'filestream.log'
$logErr = Join-Path $scratch 'filestream.err'
$pidFile = Join-Path $scratch 'filestream.pid'

# Start in background, capture PID, redirect logs
$p = Start-Process -FilePath $node -ArgumentList $script -WindowStyle Hidden -RedirectStandardOutput $logOut -RedirectStandardError $logErr -PassThru
$p.Id | Out-File -Encoding ascii -FilePath $pidFile -Force
Write-Output "STARTED PID=$($p.Id) PORT=$Port ROOTS=$Roots"
