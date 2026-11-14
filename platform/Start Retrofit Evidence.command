#!/bin/bash
set -e
cd -- "$(dirname -- "$0")"
exec ./deploy/start-local.sh
