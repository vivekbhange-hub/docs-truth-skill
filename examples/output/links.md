Scanned 5 doc file(s) and 8 link(s) (7 local, 1 external). 9 finding(s): 4 high, 2 medium, 3 low, 0 info.

Offline run: no network requests were made (use --online to check external links).

| Rule | Severity | Count | Meaning |
|---|---|---:|---|
| LK001 | high | 1 | Relative file link does not exist |
| LK002 | high | 1 | File exists but the letter case differs |
| LK003 | high | 1 | Anchor does not exist in the target file |
| LK004 | high | 1 | Image file does not exist |
| LK005 | medium | 1 | Empty link target |
| LK006 | low | 1 | http:// link, https:// is likely available |
| LK008 | medium | 1 | TODO or placeholder link |
| LK009 | low | 2 | Orphaned doc: no other doc links to it |

| Location | Rule | Severity | Section | Problem | Suggestion |
|---|---|---|---|---|---|
| `README.md:5` | LK004 | high | taskpilot | image not found: `docs/logo.png` | no similar file found; remove or re-point the link |
| `README.md:55` | LK001 | high | Quick start (first run) | file not found: `docs/configuration.md` | did you mean `docs/config.md`? |
| `README.md:56` | LK002 | high | Quick start (first run) | letter case differs from the file on disk: `changelog.md` | use `CHANGELOG.md` |
| `README.md:56` | LK003 | high | Quick start (first run) | anchor `#custom-hooks` not found in `docs/usage.md` | check the heading text; GitHub drops punctuation |
| `README.md:57` | LK005 | medium | Quick start (first run) | link has no target | add the target or remove the link |
| `README.md:57` | LK008 | medium | Quick start (first run) | placeholder link target | point it at the real page or remove it |
| `README.md:14` | LK006 | low | Install (first run) | plain http link | use `https://` if the site serves it |
| `docs/architecture.md:1` | LK009 | low |  | no other doc links to this file | link it from the README or an index page, or delete it if it is dead |
| `docs/config.md:1` | LK009 | low |  | no other doc links to this file | link it from the README or an index page, or delete it if it is dead |
