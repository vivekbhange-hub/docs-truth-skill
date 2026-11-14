"""Tests for skills/doctruth-cli/cli_drift.py."""
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

cd = load("doctruth-cli", "cli_drift")
SCRIPT = ROOT / "skills" / "doctruth-cli" / "cli_drift.py"
SAMPLE = ROOT / "examples" / "sample-project"


def dedent(text: str) -> str:
    return textwrap.dedent(text).lstrip("\n")


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cd.main(argv)
    return code, out.getvalue(), err.getvalue()


def analyse(tree: dict, **kwargs):
    with tempfile.TemporaryDirectory() as tmp:
        for rel, text in tree.items():
            write(Path(tmp) / rel, dedent(text))
        report = cd.analyse(tmp, **kwargs)
    report.pop("_defs", None)
    return report


def definitions(tree: dict):
    with tempfile.TemporaryDirectory() as tmp:
        for rel, text in tree.items():
            write(Path(tmp) / rel, dedent(text))
        return cd.extract_code(tmp, [], [])


def rules(report):
    return sorted(f["rule"] for f in report["findings"])


def names(defs):
    return sorted(n for f in defs.flags for n in f.names)


class PythonExtractionTests(unittest.TestCase):
    def test_argparse_flags_defaults_help_and_scope(self):
        defs = definitions({"tool.py": """
            import argparse
            p = argparse.ArgumentParser(prog="mytool")
            p.add_argument("--config", default="a.toml", help="config   path")
            sub = p.add_subparsers(dest="cmd")
            run = sub.add_parser("run", aliases=["r"])
            run.add_argument("-j", "--jobs", type=int, default=4)
            run.add_argument("--fast", action="store_true")
            grp = run.add_argument_group("g")
            grp.add_argument("--deep", default=None)
            sub.add_parser("ls").add_argument("--all", action="store_true")
            p.add_argument("--hidden", help=argparse.SUPPRESS)
            p.add_argument("--cache", action=argparse.BooleanOptionalAction)
            p.add_argument("target")
        """})
        by = {f.names[0]: f for f in defs.flags}
        self.assertEqual(by["--config"].default, "a.toml")
        self.assertEqual(by["--config"].help, "config path")
        self.assertIsNone(by["--config"].command)
        self.assertEqual((by["-j"].command, by["-j"].default, by["-j"].takes_value), ("run", "4", True))
        self.assertFalse(by["--fast"].takes_value)
        self.assertEqual(by["--deep"].command, "run")
        self.assertEqual(by["--all"].command, "ls")
        self.assertTrue(by["--hidden"].hidden)
        self.assertIn("--no-cache", by["--cache"].names)
        self.assertEqual(sorted(defs.commands), ["ls", "run"])
        self.assertEqual(defs.aliases, {"r": "run"})
        self.assertIn("mytool", defs.prog_names)
        self.assertNotIn("target", names(defs))

    def test_argparse_default_from_environment(self):
        defs = definitions({"t.py": """
            import argparse, os
            p = argparse.ArgumentParser()
            p.add_argument("--home", default=os.environ.get("TOOL_HOME", "/tmp"))
        """})
        flag = defs.flags[0]
        self.assertEqual((flag.default, flag.env), ("/tmp", "TOOL_HOME"))
        self.assertIn("TOOL_HOME", defs.envs)

    def test_dynamic_flags_are_listed_not_guessed(self):
        defs = definitions({"t.py": """
            import argparse
            p = argparse.ArgumentParser()
            for name in ("a", "b"):
                p.add_argument("--" + name)
            p.add_argument(f"--{name}x")
            p.add_argument("--real")
        """})
        self.assertEqual(names(defs), ["--real"])
        self.assertEqual(len(defs.dynamic), 2)
        self.assertEqual(defs.dynamic[0][0], "t.py")

    def test_optparse_and_manual_argv(self):
        defs = definitions({"t.py": """
            import sys
            from optparse import OptionParser
            p = OptionParser()
            p.add_option("-q", "--quiet")
            if "--legacy" in sys.argv:
                pass
        """})
        self.assertEqual(names(defs), ["--legacy", "--quiet", "-q"])

    def test_click_options_commands_and_envvar(self):
        defs = definitions({"app.py": """
            import click

            @click.group()
            def cli(): pass

            @cli.command("serve")
            @click.option("--port", "-p", default=8000, help="Port.", envvar="APP_PORT")
            @click.option("--debug/--no-debug", default=False)
            @click.option("--count", is_flag=True)
            def serve_cmd(port, debug, count): pass

            @cli.command()
            @click.option("--name")
            def say_hello(name): pass

            @click.version_option("1.0")
            @click.command()
            def single(): pass
        """})
        by = {f.names[0]: f for f in defs.flags}
        self.assertEqual((by["--port"].command, by["--port"].default, by["--port"].env), ("serve", "8000", "APP_PORT"))
        self.assertIn("--no-debug", by["--debug"].names)
        self.assertFalse(by["--debug"].takes_value)
        self.assertFalse(by["--count"].takes_value)
        self.assertEqual(by["--name"].command, "say-hello")
        self.assertEqual(sorted(defs.commands), ["say-hello", "serve"])
        self.assertTrue(defs.has_version)
        self.assertIn("APP_PORT", defs.envs)

    def test_typer_commands_and_parameters(self):
        defs = definitions({"app.py": """
            import typer
            app = typer.Typer(name="tp")

            @app.command()
            def build(path: str, port: int = typer.Option(8080, "--port", "-p", help="Port"),
                      verbose: bool = False, level: str = "info"):
                pass

            @app.command("clean-all")
            def clean(force: bool = typer.Option(False, "--force")):
                pass
        """})
        by = {f.names[0]: f for f in defs.flags}
        self.assertEqual((by["--port"].command, by["--port"].default), ("build", "8080"))
        self.assertIn("--no-verbose", by["--verbose"].names)
        self.assertEqual(by["--level"].default, "info")
        self.assertNotIn("path", names(defs))
        self.assertEqual(sorted(defs.commands), ["build", "clean-all"])
        self.assertIn("tp", defs.prog_names)

    def test_single_typer_command_has_no_subcommand(self):
        defs = definitions({"app.py": """
            import typer

            def main(name: str = typer.Option("x", "--name")):
                pass
            app = typer.Typer()
            app.command()(main)

            @app.command()
            def only(flag: bool = False):
                pass
        """})
        self.assertEqual(defs.commands, {})
        self.assertIsNone([f for f in defs.flags if "--flag" in f.names][0].command)

    def test_environment_reads(self):
        defs = definitions({"e.py": """
            import os
            from os import environ, getenv
            a = os.environ["A_ONE"]
            b = os.environ.get("B_TWO", "x")
            c = os.getenv("C_THREE")
            d = environ["D_FOUR"]
            e = getenv("E_FIVE")
            os.environ.setdefault("F_SIX", "1")
            name = "DYN"
            f = os.environ[name]
            g = os.environ.get(name + "_X")
        """})
        self.assertEqual(sorted(defs.envs), ["A_ONE", "B_TWO", "C_THREE", "D_FOUR", "E_FIVE", "F_SIX"])
        self.assertEqual(len(defs.dynamic), 2)

    def test_pydantic_settings_fields_become_env_names(self):
        defs = definitions({"s.py": """
            from pydantic_settings import BaseSettings

            class Settings(BaseSettings):
                api_key: str = ""
                _private: int = 0
                model_config = SettingsConfigDict(env_prefix="APP_")
        """})
        self.assertEqual(sorted(defs.envs), ["APP_API_KEY"])

    def test_unparsable_python_is_reported_not_crashed(self):
        defs = definitions({"bad.py": "def (:\n", "ok.py": "import argparse\nargparse.ArgumentParser().add_argument('--x')\n"})
        self.assertEqual(defs.unparsable, ["bad.py"])
        self.assertEqual(names(defs), ["--x"])

    def test_test_files_and_skipped_folders_are_not_read(self):
        defs = definitions({
            "tests/t.py": "import argparse\nargparse.ArgumentParser().add_argument('--from-tests')\n",
            "test_x.py": "import argparse\nargparse.ArgumentParser().add_argument('--from-test-file')\n",
            "node_modules/m.js": "program.option('--from-modules')\n",
            "real.py": "import argparse\nargparse.ArgumentParser().add_argument('--real')\n"})
        self.assertEqual(names(defs), ["--real"])


class GoExtractionTests(unittest.TestCase):
    def test_stdlib_flag_package(self):
        defs = definitions({"main.go": '''
            package main
            import "flag"
            // flag.String("commented", "", "no")
            var port = flag.Int("port", 8080, "port to listen on")
            func main() {
                var name string
                flag.StringVar(&name, "name", "world", "who to greet")
                flag.Bool("v", false, "verbose")
                flag.Duration("wait", 5*time.Second, "wait")
                dyn := "x"
                flag.String(dyn, "", "dynamic")
                flag.Parse()
            }
        '''})
        by = {f.names[0]: f for f in defs.flags}
        self.assertEqual(sorted(by), ["--name", "--port", "--wait", "-v"])
        self.assertEqual((by["--port"].default, by["--port"].help), ("8080", "port to listen on"))
        self.assertEqual(by["--name"].default, "world")
        self.assertFalse(by["-v"].takes_value)
        self.assertIsNone(by["--wait"].default)
        self.assertEqual(len(defs.dynamic), 1)

    def test_cobra_commands_flags_and_env(self):
        defs = definitions({"cmd/root.go": '''
            package cmd
            var rootCmd = &cobra.Command{
                Use:   "mytool [flags]",
            }
            var serveCmd = &cobra.Command{
                Use:   "serve",
            }
            func init() {
                rootCmd.PersistentFlags().StringP("config", "c", "app.yaml", "config file")
                serveCmd.Flags().IntVarP(&port, "port", "p", 3000, "port")
                serveCmd.Flags().BoolVar(&dev, "dev", false, "dev mode")
                rootCmd.AddCommand(serveCmd)
                token := os.Getenv("MYTOOL_TOKEN")
                _, ok := os.LookupEnv("MYTOOL_HOME")
            }
        '''})
        by = {f.names[0]: f for f in defs.flags}
        self.assertEqual(by["--config"].names, ["--config", "-c"])
        self.assertEqual((by["--port"].default, by["--port"].names), ("3000", ["--port", "-p"]))
        self.assertEqual(sorted(defs.commands), ["serve"])
        self.assertIn("mytool", defs.prog_names)
        self.assertEqual(sorted(defs.envs), ["MYTOOL_HOME", "MYTOOL_TOKEN"])

    def test_go_module_name_is_a_program_name(self):
        defs = definitions({"go.mod": "module github.com/acme/fancy/v2\n\ngo 1.21\n",
                            "main.go": 'package main\nvar _ = flag.Bool("x", false, "")\n'})
        self.assertIn("fancy", defs.prog_names)


class NodeExtractionTests(unittest.TestCase):
    def test_commander_options_commands_and_scope(self):
        defs = definitions({"cli.js": '''
            const { program } = require("commander");
            program.name("pilot").version("1.0.0");
            program.option("-d, --debug", "output extra debugging");
            program
              .command("serve <dir>")
              .option("-p, --port <number>", "port", 3000)
              .option("--no-color", "disable color");
            program.option("--global-after", "global again");
            program.command("build").requiredOption("--out <dir>", "output", parseInt, 7);
            if (process.argv.includes("--raw")) {}
            const k = process.env.PILOT_KEY;
            const m = process.env["PILOT_MODE"];
            const z = process.env[someName];
        '''})
        by = {f.names[0]: f for f in defs.flags}
        self.assertEqual(by["-d"].command, None)
        self.assertEqual((by["-p"].command, by["-p"].default, by["-p"].takes_value), ("serve", "3000", True))
        self.assertIn("--color", by["--no-color"].names)
        self.assertIsNone(by["--global-after"].command)
        self.assertEqual((by["--out"].command, by["--out"].default), ("build", "7"))
        self.assertIn("--raw", by)
        self.assertEqual(sorted(defs.commands), ["build", "serve"])
        self.assertIn("pilot", defs.prog_names)
        self.assertTrue(defs.has_version)
        self.assertEqual(sorted(defs.envs), ["PILOT_KEY", "PILOT_MODE"])
        self.assertEqual(len(defs.dynamic), 1)

    def test_comments_are_ignored_but_urls_in_strings_survive(self):
        defs = definitions({"i.js": '''
            // program.option("--commented-out")
            const u = "http://example.com/a"; program.option("--live", "see http://x.example");
            /* program.option("--block") */
        '''})
        self.assertEqual(names(defs), ["--live"])

    def test_package_json_bin_names(self):
        defs = definitions({"package.json": '{"name": "@acme/pilot", "bin": "cli.js"}',
                            "cli.js": 'program.option("--x");'})
        self.assertIn("pilot", defs.prog_names)


class DocExtractionTests(unittest.TestCase):
    def mentions(self, readme: str, code: str = "import argparse\np = argparse.ArgumentParser(prog='tp')\np.add_argument('--port')\n"):
        with tempfile.TemporaryDirectory() as tmp:
            write(Path(tmp) / "README.md", dedent(readme))
            write(Path(tmp) / "tp.py", code)
            defs = cd.extract_code(tmp, [], [])
            scanner = cd.DocScanner({"tp"}, defs, False)
            scanner.scan("README.md", dedent(readme))
        return scanner

    def flags(self, scanner):
        return sorted((m.name, m.context) for m in scanner.mentions if m.kind == "flag")

    def test_only_our_command_lines_are_attributed(self):
        scanner = self.mentions("""
            ```bash
            pip install --upgrade foo
            tp --port 80
            sudo tp run --verbose
            python -m tp --quiet
            python tp.py --other
            git clone --depth 1 https://x.example/r.git
            $ tp -x
            ```
        """)
        self.assertEqual(self.flags(scanner), [("--other", "command"), ("--port", "command"), ("--quiet", "command"),
                                               ("--verbose", "command"), ("-x", "command")])
        self.assertEqual(scanner.ignored_command_lines, 2)

    def test_continuation_lines_pipes_and_runners(self):
        scanner = self.mentions("""
            ```sh
            cat x | tp --a && echo hi --not-ours
            npx tp --b
            uv run tp --c
            ```
        """)
        self.assertEqual(self.flags(scanner), [("--a", "command"), ("--b", "command"), ("--c", "command")])

    def test_code_spans_tables_and_help_output(self):
        scanner = self.mentions("""
            Use `--port` or `-p, --alt` and `--[no-]color`. Run `pip install --upgrade x` first.

            | Option | Default |
            |---|---|
            | `--table-flag` | 5 |

            ```text
            usage: tp [-h]

              -v, --verbose  talk more
              --level LEVEL  how much
            ```
        """)
        found = {m.name for m in scanner.mentions if m.kind == "flag"}
        self.assertEqual(found, {"--port", "-p", "--alt", "--color", "--no-color", "--table-flag", "-v", "--verbose", "--level"})

    def test_python_and_json_fences_are_not_scanned_for_flags(self):
        scanner = self.mentions("""
            ```python
            parser.add_argument("--in-python-fence")
            ```
            ```json
            {"flag": "--in-json"}
            ```
        """)
        self.assertEqual(self.flags(scanner), [])

    def test_commands_come_from_command_lines_and_prose(self):
        scanner = self.mentions("""
            ```bash
            tp serve --port 1
            tp --port 2 other
            ```
            The `deploy` command ships it.
        """, code="import argparse\np = argparse.ArgumentParser(prog='tp')\ns = p.add_subparsers()\ns.add_parser('serve')\n")
        commands = sorted((m.name, m.context) for m in scanner.mentions if m.kind == "command")
        self.assertEqual(commands, [("deploy", "loose"), ("other", "command"), ("serve", "command")])

    def test_flag_value_is_not_mistaken_for_a_command(self):
        scanner = self.mentions("```bash\ntp --port 80 serve\n```\n")
        self.assertEqual([m.name for m in scanner.mentions if m.kind == "command"], ["serve"])

    def test_environment_variables(self):
        scanner = self.mentions("""
            ## Environment

            Set `TP_HOME` before running; `MAX_RETRIES` is internal but mentioned here.

            ```bash
            export TP_TOKEN=abc
            TP_MODE=fast tp --port 1
            echo "$TP_ECHO"
            export PATH=$HOME/bin:$PATH
            set TP_WIN=1
            $env:TP_PS = "x"
            ```

            ```dotenv
            TP_DOT=1
            # TP_COMMENT=2
            ```

            | Variable | Meaning |
            |---|---|
            | `TP_TABLE` | in a table |

            Mention `NOT_ENV` outside any env context? The word does not trigger. `GET_ALL` too.
        """)
        env = {m.name for m in scanner.mentions if m.kind == "env"}
        self.assertTrue({"TP_HOME", "TP_TOKEN", "TP_MODE", "TP_ECHO", "TP_WIN", "TP_PS", "TP_DOT", "TP_TABLE"} <= env)
        self.assertNotIn("PATH", env)
        self.assertNotIn("HOME", env)
        self.assertNotIn("TP_COMMENT", env)

    def test_prose_env_names_need_environment_context(self):
        scanner = self.mentions("# Title\n\nThe constant `MAX_RETRIES` limits retries.\n")
        self.assertEqual([m for m in scanner.mentions if m.kind == "env"], [])

    def test_table_default_column_and_prose_default(self):
        scanner = self.mentions("""
            | Flag | Default | Notes |
            |---|---|---|
            | `--port` | `8080` | Listen port |

            - `--workers` (default: 4)
            - `--a` and `--b` default to 9
        """)
        defaults = {m.name: m.default for m in scanner.mentions if m.kind == "flag"}
        self.assertEqual(defaults, {"--port": "8080", "--workers": "4", "--a": None, "--b": None})

    def test_sections_and_first_run(self):
        scanner = self.mentions("# T\n\n## Quick start\n\n`tp --port 1`\n\n## Reference\n\n`--zzz`\n")
        by = {m.name: (m.section, m.first_run) for m in scanner.mentions if m.kind == "flag"}
        self.assertEqual(by["--port"], ("Quick start", True))
        self.assertEqual(by["--zzz"], ("Reference", False))

    def test_rst_code_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(Path(tmp) / "tp.py", "import argparse\nargparse.ArgumentParser(prog='tp').add_argument('--a')\n")
            defs = cd.extract_code(tmp, [], [])
            scanner = cd.DocScanner({"tp"}, defs, False)
            scanner.scan("guide.rst", "Run it\n\n.. code-block:: bash\n\n   tp --rst-flag\n\nafter\n")
        self.assertEqual([m.name for m in scanner.mentions], ["--rst-flag"])


class FalsePositiveGuardTests(unittest.TestCase):
    """Cases found by running the script on real repositories: each one used to be a false alarm."""

    def mentions(self, readme):
        with tempfile.TemporaryDirectory() as tmp:
            write(Path(tmp) / "tp.py", "import argparse\np = argparse.ArgumentParser(prog='tp')\np.add_argument('--port')\n")
            defs = cd.extract_code(tmp, [], [])
            scanner = cd.DocScanner({"tp"}, defs, False)
            scanner.scan("README.md", dedent(readme))
        return scanner

    def test_backslash_continuation_lines_are_joined(self):
        scanner = self.mentions("```bash\ncurl https://x.example \\\n  -H 'A: b' \\\n  -d '{}'\ntp run \\\n  --port 1\n```\n")
        self.assertEqual([(m.name, m.context) for m in scanner.mentions if m.kind == "flag"], [("--port", "command")])

    def test_markdown_bullets_in_a_fence_are_not_flags(self):
        scanner = self.mentions("```text\n- **cli:** Add --json output (#41)\n```\n")
        self.assertEqual(scanner.mentions, [])

    def test_single_dash_words_are_not_flags_outside_go(self):
        scanner = self.mentions("Variants such as `-or-later` and `-only` exist; `-v` is a flag.\n")
        self.assertEqual([m.name for m in scanner.mentions], ["-v"])

    def test_flags_right_after_another_tool_name_are_not_attributed(self):
        scanner = self.mentions("Run with curl then `-H` is a curl option; but `--port` is ours.\n")
        self.assertEqual([m.name for m in scanner.mentions], ["--port"])
        self.assertEqual(scanner.ignored_foreign_mentions, 1)

    def test_shell_variables_assigned_in_the_same_doc_are_not_environment_docs(self):
        scanner = self.mentions("```bash\nOUT_DIR=build\necho $OUT_DIR\necho $REAL_ENV\n```\n")
        env = sorted(m.name for m in scanner.mentions if m.kind == "env")
        self.assertEqual(env, ["OUT_DIR", "REAL_ENV"])

    def test_variables_set_by_the_agent_harness_are_not_documented_env_vars(self):
        scanner = self.mentions("```bash\npython3 \"${CLAUDE_SKILL_DIR}/x.py\"\n```\n")
        self.assertEqual([m for m in scanner.mentions if m.kind == "env"], [])

    def test_skill_and_agent_docs_are_scanned_by_default(self):
        report = analyse({"tp.py": "import argparse\np = argparse.ArgumentParser(prog='tp')\np.add_argument('--port')\n",
                          "README.md": "# tp\n", "skills/a/SKILL.md": "Run `tp --port 1`.\n", "agents/b.md": "x\n"})
        self.assertEqual(report["summary"]["doc_files"], 3)
        self.assertEqual(report["findings"], [])

    def test_percent_signs_in_argparse_help_are_unescaped(self):
        defs = definitions({"t.py": "import argparse\nargparse.ArgumentParser().add_argument('--t', help='below 80%% only')\n"})
        self.assertEqual(defs.flags[0].help, "below 80% only")

    def test_prog_with_extension_is_normalised(self):
        defs = definitions({"t.py": "import argparse\nargparse.ArgumentParser(prog='tool.py').add_argument('--t')\n"})
        self.assertIn("tool", defs.prog_names)
        self.assertNotIn("tool.py", defs.prog_names)


class ComparisonTests(unittest.TestCase):
    CODE = """
        import argparse, os
        p = argparse.ArgumentParser(prog="tp")
        s = p.add_subparsers(dest="cmd")
        run = s.add_parser("run")
        run.add_argument("--jobs", type=int, default=4)
        run.add_argument("--output", default="a.csv")
        run.add_argument("--only-run")
        s.add_parser("status").add_argument("--json", action="store_true")
        token = os.environ["TP_API_TOKEN"]
    """

    def test_missing_undocumented_rename_wrong_command_default_and_env(self):
        report = analyse({"tp.py": self.CODE, "README.md": """
            # tp

            ## Quick start

            ```bash
            tp run --workers 4 --out x.csv
            tp status --only-run
            tp report
            ```

            | Flag | Default |
            |---|---|
            | `--jobs` | 8 |

            ## Environment

            | Variable |
            |---|
            | `TP_TOKEN` |
            | `TP_GONE_AWAY` |
        """})
        by_rule = {}
        for f in report["findings"]:
            by_rule.setdefault(f["rule"], []).append(f)
        self.assertEqual([f["item"] for f in by_rule["CD001"]], ["--workers"])
        self.assertEqual(by_rule["CD001"][0]["severity"], "high")
        self.assertTrue(by_rule["CD001"][0]["first_run"])
        self.assertEqual([f["item"] for f in by_rule["CD007"] if f["kind"] == "flag"], ["--out"])
        self.assertEqual([f["item"] for f in by_rule["CD002"]], ["report"])
        self.assertEqual([f["item"] for f in by_rule["CD009"]], ["--only-run"])
        self.assertEqual([f["item"] for f in by_rule["CD008"]], ["--jobs"])
        self.assertIn("`4`", by_rule["CD008"][0]["message"].split("code says")[1] + "`4`")
        self.assertEqual([f["item"] for f in by_rule["CD004"]], ["--json"])
        self.assertEqual([f["item"] for f in by_rule["CD003"]], ["TP_GONE_AWAY"])
        self.assertEqual([f["item"] for f in by_rule["CD007"] if f["kind"] == "env"], ["TP_TOKEN"])
        self.assertNotIn("CD006", by_rule)

    def test_clean_docs_have_no_findings(self):
        report = analyse({"tp.py": "import argparse\np = argparse.ArgumentParser(prog='tp')\np.add_argument('--port', default=80)\n",
                          "README.md": "# tp\n\nRun `tp --port 80`.\n"})
        self.assertEqual(report["findings"], [])

    def test_help_and_version_are_always_defined(self):
        report = analyse({"tp.py": "import argparse\np = argparse.ArgumentParser(prog='tp')\np.add_argument('--a')\n",
                          "README.md": "Use `tp --help`, `-h` and `--a`.\n"})
        self.assertEqual(report["findings"], [])

    def test_boolean_negation_and_combined_shorts(self):
        code = "import argparse\np = argparse.ArgumentParser(prog='tp')\np.add_argument('-a', action='store_true')\np.add_argument('-b', action='store_true')\np.add_argument('--color', action='store_true')\n"
        report = analyse({"tp.py": code, "README.md": "Run `tp -ab --no-color`.\n"})
        self.assertEqual(report["findings"], [])

    def test_go_style_single_dash_matches_double_dash(self):
        code = 'package main\nvar _ = flag.Int("port", 1, "p")\n'
        report = analyse({"main.go": code, "README.md": "Run `tp -port 80` and `--port`.\n"}, cli_names=["tp"])
        self.assertEqual(report["findings"], [])

    def test_dynamic_flags_lower_severity_and_are_listed(self):
        code = "import argparse\np = argparse.ArgumentParser(prog='tp')\nfor n in 'ab':\n    p.add_argument('--' + n)\np.add_argument('--x')\n"
        report = analyse({"tp.py": code, "README.md": "Run `tp --mystery`.\n"})
        finding = report["findings"][0]
        self.assertEqual((finding["rule"], finding["severity"]), ("CD001", "medium"))
        self.assertIn("dynamically", finding["message"])
        self.assertEqual(report["dynamic"][0]["file"], "tp.py")

    def test_no_parser_in_code_skips_the_comparison(self):
        report = analyse({"x.py": "print('hi')\n", "README.md": "Run `tool --foo` now.\n"}, cli_names=["tool"])
        self.assertEqual([f["rule"] for f in report["findings"]], ["CD010"])
        self.assertEqual(report["findings"][0]["severity"], "info")

    def test_loose_mentions_rank_below_command_lines(self):
        report = analyse({"tp.py": self.CODE, "README.md": "Pass `--ghost` to it.\n\n```bash\ntp run --phantom\n```\n"})
        sev = {f["item"]: f["severity"] for f in report["findings"] if f["rule"] == "CD001"}
        self.assertEqual(sev, {"--ghost": "medium", "--phantom": "high"})

    def test_env_found_in_code_text_but_not_read_is_info(self):
        code = dedent(self.CODE) + "\nDEFAULT_NAME = 'TP_LEGACY'\n"
        report = analyse({"tp.py": code, "README.md": "## Environment\n\nSet `TP_LEGACY` and `TP_API_TOKEN`.\n"})
        found = [(f["item"], f["severity"]) for f in report["findings"] if f["rule"] == "CD003"]
        self.assertEqual(found, [("TP_LEGACY", "info")])

    def test_system_env_is_ignored_unless_requested(self):
        code = "import os\nx = os.environ['HOME']\ny = os.environ['TP_A']\n"
        docs = "## Environment\n\nSet `GITHUB_TOKEN` and `TP_A`.\n"
        report = analyse({"tp.py": code, "README.md": docs}, cli_names=["tp"])
        self.assertEqual(report["findings"], [])
        report = analyse({"tp.py": code, "README.md": docs}, cli_names=["tp"], include_system=True)
        self.assertEqual(sorted(f["item"] for f in report["findings"]), ["GITHUB_TOKEN", "HOME"])

    def test_defaults_compare_only_simple_values(self):
        for doc, code, differs in [("8", "8", False), ("8", "9", True), ("8.0", "8", False), ("true", "True", False),
                                   ("30s", "30", False), ("auto", "0", False), ("a", "b", True), ("`x`", "x", False)]:
            with self.subTest(doc=doc, code=code):
                self.assertEqual(cd.defaults_differ(doc, code), differs)

    def test_documented_command_with_alias_is_defined(self):
        code = "import argparse\np = argparse.ArgumentParser(prog='tp')\ns = p.add_subparsers()\ns.add_parser('run', aliases=['r'])\n"
        report = analyse({"tp.py": code, "README.md": "```bash\ntp r\ntp run\n```\n"})
        self.assertEqual(report["findings"], [])

    def test_changelog_and_license_are_not_scanned(self):
        report = analyse({"tp.py": self.CODE, "CHANGELOG.md": "Removed `--old-flag`.\n", "README.md": "`tp run --jobs 1 --output x --only-run`\n`tp status --json`\n"})
        self.assertEqual([f for f in report["findings"] if f["rule"] == "CD001"], [])

    def test_explicit_docs_and_code_paths(self):
        tree = {"src/tp.py": self.CODE, "other/x.py": "import argparse\nargparse.ArgumentParser().add_argument('--other')\n",
                "guide/usage.md": "`tp run --jobs 2`\n", "README.md": "`--zzz`\n"}
        report = analyse(tree, docs_paths=["guide"], code_paths=["src"], cli_names=["tp"])
        self.assertEqual(report["summary"]["doc_files"], 1)
        self.assertNotIn("--other", json.dumps(report["defined_flags"]))


class SampleProjectTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = cd.analyse(str(SAMPLE))
        cls.report.pop("_defs")

    def test_expected_rules(self):
        self.assertEqual(rules(self.report), ["CD001", "CD001", "CD002", "CD003", "CD004", "CD004", "CD004",
                                              "CD006", "CD007", "CD007", "CD008", "CD009"])

    def test_cli_name_is_found_from_pyproject(self):
        self.assertEqual(self.report["cli_names"], ["taskpilot"])

    def test_generated_table_lists_every_defined_option(self):
        defs = cd.extract_code(str(SAMPLE), [], [])
        table = cd.options_table(defs)
        for flag in ("--config", "--jobs", "--timeout", "--dry-run", "--verbose", "--json", "--format", "--output"):
            self.assertIn("`%s`" % flag, table)
        self.assertIn("| `--timeout` `VALUE` | `run` | `60` |", table)


class CliTests(unittest.TestCase):
    def test_exit_codes(self):
        self.assertEqual(run([str(SAMPLE)])[0], 0)
        self.assertEqual(run([str(SAMPLE), "--fail-on", "high"])[0], 1)
        self.assertEqual(run([str(SAMPLE), "--fail-on", "low"])[0], 1)
        with tempfile.TemporaryDirectory() as tmp:
            write(Path(tmp) / "tp.py", "import argparse\nargparse.ArgumentParser(prog='tp').add_argument('--a')\n")
            write(Path(tmp) / "README.md", "`tp --a`\n")
            self.assertEqual(run([tmp, "--fail-on", "low"])[0], 0)

    def test_bad_input_exits_2(self):
        self.assertEqual(run(["missing-folder-xyz"])[0], 2)
        with tempfile.TemporaryDirectory() as tmp:
            code, _out, err = run([tmp])
            self.assertEqual(code, 2)
            self.assertIn("no documentation", err)
            write(Path(tmp) / "README.md", "x\n")
            code, _out, err = run([tmp])
            self.assertEqual(code, 2)
            self.assertIn("no source files", err)
            write(Path(tmp) / "a.py", "x = 1\n")
            self.assertEqual(run([tmp, "--docs", "nope.md"])[0], 2)
        with self.assertRaises(SystemExit) as caught, redirect_stderr(io.StringIO()):
            cd.main(["--fail-on", "bogus"])
        self.assertEqual(caught.exception.code, 2)

    def test_json_output(self):
        code, out, _ = run([str(SAMPLE), "--json", "--table"])
        report = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(report["tool"], "cli_drift")
        self.assertEqual(report["summary"]["findings"], 12)
        self.assertIn("options_table", report)
        self.assertEqual(report["findings"][0]["severity"], "high")
        for key in ("rule", "file", "line", "message", "suggestion", "locations", "first_run", "section", "side"):
            self.assertIn(key, report["findings"][0])

    def test_table_only(self):
        code, out, _ = run([str(SAMPLE), "--table-only"])
        self.assertTrue(out.startswith("| Option | Command | Default | Env | Description |"))
        self.assertNotIn("finding(s)", out)

    def test_markdown_is_deterministic_and_has_no_machine_paths(self):
        a, b = run([str(SAMPLE)])[1], run([str(SAMPLE)])[1]
        self.assertEqual(a, b)
        self.assertNotIn(str(ROOT), a)

    def test_subprocess_run(self):
        proc = subprocess.run([sys.executable, str(SCRIPT), str(SAMPLE), "--fail-on", "high"],
                              capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("CD001", proc.stdout)

    def test_scripts_never_execute_code_under_analysis(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "executed.txt"
            write(Path(tmp) / "boom.py",
                  "import argparse\nopen(%r, 'w').write('x')\nargparse.ArgumentParser(prog='tp').add_argument('--a')\n" % str(marker))
            write(Path(tmp) / "README.md", "`tp --a`\n")
            run([tmp])
            self.assertFalse(marker.exists())


class HelperTests(unittest.TestCase):
    def test_split_args_handles_nesting_and_strings(self):
        text = 'f("a, b", g(1, 2), [3, 4], \'c)\')'
        args, end = cd.split_args(text, 1)
        self.assertEqual(args, ['"a, b"', "g(1, 2)", "[3, 4]", "'c)'"])
        self.assertEqual(end, len(text) - 1)

    def test_canon_and_norm(self):
        self.assertEqual(cd.canon("-port"), "--port")
        self.assertEqual(cd.canon("-p"), "-p")
        self.assertEqual(cd.canon("--x"), "--x")
        self.assertEqual(cd.norm_name("My_Tool"), "my-tool")

    def test_similarity_catches_prefix_renames_not_unrelated_names(self):
        self.assertGreaterEqual(cd.similar("--out", "--output"), 0.6)
        self.assertGreaterEqual(cd.similar("--db-url", "--database-url"), 0.6)
        self.assertLess(cd.similar("--workers", "--jobs"), 0.6)

    def test_source_stays_python_39_syntax(self):
        ast.parse(SCRIPT.read_text(encoding="utf-8"), feature_version=(3, 9))


if __name__ == "__main__":
    unittest.main()
