param(
  [ValidateSet('Start','Health','Chunk','Stat','Lines')]
  [string]$Action = 'Health',
  [int]$Port = 17777,
  [string]$Roots = 'K:\GOOSE',
  [string]$Path,
  [int]$Offset = 0,
  [int]$Length = 1024,
  [int]$StartLine = 1,
  [int]$EndLine = 200
)

$ErrorActionPreference = 'Stop'

function Get-BaseUrl {
  param([int]$Port)
  return "http://localhost:$Port"
}

function Ensure-Server {
  param([int]$Port, [string]$Roots)
  $env:FILE_STREAM_PORT = $Port
  $env:FILE_STREAM_ROOTS = $Roots
  $env:FILE_STREAM_MAX_CHUNK = 65536
  try {
    $health = Invoke-WebRequest -UseBasicParsing -Uri ("http://localhost:{0}/health" -f $Port) -TimeoutSec 1 | Select-Object -ExpandProperty Content
    if ($health) { return $true }
  } catch {}
  Start-Process -FilePath 'node' -ArgumentList 'K:\GOOSE\KMGR\filestream\file-stream-server.mjs' -WorkingDirectory 'K:\GOOSE' -WindowStyle Hidden | Out-Null
  Start-Sleep -Milliseconds 1200
  try {
    $health = Invoke-WebRequest -UseBasicParsing -Uri ("http://localhost:{0}/health" -f $Port) -TimeoutSec 2 | Select-Object -ExpandProperty Content
    return [bool]$health
  } catch {
    return $false
  }
}

function Get-Url {
  param([string]$Path, [int]$Port, [string]$Endpoint)
  $base = Get-BaseUrl -Port $Port
  return "$base/$Endpoint?path=$([uri]::EscapeDataString($Path))"
}

switch ($Action) {
  'Start' {
    if (Ensure-Server -Port $Port -Roots $Roots) {
      (Invoke-WebRequest -UseBasicParsing -Uri ("http://localhost:{0}/health" -f $Port)).Content | Write-Output
    } else {
      Write-Error 'Failed to start file stream server'
    }
    break
  }
  'Health' {
    (Invoke-WebRequest -UseBasicParsing -Uri ("http://localhost:{0}/health" -f $Port)).Content | Write-Output
    break
  }
  'Stat' {
    if (-not $Path) { throw 'Path is required for Stat' }
    $u = (Get-Url -Path $Path -Port $Port -Endpoint 'stat')
    (Invoke-WebRequest -UseBasicParsing -Uri $u).Content | Write-Output
    break
  }
  'Chunk' {
    if (-not $Path) { throw 'Path is required for Chunk' }
    $u = (Get-Url -Path $Path -Port $Port -Endpoint 'chunk') + "&offset=$Offset&length=$Length"
    (Invoke-WebRequest -UseBasicParsing -Uri $u).Content | Write-Output
    break
  }
  'Lines' {
    if (-not $Path) { throw 'Path is required for Lines' }
    $u = (Get-Url -Path $Path -Port $Port -Endpoint 'lines') + "&start=$StartLine&end=$EndLine"
    (Invoke-WebRequest -UseBasicParsing -Uri $u).Content | Write-Output
    break
  }
  default { throw "Unknown Action: $Action" }
}
