# Run the web app on a server

Use a Linux server with Docker Engine and Compose. Caddy accepts HTTPS requests. The app runs on the private container network. One operator can sign in.

## Start the app

1. Point a domain name at the server.
2. Open ports 80 and 443 for Caddy.
3. Build the app image with the first command below.
4. Set `RETROFIT_DOMAIN` to the domain name.
5. Generate a random password with 48 hexadecimal characters.
6. Save the password in a file outside the repository.
7. Give container user ID 10001 read access to the file.
8. Set `RETROFIT_PASSWORD_FILE` to the full path of the file.
9. Start Compose with the second command below.
10. Sign in at the server's HTTPS address as `operator`.

```bash
docker build -t retrofit-evidence:0.2.0 .
docker compose -f deploy/compose.web.yaml up -d
```

## Check and back up the app

Use a separate data store for the installation check. The check adds a sample PDF and creates a draft.

1. Run `deploy/verify-web.sh` to start the local HTTPS test.
2. Use `deploy/verify-install.py` to check the test store.
3. Stop the app with the command below.
4. Copy the Docker volume to a backup location.

```bash
docker compose -f deploy/compose.web.yaml down
```

Keep the workspace and certificate volumes when you update the app.
