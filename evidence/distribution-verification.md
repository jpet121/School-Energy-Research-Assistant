# Test results

## App tests

Factory ran 197 Python tests on a clean copy of the application. All tests passed. The coverage checks found no gaps in the measured Python or TypeScript lines or branches. [Factory test record](factory-verification.json). The record omits the local extraction path.

## Local setup

Chrome on Windows passed browser tests for PDF upload, keyboard use, source inspection, and draft export. The tests also checked the layout at 200% and 400% zoom.

Linux tests passed on x64 and ARM64. The tests checked PDF isolation, full-text search, and draft export. [GitHub Actions results](https://github.com/jpet121/School-Energy-Research-Assistant/actions/runs/43625888169)

The included sample went through the app workflow. The [screenshot](../docs/images/evidence-view.png) and [exported draft](../examples/sample-draft.md) come from that run.

## Scope

The local HTTPS test checked sign-in, access controls, search, and draft export. The Mac launchers still await testing on a Mac.

The tests confirm software behavior with sample data. Formal review of the starter collection and hosted answer quality is pending. The tests made no paid model calls.
