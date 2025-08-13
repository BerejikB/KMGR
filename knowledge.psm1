function Build-RepoPack {
  [CmdletBinding()]
  param(
    [string]$Repo,
    [ValidateRange(1,4096)][int]$MaxPackMB = 1024,
    [string[]] $Include = @('*.md','*.txt','*.rst','*.py','*.ps1','*.psm1','*.cs','*.cpp','*.h','*.js','*.ts','*.tsx','*.json','*.yaml','*.yml','*.ini','*.toml','*.cfg','*.sql','*.sh','*.bat'),
    [string[]] $ExcludeDirs = @('.git','.venv','node_modules','dist','build','.idea','.vscode','.vs','__pycache__')
  )
  $step = 'Build-RepoPack'
  try {
    $repoPath = Get-RepoPath -Repo $Repo
    $alias = Get-RepoAlias -Repo $Repo
    if (-not (Test-ReadableDir $repoPath)) { throw "Repo not readable: $repoPath" }
    if (-not (Test-WritableDir $script:PACKS_DIR)) { throw "Packs dir not writable: $script:PACKS_DIR" }
    $pack = Get-PackPath -Alias $alias

    $before = (Test-Path $pack) ? (Get-Item $pack).Length : 0

    Invoke-WithRetry -Retries 3 -DelayMs 200 -ScriptBlock {
      New-KnowledgePack -RepoPath $repoPath -OutFile $pack -Include $Include -ExcludeDirs $ExcludeDirs
    } | Out-Null

    if (-not (Test-Path $pack)) { throw "Pack was not created: $pack" }
    $after = (Get-Item $pack).Length
    $maxBytes = 1MB * $MaxPackMB
    if ($after -gt $maxBytes) { throw "Pack exceeds MaxPackMB=$MaxPackMB ($after bytes)" }

    Write-KLog "$step OK: $pack size=$after"
    return Make-Result $true $step "Pack ready ($([Math]::Round($after/1MB,2)) MB)" @{ Pack=$pack; Repo=$repoPath; Alias=$alias; SizeBytes=$after; PrevBytes=$before }
  } catch {
    Write-KLog "$step FAIL: $($_.Exception.Message)"
    return Make-Result $false $step $_.Exception.Message
  }
}

function Append-Chat {
  [CmdletBinding()]
  param(
    [Parameter(Mandatory)][ValidateSet('system','user','assistant','tool')] [string]$Role,
    [Parameter(Mandatory)][ValidateNotNullOrEmpty()] [string]$Content,
    [string]$Repo,
    [switch]$Dedup
  )
  $step = 'Append-Chat'
  try {
    if ($Content.Length -gt 2MB) { throw "Content too large (>2MB). Provide a summary or chunk." }
    $alias = Get-RepoAlias -Repo $Repo
    $pack = Get-PackPath -Alias $alias
    if (-not (Test-Path $pack)) {
      $r = Build-RepoPack -Repo $Repo
      if (-not $r.Ok) { throw "Cannot append; pack missing and rebuild failed: $($r.Summary)" }
      $pack = $r.Data.Pack
    }

    $pre = (Get-Item $pack).Length
    Invoke-WithRetry -Retries 3 -DelayMs 150 -ScriptBlock {
      Add-ChatToPack -PackFile $pack -Role $Role -Content $Content -DedupByHash:$Dedup.IsPresent
    } | Out-Null
    $post = (Get-Item $pack).Length
    if ($post -le $pre) { throw "ZIP size did not increase; chat may not have appended (dedup?)" }

    Write-KLog "$step OK: role=$Role sizeDelta=$($post-$pre)"
    return Make-Result $true $step "Appended ($Role)" @{ Pack=$pack; DeltaBytes=($post-$pre) }
  } catch {
    Write-KLog "$step FAIL: $($_.Exception.Message)"
    return Make-Result $false $step $_.Exception.Message
  }
}

function Export-RepoContext {
  [CmdletBinding()]
  param(
    [Parameter(Mandatory)][ValidateNotNullOrEmpty()] [string]$Query,
    [string]$Repo,
    [ValidateRange(1024,10485760)][int]$MaxBytes = 120000,
    [string]$OutFile
  )
  $step = 'Export-RepoContext'
  try {
    $alias = Get-RepoAlias -Repo $Repo
    $pack = Get-PackPath -Alias $alias
    if (-not (Test-Path $pack)) {
      $r = Build-RepoPack -Repo $Repo
      if (-not $r.Ok) { throw "Cannot export; pack missing and rebuild failed: $($r.Summary)" }
      $pack = $r.Data.Pack
    }

    if (-not $OutFile) { $OutFile = Join-Path $script:SCRATCH 'context_payload.txt' }
    $outDir = Split-Path -Parent $OutFile
    if (-not (Test-WritableDir $outDir)) { throw "Output dir not writable: $outDir" }

    # Atomic write
    $tmp = "$OutFile.tmp"
    Invoke-WithRetry -Retries 3 -DelayMs 150 -ScriptBlock {
      $txt = Get-KnowledgeSnippets -PackFile $pack -Query $Query -MaxBytes $MaxBytes
      [IO.File]::WriteAllText($tmp, $txt, [Text.UTF8Encoding]::UTF8)
    } | Out-Null

    if (-not (Test-Path $tmp)) { throw "Temp output not created" }
    if ((Get-Item $tmp).Length -le 0) { throw "Export produced empty context (tighten query or rebuild pack)" }

    Move-Item -Force -Path $tmp -Destination $OutFile
    Write-KLog "$step OK: '$Query' -> $OutFile ($(Get-Item $OutFile).Length bytes)"
    return Make-Result $true $step "Context exported" @{ Pack=$pack; OutFile=$OutFile; Bytes=(Get-Item $OutFile).Length }
  } catch {
    Write-KLog "$step FAIL: $($_.Exception.Message)"
    return Make-Result $false $step $_.Exception.Message
  } finally {
    if ($tmp -and (Test-Path $tmp)) { Remove-Item $tmp -Force -ErrorAction SilentlyContinue }
  }
}
