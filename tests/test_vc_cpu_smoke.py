"""Real CPU engine smoke; auxiliary models come from the pinned VC runtime."""
import importlib.util
import logging
from pathlib import Path

import pytest


pytestmark = pytest.mark.slow
FIXTURE = Path(__file__).parent / "fixtures" / "rvc-tiny" / "model.pth"


@pytest.fixture
def cpu_engine(monkeypatch, tmp_path):
    torch = pytest.importorskip("torch")
    from vc_worker import engines, rvc_adapter

    assert FIXTURE.is_file(), "Generate the committed tiny RVC fixture first"
    assert FIXTURE.stat().st_size < 2_000_000
    assert (engines.SPIKE / "src/rvc/infer/module/models.py").is_file(), (
        "Prepare the pinned RVC runtime and set AI_VOICE_VC_RUNTIME"
    )
    monkeypatch.syspath_prepend(str(engines.SPIKE / "src/rvc"))
    # Exercise CPU explicitly on macOS too, without changing its device policy.
    monkeypatch.setattr(engines, "pick_device", lambda: "cpu")
    monkeypatch.setattr(rvc_adapter, "pick_device", lambda: "cpu")
    for name in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE"):
        monkeypatch.setenv(name, "1")
    for name in ("NUMBA_CACHE_DIR", "MPLCONFIGDIR", "TORCH_HOME"):
        monkeypatch.setenv(name, str(tmp_path / name))
    threads = torch.get_num_threads()
    torch.set_num_threads(1)
    torch.manual_seed(15)
    yield torch, engines.SPIKE
    torch.set_num_threads(threads)


def test_rvc_convert_cpu_smoke(cpu_engine):
    import numpy as np
    from vc_worker import rvc_adapter

    engine = rvc_adapter.load({"model": str(FIXTURE)})
    rate = 16000
    noise = np.random.default_rng(15).normal(0, 0.02, rate).astype(np.float32)
    output, output_rate = engine.convert(noise, rate, {})
    assert output.ndim == 1
    assert abs(len(output) / output_rate - len(noise) / rate) <= 0.05
    assert np.isfinite(output).all()


def test_train_one_step_cpu(cpu_engine, monkeypatch, tmp_path):
    torch, spike = cpu_engine
    import numpy as np
    import soundfile as sf
    from train import utils
    from train.data_utils import (
        DistributedBucketSampler, TextAudioCollateMultiNSFsid,
        TextAudioLoaderMultiNSFsid,
    )

    checkpoint = torch.load(FIXTURE, map_location="cpu", weights_only=True)
    config = checkpoint["config"]
    rate, hop = config[-1], int(np.prod(config[12]))
    rows = []
    rng = np.random.default_rng(15)
    # Two real float32 WAV clips plus deterministic precomputed content/F0.
    for number in range(2):
        clip = tmp_path / str(number)
        samples = hop * 20
        sf.write(str(clip) + ".wav", rng.normal(0, 0.02, samples), rate, subtype="FLOAT")
        np.save(str(clip) + ".npy", rng.normal(0, 0.1, (20, 768)).astype(np.float32))
        np.save(str(clip) + ".pitch.npy", np.full(20, 60, dtype=np.int64))
        np.save(str(clip) + ".pitchf.npy", np.full(20, 160, dtype=np.float32))
        rows.append("|".join(str(clip) + suffix for suffix in
                            (".wav", ".npy", ".pitch.npy", ".pitchf.npy")) + "|0")
    filelist = tmp_path / "filelist.txt"
    filelist.write_text("\n".join(rows) + "\n", encoding="utf-8")
    hps = utils.HParams(
        version="v2", gpus="0", if_f0=1, if_cache_data_in_gpu=False,
        save_every_epoch=100, total_epoch=2,
        train=dict(segment_size=config[1] * hop, c_mel=1, c_kl=1, log_interval=100),
        data=dict(training_files=str(filelist), max_wav_value=1, sampling_rate=rate,
                  filter_length=(config[0] - 1) * 2, hop_length=hop,
                  win_length=(config[0] - 1) * 2, n_mel_channels=16,
                  mel_fmin=0, mel_fmax=rate // 2),
    )
    monkeypatch.setattr(utils, "get_hparams", lambda: hps)
    # The trainer's upstream locale files are relative to cwd.
    monkeypatch.chdir(spike / "src/rvc")
    spec = importlib.util.spec_from_file_location(
        "vc_worker.cpu_smoke_trainer", Path(__file__).parents[1] / "vc_worker/train.py"
    )
    trainer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(trainer)
    assert trainer.device == "cpu"
    generator = trainer.RVC_Model_f0(*config, is_half=False)
    missing, unexpected = generator.load_state_dict(checkpoint["weight"], strict=False)
    assert missing and all(key.startswith("enc_q.") for key in missing)
    assert not unexpected
    discriminator = trainer.MultiPeriodDiscriminator()
    optimizers = [torch.optim.Adam(net.parameters(), lr=1e-4)
                  for net in (generator, discriminator)]
    before = [next(net.parameters()).detach().clone() for net in (generator, discriminator)]
    dataset = TextAudioLoaderMultiNSFsid(str(filelist), hps.data)
    assert len(dataset) == 2
    sampler = DistributedBucketSampler(dataset, 2, [0, 100], num_replicas=1, rank=0)
    loader = torch.utils.data.DataLoader(
        dataset, batch_sampler=sampler, collate_fn=TextAudioCollateMultiNSFsid(), num_workers=0
    )
    assert len(loader) == 1
    losses = []
    check_divergence = trainer.check_divergence

    def record_losses(*values):
        check_divergence(*values)
        losses.append([float(value.detach()) for value in values])

    monkeypatch.setattr(trainer, "check_divergence", record_losses)
    trainer.train_and_evaluate(
        1, 1, hps, (generator, discriminator), optimizers, (None, None),
        torch.amp.GradScaler("cpu", enabled=False), (loader, None),
        logging.getLogger(__name__), None, [],
    )
    assert trainer.global_step == 1
    assert len(losses) == 1 and np.isfinite(losses).all()
    for net, optimizer, original in zip((generator, discriminator), optimizers, before):
        assert not torch.equal(next(net.parameters()).detach(), original)
        assert optimizer.state and all(state["step"] == 1 for state in optimizer.state.values())
