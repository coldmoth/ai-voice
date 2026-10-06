"""Public documentation links, README parity and static site assets."""
import importlib.util
from html.parser import HTMLParser
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCS = ("README.md", "README.ru.md", "ARCHITECTURE.md", "CONTRIBUTING.md")
PENDING_ASSETS = {"site/assets/demo.gif"}
MARKDOWN_LINK = re.compile(
    r'\]\(\s*(?:<([^>]+)>|([^\s()]*(?:\([^()]*\)[^\s()]*)*))'
)
REFERENCE_LINK = re.compile(r'^ {0,3}\[[^\]]+\]:\s*(?:<([^>]+)>|(\S+))', re.MULTILINE)
CSS_URL = re.compile(r'url\(\s*(?:"([^"]*)"|\'([^\']*)\'|([^)]*?))\s*\)', re.IGNORECASE)


class References(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.links = []
        self.scripts = []
        self.anchors = set()
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        line = self.getpos()[0]
        for name in ("src", "href"):
            if name in attrs:
                self.links.append((line, attrs[name] or ""))
        if tag == "script" and "src" in attrs:
            self.scripts.append((line, attrs["src"] or ""))
        if "id" in attrs:
            self.anchors.add(attrs["id"])
        if tag == "a" and "name" in attrs:
            self.anchors.add(attrs["name"])
        if tag == "meta" and attrs.get("property") == "og:image":
            self.links.append((line, attrs.get("content", "")))

    handle_startendtag = handle_starttag


@pytest.fixture(scope="module")
def release_check():
    spec = importlib.util.spec_from_file_location("release_check", ROOT / "scripts/release_check.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def link_error(filename, line, target, release_check=None):
    location = f"{filename}:{line}: {target}"
    if target.startswith("#"):
        return None
    url = urlsplit(target)
    if url.scheme == "https" and url.netloc:
        return None
    if url.scheme or url.netloc:
        return f"{location}: only HTTPS external links are allowed"
    path = (ROOT / filename).parent / unquote(url.path)
    try:
        relative = path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return f"{location}: target is outside the repository"
    if relative in {"PROJECT_STATE.md", "AGENTS.md", "docs"} or relative.startswith("docs/"):
        return f"{location}: private documentation link"
    if release_check is not None:
        patterns = release_check.load_patterns(ROOT / "release/public-files.txt")
        if not release_check.select_files([relative], patterns, release_check.EXCLUDE):
            return f"{location}: target is outside the public allowlist"
    if not path.is_file() and relative not in PENDING_ASSETS:
        return f"{location}: target file does not exist"
    return None


@pytest.mark.parametrize("filename", DOCS)
def test_public_document_links(filename, release_check):
    source = (ROOT / filename).read_text(encoding="utf-8")
    links = References(source).links
    for pattern in (MARKDOWN_LINK, REFERENCE_LINK):
        links += [(source.count("\n", 0, match.start()) + 1,
                   next(group for group in match.groups() if group is not None))
                  for match in pattern.finditer(source)]
    # Markdown autolinks are not HTML attributes.
    links += [(source.count("\n", 0, match.start()) + 1, match[1])
              for match in re.finditer(r'<(https?://[^>]+)>', source)]
    errors = [error for line, target in links
              if (error := link_error(filename, line, target, release_check))]
    assert not errors, "\n".join(errors)


def test_readme_heading_counts_match():
    counts = {filename: len(re.findall(r'^## ', (ROOT / filename).read_text(encoding="utf-8"), re.MULTILINE))
              for filename in ("README.md", "README.ru.md")}
    assert counts["README.md"] == counts["README.ru.md"], counts


@pytest.mark.parametrize("filename", ("README.md", "README.ru.md"))
def test_readme_live_voice_engine_anchor(filename):
    source = (ROOT / filename).read_text(encoding="utf-8")
    assert "live-voice-engine" in References(source).anchors, f"{filename}: missing live-voice-engine anchor"


def test_site_has_no_http_or_external_scripts():
    filename = "site/index.html"
    source = (ROOT / filename).read_text(encoding="utf-8")
    errors = [f"{filename}:{number}: HTTP URL"
              for number, line in enumerate(source.splitlines(), 1) if "http://" in line.lower()]
    for line, target in References(source).scripts:
        url = urlsplit(target)
        if url.scheme or url.netloc:
            errors.append(f"{filename}:{line}: external script {target}")
    assert not errors, "\n".join(errors)


@pytest.mark.parametrize("filename", ("site/index.html", "site/style.css"))
def test_site_assets_exist(filename):
    source = (ROOT / filename).read_text(encoding="utf-8")
    if filename.endswith(".html"):
        links = References(source).links
    else:
        links = [(source.count("\n", 0, match.start()) + 1,
                  next(group for group in match.groups() if group is not None).strip())
                 for match in CSS_URL.finditer(source)]
    errors = [error for line, target in links if (error := link_error(filename, line, target))]
    assert not errors, "\n".join(errors)
