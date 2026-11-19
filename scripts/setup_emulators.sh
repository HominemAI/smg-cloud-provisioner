#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
PYTHON=${PYTHON:-.venv/bin/python}
"$PYTHON" -m pip install -r requirements-integration.txt
npm ci --prefix tests/integration --ignore-scripts --no-audit --no-fund
spec_dir=.cache/emulators/digitalocean-openapi
spec_commit=6ee1980060d9ec8ce9f9853edf22768ca61cdf43
if [ ! -d "$spec_dir/.git" ]; then
  git clone --no-checkout https://github.com/digitalocean/openapi.git "$spec_dir"
fi
git -C "$spec_dir" fetch --depth 1 origin "$spec_commit"
git -C "$spec_dir" checkout --detach "$spec_commit"
