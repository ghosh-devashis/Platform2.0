# Puts the portable Node.js and Terraform (installed in %LOCALAPPDATA%\Programs, no admin rights needed) on PATH
# for this PowerShell session.   Usage (repo root):  . .\harness\env.ps1
$programs = Join-Path $env:LOCALAPPDATA 'Programs'
$node = Get-ChildItem $programs -Directory -Filter 'node-v*-win-x64' -ErrorAction SilentlyContinue | Sort-Object Name -Descending | Select-Object -First 1
$tf = Get-ChildItem $programs -Directory -Filter 'terraform-*' -ErrorAction SilentlyContinue | Sort-Object Name -Descending | Select-Object -First 1
$extra = @($node, $tf) | Where-Object { $_ } | ForEach-Object { $_.FullName }
if ($extra) { $env:PATH = ($extra -join ';') + ';' + $env:PATH }
$env:VIRTUAL_ENV = $null
Write-Host ("node: {0}  npm: {1}  terraform: {2}" -f $(if (Get-Command node -ErrorAction SilentlyContinue) { node --version } else { 'missing' }), $(if (Get-Command npm.cmd -ErrorAction SilentlyContinue) { npm.cmd --version } else { 'missing' }), $(if (Get-Command terraform -ErrorAction SilentlyContinue) { (terraform version -json | ConvertFrom-Json).terraform_version } else { 'missing' }))
