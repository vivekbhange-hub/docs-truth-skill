"""Tests for skills/doctruth-snippets/snippet_check.py."""
import ast
import io
import json
import subprocess
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from _load import ROOT, load, write

sc = load("doctruth-snippets", "snippet_check")
SCRIPT = ROOT / "skills" / "doctruth-snippets" / "snippet_check.py"
SAMPLE = ROOT / "examples" / "sample-project"
FENCE = "```"


def fence(lang, body):
    return "%s%s\n%s\n%s\n" % (FENCE, lang, textwrap.dedent(body).strip("\n"), FENCE)


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = sc.main(argv)
    return code, out.getvalue(), err.getvalue()


def scan(tree: dict, toml_loader=None):
    with tempfile.TemporaryDirectory() as tmp:
        for rel, text in tree.items():
            write(Path(tmp) / rel, text)
        files = sorted(r for r in tree if r.endswith(".md"))
        return sc.analyse(tmp, files, toml_loader)


def findings_for(doc: str, extra=None, name="README.md", toml_loader=None):
    tree = {name: doc}
    tree.update(extra or {})
    return scan(tree, toml_loader)["findings"]


def rules(findings):
    return sorted(f["rule"] for f in findings)


class FenceExtractionTests(unittest.TestCase):
    def test_languages_lines_and_headings(self):
        text = "# Title\n\n## Install\n\n```bash\npip install x\n```\n\n~~~python title=\"a.py\"\nx = 1\n~~~\n\n```{.json}\n{}\n```\n"
        fences, prose = sc.extract_fences(text)
        self.assertEqual([(f.lang, f.start, f.heading) for f in fences],
                         [("shell", 6, "Install"), ("python", 10, "Install"), ("json", 14, "Install")])
        self.assertTrue(any(line == "## Install" or "Install" in line for _n, line, _h in prose))

    def test_front_matter_and_unclosed_fence(self):
        fences, _ = sc.extract_fences("---\ntitle: x\n---\n```python\nprint(1)\n")
        self.assertEqual(len(fences), 1)
        self.assertEqual([l for l in fences[0].lines if l], ["print(1)"])

    def test_indented_fences_are_dedented(self):
        fences, _ = sc.extract_fences("- item\n\n  ```python\n  x = 1\n  ```\n")
        self.assertEqual(fences[0].lines, ["x = 1"])

    def test_inline_triple_backticks_do_not_open_a_fence(self):
        fences, _ = sc.extract_fences("use ```not a fence``` here\n")
        self.assertEqual(fences, [])


class PythonTests(unittest.TestCase):
    def check(self, body):
        problems, tree = sc.check_python(textwrap.dedent(body).strip("\n").split("\n"))
        return problems, tree

    def test_valid_code_parses(self):
        problems, tree = self.check("def f(x):\n    return x + 1\n")
        self.assertEqual(problems, [])
        self.assertIsNotNone(tree)

    def test_python_2_print_is_flagged_with_line(self):
        problems, _ = self.check("x = 1\nprint 'hi'\n")
        self.assertEqual([(p[0], p[1]) for p in problems], [(1, "SN001")])

    def test_repl_prompts_and_output(self):
        problems, tree = self.check(">>> import os\n>>> for i in range(2):\n...     print(i)\n0\n1\n>>> x = [\n...   1]\n")
        self.assertEqual(problems, [])
        self.assertIsNotNone(tree)

    def test_ellipsis_placeholders_are_fine(self):
        for code in ("def f(): ...", "x = [...]", "class A:\n    ...", "call(a, ...)", "d = {'a': ...}"):
            with self.subTest(code=code):
                self.assertEqual(self.check(code)[0], [])

    def test_angle_bracket_and_unicode_ellipsis_placeholders(self):
        self.assertEqual(self.check("client = Client(api_key=<your-api-key>)")[0], [])
        self.assertEqual(self.check("items = [1, 2, \u2026]")[0], [])

    def test_shell_in_a_python_fence_is_a_mislabel_not_a_syntax_error(self):
        problems, _ = self.check("$ pip install foo\n$ python x.py\n")
        self.assertEqual([p[1] for p in problems], ["SN016"])

    def test_traceback_is_not_checked(self):
        self.assertEqual(self.check("Traceback (most recent call last):\n  File \"x\", line 1\nValueError: no\n")[0], [])

    def test_indented_snippet_is_dedented(self):
        self.assertEqual(self.check("    x = 1\n    y = 2\n")[0], [])

    def test_unclosed_paren_is_an_error(self):
        problems, _ = self.check("x = foo(1,\n")
        self.assertEqual([p[1] for p in problems], ["SN001"])

    def test_snippets_are_never_executed(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "ran.txt"
            doc = fence("python", "open(%r, 'w').write('x')\nimport os; os.system('echo hi')\n__import__('os').remove('x')" % str(marker))
            findings_for(doc)
            self.assertFalse(marker.exists())


class JsonTests(unittest.TestCase):
    def test_valid_invalid_and_noise(self):
        self.assertEqual(sc.check_json(['{"a": [1, 2]}'], "json"), [])
        bad = sc.check_json(['{"a": 1', '"b": 2}'], "json")
        self.assertEqual(bad[0][1], "SN002")
        self.assertEqual(bad[0][0], 1)
        trailing = sc.check_json(['{"a": 1,', "}"], "json")
        self.assertEqual(trailing[0][1], "SN003")
        comments = sc.check_json(["{", '  // note', '  "a": 1', "}"], "json")
        self.assertEqual(comments[0][1], "SN003")
        self.assertEqual(sc.check_json(["{", '  // note', '  "a": 1', "}"], "jsonc"), [])

    def test_ellipsis_placeholders_count_as_noise(self):
        self.assertEqual(sc.check_json(["{", '  "items": [', "    1,", "    ...", "  ]", "}"], "json")[0][1], "SN003")

    def test_noise_line_does_not_depend_on_the_json_error_message(self):
        """The reported line must be identical on every Python version, so it is found by our own scan."""
        self.assertEqual(sc.check_json(['{"a": 1,', "}"], "json")[0][0], 0)
        self.assertEqual(sc.check_json(["{", "  // note", '  "a": 1', "}"], "json")[0][0], 1)
        self.assertEqual(sc.check_json(["{", '  "a": [1,', "  2,", "  ]", "}"], "json")[0][0], 2)
        self.assertEqual(sc.noise_line(['{"u": "http://x.example", "a": 1}']), 0)

    def test_comment_markers_inside_strings_survive(self):
        self.assertEqual(sc.check_json(['{"url": "http://x.example/a"}'], "json"), [])

    def test_several_documents_in_one_fence_are_accepted(self):
        self.assertEqual(sc.check_json(['{"a": 1}', '{"b": 2}'], "json"), [])

    def test_still_broken_after_cleanup(self):
        self.assertEqual(sc.check_json(["{'a': 1}"], "json")[0][1], "SN002")


class YamlTests(unittest.TestCase):
    def test_clean_yaml(self):
        yaml = ["name: ci", "on:", "  push:", "    branches: [main]", "jobs:", "  test:", "    steps:", "      - uses: a/b@v1",
                "        with:", "          x: 1", "      - run: |", "          echo 'it''s'", "          echo \"unbalanced"]
        self.assertEqual(sc.check_yaml(yaml), [])

    def test_tab_duplicate_key_and_quotes(self):
        self.assertEqual(sc.check_yaml(["a:", "\tb: 1"])[0][1:3], ("SN004", "tab character used for indentation"))
        self.assertIn("duplicate key `a`", sc.check_yaml(["a: 1", "b: 2", "a: 3"])[0][2])
        self.assertIn("quote", sc.check_yaml(['name: "open'])[0][2])

    def test_same_key_in_different_list_items_is_fine(self):
        self.assertEqual(sc.check_yaml(["steps:", "  - name: a", "    run: x", "  - name: b", "    run: y"]), [])

    def test_same_key_in_different_documents_is_fine(self):
        self.assertEqual(sc.check_yaml(["a: 1", "---", "a: 2"]), [])


class TomlTests(unittest.TestCase):
    def test_unavailable_tomllib_skips_and_counts(self):
        report = scan({"README.md": fence("toml", "a = [")}, toml_loader=lambda: None)
        self.assertEqual(report["findings"], [])
        self.assertEqual(report["stats"]["toml_skipped"], 1)

    @unittest.skipIf(sc.load_tomllib() is None, "tomllib needs Python 3.11+")
    def test_valid_and_invalid_toml(self):
        self.assertEqual(sc.check_toml(['name = "x"', "[tool]", "a = 1"]), ([], True))
        problems, checked = sc.check_toml(["a = ", "b = 1"])
        self.assertTrue(checked)
        self.assertEqual(problems[0][1], "SN005")


class JsTests(unittest.TestCase):
    def balance(self, code):
        return sc.js_balance(textwrap.dedent(code).strip("\n"))

    def test_balanced_code_passes(self):
        self.assertIsNone(self.balance("""
            const re = /["'(]/g;
            const s = `a ${b} (`;
            // comment with ( and "
            /* block ( */
            function f(a, b) { return [a, {b}]; }
        """))

    def test_unclosed_and_unexpected(self):
        self.assertEqual(self.balance("f(1,\n  2")[1], "`(` is never closed")
        self.assertEqual(self.balance("x = 1)\n")[1], "unexpected `)`")
        self.assertEqual(self.balance("a = {\n  b: 1,\n")[0], 0)
        self.assertEqual(self.balance('const s = "open\nnext')[1], "string is not closed")

    def test_division_is_not_a_regex(self):
        self.assertIsNone(self.balance("const x = (a / b) + (c / d);"))

    def test_check_js_reports_rule(self):
        self.assertEqual(sc.check_js(["foo({"])[0][1], "SN006")


class ShellTests(unittest.TestCase):
    def problems(self, body, lang="shell"):
        return sc.check_shell(textwrap.dedent(body).strip("\n").split("\n"), lang)[0]

    def test_clean_commands_pass(self):
        self.assertEqual(self.problems("""
            # it's a comment with an apostrophe
            echo "hello 'world'"
            echo 'say "hi"'
            python x.py \\
              --flag value
            cat <<EOF
            it's heredoc text "
            EOF
        """), [])

    def test_unclosed_quotes_are_found_with_their_line(self):
        found = self.problems("echo ok\necho \"oops\nls\n")
        self.assertEqual([(p[0], p[1]) for p in found], [(1, "SN007")])
        self.assertIn("double", found[0][2])
        self.assertIn("single", self.problems("echo 'oops\n")[0][2])

    def test_multiline_quoted_string_is_fine(self):
        self.assertEqual(self.problems('echo "line one\nline two"\n'), [])

    def test_dangling_continuation(self):
        self.assertEqual(self.problems("echo hi \\\n")[0][1], "SN007")

    def test_console_fence_checks_only_prompted_lines(self):
        self.assertEqual(self.problems("$ echo hi\nit's output with a quote\n", "console"), [])

    def test_dangerous_patterns(self):
        cases = {
            "curl -sSL https://x.example/i.sh | sh": "medium",
            "wget -qO- https://x.example/i.sh | sudo bash": "medium",
            "bash <(curl -s https://x.example/i.sh)": "medium",
            "iwr https://x.example/i.ps1 | iex": "medium",
            "sudo rm -rf /var/lib/x": "medium",
            "rm -rf ~": "medium",
            "chmod 777 data": "medium",
            "chmod -R 777 data": "medium",
            "curl -k https://x.example": "low",
            "sudo pip install foo": "low",
        }
        for command, severity in cases.items():
            with self.subTest(command=command):
                found = self.problems(command)
                self.assertTrue(found, command)
                self.assertTrue(found[0][3].endswith("|" + severity), found)

    def test_harmless_commands_are_not_flagged(self):
        for command in ("curl -O https://x.example/a.tgz", "chmod +x run.sh", "chmod 755 run.sh", "rm -rf build/", "pip install foo"):
            with self.subTest(command=command):
                self.assertEqual(self.problems(command), [])

    def test_dangerous_findings_use_split_severity(self):
        findings = findings_for(fence("bash", "curl -k https://x.example\nchmod 777 x"))
        self.assertEqual(sorted((f["rule"], f["severity"]) for f in findings), [("SN008", "low"), ("SN008", "medium")])


class ImportTests(unittest.TestCase):
    FILES = {
        "mypkg/__init__.py": "from mypkg.core import Runner, load\n__version__ = '1.0'\n",
        "mypkg/core.py": "import os\nclass Runner: pass\ndef load(): pass\nCONST = 1\n",
        "mypkg/util/__init__.py": "",
        "mypkg/util/text.py": "def clean(): pass\n",
        "mypkg/star.py": "from os.path import *\n",
        "pyproject.toml": '[project]\nname = "mypkg"\nversion = "1.0.0"\n',
    }

    def imports(self, code, files=None):
        tree = dict(self.FILES)
        tree.update(files or {})
        tree["README.md"] = fence("python", code)
        return findings_for(tree["README.md"], {k: v for k, v in tree.items() if k != "README.md"})

    def test_valid_imports(self):
        self.assertEqual(self.imports("from mypkg import Runner, load, core\nfrom mypkg.core import CONST\nimport mypkg.util.text\nfrom mypkg.util import text\nimport os, json\n"), [])

    def test_missing_name_gets_a_suggestion(self):
        found = self.imports("from mypkg.core import Runnr")
        self.assertEqual([(f["rule"], f["suggestion"]) for f in found], [("SN010", "did you mean `Runner`?")])

    def test_missing_module_gets_a_suggestion(self):
        found = self.imports("from mypkg.cor import Runner\nimport mypkg.nothing")
        self.assertEqual([f["rule"] for f in found], ["SN009", "SN009"])
        self.assertIn("mypkg.core", found[0]["suggestion"])

    def test_star_import_modules_are_not_guessed(self):
        self.assertEqual(self.imports("from mypkg.star import anything"), [])

    def test_third_party_and_relative_imports_are_ignored(self):
        self.assertEqual(self.imports("from requests import get\nfrom . import x\nfrom .y import z"), [])

    def test_src_layout(self):
        files = {"src/lib/__init__.py": "def a(): pass\n"}
        found = self.imports("from lib import a, b", files)
        self.assertEqual([f["rule"] for f in found], ["SN010"])

    def test_syntax_error_snippet_skips_import_check(self):
        self.assertEqual(rules(self.imports("from mypkg.core import Nope\nprint 'x'")), ["SN001"])

    def test_js_named_imports_are_grepped_from_exports(self):
        files = {"package.json": '{"name": "pilot", "version": "1.0.0"}',
                 "src/index.js": "export function run() {}\nexport const VERSION = 1;\nexport { helper as assist };\n"}
        doc = fence("js", "import { run, assist, ruun } from 'pilot';\nconst { VERSION, gone } = require('pilot');")
        found = findings_for(doc, files)
        self.assertEqual(sorted(f["message"].split("`")[1] for f in found if f["rule"] == "SN017"), ["gone", "ruun"])

    def test_js_star_exports_disable_the_check(self):
        files = {"package.json": '{"name": "pilot"}', "src/index.js": "export * from './a';\nexport function x() {}\n"}
        self.assertEqual(findings_for(fence("js", "import { nope } from 'pilot';"), files), [])

    def test_go_import_of_a_missing_package(self):
        files = {"go.mod": "module example.com/acme/tool\n\ngo 1.21\n", "internal/a/a.go": "package a\n"}
        doc = fence("go", 'import (\n\t"example.com/acme/tool/internal/a"\n\t"example.com/acme/tool/internal/gone"\n)')
        found = findings_for(doc, files)
        self.assertEqual([f["rule"] for f in found], ["SN009"])


class CommandPathTests(unittest.TestCase):
    FILES = {"requirements.txt": "x\n", ".env.example": "A=1\n", "scripts/run.sh": "#!/bin/sh\n", "app.py": "x = 1\n",
             "package.json": '{"name": "p", "scripts": {"build": "x", "test": "y"}}', "Makefile": "build:\n\techo\n\n.PHONY: build\ntest:\n\techo\n",
             "pyproject.toml": '[project]\nname = "proj"\nversion = "1.0.0"\n', "src/keep.txt": "x"}

    def commands(self, body, lang="bash"):
        return findings_for(fence(lang, body), self.FILES)

    def test_existing_files_pass(self):
        self.assertEqual(self.commands("cp .env.example .env\npip install -r requirements.txt\npython app.py\nbash scripts/run.sh\n./scripts/run.sh\n"), [])

    def test_missing_files_are_reported_with_suggestions(self):
        found = self.commands("cp .env.sample .env\npip install -r requirement.txt\npython main.py\nbash scripts/start.sh\n")
        self.assertEqual([f["rule"] for f in found], ["SN014"] * 4)
        self.assertIn(".env.example", found[0]["suggestion"])
        self.assertIn("requirements.txt", found[1]["suggestion"])

    def test_created_files_and_unknown_state_suppress_checks(self):
        self.assertEqual(self.commands("echo hi > new.py\npython new.py\ntouch x.sh\nbash x.sh\n"), [])
        self.assertEqual(self.commands("curl -O https://x.example/a.zip\nunzip a.zip\npython other.py\n"), [])
        self.assertEqual(self.commands("git clone https://github.com/other/thing.git\ncd thing\npython nothere.py\n"), [])

    def test_cloning_this_project_resets_to_the_root(self):
        self.assertEqual(self.commands("git clone https://github.com/me/proj.git\ncd proj\npip install -r requirements.txt\n"), [])
        found = self.commands("git clone https://github.com/me/proj.git\ncd proj\npip install -r nope.txt\n")
        self.assertEqual([f["rule"] for f in found], ["SN014"])

    def test_cd_into_missing_folder(self):
        found = self.commands("cd missing\npython app.py\n")
        self.assertEqual([f["message"] for f in found], ["folder `missing` does not exist in the project"])

    def test_cd_into_existing_folder_changes_the_base(self):
        self.assertEqual(self.commands("cd src\ncat keep.txt\npython ../app.py\n"), [])

    def test_variables_globs_and_urls_are_never_checked(self):
        self.assertEqual(self.commands("cp $HOME/x .env\npython ~/a.py\nbash *.sh\ncp https://x.example/a b\n"), [])

    def test_npm_scripts_and_make_targets(self):
        found = self.commands("npm run build\nnpm run lint\nmake test\nmake deploy\n")
        self.assertEqual(sorted(f["message"] for f in found), [
            "make target `deploy` is not defined in the Makefile",
            "npm script `lint` is not defined in package.json"])

    def test_console_fence_only_looks_at_prompted_lines(self):
        self.assertEqual(self.commands("$ python app.py\npython ghost.py is just output text\n", "console"), [])

    def test_text_fence_without_prompts_is_not_treated_as_commands(self):
        self.assertEqual(self.commands("python ghost.py\n", "text"), [])

    def test_unlabeled_fence_with_prompts_is_checked(self):
        found = findings_for(fence("", "$ python ghost.py\n$ pip install foo"), self.FILES)
        self.assertEqual(rules(found), ["SN011", "SN014"])


class VersionTests(unittest.TestCase):
    FILES = {"pyproject.toml": '[project]\nname = "my-tool"\nversion = "2.3.1"\n',
             "package.json": '{"name": "@acme/my-tool", "version": "2.3.1"}'}

    def versions(self, doc, name="README.md"):
        return [f for f in findings_for(doc, self.FILES, name=name) if f["rule"] == "SN012"]

    def test_stale_pins_in_install_commands(self):
        doc = fence("bash", "pip install my-tool==1.0.0\nnpm install my-tool@1.2.0\ngo install example.com/x/my-tool@v1.0.0\npip install 'my_tool>=3.0'")
        self.assertEqual(len(self.versions(doc)), 4)

    def test_current_and_prefix_pins_are_fine(self):
        doc = fence("bash", "pip install my-tool==2.3.1\nnpm install my-tool@2.3\npip install my-tool>=2.0\nuses: acme/my-tool@v2")
        self.assertEqual(self.versions(doc), [])

    def test_other_packages_and_history_are_ignored(self):
        doc = "Run `pip install requests==2.0.0`.\n\nSince 1.0.0, `my-tool==1.0.0` was required; upgrade from my-tool==1.0.0.\n"
        self.assertEqual(self.versions(doc), [])

    def test_prose_latest_and_badge(self):
        doc = "The latest release is 1.9.0.\n\n![v](https://img.shields.io/badge/version-1.9.0-blue)\n"
        self.assertEqual(len(self.versions(doc)), 2)

    def test_changelog_file_is_not_checked_for_pins(self):
        self.assertEqual(self.versions("pip install my-tool==0.1.0\n", name="CHANGELOG.md"), [])

    def test_changelog_top_entry_versus_declared(self):
        files = dict(self.FILES, **{"CHANGELOG.md": "# Changelog\n\n## [Unreleased]\n\n## [2.2.0] - 2026-01-01\n\n## [2.1.0]\n"})
        found = scan(dict(files, **{"README.md": "x\n"}))["findings"]
        self.assertEqual([(f["rule"], f["file"]) for f in found], [("SN015", "CHANGELOG.md")])
        self.assertIn("behind", found[0]["message"])

    def test_matching_changelog_is_quiet(self):
        files = dict(self.FILES, **{"CHANGELOG.md": "## [2.3.1] - 2026-01-01\n", "README.md": "x\n"})
        self.assertEqual(scan(files)["findings"], [])

    def test_no_declared_version_means_no_version_findings(self):
        self.assertEqual(scan({"README.md": fence("bash", "pip install my-tool==0.0.1")})["findings"], [])

    def test_declared_sources(self):
        files = {"setup.py": "setup(name='abc', version='3.1.4')", "Cargo.toml": '[package]\nname = "crate"\nversion = "0.5.0"\n',
                 ".claude-plugin/plugin.json": '{"name": "plug", "version": "1.2.3"}', "go.mod": "module example.com/x/gomod\n"}
        with tempfile.TemporaryDirectory() as tmp:
            for rel, text in files.items():
                write(Path(tmp) / rel, text)
            declared = {(n, v, f) for n, v, f in sc.Project(tmp).declared()}
        self.assertEqual(declared, {("abc", "3.1.4", "setup.py"), ("crate", "0.5.0", "Cargo.toml"),
                                    ("plug", "1.2.3", ".claude-plugin/plugin.json"), ("example.com/x/gomod", None, "go.mod")})


class FunctionMentionTests(unittest.TestCase):
    CODE = {"app/core.py": "def load_tasks(path):\n    return []\n\nclass Runner:\n    def start(self):\n        pass\n    def run_all_jobs(self):\n        pass\n",
            "pyproject.toml": '[project]\nname = "app"\nversion = "1.0.0"\n'}

    def mentions(self, doc):
        return [f for f in findings_for(doc, self.CODE) if f["rule"] == "SN013"]

    def test_defined_names_are_fine(self):
        self.assertEqual(self.mentions("Call `load_tasks()` or `load_tasks(path)` or `Runner.run_all_jobs()`.\n"), [])

    def test_stale_name_with_suggestion_and_caveat(self):
        found = self.mentions("Call `load_task()` first.\n")
        self.assertEqual(len(found), 1)
        self.assertIn("possibly stale", found[0]["message"])
        self.assertEqual(found[0]["suggestion"], "did you mean `load_tasks`?")

    def test_stale_name_without_a_close_match(self):
        self.assertIn("check the current name", self.mentions("Use `frobnicate()`.\n")[0]["suggestion"])

    def test_library_and_builtin_calls_are_skipped(self):
        self.assertEqual(self.mentions("Use `requests.get()`, `json.dumps(x)`, `len(x)`, `print()` and `os.path.join(a, b)`.\n"), [])

    def test_dotted_call_on_a_project_class_is_checked(self):
        self.assertEqual(len(self.mentions("Use `Runner.vanished()`.\n")), 1)

    def test_short_and_common_names_are_skipped(self):
        self.assertEqual(self.mentions("Use `go()` and `main()`.\n"), [])

    def test_code_fences_are_not_prose(self):
        self.assertEqual(self.mentions(fence("python", "vanished()")), [])

    def test_no_code_means_no_check(self):
        self.assertEqual(findings_for("Use `vanished()`.\n"), [])

    def test_changelog_mentions_are_history(self):
        self.assertEqual([f for f in findings_for("Removed `vanished()`.\n", self.CODE, name="CHANGELOG.md")
                          if f["rule"] == "SN013"], [])


class LanguageGuessTests(unittest.TestCase):
    def test_unlabeled_fences(self):
        cases = {'{"a": 1,\n "b": 2}': "json", "$ pip install x\n$ x --help": "bash", "pip install x\nnpm install y": "bash",
                 "import os\nprint(os.name)": "python"}
        for body, lang in cases.items():
            with self.subTest(lang=lang):
                self.assertEqual(sc.guess_language(body.split("\n")), lang)
        self.assertIsNone(sc.guess_language(["docs/", "  a.md"]))
        self.assertIsNone(sc.guess_language(["hello"]))

    def test_unlabeled_json_reports_low(self):
        found = findings_for(fence("", '{\n  "a": 1\n}'))
        self.assertEqual([(f["rule"], f["severity"]) for f in found], [("SN011", "low")])

    def test_unchecked_languages_are_counted_not_guessed(self):
        report = scan({"README.md": fence("rust", "fn main() {") + fence("text", "anything ( ") + fence("ruby", "puts 1")})
        self.assertEqual(report["findings"], [])
        self.assertEqual(report["stats"]["unchecked"], {"ruby": 1, "rust": 1})


class SampleProjectTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        files = list(sc.walk(str(SAMPLE), sc.SKIP_DIRS, [], sc.MD_EXT))
        cls.report = sc.analyse(str(SAMPLE), files)

    def test_expected_rules(self):
        self.assertEqual(rules(self.report["findings"]), ["SN001", "SN003", "SN008", "SN008", "SN009", "SN010",
                                                          "SN012", "SN013", "SN014", "SN014"])

    def test_install_and_quick_start_findings_are_flagged_first_run(self):
        first_run = [f for f in self.report["findings"] if f["first_run"]]
        self.assertEqual(len(first_run), 9)
        self.assertFalse([f for f in self.report["findings"] if f["file"] == "docs/usage.md" and f["first_run"]])


class CliTests(unittest.TestCase):
    def test_exit_codes(self):
        self.assertEqual(run([str(SAMPLE)])[0], 0)
        self.assertEqual(run([str(SAMPLE), "--fail-on", "high"])[0], 1)
        self.assertEqual(run([str(SAMPLE), "--fail-on", "low"])[0], 1)
        with tempfile.TemporaryDirectory() as tmp:
            write(Path(tmp) / "README.md", fence("python", "x = 1"))
            self.assertEqual(run([tmp, "--fail-on", "low"])[0], 0)
            write(Path(tmp) / "README.md", fence("python", "x = 1") + fence("bash", "chmod 777 a"))
            self.assertEqual(run([tmp, "--fail-on", "high"])[0], 0)
            self.assertEqual(run([tmp, "--fail-on", "medium"])[0], 1)

    def test_bad_input_exits_2(self):
        self.assertEqual(run(["no-such-folder-xyz"])[0], 2)
        with tempfile.TemporaryDirectory() as tmp:
            code, _out, err = run([tmp])
            self.assertEqual(code, 2)
            self.assertIn("no Markdown", err)
            write(Path(tmp) / "a.txt", "x")
            self.assertEqual(run([str(Path(tmp) / "a.txt")])[0], 2)
        with self.assertRaises(SystemExit) as caught, redirect_stderr(io.StringIO()):
            sc.main(["--fail-on", "nope"])
        self.assertEqual(caught.exception.code, 2)

    def test_single_file_and_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(Path(tmp) / "docs" / "a.md", fence("python", "print 'x'"))
            code, out, _ = run([str(Path(tmp) / "docs" / "a.md"), "--root", tmp, "--json"])
            self.assertEqual(json.loads(out)["findings"][0]["file"], "docs/a.md")

    def test_json_output_shape(self):
        code, out, _ = run([str(SAMPLE), "--json"])
        report = json.loads(out)
        self.assertEqual(report["tool"], "snippet_check")
        self.assertEqual(report["summary"]["declared_version"], "2.1.0")
        for key in ("rule", "severity", "file", "line", "message", "suggestion", "section", "first_run", "lang"):
            self.assertIn(key, report["findings"][0])

    def test_markdown_states_that_nothing_is_executed_and_is_deterministic(self):
        a, b = run([str(SAMPLE)])[1], run([str(SAMPLE)])[1]
        self.assertEqual(a, b)
        self.assertIn("No snippet was executed", a)
        self.assertNotIn(str(ROOT), a)

    def test_subprocess_run(self):
        proc = subprocess.run([sys.executable, str(SCRIPT), str(SAMPLE), "--fail-on", "high"], capture_output=True,
                              text=True, encoding="utf-8")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("SN001", proc.stdout)

    def test_exclude(self):
        code, out, _ = run([str(SAMPLE), "--json", "--exclude", "README.md"])
        self.assertTrue(all(f["file"] != "README.md" for f in json.loads(out)["findings"]))

    def test_source_is_python_39_syntax(self):
        ast.parse(SCRIPT.read_text(encoding="utf-8"), feature_version=(3, 9))

    def test_the_script_never_calls_exec_eval_or_subprocess(self):
        tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                names.add(node.func.id)
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names.update(a.name.split(".")[0] for a in node.names)
        self.assertFalse(names & {"exec", "eval", "compile", "subprocess", "os.system", "runpy"}, names)


if __name__ == "__main__":
    unittest.main()
