#!/bin/sh
set -eu
# Build source stays beside this script. Never overwrites an existing checkout.
cd "$(dirname "$0")"
COMMIT=15bbf141eb2e17b577fa802ed0d12157f97fcf44
[ ! -e source ] || { echo 'source already exists; inspect it before rebuilding' >&2; exit 1; }
git clone --no-checkout https://github.com/donetick/donetick.git source
git -C source checkout --detach "$COMMIT"
git -C source apply --check ../donetick-cas2.patch
git -C source apply ../donetick-cas2.patch
docker build --build-arg VERSION=v0.1.79-household-planner1 --build-arg COMMIT="$COMMIT" \
  -t household-planner-donetick:0.1.0 source
