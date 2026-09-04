#!/usr/bin/env bash
set -euo pipefail

foam_bashrc=/opt/OpenFOAM/OpenFOAM-v2006/etc/bashrc
if [[ ! -f "$foam_bashrc" ]]; then
    echo "ERROR: native ESI v2006 bashrc is unavailable: $foam_bashrc" >&2
    exit 64
fi

# ESI's setup scripts interpret positional arguments as extra configuration
# files.  Preserve the container command but source the runtime with none.
command_args=("$@")
set --
# The ESI v2006 bashrc reads variables before assigning them and uses
# non-zero returns while probing optional components.  Source it outside the
# entrypoint's strict mode, then restore that mode for the remaining checks.
set +e +u
source "$foam_bashrc"
set -euo pipefail
set -- "${command_args[@]}"
case "${WM_PROJECT_VERSION:-}" in
    v2006|2006) ;;
    *)
        echo "ERROR: expected ESI/OpenCFD OpenFOAM v2006, found ${WM_PROJECT_VERSION:-unset}." >&2
        exit 64
        ;;
esac

if [[ -n "${FOAMAGENT_OPENFOAM_TARGET:-}" && "${FOAMAGENT_OPENFOAM_TARGET}" != "esi-v2006" ]]; then
    echo "ERROR: esi-v2006 image cannot run target ${FOAMAGENT_OPENFOAM_TARGET}." >&2
    exit 64
fi
export FOAMAGENT_OPENFOAM_TARGET=esi-v2006

if [[ -f /opt/conda/etc/profile.d/conda.sh ]]; then
    source /opt/conda/etc/profile.d/conda.sh
    conda activate FoamAgent >/dev/null 2>&1 || true
fi

exec "$@"
