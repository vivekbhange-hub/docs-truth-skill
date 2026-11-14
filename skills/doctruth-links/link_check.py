#!/usr/bin/env python3
"""link_check: find links, images and anchors in documentation that no longer point at anything.

Scans Markdown (plus basic .rst and .adoc) for inline links, images, reference-style links,
autolinks, HTML href/src attributes and heading anchors, and checks them offline:

  LK001 relative file link does not exist (with a "did you mean" from filename similarity)
  LK002 file exists but the case differs (works on Windows/macOS, breaks on Linux and GitHub)
  LK003 #anchor does not exist in the target file (GitHub slug rules, duplicate suffixes -1, -2)
  LK004 image path does not exist
  LK005 empty link target
  LK006 http:// link where https:// is likely
  LK007 localhost / loopback link
  LK008 TODO or placeholder link
  LK009 orphaned doc: no other doc links to it
  LK010 reference-style link uses a label that is never defined
  LK011 link climbs above the project root
  LK012 dead external link (404/410 and similar)        --online only
  LK013 external link could not be reached or verified   --online only
  LK014 external link refused by the SSRF guard          --online only

Code fences, inline code and HTML comments are skipped. Nothing touches the network unless you
pass --online; then requests are rate limited, capped, and never sent to private, loopback or
link-local addresses. Standard library only; the same input gives the same report.
"""
from __future__ import annotations

import argparse
import bisect
import difflib
import fnmatch
import html
import http.client
import ipaddress
import json
import os
import posixpath
import re
import socket
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".tox", ".mypy_cache",
             ".pytest_cache", "dist", "build", "target", "vendor", ".next", ".idea", ".vscode"}
MD_EXT = {".md", ".markdown"}
DOC_EXT = MD_EXT | {".rst", ".adoc", ".asciidoc"}
SEVERITIES = ("high", "medium", "low", "info")
SEV_RANK = {name: i for i, name in enumerate(SEVERITIES)}
MAX_FILE_BYTES = 2_000_000
MAX_INDEX_FILES = 20000
USER_AGENT = "doctruth-link-check/1.0 (+https://github.com/vivekbhange-hub/docs-truth-skill)"

RULES = {
    "LK001": ("high", "Relative file link does not exist"),
    "LK002": ("high", "File exists but the letter case differs"),
    "LK003": ("high", "Anchor does not exist in the target file"),
    "LK004": ("high", "Image file does not exist"),
    "LK005": ("medium", "Empty link target"),
    "LK006": ("low", "http:// link, https:// is likely available"),
    "LK007": ("low", "Link to localhost or a loopback address"),
    "LK008": ("medium", "TODO or placeholder link"),
    "LK009": ("low", "Orphaned doc: no other doc links to it"),
    "LK010": ("medium", "Reference-style link with an undefined label"),
    "LK011": ("low", "Link climbs above the project root"),
    "LK012": ("medium", "Dead external link"),
    "LK013": ("low", "External link unreachable or not verifiable"),
    "LK014": ("info", "External link refused by the SSRF guard"),
}

FIRST_RUN_RE = re.compile(
    r"(?i)\b(install(?:ation|ing)?|getting started|quick ?start|set ?up|usage|first run|try it|"
    r"tutorial|requirements|prerequisites|60 seconds|60-second)\b")

FENCE_RE = re.compile(r"^(\s*)(`{3,}|~{3,})(.*)$")
ATX_RE = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$")
SETEXT_RE = re.compile(r"^ {0,3}(=+|-+)[ \t]*$")
INLINE_CODE_RE = re.compile(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)")
COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
OPEN_LINK_RE = re.compile(r"(!?)\[((?:[^\[\]\\\n]|\\.|\[[^\]\n]*\])*)\]\(")
REF_USE_RE = re.compile(r"(?<![\w\])])(!?)\[((?:[^\[\]\\\n]|\\.)+)\]\[([^\]\n]*)\]")
REF_DEF_RE = re.compile(r"^ {0,3}\[([^\]\n]+)\]:[ \t]*(<[^>\n]*>|\S*)(?:[ \t]+(?:\"[^\"\n]*\"|'[^'\n]*'|\([^)\n]*\)))?[ \t]*$")
AUTOLINK_RE = re.compile(r"<((?:https?|ftp)://[^\s<>]+)>")
HTML_ATTR_RE = re.compile(r"""<(a|img|source|link|script|video|audio)\b[^>]*?\b(href|src)\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.I)
HTML_ID_RE = re.compile(r"""\b(?:id|name)\s*=\s*(?:"([^"]+)"|'([^']+)')""")
BARE_URL_RE = re.compile(r"(?<![\w\"'<=/])(https?://[^\s<>\"'`\\\]]+)")
EXPLICIT_ID_RE = re.compile(r"\s*\{#([\w:.-]+)\}\s*$")
LINE_ANCHOR_RE = re.compile(r"^L\d+(C\d+)?(-L\d+(C\d+)?)?$")
PLACEHOLDER_EXACT = {"#", "todo", "tbd", "fixme", "xxx", "link", "url", "...", "insert-link", "insert link",
                     "your-link-here", "your link here", "changeme", "placeholder", "link-here", "path/to/file",
                     "link/to/file", "#todo", "#tbd", "#link"}
PLACEHOLDER_RE = re.compile(
    r"(?:(?:^|/)(?:TODO|TBD|FIXME)(?:/|$|#|\?)|(?i:\b(?:your|my)[-_](?:username|user|org|repo|name|link|url|domain)\b)|"
    r"<[A-Za-z][^>]*>|\{\{[^}]*\}\}|/(?:USER|USERNAME|OWNER|ORG|REPO|YOUR_[A-Z_]+)(?:/|$))")
ENTRY_NAMES = ("readme", "license", "licence", "changelog", "changes", "contributing", "code_of_conduct",
               "security", "support", "funding", "skill.md", "agents.md", "claude.md", "gemini.md",
               "pull_request_template", "issue_template", "copying", "notice", "authors", "history")
ENTRY_STEMS = {"index", "readme", "home", "_sidebar", "_footer", "summary"}
NAV_FILES = ("mkdocs.yml", "mkdocs.yaml", "sidebars.js", "sidebars.json", "_toc.yml", "toc.yml", "book.toml",
             "docusaurus.config.js", "conf.py", "index.rst")


# --------------------------------------------------------------------------------------
# Markdown scanning helpers
# --------------------------------------------------------------------------------------

def blank(match) -> str:
    """Replace a regex match by spaces, keeping newlines so line numbers survive."""
    return re.sub(r"[^\n]", " ", match.group(0))


def mask_fences(text: str) -> list:
    """Return the lines of `text` with fenced code (markers included) emptied out."""
    out = []
    fence = None
    for line in text.split("\n"):
        m = FENCE_RE.match(line)
        if fence is None:
            if m and not (m.group(2).startswith("`") and "`" in m.group(3)):
                fence = (m.group(2)[0], len(m.group(2)))
                out.append("")
            else:
                out.append(line)
        else:
            out.append("")
            if m and m.group(2)[0] == fence[0] and len(m.group(2)) >= fence[1] and not m.group(3).strip():
                fence = None
    return out


def front_matter_end(lines: list) -> int:
    """Index of the first line after YAML front matter (0 when there is none)."""
    if lines and lines[0].strip() == "---":
        for i in range(1, min(len(lines), 200)):
            if lines[i].strip() in ("---", "..."):
                return i + 1
    return 0


def prepare_markdown(text: str) -> list:
    """Lines with fences, front matter, HTML comments and inline code blanked out."""
    lines = mask_fences(text.replace("\r\n", "\n"))
    skip = front_matter_end(lines)
    for i in range(skip):
        lines[i] = ""
    joined = COMMENT_RE.sub(blank, "\n".join(lines))
    lines = joined.split("\n")
    return [INLINE_CODE_RE.sub(blank, line) for line in lines]


def heading_text_to_slug_source(text: str) -> str:
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[([^\]]*)\]\[[^\]]*\]", r"\1", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("`", "")
    text = html.unescape(text)
    text = re.sub(r"(?<![\w])_+([^_\n]+?)_+(?![\w])", r"\1", text)
    text = re.sub(r":[a-z0-9_+\-]+:", "", text)
    return text


def github_slug(text: str) -> str:
    """GitHub's heading id: lowercase, drop punctuation, spaces to hyphens (no suffix handling)."""
    text = heading_text_to_slug_source(text).strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def parse_headings(lines: list) -> list:
    """Return [(line_number, text)] for ATX and setext headings in prepared lines."""
    found = []
    for i, line in enumerate(lines):
        m = ATX_RE.match(line)
        if m:
            found.append((i + 1, (m.group(2) or "").strip()))
            continue
        if i + 1 < len(lines) and line.strip() and not line.lstrip().startswith(("|", ">", "-", "*", "+", "#", "<")) \
                and not re.match(r"^\s*\d+[.)]\s", line) and SETEXT_RE.match(lines[i + 1]):
            found.append((i + 1, line.strip()))
    return found


def collect_anchors(text: str) -> set:
    """All anchors a Markdown file offers: heading slugs with GitHub duplicate suffixes, explicit ids."""
    lines = prepare_markdown(text)
    anchors = set()
    used = {}
    for _number, heading in parse_headings(lines):
        explicit = EXPLICIT_ID_RE.search(heading)
        if explicit:
            anchors.add(explicit.group(1))
            heading = heading[:explicit.start()]
        base = github_slug(heading)
        count = used.get(base, 0)
        used[base] = count + 1
        anchors.add(base if count == 0 else f"{base}-{count}")
    plain = "\n".join(mask_fences(text.replace("\r\n", "\n")))
    plain = COMMENT_RE.sub(blank, plain)
    for m in HTML_ID_RE.finditer(plain):
        anchors.add(m.group(1) or m.group(2))
    return anchors


def parse_destination(text: str, pos: int):
    """Parse an inline link destination starting at `pos` (just after `(`).

    Returns (target, end_index) or None when the link is not well formed."""
    n = len(text)
    while pos < n and text[pos] in " \t":
        pos += 1
    target = ""
    if pos < n and text[pos] == "<":
        end = text.find(">", pos)
        if end == -1 or "\n" in text[pos:end]:
            return None
        target = text[pos + 1:end]
        pos = end + 1
    else:
        depth = 0
        start = pos
        while pos < n:
            ch = text[pos]
            if ch == "\\" and pos + 1 < n:
                pos += 2
                continue
            if ch in " \t\n":
                break
            if ch == "(":
                depth += 1
            elif ch == ")":
                if depth == 0:
                    break
                depth -= 1
            pos += 1
        target = text[start:pos]
    while pos < n and text[pos] in " \t":
        pos += 1
    if pos < n and text[pos] in "\"'(":
        close = ")" if text[pos] == "(" else text[pos]
        end = text.find(close, pos + 1)
        if end == -1:
            return None
        pos = end + 1
        while pos < n and text[pos] in " \t":
            pos += 1
    if pos < n and text[pos] == ")":
        return target, pos + 1
    return None


class Link:
    """One link-like thing found in a document."""

    def __init__(self, kind: str, target: str, line: int, label: str = ""):
        self.kind = kind          # link, image, ref, autolink, html, url
        self.target = target
        self.line = line
        self.label = label

    @property
    def is_image(self) -> bool:
        return self.kind == "image"


def clean_url(url: str) -> str:
    """Strip trailing sentence punctuation and an unbalanced closing parenthesis from a bare URL."""
    url = url.rstrip(".,;:!?*_")
    while url.endswith(")") and url.count(")") > url.count("("):
        url = url[:-1].rstrip(".,;:!?*_")
    return url


def extract_markdown(text: str):
    """Return (links, headings, undefined_refs) for one Markdown document."""
    lines = prepare_markdown(text)
    joined = "\n".join(lines)
    line_starts = [0]
    for line in lines:
        line_starts.append(line_starts[-1] + len(line) + 1)

    def line_of(index: int) -> int:
        return bisect.bisect_right(line_starts, index)

    links = []
    masked = list(joined)

    def mask(start: int, end: int) -> None:
        for k in range(start, min(end, len(masked))):
            if masked[k] != "\n":
                masked[k] = " "

    # reference definitions first, so shortcut references can be recognised
    definitions = {}
    for i, line in enumerate(lines):
        m = REF_DEF_RE.match(line)
        if m:
            target = m.group(2)
            if target.startswith("<") and target.endswith(">"):
                target = target[1:-1]
            definitions[m.group(1).strip().lower()] = (target, i + 1)
            mask(line_starts[i], line_starts[i] + len(line))

    pos = 0
    while True:
        m = OPEN_LINK_RE.search(joined, pos)
        if not m:
            break
        parsed = parse_destination(joined, m.end())
        pos = m.start() + len(m.group(1)) + 1
        if parsed is None:
            continue
        target, end = parsed
        links.append(Link("image" if m.group(1) else "link", target, line_of(m.start()), m.group(2)))
        mask(m.start(), end)
    scan = "".join(masked)
    undefined = []
    image_labels = set()
    for m in REF_USE_RE.finditer(scan):
        label_text, label = m.group(2), m.group(3)
        if label_text.startswith("^"):
            continue
        key = (label or label_text).strip().lower()
        if key not in definitions:
            undefined.append((line_of(m.start()), label or label_text))
        elif m.group(1):
            image_labels.add(key)
    for key, (target, number) in sorted(definitions.items(), key=lambda item: item[1][1]):
        links.append(Link("image" if key in image_labels else "ref", target, number, key))
    for m in AUTOLINK_RE.finditer(scan):
        links.append(Link("autolink", m.group(1), line_of(m.start())))
        mask(m.start(), m.end())
    scan = "".join(masked)
    for m in HTML_ATTR_RE.finditer(scan):
        value = m.group(3) if m.group(3) is not None else m.group(4)
        kind = "image" if m.group(1).lower() == "img" or (m.group(2).lower() == "src" and m.group(1).lower() != "script") else "html"
        links.append(Link(kind, html.unescape(value), line_of(m.start()), m.group(1).lower()))
        mask(m.start(), m.end())
    scan = "".join(masked)
    for m in BARE_URL_RE.finditer(scan):
        url = clean_url(m.group(1))
        links.append(Link("url", url, line_of(m.start())))
    links.sort(key=lambda l: (l.line, l.kind, l.target))
    return links, parse_headings(lines), undefined


def extract_rst(text: str):
    links = []
    lines = text.replace("\r\n", "\n").split("\n")
    for i, line in enumerate(lines, 1):
        for m in re.finditer(r"`[^`<]*?\s*<([^>`]+)>`__?", line):
            links.append(Link("link", m.group(1).strip(), i))
        m = re.match(r"^\s*\.\.\s+(image|figure)::\s*(\S+)", line)
        if m:
            links.append(Link("image", m.group(2), i))
        m = re.match(r"^\s*\.\.\s+_[^:]+:\s*(\S+)\s*$", line)
        if m and not m.group(1).startswith("`"):
            links.append(Link("link", m.group(1), i))
    return links, [], []


def extract_adoc(text: str):
    links = []
    lines = text.replace("\r\n", "\n").split("\n")
    for i, line in enumerate(lines, 1):
        if line.lstrip().startswith("//"):
            continue
        for m in re.finditer(r"\b(link|xref):([^\s\[]+)\[", line):
            links.append(Link("link", m.group(2), i))
        for m in re.finditer(r"\bimage::?([^\s\[:][^\s\[]*)\[", line):
            links.append(Link("image", m.group(1), i))
        for m in re.finditer(r"(https?://[^\s\[<>]+)\[", line):
            links.append(Link("url", m.group(1), i))
    return links, [], []


# --------------------------------------------------------------------------------------
# Project model
# --------------------------------------------------------------------------------------

class Project:
    """Documents under a root folder plus cached directory listings."""

    def __init__(self, root: str, excludes=()):
        self.root = os.path.abspath(root)
        self.excludes = list(excludes)
        self._listings = {}
        self._index = None
        self._anchors = {}

    def excluded(self, rel: str) -> bool:
        return any(fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(posixpath.basename(rel), pat)
                   for pat in self.excludes)

    def walk(self):
        """Yield every file under the root as a posix relative path (sorted, skip dirs pruned)."""
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
            for name in sorted(filenames):
                rel = os.path.relpath(os.path.join(dirpath, name), self.root).replace(os.sep, "/")
                if not self.excluded(rel):
                    yield rel

    def doc_files(self) -> list:
        return [rel for rel in self.walk() if os.path.splitext(rel)[1].lower() in DOC_EXT]

    def listing(self, directory: str) -> list:
        key = directory
        if key not in self._listings:
            try:
                self._listings[key] = os.listdir(os.path.join(self.root, directory) if directory else self.root)
            except OSError:
                self._listings[key] = []
        return self._listings[key]

    def resolve(self, rel: str):
        """Case-sensitive lookup against real directory listings.

        Returns (status, actual_relative_path): ok, case (exists with other case) or missing."""
        parts = [p for p in rel.split("/") if p]
        actual = []
        case_differs = False
        for part in parts:
            entries = self.listing("/".join(actual))
            norm = unicodedata.normalize("NFC", part)
            exact = [e for e in entries if unicodedata.normalize("NFC", e) == norm]
            if exact:
                actual.append(exact[0])
                continue
            folded = [e for e in entries if unicodedata.normalize("NFC", e).lower() == norm.lower()]
            if folded:
                actual.append(sorted(folded)[0])
                case_differs = True
                continue
            return "missing", rel
        return ("case" if case_differs else "ok"), "/".join(actual)

    def file_index(self) -> list:
        if self._index is None:
            self._index = []
            for rel in self.walk():
                self._index.append(rel)
                if len(self._index) >= MAX_INDEX_FILES:
                    break
        return self._index

    def read(self, rel: str) -> str:
        try:
            path = os.path.join(self.root, rel)
            if os.path.getsize(path) > MAX_FILE_BYTES:
                return ""
            with open(path, "r", encoding="utf-8-sig", errors="replace") as handle:
                return handle.read()
        except OSError:
            return ""

    def anchors(self, rel: str):
        """Anchors of a Markdown file, or None when anchors cannot be checked for that file type."""
        if os.path.splitext(rel)[1].lower() not in MD_EXT:
            return None
        if rel not in self._anchors:
            self._anchors[rel] = collect_anchors(self.read(rel))
        return self._anchors[rel]

    def suggest_file(self, missing_rel: str, from_dir: str):
        """Closest existing file by name: the same file moved, or a similarly named one."""
        base = posixpath.basename(missing_rel.rstrip("/"))
        if not base:
            return None
        index = [p for p in self.file_index()]
        same = [p for p in index if posixpath.basename(p).lower() == base.lower()]
        if same:
            same.sort(key=lambda p: -difflib.SequenceMatcher(None, p.lower(), missing_rel.lower()).ratio())
            return posixpath.relpath(same[0], from_dir or ".")
        stems = {}
        for p in index:
            stems.setdefault(posixpath.basename(p).lower(), []).append(p)
        near = difflib.get_close_matches(base.lower(), sorted(stems), n=1, cutoff=0.7)
        if near:
            options = sorted(stems[near[0]], key=lambda p: -difflib.SequenceMatcher(None, p.lower(), missing_rel.lower()).ratio())
            return posixpath.relpath(options[0], from_dir or ".")
        return None


def is_localhost(host: str) -> bool:
    host = host.lower().strip("[]")
    if host in ("localhost", "0.0.0.0", "::1", "::") or host.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_loopback or ip.is_unspecified
    except ValueError:
        return False


def http_is_expected(host: str) -> bool:
    """True for hosts where plain http:// is normal (local, private, reserved example domains)."""
    host = host.lower().strip("[]")
    if is_localhost(host) or host.endswith((".local", ".test", ".internal", ".invalid", ".lan")):
        return True
    if host in ("example.com", "example.org", "example.net") or host.endswith((".example.com", ".example.org", ".example.net")):
        return True
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def split_target(target: str):
    """Split a local link target into (path, fragment), decoding %xx and HTML entities."""
    target = html.unescape(target.strip())
    path, _, fragment = target.partition("#")
    path = path.partition("?")[0]
    return urllib.parse.unquote(path), urllib.parse.unquote(fragment)


# --------------------------------------------------------------------------------------
# The check
# --------------------------------------------------------------------------------------

def make_finding(rule: str, file: str, line: int, message: str, target: str = "", suggestion: str = "",
                 section: str = "", first_run: bool = False, severity: str = "") -> dict:
    return {"rule": rule, "severity": severity or RULES[rule][0], "file": file, "line": line,
            "message": message, "target": target, "suggestion": suggestion,
            "section": section, "first_run": first_run}


def section_for(headings: list, line: int):
    """Nearest heading at or above `line`, and whether it looks like an install/quick-start section."""
    current = ""
    for number, text in headings:
        if number <= line:
            current = text
        else:
            break
    clean = heading_text_to_slug_source(current).strip()
    return clean, bool(FIRST_RUN_RE.search(clean))


def check_documents(project: Project, files: list, check_orphans: bool = True, entries=()):
    """Run every offline check. Returns (findings, external_urls, stats)."""
    findings = []
    external = []            # (url, file, line, section, first_run)
    linked = set()
    total = {"links": 0, "local": 0, "external": 0}
    doc_set = set(files)
    for rel in files:
        text = project.read(rel)
        ext = os.path.splitext(rel)[1].lower()
        if ext in MD_EXT:
            links, headings, undefined = extract_markdown(text)
        elif ext == ".rst":
            links, headings, undefined = extract_rst(text)
        else:
            links, headings, undefined = extract_adoc(text)
        for line, label in undefined:
            section, first = section_for(headings, line)
            findings.append(make_finding("LK010", rel, line, f"reference label [{label}] is not defined",
                                         label, "add `[%s]: <url>` or remove the brackets" % label, section, first))
        for link in links:
            total["links"] += 1
            section, first = section_for(headings, link.line)
            ctx = (rel, link, section, first)
            target = link.target.strip()
            if re.match(r"^(mailto|tel|sms|javascript|data|irc|ftp):", target, re.I):
                continue
            if re.match(r"^(https?:)?//", target, re.I):
                total["external"] += 1
                handle_external(target, ctx, findings, external)
                continue
            total["local"] += 1
            check_local(project, ctx, findings, linked, doc_set)
    if check_orphans and len(files) > 1:
        findings.extend(find_orphans(project, files, linked, entries))
    return findings, external, total


def handle_external(target: str, ctx, findings: list, external: list) -> None:
    rel, link, section, first = ctx
    url = target if not target.startswith("//") else "https:" + target
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    if PLACEHOLDER_RE.search(target):
        findings.append(make_finding("LK008", rel, link.line, "placeholder in URL", target,
                                     "replace it with the real address or remove the link", section, first))
        return
    if is_localhost(host):
        findings.append(make_finding("LK007", rel, link.line, "link points at this machine", target,
                                     "fine for a dev-server hint; otherwise link to the published address",
                                     section, first))
        return
    if parts.scheme == "http" and not http_is_expected(host):
        findings.append(make_finding("LK006", rel, link.line, "plain http link", target,
                                     "use `https://` if the site serves it", section, first))
    external.append((url, rel, link.line, section, first))


def check_local(project: Project, ctx, findings: list, linked: set, doc_set: set) -> None:
    rel, link, section, first = ctx
    target = link.target.strip()
    if target.startswith("<") and target.endswith(">"):
        target = target[1:-1].strip()
    here = posixpath.dirname(rel)
    kind_rule = "LK004" if link.is_image else "LK001"
    if not target:
        findings.append(make_finding("LK005", rel, link.line, "link has no target", "",
                                     "add the target or remove the link", section, first))
        return
    if target.lower() in PLACEHOLDER_EXACT or PLACEHOLDER_RE.search(target):
        findings.append(make_finding("LK008", rel, link.line, "placeholder link target", target,
                                     "point it at the real page or remove it", section, first))
        return
    path, fragment = split_target(target)
    if not path:                       # same-file anchor
        target_rel, status = rel, "ok"
    else:
        base = "" if path.startswith("/") else here
        joined = posixpath.normpath(posixpath.join(base, path.lstrip("/")))
        if joined == ".":
            joined = ""
        if joined == ".." or joined.startswith("../"):
            findings.append(make_finding("LK011", rel, link.line, "link climbs above the project root", target,
                                         "GitHub treats this as a web route; use an absolute URL if that is the intent",
                                         section, first))
            return
        status, actual = project.resolve(joined)
        target_rel = actual
        if status == "missing":
            fixed = path.replace("\\", "/")
            if fixed != path and project.resolve(posixpath.normpath(posixpath.join(base, fixed.lstrip("/"))))[0] == "ok":
                findings.append(make_finding(kind_rule, rel, link.line, f"file not found: `{path}`", target,
                                             f"use forward slashes: `{fixed}`", section, first))
                return
            suggestion = project.suggest_file(joined, here)
            hint = f"did you mean `{suggestion}`?" if suggestion else "no similar file found; remove or re-point the link"
            noun = "image" if link.is_image else "file"
            findings.append(make_finding(kind_rule, rel, link.line, f"{noun} not found: `{path}`", target, hint,
                                         section, first))
            return
        if status == "case":
            shown = posixpath.relpath(actual, here or ".")
            findings.append(make_finding("LK002", rel, link.line,
                                         f"letter case differs from the file on disk: `{path}`", target,
                                         f"use `{shown}`", section, first))
        if target_rel != rel:
            linked.add(target_rel)
            if status != "missing" and os.path.isdir(os.path.join(project.root, target_rel)):
                linked.add(target_rel.rstrip("/") + "/")
    if fragment and not LINE_ANCHOR_RE.match(fragment) and not fragment.startswith(":~:"):
        full = os.path.join(project.root, target_rel)
        if os.path.isfile(full):
            anchors = project.anchors(target_rel)
            if anchors is not None and fragment not in anchors and fragment.lower() not in {a.lower() for a in anchors}:
                near = difflib.get_close_matches(fragment.lower(), sorted(anchors), n=1, cutoff=0.6)
                hint = f"did you mean `#{near[0]}`?" if near else "check the heading text; GitHub drops punctuation"
                where = "this file" if target_rel == rel else f"`{target_rel}`"
                findings.append(make_finding("LK003", rel, link.line, f"anchor `#{fragment}` not found in {where}",
                                             target, hint, section, first))


def is_entry_point(rel: str, entries) -> bool:
    base = posixpath.basename(rel).lower()
    stem = posixpath.splitext(base)[0]
    if stem in ENTRY_STEMS or any(base.startswith(name) or stem.startswith(name) for name in ENTRY_NAMES):
        return True
    parts = rel.split("/")
    if len(parts) > 1 and parts[0] in (".github", ".claude", ".claude-plugin", ".gitlab", "agents", "commands", "hooks"):
        return True
    return any(fnmatch.fnmatch(rel, pat) for pat in entries)


def find_orphans(project: Project, files: list, linked: set, entries) -> list:
    nav_text = ""
    for name in NAV_FILES:
        if os.path.isfile(os.path.join(project.root, name)):
            nav_text += project.read(name) + "\n"
    out = []
    folders = [l for l in linked if l.endswith("/")]
    for rel in files:
        if os.path.splitext(rel)[1].lower() not in DOC_EXT or rel in linked or is_entry_point(rel, entries):
            continue
        if any(rel.startswith(folder) for folder in folders):
            continue
        short = rel.split("/", 1)[1] if rel.startswith("docs/") else rel
        if nav_text and (rel in nav_text or short in nav_text):
            continue
        out.append(make_finding("LK009", rel, 1, "no other doc links to this file", rel,
                                "link it from the README or an index page, or delete it if it is dead"))
    return out


# --------------------------------------------------------------------------------------
# Online checks (opt-in)
# --------------------------------------------------------------------------------------

def default_resolver(host: str) -> list:
    return sorted({info[4][0] for info in socket.getaddrinfo(host, None)})


def host_is_public(host: str, resolver=default_resolver):
    """Return (ok, reason). Refuses loopback, private, link-local, reserved and unresolvable hosts."""
    host = (host or "").strip("[]")
    if not host:
        return False, "no host"
    if is_localhost(host):
        return False, "loopback address"
    try:
        addresses = [str(ipaddress.ip_address(host))]
    except ValueError:
        try:
            addresses = list(resolver(host))
        except (OSError, UnicodeError) as exc:
            return False, f"cannot resolve ({exc.__class__.__name__})"
    if not addresses:
        return False, "cannot resolve"
    for text in addresses:
        try:
            ip = ipaddress.ip_address(text.split("%")[0])
        except ValueError:
            return False, "unparseable address"
        mapped = getattr(ip, "ipv4_mapped", None)
        if mapped is not None:
            ip = mapped
        if not ip.is_global or ip.is_multicast:
            return False, f"non-public address {ip}"
    return True, ""


class GuardedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow redirects only to public http(s) hosts."""

    def __init__(self, resolver=default_resolver):
        self.resolver = resolver

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parts = urllib.parse.urlsplit(newurl)
        if parts.scheme not in ("http", "https"):
            raise urllib.error.URLError("redirect to a non-http scheme refused")
        ok, reason = host_is_public(parts.hostname or "", self.resolver)
        if not ok:
            raise urllib.error.URLError(f"redirect refused: {reason}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


UNVERIFIABLE = {401, 403, 405, 406, 429, 451, 999}


class Fetcher:
    """HEAD-then-GET URL checker with a per-host rate limit and a request cap."""

    def __init__(self, opener=None, resolver=default_resolver, sleep=time.sleep, clock=time.monotonic,
                 timeout: float = 8.0, delay: float = 0.5, max_requests: int = 50):
        self.opener = opener or urllib.request.build_opener(GuardedRedirectHandler(resolver))
        self.resolver = resolver
        self.sleep = sleep
        self.clock = clock
        self.timeout = timeout
        self.delay = delay
        self.max_requests = max_requests
        self.requests = 0
        self._last = {}

    def _throttle(self, host: str) -> None:
        last = self._last.get(host)
        now = self.clock()
        if last is not None and now - last < self.delay:
            self.sleep(self.delay - (now - last))
            now = self.clock()
        self._last[host] = now

    def _request(self, url: str, method: str):
        req = urllib.request.Request(url, method=method, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
        self.requests += 1
        try:
            resp = self.opener.open(req, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            return exc.code
        try:
            code = getattr(resp, "status", None) or resp.getcode()
            if method == "GET":
                resp.read(1)
            return code
        finally:
            close = getattr(resp, "close", None)
            if close:
                close()

    def check(self, url: str) -> dict:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("http", "https") or parts.username or parts.password:
            return {"outcome": "refused", "status": None, "detail": "only plain http(s) URLs without credentials"}
        ok, reason = host_is_public(parts.hostname or "", self.resolver)
        if not ok:
            return {"outcome": "refused", "status": None, "detail": reason}
        host = (parts.hostname or "").lower()
        try:
            self._throttle(host)
            code = self._request(url, "HEAD")
            if code >= 400:
                self._throttle(host)
                code = self._request(url, "GET")
        except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as exc:
            reason = getattr(exc, "reason", exc)
            return {"outcome": "unreachable", "status": None, "detail": str(reason)[:80]}
        if code < 400:
            return {"outcome": "ok", "status": code, "detail": ""}
        if code in UNVERIFIABLE:
            return {"outcome": "unverified", "status": code, "detail": ""}
        if code >= 500:
            return {"outcome": "server", "status": code, "detail": ""}
        return {"outcome": "dead", "status": code, "detail": ""}


def check_online(external: list, fetcher: Fetcher):
    """Check each distinct URL once; return (findings, stats)."""
    findings = []
    results = {}
    order = []
    for url, *_rest in external:
        if url not in order:
            order.append(url)
    skipped = 0
    for url in order:
        if fetcher.requests >= fetcher.max_requests:
            skipped += 1
            continue
        results[url] = fetcher.check(url)
    for url, rel, line, section, first in external:
        res = results.get(url)
        if res is None or res["outcome"] == "ok":
            continue
        outcome, status = res["outcome"], res["status"]
        if outcome == "dead":
            findings.append(make_finding("LK012", rel, line, f"HTTP {status}", url,
                                         "find the page's new address, link the Wayback Machine copy, or remove it",
                                         section, first))
        elif outcome == "server":
            findings.append(make_finding("LK013", rel, line, f"server error HTTP {status} (may be temporary)", url,
                                         "re-run later before changing the doc", section, first))
        elif outcome == "unverified":
            findings.append(make_finding("LK013", rel, line, f"HTTP {status}: the site blocks automated checks",
                                         url, "open it in a browser to confirm", section, first, severity="info"))
        elif outcome == "unreachable":
            findings.append(make_finding("LK013", rel, line, f"could not connect ({res['detail']})", url,
                                         "re-run later; if it persists the host is gone", section, first))
        else:
            findings.append(make_finding("LK014", rel, line, f"not requested: {res['detail']}", url,
                                         "the guard never contacts private or local addresses", section, first))
    stats = {"urls": len(order), "requested": fetcher.requests, "skipped_by_cap": skipped}
    return findings, stats


# --------------------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------------------

def sort_findings(findings: list) -> list:
    return sorted(findings, key=lambda f: (SEV_RANK[f["severity"]], f["file"], f["line"], f["rule"], f["target"]))


def counts(findings: list) -> dict:
    return {sev: sum(1 for f in findings if f["severity"] == sev) for sev in SEVERITIES}


def cell(text: str) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def render_markdown(report: dict) -> str:
    s = report["summary"]
    c = s["by_severity"]
    lines = [f"Scanned {s['files']} doc file(s) and {s['links']} link(s) "
             f"({s['local_links']} local, {s['external_links']} external). "
             f"{s['findings']} finding(s): {c['high']} high, {c['medium']} medium, {c['low']} low, {c['info']} info.", ""]
    online = report["online"]
    if online["enabled"]:
        lines.append(f"Online check: {online['requested']} request(s) for {online['urls']} URL(s), "
                     f"{online['skipped_by_cap']} not checked because of the request cap.")
    else:
        lines.append("Offline run: no network requests were made (use --online to check external links).")
    lines.append("")
    if not report["findings"]:
        lines.append("No findings.")
        return "\n".join(lines) + "\n"
    by_rule = {}
    for f in report["findings"]:
        by_rule[f["rule"]] = by_rule.get(f["rule"], 0) + 1
    lines += ["| Rule | Severity | Count | Meaning |", "|---|---|---:|---|"]
    for rule in sorted(by_rule):
        lines.append(f"| {rule} | {RULES[rule][0]} | {by_rule[rule]} | {RULES[rule][1]} |")
    lines += ["", "| Location | Rule | Severity | Section | Problem | Suggestion |", "|---|---|---|---|---|---|"]
    for f in report["findings"]:
        section = f["section"] + (" (first run)" if f["first_run"] else "") if f["section"] else ""
        lines.append(f"| `{f['file']}:{f['line']}` | {f['rule']} | {f['severity']} | {cell(section)} | "
                     f"{cell(f['message'])} | {cell(f['suggestion'])} |")
    return "\n".join(lines) + "\n"


def build_report(project: Project, findings: list, total: dict, files: list, online: dict) -> dict:
    ordered = sort_findings(findings)
    return {
        "tool": "link_check",
        "summary": {"files": len(files), "links": total["links"], "local_links": total["local"],
                    "external_links": total["external"], "findings": len(ordered),
                    "by_severity": counts(ordered)},
        "online": online,
        "findings": ordered,
    }


def should_fail(findings: list, fail_on: str) -> bool:
    if fail_on == "never":
        return False
    limit = SEV_RANK[fail_on]
    return any(SEV_RANK[f["severity"]] <= limit for f in findings)


def main(argv=None, fetcher=None) -> int:
    parser = argparse.ArgumentParser(
        prog="link_check.py",
        description="Find broken links, images and anchors in Markdown, reStructuredText and AsciiDoc.")
    parser.add_argument("path", nargs="?", default=".", help="project folder or a single doc file (default: .)")
    parser.add_argument("--root", help="project root for /absolute links and orphan detection "
                                       "(default: the folder, or the file's folder)")
    parser.add_argument("--json", action="store_true", help="print JSON instead of Markdown")
    parser.add_argument("--fail-on", default="never", choices=["high", "medium", "low", "never"],
                        help="exit 1 when a finding of this severity or worse exists (default: never)")
    parser.add_argument("--exclude", action="append", default=[], metavar="GLOB", help="skip paths matching GLOB")
    parser.add_argument("--no-orphans", action="store_true", help="skip the orphaned-doc check")
    parser.add_argument("--entry", action="append", default=[], metavar="GLOB",
                        help="treat docs matching GLOB as entry points, never orphans")
    parser.add_argument("--online", action="store_true",
                        help="also check external http(s) links over the network (off by default)")
    parser.add_argument("--max-requests", type=int, default=50, help="request cap for --online (default 50)")
    parser.add_argument("--timeout", type=float, default=8.0, help="seconds per request (default 8)")
    parser.add_argument("--delay", type=float, default=0.5, help="seconds between requests to one host (default 0.5)")
    args = parser.parse_args(argv)

    target = os.path.abspath(args.path)
    if not os.path.exists(target):
        print(f"error: {args.path} does not exist", file=sys.stderr)
        return 2
    if args.max_requests < 0 or args.timeout <= 0 or args.delay < 0:
        print("error: --max-requests, --timeout and --delay must not be negative (timeout above zero)", file=sys.stderr)
        return 2
    single = os.path.isfile(target)
    root = os.path.abspath(args.root) if args.root else (os.path.dirname(target) if single else target)
    if not os.path.isdir(root):
        print(f"error: root {root} is not a folder", file=sys.stderr)
        return 2
    project = Project(root, args.exclude)
    if single:
        files = [os.path.relpath(target, root).replace(os.sep, "/")]
        if files[0].startswith("..") or os.path.splitext(files[0])[1].lower() not in DOC_EXT:
            print("error: the file must be a .md, .markdown, .rst or .adoc file inside the root", file=sys.stderr)
            return 2
    else:
        files = project.doc_files()
    if not files:
        print(f"error: no .md, .rst or .adoc files found under {args.path}", file=sys.stderr)
        return 2
    findings, external, total = check_documents(project, files, not (args.no_orphans or single), args.entry)
    online = {"enabled": False, "urls": 0, "requested": 0, "skipped_by_cap": 0}
    if args.online:
        fetcher = fetcher or Fetcher(timeout=args.timeout, delay=args.delay, max_requests=args.max_requests)
        extra, stats = check_online(external, fetcher)
        findings.extend(extra)
        online = dict(stats, enabled=True)
    report = build_report(project, findings, total, files, online)
    if args.json:
        sys.stdout.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
    else:
        sys.stdout.write(render_markdown(report))
    return 1 if should_fail(report["findings"], args.fail_on) else 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    sys.exit(main())
