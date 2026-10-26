#!/usr/bin/env bash
# Build the scaffold environments used for trace collection (plan §4) with
# the versions pinned on deep8 (2026-10-25). Each scaffold gets its own venv
# because Harbor needs Python >= 3.12 while R2E-Gym pins its own lockfile.
# The trace tools themselves run in venv-r2e.
#
#   $ENV_ROOT/R2E-Gym      R2E-Gym checkout at R2E_COMMIT
#   $ENV_ROOT/venv-r2e     R2E-Gym from its uv.lock (py3.10)  W1, W5
#   $ENV_ROOT/venv-mini    mini-swe-agent (py3.10)            W2
#   $ENV_ROOT/venv-harbor  Harbor (py3.12)                    W3
#   $ENV_ROOT/VERSIONS     recorded into each run's manifest
#
# usage: [ENV_ROOT=~/rollout-env] setup_env.sh
set -euo pipefail
ENV_ROOT=${ENV_ROOT:-$HOME/rollout-env}
R2E_COMMIT=0d94c4eb9431cd195c55a7ea3abd54006c9a1735
MINI_VERSION=2.4.6
HARBOR_VERSION=0.24.0
VLLM_IMAGE="vllm/vllm-openai:v0.28.0 sha256:61fc8a896b0a4fbbbdc063bc4b0dbc25ce98e02b5050c24aeb7830ac02039b14"

command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH=$HOME/.local/bin:$PATH
# Keep package downloads gentle on a shared link.
export UV_CONCURRENT_DOWNLOADS=${UV_CONCURRENT_DOWNLOADS:-2}

mkdir -p "$ENV_ROOT"
cd "$ENV_ROOT"

[ -d R2E-Gym ] || git clone -q https://github.com/R2E-Gym/R2E-Gym.git
git -C R2E-Gym checkout -q "$R2E_COMMIT"
# `uv pip install -e` spends a long time resolving R2E-Gym's loose
# dependencies; its lockfile installs the tested set directly.
(cd R2E-Gym && UV_PROJECT_ENVIRONMENT="$ENV_ROOT/venv-r2e" uv sync --frozen --no-dev -q -p 3.10)
VIRTUAL_ENV=venv-r2e uv pip install -q --no-deps -e ./R2E-Gym
venv-r2e/bin/python -c "import r2egym, docker, aiohttp, pyarrow"

[ -d venv-mini ] || uv venv -q -p 3.10 venv-mini
VIRTUAL_ENV=venv-mini uv pip install -q "mini-swe-agent==$MINI_VERSION"

[ -d venv-harbor ] || uv venv -q -p 3.12 venv-harbor
VIRTUAL_ENV=venv-harbor uv pip install -q "harbor==$HARBOR_VERSION"

cat > VERSIONS <<EOF
R2E-Gym $R2E_COMMIT (uv.lock, py3.10)
mini-swe-agent $MINI_VERSION (py3.10)
harbor $HARBOR_VERSION (py$(venv-harbor/bin/python -V | cut -d' ' -f2))
$VLLM_IMAGE
EOF
cat VERSIONS
