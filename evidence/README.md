# Retrofit Evidence M1

A local, single-user browser workbench for public school retrofit research. Stage and review original PDFs, publish a verified collection, search qualified evidence, inspect the recorded page and edition, and export an immutable draft. Generation is disabled by default.

## Factory continuation candidate v0.1.5, not a release

See [current intake and continuation](evidence/factory-v0.1.5/REPORT.md), the
[246-row trace](evidence/factory-v0.1.5/trace.csv), and
[external handoff worksheets](evidence/factory-v0.1.5/worksheets/README.md).
Incoming v0.1.3 and issued v0.1.4 are preserved. Python and frontend now meet
the required 100% production line/branch gate with zero exclusions. That
automated gate does not establish qualified corpus/evaluator/human acceptance.
Formal Stage 6 promotion, M1 acceptance and D6 eligibility remain withheld.
The final ZIP verification sidecar records the separate fresh-extraction checks.

## Run locally

The tested server platform is Ubuntu 24.04 x86-64, including Ubuntu under WSL2, with Python 3.12, SQLite FTS5 and libseccomp. Use a Linux filesystem for the private data directory. For WSL, open the running app from Windows Chrome using the exact address printed by the launcher.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m retrofit --data /var/tmp/retrofit-evidence-local
```

If the distribution lacks Python's venv support, install its `python3-venv` package through the normal system package manager. The launcher creates a private data directory. If supplying an existing directory, set mode 0700. Open `http://127.0.0.1:8765` and keep the service running. Use `--port` for a different loopback port. Stop it with Ctrl+C and restart with the same data directory to reopen the collection and drafts.

The bundled JavaScript is built from `web/workbench.ts`. Rebuild and check it with Node.js and npm.

```sh
npm ci
npm run typecheck
npm run build
```

## Workbench

1. Choose a local public PDF and record the source, publication date or unknown reason, scope and pages with a use basis.
2. Open the staged source, run bounded extraction and compare the selected spans with the rendered original. Record a verified printed page label when it differs from the PDF navigation label or is missing from the PDF metadata. Table links identify cells, headings, units and mandatory notes. Leave unsupported or unverifiable regions unselected.
   After a parser-profile change, use **Check extraction configuration**.
   Changed configurations produce fresh, unreviewed extraction; unchanged
   configurations retain the saved review. Published snapshots stay immutable.
   **Open publisher source (new tab)** navigates only after an explicit click.
3. Prepare and publish a complete snapshot. A lexical snapshot runs without model assets. With optional pinned models, select a vector snapshot to enable dense and hybrid search.
4. Search, inspect the recorded evidence and attach a qualified note or draft manually. Save a revision and prepare the Markdown or JSON download. Withdrawal suppresses new use, and historical exports retain the saved associations with warnings.
5. Review parser controls, recorded retrieval attempts and cleanup impact under **Local controls**. Use **Evaluation** to open aggregate external reports.

## Local embedding assets

For real offline dense and hybrid retrieval, fetch the two pinned BGE candidates from their publisher's repositories. The script verifies SHA-256 before admitting each file to the model directory. Expect about 570 MB for both weights and tokenizers.

```sh
.venv/bin/python -m retrofit.fetch_models /var/tmp/retrofit-models
.venv/bin/python -m retrofit --data /var/tmp/retrofit-evidence-local --models /var/tmp/retrofit-models
```

The small and base weights are optional for lexical operation. The model versions, tokenizers, normalized vector settings and ranking policies are recorded in each prepared vector manifest. The model assets are distributed by BAAI. [Small model repository](https://huggingface.co/BAAI/bge-small-en-v1.5) · [Base model repository](https://huggingface.co/BAAI/bge-base-en-v1.5)

## Verification

```sh
coverage_dir="$(mktemp -d)"
export COVERAGE_FILE="$coverage_dir/.coverage"
RETROFIT_TEST_MODELS=/var/tmp/retrofit-models .venv/bin/python -m coverage run -m pytest
.venv/bin/python -m coverage combine
.venv/bin/python -m coverage report --show-missing
.venv/bin/python -m coverage json -o "$coverage_dir/coverage.json"
.venv/bin/python -m mypy retrofit
npm run typecheck
```

The Python report enforces the 100% statement and branch gate across the `retrofit` package. The suite records actual process execution for the evaluator CLI, loopback server and sandboxed parser tests. Chrome journeys run separately.

On WSL, set `RETROFIT_WINDOWS_PYTHON` to the Linux-visible path of native Windows Python with the pinned coverage and Pydantic versions installed. The platform tests verify the native launcher denial and native SQLite and filesystem persistence. Their observed arcs are merged into the report. The hosted-boundary tests use loopback HTTP and the real adapter. They use the public `cl100k_base` tokenizer asset, so supply a populated `TIKTOKEN_CACHE_DIR` for an offline test run. They need no real provider credentials.

Run browser verification with a **fresh test data directory**. In a second terminal with the test app running, set `RETROFIT_TEST_ORIGIN` to the exact loopback origin, `CHROMIUM_EXECUTABLE` to a local Chrome/Chromium path, and optionally `RETROFIT_BROWSER_EVIDENCE` to an output directory. Run `npm run test:browser`. The test uses actual file upload, HTTP APIs, PDF rendering and browser downloads. It writes only fixture data.

See [the completion ledger](.scratch/retrofit-evidence-m1/completion-ledger.md), [the case trace](.scratch/retrofit-evidence-m1/acceptance-trace.csv), and [verification and readiness](evidence/VERIFICATION.md). Deferred shared and native obligations are in [the milestone register](.scratch/retrofit-evidence-m1/deferred.md).

## Evaluation and hosted generation

[Evaluator instructions](docs/evaluation.md) cover source coverage, the frozen split and configuration, external custody, retrieval execution and per-repetition reporting. Fixture tests check behavior and arithmetic. Full M1 application acceptance needs the applicable real corpus, authored benchmark, engineering evidence and human evaluation.

The [historical acceptance report](evidence/acceptance-v1.0.0/report.md) preserves the preceding review. The [current Factory report](evidence/factory-v0.1.4/REPORT.md) controls the latest engineering disposition. The [v0.1.2 engineering record](evidence/ENGINEERING.md) preserves the preceding measured source, logs and browser observations.

The default launch makes no paid calls. A future operator may supply `--hosted-policy` only under the accepted D3 process and after current account terms, rates, tokenizer and provider capability have been verified. See [hosted operation](docs/hosted.md). The browser independently requires D7 exact-input confirmation. Readiness and eligibility reports require separate owner acceptance and release decisions.

## Inputs and precedence

The compiler artifacts, accepted baseline, historical handoff and passing fidelity review retain their original identities. [Input checks](input-checks.json) records their hashes. The accepted reconciliation v1.1.0 and RD-0001–RD-0004 govern the work. The [specification index](.scratch/retrofit-evidence-m1/spec.md) identifies the underlying documents and contracts.
