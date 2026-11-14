#!/bin/bash
set -e
cd -- "$(dirname -- "$0")"
if [[ -d ../deploy && ! -d deploy ]]; then cd ..; fi
exec ./deploy/start-local.sh
