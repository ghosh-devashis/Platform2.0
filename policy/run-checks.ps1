# Run the policy bundle against an agent repo (Windows equivalent of run-checks.sh).
#   .\policy\run-checks.ps1 [-Fast] [-Target <dir>]
# -Fast: pre-commit subset; a missing tool is skipped with a warning. Without -Fast every tool is required.
param([switch]$Fast, [string]$Target = '.')
$here = $PSScriptRoot
$script:failed = $false

function Invoke-Step([string]$Name, [string]$Tool, [scriptblock]$Command) {
    if (-not (Get-Command $Tool -ErrorAction SilentlyContinue)) {
        if ($Fast) { Write-Host "SKIP  $Name ($Tool not installed)"; return }
        Write-Host "FAIL  $Name ($Tool is required but not installed)"; $script:failed = $true; return
    }
    & $Command
    if ($LASTEXITCODE -eq 0) { Write-Host "PASS  $Name" } else { Write-Host "FAIL  $Name"; $script:failed = $true }
}

Write-Host "Policy bundle $((Get-Content "$here/VERSION").Trim()) on $Target"
Invoke-Step 'secret scan (gitleaks)' 'gitleaks' { gitleaks detect --source $Target --config "$here/gitleaks.toml" --no-banner --redact }
Invoke-Step 'code rules (ENT-001..008)' 'semgrep' { semgrep scan --config "$here/semgrep" --error --quiet $Target }
if (Test-Path "$Target/agent.yaml") {
    Invoke-Step 'manifest rules (ENT-020..023)' 'conftest' { conftest test --policy "$here/opa" --data "$here/data" --namespace manifest "$Target/agent.yaml" }
    Invoke-Step 'manifest schema' 'ent-agent' { ent-agent manifest validate "$Target/agent.yaml" }
}
if (Test-Path "$Target/pyproject.toml") {
    Invoke-Step 'dependency allowlist (ENT-010..012)' 'conftest' { conftest test --policy "$here/opa" --data "$here/data" --namespace dependencies "$Target/pyproject.toml" }
}
Invoke-Step 'waivers (ENT-030..033)' 'python' { python "$here/tools/check_waivers.py" --root $Target }
Invoke-Step 'supported versions (ENT-060..062)' 'python' { python "$here/tools/check_versions.py" --root $Target }
if ((-not $Fast) -and (Test-Path "$Target/.github/workflows")) {
    Invoke-Step 'pipeline meta-check (ENT-040..043)' 'python' { python "$here/tools/check_pipeline.py" --root $Target }
}
if ($script:failed) { Write-Host 'Policy checks failed.'; exit 1 } else { Write-Host 'All policy checks passed.' }
