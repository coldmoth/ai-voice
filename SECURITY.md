# Security policy

## Reporting a vulnerability

Please report security issues privately through GitHub: open the repository's **Security** tab and use
**Report a vulnerability** (private advisories). Do not file a public issue for a security problem.

You can expect an acknowledgement within a few days. If the report is confirmed, a fix ships in the next release
and the advisory is published after that.

## Scope

The areas where vulnerabilities matter most:

- **Key handling** — API keys live in the macOS Keychain and must never leak into logs, the UI, or files.
- **The local HTTP server** — the backend listens on 127.0.0.1 and guards the UI with a random window token,
  Host/Origin checks and a strict CSP (see [ARCHITECTURE.md](ARCHITECTURE.md#security-model)).
- **The driver installer** — installing the bundled virtual microphone asks for the macOS admin password and
  writes to `/Library/Audio/Plug-Ins/HAL/`.
- **The Live voice engine downloader** — fetches a pinned, resumable set of files from Hugging Face into the app's
  data directory.

Please do not test against the paid Fish Audio API or other people's accounts.
