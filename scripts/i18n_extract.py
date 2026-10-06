#!/usr/bin/env python3
"""Scan source literals/comments for translation work, or format dictionaries.

Multiple literals on one source line are joined with a newline in the JSON entry.
Python uses its stdlib parser; JS/Swift use a lightweight lexical scanner and HTML
uses HTMLParser to include text nodes as well as attributes.
"""
import argparse
import ast
from html.parser import HTMLParser
import io
import json
from pathlib import Path
import re
import tokenize
import warnings

ROOT = Path(__file__).resolve().parents[1]
LOCALES_DIR = ROOT / "src/ai_voice/locales"
CYRILLIC = re.compile(r"[\u0410-\u042f\u0430-\u044f\u0401\u0451]")
LEXEMES = re.compile(
    r'(?P<comment>//[^\n]*|/\*[\s\S]*?\*/)'
    r'|(?P<string>"""[\s\S]*?"""|"(?:\\[\s\S]|[^"\\])*"'
    r"|'(?:\\[\s\S]|[^'\\])*'|`(?:\\[\s\S]|[^`\\])*`)"
)


def _record(result, path, line, text, comment=False):
    if comment:
        for offset, content in enumerate(text.splitlines()):
            if CYRILLIC.search(content):
                key = f"{path}:{line + offset}#comment"
                result[key] = result[key] + "\n" + content if key in result else content
    elif CYRILLIC.search(text):
        key = f"{path}:{line}"
        result[key] = result[key] + "\n" + text if key in result else text


def _python(source, path, result):
    class Literals(ast.NodeVisitor):
        def visit_Constant(self, node):
            if isinstance(node.value, str):
                _record(result, path, node.lineno, node.value)

        def visit_JoinedStr(self, node):
            # Preserve interpolation expressions; do not also visit their text pieces.
            text = ast.get_source_segment(source, node)
            match = re.match(r"(?i:[rubf]*)(\"\"\"|'''|\"|')", text)
            if match:
                _record(result, path, node.lineno, text[match.end():-len(match[1])])

    Literals().visit(ast.parse(source))
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT:
            _record(result, path, token.start[0], token.string, comment=True)


def _code(source, path, result, first_line=1):
    for match in LEXEMES.finditer(source):
        line = first_line + source.count("\n", 0, match.start())
        text = match[0]
        if match.lastgroup == "comment":
            _record(result, path, line, text, comment=True)
        else:
            quote = 3 if text.startswith('"""') else 1
            value = text[quote:-quote]
            if quote == 1 and text[0] != "`":
                try:
                    with warnings.catch_warnings():
                        # Swift interpolation is not a Python escape sequence.
                        warnings.simplefilter("ignore", SyntaxWarning)
                        value = ast.literal_eval(text)
                except (SyntaxError, ValueError):
                    pass  # JS/Swift interpolation and escapes stay in source form.
            _record(result, path, line, value)


def _html(source, path, result):
    class Literals(HTMLParser):
        in_script = False

        def handle_starttag(self, tag, attrs):
            raw = self.get_starttag_text()
            for match in re.finditer(r'''[\w:-]+\s*=\s*(["'])(.*?)\1''', raw, re.DOTALL):
                line = self.getpos()[0] + raw.count("\n", 0, match.start())
                _record(result, path, line, match[2])
            if tag == "script":
                self.in_script = True

        def handle_endtag(self, tag):
            if tag == "script":
                self.in_script = False

        def handle_data(self, data):
            if self.in_script:
                _code(data, path, result, self.getpos()[0])
            else:
                for offset, line in enumerate(data.splitlines()):
                    _record(result, path, self.getpos()[0] + offset, line.strip())

        def handle_comment(self, data):
            _record(result, path, self.getpos()[0], "<!--" + data + "-->", comment=True)

    parser = Literals()
    parser.feed(source)
    parser.close()


def scan(paths) -> dict[str, str]:
    result = {}
    for path in paths:
        path = Path(path)
        source = path.read_text(encoding="utf-8")
        if path.suffix == ".py":
            _python(source, path, result)
        elif path.suffix == ".html":
            _html(source, path, result)
        else:
            _code(source, path, result)
    return result


def fmt() -> None:
    for lang in ("en", "ru"):
        path = LOCALES_DIR / f"{lang}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("scan").add_argument("paths", nargs="*")
    commands.add_parser("fmt")
    args = parser.parse_args()
    if args.command == "fmt":
        fmt()
    else:
        paths = args.paths or [
            "macos/desktop/app.js", "macos/desktop/index.html", "macos/Desktop.swift",
            *(str(path.relative_to(ROOT)) for path in sorted((ROOT / "src/ai_voice").glob("*.py"))),
        ]
        try:
            print(json.dumps(scan(paths), ensure_ascii=False, indent=2, sort_keys=True))
        except BrokenPipeError:
            pass  # Allow scan | head without a traceback.


if __name__ == "__main__":
    main()
