#!/usr/bin/env python3
"""cli_drift: compare the CLI flags, commands and environment variables your docs mention with
the ones your code defines.

Docs side: flags in code spans, shell fences, option tables and help-output blocks; subcommands in
command lines; environment variables in `export X=`, tables, `.env` blocks and prose that talks
about the environment. Flags are only attributed to your CLI when the command line starts with one
of its names (from pyproject/setup scripts, package.json bin, go.mod, argparse prog=, or --cli)
or when the mention is a bare flag such as `--port` in a code span or table ("loose").

Code side (static, nothing is imported or executed): Python argparse/optparse, click, typer basics
and `"--x" in sys.argv`; Go flag, pflag and cobra; Node commander and `process.argv.includes`;
environment reads from os.environ, os.getenv, process.env, os.Getenv, click envvar and
pydantic-settings fields.

Rules:
  CD001 flag documented but not defined          CD004 flag defined but not documented
  CD002 command documented but not defined       CD005 command defined but not documented
  CD003 env var documented but never read        CD006 env var read but not documented
  CD007 likely rename (similar names)            CD008 documented default differs from the code
  CD009 flag documented under the wrong command  CD010 comparison skipped (nothing recognised)

Flags that the code builds dynamically are counted and listed, never guessed. The code is treated
as the source of truth. Standard library only; the same input gives the same report.
"""
from __future__ import annotations

import argparse
import ast
import difflib
import fnmatch
import json
import os
import posixpath
import re
import shlex
import sys
import warnings

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".tox", ".mypy_cache", ".pytest_cache",
             "dist", "build", "target", "vendor", ".next", ".idea", ".vscode", "tests", "test", "__tests__",
             "spec", "e2e", "examples", "docs", "doc"}
DOC_SKIP_DIRS = SKIP_DIRS - {"docs", "doc", "examples", "tests", "test", "spec"}
DOC_EXT = {".md", ".markdown", ".rst", ".adoc"}
CODE_EXT = {".py", ".go", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx"}
DOC_SKIP_NAMES = ("changelog", "history", "news", "release", "license", "licence", "code_of_conduct", "authors")
MAX_FILE_BYTES = 1_000_000
SEVERITIES = ("high", "medium", "low", "info")
SEV_RANK = {name: i for i, name in enumerate(SEVERITIES)}
FIRST_RUN_RE = re.compile(
    r"(?i)\b(install(?:ation|ing)?|getting started|quick ?start|set ?up|usage|first run|try it|"
    r"tutorial|requirements|prerequisites|60 seconds|60-second)\b")

RULES = {
    "CD001": "Flag is documented but the code does not define it",
    "CD002": "Command is documented but the code does not define it",
    "CD003": "Environment variable is documented but the code never reads it",
    "CD004": "Flag is defined in the code but not documented",
    "CD005": "Command is defined in the code but not documented",
    "CD006": "Environment variable is read by the code but not documented",
    "CD007": "Likely rename: a documented name is close to an undocumented one",
    "CD008": "Documented default differs from the default in the code",
    "CD009": "Flag is documented under a command that does not define it",
    "CD010": "Comparison skipped: no argument parser recognised",
}

SYSTEM_ENV = {"PATH", "HOME", "USER", "USERNAME", "SHELL", "PWD", "OLDPWD", "TERM", "LANG", "LC_ALL", "TMPDIR",
              "TEMP", "TMP", "EDITOR", "VISUAL", "PAGER", "HOSTNAME", "DISPLAY", "LOGNAME", "USERPROFILE",
              "APPDATA", "LOCALAPPDATA", "SYSTEMROOT", "COMSPEC", "PATHEXT", "VIRTUAL_ENV", "CI", "TZ",
              "PYTHONPATH", "PYTHONUNBUFFERED", "PYTHONDONTWRITEBYTECODE", "PYTHONUTF8", "NO_COLOR",
              "FORCE_COLOR", "PROGRAMFILES", "CONDA_PREFIX", "SUDO_USER", "PS1"}
SYSTEM_PREFIXES = ("GITHUB_", "RUNNER_", "XDG_", "LC_", "CI_", "CLAUDE_")
SHELL_LANGS = {"", "bash", "sh", "shell", "console", "zsh", "text", "txt", "powershell", "ps1", "pwsh", "cmd",
               "bat", "batch", "fish", "shell-session", "sh-session", "terminal", "output"}
ENV_FENCE_LANGS = {"env", "dotenv", "ini", "properties"}
PROMPT_ONLY_LANGS = {"console", "shell-session", "sh-session"}
ENV_TOKEN = r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+"
ENV_CONTEXT_RE = re.compile(r"(?i)\b(env|environment|variables?|export|dotenv|\.env)\b|\bset\b")
INLINE_CODE_RE = re.compile(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)")
FLAG_TOKEN_RE = re.compile(r"(?<![\w-])(--?[A-Za-z][\w-]*)")
HELP_LINE_RE = re.compile(r"^\s{1,}((?:-[A-Za-z],?\s+)?--[A-Za-z][\w-]*)(?:[ =][^\s]+)?(?:\s{2,}\S|\s*$)")
RUNNERS = {"sudo", "time", "env", "nohup", "exec", "command", "npx", "pnpx", "bunx", "uvx", "$", ">", "%"}
PYTHONS = {"python", "python3", "py", "python2"}
FOREIGN_TOOL_RE = re.compile(r"\b(?:curl|wget|gh|git|docker|kubectl|npm|npx|pip|pip3|jq|ssh|grep|aws|gcloud|brew|apt|make|tar|ls|cat|sed|awk)\b")
GENERIC_STEMS = {"cli", "main", "app", "index", "__init__", "__main__", "run", "command", "commands"}


# --------------------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------------------

def norm_name(name: str) -> str:
    return name.lower().replace("_", "-")


def canon(flag: str) -> str:
    """Single-dash long flags (Go style, `-port`) are the same flag as `--port`."""
    if flag.startswith("-") and not flag.startswith("--") and len(flag) > 2:
        return "--" + flag[1:]
    return flag


def is_system_env(name: str) -> bool:
    return name in SYSTEM_ENV or name.startswith(SYSTEM_PREFIXES)


def squash(text) -> str:
    return re.sub(r"\s+", " ", str(text)).strip()


def read_text(path: str) -> str:
    try:
        if os.path.getsize(path) > MAX_FILE_BYTES:
            return ""
        with open(path, "r", encoding="utf-8-sig", errors="replace") as handle:
            return handle.read().replace("\r\n", "\n")
    except OSError:
        return ""


def walk_files(root: str, skip_dirs, excludes, extensions):
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root).replace(os.sep, "/")
        dirnames[:] = sorted(d for d in dirnames if d not in skip_dirs)
        for name in sorted(filenames):
            if os.path.splitext(name)[1].lower() not in extensions:
                continue
            rel = os.path.normpath(os.path.join(rel_dir, name)).replace(os.sep, "/")
            if any(fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(name, pat) for pat in excludes):
                continue
            yield rel


def is_test_file(rel: str) -> bool:
    name = posixpath.basename(rel)
    return bool(re.match(r"^(test_.*|.*_test)\.(py|go)$|^.*\.(test|spec)\.(js|mjs|cjs|ts|tsx|jsx)$|^conftest\.py$", name))


# --------------------------------------------------------------------------------------
# Definitions found in the code
# --------------------------------------------------------------------------------------

class Flag:
    """One option defined by the code."""

    def __init__(self, names, command, default, help_text, takes_value, file, line, source, env=None, hidden=False):
        self.names = list(names)
        self.command = command
        self.default = default
        self.help = help_text or ""
        self.takes_value = takes_value
        self.file = file
        self.line = line
        self.source = source
        self.env = env
        self.hidden = hidden

    def to_dict(self) -> dict:
        return {"names": self.names, "command": self.command, "default": self.default, "help": self.help,
                "takes_value": self.takes_value, "file": self.file, "line": self.line, "source": self.source,
                "env": self.env, "hidden": self.hidden}


class Definitions:
    def __init__(self):
        self.flags = []
        self.commands = {}        # name -> (file, line)
        self.aliases = {}         # alias -> command name
        self.envs = {}            # NAME -> [(file, line, via)]
        self.prog_names = set()
        self.dynamic = []         # (file, line, why)
        self.unparsable = []
        self.has_version = False
        self.code_files = 0
        self.frameworks = set()
        self.code_text = ""

    def add_env(self, name: str, file: str, line: int, via: str) -> None:
        self.envs.setdefault(name, []).append((file, line, via))

    def flag_index(self) -> dict:
        index = {}
        for flag in self.flags:
            for name in flag.names:
                index.setdefault(canon(name), []).append(flag)
        return index


def strip_c_comments(text: str) -> str:
    """Remove // and /* */ comments from C-like code, leaving strings and line breaks alone."""
    out = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch in "\"'`":
            j = i + 1
            while j < n and text[j] != ch:
                if text[j] == "\\" and ch != "`":
                    j += 1
                if text[j:j + 1] == "\n" and ch != "`":
                    break
                j += 1
            out.append(text[i:j + 1])
            i = j + 1
        elif text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            end = n if end == -1 else end + 2
            out.append("\n" * text.count("\n", i, end))
            i = end
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def split_args(text: str, open_idx: int):
    """Split the arguments of the call whose `(` is at text[open_idx]. Returns (args, end_index) or None."""
    depth, i, n = 0, open_idx, len(text)
    args, start = [], open_idx + 1
    while i < n:
        ch = text[i]
        if ch in "\"'`":
            j = i + 1
            while j < n and text[j] != ch:
                if text[j] == "\\" and ch != "`":
                    j += 1
                j += 1
            i = j
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth == 0:
                tail = text[start:i].strip()
                if tail or args:
                    args.append(tail)
                return args, i
        elif ch == "," and depth == 1:
            args.append(text[start:i].strip())
            start = i + 1
        i += 1
    return None


def c_string(raw: str):
    """Value of a plain Go/JS string literal, or None."""
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'`":
        body = raw[1:-1]
        return body.replace("\\\"", "\"").replace("\\'", "'").replace("\\\\", "\\")
    return None


def c_literal(raw: str):
    """Normalised text of a simple Go/JS literal (string, number, bool), or None when not simple."""
    raw = raw.strip()
    text = c_string(raw)
    if text is not None:
        return text
    if re.fullmatch(r"-?\d+(?:\.\d+)?", raw):
        return raw
    if raw in ("true", "false"):
        return raw
    return None


def py_literal(node):
    """Normalised text of a simple Python literal node, or None when not a plain literal."""
    try:
        value = ast.literal_eval(node)
    except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
        return None
    if value is None or isinstance(value, (list, tuple, dict, set, bytes)):
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def const_str(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def name_of(node) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def dotted(node) -> str:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def is_environ(node) -> bool:
    return (isinstance(node, ast.Attribute) and node.attr == "environ") or (isinstance(node, ast.Name) and node.id == "environ")


def kw_of(call, name):
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def flag_names_from(strings) -> list:
    """Split option declarations such as `--shout/--no-shout` or `-p` into flag names."""
    names = []
    for text in strings:
        for part in text.split("/"):
            part = part.strip()
            if part.startswith("-") and len(part) > 1 and not part[1:2].isdigit():
                names.append(part)
    return names


def default_from_node(node):
    """(default text, env name) from a default= keyword, understanding os.environ.get("X", fallback)."""
    if node is None:
        return None, None
    if isinstance(node, ast.Call):
        func = node.func
        is_env = (isinstance(func, ast.Attribute) and func.attr in ("get", "getenv") and
                  (is_environ(func.value) or dotted(func.value) == "os")) or (isinstance(func, ast.Name) and func.id == "getenv")
        if is_env and node.args and const_str(node.args[0]):
            fallback = py_literal(node.args[1]) if len(node.args) > 1 else None
            return fallback, const_str(node.args[0])
        return None, None
    return py_literal(node), None


VALUELESS_ACTIONS = {"store_true", "store_false", "count", "version", "help", "store_const", "append_const",
                     "BooleanOptionalAction"}


def extract_python(rel: str, text: str, defs: Definitions) -> None:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        defs.unparsable.append(rel)
        return
    nodes = [n for n in ast.walk(tree) if hasattr(n, "lineno")]
    nodes.sort(key=lambda n: (n.lineno, getattr(n, "col_offset", 0)))
    scope = {}
    for node in nodes:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            call = node.value
            attr = name_of(call.func)
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if attr == "add_parser" and call.args and const_str(call.args[0]):
                register_command(defs, call, rel)
                for target in targets:
                    scope[target] = const_str(call.args[0])
            elif attr in ("add_argument_group", "add_mutually_exclusive_group"):
                receiver = call.func.value if isinstance(call.func, ast.Attribute) else None
                for target in targets:
                    scope[target] = scope.get(receiver.id) if isinstance(receiver, ast.Name) else None
            elif attr in ("ArgumentParser", "OptionParser"):
                prog = const_str(kw_of(call, "prog")) if kw_of(call, "prog") is not None else None
                if prog:
                    defs.prog_names.add(os.path.splitext(prog)[0] if prog.endswith((".py", ".js")) else prog)
                for target in targets:
                    scope[target] = None
    typer_commands = []
    for node in nodes:
        if isinstance(node, ast.Call):
            attr = name_of(node.func)
            if attr == "add_parser" and not isinstance(node, ast.Assign):
                register_command(defs, node, rel)
            if attr in ("add_argument", "add_option") and isinstance(node.func, ast.Attribute):
                read_argparse_call(node, scope, rel, defs)
            elif attr in ("getenv", "get", "setdefault", "pop") and node.args:
                func = node.func
                reads_env = (isinstance(func, ast.Attribute) and is_environ(func.value) and attr != "getenv") or \
                            (attr == "getenv" and (isinstance(func, ast.Name) or dotted(func.value) == "os"))
                if reads_env:
                    key = const_str(node.args[0])
                    if key is not None:
                        defs.add_env(key, rel, node.lineno, "os.environ")
                    else:
                        defs.dynamic.append((rel, node.lineno, "environment variable name is not a literal"))
            elif attr in ("ArgumentParser", "OptionParser"):
                prog = const_str(kw_of(node, "prog")) if kw_of(node, "prog") is not None else None
                if prog:
                    defs.prog_names.add(os.path.splitext(prog)[0] if prog.endswith((".py", ".js")) else prog)
            elif attr == "Typer":
                name = const_str(kw_of(node, "name")) if kw_of(node, "name") is not None else None
                if name:
                    defs.prog_names.add(name)
        elif isinstance(node, ast.Subscript) and is_environ(node.value):
            sl = getattr(node.slice, "value", node.slice) if node.slice.__class__.__name__ == "Index" else node.slice
            key = const_str(sl)
            if key is not None:
                defs.add_env(key, rel, node.lineno, "os.environ")
            else:
                defs.dynamic.append((rel, node.lineno, "environment variable name is not a literal"))
        elif isinstance(node, ast.Compare) and len(node.ops) == 1 and isinstance(node.ops[0], ast.In):
            flag = const_str(node.left)
            if flag and flag.startswith("-") and len(flag) > 1 and "argv" in ast.dump(node.comparators[0]):
                defs.flags.append(Flag([flag], None, None, "", False, rel, node.lineno, "sys.argv"))
                defs.frameworks.add("sys.argv")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if "click" in text:
                read_click_function(node, rel, defs)
            if "typer" in text:
                read_typer_function(node, rel, defs, typer_commands)
        elif isinstance(node, ast.ClassDef):
            read_settings_class(node, rel, defs)
    finish_typer(typer_commands, defs)


def register_command(defs: Definitions, call, rel: str) -> None:
    name = const_str(call.args[0]) if call.args else None
    if not name:
        return
    defs.commands.setdefault(name, (rel, call.lineno))
    aliases = kw_of(call, "aliases")
    if isinstance(aliases, (ast.List, ast.Tuple)):
        for item in aliases.elts:
            if const_str(item):
                defs.aliases[const_str(item)] = name


def read_argparse_call(call, scope: dict, rel: str, defs: Definitions) -> None:
    receiver = call.func.value
    command = None
    if isinstance(receiver, ast.Name):
        command = scope.get(receiver.id)
    elif isinstance(receiver, ast.Call) and name_of(receiver.func) == "add_parser" and receiver.args:
        command = const_str(receiver.args[0])
        register_command(defs, receiver, rel)
    strings, dynamic = [], False
    for arg in call.args:
        text = const_str(arg)
        if text is None:
            dynamic = True
        else:
            strings.append(text)
    if dynamic:
        defs.dynamic.append((rel, call.lineno, "add_argument with a name that is not a string literal"))
    names = [s for s in strings if s.startswith("-")]
    if not names:
        return
    defs.frameworks.add("argparse")
    action_node = kw_of(call, "action")
    action = ""
    if action_node is not None:
        action = const_str(action_node) or name_of(action_node)
    default, env = default_from_node(kw_of(call, "default"))
    help_node = kw_of(call, "help")
    hidden = name_of(help_node) == "SUPPRESS" if help_node is not None else False
    help_text = squash((const_str(help_node) or "").replace("%%", "%")) if help_node is not None else ""
    if action == "version":
        defs.has_version = True
    if action == "BooleanOptionalAction":
        names = names + ["--no-" + n[2:] for n in names if n.startswith("--")]
    defs.flags.append(Flag(names, command, default, help_text, action not in VALUELESS_ACTIONS, rel, call.lineno,
                           "argparse", env, hidden))


def decorator_call(dec):
    return dec if isinstance(dec, ast.Call) else None


def read_click_function(func, rel: str, defs: Definitions) -> None:
    options, command_name = [], None
    for dec in func.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        attr = name_of(target)
        if attr == "option" and isinstance(dec, ast.Call):
            options.append(dec)
        elif attr == "version_option":
            defs.has_version = True
        elif attr in ("command", "group"):
            root_style = isinstance(target, ast.Name) or (isinstance(target, ast.Attribute) and name_of(target.value) == "click")
            call = decorator_call(dec)
            explicit = None
            if call is not None:
                explicit = const_str(call.args[0]) if call.args and const_str(call.args[0]) else None
                if explicit is None and kw_of(call, "name") is not None:
                    explicit = const_str(kw_of(call, "name"))
            if not root_style:
                command_name = explicit or func.name.replace("_", "-")
                defs.commands.setdefault(command_name, (rel, func.lineno))
    for opt in options:
        names = flag_names_from([const_str(a) for a in opt.args if const_str(a)])
        if not names:
            continue
        defs.frameworks.add("click")
        default, env_default = default_from_node(kw_of(opt, "default"))
        envvar = kw_of(opt, "envvar")
        env = const_str(envvar) if envvar is not None else env_default
        if env:
            defs.add_env(env, rel, opt.lineno, "click envvar")
        flagish = any(kw_of(opt, k) is not None and py_literal(kw_of(opt, k)) == "true" for k in ("is_flag", "count"))
        flagish = flagish or kw_of(opt, "flag_value") is not None or any("/" in const_str(a) for a in opt.args if const_str(a))
        help_node = kw_of(opt, "help")
        defs.flags.append(Flag(names, command_name, default, squash(const_str(help_node) or "") if help_node is not None else "",
                               not flagish, rel, opt.lineno, "click", env))


def read_typer_function(func, rel: str, defs: Definitions, commands: list) -> None:
    cmd_dec = None
    for dec in func.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        if name_of(target) in ("command", "callback") and not (isinstance(target, ast.Attribute) and name_of(target.value) == "click"):
            cmd_dec = dec
    if cmd_dec is None:
        return
    target = cmd_dec.func if isinstance(cmd_dec, ast.Call) else cmd_dec
    is_callback = name_of(target) == "callback"
    name = None
    if isinstance(cmd_dec, ast.Call):
        name = const_str(cmd_dec.args[0]) if cmd_dec.args and const_str(cmd_dec.args[0]) else None
        if name is None and kw_of(cmd_dec, "name") is not None:
            name = const_str(kw_of(cmd_dec, "name"))
    name = None if is_callback else (name or func.name.replace("_", "-"))
    params = list(func.args.args) + list(func.args.kwonlyargs)
    defaults = [None] * (len(func.args.args) - len(func.args.defaults)) + list(func.args.defaults) + list(func.args.kw_defaults)
    flags = []
    for param, default in zip(params, defaults):
        if param.arg in ("self", "ctx", "context"):
            continue
        opt = None
        if isinstance(default, ast.Call) and name_of(default.func) in ("Option", "Argument"):
            opt = default
        elif param.annotation is not None:
            for sub in ast.walk(param.annotation):
                if isinstance(sub, ast.Call) and name_of(sub.func) in ("Option", "Argument"):
                    opt = sub
        if opt is not None and name_of(opt.func) == "Argument":
            continue
        if opt is None and default is None:
            continue
        explicit = flag_names_from([const_str(a) for a in (opt.args if opt is not None else []) if const_str(a)])
        is_bool = (isinstance(default, ast.Constant) and isinstance(default.value, bool)) or \
            (param.annotation is not None and "bool" in ast.dump(param.annotation))
        names = explicit or ["--" + param.arg.replace("_", "-")]
        if is_bool and not explicit:
            names.append("--no-" + param.arg.replace("_", "-"))
        shown_default = None
        if opt is not None and kw_of(opt, "default") is not None:
            shown_default = py_literal(kw_of(opt, "default"))
        elif opt is not None and opt is default and opt.args:
            first = opt.args[0]
            first_text = const_str(first)
            if first_text is None:
                shown_default = py_literal(first)
            elif not first_text.startswith("-"):
                shown_default = first_text
        elif default is not None and opt is not default:
            shown_default = py_literal(default)
        if shown_default in ("Ellipsis", None):
            shown_default = None
        help_node = kw_of(opt, "help") if opt is not None else None
        env = const_str(kw_of(opt, "envvar")) if opt is not None and kw_of(opt, "envvar") is not None else None
        if env:
            defs.add_env(env, rel, func.lineno, "typer envvar")
        flags.append(Flag(names, name, shown_default, squash(const_str(help_node) or "") if help_node is not None else "",
                          not is_bool, rel, func.lineno, "typer", env))
    commands.append((name, rel, func.lineno, flags, is_callback))


def finish_typer(commands: list, defs: Definitions) -> None:
    if not commands:
        return
    defs.frameworks.add("typer")
    named = [c for c in commands if c[0]]
    multi = len(named) > 1 or any(c[4] for c in commands)
    for name, rel, line, flags, _cb in commands:
        for flag in flags:
            flag.command = name if (multi and name) else None
            defs.flags.append(flag)
        if multi and name:
            defs.commands.setdefault(name, (rel, line))


def read_settings_class(cls, rel: str, defs: Definitions) -> None:
    if not any(name_of(b) == "BaseSettings" for b in cls.bases):
        return
    prefix = ""
    for stmt in cls.body:
        if isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Call):
            node = kw_of(stmt.value, "env_prefix")
            if node is not None and const_str(node):
                prefix = const_str(node)
        elif isinstance(stmt, ast.ClassDef) and stmt.name == "Config":
            for inner in stmt.body:
                if isinstance(inner, ast.Assign) and const_str(inner.value) and \
                        any(isinstance(t, ast.Name) and t.id == "env_prefix" for t in inner.targets):
                    prefix = const_str(inner.value)
    for stmt in cls.body:
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and not stmt.target.id.startswith("_") \
                and stmt.target.id != "model_config":
            defs.add_env((prefix + stmt.target.id).upper(), rel, stmt.lineno, "pydantic-settings")


GO_KINDS = ("String", "Int", "Int8", "Int16", "Int32", "Int64", "Uint", "Uint8", "Uint16", "Uint32", "Uint64", "Bool",
            "Float32", "Float64", "Duration", "StringSlice", "StringArray", "IntSlice", "StringToString", "Count",
            "IP", "Func", "BoolFunc", "Var", "TextVar")
GO_FUNCS = {}
for _kind in GO_KINDS:
    for _var in ("", "Var"):
        for _p in ("", "P"):
            GO_FUNCS[_kind + _var + _p] = (_kind, _var == "Var" or _kind in ("Var", "TextVar"), _p == "P")
GO_FLAG_CALL_RE = re.compile(
    r"(?:\b(?:flag|pflag)|\b\w*[Ff]lag[Ss]et|\bfs\b|\bflags\b|\b(?:\w+\.)?(?:Persistent)?Flags\(\))\s*\.\s*([A-Z][A-Za-z0-9]*)\(")
COBRA_RE = re.compile(r"(?:(\w+)\s*(?::=|=)\s*)?&cobra\.Command\s*\{")
GO_ENV_RE = re.compile(r"\bos\.(?:Getenv|LookupEnv)\(\s*(\"[^\"\n]*\"|[^)\n]*)\)")


def extract_go(rel: str, raw: str, defs: Definitions) -> None:
    text = strip_c_comments(raw)
    line_of = line_finder(text)
    for m in GO_FLAG_CALL_RE.finditer(text):
        spec = GO_FUNCS.get(m.group(1))
        if spec is None:
            continue
        parsed = split_args(text, m.end() - 1)
        if parsed is None:
            continue
        args = parsed[0]
        kind, is_var, has_short = spec
        name_idx = 1 if is_var else 0
        if len(args) <= name_idx:
            continue
        name = c_string(args[name_idx])
        line = line_of(m.start())
        if name is None:
            defs.dynamic.append((rel, line, "flag name is not a string literal"))
            continue
        names = ["--" + name if len(name) > 1 else "-" + name]
        if has_short and len(args) > name_idx + 1 and c_string(args[name_idx + 1]):
            names.append("-" + c_string(args[name_idx + 1]))
        no_default = kind in ("Var", "TextVar", "Func", "BoolFunc", "Count")
        default_idx = name_idx + (2 if has_short else 1)
        default = None
        if not no_default and len(args) > default_idx:
            default = c_literal(args[default_idx])
        usage_idx = default_idx + (0 if no_default else 1)
        usage = c_string(args[usage_idx]) if len(args) > usage_idx and c_string(args[usage_idx]) else ""
        defs.frameworks.add("go-flag")
        defs.flags.append(Flag(names, None, default, squash(usage), kind not in ("Bool", "BoolFunc", "Count"), rel, line, "go-flag"))
    cobras = []
    for m in COBRA_RE.finditer(text):
        end = text.find("\n}", m.end())
        body = text[m.end(): end if end != -1 else m.end() + 600]
        use = re.search(r"\bUse:\s*\"([^\"]+)\"", body)
        if use:
            cobras.append((m.group(1) or "", use.group(1).split()[0], line_of(m.start())))
    if cobras:
        defs.frameworks.add("cobra")
    roots = [c for c in cobras if "root" in c[0].lower()]
    if not roots and len(cobras) == 1:
        roots = cobras
    for var, use, line in cobras:
        if (var, use, line) in roots:
            defs.prog_names.add(use)
        elif use not in ("help", "completion"):
            defs.commands.setdefault(use, (rel, line))
    for m in GO_ENV_RE.finditer(text):
        name = c_string(m.group(1))
        if name is not None:
            defs.add_env(name, rel, line_of(m.start()), "os.Getenv")
        else:
            defs.dynamic.append((rel, line_of(m.start()), "environment variable name is not a literal"))
    if "AutomaticEnv" in text:
        defs.dynamic.append((rel, line_of(text.index("AutomaticEnv")), "viper AutomaticEnv derives env names from keys"))


def line_finder(text: str):
    starts = [0]
    for m in re.finditer("\n", text):
        starts.append(m.end())

    def find(index: int) -> int:
        lo, hi = 0, len(starts)
        while lo < hi - 1:
            mid = (lo + hi) // 2
            if starts[mid] <= index:
                lo = mid
            else:
                hi = mid
        return lo + 1
    return find


JS_OPTION_RE = re.compile(r"\.\s*(option|requiredOption)\s*\(")
JS_COMMAND_RE = re.compile(r"\.\s*command\s*\(")
JS_NAME_RE = re.compile(r"\.\s*name\s*\(\s*(['\"`])([^'\"`]+)\1\s*\)")
JS_ENV_RES = (re.compile(r"\bprocess\.env\.([A-Za-z_][A-Za-z0-9_]*)"),
              re.compile(r"\bprocess\.env\[\s*['\"`]([^'\"`]+)['\"`]\s*\]"),
              re.compile(r"\bimport\.meta\.env\.([A-Za-z_][A-Za-z0-9_]*)"))
JS_ARGV_RE = re.compile(r"\bargv\.includes\(\s*['\"`](--?[A-Za-z][\w-]*)['\"`]\s*\)")


def extract_js(rel: str, raw: str, defs: Definitions) -> None:
    text = strip_c_comments(raw)
    line_of = line_finder(text)
    events = []
    for m in JS_COMMAND_RE.finditer(text):
        events.append((m.start(), "command", m))
    for m in JS_OPTION_RE.finditer(text):
        events.append((m.start(), "option", m))
    events.sort(key=lambda e: e[0])
    current, current_end = None, 0
    for pos, kind, m in events:
        parsed = split_args(text, m.end() - 1)
        if parsed is None:
            continue
        args = parsed[0]
        if kind == "command":
            word = c_string(args[0]).split()[0] if args and c_string(args[0]) and c_string(args[0]).split() else None
            if word and word != "*":
                defs.commands.setdefault(word, (rel, line_of(pos)))
                current, current_end = word, parsed[1]
                defs.frameworks.add("commander")
            continue
        if ";" in text[current_end:pos]:
            current = None
        spec = c_string(args[0]) if args else None
        if spec is None:
            if args:
                defs.dynamic.append((rel, line_of(pos), "option spec is not a string literal"))
            continue
        names = [t for t in re.split(r"[,|\s]+", spec) if t.startswith("-") and len(t) > 1]
        if not names:
            continue
        takes_value = "<" in spec or "[" in spec
        for n in list(names):
            if n.startswith("--no-"):
                names.append("--" + n[5:])
        default = None
        if len(args) > 2:
            third = args[2]
            if re.match(r"^(?:\(|function\b|[A-Za-z_$][\w$.]*\s*$)", third) and not c_literal(third) and len(args) > 3:
                default = c_literal(args[3])
            else:
                default = c_literal(third)
        desc = c_string(args[1]) if len(args) > 1 and c_string(args[1]) else ""
        defs.frameworks.add("commander")
        defs.flags.append(Flag(names, current, default, squash(desc), takes_value, rel, line_of(pos), "commander"))
    for m in JS_NAME_RE.finditer(text):
        defs.prog_names.add(m.group(2))
    if re.search(r"\.\s*version\s*\(", text):
        defs.has_version = True
    for m in JS_ARGV_RE.finditer(text):
        defs.flags.append(Flag([m.group(1)], None, None, "", False, rel, line_of(m.start()), "process.argv"))
        defs.frameworks.add("process.argv")
    for regex in JS_ENV_RES:
        for m in regex.finditer(text):
            defs.add_env(m.group(1), rel, line_of(m.start()), "process.env")
    for m in re.finditer(r"\bprocess\.env\[\s*(?!['\"`])", text):
        defs.dynamic.append((rel, line_of(m.start()), "environment variable name is not a literal"))


def program_names_from_manifests(root: str, defs: Definitions) -> None:
    text = read_text(os.path.join(root, "pyproject.toml"))
    section = ""
    for raw in text.split("\n"):
        line = raw.strip()
        header = re.match(r"^\[([^\]]+)\]$", line)
        if header:
            section = header.group(1)
            continue
        m = re.match(r"^([A-Za-z0-9_.-]+)\s*=\s*[\"']", line)
        if m and section in ("project.scripts", "tool.poetry.scripts"):
            defs.prog_names.add(m.group(1))
    cfg = read_text(os.path.join(root, "setup.cfg")) + "\n" + read_text(os.path.join(root, "setup.py"))
    for m in re.finditer(r"^\s*[\"']?([A-Za-z0-9_.-]+)\s*=\s*[\w.]+:[\w.]+[\"']?\s*,?\s*$", cfg, re.M):
        defs.prog_names.add(m.group(1))
    package = read_text(os.path.join(root, "package.json"))
    if package:
        try:
            data = json.loads(package)
        except ValueError:
            data = {}
        if isinstance(data, dict):
            bin_field = data.get("bin")
            if isinstance(bin_field, str) and isinstance(data.get("name"), str):
                defs.prog_names.add(data["name"].split("/")[-1])
            elif isinstance(bin_field, dict):
                defs.prog_names.update(str(k) for k in bin_field)
    gomod = read_text(os.path.join(root, "go.mod"))
    m = re.search(r"^module\s+(\S+)", gomod, re.M)
    if m:
        parts = [p for p in m.group(1).split("/") if not re.fullmatch(r"v\d+", p)]
        if parts:
            defs.prog_names.add(parts[-1])
    cmd_dir = os.path.join(root, "cmd")
    if os.path.isdir(cmd_dir):
        defs.prog_names.update(d for d in sorted(os.listdir(cmd_dir)) if os.path.isdir(os.path.join(cmd_dir, d)))
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        if "__main__.py" in filenames and os.path.exists(os.path.join(dirpath, "__init__.py")):
            defs.prog_names.add(os.path.basename(dirpath))


def extract_code(root: str, code_paths, excludes) -> Definitions:
    defs = Definitions()
    seen = set()
    targets = code_paths or ["."]
    for target in targets:
        base = os.path.normpath(os.path.join(root, target))
        if os.path.isfile(base):
            files = [(os.path.relpath(base, root).replace(os.sep, "/"))]
        else:
            files = [os.path.normpath(os.path.join(os.path.relpath(base, root), rel)).replace(os.sep, "/")
                     for rel in walk_files(base, SKIP_DIRS, excludes, CODE_EXT)]
        for rel in files:
            if rel in seen or is_test_file(rel):
                continue
            seen.add(rel)
            text = read_text(os.path.join(root, rel))
            if not text:
                continue
            defs.code_files += 1
            ext = os.path.splitext(rel)[1].lower()
            before = len(defs.flags)
            if ext == ".py":
                extract_python(rel, text, defs)
            elif ext == ".go":
                extract_go(rel, text, defs)
            else:
                extract_js(rel, text, defs)
            if len(defs.flags) > before:
                stem = posixpath.splitext(posixpath.basename(rel))[0]
                if stem not in GENERIC_STEMS:
                    defs.prog_names.add(stem)
    program_names_from_manifests(root, defs)
    defs.code_text = "\n".join(read_text(os.path.join(root, rel)) for rel in sorted(seen))
    return defs


# --------------------------------------------------------------------------------------
# Mentions found in the docs
# --------------------------------------------------------------------------------------

class Mention:
    def __init__(self, kind, name, file, line, section, first_run, context, command=None, default=None):
        self.kind = kind          # flag, command, env
        self.name = name
        self.file = file
        self.line = line
        self.section = section
        self.first_run = first_run
        self.context = context    # command (inside a command line of this CLI) or loose
        self.command = command
        self.default = default


def doc_files_default(root: str, excludes) -> list:
    found = []
    for name in sorted(os.listdir(root)):
        full = os.path.join(root, name)
        low = name.lower()
        if os.path.isfile(full) and os.path.splitext(low)[1] in DOC_EXT and not low.startswith(DOC_SKIP_NAMES):
            found.append(name)
        elif os.path.isdir(full) and low in ("docs", "doc", "documentation", "skills", "agents", "commands"):
            for rel in walk_files(full, DOC_SKIP_DIRS, excludes, DOC_EXT):
                found.append(os.path.normpath(os.path.join(name, rel)).replace(os.sep, "/"))
    return [f for f in found if not any(fnmatch.fnmatch(f, pat) for pat in excludes)
            and not posixpath.basename(f).lower().startswith(DOC_SKIP_NAMES)]


def collect_doc_files(root: str, docs_paths, excludes) -> list:
    if not docs_paths:
        return doc_files_default(root, excludes)
    found = []
    for target in docs_paths:
        base = os.path.normpath(os.path.join(root, target))
        if os.path.isfile(base):
            found.append(os.path.relpath(base, root).replace(os.sep, "/"))
        else:
            found.extend(os.path.normpath(os.path.join(os.path.relpath(base, root), rel)).replace(os.sep, "/")
                         for rel in walk_files(base, DOC_SKIP_DIRS, excludes, DOC_EXT))
    return sorted(set(found))


def iter_doc_lines(text: str, ext: str):
    """Yield (line_number, line, fence_language or None, heading). Fence markers are not yielded."""
    lines = text.split("\n")
    fence = None            # (char, length, lang)
    rst_block = None        # (indent, lang)
    heading = ""
    for number, line in enumerate(lines, 1):
        if ext in (".md", ".markdown"):
            m = re.match(r"^(\s*)(`{3,}|~{3,})\s*([\w+#.-]*)", line)
            if fence is None:
                if m:
                    fence = (m.group(2)[0], len(m.group(2)), m.group(3).lower())
                    continue
            else:
                if m and m.group(2)[0] == fence[0] and len(m.group(2)) >= fence[1] and not line.strip().strip("`~"):
                    fence = None
                    continue
                yield number, line, fence[2], heading
                continue
            h = re.match(r"^ {0,3}#{1,6}[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*$", line)
            if h:
                heading = h.group(1)
        elif ext == ".rst":
            indent = len(line) - len(line.lstrip())
            if rst_block is not None:
                if line.strip() and indent <= rst_block[0]:
                    rst_block = None
                else:
                    yield number, line, rst_block[1], heading
                    continue
            m = re.match(r"^(\s*)\.\.\s+(?:code-block|sourcecode|code)::\s*(\w*)", line)
            if m:
                rst_block = (len(m.group(1)), m.group(2).lower())
                continue
            if line.rstrip().endswith("::") and line.strip() != "::":
                rst_block = (indent, "")
        else:
            if line.strip() in ("----", "...."):
                fence = None if fence else ("-", 4, "")
                continue
            if fence:
                yield number, line, fence[2], heading
                continue
            h = re.match(r"^=+\s+(.*)$", line)
            if h:
                heading = h.group(1)
        yield number, line, None, heading


def tokenize(line: str) -> list:
    try:
        return shlex.split(line, posix=True)
    except ValueError:
        return line.split()


def parse_command_segments(line: str, prog_names: set, root_names: set):
    """Return [(program_candidates, args)] for each shell segment of a command line."""
    tokens = tokenize(line)
    segments, current = [], []
    for token in tokens:
        if token in ("|", "||", "&&", ";", "&"):
            segments.append(current)
            current = []
        else:
            current.append(token)
    segments.append(current)
    out = []
    for seg in segments:
        i = 0
        while i < len(seg) and (seg[i] in RUNNERS or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", seg[i])):
            i += 1
        if i < len(seg) and seg[i] in ("pipx", "uv", "poetry", "pipenv", "pdm", "hatch") and i + 1 < len(seg) and seg[i + 1] == "run":
            i += 2
        if i >= len(seg):
            continue
        prog = seg[i]
        rest = seg[i + 1:]
        candidates = []
        base = posixpath.basename(prog.replace("\\", "/"))
        stem = posixpath.splitext(base)[0]
        if base in PYTHONS or stem in PYTHONS:
            j = 0
            while j < len(rest) and rest[j].startswith("-") and rest[j] != "-m":
                j += 1
            if j < len(rest) and rest[j] == "-m" and j + 1 < len(rest):
                module = rest[j + 1]
                candidates = [module, module.split(".")[0], module.split(".")[-1]]
                rest = rest[j + 2:]
            elif j < len(rest):
                candidates = [posixpath.splitext(posixpath.basename(rest[j].replace("\\", "/")))[0]]
                rest = rest[j + 1:]
        elif stem == "node" and rest:
            candidates = [posixpath.splitext(posixpath.basename(rest[0].replace("\\", "/")))[0]]
            rest = rest[1:]
        elif stem == "go" and rest[:1] == ["run"] and len(rest) > 1:
            target = rest[1].rstrip("/.") or "."
            candidates = [posixpath.basename(target)] if target not in (".", "") else list(root_names)
            candidates += list(root_names) if target in (".", "./...") else []
            rest = rest[2:]
        else:
            candidates = [stem if stem else base, base]
        if any(norm_name(c) in prog_names for c in candidates):
            out.append((candidates, rest))
    return out


class DocScanner:
    def __init__(self, prog_names: set, defs: Definitions, include_system: bool):
        self.prog_names = {norm_name(n) for n in prog_names}
        self.defs = defs
        self.include_system = include_system
        self.mentions = []
        self.ignored_command_lines = 0
        self.assigned = set()
        self.ignored_foreign_mentions = 0
        self.flag_index = defs.flag_index()

    def env_ok(self, name: str) -> bool:
        return self.include_system or not is_system_env(name)

    def add(self, kind, name, file, line, section, context, command=None, default=None):
        if kind == "flag" and context == "loose" and re.fullmatch(r"-[A-Za-z][\w-]+", name) and "go-flag" not in self.defs.frameworks:
            return          # `-or-later`, `-only`: not a flag outside Go-style CLIs
        self.mentions.append(Mention(kind, name, file, line, section, bool(FIRST_RUN_RE.search(section or "")),
                                     context, command, default))

    def scan(self, rel: str, text: str) -> None:
        ext = os.path.splitext(rel)[1].lower()
        self.assigned = set(re.findall(r"(?:^|[\s;])(?:export\s+)?([A-Z][A-Z0-9_]*)=", text, re.M))
        table_header = ""
        default_col = None
        in_table = False
        pending = None
        for number, line, lang, heading in iter_doc_lines(text, ext):
            if lang is not None:
                if pending is not None:
                    number, line = pending[0], pending[1] + " " + line.strip()
                    pending = None
                if line.rstrip().endswith("\\") and not line.rstrip().endswith("\\\\"):
                    pending = (number, line.rstrip()[:-1].rstrip(), lang, heading)
                    continue
                self.scan_fence(rel, number, line, lang, heading)
                continue
            if pending is not None:
                self.scan_fence(rel, pending[0], pending[1], pending[2], pending[3])
                pending = None
            if line.lstrip().startswith("|"):
                cells = [c.strip() for c in line.strip().strip("|").split("|")]
                if not in_table:
                    in_table, table_header = True, " ".join(cells)
                    default_col = next((i for i, c in enumerate(cells) if re.search(r"(?i)\bdefault", c)), None)
                    continue
                if all(re.fullmatch(r":?-{2,}:?", c) or not c for c in cells):
                    continue
                self.scan_table_row(rel, number, cells, heading, table_header, default_col)
                continue
            in_table, table_header, default_col = False, "", None
            self.scan_prose(rel, number, line, heading)
        if pending is not None:
            self.scan_fence(rel, pending[0], pending[1], pending[2], pending[3])

    def scan_table_row(self, rel, number, cells, heading, header, default_col):
        default_text = None
        if default_col is not None and default_col < len(cells):
            default_text = clean_default(cells[default_col])
        for index, cell in enumerate(cells):
            if index == default_col:
                continue
            for span in code_spans(cell):
                self.scan_span(rel, number, span, heading, env_hint=bool(ENV_CONTEXT_RE.search(header + " " + heading)),
                               default=default_text)

    def scan_prose(self, rel, number, line, heading):
        spans = code_spans(line)
        env_hint = bool(ENV_CONTEXT_RE.search(line)) or bool(re.search(r"(?i)\b(environment|env)\b", heading))
        flag_spans = [s for s in spans if s.startswith("-")]
        default = prose_default(line) if len(flag_spans) == 1 else None
        near_foreign = set()
        for m in INLINE_CODE_RE.finditer(line):
            if m.group(2).strip().startswith("-") and FOREIGN_TOOL_RE.search(line[max(0, m.start() - 14):m.start()]):
                near_foreign.add(m.group(2).strip())
        for span in spans:
            if span in near_foreign:
                self.ignored_foreign_mentions += 1
                continue
            self.scan_span(rel, number, span, heading, env_hint, default if span.startswith("-") else None)
        for m in re.finditer(r"`([a-z][\w-]*)`\s+(?:sub)?command", line):
            if self.defs.commands or self.prog_names:
                self.add("command", m.group(1), rel, number, heading, "loose")

    def scan_span(self, rel, number, span, heading, env_hint, default=None):
        span = span.strip()
        if not span:
            return
        if span.startswith("-"):
            for name in flags_in_span(span):
                self.add("flag", name, rel, number, heading, "loose", default=default)
            return
        if re.search(r"\s", span) or "=" in span:
            handled = self.scan_command_text(rel, number, span, heading)
            if handled:
                return
        if env_hint and re.fullmatch(ENV_TOKEN, span) and self.env_ok(span):
            self.add("env", span, rel, number, heading, "loose")
        elif (env_hint or span.startswith("export ")) and re.match(r"^(?:export\s+)?[A-Z][A-Z0-9_]*=", span):
            name = re.match(r"^(?:export\s+)?([A-Z][A-Z0-9_]*)=", span).group(1)
            if self.env_ok(name):
                self.add("env", name, rel, number, heading, "loose")

    def scan_command_text(self, rel, number, text, heading) -> bool:
        segs = parse_command_segments(text, self.prog_names, self.root_names())
        if not segs:
            return False
        for _cands, args in segs:
            self.add_command_mentions(rel, number, heading, args)
        return True

    def root_names(self):
        return {n for n in self.prog_names}

    def add_command_mentions(self, rel, number, heading, args):
        skip_next = False
        command = None
        flags = []
        for token in args:
            if token == "--":
                break
            if skip_next:
                skip_next = False
                continue
            if token.startswith("--") and len(token) > 2:
                name = token.split("=")[0]
                flags.append(name)
                defined = self.flag_index.get(canon(name))
                if "=" not in token and defined and defined[0].takes_value:
                    skip_next = True
            elif token.startswith("-") and len(token) >= 2 and token[1].isalpha():
                name = token.split("=")[0]
                flags.append(name)
                defined = self.flag_index.get(canon(name))
                if "=" not in token and defined and defined[0].takes_value:
                    skip_next = True
            elif command is None and re.fullmatch(r"[a-z][a-z0-9_-]*", token):
                command = token
        if command:
            self.add("command", command, rel, number, heading, "command")
        for name in flags:
            self.add("flag", name, rel, number, heading, "command", command=command)

    def scan_fence(self, rel, number, line, lang, heading):
        if lang in ENV_FENCE_LANGS:
            m = re.match(r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=", line)
            if m and self.env_ok(m.group(1)):
                self.add("env", m.group(1), rel, number, heading, "loose")
            return
        if lang not in SHELL_LANGS:
            return
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            return
        prompt = re.match(r"^\s*(?:\$|%|>|PS [^>]*>)\s+(.*)$", line)
        if lang in PROMPT_ONLY_LANGS and not prompt:
            return
        body = prompt.group(1) if prompt else stripped
        help_m = HELP_LINE_RE.match(line) if not prompt else None
        if help_m:
            for name in flags_in_span(help_m.group(1)):
                self.add("flag", name, rel, number, heading, "loose")
            return
        self.scan_env_in_line(rel, number, body, heading)
        if self.scan_command_text(rel, number, body, heading):
            return
        if re.match(r"^--?[A-Za-z]", body):
            for name in flags_in_span(body):
                self.add("flag", name, rel, number, heading, "loose")
        elif re.match(r"^[a-z][\w.-]*\s", body) or prompt:
            self.ignored_command_lines += 1

    def scan_env_in_line(self, rel, number, body, heading):
        for m in re.finditer(r"(?:^|[;&|]\s*)(?:export|set|setx|declare\s+-x)\s+([A-Za-z_][A-Za-z0-9_]*)\s*[= ]", body):
            if self.env_ok(m.group(1)):
                self.add("env", m.group(1), rel, number, heading, "loose")
        for m in re.finditer(r"\$env:([A-Za-z_][A-Za-z0-9_]*)", body):
            if self.env_ok(m.group(1)):
                self.add("env", m.group(1), rel, number, heading, "loose")
        m = re.match(r"^([A-Z][A-Z0-9_]*)=\S*\s*$", body)
        if m and self.env_ok(m.group(1)):
            self.add("env", m.group(1), rel, number, heading, "loose")
        for m in re.finditer(r"\$\{?(" + ENV_TOKEN + r")\}?", body):
            if self.env_ok(m.group(1)) and m.group(1) not in self.assigned:
                self.add("env", m.group(1), rel, number, heading, "loose")
        m = re.match(r"^((?:[A-Z][A-Z0-9_]*=\S+\s+)+)(\S+)", body)
        if m and norm_name(posixpath.splitext(posixpath.basename(m.group(2)))[0]) in self.prog_names:
            for assign in re.findall(r"([A-Z][A-Z0-9_]*)=", m.group(1)):
                if self.env_ok(assign):
                    self.add("env", assign, rel, number, heading, "loose")


def code_spans(line: str) -> list:
    return [m.group(2).strip() for m in INLINE_CODE_RE.finditer(line)]


def flags_in_span(span: str) -> list:
    span = re.sub(r"--\[no-\]([\w-]+)", r"--\1 --no-\1", span)
    names = []
    for part in re.split(r"[\s,|/=]+", span):
        m = re.fullmatch(r"--?[A-Za-z][\w-]*", part)
        if m and part not in names:
            names.append(part)
    return names


def clean_default(cell: str):
    text = cell.strip().strip("`").strip().strip("\"'")
    if not text or text in ("-", "none", "None", "n/a", "N/A", "null", "(none)", "required"):
        return None
    return text


def prose_default(line: str):
    m = re.search(r"(?i)\bdefaults?\b(?:\s+(?:to|is|value))?\s*[:=]?\s*(?:`([^`]+)`|\"([^\"]+)\"|'([^']+)'|([\w./:+-]+))", line)
    if not m:
        return None
    return clean_default(next(g for g in m.groups() if g is not None))


# --------------------------------------------------------------------------------------
# Comparison
# --------------------------------------------------------------------------------------

def make_finding(rule, severity, file, line, message, suggestion, kind, item, section="", first_run=False,
                 side="docs", locations=None):
    return {"rule": rule, "severity": severity, "file": file, "line": line, "message": message,
            "suggestion": suggestion, "kind": kind, "item": item, "section": section, "first_run": first_run,
            "side": side, "locations": locations or ["%s:%d" % (file, line)]}


def lower_severity(severity: str) -> str:
    return SEVERITIES[min(SEV_RANK[severity] + 1, len(SEVERITIES) - 1)]


def group_mentions(mentions: list, key) -> dict:
    groups = {}
    for m in mentions:
        groups.setdefault(key(m), []).append(m)
    return groups


def normalise_default(text):
    if text is None:
        return None
    return str(text).strip().strip("`\"'").strip().lower()


def defaults_differ(doc_text, code_text) -> bool:
    a, b = normalise_default(doc_text), normalise_default(code_text)
    if a is None or b is None or not re.fullmatch(r"[\w.+-]+", a) or not re.fullmatch(r"[\w.+-]+", b):
        return False

    def number(text):
        try:
            return float(text)
        except ValueError:
            return None
    na, nb = number(a), number(b)
    if na is not None and nb is not None:
        return na != nb
    if (na is None) != (nb is None):
        return False
    bools = {"true", "false"}
    if (a in bools) != (b in bools):
        return False
    return a != b


def similar(a: str, b: str) -> float:
    a, b = a.lstrip("-").replace("_", "-").lower(), b.lstrip("-").replace("_", "-").lower()
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    if min(len(a), len(b)) >= 4 and (a in b or b in a):
        ratio = max(ratio, 0.7)
    return ratio


def compare(root: str, defs: Definitions, mentions: list, doc_corpus: str, include_system: bool, ignored_lines: int) -> list:
    findings = []
    index = defs.flag_index()
    defined_names = set(index)
    always = {"-h", "--help"}
    if defs.has_version or re.search(r"version_option|\.version\(|Version:", getattr(defs, "code_text", "")):
        always |= {"--version", "-V"}
    dynamic_flag_files = sorted({f for f, _l, why in defs.dynamic if "environment" not in why and "viper" not in why})
    parser_found = bool(defs.flags or defs.commands)
    flag_mentions = [m for m in mentions if m.kind == "flag"]
    cmd_mentions = [m for m in mentions if m.kind == "command"]
    env_mentions = [m for m in mentions if m.kind == "env"]

    def flag_defined(name: str) -> bool:
        c = canon(name)
        if c in defined_names or c in always:
            return True
        if c.startswith("--no-") and ("--" + c[5:]) in defined_names:
            return True
        if re.fullmatch(r"-[A-Za-z]{2,}", name):
            return all(("-" + ch) in defined_names or ("-" + ch) in always for ch in name[1:])
        return False

    missing_flags, missing_cmds = [], []
    if not parser_found:
        if flag_mentions or cmd_mentions:
            findings.append(make_finding(
                "CD010", "info", flag_mentions[0].file if flag_mentions else cmd_mentions[0].file,
                flag_mentions[0].line if flag_mentions else cmd_mentions[0].line,
                "no argparse, click, typer, flag, cobra or commander definitions were found in the code",
                "pass --code with the folder that holds the CLI, or compare by hand", "flag", "(all)"))
    else:
        for name, group in sorted(group_mentions(flag_mentions, lambda m: canon(m.name)).items()):
            first = group[0]
            if flag_defined(first.name):
                continue
            missing_flags.append(group)
        for name, group in sorted(group_mentions(cmd_mentions, lambda m: m.name).items()):
            if not defs.commands:
                continue
            if name in defs.commands or name in defs.aliases or norm_name(name) in {norm_name(c) for c in defs.commands}:
                continue
            first = min(group, key=lambda m: (m.context != "command", m.file, m.line))
            sev = "high" if first.context == "command" else "medium"
            if dynamic_flag_files:
                sev = lower_severity(sev)
            near = difflib.get_close_matches(name, sorted(defs.commands), n=1, cutoff=0.6)
            missing_cmds.append((first, group, sev, near))
        # wrong command scope
        for m in flag_mentions:
            if m.context != "command" or not m.command or not flag_defined(m.name):
                continue
            real = defs.aliases.get(m.command, m.command)
            if real not in defs.commands:
                continue
            owners = index.get(canon(m.name), [])
            if not owners or any(f.command is None or f.command == real for f in owners):
                continue
            where = sorted({f.command for f in owners})
            findings.append(make_finding(
                "CD009", "medium", m.file, m.line,
                "`%s` is documented under `%s` but defined for %s" % (m.name, m.command, ", ".join("`%s`" % w for w in where)),
                "move the example to `%s %s` or add the flag to `%s`" % (where[0], m.name, m.command),
                "flag", canon(m.name), m.section, m.first_run))

    # documented text corpus lookups are lenient, so undocumented means "nowhere in the docs"
    def in_docs(token: str) -> bool:
        return re.search(r"(?<![\w-])" + re.escape(token) + r"(?![\w-])", doc_corpus) is not None

    mentioned = set()
    for m in flag_mentions:
        mentioned.add(canon(m.name))
        if re.fullmatch(r"-[A-Za-z]{2,}", m.name):
            mentioned.update("-" + ch for ch in m.name[1:])
        if m.name.startswith("--no-"):
            mentioned.add("--" + m.name[5:])
    undocumented_flags = []
    for flag in defs.flags:
        if flag.hidden or all(canon(n) in always for n in flag.names):
            continue
        if any(canon(n) in mentioned or in_docs(n) for n in flag.names):
            continue
        undocumented_flags.append(flag)
    undocumented_cmds = [(name, loc) for name, loc in sorted(defs.commands.items())
                         if not re.search(r"(?<![\w-])" + re.escape(name) + r"(?![\w-])", doc_corpus)]

    # renames: pair a missing documented name with an undocumented defined one
    used_missing, used_undoc = set(), set()
    pairs = []
    for gi, group in enumerate(missing_flags):
        for fi, flag in enumerate(undocumented_flags):
            long_names = [n for n in flag.names if len(canon(n)) > 2]
            if len(canon(group[0].name)) <= 2 or not long_names:
                continue
            score = max(similar(group[0].name, n) for n in long_names)
            if score >= 0.6:
                pairs.append((score, gi, fi))
    pairs.sort(key=lambda p: (-p[0], p[1], p[2]))
    for score, gi, fi in pairs:
        if gi in used_missing or fi in used_undoc:
            continue
        used_missing.add(gi)
        used_undoc.add(fi)
        group, flag = missing_flags[gi], undocumented_flags[fi]
        first = min(group, key=lambda m: (m.context != "command", m.file, m.line))
        sev = "high" if any(m.context == "command" for m in group) else "medium"
        real = max(flag.names, key=lambda n: similar(first.name, n))
        findings.append(make_finding(
            "CD007", sev, first.file, first.line,
            "`%s` is documented, the code defines `%s` (%s:%d); likely renamed" % (first.name, real, flag.file, flag.line),
            "update the docs to `%s`" % real, "flag", canon(first.name), first.section, first.first_run,
            locations=["%s:%d" % (m.file, m.line) for m in group]))
    for gi, group in enumerate(missing_flags):
        if gi in used_missing:
            continue
        first = min(group, key=lambda m: (m.context != "command", m.file, m.line))
        sev = "high" if any(m.context == "command" for m in group) else "medium"
        note = ""
        if dynamic_flag_files:
            sev = lower_severity(sev)
            note = " (the code builds flags dynamically in %d file(s); it may be defined there)" % len(dynamic_flag_files)
        near = difflib.get_close_matches(first.name, sorted(defined_names), n=1, cutoff=0.6)
        findings.append(make_finding(
            "CD001", sev, first.file, first.line, "`%s` is documented but not defined%s" % (first.name, note),
            "did you mean `%s`?" % near[0] if near else "remove it from the docs, or add the flag to the code",
            "flag", canon(first.name), first.section, first.first_run,
            locations=["%s:%d" % (m.file, m.line) for m in group]))
    for first, group, sev, near in missing_cmds:
        findings.append(make_finding(
            "CD002", sev, first.file, first.line, "command `%s` is documented but not defined" % first.name,
            "did you mean `%s`?" % near[0] if near else "remove it from the docs, or add the command to the code",
            "command", first.name, first.section, first.first_run,
            locations=["%s:%d" % (m.file, m.line) for m in group]))
    for fi, flag in enumerate(undocumented_flags):
        if fi in used_undoc:
            continue
        findings.append(make_finding(
            "CD004", "low", flag.file, flag.line,
            "`%s` is defined%s but appears nowhere in the docs" % (", ".join(flag.names), " for `%s`" % flag.command if flag.command else ""),
            "document it: %s" % (flag.help or "add a row to the options table"), "flag", canon(flag.names[0]),
            side="code"))
    for name, (file, line) in undocumented_cmds:
        findings.append(make_finding("CD005", "low", file, line, "command `%s` is defined but appears nowhere in the docs" % name,
                                     "add it to the usage section", "command", name, side="code"))

    # defaults
    for m in flag_mentions:
        if m.default is None:
            continue
        owners = index.get(canon(m.name), [])
        scoped = [f for f in owners if m.command is None or f.command in (None, defs.aliases.get(m.command, m.command))]
        for flag in scoped[:1]:
            if flag.default is not None and defaults_differ(m.default, flag.default):
                findings.append(make_finding(
                    "CD008", "medium", m.file, m.line,
                    "`%s`: docs say default `%s`, code says `%s` (%s:%d)" % (m.name, m.default, flag.default, flag.file, flag.line),
                    "change the docs to `%s`" % flag.default, "flag", canon(m.name), m.section, m.first_run))

    # environment variables
    code_text = getattr(defs, "code_text", "")
    for name, group in sorted(group_mentions(env_mentions, lambda m: m.name).items()):
        if name in defs.envs:
            continue
        first = min(group, key=lambda m: (m.file, m.line))
        literal = re.search(r"(?<![A-Z0-9_])" + re.escape(name) + r"(?![A-Z0-9_])", code_text) is not None
        sev = "info" if literal else "medium"
        near = difflib.get_close_matches(name, sorted(n for n in defs.envs if include_system or not is_system_env(n)), n=1, cutoff=0.75)
        if literal:
            msg = "`%s` is documented; the name appears in the code but not through a recognised environment read" % name
            hint = "check how the code reads it"
        else:
            msg = "`%s` is documented but the code never reads it" % name
            hint = "did you mean `%s`?" % near[0] if near else "remove it from the docs, or read it in the code"
        rename_target = near[0] if near and not literal else None
        if rename_target and not any(m.name == rename_target for m in env_mentions):
            findings.append(make_finding(
                "CD007", "medium", first.file, first.line,
                "`%s` is documented, the code reads `%s` (%s:%d); likely renamed" % (name, rename_target, defs.envs[rename_target][0][0], defs.envs[rename_target][0][1]),
                "update the docs to `%s`" % rename_target, "env", name, first.section, first.first_run,
                locations=["%s:%d" % (m.file, m.line) for m in group]))
            continue
        findings.append(make_finding("CD003", sev, first.file, first.line, msg, hint, "env", name, first.section,
                                     first.first_run, locations=["%s:%d" % (m.file, m.line) for m in group]))
    renamed_env_targets = {f["suggestion"].split("`")[1] for f in findings if f["rule"] == "CD007" and f["kind"] == "env"}
    for name, reads in sorted(defs.envs.items()):
        if not include_system and is_system_env(name):
            continue
        if name in renamed_env_targets:
            continue
        if re.search(r"(?<![A-Za-z0-9_])" + re.escape(name) + r"(?![A-Za-z0-9_])", doc_corpus):
            continue
        file, line, via = reads[0]
        findings.append(make_finding("CD006", "low", file, line, "`%s` is read (%s) but appears nowhere in the docs" % (name, via),
                                     "add it to the configuration section", "env", name, side="code"))
    return findings


def sort_findings(findings: list) -> list:
    return sorted(findings, key=lambda f: (SEV_RANK[f["severity"]], f["rule"], f["file"], f["line"], f["item"]))


def counts(findings: list) -> dict:
    return {sev: sum(1 for f in findings if f["severity"] == sev) for sev in SEVERITIES}


def should_fail(findings: list, fail_on: str) -> bool:
    if fail_on == "never":
        return False
    return any(SEV_RANK[f["severity"]] <= SEV_RANK[fail_on] for f in findings)


def cell(text) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def options_table(defs: Definitions) -> str:
    rows = [f for f in defs.flags if not f.hidden and f.source not in ("sys.argv", "process.argv")]
    rows.sort(key=lambda f: (f.command or "", canon(f.names[0])))
    lines = ["| Option | Command | Default | Env | Description |", "|---|---|---|---|---|"]
    for flag in rows:
        option = ", ".join("`%s`" % n for n in sorted(flag.names, key=lambda n: (not n.startswith("--"), n)))
        if flag.takes_value:
            option += " `VALUE`"
        lines.append("| %s | %s | %s | %s | %s |" % (
            option, "`%s`" % flag.command if flag.command else "all",
            "`%s`" % flag.default if flag.default is not None else "", "`%s`" % flag.env if flag.env else "",
            cell(flag.help)))
    return "\n".join(lines) + "\n"


def render_markdown(report: dict, table: str = "", table_only: bool = False) -> str:
    if table_only:
        return table
    s = report["summary"]
    c = s["by_severity"]
    lines = ["Scanned %d doc file(s) and %d code file(s). CLI names: %s." % (
        s["doc_files"], s["code_files"], ", ".join("`%s`" % n for n in report["cli_names"]) or "none found"),
        "Documented: %d flag(s), %d command(s), %d environment variable(s). Defined in code: %d flag(s), %d command(s), %d environment variable(s)." % (
            s["documented"]["flags"], s["documented"]["commands"], s["documented"]["env"],
            s["defined"]["flags"], s["defined"]["commands"], s["defined"]["env"]),
        "%d finding(s): %d high, %d medium, %d low, %d info." % (s["findings"], c["high"], c["medium"], c["low"], c["info"]), ""]
    if report["findings"]:
        by_rule = {}
        for f in report["findings"]:
            by_rule[f["rule"]] = by_rule.get(f["rule"], 0) + 1
        lines += ["| Rule | Count | Meaning |", "|---|---:|---|"]
        for rule in sorted(by_rule):
            lines.append("| %s | %d | %s |" % (rule, by_rule[rule], RULES[rule]))
        lines += ["", "| Location | Rule | Severity | Section | Problem | Suggestion |", "|---|---|---|---|---|---|"]
        for f in report["findings"]:
            section = f["section"] + (" (first run)" if f["first_run"] else "") if f["section"] else ""
            more = " (+%d more)" % (len(f["locations"]) - 1) if len(f["locations"]) > 1 else ""
            lines.append("| `%s:%d`%s | %s | %s | %s | %s | %s |" % (
                f["file"], f["line"], more, f["rule"], f["severity"], cell(section), cell(f["message"]), cell(f["suggestion"])))
    else:
        lines.append("No findings.")
    dyn = report["dynamic"]
    lines.append("")
    if dyn:
        lines.append("Built dynamically, not checked (%d place(s) in %d file(s)):" % (len(dyn), len({d["file"] for d in dyn})))
        lines.append("")
        for d in dyn:
            lines.append("- `%s:%d` %s" % (d["file"], d["line"], d["why"]))
    else:
        lines.append("No dynamically built flags or environment names were found.")
    if report["unparsable"]:
        lines.append("")
        lines.append("Python files that could not be parsed: %s." % ", ".join("`%s`" % f for f in report["unparsable"]))
    if report["ignored_command_lines"]:
        lines.append("")
        lines.append("Shell lines for other programs were ignored: %d." % report["ignored_command_lines"])
    if table:
        lines += ["", "### Options table generated from the code", "", table.rstrip("\n")]
    return "\n".join(lines) + "\n"


def analyse(root: str, docs_paths=(), code_paths=(), excludes=(), cli_names=(), include_system=False) -> dict:
    defs = extract_code(root, list(code_paths), list(excludes))
    names = sorted(set(defs.prog_names) | set(cli_names), key=str.lower)
    if not names:
        names = [os.path.basename(os.path.abspath(root))]
    doc_files = collect_doc_files(root, list(docs_paths), list(excludes))
    scanner = DocScanner({norm_name(n) for n in names}, defs, include_system)
    corpus = []
    for rel in doc_files:
        text = read_text(os.path.join(root, rel))
        corpus.append(text)
        scanner.scan(rel, text)
    mentions = scanner.mentions
    findings = compare(root, defs, mentions, "\n".join(corpus), include_system, scanner.ignored_command_lines)
    ordered = sort_findings(findings)
    return {
        "tool": "cli_drift",
        "cli_names": names,
        "summary": {
            "doc_files": len(doc_files), "code_files": defs.code_files, "findings": len(ordered),
            "by_severity": counts(ordered),
            "documented": {"flags": len({canon(m.name) for m in mentions if m.kind == "flag"}),
                           "commands": len({m.name for m in mentions if m.kind == "command"}),
                           "env": len({m.name for m in mentions if m.kind == "env"})},
            "defined": {"flags": len(defs.flags), "commands": len(defs.commands), "env": len(defs.envs)},
            "frameworks": sorted(defs.frameworks),
        },
        "findings": ordered,
        "dynamic": [{"file": f, "line": l, "why": w} for f, l, w in sorted(set(defs.dynamic))],
        "unparsable": sorted(defs.unparsable),
        "ignored_command_lines": scanner.ignored_command_lines,
        "defined_flags": [f.to_dict() for f in defs.flags],
        "_defs": defs,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="cli_drift.py",
        description="Compare documented CLI flags, commands and environment variables with what the code defines.")
    parser.add_argument("path", nargs="?", default=".", help="project root (default: .)")
    parser.add_argument("--docs", action="append", default=[], metavar="PATH",
                        help="doc file or folder, relative to the root (repeatable; default: root docs and docs/)")
    parser.add_argument("--code", action="append", default=[], metavar="PATH",
                        help="code file or folder to read, relative to the root (repeatable; default: the root)")
    parser.add_argument("--exclude", action="append", default=[], metavar="GLOB", help="skip paths matching GLOB")
    parser.add_argument("--cli", action="append", default=[], metavar="NAME", help="name of your CLI program (repeatable)")
    parser.add_argument("--include-system", action="store_true", help="also report PATH, HOME, CI, GITHUB_* ...")
    parser.add_argument("--table", action="store_true", help="append an options table generated from the code")
    parser.add_argument("--table-only", action="store_true", help="print only the generated options table")
    parser.add_argument("--json", action="store_true", help="print JSON instead of Markdown")
    parser.add_argument("--fail-on", default="never", choices=["high", "medium", "low", "never"],
                        help="exit 1 when a finding of this severity or worse exists (default: never)")
    args = parser.parse_args(argv)
    root = os.path.abspath(args.path)
    if not os.path.isdir(root):
        print("error: %s is not a folder" % args.path, file=sys.stderr)
        return 2
    for extra in args.docs + args.code:
        if not os.path.exists(os.path.join(root, extra)):
            print("error: %s does not exist under %s" % (extra, args.path), file=sys.stderr)
            return 2
    report = analyse(root, args.docs, args.code, args.exclude, args.cli, args.include_system)
    if report["summary"]["doc_files"] == 0:
        print("error: no documentation files found (looked for README and docs/ under %s)" % args.path, file=sys.stderr)
        return 2
    if report["summary"]["code_files"] == 0:
        print("error: no source files (.py, .go, .js, .ts) found under %s" % args.path, file=sys.stderr)
        return 2
    defs = report.pop("_defs")
    table = options_table(defs) if (args.table or args.table_only) else ""
    if args.json:
        if table:
            report["options_table"] = table
        sys.stdout.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
    else:
        sys.stdout.write(render_markdown(report, table, args.table_only))
    return 1 if should_fail(report["findings"], args.fail_on) else 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    sys.exit(main())
