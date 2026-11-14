"""Tests for skills/doctruth-links/link_check.py. No test touches the network."""
import io
import json
import socket
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from _load import ROOT, load, write

lc = load("doctruth-links", "link_check")
SCRIPT = ROOT / "skills" / "doctruth-links" / "link_check.py"


def forbid_network():
    """Patch every way the script could reach the network so that any attempt fails the test."""
    def boom(*args, **kwargs):
        raise AssertionError("network access attempted")
    return [
        mock.patch.object(socket, "getaddrinfo", boom),
        mock.patch.object(socket.socket, "connect", boom),
        mock.patch.object(urllib.request.OpenerDirector, "open", boom),
        mock.patch.object(urllib.request, "urlopen", boom),
    ]


class NoNetworkMixin:
    def setUp(self):
        self._patches = forbid_network()
        for patch in self._patches:
            patch.start()
        self.addCleanup(lambda: [p.stop() for p in self._patches])


def run(argv, **kwargs):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = lc.main(argv, **kwargs)
    return code, out.getvalue(), err.getvalue()


def scan(tree: dict, **options):
    """Write `tree` (relative path -> text) to a temp folder and return the findings list."""
    with tempfile.TemporaryDirectory() as tmp:
        for rel, text in tree.items():
            write(Path(tmp) / rel, text)
        argv = [tmp, "--json"]
        for key, value in options.items():
            argv += ["--" + key.replace("_", "-")] + ([] if value is True else [str(value)])
        code, out, _err = run(argv)
        return json.loads(out)["findings"]


def rules(findings):
    return sorted(f["rule"] for f in findings)


class SlugTests(unittest.TestCase):
    def test_github_slug_rules(self):
        cases = {
            "Getting Started": "getting-started",
            "What's new?": "whats-new",
            "C++ & Rust": "c--rust",
            "`code` in heading": "code-in-heading",
            "Under_score and-dash": "under_score-and-dash",
            "[Link text](http://x.example) here": "link-text-here",
            "Emoji :rocket: time": "emoji--time",
            "**Bold** and _em_": "bold-and-em",
            "Über uns": "über-uns",
            "1. Step one": "1-step-one",
        }
        for heading, slug in cases.items():
            with self.subTest(heading=heading):
                self.assertEqual(lc.github_slug(heading), slug)

    def test_duplicate_headings_get_numeric_suffixes(self):
        anchors = lc.collect_anchors("# Intro\n\n## Usage\n\n## Usage\n\n## Usage\n")
        self.assertEqual(anchors, {"intro", "usage", "usage-1", "usage-2"})

    def test_setext_atx_closing_hashes_and_explicit_ids(self):
        text = "Title\n=====\n\nSub\n---\n\n## Closed ##\n\n### Custom {#my-id}\n"
        anchors = lc.collect_anchors(text)
        self.assertTrue({"title", "sub", "closed", "my-id"} <= anchors)

    def test_headings_inside_fences_and_front_matter_are_ignored(self):
        text = "---\ntitle: x\n---\n# Real\n```\n# not a heading\n```\n"
        self.assertEqual(lc.collect_anchors(text), {"real"})

    def test_html_anchors_count(self):
        anchors = lc.collect_anchors('# A\n<a name="legacy"></a>\n<div id="box"></div>\n')
        self.assertTrue({"a", "legacy", "box"} <= anchors)


class ExtractionTests(unittest.TestCase):
    def extract(self, text):
        links, _headings, undefined = lc.extract_markdown(text)
        return [(l.kind, l.target) for l in links], undefined

    def test_inline_links_and_images(self):
        found, _ = self.extract("[a](docs/a.md) and ![pic](img/p.png \"title\") and [b](<my file.md>)")
        self.assertEqual(sorted(found), [("image", "img/p.png"), ("link", "docs/a.md"), ("link", "my file.md")])

    def test_nested_badge_image_inside_link(self):
        found, _ = self.extract("[![build](https://ci.example/b.svg)](https://ci.example/run)")
        self.assertEqual(sorted(found), [("image", "https://ci.example/b.svg"), ("link", "https://ci.example/run")])

    def test_parentheses_in_target(self):
        found, _ = self.extract("[x](https://en.example/wiki/Foo_(bar)) text")
        self.assertEqual(found, [("link", "https://en.example/wiki/Foo_(bar)")])

    def test_code_fences_inline_code_and_comments_are_skipped(self):
        text = "`[a](no1.md)`\n```\n[b](no2.md)\n```\n~~~\n[c](no3.md)\n~~~\n<!-- [d](no4.md) -->\n[e](yes.md)\n"
        found, _ = self.extract(text)
        self.assertEqual(found, [("link", "yes.md")])

    def test_longer_fence_is_not_closed_by_shorter(self):
        text = "````\n```\n[a](no.md)\n```\n````\n[b](yes.md)\n"
        found, _ = self.extract(text)
        self.assertEqual(found, [("link", "yes.md")])

    def test_reference_style(self):
        found, undefined = self.extract("See [the guide][g] and [g][] and [missing][nope] and ![logo][lg].\n\n"
                                        "[g]: docs/guide.md\n[lg]: img/logo.png \"Logo\"\n")
        self.assertIn(("ref", "docs/guide.md"), found)
        self.assertIn(("image", "img/logo.png"), found)
        self.assertEqual(undefined, [(1, "nope")])

    def test_array_indexing_is_not_a_reference(self):
        _found, undefined = self.extract("the value matrix[i][j] and - [ ] task and [x] done")
        self.assertEqual(undefined, [])

    def test_autolinks_html_and_bare_urls(self):
        text = '<https://a.example/x> <a href="docs/b.md">b</a> <img src="img/c.png"> see https://d.example/y.\n'
        found, _ = self.extract(text)
        self.assertIn(("autolink", "https://a.example/x"), found)
        self.assertIn(("html", "docs/b.md"), found)
        self.assertIn(("image", "img/c.png"), found)
        self.assertIn(("url", "https://d.example/y"), found)

    def test_bare_url_in_parentheses_drops_the_closing_one(self):
        found, _ = self.extract("(see https://d.example/y) and (https://e.example/a_(b))")
        self.assertIn(("url", "https://d.example/y"), found)
        self.assertIn(("url", "https://e.example/a_(b)"), found)

    def test_rst_and_adoc(self):
        rst, _h, _u = lc.extract_rst("See `Guide <docs/guide.rst>`_.\n.. image:: img/a.png\n.. _site: https://s.example\n")
        self.assertEqual([(l.kind, l.target) for l in rst],
                         [("link", "docs/guide.rst"), ("image", "img/a.png"), ("link", "https://s.example")])
        adoc, _h, _u = lc.extract_adoc("link:docs/a.adoc[A]\nimage::img/b.png[B]\n// link:skip.adoc[x]\n")
        self.assertEqual([(l.kind, l.target) for l in adoc], [("link", "docs/a.adoc"), ("image", "img/b.png")])


class OfflineChecksTests(NoNetworkMixin, unittest.TestCase):
    def test_broken_relative_link_with_did_you_mean(self):
        findings = scan({"README.md": "[x](docs/setup-guide.md)\n", "docs/setup_guide.md": "# S\n"})
        broken = [f for f in findings if f["rule"] == "LK001"]
        self.assertEqual(len(broken), 1)
        self.assertIn("did you mean `docs/setup_guide.md`", broken[0]["suggestion"])

    def test_moved_file_found_by_basename(self):
        findings = scan({"README.md": "[x](usage.md)\n", "docs/guides/usage.md": "# U\n"})
        self.assertIn("docs/guides/usage.md", findings[0]["suggestion"])

    def test_case_mismatch_uses_real_directory_listing(self):
        findings = scan({"README.md": "[x](Docs/Usage.md)\n", "docs/usage.md": "# U\n"})
        self.assertEqual(rules(findings), ["LK002"])
        self.assertIn("docs/usage.md", findings[0]["suggestion"])

    def test_existing_link_is_clean(self):
        findings = scan({"README.md": "[x](docs/usage.md) [d](docs/) [r](/docs/usage.md)\n", "docs/usage.md": "# U\n"})
        self.assertEqual(findings, [])

    def test_percent_encoding_query_and_backslash(self):
        tree = {"README.md": "[a](my%20file.md) [b](docs/usage.md?plain=1) [c](docs\\usage.md)\n",
                "my file.md": "# M\n", "docs/usage.md": "# U\n"}
        findings = scan(tree)
        self.assertEqual(rules(findings), ["LK001"])
        self.assertIn("forward slashes", findings[0]["suggestion"])

    def test_missing_image(self):
        findings = scan({"README.md": "![logo](img/logo.png)\n"})
        self.assertEqual(rules(findings), ["LK004"])

    def test_html_image_and_link(self):
        findings = scan({"README.md": '<img src="a.png"> <a href="b.md">b</a>\n'})
        self.assertEqual(rules(findings), ["LK001", "LK004"])

    def test_broken_anchor_in_other_file_and_same_file(self):
        tree = {"README.md": "# Top\n[a](docs/u.md#hooks) [b](docs/u.md#hook) [c](#top) [d](#tpo)\n",
                "docs/u.md": "# U\n## Hooks\n"}
        findings = scan(tree)
        self.assertEqual(rules(findings), ["LK003", "LK003"])
        hints = sorted(f["suggestion"] for f in findings)
        self.assertEqual(hints, ["did you mean `#hooks`?", "did you mean `#top`?"])

    def test_anchor_case_is_lenient_and_line_anchors_skipped(self):
        tree = {"README.md": "[a](docs/u.md#HOOKS) [b](src/x.py#L10-L20) [c](docs/u.md#:~:text=foo)\n",
                "docs/u.md": "# U\n## Hooks\n", "src/x.py": "x = 1\n"}
        self.assertEqual(scan(tree), [])

    def test_anchor_on_non_markdown_target_is_not_checked(self):
        self.assertEqual(scan({"README.md": "[a](page.html#whatever)\n", "page.html": "<p>x</p>"}), [])

    def test_duplicate_heading_anchor_suffix(self):
        tree = {"README.md": "[a](#usage-1) [b](#usage-2)\n\n## Usage\n\n## Usage\n"}
        self.assertEqual([f["target"] for f in scan(tree)], ["#usage-2"])

    def test_empty_placeholder_localhost_and_http(self):
        text = ("[a]() [b](TODO) [c](#) [d](https://github.com/TODO/repo) [e](http://localhost:3000/x) "
                "[f](http://docs.somewhere.org/a) [g](http://example.com/ok) [h](http://10.0.0.5/ok) "
                "[i](https://github.com/<user>/repo) [j](https://x.org/?q=TODO)\n")
        findings = scan({"README.md": text})
        self.assertEqual(rules(findings), ["LK005", "LK006", "LK007", "LK008", "LK008", "LK008", "LK008"])
        http_hit = [f for f in findings if f["rule"] == "LK006"][0]
        self.assertEqual(http_hit["target"], "http://docs.somewhere.org/a")

    def test_mailto_and_tel_are_ignored(self):
        self.assertEqual(scan({"README.md": "[m](mailto:a@b.example) [t](tel:123)\n"}), [])

    def test_link_above_root(self):
        findings = scan({"README.md": "[x](../../issues)\n"})
        self.assertEqual(rules(findings), ["LK011"])

    def test_undefined_reference_label(self):
        findings = scan({"README.md": "see [the docs][docs]\n"})
        self.assertEqual(rules(findings), ["LK010"])

    def test_findings_carry_section_and_first_run_flag(self):
        text = "# Proj\n\n## Installation\n\n[x](gone.md)\n\n## Changelog\n\n[y](gone2.md)\n"
        findings = {f["target"]: f for f in scan({"README.md": text})}
        self.assertTrue(findings["gone.md"]["first_run"])
        self.assertEqual(findings["gone.md"]["section"], "Installation")
        self.assertFalse(findings["gone2.md"]["first_run"])

    def test_rst_and_adoc_files_are_checked(self):
        tree = {"index.rst": "See `Guide <missing.rst>`_.\n", "a.adoc": "link:gone.adoc[G]\n"}
        self.assertEqual([f["rule"] for f in scan(tree) if f["rule"] == "LK001"], ["LK001", "LK001"])

    def test_exclude_glob(self):
        tree = {"README.md": "ok\n", "vendor_docs/a.md": "[x](gone.md)\n"}
        self.assertEqual(scan(tree, exclude="vendor_docs/*"), [])


class OrphanTests(NoNetworkMixin, unittest.TestCase):
    def test_orphan_is_reported_and_linked_doc_is_not(self):
        tree = {"README.md": "[a](docs/a.md)\n", "docs/a.md": "# A\n", "docs/b.md": "# B\n"}
        orphans = [f["file"] for f in scan(tree) if f["rule"] == "LK009"]
        self.assertEqual(orphans, ["docs/b.md"])

    def test_entry_points_are_never_orphans(self):
        tree = {"README.md": "x\n", "CHANGELOG.md": "x\n", "CONTRIBUTING.md": "x\n", "docs/index.md": "x\n",
                "skills/a/SKILL.md": "x\n", ".github/PULL_REQUEST_TEMPLATE.md": "x\n"}
        self.assertEqual(scan(tree), [])

    def test_nav_config_counts_as_a_link(self):
        tree = {"README.md": "x\n", "docs/a.md": "# A\n", "mkdocs.yml": "nav:\n  - A: a.md\n"}
        self.assertEqual(scan(tree), [])

    def test_no_orphans_flag_and_entry_option(self):
        tree = {"README.md": "x\n", "docs/b.md": "# B\n"}
        self.assertEqual(scan(tree, no_orphans=True), [])
        self.assertEqual(scan(tree, entry="docs/*.md"), [])

    def test_link_to_a_folder_counts_for_the_docs_inside_it(self):
        tree = {"README.md": "See [reports](examples/output/).\n", "examples/output/a.md": "# A\n",
                "examples/output/deep/b.md": "# B\n", "examples/other/c.md": "# C\n"}
        self.assertEqual([f["file"] for f in scan(tree) if f["rule"] == "LK009"], ["examples/other/c.md"])

    def test_agent_and_command_folders_are_entry_points(self):
        tree = {"README.md": "x\n", "agents/helper.md": "# H\n", "commands/run.md": "# R\n"}
        self.assertEqual(scan(tree), [])

    def test_case_mismatched_link_still_counts_as_linking(self):
        tree = {"README.md": "[a](Docs/A.md)\n", "docs/a.md": "# A\n"}
        self.assertEqual(rules(scan(tree)), ["LK002"])

    def test_single_file_run_skips_orphans(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(Path(tmp) / "README.md", "[x](gone.md)\n")
            write(Path(tmp) / "docs" / "b.md", "# B\n")
            code, out, _ = run([str(Path(tmp) / "README.md"), "--json"])
            self.assertEqual(rules(json.loads(out)["findings"]), ["LK001"])


class HostGuardTests(unittest.TestCase):
    def resolver(self, mapping):
        def resolve(host):
            if host not in mapping:
                raise socket.gaierror("no such host")
            return mapping[host]
        return resolve

    def test_refuses_private_loopback_link_local_and_reserved(self):
        for host in ("10.1.2.3", "127.0.0.1", "169.254.169.254", "192.168.0.9", "172.16.0.1", "0.0.0.0",
                     "100.64.0.1", "224.0.0.1", "::1", "fe80::1", "fc00::1", "::ffff:127.0.0.1", "localhost"):
            with self.subTest(host=host):
                ok, _reason = lc.host_is_public(host, self.resolver({}))
                self.assertFalse(ok)

    def test_hostname_resolving_to_a_private_address_is_refused(self):
        ok, reason = lc.host_is_public("internal.example", self.resolver({"internal.example": ["10.0.0.7"]}))
        self.assertFalse(ok)
        self.assertIn("10.0.0.7", reason)

    def test_mixed_answers_are_refused(self):
        ok, _ = lc.host_is_public("x.example", self.resolver({"x.example": ["93.184.216.34", "127.0.0.1"]}))
        self.assertFalse(ok)

    def test_public_address_is_allowed(self):
        ok, _ = lc.host_is_public("x.example", self.resolver({"x.example": ["93.184.216.34"]}))
        self.assertTrue(ok)
        self.assertTrue(lc.host_is_public("93.184.216.34", self.resolver({}))[0])

    def test_unresolvable_host_is_refused(self):
        self.assertFalse(lc.host_is_public("nowhere.example", self.resolver({}))[0])

    def test_redirect_to_private_address_is_refused(self):
        handler = lc.GuardedRedirectHandler(self.resolver({"good.example": ["93.184.216.34"]}))
        req = urllib.request.Request("https://good.example/a")
        with self.assertRaises(urllib.error.URLError):
            handler.redirect_request(req, None, 302, "Found", {}, "http://127.0.0.1/admin")
        with self.assertRaises(urllib.error.URLError):
            handler.redirect_request(req, None, 302, "Found", {}, "file:///etc/passwd")
        new = handler.redirect_request(req, io.BytesIO(b""), 302, "Found", {}, "https://good.example/b")
        self.assertEqual(new.full_url, "https://good.example/b")


class FakeResponse:
    def __init__(self, status):
        self.status = status
        self.closed = False

    def read(self, n=-1):
        return b""

    def close(self):
        self.closed = True


class FakeOpener:
    """Stands in for urllib's opener: `plan` maps (METHOD, url) to a status, an exception or a callable."""

    def __init__(self, plan, default=200):
        self.plan = plan
        self.default = default
        self.calls = []

    def open(self, request, timeout=None):
        self.calls.append((request.get_method(), request.full_url, timeout))
        step = self.plan.get((request.get_method(), request.full_url), self.plan.get(request.full_url, self.default))
        if isinstance(step, Exception):
            raise step
        if step >= 400:
            raise urllib.error.HTTPError(request.full_url, step, "err", {}, None)
        return FakeResponse(step)


PUBLIC = lambda host: ["93.184.216.34"]  # noqa: E731


class FetcherTests(unittest.TestCase):
    def fetcher(self, plan, **kwargs):
        self.sleeps = []
        self.now = [0.0]
        return lc.Fetcher(opener=FakeOpener(plan), resolver=PUBLIC, sleep=self.sleeps.append,
                          clock=lambda: self.now[0], **kwargs)

    def test_head_success_makes_one_request(self):
        f = self.fetcher({})
        self.assertEqual(f.check("https://a.example/x")["outcome"], "ok")
        self.assertEqual([c[0] for c in f.opener.calls], ["HEAD"])
        self.assertEqual(f.opener.calls[0][2], 8.0)

    def test_head_failure_falls_back_to_get(self):
        f = self.fetcher({("HEAD", "https://a.example/x"): 405})
        self.assertEqual(f.check("https://a.example/x")["outcome"], "ok")
        self.assertEqual([c[0] for c in f.opener.calls], ["HEAD", "GET"])

    def test_404_is_dead_after_both_methods(self):
        f = self.fetcher({"https://a.example/x": 404})
        result = f.check("https://a.example/x")
        self.assertEqual((result["outcome"], result["status"]), ("dead", 404))
        self.assertEqual(len(f.opener.calls), 2)

    def test_status_classes(self):
        expected = {403: "unverified", 429: "unverified", 410: "dead", 500: "server", 503: "server", 400: "dead"}
        for status, outcome in expected.items():
            with self.subTest(status=status):
                f = self.fetcher({"https://a.example/x": status})
                self.assertEqual(f.check("https://a.example/x")["outcome"], outcome)

    def test_connection_errors_are_unreachable(self):
        for exc in (urllib.error.URLError("boom"), socket.timeout("slow"), ConnectionResetError("reset")):
            with self.subTest(exc=exc.__class__.__name__):
                f = self.fetcher({"https://a.example/x": exc})
                self.assertEqual(f.check("https://a.example/x")["outcome"], "unreachable")

    def test_private_targets_are_never_requested(self):
        f = self.fetcher({})
        for url in ("http://127.0.0.1/a", "http://169.254.169.254/latest/meta-data", "http://[::1]/",
                    "http://10.0.0.1/", "ftp://a.example/x", "https://user:pw@a.example/x"):
            with self.subTest(url=url):
                self.assertEqual(f.check(url)["outcome"], "refused")
        self.assertEqual(f.opener.calls, [])

    def test_per_host_rate_limit(self):
        f = self.fetcher({}, delay=1.0)
        f.check("https://a.example/1")
        f.check("https://a.example/2")
        f.check("https://b.example/1")
        self.assertEqual(self.sleeps, [1.0])

    def test_check_online_caps_requests_and_dedupes(self):
        external = [("https://a.example/%d" % i, "README.md", i, "", False) for i in range(5)]
        external.append(("https://a.example/0", "docs/x.md", 3, "", False))
        f = self.fetcher({"https://a.example/0": 404}, max_requests=4)
        findings, stats = lc.check_online(external, f)
        self.assertEqual(stats["urls"], 5)
        self.assertEqual(stats["skipped_by_cap"], 2)
        self.assertLessEqual(f.requests, 5)
        self.assertEqual(sorted((x["file"], x["rule"]) for x in findings),
                         [("README.md", "LK012"), ("docs/x.md", "LK012")])


class OnlineMainTests(unittest.TestCase):
    def test_online_flag_uses_the_injected_fetcher(self):
        patches = forbid_network()
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])
        fetcher = lc.Fetcher(opener=FakeOpener({"https://gone.example/a": 404, "https://slow.example/b": 503}),
                             resolver=PUBLIC, sleep=lambda s: None)
        with tempfile.TemporaryDirectory() as tmp:
            write(Path(tmp) / "README.md",
                  "[a](https://gone.example/a) [b](https://slow.example/b) [c](https://ok.example/c) "
                  "[d](http://127.0.0.1:8080/x)\n")
            code, out, _ = run([tmp, "--online", "--json", "--fail-on", "medium"], fetcher=fetcher)
        report = json.loads(out)
        self.assertEqual(code, 1)
        self.assertTrue(report["online"]["enabled"])
        self.assertEqual(sorted(f["rule"] for f in report["findings"]), ["LK007", "LK012", "LK013"])
        self.assertNotIn("127.0.0.1", [c[1] for c in fetcher.opener.calls])


class NetworkIsOffByDefaultTests(NoNetworkMixin, unittest.TestCase):
    def test_external_links_are_not_fetched_without_online(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(Path(tmp) / "README.md", "[a](https://gone.example/a) [b](http://old.example/b) https://c.example\n")
            code, out, _ = run([tmp])
        self.assertEqual(code, 0)
        self.assertIn("Offline run: no network requests were made", out)

    def test_script_never_builds_a_fetcher_offline(self):
        with mock.patch.object(lc, "Fetcher", side_effect=AssertionError("Fetcher built")):
            with tempfile.TemporaryDirectory() as tmp:
                write(Path(tmp) / "README.md", "[a](https://x.example)\n")
                self.assertEqual(run([tmp])[0], 0)


class CliTests(NoNetworkMixin, unittest.TestCase):
    def project(self, tmp):
        write(Path(tmp) / "README.md", "[x](gone.md) [y](http://old.example/a)\n")
        return tmp

    def test_exit_codes_for_fail_on(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.project(tmp)
            self.assertEqual(run([tmp])[0], 0)
            self.assertEqual(run([tmp, "--fail-on", "never"])[0], 0)
            self.assertEqual(run([tmp, "--fail-on", "high"])[0], 1)
            self.assertEqual(run([tmp, "--fail-on", "low"])[0], 1)
            write(Path(tmp) / "README.md", "[y](http://old.example/a)\n")
            self.assertEqual(run([tmp, "--fail-on", "high"])[0], 0)
            self.assertEqual(run([tmp, "--fail-on", "medium"])[0], 0)
            self.assertEqual(run([tmp, "--fail-on", "low"])[0], 1)

    def test_bad_input_exits_2(self):
        code, _out, err = run(["definitely-missing-folder-xyz"])
        self.assertEqual(code, 2)
        self.assertIn("does not exist", err)
        with tempfile.TemporaryDirectory() as tmp:
            code, _out, err = run([tmp])
            self.assertEqual(code, 2)
            self.assertIn("no .md", err)
            write(Path(tmp) / "a.txt", "x")
            self.assertEqual(run([str(Path(tmp) / "a.txt")])[0], 2)
            self.assertEqual(run([tmp, "--timeout", "0"])[0], 2)

    def test_invalid_option_exits_2(self):
        with self.assertRaises(SystemExit) as caught, redirect_stderr(io.StringIO()):
            lc.main(["--fail-on", "bogus"])
        self.assertEqual(caught.exception.code, 2)

    def test_json_shape_and_ordering(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.project(tmp)
            code, out, _ = run([tmp, "--json"])
        report = json.loads(out)
        self.assertEqual(report["tool"], "link_check")
        self.assertEqual(report["summary"]["by_severity"], {"high": 1, "medium": 0, "low": 1, "info": 0})
        self.assertEqual([f["severity"] for f in report["findings"]], ["high", "low"])
        for key in ("rule", "file", "line", "message", "suggestion", "section", "first_run"):
            self.assertIn(key, report["findings"][0])

    def test_markdown_report_is_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.project(tmp)
            self.assertEqual(run([tmp])[1], run([tmp])[1])

    def test_pipe_in_message_is_escaped_in_tables(self):
        self.assertEqual(lc.cell("a|b\nc"), "a\\|b c")

    def test_clean_project_says_no_findings(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(Path(tmp) / "README.md", "# Hi\n\n[self](#hi)\n")
            self.assertIn("No findings.", run([tmp])[1])

    def test_script_runs_as_a_subprocess(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.project(tmp)
            proc = subprocess.run([sys.executable, str(SCRIPT), tmp, "--fail-on", "high"],
                                  capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("LK001", proc.stdout)


class FileSystemTests(NoNetworkMixin, unittest.TestCase):
    def test_skip_dirs_are_not_scanned(self):
        tree = {"README.md": "ok\n", "node_modules/x/README.md": "[a](gone.md)\n", ".git/x.md": "[a](gone.md)\n"}
        self.assertEqual(scan(tree), [])

    def test_resolve_reports_case_and_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(Path(tmp) / "Docs" / "Guide.md", "# G\n")
            project = lc.Project(tmp)
            self.assertEqual(project.resolve("Docs/Guide.md"), ("ok", "Docs/Guide.md"))
            self.assertEqual(project.resolve("docs/guide.md"), ("case", "Docs/Guide.md"))
            self.assertEqual(project.resolve("Docs/Other.md")[0], "missing")


if __name__ == "__main__":
    unittest.main()
