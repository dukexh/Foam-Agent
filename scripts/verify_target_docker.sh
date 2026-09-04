#!/usr/bin/env bash
# Verify that a pre-built target image starts its intended OpenFOAM runtime.
# This is intentionally a runtime smoke test only; it does not consume an LLM
# key or run a CFD case.
set -euo pipefail

target=${1:-foundation-v10}
image=${2:-foamagent:${target}}

case "$target" in
    foundation-v10)
        version_check='[ "${WM_PROJECT_VERSION:-}" = "10" ]'
        ;;
    esi-v2006)
        version_check='case "${WM_PROJECT_VERSION:-}" in v2006|2006) ;; *) exit 1 ;; esac'
        ;;
    *)
        echo "Usage: $0 [foundation-v10|esi-v2006] [image-tag]" >&2
        exit 64
        ;;
esac

docker run --rm \
    -e FOAMAGENT_SKIP_UPDATE=1 \
    "$image" \
    bash -lc "${version_check}; command -v blockMesh >/dev/null; python scripts/verify_target_corpus.py --target ${target}; printf 'runtime=%s\\n' \"\${WM_PROJECT_VERSION}\""
