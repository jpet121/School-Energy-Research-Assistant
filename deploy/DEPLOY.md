# Deploy the single-operator web workbench

Use a Linux host with Docker Engine and Compose. Point an HTTPS hostname at the host and permit inbound HTTPS and HTTP for Caddy certificate provisioning.

Build the image with `docker build -t retrofit-evidence:0.2.0 .` or load the matching bundled image. On arm64, exercise the parser isolation and workflow on the host before accepting uploads.

Set `RETROFIT_DOMAIN` to the hostname without a scheme. Generate a random 48-character hex password. Store it outside the checkout in a file readable by container UID 10001 and set `RETROFIT_PASSWORD_FILE` to its absolute path. Use `docker compose -f deploy/compose.web.yaml up -d`. Sign in with user `operator`. Keep the application port private to the Compose network.

Stop with `docker compose -f deploy/compose.web.yaml down`. Back up the entire workspace volume while the application is stopped. Preserve the workspace and TLS volumes across upgrades. Rotate the password and restart the app if access changes.

`verify-install.py` uses a real PDF, document review, search, and draft export on a disposable store. `verify-web.sh` starts a local HTTPS test deployment. Run deployment verification on a disposable store.
