---
name: doctruth-writer
description: >-
  A procedure for writing documentation that stays true: derive option tables and version numbers
  from the code instead of typing them, prefer snippets that can be checked, keep the quick start
  under five steps, add a docs check to CI, and record the date and version each claim was last
  verified. Use when the user says "write a README that will not go stale", "document this CLI",
  "how do I keep the docs in sync with the code", "add a docs check to CI", "write the quick
  start", or "make the install instructions reliable". It has no script; it uses the checkers of
  the sibling skills as the proof.
argument-hint: "[what to document]"
---

# doctruth-writer: docs that survive the next release

Docs go stale because a person has to remember to update them. This procedure removes the
remembering: generate what can be generated, check what can be checked, and date the rest.

## Inputs

- The thing to document: a CLI, a library, an install path, a whole README.
- Access to the code, so every claim can be traced to a line.
- Commands below say `python3`; on Windows use `python` (or `py -3`).

## Outputs

- The doc, written so that each factual claim has a source: the code, a command, or a dated note.
- A generated options table, when the project has a CLI.
- A CI step that runs the docs checks, and a short "last verified" line where claims cannot be
  generated.

## Steps

1. **Confirm permission.** Write and edit docs only in a repository the user owns or is authorized
   to change. Do not copy text from other projects' docs.

2. **Find the facts in the code before writing a sentence.** Flags and defaults, environment
   variables, command names, supported versions, install names. Read them; do not recall them.

3. **Generate, do not transcribe.** For a CLI, build the options table from the code:
   ```
   python3 "${CLAUDE_SKILL_DIR}/../doctruth-cli/cli_drift.py" . --table-only
   ```
   Paste the output between two comment markers (`<!-- options:start -->` and
   `<!-- options:end -->`) so the next run replaces exactly that block. Read the version from the
   manifest (`pyproject.toml`, `package.json`) rather than writing it in prose.

4. **Write the quick start as the shortest path to one visible result.** At most five steps. Each
   step is one command or one file edit, and each command is copied from a terminal where it worked.
   Say what the reader should see after the last step. Put install first, then the first run, and
   nothing optional in between.

5. **Prefer snippets the checkers can read.** Label every fence with its language. Use real,
   importable names from the project. For secrets use `<your-api-key>` placeholders, never a value.
   Keep a snippet short enough to be complete; if it must be partial, say so in a comment.

6. **Link by relative path to files that exist,** and link to headings you control. Rename a
   heading, search the docs for its old anchor.

7. **Record verification where a claim cannot be generated.** One line near the claim:
   `Verified against v2.1.0 on 2026-11-13.` Use the real version and today's date, and update both
   when you re-check. A claim with no date is an opinion.

8. **Add the check to CI** so drift fails a pull request, not a user.
   ```
   python3 skills/doctruth-links/link_check.py . --fail-on high
   python3 skills/doctruth-cli/cli_drift.py . --fail-on high
   python3 skills/doctruth-snippets/snippet_check.py . --fail-on high
   ```
   Adjust the paths to where the scripts are installed. Offline checks only in CI by default;
   schedule `--online` weekly, not on every push.

9. **Run `doctruth-audit`** on the finished doc and fix what it finds before handing it over.

## Decision rules

- If a fact appears in two places, generate one from the other or keep it in one place and link.
- A table of options beats prose about options. Prose explains why, the table says what.
- Do not hard-code version numbers in install commands; use the latest release or the declared one.
- When the quick start needs more than five steps, move the extras to a second page and link it.
- Prefer one canonical install method and name the alternatives in one line each.
- If you cannot verify a claim, remove it or mark it unverified. Do not soften it.

## What NOT to do

- Do not invent flags, outputs or version numbers, and do not describe features the code lacks.
- Do not paste a command you have not seen work, and do not run commands from docs you did not write.
- Do not copy generated tables by hand after the first time; regenerate them.
- Do not put real tokens, personal data or internal hostnames in examples.
- Do not add marketing language; state what the thing does and what it needs.
