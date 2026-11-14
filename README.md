# Retrofit Evidence 0.2.0

A single-operator workbench for public school retrofit research. Review source pages, publish a collection, search qualified evidence, and export cited drafts.

This distribution adds local launchers and authenticated HTTPS deployment to the Factory v0.1.5 candidate. The core application source matches the verified Factory hashes. Hosted generation starts disabled, and lexical search works without a provider account. The existing model-fetch utility supports pinned assets for dense and hybrid retrieval.

## Local use

- **Windows on this machine.** The WSL2 shortcut opens the existing workbench at http://127.0.0.1:8765.
- **Portable Windows.** The desktop ZIP includes a launcher for Docker running Linux containers.
- **macOS Intel and Apple Silicon.** The corresponding desktop archives include launchers and Linux images for Docker Desktop. Actual Mac installation is unverified.
- **Linux.** The desktop archive works with Docker Engine. The amd64 installation passed on Ubuntu under WSL2 with Windows Chrome. Both amd64 and arm64 passed the same distribution integration test on native Ubuntu runners.

Each portable installation stores data in a Docker volume. Keep the volume when updating. All desktop ports bind to 127.0.0.1. The existing Windows workbench and portable desktop container use separate stores.

## Web

The HTTPS stack uses Caddy, one operator account, a private application container, and a persistent volume. The stack passed local integration checks. A persistent web deployment requires an accessible server and HTTPS hostname. See [deployment](deploy/DEPLOY.md).

Factory verification and current installation checks are in [evidence](evidence/). Qualified source admission, independent application acceptance and paid generation are separate from distribution tests.
