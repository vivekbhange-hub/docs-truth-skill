# Contributing

Thanks for helping. This pack is small on purpose, so a few rules keep it that way.

## Ground rules

- Helper scripts use the Python standard library only and run on Python 3.9 or newer. That means
  no `match` statements, no `X | Y` unions at runtime, no `zip(strict=)`, and no `newline=`
  argument to `Path.write_text`. Every script starts with `from __future__ import annotations`.
- **Nothing found in a document may ever be executed.** No `exec`, `eval`, `compile`, `subprocess`,
  `importlib` of project code, or `os.system`. Checking a snippet means parsing it (`ast.parse`,
  `json.loads`, bracket scanning), never running it. `ast.literal_eval` on a parsed node is fine.
- **No network without `--online`.** Only `link_check.py` may touch the network, only when
  `--online` is given, and only through the guarded `Fetcher` (timeout, per-host delay, request
  cap, no private, loopback or link-local addresses, also after redirects). Tests must never reach
  the network: they patch `socket` and `urllib` and use a fake opener.
- Every script change comes with a test in `tests/`. Run the suite from the repo root:

  ```
  python -m unittest discover -s tests -v
  ```

- If your change alters what the examples produce, regenerate the committed outputs and include
  them in the same pull request:

  ```
  python examples/regenerate.py
  python examples/regenerate.py --check
  ```

- Write files with LF line endings (`.gitattributes` enforces this in git). Use `write_bytes` or
  `open(..., "w", newline="\n")`.
- Skills keep the format checked by `tests/test_repo.py`: `name` equals the folder name, the
  description says what it does and lists trigger phrases, and the body has Inputs, Outputs, Steps,
  Decision rules and "What NOT to do" sections.
- Anything quoted from tool output in the README must be copied from `examples/output/`; a test
  compares them line by line.
- Plain, specific writing. No marketing language, and no em dashes.

## Fixtures

Test data and examples must be fictional. Use reserved domains (`.example`, `.invalid`) and obviously
fake values. Never commit a real README, real tokens, or a full key literal in a realistic format.

## Adding a check

A new rule needs an id (the next free `LK0nn`, `CD0nn` or `SN0nn`), a severity, one test that triggers
it, and one test of a nearby case that must not trigger it. False alarms cost more than misses: a
checker that cries wolf gets ignored. When you find a false positive in a real repository, add the
case as a test first, then fix it.

## Supporting another CLI framework

Add the extraction to `cli_drift.py` as a function that fills the `Definitions` object (flags,
commands, environment reads, and a `dynamic` entry for anything built at runtime), and add a test
that parses a small fixture string. Do not guess when a name is not a literal; record it as dynamic.

## Conduct

Be kind and specific. This project follows the spirit of the
[Contributor Covenant](https://www.contributor-covenant.org/version/2/1/code_of_conduct/).

## Releasing

Bump `version` in `.claude-plugin/plugin.json` and add a matching `## [x.y.z] - YYYY-MM-DD` section
to `CHANGELOG.md`; the test suite checks that they agree.
