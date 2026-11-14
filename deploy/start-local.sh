#!/usr/bin/env bash
set -euo pipefail

name=retrofit-evidence-desktop
image=retrofit-evidence:0.2.0
port=${RETROFIT_LOCAL_PORT:-8765}
origin="http://127.0.0.1:$port"
root=$(cd -- "$(dirname -- "$0")/.." && pwd)
action=${1:-start}
if [[ $action != start && $action != stop ]]; then
    echo "Usage: $0 [start|stop]" >&2
    exit 2
fi
docker info >/dev/null
if [[ $action == stop ]]; then
    docker stop "$name" >/dev/null
    exit
fi
if ! docker image inspect "$image" >/dev/null 2>&1; then
    case "$(docker info --format '{{.Architecture}}')" in
        arm64|aarch64) arch=arm64 ;;
        amd64|x86_64) arch=amd64 ;;
        *) echo "A Linux amd64 or arm64 container runtime is required." >&2; exit 1 ;;
    esac
    archive="$root/retrofit-evidence-linux-$arch.tar.gz"
    if [[ -f "$archive" ]]; then
        docker load --input "$archive"
    else
        docker build --tag "$image" "$root"
    fi
fi
if docker container inspect "$name" >/dev/null 2>&1; then
    docker start "$name" >/dev/null
else
    docker run -d --name "$name" --env "RETROFIT_LOCAL_PORT=$port" --publish "127.0.0.1:$port:8765" \
        --mount type=volume,source=retrofit-evidence-desktop,target=/data \
        --read-only --tmpfs /tmp:rw,noexec,nosuid,size=256m --cap-drop ALL \
        --security-opt no-new-privileges --pids-limit 128 "$image" >/dev/null
fi
for ((attempt=0; attempt<60; attempt++)); do
    if curl -fsS --max-time 1 "$origin/" | grep -q '<title>Retrofit Evidence'; then
        printf '%s\n' "$origin"
        case "$(uname -s)" in
            Darwin) open "$origin" ;;
            Linux) if [[ -n ${DISPLAY:-}${WAYLAND_DISPLAY:-} ]] && command -v xdg-open >/dev/null; then xdg-open "$origin"; fi ;;
        esac
        exit
    fi
    sleep 1
done
docker logs --tail 25 "$name" >&2
exit 1
