"""Loading and running tasks."""
import os

HOME = os.environ.get("TASKPILOT_HOME", os.path.expanduser("~/.taskpilot"))


def load_tasks(path):
    """Read one task per line, skipping blanks and comments."""
    with open(path, encoding="utf-8") as handle:
        return [line.strip() for line in handle if line.strip() and not line.startswith("#")]


def cache_dir():
    return os.getenv("TASKPILOT_CACHE", os.path.join(HOME, "cache"))


def upload_token():
    return os.environ["TASKPILOT_API_TOKEN"]


class TaskRunner:
    """Run tasks with a fixed number of parallel jobs."""

    def __init__(self, jobs=4, timeout=60):
        self.jobs = jobs
        self.timeout = timeout

    def run(self, tasks):
        return [(task, "ok") for task in tasks]
