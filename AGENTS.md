# Project Agent Rules

- Changelog history is append-only: when updating changelog files, preserve all existing historical entries and do not wipe prior data.
- Lint execution policy: when running lint/quality checks, run `grain` and `semgrep` through the project environment with `uv run` (not direct system binaries). Treat any `grain` error or `semgrep` finding as release-blocking until fixed or explicitly waived.
