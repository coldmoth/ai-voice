"""GitHub release checks with an opt-out and a persisted daily cache."""
import httpx

from . import __version__

REPO = "coldmoth/ai-voice"
API = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASE_URL_PREFIX = f"https://github.com/{REPO}/releases/"
INTERVAL = 24 * 3600


def parse_version(text: object) -> tuple[int, int, int] | None:
    if not isinstance(text, str):
        return None
    parts = text.removeprefix("v").split(".")
    if len(parts) != 3 or not all(part.isdecimal() for part in parts):
        return None
    try:
        return tuple(int(part) for part in parts)
    except ValueError:
        return None


def is_newer(remote: str, local: str) -> bool:
    remote_version, local_version = parse_version(remote), parse_version(local)
    return remote_version is not None and local_version is not None and remote_version > local_version


def check(state: dict, auto: bool, *, now: float, force: bool = False,
          client: httpx.Client | None = None) -> tuple[dict, dict]:
    if not force and not auto:
        return {"status": "disabled"}, state
    new_state = state
    if force or not 0 <= now - state["last_check"] < INTERVAL:
        headers = {"Accept": "application/vnd.github+json", "User-Agent": f"AI-Voice/{__version__}"}
        try:
            if client is not None:
                response = client.get(API, headers=headers, timeout=8, follow_redirects=False)
            else:
                with httpx.Client(timeout=8) as own:
                    response = own.get(API, headers=headers, follow_redirects=False)
        except (httpx.HTTPError, OSError, TimeoutError):
            return {"status": "error", "error": "network"}, state
        latest = None
        if response.status_code == 200:
            try:
                body = response.json()
            except ValueError:
                return {"status": "error", "error": "github"}, state
            version = parse_version(body.get("tag_name")) if isinstance(body, dict) else None
            url = body.get("html_url") if isinstance(body, dict) else None
            if (version is None or not isinstance(url, str) or not url.startswith(RELEASE_URL_PREFIX)
                    or body.get("draft") or body.get("prerelease")):
                return {"status": "error", "error": "github"}, state
            latest = {"version": ".".join(str(part) for part in version), "url": url}
        elif response.status_code != 404:
            return {"status": "error", "error": "github"}, state
        new_state = {**state, "last_check": now, "latest": latest}
    latest = new_state["latest"]
    if (latest is not None and is_newer(latest["version"], __version__)
            and (force or latest["version"] != new_state["skipped"])):
        return {"status": "available", **latest}, new_state
    return {"status": "current", "version": __version__}, new_state
