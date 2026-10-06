# AI Voice Mic driver

AI Voice Mic is based on [BlackHole](https://github.com/ExistentialAudio/BlackHole)
by Existential Audio Inc., licensed under GPL-3.0.

Pinned upstream: tag `v0.7.1`, commit
`e2b22aaaba4e507a097131704bf96dabc004d9cf`. The shell-sourceable `UPSTREAM`
file records this revision and the fixed factory UUID
`96C11719-FAB7-4A61-A09C-B95ECB0B0815` (replacing upstream
`e395c745-4eea-4d94-bb92-46224221047c`).

Changes are limited to build defines (two channels, AI Voice Mic device and driver
names, bundle ID `io.github.coldmoth.aivoicemic`) and the factory UUID. The upstream
C source is unchanged. Bundle metadata is expanded for the standalone build.

Rebuild from the project root with Command Line Tools installed:

```sh
scripts/build-driver.sh
```

The ad-hoc signed universal arm64 + x86_64 bundle is written to
`build/driver/AIVoiceMic.driver`. The build uses a disposable upstream checkout in
`build/cache/blackhole` and reuses the driver when it is newer than the script and
`UPSTREAM`.

The installed driver lives at `/Library/Audio/Plug-Ins/HAL/AIVoiceMic.driver`.
To uninstall it manually:

```sh
sudo rm -rf /Library/Audio/Plug-Ins/HAL/AIVoiceMic.driver
```

Restart the Mac after uninstalling so Core Audio unloads the driver.
