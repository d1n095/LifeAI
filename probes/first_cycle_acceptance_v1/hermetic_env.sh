#!/bin/bash
# Source me. Hermetic, credential-free environment for every acceptance suite.
# - git can never see host config or credential helpers (temp HOME, GIT_CONFIG_GLOBAL=/dev/null, NOSYSTEM)
# - no token / askpass / agent variables; GitHub settings inside tests are synthetic ("fake-not-used")
# - no interactive prompts; PATH includes the backend venv (alembic etc.)
: "${ACCEPT_VENV:?set ACCEPT_VENV to the backend venv python}"
ACCEPT_HOME="$(mktemp -d -t accept-home)"
export HOME="$ACCEPT_HOME" XDG_CONFIG_HOME="$ACCEPT_HOME/xdg"
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_SYSTEM=/dev/null GIT_TERMINAL_PROMPT=0 GCM_INTERACTIVE=never
export GIT_AUTHOR_NAME=acceptance GIT_AUTHOR_EMAIL=acceptance@test.local GIT_COMMITTER_NAME=acceptance GIT_COMMITTER_EMAIL=acceptance@test.local
unset GH_TOKEN GITHUB_TOKEN GH_ENTERPRISE_TOKEN GIT_ASKPASS SSH_ASKPASS SSH_AUTH_SOCK GIT_CONFIG_COUNT GIT_CONFIG_PARAMETERS MAINAI_PUSH_TOKEN MAINAI_EFFECT_TOKEN
for v in $(env | grep -oE '^GIT_CONFIG_(KEY|VALUE)_[0-9]+' ); do unset "$v"; done
export PATH="$(dirname "$ACCEPT_VENV"):$PATH" PYTHONDONTWRITEBYTECODE=1
export REDIS_URL="${REDIS_URL:-redis://127.0.0.1:6390/0}"
