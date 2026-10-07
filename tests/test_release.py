"""Release version and repository cleanup contracts."""
from pathlib import Path
import re
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_ci_workflow_runs_python_and_desktop_checks():
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    for required in (
        "push:", "pull_request:", "branches: [main]", "runs-on: macos-14",
        "actions/checkout@", "actions/setup-python@", 'python-version: "3.13"',
        "pip install -e '.[test]'", "pytest -q",
        "node --check macos/desktop/app.js",
        "npm i --no-save playwright@1 && npx playwright install chromium",
        "NODE_PATH=$PWD/node_modules node tests/desktop-ui.cjs",
    ):
        assert required in workflow


def test_release_workflow_builds_and_attaches_artifacts():
    workflow = (ROOT / ".github/workflows/release.yml").read_text()
    for required in (
        "tags: ['v*']", "contents: write", "runs-on: macos-14", "fetch-depth: 0",
        "brew install uv create-dmg", "scripts/build-app.sh --dmg", 'VERSION="${GITHUB_REF_NAME#v}"',
        'ditto -c -k --keepParent "build/AI Voice.app" "AI-Voice-${VERSION}.zip"',
        'shasum -a 256 "AI-Voice-${VERSION}.zip" > "AI-Voice-${VERSION}.zip.sha256"',
        'gh release create "v$VERSION" --notes-file notes.md',
        'AI-Voice.dmg AI-Voice.dmg.sha256 "AI-Voice-${VERSION}.zip" "AI-Voice-${VERSION}.zip.sha256"',
        "GH_TOKEN: ${{ github.token }}",
        "  windows:\n    needs: release\n    runs-on: windows-latest",
        "$version = $env:GITHUB_REF_NAME -replace '^v', ''",
        "scripts/build-win.ps1 -Version $version",
        'gh release upload "$env:GITHUB_REF_NAME" "build/win/AI-Voice-Setup-$version.exe" --clobber',
    ):
        assert required in workflow


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")
def test_release_notes_contain_only_the_selected_version(tmp_path):
    workflow = (ROOT / ".github/workflows/release.yml").read_text()
    command = next(line.strip() for line in workflow.splitlines() if line.strip().startswith("awk "))
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [0.4.1] - unreleased\n\n- Future feature.\n\n"
        "## [0.4.0] - unreleased\n\n### Added\n\n- Public release.\n\n"
        "## [0.3.0] - earlier\n\n- Older feature.\n",
    )
    subprocess.run(["bash", "-eu", "-c", 'VERSION=0.4.0\n' + command], cwd=tmp_path, check=True)
    assert (tmp_path / "notes.md").read_text() == "\n### Added\n\n- Public release.\n\n"


def test_public_license_and_initial_changelog():
    license_text = (ROOT / "LICENSE").read_text()
    assert license_text.splitlines()[0] == "                    GNU GENERAL PUBLIC LICENSE"
    assert "Version 3, 29 June 2007" in license_text
    assert "END OF TERMS AND CONDITIONS" in license_text
    assert "How to Apply These Terms to Your New Programs" in license_text
    changelog = (ROOT / "CHANGELOG.md").read_text()
    assert "## [0.4.0] - 2026-10-06" in changelog
    for feature in ("First public release", "English and Russian UI", "First-launch setup",
                    "Update checker", "Built-in virtual microphone", "Live voice engine download"):
        assert feature in changelog


def test_version_constant():
    import ai_voice
    assert ai_voice.__version__ == "0.4.6"


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")
def test_version_script_matches_package():
    out = subprocess.run(["scripts/version.sh"], capture_output=True, text=True, check=True).stdout.strip()
    import ai_voice
    assert out == ai_voice.__version__


def test_legacy_directory_is_removed():
    assert not (ROOT / "legacy").exists()


def test_runtime_files_do_not_reference_legacy_directory():
    result = subprocess.run(
        ["git", "grep", "-n", "legacy" + "/", "--", "src", "scripts", "tests", "Makefile"],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 1, result.stdout + result.stderr


@pytest.mark.parametrize("name", ["build-app.sh", "install-app.sh", "rebuild.sh", "watch-app.sh"])
def test_build_and_install_scripts_are_english(name):
    script = (ROOT / "scripts" / name).read_text()
    assert not re.search(r"[\u0410-\u044f\u0401\u0451]", script)


def test_build_requires_uv_without_private_fallback():
    script = (ROOT / "scripts" / "build-app.sh").read_text()
    assert "state/vc-spike" not in script
    assert "uv is required: brew install uv" in script


@pytest.fixture
def release_check():
    import importlib.util

    if not (ROOT / "scripts/release_check.py").exists():
        pytest.skip("release tooling is private (not in the public export)")
    spec = importlib.util.spec_from_file_location("release_check", ROOT / "scripts/release_check.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_load_patterns_skips_blank_and_comment_lines(release_check, tmp_path):
    path = tmp_path / "patterns.txt"
    path.write_text("# comment\n\n  src/**  \n  # indented comment\nREADME.md\n")
    assert release_check.load_patterns(path) == ["src/**", "README.md"]


def test_select_files_preserves_order_and_excludes_caches(release_check):
    files = ["src/a.py", "src/x/__pycache__/a.pyc", "docs/p.md", "README.md"]
    assert release_check.select_files(
        files, ["src/**", "README.md", "missing/**"], release_check.EXCLUDE,
    ) == ["src/a.py", "README.md"]


@pytest.mark.parametrize("pattern,selected", [
    ("src/*.py", ["src/a.py", "src/a1.py", "src/b2.py", "src/c3.py"]),
    ("src/**/*.py", ["src/a.py", "src/x/b.py", "src/a1.py", "src/b2.py", "src/c3.py"]),
    ("**/__pycache__/**", ["__pycache__/a.pyc", "src/__pycache__/b.pyc"]),
    ("src/[ab]?.py", ["src/a1.py", "src/b2.py"]),
])
def test_globs_respect_path_segments(release_check, pattern, selected):
    files = ["src/a.py", "src/x/b.py", "__pycache__/a.pyc", "src/__pycache__/b.pyc",
             "src/a1.py", "src/b2.py", "src/c3.py"]
    assert release_check.select_files(files, [pattern], []) == selected


def test_scan_reports_lines_and_binary_content_and_allows_marked_lines(release_check, tmp_path):
    pattern = "never" + "eply"
    (tmp_path / "a.txt").write_text(
        f"clean\n{pattern.upper()}\n{pattern} # publish-allow\n",
    )
    (tmp_path / "b.bin").write_bytes(b"\xff\xfe " + pattern.encode())
    assert release_check.scan(tmp_path, ["a.txt", "b.bin"], [pattern]) == [
        ("a.txt", 2, pattern), ("b.bin", 1, pattern),
    ]


def test_scan_user_path_requires_a_name_and_is_case_insensitive(release_check, tmp_path):
    pattern = "/" + "Users/[a-z]"
    (tmp_path / "paths.txt").write_text("/" + "Users/bob\n/" + "Users/\nUPPERCASE\n/" + "Users/Bob\n")
    assert release_check.scan(tmp_path, ["paths.txt"], [pattern]) == [
        ("paths.txt", 1, pattern), ("paths.txt", 4, pattern),
    ]


def test_public_allowlist_keeps_release_tools_and_private_fixtures_private(release_check):
    private = ["release/denylist.txt", "scripts/publish.sh", "scripts/release_check.py",
               "AGENTS.md", "docs/proposals/x.md", "scripts/" + "clo" + "dex_exec.py",
               "tests/fixtures/private/a.txt", "src/__pycache__/a.pyc"]
    include = release_check.load_patterns(ROOT / "release/public-files.txt")
    assert release_check.select_files(private + ["src/a.py", "README.md"], include,
                                      release_check.EXCLUDE) == ["src/a.py", "README.md"]


def test_public_allowlist_includes_windows_build_and_runtime_files(release_check):
    files = ["scripts/build-win.ps1", "windows/installer.iss", "windows/AppIcon.ico",
             "vc_worker/engine-requirements-win.lock", "vc_worker/engine-manifest.json",
             "src/ai_voice/winshell.py", "src/ai_voice/vad.py"]
    include = release_check.load_patterns(ROOT / "release/public-files.txt")
    assert release_check.select_files(files, include, release_check.EXCLUDE) == files


def test_windows_installer_cleans_runtime_and_offers_launch():
    installer = (ROOT / "windows/installer.iss").read_text()
    cleanup = installer.split("[UninstallDelete]\n", 1)[1].split("\n[", 1)[0]
    assert cleanup.strip() == 'Type: filesandordirs; Name: "{app}"'
    run = installer.split("[Run]\n", 1)[1].split("\n[", 1)[0]
    for required in ('Filename: "{app}\\python\\pythonw.exe"',
                     'Parameters: "-X utf8 -m ai_voice.winshell"',
                     'WorkingDir: "{app}"', 'Flags: nowait postinstall skipifsilent',
                     'Description: "Launch AI Voice"'):
        assert required in run


def test_cli_exports_tracked_files_and_scans_relative_to_export_root(release_check, tmp_path, monkeypatch, capsys):
    pattern = "never" + "eply"
    (tmp_path / "README.md").write_text(pattern)
    (tmp_path / "AGENTS.md").write_text(pattern)
    monkeypatch.setattr(release_check.subprocess, "run", lambda *args, **kwargs:
                        subprocess.CompletedProcess(args[0], 0, "README.md\0AGENTS.md\0"))
    assert release_check.main(["export-list"]) == 0
    assert capsys.readouterr().out == "README.md\n"
    assert release_check.main(["scan", str(tmp_path)]) == 1
    assert capsys.readouterr().out == f"README.md:1: {pattern}\n"
    (tmp_path / "README.md").write_text("clean")
    assert release_check.main(["scan", str(tmp_path)]) == 0
    assert capsys.readouterr().out == ""
