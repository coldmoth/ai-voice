# Changelog

All notable changes to this project will be documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.4.8] - 2026-10-09

### Added

- In-app update: the "update available" banner has an Update button that downloads the release, verifies its SHA256, installs it and restarts the app (macOS: app bundle swap; Windows: silent Inno Setup installer).

### Fixed

- macOS: Start now asks for undecided microphone and speech recognition permissions instead of showing an error.

## [0.4.7] - 2026-10-09

### Fixed

- Windows: GigaAM failed to load with `[Errno 22] Invalid argument` (download progress bar in a windowed app); load errors are now logged with a traceback.
- macOS: the missing-permission message now points to System Settings instead of a console command.

## [0.4.6] - 2026-10-07

### Added

- Windows port (beta): Windows 10 (1809+)/11 x64 installer `AI-Voice-Setup.exe` published with each release,
  VB-CABLE virtual microphone setup, README and site download button.
  macOS: no changes in this release.

## [0.4.5] - 2026-10-06

### Fixed

- The onboarding window can be dragged by its empty areas.
- The onboarding permissions step asks for microphone and speech recognition access itself and shows the real status; the System Settings link appears only after a denial.

## [0.4.4] - 2026-10-06

### Fixed

- Release DMG now includes its background image, so the drag-to-Applications window is styled.

## [0.4.3] - 2026-10-06

### Added

- Releases include a drag-to-Applications `AI-Voice.dmg`; README and site have a direct download button.

## [0.4.2] - 2026-10-06

### Fixed

- CI: tests for dev-only scripts are skipped when those scripts are not part of the source tree.

## [0.4.1] - 2026-10-06

### Fixed

- CI: tests that import `vc_worker` and `tests` now collect with plain `pytest`.
- Speech locale probe no longer fails when a timed-out helper has already exited (macOS `EPERM`).

## [0.4.0] - 2026-10-06

### Added

- First public release.
- English and Russian UI.
- First-launch setup.
- API keys in Settings.
- Update checker: checks GitHub Releases once a day, shows a banner when a new version is available, can be turned
  off in Settings → General. It never installs anything itself.
- Built-in virtual microphone: `AIVoiceMic.driver` (BlackHole v0.7.1 renamed), installed and removed from
  Settings → Audio with the Mac admin password.
- Live voice engine download: one-click install of Python 3.10 + PyTorch, the RVC and Seed-VC sources and the base
  models from Hugging Face (~3.4 GB) into `~/Library/Application Support/AI Voice/vc-runtime`, with removal from
  Settings → Storage.
- Project documentation and site: English README with a Russian twin, architecture overview, contributing and
  security guides, issue and pull request templates, and a static site for GitHub Pages.
