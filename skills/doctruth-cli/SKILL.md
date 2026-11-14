---
name: doctruth-cli
description: >-
  Compares the CLI flags, subcommands and environment variables a README or docs folder mentions
  with what the code actually defines (argparse, click, typer, Go flag and cobra, Node commander,
  and os.environ, process.env, os.Getenv reads). Reports flags that are documented but gone,
  options that exist but are undocumented, probable renames, defaults that disagree and flags shown
  under the wrong command, then writes a corrected options table from the code. Use when the user
  says "is my README still right about the CLI", "the docs mention a flag that does not exist",
  "which options are undocumented", "generate the options table from the code", "sync the
  environment variable docs", or "did we rename a flag".
argument-hint: "[project folder] [--cli NAME]"
---

# doctruth-cli: the options the docs promise versus the options the code has

A user types what the README says. If `--workers` became `--jobs` two releases ago, the README
is wrong and the first run fails. `cli_drift.py` reads the docs and the code statically, nothing
is imported or executed, and lists every difference. The code wins unless the user says otherwise.

## Inputs

- A project folder with docs (README, `docs/`, `skills/`, `agents/`) and the CLI's source.
- Optional: `--cli NAME` when the program name is not in `pyproject.toml`, `package.json`,
  `go.mod` or an argparse `prog=`; `--docs PATH` and `--code PATH` to narrow either side.
- Commands below say `python3`; on Windows use `python` (or `py -3`).

## Outputs

- The findings table (CD001 to CD010) with severity, `file:line` and section.
- A corrected options table generated from the code, ready to paste.
- Doc patches that apply it, plus a list of things only the user can decide (was the code wrong?).

## Steps

1. **Confirm permission.** Read and edit docs only in a repository the user owns or is authorized
   to change. Never run the CLI under analysis to "see what it prints"; the script reads source.

2. **Run the comparison.**
   ```
   python3 "${CLAUDE_SKILL_DIR}/cli_drift.py" . --json > doctruth-cli.json
   python3 "${CLAUDE_SKILL_DIR}/cli_drift.py" .
   ```
   For CI use `--fail-on high`. `--include-system` also reports PATH, HOME, CI and GITHUB_*.

3. **Check the coverage note first.** If the report says no parser was recognised (CD010), or
   lists files "built dynamically", the comparison is incomplete there. Read those files by hand
   and say which flags you could not verify. Do not guess them.

4. **Work the findings by severity.**
   - `CD001`, `CD002` documented but not defined (high when inside a command line of this CLI,
     medium when the flag is only mentioned). Look for the real name before deleting anything.
   - `CD007` probable rename: confirm in the code, then update every location listed.
   - `CD009` flag under the wrong command: move the example, or ask whether the flag should also
     exist on that command.
   - `CD008` documented default differs: change the docs to the code's value.
   - `CD003`, `CD006` environment variables: documented but never read, or read but never listed.
   - `CD004`, `CD005` defined but undocumented: add them, using the help text from the code.

5. **Generate the table instead of typing it.**
   ```
   python3 "${CLAUDE_SKILL_DIR}/cli_drift.py" . --table-only
   ```
   Replace the hand-written options table with this output. Keep the descriptions the code
   provides; add prose around the table, not inside it.

6. **Re-run** and show the finding counts before and after. Anything left should be a deliberate
   decision, named as such.

## Decision rules

- Code is the source of truth. Only flip that when the user says the code is the bug; then the fix
  is a code change, and it is a separate task.
- A loose mention (a bare `--flag` in prose) may describe another tool's flag, for example `gh` or
  `curl`. Read the sentence before you change it. Command lines starting with this CLI are reliable.
- A flag defined only for one subcommand and shown under another is a doc bug unless the user wants
  the flag added.
- Environment variables set by Claude Code or CI (`CLAUDE_*`, `GITHUB_*`) are not the project's.
- When docs and code agree on the name but not the default, check whether the code applies the
  default later (a config file, an environment override) before editing.

## What NOT to do

- Do not run the program, its `--help`, or any snippet from the docs to confirm a flag.
- Do not delete documentation for an option just because the script did not see it in the code;
  dynamic definitions are listed separately for that reason.
- Do not rename flags in the code to match the docs.
- Do not paste secrets or real tokens into the environment variable table; use placeholders.
- Do not treat a similarity-based rename as certain. Verify it in the code.
