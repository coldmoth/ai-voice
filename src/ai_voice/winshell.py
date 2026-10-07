"""Windows WebView2 shell and headless backend smoke check."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import parse_qs, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from . import paths


def _stop_backend(process: subprocess.Popen) -> None:
    try:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    finally:
        if process.stdout is not None:
            process.stdout.close()


def start_backend(python: Path, env: dict) -> tuple[subprocess.Popen, str]:
    """Start the desktop server with the shell's PID for its parent watchdog."""
    process = subprocess.Popen(
        [str(python), "-m", "ai_voice.desktop", "--parent-pid", str(os.getpid())],
        env={
            **env,
            "AI_VOICE_RESOURCES": env.get("AI_VOICE_RESOURCES") or str(paths.ROOT),
            "AI_VOICE_WEB": env.get("AI_VOICE_WEB") or str(paths.WEB),
            "AI_VOICE_HOME": env.get("AI_VOICE_HOME") or str(paths.app_home()),
            "AI_VOICE_VC_RUNTIME": env.get("AI_VOICE_VC_RUNTIME") or str(paths.vc_runtime_home()),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTHONUTF8": "1",
        }, stdout=subprocess.PIPE,
        text=True, encoding="utf-8",
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    try:
        line = process.stdout.readline()
        url = json.loads(line)["url"]
        if not isinstance(url, str) or not url:
            raise ValueError("Backend did not provide a URL")
        return process, url
    except BaseException:
        _stop_backend(process)
        raise


class Bridge:
    def __init__(self):
        self._window = None

    def language(self, payload):
        pass  # Windows has no native menu.

    def drag(self, payload):
        pass  # pywebview-drag-region handles dragging.

    def hotkey(self, payload):
        pass  # Task 8 connects HotkeyThread here.

    def notify(self, payload):
        if self._window is not None:
            self._window.evaluate_js("showToast(" + json.dumps(payload.get("body", "")) + ")")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--smoke", action="store_true")
    args = parser.parse_args(argv)
    process = None
    try:
        process, url = start_backend(Path(sys.executable), {**os.environ, "PYTHONUTF8": "1"})
        if args.smoke:
            parts = urlsplit(url)
            token = parse_qs(parts.query)["token"][0]
            health = urlunsplit((parts.scheme, parts.netloc, "/health", "", ""))
            request = Request(health, headers={"X-AI-Voice-Token": token})
            with urlopen(request, timeout=10) as response:
                if response.status != 200 or json.load(response) != {"ok": True}:
                    raise RuntimeError("Backend health check failed")
            print("ok")
        else:
            import webview
            bridge = Bridge()
            bridge._window = webview.create_window(
                "AI Voice", url, width=1100, height=720,
                frameless=False, easy_drag=False, js_api=bridge,
            )
            webview.start(gui="edgechromium")
        return 0
    except Exception:
        # Backend URLs contain an authentication token: never echo exception data.
        if sys.stderr is not None:
            print("AI Voice shell failed to start or check the backend", file=sys.stderr)
        return 1
    finally:
        if process is not None:
            _stop_backend(process)


if __name__ == "__main__":
    raise SystemExit(main())
