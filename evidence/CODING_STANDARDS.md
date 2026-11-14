# Coding standards

- Use the version 1.1 data and interface contracts and opaque immutable identifiers. Keep persisted source and revision records separate from current eligibility and delivery state.
- The local FastAPI service and SQLite own all writes, eligibility decisions, and transaction boundaries. Browser controls and provider output supply untrusted inputs. Never interpolate those inputs as HTML or SQL.
- Use bounded inputs and execution, validated references, deterministic failure behavior and safe structured errors. Log only non-sensitive diagnostic metadata outside the restricted store.
- Use focused integration checks through browser, HTTP, and evaluator interfaces backed by real stores and parser processes. Limit injected failures to system boundaries. Record observed test evidence separately from production acceptance.
