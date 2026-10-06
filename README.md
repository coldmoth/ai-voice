English | [Русский](README.ru.md)

![AI Voice banner](site/assets/banner.png)

[![Latest release](https://img.shields.io/github/v/release/coldmoth/ai-voice)](https://github.com/coldmoth/ai-voice/releases/latest)
[![License: GPL-3.0](https://img.shields.io/badge/license-GPL--3.0-blue)](LICENSE)
[![CI](https://github.com/coldmoth/ai-voice/actions/workflows/ci.yml/badge.svg)](https://github.com/coldmoth/ai-voice/actions/workflows/ci.yml)
![macOS 13+ · Apple silicon](https://img.shields.io/badge/macOS%2013%2B-Apple%20silicon-black)

# AI Voice

You talk (or type), macOS recognises your speech, [Fish Audio](https://fish.audio) speaks it in the voice you chose, and the result goes to a virtual microphone that any app — a call, a stream, a game — can use as its input. An optional experimental **Live voice** converts your voice in real time, on-device.

![Demo: pick a voice, speak, hear the result](site/assets/demo.gif)

## Features

- **Speech or text input** — speak into the microphone, or type up to 1000 characters and press Speak text.
- **Thousands of Fish Audio voices** with search, filters and speech demos, plus voices from your own Fish account.
- **Favourites** and a **hotkey voice swap** in the middle of a call.
- **Listen-back monitor** — hear the new voice in your headphones while the virtual microphone output stays unchanged.
- **Usage and limit tracking** for your Fish Audio characters.
- **Live voice** (experimental, Apple silicon): real-time voice conversion that runs locally with RVC and Seed-VC models.
- **English and Russian UI.**
- **Keys stored in the macOS Keychain**, never in files.

![Main window](site/assets/main.png)
![Voice catalog](site/assets/catalog.png)

## Requirements

- macOS 13 or later, Apple silicon (the published build is arm64-only; Live voice also requires Apple silicon).
- A [Fish Audio](https://fish.audio) account with an API key. Paid usage is billed by Fish Audio to your own account.
- Any app that accepts a microphone input.

## Install

1. Download `AI-Voice-X.Y.Z.zip` from [Releases](https://github.com/coldmoth/ai-voice/releases/latest).
2. Unzip it and move **AI Voice.app** to Applications.
3. First open:
   - macOS 13–14: right-click the app → **Open** → **Open**.
   - macOS 15+: try to open it once, then go to **System Settings → Privacy & Security** and click **Open Anyway**.

   Alternative: `xattr -dr com.apple.quarantine "/Applications/AI Voice.app"`.

**Why the warning?** The app is not notarized (there is no paid Apple Developer account behind it). It is built from this repository by GitHub Actions — see the [release workflow runs](https://github.com/coldmoth/ai-voice/actions/workflows/release.yml) — and every release ships a `.sha256` file so you can verify the download.

## First launch

The setup screens walk you through language, permissions, your Fish Audio key and the virtual microphone. Every step is skippable, and you can run the setup again later from **Settings → General → Run setup again**.

![Setup: Fish Audio key](site/assets/onboarding-fish.png)

- **Language** — English or Russian UI.
- **Permissions** — microphone and speech recognition, requested by a separate helper so macOS shows a clear prompt.
- **Fish Audio** — paste your API key; it is stored in the macOS Keychain and usage goes to your own Fish account and limits. You can manage keys later in **Settings → API keys**.
- **Virtual microphone** — pick the device other apps will hear (see the next sections).

![Settings: API keys](site/assets/settings-keys.png)

## Using with other apps

In the app's audio settings, choose your virtual device as the microphone (input device): **AI Voice Mic** (built-in), **BlackHole 2ch**, or your Loopback device.

For better quality, turn off the app's noise suppression and echo cancellation (a recommendation, not a requirement).

## Virtual audio device

AI Voice needs a virtual audio device between it and other apps. Three options:

| Option | Pros | Cons |
|---|---|---|
| **AI Voice Mic (built-in)** | One click in Settings, nothing to download | Asks for your Mac admin password; audio devices reload for a few seconds during install |
| **BlackHole 2ch** | Free, widely used | Separate download and installer |
| **Other (e.g. Loopback)** | Flexible routing | Manual setup; Loopback is paid |

The built-in driver is [BlackHole](https://existential.audio/blackhole/) v0.7.1 renamed to `AIVoiceMic.driver`. Install or remove it in **Settings → Audio → Virtual microphone**. To remove it manually, delete `/Library/Audio/Plug-Ins/HAL/AIVoiceMic.driver` and run `sudo killall coreaudiod`.

![Setup: choosing the virtual microphone](site/assets/onboarding-device.png)

## Live voice engine
<a id="live-voice-engine"></a>

Live voice is optional and experimental. Clicking **Download components** installs, into `~/Library/Application Support/AI Voice/vc-runtime`:

- Python 3.10 with PyTorch (via [uv](https://github.com/astral-sh/uv)),
- the RVC and Seed-VC source code,
- base models from Hugging Face — about 3.4 GB in total.

Everything stays on this Mac and the conversion runs locally. Remove the components in **Settings → Storage**. Voice models come from the Hugging Face catalog inside the app, or from your own recordings. Prefer the terminal? `scripts/install-vc-engine.sh` performs the same install.

![Live voice engine](site/assets/engine.png)

## Updates

The app checks GitHub Releases once a day and shows a banner when a new version is available. It never installs anything itself. Turn the check off in **Settings → General → Check for updates automatically**.

![Update available](site/assets/update.png)

## Privacy

- No telemetry, no accounts, no analytics.
- Network calls go only to: Fish Audio (speech synthesis and the voice catalog), Apple's speech recognition (on-device or on Apple servers, depending on your macOS settings), GitHub (update check), and Hugging Face (voice catalog and engine download).
- API keys live only in the macOS Keychain.
- Audio and transcripts are not saved to disk.

## Responsible use

Use your own voice or voices you have the right to use. **Do not impersonate real people**, deceive, harass, commit fraud or bypass voice verification. Follow the terms of service of Fish Audio and of the apps you use it with. Voice models from Hugging Face have their own licences — check them before use. The authors are not responsible for misuse.

## Build from source

```sh
brew install uv
git clone https://github.com/coldmoth/ai-voice.git
cd ai-voice
python3 -m venv .venv && .venv/bin/pip install -e '.[test]'
scripts/build-app.sh
scripts/install-app.sh
```

Run the tests with `.venv/bin/python -m pytest -q`. During development, `make watch` rebuilds the app automatically when files change.

See [ARCHITECTURE.md](ARCHITECTURE.md) for how it works inside and [CONTRIBUTING.md](CONTRIBUTING.md) if you want to help.

## License

[GPL-3.0](LICENSE).

**Credits:** [Fish Audio](https://fish.audio), [BlackHole](https://existential.audio/blackhole/) by Existential Audio, [RVC Project](https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI), [Seed-VC](https://github.com/Plachtaa/seed-vc), [uv](https://github.com/astral-sh/uv). Not affiliated with Fish Audio.
