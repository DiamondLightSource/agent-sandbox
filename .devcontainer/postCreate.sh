#!/usr/bin/env bash
# postCreate: run the installer. Idempotent so devcontainer
# rebuilds re-establish the shadow without re-downloading Claude.
#
# --here is required: this is the sandbox's OWN devcontainer, so the
# checkout being developed is exactly what should be installed. Without
# it `install` defaults to the newest release tag and every rebuild would
# quietly replace the branch under test with the last release.
set -euo pipefail

bash install --here
