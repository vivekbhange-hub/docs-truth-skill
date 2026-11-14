Scanned 5 doc file(s) and 7 code fence(s). Checked: json 1, python 2, shell 4. 10 finding(s): 5 high, 3 medium, 2 low, 0 info.
Declared version: 2.1.0 (pyproject.toml).
No snippet was executed; every check is static.

| Rule | Severity | Count | Meaning |
|---|---|---:|---|
| SN001 | high | 1 | Python snippet does not parse |
| SN003 | low | 1 | JSON snippet needs comments or trailing commas to be valid |
| SN008 | medium | 2 | Risky shell advice |
| SN009 | high | 1 | Imported module of this project does not exist |
| SN010 | high | 1 | Imported name is not defined in the module |
| SN012 | medium | 1 | Version number in the docs differs from the declared version |
| SN013 | low | 1 | Function mentioned in the text is not in the code (possibly stale) |
| SN014 | high | 2 | Command refers to something that does not exist |

| Location | Rule | Severity | Section | Problem | Suggestion |
|---|---|---|---|---|---|
| `README.md:11` | SN014 | high | Install (first run) | file `.env.sample` does not exist in the project | did you mean `.env.example`? |
| `README.md:28` | SN014 | high | Quick start (first run) | script `scripts/seed.py` does not exist in the project | update the command or add the file |
| `README.md:42` | SN010 | high | Quick start (first run) | `load_all` is not defined in `taskpilot.core` | did you mean `load_tasks`? |
| `README.md:43` | SN009 | high | Quick start (first run) | module `taskpilot.runner` does not exist | did you mean `taskpilot.core`? |
| `README.md:51` | SN001 | high | Quick start (first run) | Python syntax error near `print "starting"` | fix the snippet so it parses, or label it as pseudo-code (`text`) |
| `README.md:10` | SN012 | medium | Install (first run) | docs say 1.4.0, the declared version is 2.1.0 | use `2.1.0`, or stop pinning a version in the docs |
| `README.md:17` | SN008 | medium | Install (first run) | pipes a download straight into an interpreter | download, read, then run it; or give a package-manager install |
| `README.md:18` | SN008 | medium | Install (first run) | `chmod 777` makes files world-writable | use the narrowest mode that works (755 or 644) |
| `README.md:54` | SN013 | low | Quick start (first run) | `load_config()` is mentioned but nothing in the code defines or uses `load_config` (possibly stale: grep based) | check the current name, or remove the mention |
| `docs/usage.md:17` | SN003 | low | Hooks | valid only after removing comments, `...` or trailing commas | label the fence `jsonc`, or make it strict JSON so readers can copy it |
