# Distribution verification

- The source files match every Factory v0.1.5 source hash.
- Windows using WSL. The existing local workbench launches with parser isolation active and hosted generation disabled. Chrome passed 11 browser checks on the amd64 Docker distribution, including PDF upload, keyboard navigation, 200% and 400% reflow, original-region inspection and draft downloads.
- Linux amd64 and arm64. Native Ubuntu runners independently built the runtime images and passed loopback authorization, sandbox probe, source extraction and rendering, review and publication, lexical search, draft creation and export. Evidence is in [GitHub Actions run 43625888169](https://github.com/jpet121/School-Energy-Research-Assistant/actions/runs/43625888169).
- The arm64 isolation probe failed under QEMU user-mode emulation on the Windows host. The native arm64 test passed with the same parser and resource policy. The Mac launchers still need a Mac with a Linux VM or container engine.
- Authenticated web preview. HTTPS sign-in and protection of static assets, API origin and token checks, real sandboxed PDF workflow and draft export passed on a disposable local Compose instance.
- The Windows user store has four staged starter sources with verified original hashes. Independent application acceptance and publication of a qualified starter corpus remain pending.

The distribution wraps the existing single-user workbench. A permanent HTTPS server, native app bundles, and Mac hardware validation remain pending. No paid provider calls were made.
