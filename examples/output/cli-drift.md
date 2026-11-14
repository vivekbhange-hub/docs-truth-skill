Scanned 4 doc file(s) and 3 code file(s). CLI names: `taskpilot`.
Documented: 8 flag(s), 4 command(s), 3 environment variable(s). Defined in code: 8 flag(s), 3 command(s), 3 environment variable(s).
12 finding(s): 4 high, 4 medium, 4 low, 0 info.

| Rule | Count | Meaning |
|---|---:|---|
| CD001 | 2 | Flag is documented but the code does not define it |
| CD002 | 1 | Command is documented but the code does not define it |
| CD003 | 1 | Environment variable is documented but the code never reads it |
| CD004 | 3 | Flag is defined in the code but not documented |
| CD006 | 1 | Environment variable is read by the code but not documented |
| CD007 | 2 | Likely rename: a documented name is close to an undocumented one |
| CD008 | 1 | Documented default differs from the default in the code |
| CD009 | 1 | Flag is documented under a command that does not define it |

| Location | Rule | Severity | Section | Problem | Suggestion |
|---|---|---|---|---|---|
| `README.md:27` | CD001 | high | Quick start (first run) | `--workers` is documented but not defined | remove it from the docs, or add the flag to the code |
| `README.md:36` | CD001 | high | Quick start (first run) | `--last` is documented but not defined | remove it from the docs, or add the flag to the code |
| `README.md:36` | CD002 | high | Quick start (first run) | command `report` is documented but not defined | did you mean `export`? |
| `README.md:35` | CD007 | high | Quick start (first run) | `--out` is documented, the code defines `--output` (taskpilot/cli.py:24); likely renamed | update the docs to `--output` |
| `docs/config.md:9` | CD003 | medium | Environment variables | `TASKPILOT_LOG_LEVEL` is documented but the code never reads it | remove it from the docs, or read it in the code |
| `docs/config.md:8` | CD007 | medium | Environment variables | `TASKPILOT_TOKEN` is documented, the code reads `TASKPILOT_API_TOKEN` (taskpilot/core.py:18); likely renamed | update the docs to `TASKPILOT_API_TOKEN` |
| `docs/usage.md:8` | CD008 | medium | Options | `--timeout`: docs say default `30`, code says `60` (taskpilot/cli.py:15) | change the docs to `60` |
| `README.md:34` | CD009 | medium | Quick start (first run) | `--verbose` is documented under `status` but defined for `run` | move the example to `run --verbose` or add the flag to `status` |
| `taskpilot/cli.py:9` | CD004 | low |  | `--config` is defined but appears nowhere in the docs | document it: path to the config file |
| `taskpilot/cli.py:16` | CD004 | low |  | `--dry-run` is defined for `run` but appears nowhere in the docs | document it: print what would run |
| `taskpilot/cli.py:20` | CD004 | low |  | `--json` is defined for `status` but appears nowhere in the docs | document it: print JSON instead of text |
| `taskpilot/core.py:14` | CD006 | low |  | `TASKPILOT_CACHE` is read (os.environ) but appears nowhere in the docs | add it to the configuration section |

No dynamically built flags or environment names were found.

Shell lines for other programs were ignored: 5.
