<#
.SYNOPSIS
  Publish the built .vsix to a private feed folder (for example a network share) and write updates.json.
.EXAMPLE
  npm run package; .\scripts\publish-private.ps1 -FeedDir \\fileserver\ent-extensions
#>
param(
  [Parameter(Mandatory = $true)][string]$FeedDir,
  [string]$Vsix
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$pkg = Get-Content (Join-Path $root 'package.json') -Raw | ConvertFrom-Json
if (-not $Vsix) { $Vsix = Join-Path $root ("dist\{0}-{1}.vsix" -f $pkg.name, $pkg.version) }
if (-not (Test-Path $Vsix)) { throw "VSIX not found: $Vsix. Run 'npm run package' first." }
New-Item -ItemType Directory -Force -Path $FeedDir | Out-Null
$name = Split-Path -Leaf $Vsix
Copy-Item $Vsix (Join-Path $FeedDir $name) -Force
# Only move "latest" forward: never let an older build overwrite a newer feed entry.
$feedFile = Join-Path $FeedDir 'updates.json'
$version = [version]($pkg.version -replace '[-+].*$', '')
if (Test-Path $feedFile) {
  $cur = Get-Content $feedFile -Raw | ConvertFrom-Json
  if ($cur.latest -and ([version]($cur.latest -replace '[-+].*$', '')) -gt $version) {
    Write-Warning "Feed already has newer version $($cur.latest); copied the file but left updates.json unchanged."
    return
  }
}
$json = [ordered]@{ latest = $pkg.version; vsix = $name } | ConvertTo-Json
[System.IO.File]::WriteAllText($feedFile, $json, (New-Object System.Text.UTF8Encoding($false)))
Write-Host "Published $name to $FeedDir (latest = $($pkg.version))"
Write-Host "Point developers at it with the VS Code setting: ent.updateFeedUrl = $FeedDir"
