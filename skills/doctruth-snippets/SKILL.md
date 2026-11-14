---
name: doctruth-snippets
description: >-
  Statically checks the code in documentation: Python snippets that no longer parse, JSON that is
  not valid, YAML, TOML and JS/TS or shell snippets with broken syntax, imports of the project's own
  modules that no longer exist, install commands that name a stale version, commands that point at
  files or scripts that are gone, risky advice such as curl piped to sh, and function names in the
  text that nothing in the code uses. Nothing in the docs is ever executed. Use when the user says
  "do the examples in my README still work", "check the code blocks in the docs", "my quick start is
  broken", "is the version in the install command current", "which snippets are out of date", or
  "fix the docs code that no longer parses".
argument-hint: "[folder or Markdown file]"
---

# doctruth-snippets: docs code that no longer matches the code

A snippet that fails to parse, imports a module that was renamed, or copies a file that does not
exist is the most expensive kind of doc bug: it fails on the reader's first run. `snippet_check.py`
parses fences with `ast`, `json` and bracket scanning, and checks imports and file references
against the project. It never runs anything from a document.

## Inputs

- A project folder or one Markdown file. The project root is used for imports, versions and paths.
- Commands below say `python3`; on Windows use `python` (or `py -3`).

## Outputs

- A findings table (SN001 to SN017) with severity, `file:line`, the section and a suggestion.
- Fixed snippets and install commands, each checked again by the script.
- A short list of snippets you could not judge statically (a language with no checker).

## Steps

1. **Confirm permission.** Read and edit docs only in a repository the user owns or is authorized
   to change. Do not run a snippet to test it, even a harmless-looking one.

2. **Run the check.**
   ```
   python3 "${CLAUDE_SKILL_DIR}/snippet_check.py" . --json > doctruth-snippets.json
   python3 "${CLAUDE_SKILL_DIR}/snippet_check.py" .
   ```
   Use `--fail-on high` in CI. A single file: `snippet_check.py README.md --root .`.

3. **Fix the first-run problems first.** Findings in an Install, Quick start or Usage section are
   marked `(first run)`. Order: `SN014` missing files and scripts, `SN009` and `SN010` bad imports,
   `SN001` and `SN002` syntax, `SN012` stale versions, then the rest.

4. **Fix each kind the right way.**
   - `SN001` Python: open the code, find the current API, and rewrite the snippet. Do not pad it
     with `# noqa` or `pass`. A placeholder like `<your-key>` and `...` are accepted as is.
   - `SN009`, `SN010`: read the module and use a name that exists there now. If the suggestion is
     the same thing under a new name, use it; if the feature was removed, change the prose.
   - `SN012`: prefer an unpinned install, or the declared version. Docs should not hard-code a
     version that releases will outdate; point at the latest release instead.
   - `SN014`: restore the path in the command, or add the missing file (for example `.env.example`)
     if the docs are right and the repository forgot it.
   - `SN003`: label the fence `jsonc`, or remove the comments and trailing commas.
   - `SN008`: replace `curl | sh` with a download, a checksum and a run; replace `chmod 777` with
     the narrowest mode; keep `sudo rm` out of docs.
   - `SN013`: only "possibly stale". Search the code for the new name before editing the text.

5. **Re-run** and show before and after counts. Say which fences were skipped because their
   language has no checker.

## Decision rules

- A snippet that is deliberately partial should say so (`# ...` or a comment). Mark it as `text`
  or `python` with a visible `...`; do not make it falsely complete.
- `SN011` (no fence language) is cosmetic; add the language the script names.
- Version numbers in a CHANGELOG are history and are not flagged. `SN015` compares only the top
  entry with the declared version.
- TOML fences are checked only on Python 3.11+ (`tomllib`); on older versions they are skipped.
- YAML checks are basic: tabs, unclosed quotes, duplicate keys. A clean result is not a proof of
  valid YAML.
- A function name in backticks that nothing in the code mentions may belong to a library. Check
  the imports before calling it stale.

## What NOT to do

- Do not execute, import or `eval` anything found in a document. The script does not, and neither
  should you, to "confirm" a finding.
- Do not edit the code to match a stale snippet, unless the user says the snippet is right.
- Do not pin new version numbers in docs by hand; read them from the project manifest.
- Do not add real keys, tokens or personal data to a fixed snippet.
- Do not silence a finding by deleting the snippet without saying so.
