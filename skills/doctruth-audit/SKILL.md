---
name: doctruth-audit
description: >-
  Runs the three docs-truth checks in order (links, CLI flags and environment variables, code
  snippets and versions), merges the findings into one prioritised report where broken first-run
  instructions rank highest, and proposes a minimal patch per finding. Use when the user says
  "audit my docs", "does my README lie", "is the quick start still correct", "check the docs
  against the code", "full docs check before the release", or "what in my documentation is out of
  date". This skill has no script of its own; it calls the scripts of its three sibling skills.
argument-hint: "[project folder]"
---

# doctruth-audit: one report, worst problem first

The three checkers each answer one question. This skill runs all three, puts their findings on one
scale, and ranks by what a first-time reader would hit first. It calls the scripts that live in
the sibling skill folders, so the pack must be installed whole: `doctruth-links`, `doctruth-cli`
and `doctruth-snippets`.

## Inputs

- A project folder with docs and code.
- Optional: `--online` for external links (only with the user's agreement, see `doctruth-links`).
- Commands below say `python3`; on Windows use `python` (or `py -3`).

## Outputs

- `docs-truth/audit.md`: a ranked table, a patch per finding, and what could not be checked.
- `docs-truth/*.json`: the raw output of each script, so the report can be regenerated.
- A summary for the user: how many findings, how many block a first run, what to fix first.

## Steps

1. **Confirm permission.** Audit and edit docs only in a repository the user owns or is
   authorized to change. The scripts read files and never execute documentation.

2. **Run the three scripts in order**, saving JSON, from the project root.
   ```
   python3 "${CLAUDE_SKILL_DIR}/../doctruth-links/link_check.py" . --json > docs-truth/links.json
   python3 "${CLAUDE_SKILL_DIR}/../doctruth-cli/cli_drift.py" . --json > docs-truth/cli.json
   python3 "${CLAUDE_SKILL_DIR}/../doctruth-snippets/snippet_check.py" . --json > docs-truth/snippets.json
   ```
   Create `docs-truth/` first. Exit code 2 means bad input (a missing folder, nothing to scan): read
   the error, fix the arguments, run again. Exit code 1 appears only when you pass `--fail-on`.

3. **Merge on one scale.** Every finding in the three JSON files has `rule`, `severity`, `file`,
   `line`, `message`, `suggestion`, `section` and `first_run`. Sort by this key:
   1. `first_run` true before false (an Install, Quick start or Usage section);
   2. severity: high, medium, low, info;
   3. rule family: a missing file or bad import (`LK001`, `SN014`, `SN009`, `SN010`) before syntax
      (`SN001`, `SN002`), before wrong flags (`CD001`, `CD002`, `CD007`), before the rest;
   4. file, then line.
   Treat the same underlying cause as one item: `--out` renamed to `--output` shows up in the
   README, the quick start and the usage doc, and is one fix with three locations.

4. **Propose a minimal patch for each item.** One line of before and after, or a command to run.
   Use the script's own suggestion where it has one, and verify it against the code first (open the
   file, check the name). For a flag table, use `cli_drift.py --table-only`.

5. **Mark each item** `confirm` (you checked the code and it is a real drift), `needs a human`
   (the right fix depends on intent), or `rejected` (a false positive, with the reason). The
   `docs-auditor` agent can do the confirming read-only.

6. **Write the report** in this shape and keep it short:
   - Verdict: "N findings, M in first-run sections" and the top three to fix.
   - The ranked table: `#`, `where`, `what is wrong`, `patch`, `status`.
   - "Not checked": languages with no snippet checker, flags built dynamically, TOML skipped.

7. **Apply the patches** only if the user asked for fixes. Re-run all three scripts afterwards and
   show the counts before and after.

## Decision rules

- A first-run finding of any severity outranks a high one deep in a reference page.
- Info findings (blocked or unverifiable links) never appear in the top of the report.
- If two scripts disagree about the same line, the cause is usually one of them seeing a snippet
  in a fence and the other in prose. Report it once.
- A clean run is a statement about what the scripts can see. Say what they cannot: prose claims,
  screenshots, anything dynamic, and every language without a checker.
- For a release gate, recommend `--fail-on high` on each script in CI, and say which medium
  findings you would want to see in the pull request.

## What NOT to do

- Do not run, import or `eval` any snippet from the docs, and do not run the CLI to see its output.
- Do not apply patches the user did not ask for, and do not edit code to satisfy the docs.
- Do not pass `--online` without the user's agreement; it contacts third-party servers.
- Do not report the same cause three times, and do not pad the report with info findings.
- Do not present a similarity guess ("did you mean") as a verified fix.
