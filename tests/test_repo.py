"""Structural checks: frontmatter, manifests, README coverage, safety rules, license, changelog, line endings."""
import ast
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPO_NAME = "docs-truth-skill"
PREFIX = "doctruth-"
SKILLS = ["doctruth-audit", "doctruth-cli", "doctruth-links", "doctruth-snippets", "doctruth-writer"]
AGENTS = ["docs-auditor", "newcomer-simulator"]
SCRIPTS = {"doctruth-links": "link_check.py", "doctruth-cli": "cli_drift.py", "doctruth-snippets": "snippet_check.py"}
TEXT_SUFFIXES = {".md", ".py", ".json", ".yml", ".yaml", ".toml", ".txt", ".example"}
OUTPUT_DIR = ROOT / "examples" / "output"


def frontmatter(path: Path) -> dict:
    """Parse simple YAML frontmatter by hand: `key: value` and folded (>-) blocks."""
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---", 4)
    if end == -1:
        return {}
    data, key = {}, None
    for line in text[4:end].split("\n"):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", line)
        if m and not line.startswith((" ", "\t")):
            key, value = m.group(1), m.group(2).strip()
            data[key] = "" if value in {">-", ">", "|", "|-"} else value.strip('"').strip("'")
        elif key is not None:
            data[key] = (data[key] + " " + line.strip()).strip()
    return data


def skill_dirs():
    return sorted(p for p in (ROOT / "skills").iterdir() if p.is_dir() and not p.name.startswith("_"))


def agent_files():
    return sorted((ROOT / "agents").glob("*.md"))


def plugin_json() -> dict:
    return json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))


def line_count(path: Path) -> int:
    return path.read_text(encoding="utf-8").count("\n") + 1


def repo_files():
    for path in ROOT.rglob("*"):
        if path.is_file() and ".git" not in path.parts and "__pycache__" not in path.parts:
            yield path


def script_paths():
    return sorted(ROOT.glob("skills/*/*.py"))


class SkillTests(unittest.TestCase):
    def test_five_skills(self):
        self.assertEqual([p.name for p in skill_dirs()], SKILLS)

    def test_skill_frontmatter(self):
        for d in skill_dirs():
            with self.subTest(skill=d.name):
                fm = frontmatter(d / "SKILL.md")
                self.assertEqual(fm.get("name"), d.name)
                self.assertTrue(d.name.startswith(PREFIX))
                self.assertRegex(d.name, r"^[a-z0-9]+(-[a-z0-9]+)*$")
                self.assertTrue(fm.get("description"))
                self.assertLess(len(fm["description"]), 1024)
                self.assertIn("Use when", fm["description"])
                self.assertGreaterEqual(fm["description"].count('"'), 10, "5+ quoted trigger phrases")

    def test_skill_body(self):
        for d in skill_dirs():
            with self.subTest(skill=d.name):
                text = (d / "SKILL.md").read_text(encoding="utf-8")
                self.assertTrue(60 <= line_count(d / "SKILL.md") <= 200, f"{d.name}: {line_count(d / 'SKILL.md')} lines")
                for heading in ("## Inputs", "## Outputs", "## Steps", "## Decision rules", "## What NOT to do"):
                    self.assertIn(heading, text)
                self.assertIn("on Windows use `python`", text)
                self.assertRegex(text, r"authoriz|permission", "responsible-use rule")

    def test_referenced_scripts_exist(self):
        for d in skill_dirs():
            text = (d / "SKILL.md").read_text(encoding="utf-8")
            for rel in re.findall(r"\$\{CLAUDE_SKILL_DIR\}/([\w./-]+\.py)", text):
                with self.subTest(skill=d.name, script=rel):
                    self.assertTrue((d / rel).resolve().exists(), rel)

    def test_script_skills_reference_their_script(self):
        for skill, script in SCRIPTS.items():
            with self.subTest(skill=skill):
                text = (ROOT / "skills" / skill / "SKILL.md").read_text(encoding="utf-8")
                self.assertIn("${CLAUDE_SKILL_DIR}/" + script, text)
                self.assertTrue((ROOT / "skills" / skill / script).exists())

    def test_audit_calls_the_sibling_scripts_and_says_so(self):
        text = (ROOT / "skills" / "doctruth-audit" / "SKILL.md").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for skill, script in SCRIPTS.items():
            ref = "${CLAUDE_SKILL_DIR}/../%s/%s" % (skill, script)
            self.assertIn(ref, text)
            self.assertTrue((ROOT / "skills" / "doctruth-audit" / ".." / skill / script).resolve().exists())
        self.assertIn("sibling", text)
        self.assertIn("sibling", readme)
        self.assertEqual(list((ROOT / "skills" / "doctruth-audit").glob("*.py")), [], "the audit skill has no script of its own")

    def test_writer_skill_has_no_script_and_covers_the_procedure(self):
        folder = ROOT / "skills" / "doctruth-writer"
        self.assertEqual(list(folder.glob("*.py")), [])
        text = (folder / "SKILL.md").read_text(encoding="utf-8")
        for needle in ("five steps", "CI", "Verified against", "--table-only"):
            self.assertIn(needle, text)

    def test_audit_ranks_first_run_findings_first(self):
        text = (ROOT / "skills" / "doctruth-audit" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("`first_run` true before false", text)

    def test_skills_never_tell_the_agent_to_run_snippets(self):
        for d in skill_dirs():
            if d.name == "doctruth-links":
                continue            # links are never run; the rule is about code blocks
            text = (d / "SKILL.md").read_text(encoding="utf-8")
            with self.subTest(skill=d.name):
                self.assertRegex(text, r"(?i)(do not|never)[^.\n]*(execute|run)[^.\n]*(snippet|doc|command|cli|program|code)")

    def test_every_script_has_tests(self):
        scripts = script_paths() + [ROOT / "examples" / "regenerate.py"]
        self.assertEqual(len(scripts), 4)
        for script in scripts:
            with self.subTest(script=script.name):
                name = "test_examples.py" if script.name == "regenerate.py" else f"test_{script.stem}.py"
                self.assertTrue((ROOT / "tests" / name).exists(), name)
                text = script.read_text(encoding="utf-8")
                self.assertIn("argparse", text)
                self.assertIn("from __future__ import annotations", text)
                self.assertNotRegex(text, r"(?m)^\s*match .*:\s*$", "no match statements (Python 3.9)")
                self.assertNotIn("newline=", text.replace('newline="\\n"', ""), "write files with LF only")
                imports = set(re.findall(r"^(?:from|import) ([\w.]+)", text, re.M))
                self.assertFalse(imports & {"requests", "yaml", "httpx", "aiohttp", "pytest", "numpy", "toml"}, imports)

    def test_scripts_have_json_and_fail_on(self):
        for script in script_paths():
            text = script.read_text(encoding="utf-8")
            with self.subTest(script=script.name):
                self.assertIn('"--json"', text)
                self.assertIn('"--fail-on"', text)
                self.assertIn("return 2", text, "usage errors exit 2")
                self.assertIn("return 1 if", text, "threshold hits exit 1")

    def test_python_sources_parse_as_python_39(self):
        for path in repo_files():
            if path.suffix == ".py":
                with self.subTest(file=str(path.relative_to(ROOT))):
                    tree = ast.parse(path.read_text(encoding="utf-8"), feature_version=(3, 9))
                    self.assertIsInstance(tree, ast.Module)


class SafetyTests(unittest.TestCase):
    """The scripts read documents. They must never run what they find, and only link_check may use the network."""

    def calls_and_imports(self, path):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        called, imported = set(), set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    called.add(node.func.id)
                elif isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
                    called.add(node.func.value.id + "." + node.func.attr)
            if isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        return called, imported

    def test_no_script_executes_code(self):
        for script in script_paths():
            called, imported = self.calls_and_imports(script)
            with self.subTest(script=script.name):
                self.assertFalse(called & {"exec", "eval", "compile", "__import__", "os.system", "os.popen", "os.exec"}, called)
                self.assertFalse(imported & {"subprocess", "runpy", "code", "pty", "multiprocessing"}, imported)

    def test_importlib_is_only_used_for_tomllib(self):
        for script in script_paths():
            text = script.read_text(encoding="utf-8")
            uses = re.findall(r"import_module\(([^)]*)\)", text)
            with self.subTest(script=script.name):
                self.assertTrue(all(u.strip().strip('"') == "tomllib" for u in uses), uses)
                self.assertNotIn("spec_from_file_location", text)

    def test_only_link_check_may_touch_the_network(self):
        for script in script_paths():
            _called, imported = self.calls_and_imports(script)
            net = imported & {"socket", "urllib.request", "http.client", "ssl", "ftplib", "smtplib", "xmlrpc.client"}
            with self.subTest(script=script.name):
                if script.name == "link_check.py":
                    self.assertTrue(net)
                else:
                    self.assertFalse(net, net)

    def test_link_check_network_code_is_behind_online(self):
        text = (ROOT / "skills" / "doctruth-links" / "link_check.py").read_text(encoding="utf-8")
        self.assertIn('"--online"', text)
        self.assertIn("if args.online:", text)
        self.assertEqual(text.count("Fetcher("), 1, "constructed once, in main, inside the --online branch")
        self.assertIn("class Fetcher", text)
        for needle in ("host_is_public", "GuardedRedirectHandler", "max_requests", "timeout", "HEAD", "GET"):
            self.assertIn(needle, text)

    def test_toml_is_not_imported_at_module_level(self):
        for script in script_paths():
            self.assertNotRegex(script.read_text(encoding="utf-8"), r"(?m)^(?:import|from) tomllib")


class AgentTests(unittest.TestCase):
    def test_two_agents(self):
        self.assertEqual([f.stem for f in agent_files()], AGENTS)

    def test_agent_frontmatter_and_body(self):
        for f in agent_files():
            with self.subTest(agent=f.name):
                fm = frontmatter(f)
                self.assertEqual(fm.get("name"), f.stem)
                self.assertTrue(fm.get("description"))
                tools = {t.strip() for t in fm.get("tools", "").split(",")}
                self.assertEqual(tools, {"Read", "Grep", "Glob", "Bash"}, "agents are read-only")
                self.assertTrue(40 <= line_count(f) <= 120, f"{f.name}: {line_count(f)} lines")
                text = f.read_text(encoding="utf-8")
                for heading in ("## How you work", "## Output format", "## Hard rules"):
                    self.assertIn(heading, text)
                self.assertRegex(text, r"(?i)never (edit|execute)")

    def test_newcomer_simulator_never_runs_commands(self):
        text = (ROOT / "agents" / "newcomer-simulator.md").read_text(encoding="utf-8")
        self.assertIn("never execute a command", text)
        self.assertIn("static", text)


class ManifestTests(unittest.TestCase):
    def test_plugin_json(self):
        data = plugin_json()
        self.assertEqual(data["name"], REPO_NAME)
        self.assertEqual(data["version"], "1.0.0")
        self.assertEqual(data["license"], "MIT")
        self.assertEqual(data["author"]["name"], "Vivek Bhange")
        self.assertEqual(data["homepage"], f"https://github.com/vivekbhange-hub/{REPO_NAME}")
        self.assertEqual(data["repository"], f"https://github.com/vivekbhange-hub/{REPO_NAME}")
        self.assertTrue(6 <= len(data["keywords"]) <= 10)
        self.assertIn("claude-code", data["keywords"])
        self.assertIn("claude-skills", data["keywords"])

    def test_marketplace_json(self):
        data = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
        self.assertEqual(data["name"], REPO_NAME)
        self.assertEqual(data["owner"]["name"], "Vivek Bhange")
        self.assertEqual(len(data["plugins"]), 1)
        plugin = data["plugins"][0]
        self.assertEqual(plugin["name"], REPO_NAME)
        self.assertEqual(plugin["source"], "./")
        self.assertEqual(plugin["category"], "development")
        self.assertTrue(plugin["homepage"].endswith("/" + REPO_NAME))


class DocsTests(unittest.TestCase):
    def setUp(self):
        self.readme = (ROOT / "README.md").read_text(encoding="utf-8")

    def test_readme_mentions_every_skill_and_agent(self):
        for d in skill_dirs():
            self.assertIn(d.name, self.readme)
        for f in agent_files():
            self.assertIn(f.stem, self.readme)
        self.assertIn(f"/plugin marketplace add vivekbhange-hub/{REPO_NAME}", self.readme)
        self.assertIn(f"/plugin install {REPO_NAME}@{REPO_NAME}", self.readme)

    def test_readme_sections(self):
        for needle in ("## Try it in 60 seconds", "## How it works", "## Responsible use", "## Principles and limits",
                       "```mermaid", "examples/sample-project", "Your README is a claim. Check it."):
            self.assertIn(needle, self.readme)

    def test_readme_excerpts_match_committed_outputs(self):
        """Every line inside a ```text block of the README must be a whole line of a committed output."""
        committed = set()
        for out in OUTPUT_DIR.glob("*.md"):
            committed.update(out.read_text(encoding="utf-8").split("\n"))
        blocks = re.findall(r"```text\n(.*?)```", self.readme, re.S)
        self.assertGreaterEqual(len(blocks), 6, "the README should quote real output")
        for block in blocks:
            for line in block.split("\n"):
                if line.strip():
                    with self.subTest(line=line[:70]):
                        self.assertIn(line, committed)

    def test_readme_commands_match_what_regenerate_runs(self):
        regenerate = (ROOT / "examples" / "regenerate.py").read_text(encoding="utf-8")
        commands = re.findall(r"^python3 (skills/[\w./-]+\.py) (\S+)( --table-only)?$", self.readme, re.M)
        self.assertEqual(len(commands), 4)
        for script, target, flag in commands:
            with self.subTest(script=script, flag=flag):
                self.assertTrue((ROOT / script).exists(), script)
                self.assertTrue((ROOT / target).exists(), target)
                self.assertIn(Path(script).stem, regenerate)
                self.assertIn("SAMPLE" if target == "examples/sample-project" else target, regenerate)
                if flag:
                    self.assertIn("--table-only", regenerate)

    def test_readme_local_links_exist(self):
        for target in re.findall(r"\]\(([^)#\s]+)\)", self.readme):
            if re.match(r"https?:", target):
                continue
            with self.subTest(target=target):
                self.assertTrue((ROOT / target).exists(), target)

    def test_the_packs_own_docs_pass_its_own_link_check(self):
        from _load import load
        lc = load("doctruth-links", "link_check")
        project = lc.Project(str(ROOT), ["examples/sample-project/*"])
        files = [f for f in project.doc_files() if not f.startswith("examples/sample-project/")]
        findings, _external, _total = lc.check_documents(project, files, check_orphans=False)
        broken = [f for f in findings if f["rule"] in ("LK001", "LK002", "LK003", "LK004")]
        self.assertEqual(broken, [])

    def test_license(self):
        text = (ROOT / "LICENSE").read_text(encoding="utf-8")
        self.assertIn("MIT", text)
        self.assertIn("Copyright (c) 2026 Vivek Bhange", text)

    def test_changelog_matches_version(self):
        version = plugin_json()["version"]
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertIn(f"## [{version}] - ", changelog, "every released version needs a CHANGELOG entry")
        self.assertIn("## [1.0.0] - 2026-11-13", changelog)
        self.assertTrue((ROOT / "CONTRIBUTING.md").exists())

    def test_ci_and_gitattributes(self):
        self.assertIn("* text=auto eol=lf", (ROOT / ".gitattributes").read_text(encoding="utf-8"))
        ci = (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
        for needle in ("ubuntu-latest", "windows-latest", "macos-latest", '"3.9"', '"3.12"', '"3.13"',
                       "actions/checkout@v4", "actions/setup-python@v5", "permissions:", "contents: read",
                       "timeout-minutes: 15", "python -m unittest discover -s tests -v"):
            self.assertIn(needle, ci)
        ignore = (ROOT / ".gitignore").read_text(encoding="utf-8").split()
        for entry in (".DS_Store", "__pycache__/", "*.pyc", ".venv/", "*.tmp"):
            self.assertIn(entry, ignore)

    def test_text_files_use_lf(self):
        for path in repo_files():
            if path.suffix in TEXT_SUFFIXES or path.name in {"LICENSE", ".gitignore", ".gitattributes", ".env.example"}:
                with self.subTest(file=str(path.relative_to(ROOT))):
                    self.assertNotIn(b"\r\n", path.read_bytes())

    def test_no_ai_attribution_or_em_dashes(self):
        forbidden = ["Co-Authored" + "-By", "Generated" + " with", "—"]
        for path in repo_files():
            if (path.suffix in TEXT_SUFFIXES or path.name == ".env.example") and path.name != "test_repo.py":
                text = path.read_text(encoding="utf-8")
                for bad in forbidden:
                    with self.subTest(file=str(path.relative_to(ROOT)), text=bad):
                        self.assertNotIn(bad, text)

    def test_fixtures_contain_no_real_looking_secrets_or_personal_data(self):
        patterns = [r"AKIA[0-9A-Z]{16}", r"gh[pousr]_[A-Za-z0-9]{30,}", r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
                    r"xox[abprs]-[A-Za-z0-9-]{10,}", r"sk-[A-Za-z0-9]{20,}", r"[\w.+-]+@(?!example\.|noreply)[\w-]+\.(?:com|org|net)\b"]
        for path in repo_files():
            if path.suffix in TEXT_SUFFIXES and path.name != "test_repo.py":
                text = path.read_text(encoding="utf-8")
                for pattern in patterns:
                    with self.subTest(file=str(path.relative_to(ROOT)), pattern=pattern):
                        self.assertIsNone(re.search(pattern, text))


if __name__ == "__main__":
    unittest.main()
