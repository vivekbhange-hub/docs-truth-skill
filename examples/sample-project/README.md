# taskpilot

Run and report on small task files.

![taskpilot logo](docs/logo.png)

## Install

```bash
pip install taskpilot==1.4.0
cp .env.sample .env
```

Or use the installer (see [the old site](http://docs.taskpilot.example/guide)):

```bash
curl -sSL https://example.com/install.sh | sh
chmod 777 ~/.taskpilot
```

## Quick start

1. Write a task file with one task per line.
2. Run it with four workers:

```bash
taskpilot run tasks.txt --workers 4
python scripts/seed.py
```

3. Check the result and export it:

```bash
taskpilot status --verbose
taskpilot export --format csv --out report.csv
taskpilot report --last
```

In Python:

```python
from taskpilot.core import TaskRunner, load_all
from taskpilot.runner import Scheduler

runner = TaskRunner(jobs=2)
```

An older example, kept for reference:

```python
print "starting"
```

Call `load_config()` to read the settings. Settings are described in
[Configuration](docs/configuration.md#environment-variable) and the CLI in
[Usage](docs/usage.md#custom-hooks). Release notes are in [Changelog](changelog.md).
More: [Contributing]() and [Roadmap](TODO).
