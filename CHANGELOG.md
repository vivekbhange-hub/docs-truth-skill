# Changelog

All notable changes to this project are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [1.0.0] - 2026-11-13

### Added

- Five skills: `doctruth-links`, `doctruth-cli`, `doctruth-snippets`, `doctruth-audit` and
  `doctruth-writer`.
- Two read-only agents: `docs-auditor` and `newcomer-simulator`.
- Helper scripts (standard library only, Python 3.9 or newer): `link_check.py` (relative links,
  case, images, anchors with GitHub slug rules, placeholders, orphaned docs, opt-in `--online`
  check with an SSRF guard), `cli_drift.py` (flags, commands and environment variables in docs
  versus argparse, click, typer, Go flag and cobra, commander, and environment reads) and
  `snippet_check.py` (static checks of Python, JSON, YAML, TOML, JS/TS and shell fences, imports of
  the project's own modules, stale versions, missing files in commands, stale function names).
- Findings carry their section and a `first_run` flag so the audit can rank broken install and quick
  start instructions first.
- A fictional sample project under `examples/sample-project/` with deliberately drifted docs, and
  generated reports under `examples/output/` that the test suite checks byte for byte.
- CI on Linux, Windows and macOS with Python 3.9, 3.12 and 3.13.
