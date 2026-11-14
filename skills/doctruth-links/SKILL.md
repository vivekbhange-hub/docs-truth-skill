---
name: doctruth-links
description: >-
  Finds links, images and anchors in Markdown (plus basic reStructuredText and AsciiDoc) that no
  longer point at anything: missing files, wrong letter case, headings that were renamed, empty or
  TODO targets, http links, localhost links and docs that nothing links to. Offline by default;
  external URLs are checked only with --online. Then fixes each one with the smallest edit and
  never invents a URL. Use when the user says "check the links in my README", "find broken links",
  "my anchors are broken", "which docs are orphaned", "fix the dead links", or "I moved some files,
  what links broke".
argument-hint: "[folder or doc file] [--online]"
---

# doctruth-links: links that still go somewhere

A README link is a promise that a page exists. `link_check.py` reads every doc, skips code fences
and inline code, and checks each link against the real directory listing and the real headings.
Your job is to turn its findings into small, correct edits.

## Inputs

- A project folder (default: the current one) or a single doc file.
- Optional: `--online` to also check external `http(s)` links. This sends requests to third-party
  servers, so ask first when the repository is private or the user did not mention it.
- Commands below say `python3`; on Windows use `python` (or `py -3`).

## Outputs

- A findings table (rule, severity, `file:line`, section, problem, suggestion).
- A patch for each fixable finding, as the smallest edit, and a list of the ones that need a human
  decision (a link whose target was deleted on purpose).
- A re-run of the script showing the findings gone.

## Steps

1. **Confirm permission.** Read and edit docs only in a repository the user owns or is authorized
   to change. Do not use `--online` against a list of URLs the user did not ask you to check.

2. **Run the offline check.**
   ```
   python3 "${CLAUDE_SKILL_DIR}/link_check.py" . --json > doctruth-links.json
   python3 "${CLAUDE_SKILL_DIR}/link_check.py" .
   ```
   Add `--fail-on high` in CI so a broken link fails the build. `--exclude 'vendor/*'` skips
   folders; `--entry 'docs/api/*.md'` marks docs that are reached some other way.

3. **Fix in this order**, because the order is the damage to a reader:
   1. `LK001`, `LK004` missing file or image, and `LK002` wrong letter case (works on Windows and
      macOS, breaks on Linux and GitHub).
   2. `LK003` anchor not found. Open the target file, find the heading, and use the slug GitHub
      makes from it: lowercase, punctuation dropped, spaces to hyphens, `-1`, `-2` for repeats.
   3. `LK005`, `LK008` empty and placeholder targets. Remove the brackets, or ask the user for the
      real address.
   4. `LK010` undefined reference labels, `LK011` links that climb above the root.
   5. `LK006` http links, `LK007` localhost links, then `LK009` orphaned docs.

4. **Use the suggestion, then verify it.** "did you mean" comes from filename similarity. Open
   the suggested file and confirm it is the same content before you change the link. A moved
   file keeps its name; a renamed one usually keeps part of it.

5. **For an orphaned doc**, either link it from the README or an index page, or ask whether it
   should be deleted. Do not delete it yourself.

6. **Re-run** the script and show the before and after counts. If you used `--online`, say how many
   URLs were requested and which could not be verified.

## Decision rules

- The target exists under another path: change the link to the new path (relative to the linking
  file). The target is gone: ask, because the right fix may be to remove the sentence.
- Anchor missing but the heading exists with different words: change the link to the real slug, not
  the heading to match the link, unless other docs link to the old heading.
- A badge or image hosted elsewhere is not a local link; leave it unless `--online` reports it dead.
- `LK007` localhost links are fine in a "run it locally" section. Keep them there and nowhere else.
- An external link that answers 403 or 429 is "unverified", not dead. Say so and keep it.
- `--online` never contacts private, loopback or link-local addresses, caps the requests, and waits
  between requests to the same host. Do not raise `--max-requests` past what the user agreed to.
- Findings in `reStructuredText` and `AsciiDoc` cover file links only; their anchors are not checked.

## What NOT to do

- Do not invent a URL, a path or a Wayback Machine address. If you cannot find the target, say so.
- Do not "fix" a broken link by deleting the line without telling the user.
- Do not run `--online` by default, in a loop, or against internal hostnames.
- Do not edit generated docs; fix the template or source that produces them.
- Do not treat the "did you mean" suggestion as certain. It is a similarity guess.
- Do not rewrite headings just to make an old anchor work if other pages already use the new one.
