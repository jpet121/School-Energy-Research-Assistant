set -euo pipefail
secret=$(mktemp -d /tmp/retrofit-web-test.XXXXXXXX)
chmod 0711 "$secret"
openssl rand -hex 24 > "$secret/password"
chown 10001 "$secret/password"
chmod 0400 "$secret/password"
root=$(pwd)
export RETROFIT_PASSWORD_FILE="$secret/password"
export RETROFIT_DOMAIN=localhost:9443
cat > "$secret/test.yaml" <<'YAML'
services:
  edge:
    ports: !override
      - "127.0.0.1:9443:9443"
    volumes:
      - type: bind
        source: ${RETROFIT_TEST_CADDYFILE:?}
        target: /etc/caddy/Caddyfile
        read_only: true
YAML
export RETROFIT_TEST_CADDYFILE="$secret/Caddyfile"
sed '/{\$RETROFIT_DOMAIN} {/a\    tls internal' "$root/deploy/Caddyfile" > "$RETROFIT_TEST_CADDYFILE"
docker compose -p retrofit-test-v020 -f deploy/compose.web.yaml -f "$secret/test.yaml" config --quiet
docker compose -p retrofit-test-v020 -f deploy/compose.web.yaml -f "$secret/test.yaml" up -d
printf '%s\n' "$secret" > /tmp/retrofit-v020-web-test-dir
