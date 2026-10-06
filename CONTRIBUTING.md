# Contributing to AI Voice

Thanks for helping out! This document covers the development setup and the ground rules.

## Development setup

Same as [Build from source](README.md#build-from-source):

```sh
brew install uv
git clone https://github.com/coldmoth/ai-voice.git
cd ai-voice
python3 -m venv .venv && .venv/bin/pip install -e '.[test]'
scripts/build-app.sh
scripts/install-app.sh
```

During development, `make watch` rebuilds the app automatically when files change.

## Tests

Run these before opening a pull request:

```sh
.venv/bin/python -m pytest -q            # offline Python test suite
node --check macos/desktop/app.js        # UI syntax check
scripts/build-app.sh                     # verifies the build and the code signature
```

The headless UI tests additionally need the `playwright` npm package (`npm install playwright`), then run for
example `node tests/desktop-ui.cjs`.

All tests are offline: they never call the paid TTS API, the microphone, or the network.

## Ground rules

- Match the surrounding code style; keep changes small and focused — one pull request per topic.
- No new dependencies without discussing them in an issue first. Prefer the standard library and what is already
  installed.
- UI strings live in `src/ai_voice/locales/en.json` and `src/ai_voice/locales/ru.json` — always update both
  (see [ARCHITECTURE.md](ARCHITECTURE.md#adding-a-ui-language)).
- Never commit secrets, API keys, personal paths, logs, or build artifacts.
- Bugs are fixed in the shared function, not patched at every call site.

## License

AI Voice is licensed under [GPL-3.0](LICENSE). By contributing you agree that your contributions are licensed under
the same license (inbound = outbound).
