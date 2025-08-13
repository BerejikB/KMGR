Import-Module "$PSScriptRoot\knowledge.psm1" -Force

param(
  [string]$Repo # optional: alias or path; falls back to $Env:GOOSE_REPO or default
)

$pack = Build-RepoPack -Repo $Repo
Write-Host "Pack ready: $pack"
