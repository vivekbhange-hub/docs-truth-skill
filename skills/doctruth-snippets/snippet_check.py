#!/usr/bin/env python3
"""snippet_check: statically check the code in documentation against the project it documents.

NOTHING IN A DOCUMENT IS EVER EXECUTED. Snippets are only parsed: `ast.parse` for Python, `json`
for JSON, bracket and quote scanning for JavaScript, TypeScript and shell, a few structural checks
for YAML, `tomllib` for TOML when the interpreter has it (otherwise TOML fences are skipped).

Rules:
  SN001 Python snippet does not parse           SN010 `from pkg import X`: X is not defined in pkg
  SN002 JSON snippet is invalid                 SN011 fence has no language but looks like code
  SN003 JSON only valid with comments/commas    SN012 stale version number in an install command
  SN004 YAML problem (basic checks)             SN013 `name()` in the text no longer exists in the code
  SN005 TOML does not parse                     SN014 command refers to a file, folder or script that is missing
  SN006 JS/TS brackets or strings do not close  SN015 CHANGELOG top version differs from the declared version
  SN007 shell quotes do not close               SN016 fence language does not match its content
  SN008 risky shell advice (curl | sh, sudo rm, chmod 777)
  SN009 `from pkg.module import ...`: module does not exist

Python snippets may use REPL prompts (`>>>`), `...` placeholders and <angle placeholders>. The
function-name check is grep based and reports "possibly stale" only. Standard library only; the
same input gives the same report.
"""
from __future__ import annotations

import argparse
import ast
import bisect
import difflib
import fnmatch
import importlib
import json
import os
import posixpath
import re
import shlex
import sys
import textwrap
import warnings

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".tox", ".mypy_cache", ".pytest_cache",
             "dist", "build", "target", "vendor", ".next", ".idea", ".vscode"}
CODE_SKIP_DIRS = SKIP_DIRS | {"docs", "doc", "examples", "example"}
MD_EXT = {".md", ".markdown"}
CODE_EXT = {".py", ".go", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".rs", ".java", ".kt", ".rb", ".php", ".cs",
            ".c", ".h", ".cpp", ".hpp", ".swift", ".sh", ".lua", ".ex", ".exs", ".scala", ".dart"}
HISTORY_NAMES = ("changelog", "history", "news", "release", "changes")
MAX_FILE_BYTES = 1_000_000
SEVERITIES = ("high", "medium", "low", "info")
SEV_RANK = {name: i for i, name in enumerate(SEVERITIES)}
FIRST_RUN_RE = re.compile(
    r"(?i)\b(install(?:ation|ing)?|getting started|quick ?start|set ?up|usage|first run|try it|"
    r"tutorial|requirements|prerequisites|60 seconds|60-second)\b")

RULES = {
    "SN001": ("high", "Python snippet does not parse"),
    "SN002": ("high", "JSON snippet is invalid"),
    "SN003": ("low", "JSON snippet needs comments or trailing commas to be valid"),
    "SN004": ("medium", "YAML snippet problem"),
    "SN005": ("medium", "TOML snippet does not parse"),
    "SN006": ("medium", "JS/TS brackets or strings do not close"),
    "SN007": ("medium", "Shell quotes do not close"),
    "SN008": ("medium", "Risky shell advice"),
    "SN009": ("high", "Imported module of this project does not exist"),
    "SN010": ("high", "Imported name is not defined in the module"),
    "SN011": ("low", "Fence has no language but looks like code"),
    "SN012": ("medium", "Version number in the docs differs from the declared version"),
    "SN013": ("low", "Function mentioned in the text is not in the code (possibly stale)"),
    "SN014": ("high", "Command refers to something that does not exist"),
    "SN015": ("medium", "CHANGELOG top version differs from the declared version"),
    "SN016": ("low", "Fence language does not match its content"),
    "SN017": ("medium", "JS import from this package: name is not exported"),
}

LANG_ALIASES = {
    "py": "python", "python3": "python", "python": "python", "pycon": "python", "py3": "python",
    "json": "json", "jsonc": "jsonc", "json5": "jsonc",
    "yaml": "yaml", "yml": "yaml",
    "toml": "toml",
    "sh": "shell", "bash": "shell", "shell": "shell", "zsh": "shell", "console": "console", "shell-session": "console",
    "sh-session": "console", "terminal": "console",
    "powershell": "powershell", "ps1": "powershell", "pwsh": "powershell", "cmd": "powershell", "bat": "powershell",
    "js": "js", "javascript": "js", "jsx": "js", "mjs": "js", "node": "js", "ts": "js", "typescript": "js", "tsx": "js",
    "go": "go", "golang": "go",
}
SKIP_LANGS = {"text", "txt", "plain", "plaintext", "output", "log", "diff", "patch", "mermaid", "ini", "csv", "tsv",
              "html", "xml", "css", "sql", "dockerfile", "makefile", "env", "dotenv", "http", "graphql", "markdown", "md"}
SHELL_COMMAND_STARTS = ("pip", "pip3", "npm", "npx", "yarn", "pnpm", "git", "cd", "python", "python3", "curl", "wget",
                        "cargo", "go", "docker", "make", "brew", "apt", "apt-get", "export", "cp", "mkdir", "sudo", "uv")
STOP_PROGRAMS = {"curl", "wget", "tar", "unzip", "django-admin", "npx", "pipx", "brew", "apt", "apt-get", "docker",
                 "conda", "uv", "poetry", "cargo", "npm", "yarn", "pnpm", "go", "make"}
RUNNING_PROGRAMS = {"npm", "pnpm", "yarn"}
JS_GLOBALS = {"require", "fetch", "setTimeout", "setInterval", "parseInt", "parseFloat", "console", "alert", "eval",
              "Promise", "Boolean", "String", "Number", "Array", "Object", "Symbol", "BigInt", "Date", "Error"}
COMMON_NAMES = {"main", "init", "run", "test", "help", "exit", "get", "set", "post", "put", "delete", "list",
                "open", "read", "write", "close", "print", "parse", "start", "stop", "build", "setup", "update", "now",
                "sleep", "random", "time", "date", "today", "uuid", "hash", "copy", "sort", "map", "filter", "reduce",
                "format", "log", "len", "sum", "max", "min"}


# --------------------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------------------

def read_text(path: str) -> str:
    try:
        if os.path.getsize(path) > MAX_FILE_BYTES:
            return ""
        with open(path, "r", encoding="utf-8-sig", errors="replace") as handle:
            return handle.read().replace("\r\n", "\n")
    except OSError:
        return ""


def walk(root: str, skip_dirs, excludes, extensions):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in skip_dirs)
        for name in sorted(filenames):
            if os.path.splitext(name)[1].lower() not in extensions:
                continue
            rel = os.path.relpath(os.path.join(dirpath, name), root).replace(os.sep, "/")
            if any(fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(name, pat) for pat in excludes):
                continue
            yield rel


def norm_pkg(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name.strip().lower())


def parse_ast(code: str):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return ast.parse(code)


def version_tuple(text: str):
    parts = re.findall(r"\d+", text.split("+")[0].split("-")[0])
    return tuple(int(p) for p in parts[:4])


class Fence:
    def __init__(self, raw_lang, lang, start, lines, heading):
        self.raw_lang = raw_lang
        self.lang = lang
        self.start = start        # line number of the first content line
        self.lines = lines
        self.heading = heading


def normalise_lang(info: str):
    info = info.strip()
    info = re.sub(r"^\{\.?", "", info)
    word = re.split(r"[\s{},]", info, maxsplit=1)[0].lstrip(".").lower()
    return word


def extract_fences(text: str):
    """Return (fences, prose_lines) where prose_lines is [(number, line, heading)] outside fences."""
    fences, prose = [], []
    lines = text.split("\n")
    i, heading = 0, ""
    if lines and lines[0].strip() == "---":
        for j in range(1, min(len(lines), 200)):
            if lines[j].strip() in ("---", "..."):
                i = j + 1
                break
    n = len(lines)
    while i < n:
        line = lines[i]
        m = re.match(r"^(\s*)(`{3,}|~{3,})(.*)$", line)
        if m and not (m.group(2)[0] == "`" and "`" in m.group(3)):
            indent, marker, info = len(m.group(1)), m.group(2), m.group(3)
            body, j = [], i + 1
            while j < n:
                close = re.match(r"^\s*(`{3,}|~{3,})\s*$", lines[j])
                if close and close.group(1)[0] == marker[0] and len(close.group(1)) >= len(marker):
                    break
                body.append(lines[j][indent:] if lines[j][:indent].strip() == "" else lines[j].lstrip())
                j += 1
            raw = normalise_lang(info)
            fences.append(Fence(raw, LANG_ALIASES.get(raw, raw), i + 2, body, heading))
            i = j + 1
            continue
        h = re.match(r"^ {0,3}#{1,6}[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*$", line)
        if h:
            heading = re.sub(r"[`*_]", "", h.group(1))
        prose.append((i + 1, line, heading))
        i += 1
    return fences, prose


# --------------------------------------------------------------------------------------
# Per-language checks. Each returns [(line_offset, rule, message, suggestion)] (offset 0-based)
# --------------------------------------------------------------------------------------

def python_code(lines: list):
    """Prepare snippet text: strip REPL prompts and drop REPL output. Returns (code, line_map)."""
    repl = any(re.match(r"^\s*>>>(\s|$)", l) for l in lines)
    kept, mapping = [], []
    for index, line in enumerate(lines):
        if repl:
            m = re.match(r"^\s*(?:>>>|\.\.\.)(?: (.*)|)$", line)
            if not m:
                continue
            kept.append(m.group(1) or "")
        else:
            kept.append(line)
        mapping.append(index)
    code = "\n".join(kept)
    return textwrap.dedent(code), mapping


def looks_like_shell(lines: list) -> bool:
    body = [l.strip() for l in lines if l.strip() and not l.strip().startswith("#")]
    if not body:
        return False
    return all(re.match(r"^\$\s", l) or l.split()[0] in SHELL_COMMAND_STARTS for l in body)


def check_python(lines: list):
    problems = []
    if not any(l.strip() for l in lines):
        return problems, None
    first = next(l for l in lines if l.strip())
    if first.startswith("Traceback (most recent call last)"):
        return problems, None
    if looks_like_shell(lines):
        problems.append((0, "SN016", "the fence is labelled python but its lines are shell commands", "label it `bash`"))
        return problems, None
    code, mapping = python_code(lines)
    if not code.strip():
        return problems, None
    try:
        return problems, parse_ast(code)
    except (SyntaxError, ValueError, RecursionError, MemoryError) as first_error:
        relaxed = re.sub(r"<[A-Za-z_][\w -]*>", "PLACEHOLDER", code).replace("\u2026", "...")
        try:
            return problems, parse_ast(relaxed)
        except (SyntaxError, ValueError, RecursionError, MemoryError):
            pass
        line = getattr(first_error, "lineno", None) or 1
        offset = mapping[min(max(line - 1, 0), len(mapping) - 1)] if mapping else 0
        shown = lines[offset].strip()[:60] if offset < len(lines) else ""
        problems.append((offset, "SN001", "Python syntax error near `%s`" % shown,
                         "fix the snippet so it parses, or label it as pseudo-code (`text`)"))
        return problems, None


def strip_json_noise(text: str) -> str:
    out, i, n = [], 0, len(text)
    while i < n:
        ch = text[i]
        if ch == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            out.append(text[i:j + 1])
            i = j + 1
        elif text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end == -1 else end + 2
        else:
            out.append(ch)
            i += 1
    cleaned = "".join(out)
    cleaned = re.sub(r"(?m)^\s*(?:\.\.\.|\u2026)\s*,?\s*$", "", cleaned)
    cleaned = re.sub(r"(?<=[\[{,:])\s*(?:\.\.\.|\u2026)\s*(?=[\]},])", "null", cleaned)
    cleaned = re.sub(r",(\s*[}\]])", r"\1", cleaned)
    return cleaned


def parse_json_documents(text: str):
    """Parse one or several concatenated JSON documents; raise ValueError on the first problem."""
    decoder = json.JSONDecoder()
    pos, count = 0, 0
    while pos < len(text):
        while pos < len(text) and text[pos] in " \t\r\n":
            pos += 1
        if pos >= len(text):
            break
        _value, pos = decoder.raw_decode(text, pos)
        count += 1
    if count == 0:
        raise ValueError("empty")
    return count


def noise_line(lines: list) -> int:
    """0-based index of the first comment, `...` placeholder or trailing comma (independent of the json module's message)."""
    for index, line in enumerate(lines):
        bare = re.sub(r'"(?:[^"\\]|\\.)*"', '""', line)
        if "//" in bare or "/*" in bare or "..." in bare or "…" in bare:
            return index
        if bare.rstrip().endswith(","):
            following = next((l.strip() for l in lines[index + 1:] if l.strip()), "")
            if following[:1] in ("}", "]"):
                return index
    return 0


def check_json(lines: list, lang: str):
    text = "\n".join(lines)
    if not text.strip():
        return []
    try:
        json.loads(text)
        return []
    except ValueError as error:
        message = getattr(error, "msg", str(error))
        line = max((getattr(error, "lineno", 1) or 1) - 1, 0)
        if message.startswith("Extra data"):
            try:
                parse_json_documents(text)
                return []
            except ValueError:
                pass
        try:
            json.loads(strip_json_noise(text))
            if lang == "jsonc":
                return []
            return [(noise_line(lines), "SN003", "valid only after removing comments, `...` or trailing commas",
                     "label the fence `jsonc`, or make it strict JSON so readers can copy it")]
        except ValueError:
            try:
                parse_json_documents(strip_json_noise(text))
                return []
            except ValueError:
                return [(line, "SN002", "invalid JSON: %s" % message, "fix the snippet; readers will paste it")]


def check_yaml(lines: list):
    problems = []
    stack = []
    block_indent = None
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        if block_indent is not None:
            if indent > block_indent or not line.strip():
                continue
            block_indent = None
        if line.strip() in ("---", "..."):
            stack = []
            continue
        if re.match(r"^\s*\t", line) or re.match(r"^ *\t", line):
            problems.append((index, "SN004", "tab character used for indentation", "YAML only allows spaces"))
            continue
        body = re.sub(r"\s+#.*$", "", line) if "#" in line else line
        stripped = re.sub(r"\"(?:[^\"\\]|\\.)*\"|'(?:[^']|'')*'", "", body)
        if stripped.count('"') % 2 or (stripped.count("'") % 2 and not re.search(r"\w'\w", stripped)):
            problems.append((index, "SN004", "quote is not closed", "close the quote"))
            continue
        depth = sum(stripped.count(c) for c in "[{") - sum(stripped.count(c) for c in "]}")
        if depth and not re.search(r":\s*[|>]", stripped) and re.search(r":\s+[\[{]", stripped) is None and depth > 0 \
                and not stripped.strip().startswith(("-", "[", "{")):
            problems.append((index, "SN004", "bracket is not closed", "close the bracket"))
            continue
        m = re.match(r"^(\s*)(-\s+)?((?:\"[^\"]+\"|'[^']+'|[^\s:#'\"\-][^:#]*?|-[^\s:][^:]*?)):(?:\s|$)", body)
        if re.search(r":\s*[|>][+-]?\d*\s*$", body):
            block_indent = indent
        if not m:
            continue
        key_indent = len(m.group(1)) + (len(m.group(2)) if m.group(2) else 0)
        key = m.group(3).strip()
        if m.group(2):
            while stack and stack[-1][0] >= key_indent:
                stack.pop()
        while stack and stack[-1][0] > key_indent:
            stack.pop()
        if stack and stack[-1][0] == key_indent:
            if key in stack[-1][1]:
                problems.append((index, "SN004", "duplicate key `%s` at the same level" % key, "keep one"))
            stack[-1][1].add(key)
        else:
            stack.append((key_indent, {key}))
    return problems[:3]


def load_tomllib():
    try:
        return importlib.import_module("tomllib")
    except ImportError:
        return None


def check_toml(lines: list, loader=None):
    """Returns (problems, checked). `checked` is False when tomllib is unavailable."""
    module = load_tomllib() if loader is None else loader()
    if module is None:
        return [], False
    text = "\n".join(lines)
    if not text.strip():
        return [], True
    try:
        module.loads(text)
        return [], True
    except Exception as error:  # tomllib.TOMLDecodeError subclasses ValueError; keep static, never execute
        m = re.search(r"line (\d+)", str(error))
        line = int(m.group(1)) - 1 if m else 0
        return [(line, "SN005", "TOML does not parse", "fix the snippet")], True


def js_balance(text: str):
    """Scan JS/TS text for unclosed brackets and strings. Returns (offset_line, message) or None."""
    pairs = {")": "(", "]": "[", "}": "{"}
    stack = []
    i, n, line = 0, len(text), 0
    prev = ""
    while i < n:
        ch = text[i]
        if ch == "\n":
            line += 1
            i += 1
            continue
        if text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
            continue
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            end = n if end == -1 else end + 2
            line += text.count("\n", i, end)
            i = end
            continue
        if ch in "\"'`":
            start_line, j = line, i + 1
            while j < n and text[j] != ch:
                if text[j] == "\\":
                    j += 1
                elif text[j] == "\n":
                    if ch != "`":
                        return start_line, "string is not closed"
                    line += 1
                j += 1
            if j >= n:
                return start_line, "string is not closed"
            i = j + 1
            prev = ch
            continue
        if ch == "/" and (prev == "" or prev in "(,=:[!&|?{};"):
            j = i + 1
            in_class = False
            while j < n and text[j] != "\n":
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == "[":
                    in_class = True
                elif text[j] == "]":
                    in_class = False
                elif text[j] == "/" and not in_class:
                    break
                j += 1
            if j < n and text[j] == "/":
                i = j + 1
                prev = "/"
                continue
        if ch in "([{":
            stack.append((ch, line))
        elif ch in ")]}":
            if not stack or stack[-1][0] != pairs[ch]:
                return line, "unexpected `%s`" % ch
            stack.pop()
        if not ch.isspace():
            prev = ch
        i += 1
    if stack:
        return stack[-1][1], "`%s` is never closed" % stack[-1][0]
    return None


def check_js(lines: list):
    result = js_balance("\n".join(lines))
    if result:
        return [(result[0], "SN006", result[1], "close it, or mark the snippet as an excerpt with a comment")]
    return []


def shell_commands(lines: list, lang: str):
    """Return [(first_line_offset, logical_command_text)] for the command lines of a shell fence."""
    prompted = [bool(re.match(r"^\s*(?:\$|%|PS [^>]*>)\s", l) or re.match(r"^\s*>\s", l) and lang == "console") for l in lines]
    only_prompted = lang == "console" or (any(prompted) and not all(prompted or [True]))
    commands, current, start, heredoc = [], "", 0, None
    for index, line in enumerate(lines):
        if heredoc is not None:
            if line.strip() == heredoc:
                heredoc = None
            continue
        if only_prompted and not prompted[index] and not current:
            continue
        text = re.sub(r"^\s*(?:\$|%|PS [^>]*>|>)\s", "", line) if prompted[index] else line
        if not text.strip() and not current:
            continue
        if not current:
            start = index
        if text.rstrip().endswith("\\") and not text.rstrip().endswith("\\\\"):
            current += text.rstrip()[:-1] + " "
            continue
        current += text
        h = re.search(r"<<-?\s*['\"]?(\w+)['\"]?", current)
        if h:
            heredoc = h.group(1)
        commands.append((start, current))
        current = ""
    if current:
        commands.append((start, current))
    return commands


def quote_problem(commands: list):
    """First unclosed quote across the commands (quotes may span lines). Returns (offset, text) or None."""
    quote, opened = None, 0
    for offset, command in commands:
        i, n = 0, len(command)
        while i < n:
            ch = command[i]
            if quote is None:
                if ch == "\\":
                    i += 2
                    continue
                if ch == "#" and (i == 0 or command[i - 1] in " \t"):
                    break
                if ch in "\"'":
                    quote, opened = ch, offset
            elif quote == '"':
                if ch == "\\":
                    i += 2
                    continue
                if ch == '"':
                    quote = None
            elif ch == "'":
                quote = None
            i += 1
    if quote is not None:
        return opened, "a %s quote is never closed" % ("double" if quote == '"' else "single")
    return None


DANGEROUS = (
    (re.compile(r"(?i)\b(?:curl|wget|iwr|irm|Invoke-WebRequest|Invoke-RestMethod)\b[^|\n]*\|\s*(?:sudo\s+)?(?:ba|z|da|k)?sh\b|"
                r"\b(?:curl|wget)\b[^|\n]*\|\s*(?:sudo\s+)?(?:python3?|perl|ruby|node)\b|"
                r"(?i:\|\s*iex\b)|(?:ba)?sh\s+<\(\s*curl|(?:ba)?sh\s+-c\s+\"?\$\(\s*curl"),
     "SN008", "pipes a download straight into an interpreter", "download, read, then run it; or give a package-manager install", "medium"),
    (re.compile(r"\bsudo\s+rm\s+-[a-zA-Z]*[rRf]"), "SN008", "`sudo rm -r/-f` in documentation",
     "name the exact path and drop sudo, or explain why it is needed", "medium"),
    (re.compile(r"\brm\s+-[a-zA-Z]*[rR][a-zA-Z]*\s+(?:/|~|\$HOME|\*)(?:\s|$)"), "SN008", "`rm -r` on a root, home or wildcard path",
     "use a specific path", "medium"),
    (re.compile(r"\bchmod\s+(?:-R\s+)?0?777\b"), "SN008", "`chmod 777` makes files world-writable",
     "use the narrowest mode that works (755 or 644)", "medium"),
    (re.compile(r"\bcurl\b[^\n]*\s(?:-k|--insecure)\b|--no-check-certificate|NODE_TLS_REJECT_UNAUTHORIZED=0|--trusted-host"),
     "SN008", "turns off TLS verification", "fix the certificate instead of disabling the check", "low"),
    (re.compile(r"\bsudo\s+pip3?\s+install\b"), "SN008", "`sudo pip install` changes the system Python",
     "use a virtual environment or `pip install --user`", "low"),
)


def check_shell(lines: list, lang: str):
    problems = []
    commands = shell_commands(lines, lang)
    if lang in ("shell", "console"):
        issue = quote_problem(commands)
        if issue:
            problems.append((issue[0], "SN007", issue[1], "close the quote"))
        if lines and lines[-1].rstrip().endswith("\\") and not lines[-1].rstrip().endswith("\\\\"):
            problems.append((len(lines) - 1, "SN007", "the last line ends with a backslash continuation", "remove the backslash"))
    for offset, command in commands:
        body = re.sub(r"\s#.*$", "", command)
        for regex, rule, message, hint, severity in DANGEROUS:
            if regex.search(body):
                problems.append((offset, rule, message, hint + "|" + severity))
    return problems, commands


def guess_language(lines: list):
    body = [l for l in lines if l.strip()]
    if len(body) < 2 and not (body and body[0].lstrip().startswith("$ ")):
        return None
    text = "\n".join(lines).strip()
    if text[:1] in "{[":
        try:
            json.loads(text)
            return "json"
        except ValueError:
            pass
    if all(re.match(r"^\$\s", l.strip()) or l.strip().split()[0] in SHELL_COMMAND_STARTS for l in body):
        return "bash"
    if re.search(r"^(?:import |from \S+ import |def |class )", text, re.M):
        try:
            parse_ast(textwrap.dedent(text))
            return "python"
        except (SyntaxError, ValueError, RecursionError, MemoryError):
            return None
    return None


# --------------------------------------------------------------------------------------
# The project the docs describe
# --------------------------------------------------------------------------------------

class Project:
    def __init__(self, root: str):
        self.root = root
        self._versions = None
        self._packages = None
        self._code_words = None
        self._defined = None
        self._listing = {}
        self._module_cache = {}
        self._js_exports = None
        self._index = None

    # ---- declared names and versions
    def declared(self) -> list:
        """[(name, version or None, file)] from the manifests found in the root."""
        if self._versions is not None:
            return self._versions
        found = []
        text = read_text(os.path.join(self.root, "pyproject.toml"))
        if text:
            section, name, version = "", None, None
            for raw in text.split("\n"):
                line = raw.strip()
                head = re.match(r"^\[([^\]]+)\]$", line)
                if head:
                    section = head.group(1)
                    continue
                if section in ("project", "tool.poetry"):
                    m = re.match(r"^(name|version)\s*=\s*[\"']([^\"']+)[\"']", line)
                    if m and m.group(1) == "name":
                        name = m.group(2)
                    elif m:
                        version = m.group(2)
            if name:
                found.append((name, version, "pyproject.toml"))
        text = read_text(os.path.join(self.root, "setup.py"))
        if text:
            n = re.search(r"\bname\s*=\s*[\"']([^\"']+)[\"']", text)
            v = re.search(r"\bversion\s*=\s*[\"']([^\"']+)[\"']", text)
            if n:
                found.append((n.group(1), v.group(1) if v else None, "setup.py"))
        text = read_text(os.path.join(self.root, "setup.cfg"))
        if text and "[metadata]" in text:
            n = re.search(r"^name\s*=\s*(\S+)", text, re.M)
            v = re.search(r"^version\s*=\s*(\d[^\s]*)", text, re.M)
            if n:
                found.append((n.group(1), v.group(1) if v else None, "setup.cfg"))
        for rel in ("package.json", ".claude-plugin/plugin.json"):
            text = read_text(os.path.join(self.root, rel))
            if text:
                try:
                    data = json.loads(text)
                except ValueError:
                    data = None
                if isinstance(data, dict) and isinstance(data.get("name"), str):
                    version = data.get("version") if isinstance(data.get("version"), str) else None
                    found.append((data["name"], version, rel))
        text = read_text(os.path.join(self.root, "Cargo.toml"))
        if text:
            m = re.search(r"\[package\](.*?)(?:\n\[|\Z)", text, re.S)
            if m:
                n = re.search(r"^name\s*=\s*\"([^\"]+)\"", m.group(1), re.M)
                v = re.search(r"^version\s*=\s*\"([^\"]+)\"", m.group(1), re.M)
                if n:
                    found.append((n.group(1), v.group(1) if v else None, "Cargo.toml"))
        text = read_text(os.path.join(self.root, "go.mod"))
        m = re.search(r"^module\s+(\S+)", text, re.M) if text else None
        if m:
            found.append((m.group(1), None, "go.mod"))
        self._versions = found
        return found

    def name_variants(self) -> dict:
        """normalised name -> [(version, file)] for every declared package name, including short forms."""
        variants = {}
        for name, version, file in self.declared():
            keys = {norm_pkg(name), norm_pkg(name.split("/")[-1])}
            for key in keys:
                variants.setdefault(key, []).append((version, file))
        return variants

    def primary_version(self):
        for _name, version, file in self.declared():
            if version:
                return version, file
        return None, None

    # ---- python packages
    def python_packages(self) -> dict:
        """top-level importable name -> base directory (root or src)."""
        if self._packages is not None:
            return self._packages
        found = {}
        skip = {"tests", "test", "docs", "doc", "examples", "example", "scripts", "benchmarks", "build", "dist", "tools"}
        for base in (self.root, os.path.join(self.root, "src")):
            if not os.path.isdir(base):
                continue
            for name in sorted(os.listdir(base)):
                full = os.path.join(base, name)
                if name.startswith(".") or name in skip or name in SKIP_DIRS:
                    continue
                if os.path.isdir(full) and os.path.isfile(os.path.join(full, "__init__.py")):
                    found.setdefault(name, base)
                elif os.path.isfile(full) and name.endswith(".py") and name not in ("setup.py", "conftest.py"):
                    found.setdefault(name[:-3], base)
        self._packages = found
        return found

    def resolve_module(self, parts: list):
        """Return the file that defines dotted module `parts`, '' for a namespace folder, None when missing."""
        packages = self.python_packages()
        base = packages.get(parts[0])
        if base is None:
            return None
        path = os.path.join(base, *parts)
        if os.path.isfile(path + ".py"):
            return path + ".py"
        if os.path.isfile(os.path.join(path, "__init__.py")):
            return os.path.join(path, "__init__.py")
        if os.path.isdir(path):
            return ""
        return None

    def module_names(self, file: str, parts: list):
        """Names a module defines, or None when that cannot be known statically."""
        if file in self._module_cache:
            return self._module_cache[file]
        names = None
        if file:
            try:
                tree = parse_ast(read_text(file))
            except (SyntaxError, ValueError, RecursionError, MemoryError):
                tree = None
            if tree is not None:
                names, unknown = set(), False
                for node in tree.body:
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        names.add(node.name)
                        if node.name == "__getattr__":
                            unknown = True
                    elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                        for target in targets:
                            for sub in ast.walk(target):
                                if isinstance(sub, ast.Name):
                                    names.add(sub.id)
                    elif isinstance(node, (ast.Import, ast.ImportFrom)):
                        for alias in node.names:
                            if alias.name == "*":
                                unknown = True
                            else:
                                names.add(alias.asname or alias.name.split(".")[0])
                    elif isinstance(node, (ast.If, ast.Try)):
                        for sub in ast.walk(node):
                            if isinstance(sub, (ast.FunctionDef, ast.ClassDef)):
                                names.add(sub.name)
                            elif isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Store):
                                names.add(sub.id)
                            elif isinstance(sub, (ast.Import, ast.ImportFrom)):
                                for alias in sub.names:
                                    names.add(alias.asname or alias.name.split(".")[0])
                if unknown:
                    names = None
                elif os.path.basename(file) == "__init__.py":
                    folder = os.path.dirname(file)
                    for entry in os.listdir(folder):
                        names.add(entry[:-3] if entry.endswith(".py") else entry)
        self._module_cache[file] = names
        return names

    def all_modules(self) -> list:
        mods = []
        for top, base in sorted(self.python_packages().items()):
            mods.append(top)
            folder = os.path.join(base, top)
            if os.path.isdir(folder):
                for dirpath, dirnames, filenames in os.walk(folder):
                    dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
                    rel = os.path.relpath(dirpath, base).replace(os.sep, "/").split("/")
                    for name in sorted(filenames):
                        if name.endswith(".py") and name != "__init__.py":
                            mods.append(".".join(rel + [name[:-3]]))
        return mods

    # ---- code corpus
    def file_index(self) -> list:
        if self._index is None:
            self._index = list(self._walk_all())
        return self._index

    def _walk_all(self):
        count = 0
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
            for name in sorted(filenames):
                yield os.path.relpath(os.path.join(dirpath, name), self.root).replace(os.sep, "/")
                count += 1
                if count > 20000:
                    return

    def code_words(self):
        if self._code_words is None:
            words, defined = set(), set()
            for rel in walk(self.root, CODE_SKIP_DIRS, [], CODE_EXT):
                text = read_text(os.path.join(self.root, rel))
                words.update(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", text))
                defined.update(re.findall(
                    r"\b(?:def|class|function|fn|const|let|var|type|interface|struct|trait|func)\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)", text))
            self._code_words, self._defined = words, defined
        return self._code_words, self._defined

    def js_exports(self):
        """(names, star) exported by the JS/TS sources; star means `export *` makes the list incomplete."""
        if self._js_exports is None:
            names, star = set(), False
            for rel in walk(self.root, CODE_SKIP_DIRS, [], {".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx"}):
                text = read_text(os.path.join(self.root, rel))
                names.update(re.findall(
                    r"\bexport\s+(?:default\s+)?(?:async\s+)?(?:function\*?|class|const|let|var|type|interface|enum)\s+([A-Za-z_$][\w$]*)", text))
                for block in re.findall(r"\bexport\s*\{([^}]*)\}", text):
                    for part in block.split(","):
                        piece = part.strip().split(" as ")[-1].strip()
                        if piece:
                            names.add(piece)
                names.update(re.findall(r"\bexports\.([A-Za-z_$][\w$]*)\s*=", text))
                for block in re.findall(r"module\.exports\s*=\s*\{([^}]*)\}", text):
                    for part in block.split(","):
                        piece = part.split(":")[0].strip()
                        if piece:
                            names.add(piece)
                if re.search(r"\bexport\s+\*", text) or re.search(r"module\.exports\s*=\s*(?!\{)", text):
                    star = True
            self._js_exports = (names, star)
        return self._js_exports

    def make_targets(self):
        text = read_text(os.path.join(self.root, "Makefile")) or read_text(os.path.join(self.root, "makefile"))
        if not text:
            return None
        return set(re.findall(r"^([A-Za-z0-9_.-]+)\s*:(?!=)", text, re.M))

    def npm_scripts(self):
        text = read_text(os.path.join(self.root, "package.json"))
        if not text:
            return None
        try:
            data = json.loads(text)
        except ValueError:
            return None
        scripts = data.get("scripts") if isinstance(data, dict) else None
        return set(scripts) if isinstance(scripts, dict) else set()


# --------------------------------------------------------------------------------------
# Cross-checks against the project
# --------------------------------------------------------------------------------------

def check_python_imports(tree, project: Project, fence_lines: list):
    """SN009/SN010 for imports of this project's own modules. Offsets are 0-based fence lines."""
    problems = []
    packages = project.python_packages()
    modules = None
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module and node.module.split(".")[0] in packages:
            parts = node.module.split(".")
            file = project.resolve_module(parts)
            offset = find_line(fence_lines, node.module)
            if file is None:
                if modules is None:
                    modules = project.all_modules()
                near = difflib.get_close_matches(node.module, modules, n=1, cutoff=0.6)
                problems.append((offset, "SN009", "module `%s` does not exist" % node.module,
                                 "did you mean `%s`?" % near[0] if near else "the module was renamed or removed"))
                continue
            known = project.module_names(file, parts) if file else None
            if known is None:
                continue
            for alias in node.names:
                if alias.name != "*" and alias.name not in known:
                    near = difflib.get_close_matches(alias.name, sorted(known), n=1, cutoff=0.6)
                    problems.append((find_line(fence_lines, alias.name, offset), "SN010",
                                     "`%s` is not defined in `%s`" % (alias.name, node.module),
                                     "did you mean `%s`?" % near[0] if near else "check the module's current names"))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                if parts[0] in packages and project.resolve_module(parts) is None:
                    if modules is None:
                        modules = project.all_modules()
                    near = difflib.get_close_matches(alias.name, modules, n=1, cutoff=0.6)
                    problems.append((find_line(fence_lines, alias.name), "SN009", "module `%s` does not exist" % alias.name,
                                     "did you mean `%s`?" % near[0] if near else "the module was renamed or removed"))
    return problems


def find_line(lines: list, needle: str, start: int = 0) -> int:
    for index in range(start, len(lines)):
        if needle in lines[index]:
            return index
    for index in range(len(lines)):
        if needle in lines[index]:
            return index
    return 0


def check_js_imports(lines: list, project: Project):
    problems = []
    package = None
    for name, _version, file in project.declared():
        if file == "package.json":
            package = name
    if not package:
        return problems
    names, star = project.js_exports()
    if star or not names:
        return problems
    text = "\n".join(lines)
    quoted = re.escape(package)
    patterns = (r"import\s*\{([^}]*)\}\s*from\s*['\"]" + quoted + r"['\"]",
                r"(?:const|let|var)\s*\{([^}]*)\}\s*=\s*require\(\s*['\"]" + quoted + r"['\"]\s*\)")
    for pattern in patterns:
        for m in re.finditer(pattern, text):
            for part in m.group(1).split(","):
                imported = part.strip().split(" as ")[0].split(":")[0].strip()
                if imported and imported not in names:
                    near = difflib.get_close_matches(imported, sorted(names), n=1, cutoff=0.6)
                    problems.append((find_line(lines, imported), "SN017",
                                     "`%s` is not exported by `%s` (grep of export statements)" % (imported, package),
                                     "did you mean `%s`?" % near[0] if near else "check the package's exports"))
    return problems


def check_go_imports(lines: list, project: Project):
    problems = []
    module = None
    for name, _v, file in project.declared():
        if file == "go.mod":
            module = name
    if not module:
        return problems
    for index, line in enumerate(lines):
        m = re.search(r"\"(" + re.escape(module) + r"(?:/[\w./-]+)?)\"", line)
        if m:
            sub = m.group(1)[len(module):].strip("/")
            if sub and not os.path.isdir(os.path.join(project.root, sub)):
                problems.append((index, "SN009", "package `%s` does not exist in this module" % m.group(1),
                                 "the folder `%s` is missing" % sub))
    return problems


def tokenize(command: str) -> list:
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        return command.split()


def path_like(token: str) -> bool:
    return bool(token) and not any(c in token for c in "$*~<>`{}=|") and not token.startswith(("-", "http:", "https:"))


def check_commands(commands: list, project: Project, project_names: set):
    """SN014 for commands that name a file, script or folder that does not exist (repo root assumed)."""
    problems = []
    cwd, stop, created = "", False, set()
    clone_dir = None
    npm = project.npm_scripts()
    make = project.make_targets()

    def exists(rel: str) -> bool:
        full = os.path.normpath(os.path.join(project.root, cwd, rel))
        return os.path.exists(full) or rel in created

    def missing(offset: int, kind: str, rel: str):
        near = None
        base = posixpath.basename(rel.rstrip("/"))
        pool = {posixpath.basename(p): p for p in project.file_index()}
        match = difflib.get_close_matches(base, sorted(pool), n=1, cutoff=0.6)
        if match:
            near = pool[match[0]]
        problems.append((offset, "SN014", "%s `%s` does not exist in the project" % (kind, rel),
                         "did you mean `%s`?" % near if near else "update the command or add the file"))

    for offset, command in commands:
        for part in re.split(r"\s*(?:&&|;)\s*", re.sub(r"\s#.*$", "", command)):
            tokens = tokenize(part)
            while tokens and tokens[0] in ("sudo", "time", "env"):
                tokens = tokens[1:]
            if not tokens:
                continue
            if ">" in tokens or ">>" in tokens:
                idx = max(i for i, t in enumerate(tokens) if t in (">", ">>"))
                if idx + 1 < len(tokens):
                    created.add(tokens[idx + 1])
            prog = posixpath.basename(tokens[0])
            args = tokens[1:]
            if prog == "git" and args[:1] == ["clone"]:
                target = [a for a in args[1:] if not a.startswith("-")]
                if target:
                    name = posixpath.splitext(posixpath.basename(target[0].rstrip("/")))[0]
                    if len(target) > 1:
                        name = target[1]
                    if norm_pkg(name) in project_names:
                        clone_dir = name
                    else:
                        stop = True
                continue
            if prog == "cd":
                if not args or not path_like(args[0]) or args[0].startswith("/"):
                    stop = True
                    continue
                if clone_dir and args[0].rstrip("/") == clone_dir:
                    cwd, clone_dir = "", None
                    continue
                if exists(args[0]):
                    cwd = posixpath.normpath(posixpath.join(cwd, args[0]))
                else:
                    if not stop:
                        missing(offset, "folder", args[0])
                    stop = True
                continue
            if prog in ("mkdir", "touch", "tee"):
                created.update(a for a in args if not a.startswith("-"))
                continue
            if prog in STOP_PROGRAMS and prog not in RUNNING_PROGRAMS and not (prog == "make" and make is not None):
                stop = True
                continue
            if stop:
                continue
            if prog in ("cp", "mv", "copy") and len(args) >= 2:
                plain = [a for a in args if not a.startswith("-")]
                if len(plain) >= 2 and path_like(plain[0]) and not exists(plain[0]):
                    missing(offset, "file", plain[0])
                created.add(plain[-1])
            elif prog in ("python", "python3", "py") or re.fullmatch(r"python3\.\d+", prog):
                rest = [a for a in args if not a.startswith("-")] if "-m" not in args else []
                if rest and rest[0].endswith(".py") and path_like(rest[0]) and not exists(rest[0]):
                    missing(offset, "script", rest[0])
                if "-m" in args and "pip" in args and "-r" in args:
                    target = args[args.index("-r") + 1] if args.index("-r") + 1 < len(args) else ""
                    if path_like(target) and not exists(target):
                        missing(offset, "requirements file", target)
            elif prog in ("pip", "pip3") and "-r" in args:
                target = args[args.index("-r") + 1] if args.index("-r") + 1 < len(args) else ""
                if path_like(target) and not exists(target):
                    missing(offset, "requirements file", target)
            elif prog in ("bash", "sh", "zsh", "source", ".") and args and path_like(args[0]) and not args[0].startswith("-") \
                    and (args[0].endswith((".sh", ".bash")) or "/" in args[0]):
                if not exists(args[0]):
                    missing(offset, "script", args[0])
            elif prog == "node" and args and path_like(args[0]) and args[0].endswith((".js", ".mjs", ".cjs", ".ts")):
                if not exists(args[0]):
                    missing(offset, "script", args[0])
            elif tokens[0].startswith("./") and path_like(tokens[0]) and len(tokens[0]) > 2:
                if not exists(tokens[0][2:]):
                    missing(offset, "script", tokens[0])
            elif prog in ("docker-compose", "docker") and "-f" in args:
                target = args[args.index("-f") + 1] if args.index("-f") + 1 < len(args) else ""
                if path_like(target) and not exists(target):
                    missing(offset, "compose file", target)
            elif prog in RUNNING_PROGRAMS and len(args) >= 2 and args[0] == "run" and npm is not None:
                if args[1] not in npm:
                    problems.append((offset, "SN014", "npm script `%s` is not defined in package.json" % args[1],
                                     "defined scripts: %s" % (", ".join(sorted(npm)) or "none")))
            elif prog == "make" and make is not None and args:
                target = [a for a in args if not a.startswith("-") and "=" not in a]
                if target and target[0] not in make:
                    problems.append((offset, "SN014", "make target `%s` is not defined in the Makefile" % target[0],
                                     "defined targets: %s" % (", ".join(sorted(make)[:6]) or "none")))
    return problems


VERSION_PATTERNS = (
    ("pin", r"(?<![\w.-])({name})(?:\[[\w,.-]+\])?\s*(==|===|~=|>=)\s*v?(\d+(?:\.\d+)*[A-Za-z0-9.+-]*)"),
    ("at", r"(?<![\w-])(?:[\w.-]+/)?({name})@v?(\d+(?:\.\d+)*[A-Za-z0-9.+-]*)"),
    ("toml", r"^\s*({name})\s*=\s*\"v?(\d+(?:\.\d+)*[A-Za-z0-9.+-]*)\""),
)
HISTORICAL_RE = re.compile(r"(?i)\b(since|added in|introduced in|removed in|deprecated|requires|minimum|before|prior to|older|"
                           r"upgrad\w+ from|migrat\w+ from|renamed in|changed in)\b")


def version_problems(line: str, variants: dict):
    """Find version pins for this project's own package names in one line of docs."""
    found = []
    if HISTORICAL_RE.search(line):
        return found
    for key, entries in variants.items():
        declared = [v for v, _f in entries if v]
        if not declared:
            continue
        names = set()
        for raw in re.findall(r"[A-Za-z0-9_.-]+", line):
            if norm_pkg(raw) == key:
                names.add(re.escape(raw))
        if not names:
            continue
        alternation = "|".join(sorted(names))
        for kind, template in VERSION_PATTERNS:
            for m in re.finditer(template.replace("{name}", alternation), line, re.M):
                if kind == "pin":
                    op, doc = m.group(2), m.group(3)
                else:
                    op, doc = "==", m.group(2)
                matches = [d for d in declared if d == doc or d.startswith(doc + ".") or d.startswith(doc + "-")]
                if op in ("==", "===") and not matches:
                    found.append((doc, declared[0], op))
                elif op in (">=", "~=") and version_tuple(doc) > version_tuple(declared[0]):
                    found.append((doc, declared[0], op))
    for m in re.finditer(r"(?i)\b(?:latest|current)\s+(?:release|version)\b[^\n\d]{0,25}v?(\d+\.\d+(?:\.\d+)?)", line):
        pool = [v for entries in variants.values() for v, _f in entries if v]
        if pool and not any(d == m.group(1) or d.startswith(m.group(1) + ".") for d in pool):
            found.append((m.group(1), pool[0], "latest"))
    for m in re.finditer(r"badge/version-v?(\d+\.\d+\.\d+)", line):
        pool = [v for entries in variants.values() for v, _f in entries if v]
        if pool and m.group(1) not in pool:
            found.append((m.group(1), pool[0], "badge"))
    return found


def check_function_mentions(prose: list, project: Project, history: bool):
    """SN013: `name()` in text where nothing in the code uses or defines that name."""
    if history:
        return []
    words, defined = project.code_words()
    if not words:
        return []
    import builtins as _builtins
    skip = set(dir(_builtins)) | JS_GLOBALS | COMMON_NAMES
    heads = set(project.python_packages()) | defined
    out = []
    seen = set()
    for number, line, heading in prose:
        for m in re.finditer(r"(?<!`)`([A-Za-z_][\w]*(?:\.[A-Za-z_]\w*)*)\((?:[^()`]*)\)`(?!`)", line):
            dotted_name = m.group(1)
            parts = dotted_name.split(".")
            name = parts[-1]
            if len(name) < 3 or name in skip or name in words:
                continue
            if len(parts) > 1 and parts[-2] not in heads:
                continue
            if (name, number) in seen:
                continue
            seen.add((name, number))
            near = difflib.get_close_matches(name, sorted(defined), n=1, cutoff=0.7)
            out.append((number, heading, name, near[0] if near else None))
    return out


# --------------------------------------------------------------------------------------
# Driving the checks
# --------------------------------------------------------------------------------------

def make_finding(rule, file, line, message, suggestion="", section="", severity="", lang=""):
    return {"rule": rule, "severity": severity or RULES[rule][0], "file": file, "line": line, "message": message,
            "suggestion": suggestion, "section": section, "first_run": bool(FIRST_RUN_RE.search(section or "")),
            "lang": lang}


def analyse_file(rel: str, text: str, project: Project, stats: dict, toml_loader=None):
    findings = []
    fences, prose = extract_fences(text)
    history = posixpath.basename(rel).lower().startswith(HISTORY_NAMES)
    variants = project.name_variants()
    project_names = set(variants)
    for fence in fences:
        stats["fences"] += 1
        lang = fence.lang
        section = fence.heading
        found = []
        if lang == "":
            stats["unlabeled"] += 1
            guess = guess_language(fence.lines)
            if guess:
                found.append((0, "SN011", "the fence has no language but looks like %s" % guess,
                              "label it `%s` so it highlights and can be checked" % guess))
                lang_for_checks = {"bash": "shell", "python": "python", "json": "json"}[guess]
            else:
                lang_for_checks = ""
        else:
            lang_for_checks = lang
        tree = None
        commands = []
        if lang_for_checks == "python":
            probs, tree = check_python(fence.lines)
            found += probs
            if tree is not None:
                found += check_python_imports(tree, project, fence.lines)
        elif lang_for_checks in ("json", "jsonc"):
            found += check_json(fence.lines, lang_for_checks)
        elif lang_for_checks == "yaml":
            found += check_yaml(fence.lines)
        elif lang_for_checks == "toml":
            probs, checked = check_toml(fence.lines, toml_loader)
            if not checked:
                stats["toml_skipped"] += 1
            found += probs
        elif lang_for_checks == "js":
            found += check_js(fence.lines)
            found += check_js_imports(fence.lines, project)
        elif lang_for_checks == "go":
            found += check_go_imports(fence.lines, project)
        elif lang_for_checks in ("shell", "console", "powershell"):
            probs, commands = check_shell(fence.lines, lang_for_checks)
            found += probs
            has_prompts = any(re.match(r"^\s*\$\s", l) for l in fence.lines)
            if lang_for_checks in ("shell", "console") or has_prompts:
                found += check_commands(commands, project, project_names)
        elif lang in SKIP_LANGS or lang == "":
            pass
        else:
            stats["unchecked"][lang] = stats["unchecked"].get(lang, 0) + 1
        if lang_for_checks in ("python", "json", "jsonc", "yaml", "toml", "js", "go", "shell", "console", "powershell"):
            key = "shell" if lang_for_checks in ("console", "powershell") else ("json" if lang_for_checks == "jsonc" else lang_for_checks)
            stats["checked"][key] = stats["checked"].get(key, 0) + 1
        if not history:
            for number, line in enumerate(fence.lines):
                for doc, declared, op in version_problems(line, variants):
                    found.append((number, "SN012", "docs say %s%s, the declared version is %s" % (
                        ("`%s` " % op) if op not in ("==", "latest", "badge") else "", doc, declared),
                        "use `%s`, or stop pinning a version in the docs" % declared))
        for offset, rule, message, hint in found:
            severity = ""
            if rule == "SN008" and "|" in hint:
                hint, severity = hint.rsplit("|", 1)
            findings.append(make_finding(rule, rel, fence.start + offset, message, hint, section, severity, lang))
    if not history:
        for number, line, heading in prose:
            for doc, declared, op in version_problems(line, variants):
                findings.append(make_finding("SN012", rel, number, "docs say %s%s, the declared version is %s" % (
                    ("`%s` " % op) if op not in ("==", "latest", "badge") else "", doc, declared),
                    "use `%s`, or stop pinning a version in the docs" % declared, heading))
        for number, heading, name, near in check_function_mentions(prose, project, history):
            findings.append(make_finding(
                "SN013", rel, number, "`%s()` is mentioned but nothing in the code defines or uses `%s` (possibly stale: grep based)" % (name, name),
                "did you mean `%s`?" % near if near else "check the current name, or remove the mention", heading))
    return findings


def changelog_findings(project: Project, files: list) -> list:
    version, source = project.primary_version()
    if not version:
        return []
    out = []
    for rel in files:
        if not posixpath.basename(rel).lower().startswith(("changelog", "changes", "history")):
            continue
        text = read_text(os.path.join(project.root, rel))
        for number, line in enumerate(text.split("\n"), 1):
            m = re.match(r"^#{1,3}\s*\[?v?(\d+\.\d+\.\d+[\w.+-]*)\]?", line)
            if m and "unreleased" not in line.lower():
                top = m.group(1)
                if top != version:
                    relation = "behind" if version_tuple(top) < version_tuple(version) else "ahead of"
                    out.append(make_finding("SN015", rel, number,
                                            "the top entry is %s, %s the declared version %s (%s)" % (top, relation, version, source),
                                            "add a `%s` entry or fix the declared version" % version))
                break
    return out


def sort_findings(findings: list) -> list:
    return sorted(findings, key=lambda f: (SEV_RANK[f["severity"]], f["file"], f["line"], f["rule"]))


def counts(findings: list) -> dict:
    return {sev: sum(1 for f in findings if f["severity"] == sev) for sev in SEVERITIES}


def should_fail(findings: list, fail_on: str) -> bool:
    if fail_on == "never":
        return False
    return any(SEV_RANK[f["severity"]] <= SEV_RANK[fail_on] for f in findings)


def cell(text) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def analyse(root: str, files: list, toml_loader=None) -> dict:
    project = Project(root)
    stats = {"fences": 0, "unlabeled": 0, "checked": {}, "unchecked": {}, "toml_skipped": 0}
    findings = []
    for rel in files:
        findings += analyse_file(rel, read_text(os.path.join(root, rel)), project, stats, toml_loader)
    findings += changelog_findings(project, files)
    ordered = sort_findings(findings)
    version, source = project.primary_version()
    return {
        "tool": "snippet_check",
        "summary": {"files": len(files), "fences": stats["fences"], "findings": len(ordered), "by_severity": counts(ordered),
                    "declared_version": version, "declared_in": source},
        "stats": {"checked": stats["checked"], "unchecked": stats["unchecked"], "unlabeled": stats["unlabeled"],
                  "toml_skipped": stats["toml_skipped"]},
        "findings": ordered,
    }


def render_markdown(report: dict) -> str:
    s, c, st = report["summary"], report["summary"]["by_severity"], report["stats"]
    checked = ", ".join("%s %d" % (k, v) for k, v in sorted(st["checked"].items())) or "none"
    lines = ["Scanned %d doc file(s) and %d code fence(s). Checked: %s. %d finding(s): %d high, %d medium, %d low, %d info." % (
        s["files"], s["fences"], checked, s["findings"], c["high"], c["medium"], c["low"], c["info"])]
    lines.append("Declared version: %s." % ("%s (%s)" % (s["declared_version"], s["declared_in"]) if s["declared_version"] else "none found"))
    if st["unchecked"]:
        lines.append("Fences in languages with no checker: %s." % ", ".join("%s %d" % (k, v) for k, v in sorted(st["unchecked"].items())))
    if st["toml_skipped"]:
        lines.append("TOML fences skipped because this Python has no tomllib: %d." % st["toml_skipped"])
    lines.append("No snippet was executed; every check is static.")
    lines.append("")
    if not report["findings"]:
        lines.append("No findings.")
        return "\n".join(lines) + "\n"
    by_rule = {}
    for f in report["findings"]:
        by_rule[f["rule"]] = by_rule.get(f["rule"], 0) + 1
    lines += ["| Rule | Severity | Count | Meaning |", "|---|---|---:|---|"]
    for rule in sorted(by_rule):
        lines.append("| %s | %s | %d | %s |" % (rule, RULES[rule][0], by_rule[rule], RULES[rule][1]))
    lines += ["", "| Location | Rule | Severity | Section | Problem | Suggestion |", "|---|---|---|---|---|---|"]
    for f in report["findings"]:
        section = f["section"] + (" (first run)" if f["first_run"] else "") if f["section"] else ""
        lines.append("| `%s:%d` | %s | %s | %s | %s | %s |" % (f["file"], f["line"], f["rule"], f["severity"], cell(section),
                                                              cell(f["message"]), cell(f["suggestion"])))
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="snippet_check.py",
        description="Statically check code fences, install commands, imports and version numbers in Markdown docs. "
                    "Nothing in the docs is executed.")
    parser.add_argument("path", nargs="?", default=".", help="project folder or one Markdown file (default: .)")
    parser.add_argument("--root", help="project root for imports, versions and file checks (default: the folder, or the file's folder)")
    parser.add_argument("--exclude", action="append", default=[], metavar="GLOB", help="skip docs matching GLOB")
    parser.add_argument("--json", action="store_true", help="print JSON instead of Markdown")
    parser.add_argument("--fail-on", default="never", choices=["high", "medium", "low", "never"],
                        help="exit 1 when a finding of this severity or worse exists (default: never)")
    args = parser.parse_args(argv)
    target = os.path.abspath(args.path)
    if not os.path.exists(target):
        print("error: %s does not exist" % args.path, file=sys.stderr)
        return 2
    single = os.path.isfile(target)
    root = os.path.abspath(args.root) if args.root else (os.path.dirname(target) if single else target)
    if not os.path.isdir(root):
        print("error: root %s is not a folder" % root, file=sys.stderr)
        return 2
    if single:
        rel = os.path.relpath(target, root).replace(os.sep, "/")
        if rel.startswith("..") or os.path.splitext(rel)[1].lower() not in MD_EXT:
            print("error: the file must be a Markdown file inside the root", file=sys.stderr)
            return 2
        files = [rel]
    else:
        files = list(walk(root, SKIP_DIRS, args.exclude, MD_EXT))
    if not files:
        print("error: no Markdown files found under %s" % args.path, file=sys.stderr)
        return 2
    report = analyse(root, files)
    if args.json:
        sys.stdout.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
    else:
        sys.stdout.write(render_markdown(report))
    return 1 if should_fail(report["findings"], args.fail_on) else 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    sys.exit(main())
