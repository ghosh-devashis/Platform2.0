#!/usr/bin/env bash
# Deploy gate (POL-09): only signed images with build provenance may be deployed.
#   policy/tools/verify_image.sh <registry/org/repo@sha256:digest> <github-org/repo>
# Verifies, against the image digest (never a mutable tag):
#   1. the cosign (Sigstore keyless) signature was made by the repo's GitHub Actions workflow;
#   2. the SLSA build-provenance attestation exists for the same repo.
# Needs: cosign, and the GitHub CLI (gh) for the attestation check. Exits non-zero if anything fails.
set -euo pipefail
image="${1:?usage: verify_image.sh <image@sha256:digest> <github-org/repo>}"
repo="${2:?usage: verify_image.sh <image@sha256:digest> <github-org/repo>}"
case "$image" in *@sha256:*) ;; *) echo "FAIL: use the image digest (name@sha256:...), not a tag."; exit 1 ;; esac

echo "Verifying signature of $image ..."
cosign verify "$image" \
  --certificate-identity-regexp "^https://github.com/${repo}/\.github/workflows/" \
  --certificate-oidc-issuer "https://token.actions.githubusercontent.com" >/dev/null
echo "OK  signature"

echo "Verifying build provenance ..."
gh attestation verify "oci://$image" --repo "$repo" >/dev/null
echo "OK  provenance"
echo "Image is signed and has provenance: safe to deploy."
