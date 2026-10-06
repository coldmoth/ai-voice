# Voice conversion worker — phase 1

Run from the project root with `.venv-vc/bin/python -m vc_worker`. The main
application does not import this worker or torch. No desktop integration is
included. Install `sounddevice` in the VC environment before using devices;
The additional dependency is listed in `vc_worker/requirements.txt`.
NumPy/SciPy/librosa/torch/torchaudio and upstream dependencies are required there.
Tests run in the main `.venv` with a fake engine and fake sounddevice streams.

RVC v2/f0 exported models are used for `imported` and `trained`; RMVPE and
HuBERT run on MPS, without CPU inference fallback. The exported checkpoint is
loaded with `weights_only=True`; training-only/base checkpoints are rejected.
Optional `.index` retrieval and pitch shift are applied by RVC.
Seed-VC tiny is used for `zeroshot`, with a 10–30 s reference, 10 diffusion
steps and CFG 0.7. This tiny model is tone conversion without f0 conditioning:
`pitch_shift` and `index_rate` have no effect for Seed-VC.

Seed-VC code and weights are **GPL-3.0, personal use only** for this project.
RVC upstream code is MIT. Auxiliary license declarations and limitations are
recorded in the phase-0 proposal. No distribution approval is implied.
Adapter code was migrated from `state/vc-spike/{rvc,seed}_adapter.py`; originals
are retained for reproducibility. Engine sources, model weights and caches
remain under `state/vc-spike/`, dependencies under `.venv-vc/`. Loads are local,
Hugging Face offline mode is forced; missing cached components produce errors.
`SYSTEM_VERSION_COMPAT=0` is forced before torch imports.

## JSON-lines protocol (one object per line)

stdout contains only events; engine prints go to stderr. Commands:

```json
{"cmd":"load","voice":{"kind":"trained","model":"state/vc-voices/id/model.pth","index":"state/vc-voices/id/model.index"},"config":{"sample_rate":40000,"hop_ms":256,"context_ms":500,"crossfade_ms":64,"search_ms":16}}
{"cmd":"load","voice":{"kind":"zeroshot","reference":"state/vc-voices/id/reference.wav"}}
{"cmd":"start","devices":{"input_device":"MIC","output_device":"AI Voice","monitor_enabled":false,"monitor_device":null}}
{"cmd":"params","params":{"pitch_shift":0,"index_rate":0.5,"output_gain_db":0,"monitor_gain_db":-6}}
{"cmd":"status"}
{"cmd":"stop"}
{"cmd":"quit"}
```

Events use `event`: `status`, `levels`, `error`, `stopped`. Status reports state,
config, params, high-water `rss_mb` (MiB), dropped_blocks, processing_ms and
estimated latency_ms (capture hop + processing + one output hop). This estimate
is not an acoustic or end-to-end measurement. `levels` reports RMS of
input and converted output before output gain. Monitor gain is independent;
monitor must use a distinct explicitly selected output. Devices belong solely
to the worker. Stop is idempotent. Quit or stdin EOF closes streams. Errors do
not put engine diagnostics on stdout.

The input queue holds only the newest hop; output/monitor queues hold at most
two hops. No input backlog accumulates. Each engine window is context + overlap
of **previous** captured audio + the new hop (no future lookahead). Discard the
context, SOLA-align within ±search_ms, linear-crossfade the previous output tail,
emit exactly one hop, retain crossfade_ms. The initial history/tail is silence.
Stop drains the retained tail with a linear fade-out before `stopped`; device
drain has a 1 s timeout. A hop exceeding its time budget emits silence, drops
queued stale capture and increments dropped_blocks. Configuration ranges:
8–96 kHz, hop 160–256 ms, context 0–1000 ms, fade 40–80 ms, search 0–fade ms.

RSS ceiling is **3 GB** (3,000,000,000 bytes), checked before/after conversion and by
a 100 ms watchdog even during model load/idle. High-water RSS is conservative.
On breach: `error` with code `rss_budget`, streams close, `stopped`, process
exits 3 to release model memory. A stalled inference at stop closes streams
and returns an error after 2 s; quit terminates the daemon thread/process.
Native devices/MPS/quality and end-to-end remain manual validations.

WAV streaming benchmark: `.venv-vc/bin/python scripts/vc_bench.py <voice_id> <wav> --output <wav>`.
It reads an existing `state/vc-voices/<voice_id>/meta.json`, processes consecutive
causal windows through the same SOLA/deadline path, and reports RTF including
the cold first step, all step times, drops and peak RSS. The last input hop is
zero-padded; output contains whole hops plus the faded tail. No device opens.
The phase-0 `--preflight` and `--adapter … --wav …` interfaces are retained.
