"""The bundled demo: committed outputs must be exactly what the scripts produce (examples/regenerate.py)."""
import io
import json
import re
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from _load import ROOT, load, load_path

regen = load_path(ROOT / "examples" / "regenerate.py", "regenerate_under_test")
lc = load("doctruth-links", "link_check")
cd = load("doctruth-cli", "cli_drift")
sc = load("doctruth-snippets", "snippet_check")
OUTPUT = ROOT / "examples" / "output"
SAMPLE = ROOT / "examples" / "sample-project"
EXPECTED = ["cli-drift.md", "links.md", "options-table.md", "snippets.md"]


class DeterminismTests(unittest.TestCase):
    def test_regenerating_is_byte_identical(self):
        with tempfile.TemporaryDirectory() as tmp:
            written = regen.generate(Path(tmp))
            self.assertEqual(written, EXPECTED)
            self.assertEqual(regen.compare(OUTPUT, Path(tmp)), [])

    def test_check_mode(self):
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(regen.main(["--check"]), 0)
        self.assertIn("up to date", out.getvalue())

    def test_compare_reports_differences(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            (Path(a) / "x.txt").write_text("1", encoding="utf-8")
            (Path(b) / "x.txt").write_text("2", encoding="utf-8")
            (Path(b) / "y.txt").write_text("2", encoding="utf-8")
            self.assertEqual(regen.compare(Path(a), Path(b)), ["y.txt: not committed", "x.txt: content differs"])

    def test_check_mode_fails_when_an_output_is_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            regen.generate(Path(tmp))
            (Path(tmp) / "links.md").write_bytes(b"stale\n")
            self.assertEqual(regen.compare(Path(tmp), OUTPUT), ["links.md: content differs"])

    def test_outputs_use_lf_and_no_machine_paths(self):
        for name in EXPECTED:
            with self.subTest(file=name):
                data = (OUTPUT / name).read_bytes()
                self.assertNotIn(b"\r\n", data)
                self.assertNotIn(b"Users", data)
                self.assertNotIn(b":\\", data)
                self.assertNotIn(b"\xe2\x80\x94", data)


class FixtureTests(unittest.TestCase):
    def test_fixture_is_fictional_and_small(self):
        files = sorted(p.relative_to(SAMPLE).as_posix() for p in SAMPLE.rglob("*") if p.is_file() and "__pycache__" not in p.parts)
        self.assertEqual(files, [".env.example", "CHANGELOG.md", "README.md", "docs/architecture.md", "docs/config.md",
                                 "docs/usage.md", "pyproject.toml", "taskpilot/__init__.py", "taskpilot/cli.py",
                                 "taskpilot/core.py"])
        text = "\n".join((SAMPLE / f).read_text(encoding="utf-8") for f in files)
        self.assertIn("docs.taskpilot.example", text)
        self.assertNotRegex(text, r"https?://(?!docs\.taskpilot\.example|example\.com)[a-z0-9.-]+\.(com|org|net|io)")

    def test_the_fixture_code_is_valid_python_and_never_imported(self):
        import ast
        for path in (SAMPLE / "taskpilot").glob("*.py"):
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    def test_each_kind_of_drift_is_present_exactly_where_intended(self):
        links = (OUTPUT / "links.md").read_text(encoding="utf-8")
        for needle in ("`docs/configuration.md`", "did you mean `docs/config.md`?", "use `CHANGELOG.md`",
                       "anchor `#custom-hooks` not found", "image not found: `docs/logo.png`",
                       "link has no target", "placeholder link target", "plain http link",
                       "`docs/architecture.md:1` | LK009"):
            self.assertIn(needle, links)
        cli = (OUTPUT / "cli-drift.md").read_text(encoding="utf-8")
        for needle in ("`--workers` is documented but not defined", "`--out` is documented, the code defines `--output`",
                       "command `report` is documented but not defined", "`--verbose` is documented under `status`",
                       "docs say default `30`, code says `60`", "`TASKPILOT_TOKEN` is documented, the code reads `TASKPILOT_API_TOKEN`",
                       "`TASKPILOT_CACHE` is read", "`--dry-run` is defined for `run` but appears nowhere"):
            self.assertIn(needle, cli)
        snippets = (OUTPUT / "snippets.md").read_text(encoding="utf-8")
        for needle in ("Python syntax error near `print \"starting\"`", "`load_all` is not defined in `taskpilot.core`",
                       "module `taskpilot.runner` does not exist", "docs say 1.4.0, the declared version is 2.1.0",
                       "pipes a download straight into an interpreter", "`chmod 777`", "file `.env.sample` does not exist",
                       "script `scripts/seed.py` does not exist", "`load_config()` is mentioned",
                       "valid only after removing comments"):
            self.assertIn(needle, snippets)

    def test_first_run_findings_rank_first_in_the_audit_sort_key(self):
        data = json.loads(self._json(cd, ["--json"]))
        first = [f for f in data["findings"] if f["first_run"]]
        self.assertTrue(first)
        self.assertEqual(first[0]["severity"], "high")

    def _json(self, module, extra):
        out = io.StringIO()
        with redirect_stdout(out):
            module.main([str(SAMPLE)] + extra)
        return out.getvalue()

    def test_options_table_matches_the_code(self):
        table = (OUTPUT / "options-table.md").read_text(encoding="utf-8")
        self.assertTrue(table.startswith("| Option | Command | Default | Env | Description |"))
        self.assertIn("| `--timeout` `VALUE` | `run` | `60` |  | seconds before a task is stopped |", table)
        self.assertIn("| `--jobs`, `-j` `VALUE` | `run` | `4` |", table)
        self.assertEqual(len([l for l in table.split("\n") if l.startswith("| `")]), 8)

    def test_nothing_is_fetched_when_generating(self):
        import socket
        import urllib.request
        from unittest import mock

        def boom(*args, **kwargs):
            raise AssertionError("network access attempted")
        with mock.patch.object(socket, "getaddrinfo", boom), mock.patch.object(socket.socket, "connect", boom), \
                mock.patch.object(urllib.request.OpenerDirector, "open", boom):
            with tempfile.TemporaryDirectory() as tmp:
                regen.generate(Path(tmp))

    def test_json_reports_agree_with_the_committed_markdown(self):
        for module, name in ((lc, "links.md"), (cd, "cli-drift.md"), (sc, "snippets.md")):
            with self.subTest(report=name):
                data = json.loads(self._json(module, ["--json"]))
                header = (OUTPUT / name).read_text(encoding="utf-8").split("\n")[0 if name != "cli-drift.md" else 2]
                count = data["summary"]["findings"]
                self.assertRegex(header, r"\b%d finding\(s\)" % count)


if __name__ == "__main__":
    unittest.main()
