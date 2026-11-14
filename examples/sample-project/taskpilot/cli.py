"""Command line entry point for taskpilot."""
import argparse

from taskpilot.core import TaskRunner, load_tasks


def build_parser():
    parser = argparse.ArgumentParser(prog="taskpilot", description="Run and report on small task files.")
    parser.add_argument("--config", default="taskpilot.toml", help="path to the config file")
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="run every task in the file")
    run.add_argument("tasks", help="task file, one task per line")
    run.add_argument("-j", "--jobs", type=int, default=4, help="number of parallel jobs")
    run.add_argument("--timeout", type=int, default=60, help="seconds before a task is stopped")
    run.add_argument("--dry-run", action="store_true", help="print what would run")
    run.add_argument("-v", "--verbose", action="store_true", help="print each task as it starts")

    status = sub.add_parser("status", help="show the state of the last run")
    status.add_argument("--json", action="store_true", help="print JSON instead of text")

    export = sub.add_parser("export", help="write the last run to a file")
    export.add_argument("--format", choices=["csv", "json"], default="csv", help="output format")
    export.add_argument("--output", default="report.csv", help="file to write")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.command == "run":
        runner = TaskRunner(jobs=args.jobs, timeout=args.timeout)
        for task, state in runner.run(load_tasks(args.tasks)):
            print(state, task)
    return 0
