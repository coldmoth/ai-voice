"""Offline device selection and the worker-to-UI device contract."""
import ast
from contextlib import nullcontext
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from vc_worker import engines, protocol
from ai_voice.vc_control import VcController, VcWorker

ROOT = Path(__file__).resolve().parents[1]


def fake_torch(monkeypatch, *, cuda=False, mps=False):
    torch = SimpleNamespace(
        cuda=SimpleNamespace(is_available=Mock(return_value=cuda)),
        backends=SimpleNamespace(mps=SimpleNamespace(is_available=Mock(return_value=mps))),
    )
    monkeypatch.setitem(sys.modules, "torch", torch)
    return torch


def test_pick_device_cuda(monkeypatch):
    torch = fake_torch(monkeypatch, cuda=True, mps=True)
    assert engines.pick_device("win32") == "cuda"
    assert engines.pick_device("darwin") == "cuda"
    torch.backends.mps.is_available.assert_not_called()


def test_pick_device_mps_mac(monkeypatch):
    fake_torch(monkeypatch, mps=True)
    assert engines.pick_device("darwin") == "mps"


def test_pick_device_mac_no_mps_raises(monkeypatch):
    fake_torch(monkeypatch)
    with pytest.raises(RuntimeError, match="MPS unavailable; CPU fallback is disabled"):
        engines.pick_device("darwin")


def test_pick_device_cpu_on_windows_without_cuda(monkeypatch):
    torch = fake_torch(monkeypatch, mps=True)
    assert engines.pick_device("win32") == "cpu"
    torch.backends.mps.is_available.assert_not_called()


def test_no_mps_literals_left():
    for path in (ROOT / "vc_worker").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if path.name == "engines.py" and isinstance(node, ast.FunctionDef) and node.name == "pick_device":
                continue
            assert not any(isinstance(item, ast.Constant) and item.value == "mps"
                           for item in ast.walk(node)), path


@pytest.mark.parametrize("device", ["cuda", "mps", "cpu"])
def test_loaded_status_carries_device_to_controller(monkeypatch, tmp_path, device):
    monkeypatch.setattr(protocol, "pick_device", lambda: device, raising=False)
    service = protocol.Service(io.StringIO(), loader=lambda voice: object(), rss=lambda: 100)
    worker = VcWorker()
    controller = VcController(
        store=object(), trainer=SimpleNamespace(status=lambda: None, close=lambda: None),
        worker=worker, uploads=tmp_path / "uploads", inject_dir=tmp_path / "inject",
    )
    try:
        assert controller.snapshot()["vc"]["device"] is None
        service.command({"cmd": "load", "voice": {"kind": "trained", "model": "fake"}})
        events = [json.loads(line) for line in service.output.getvalue().splitlines()]
        assert [event["event"] for event in events] == ["status", "status"]
        assert events[-1]["state"] == "loaded"
        assert events[-1]["device"] == device
        for event in events:
            worker._handle_event(event)
        assert controller.snapshot()["vc"]["device"] == device
        worker._handle_event({"event": "status", "state": "running"})
        assert controller.snapshot()["vc"]["device"] == device
        # A requested stream stop retains the model for restart_stream().
        worker._handle_event({"event": "stopped", "reason": "requested"})
        assert controller.snapshot()["vc"]["device"] == device
        worker.stop()
        assert controller.snapshot()["vc"]["device"] is None
    finally:
        controller.close()


@pytest.mark.parametrize("event", [
    {"event": "status", "state": "loading"},
    {"event": "status", "state": "idle"},
    {"event": "stopped", "reason": "rss_budget"},
    {"event": "stopped", "reason": "error"},
])
def test_device_cleared_when_worker_unloads(event):
    worker = VcWorker()
    worker._handle_event({"event": "status", "state": "loaded", "device": "cuda"})
    assert worker.snapshot()["device"] == "cuda"
    worker._handle_event(event)
    assert worker.snapshot()["device"] is None


def test_device_cleared_on_process_exit():
    worker = VcWorker()
    worker._handle_event({"event": "status", "state": "loaded", "device": "cpu"})
    worker._proc = SimpleNamespace(stdout=iter([]), poll=lambda: 1)
    worker._reader_loop()
    assert worker.snapshot()["device"] is None


def test_old_worker_without_device_remains_unknown():
    worker = VcWorker()
    worker._handle_event({"event": "status", "state": "loaded"})
    assert worker.snapshot()["device"] is None


@pytest.mark.parametrize("device", [None, "invalid", 1, ["cuda"]])
def test_invalid_device_is_not_exposed(device):
    worker = VcWorker()
    worker._handle_event({"event": "status", "state": "loaded", "device": device})
    assert worker.snapshot()["device"] is None


def test_device_cleared_on_broken_pipe():
    worker = VcWorker()
    worker._handle_event({"event": "status", "state": "loaded", "device": "cuda"})
    worker._mark_crashed()
    assert worker.snapshot()["device"] is None


def test_device_cleared_after_process_stop():
    worker = VcWorker()
    worker._handle_event({"event": "status", "state": "loaded", "device": "cpu"})
    worker._proc = SimpleNamespace(stdin=io.StringIO(), stdout=io.StringIO(),
                                   wait=Mock(return_value=0), pid=123)
    worker.stop()
    assert worker.snapshot()["state"] == "idle"
    assert worker.snapshot()["device"] is None
    assert worker._proc is None


class Tensor:
    """Small upstream double that records tensor placement without a GPU."""
    def __init__(self, placements):
        self.placements = placements
        self.shape = (1, 768)
        self.enc_q = object()

    def to(self, device):
        self.placements.append(device)
        return self

    def __getitem__(self, key):
        return self

    def __sub__(self, other):
        return self

    def size(self, dim):
        return 10

    def numpy(self):
        return np.zeros((2, 768), dtype=np.float32)

    def load_state_dict(self, *args, **kwargs):
        return None

    def parameters(self):
        return []

    def eval(self):
        return self

    float = cpu = eval

    def squeeze(self, *args, **kwargs):
        return self

    unsqueeze = mean = squeeze


def adapter_torch(monkeypatch, device):
    torch = fake_torch(monkeypatch, cuda=device == "cuda", mps=device == "mps")
    torch.cuda.synchronize = Mock()
    torch.cuda.empty_cache = Mock()
    torch.mps = SimpleNamespace(synchronize=Mock(), empty_cache=Mock())
    torch.float32, torch.float16 = "float32", "float16"
    torch.inference_mode = nullcontext
    torch.device = lambda value: value
    placements = []
    def tensor(*args, **kwargs):
        if "device" in kwargs:
            placements.append(kwargs["device"])
        return Tensor(placements)
    torch.tensor = tensor
    torch.cat = lambda *args, **kwargs: Tensor(placements)
    return torch, placements


@pytest.mark.parametrize("device", ["cuda", "mps", "cpu"])
def test_rvc_adapter_uses_selected_device(monkeypatch, device):
    from vc_worker import rvc_adapter
    torch, placements = adapter_torch(monkeypatch, device)
    monkeypatch.setattr(rvc_adapter, "pick_device", lambda: device)
    monkeypatch.setattr(sys, "path", sys.path[:])
    torch.load = Mock(return_value={"version": "v2", "f0": 1, "config": [1, 2, 40000],
                                   "weight": {"emb_g.weight": Tensor(placements)}})
    rmvpe = Mock(return_value=SimpleNamespace())
    pipe = SimpleNamespace(pipeline=Mock(return_value=np.zeros(80, np.float32)))
    pipeline = Mock(return_value=pipe)
    monkeypatch.setitem(sys.modules, "infer.module.models", SimpleNamespace(
        SynthesizerTrnMs768NSFsid=lambda *a, **kw: Tensor(placements)))
    monkeypatch.setitem(sys.modules, "infer.hubert", SimpleNamespace(
        HubertModelWithFinalProj=SimpleNamespace(from_pretrained=lambda *a, **kw: Tensor(placements))))
    monkeypatch.setitem(sys.modules, "infer.vc.pipeline", SimpleNamespace(Pipeline=pipeline))
    monkeypatch.setitem(sys.modules, "infer.rmvpe", SimpleNamespace(RMVPE=rmvpe))
    monkeypatch.setitem(sys.modules, "librosa", SimpleNamespace(resample=lambda x, **kw: x))
    engine = rvc_adapter.load({"model": "local.pth"})
    out, sr = engine.convert(np.zeros(80, np.float32), 8000, {})
    assert (len(out), sr) == (80, 40000)
    assert placements == [device, device]
    assert pipeline.call_args.args[1].device == device
    assert rmvpe.call_args.kwargs == {"device": device}
    assert torch.load.call_args.kwargs["weights_only"] is True
    assert torch.cuda.synchronize.call_count == int(device == "cuda")
    assert torch.mps.synchronize.call_count == int(device == "mps")


@pytest.mark.parametrize("device", ["cuda", "mps", "cpu"])
def test_seed_adapter_uses_selected_device(monkeypatch, tmp_path, device):
    from vc_worker import seed_adapter
    torch, placements = adapter_torch(monkeypatch, device)
    monkeypatch.setattr(seed_adapter, "pick_device", lambda: device)
    monkeypatch.setattr(seed_adapter, "SPIKE", tmp_path)
    monkeypatch.setattr(sys, "path", sys.path[:])
    (tmp_path / "src/seed-vc").mkdir(parents=True)
    tensor = lambda *a, **kw: Tensor(placements)
    model = SimpleNamespace(length_regulator=lambda *a, **kw: (tensor(),),
                            cfm=SimpleNamespace(inference=tensor))
    inf = SimpleNamespace(
        load_models=lambda args: (model, tensor, None, tensor, tensor, tensor, {"sampling_rate": 8000}),
        torchaudio=SimpleNamespace(functional=SimpleNamespace(resample=lambda x, *a: x),
                                  compliance=SimpleNamespace(kaldi=SimpleNamespace(fbank=tensor))),
    )
    monkeypatch.setitem(sys.modules, "inference", inf)
    monkeypatch.setitem(sys.modules, "librosa", SimpleNamespace(
        load=lambda *a, **kw: (np.zeros(80000, np.float32), 8000), resample=lambda x, **kw: x))
    engine = seed_adapter.load({"reference": "local.wav"})
    out, sr = engine.convert(np.zeros(80, np.float32), 8000, {})
    assert sr == 8000 and np.isfinite(out).all()
    assert inf.device == device
    assert len(placements) == 5 and set(placements) == {device}
    assert torch.cuda.synchronize.call_count == int(device == "cuda")
    assert torch.mps.synchronize.call_count == int(device == "mps")


@pytest.mark.parametrize("device", ["cuda", "mps", "cpu"])
def test_train_runner_prepares_features_on_selected_device(monkeypatch, tmp_path, device):
    # Execute the actual pipeline with local files and upstream doubles.
    import runpy
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "0")
    for key in ("SYSTEM_VERSION_COMPAT", "HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE",
                "PYTORCH_ENABLE_MPS_FALLBACK"):
        monkeypatch.setenv(key, "")
    runner = runpy.run_path(str(ROOT / "vc_worker/train_runner.py"))
    torch, placements = adapter_torch(monkeypatch, device)
    torch.set_default_dtype = Mock()
    monkeypatch.setattr(engines, "pick_device", lambda: device)
    monkeypatch.setattr(sys, "path", sys.path[:])
    # Exercise the Windows copy path without requiring symlink privileges.
    monkeypatch.setitem(runner["pipeline"].__globals__, "sys", SimpleNamespace(path=sys.path, platform="win32"))
    monkeypatch.chdir(tmp_path)
    args = SimpleNamespace(voice_dir=tmp_path / "voice", rvc_src=tmp_path / "rvc",
                           pretrained_dir=tmp_path / "weights", epochs=1, batch=1)
    (args.voice_dir / "dataset").mkdir(parents=True)
    (args.voice_dir / "dataset/0.wav").touch()
    (args.rvc_src / "i18n").mkdir(parents=True)
    (args.rvc_src / "i18n/test.json").write_text("{}")
    (args.rvc_src / "configs/v2").mkdir(parents=True)
    (args.rvc_src / "configs/v2/48k.json").write_text('{"train": {}, "data": {}}')
    args.pretrained_dir.mkdir()
    for name in ("rmvpe.pt", "hubert_base", "f0G48k.pth", "f0D48k.pth"):
        (args.pretrained_dir / name).touch()
    audio = np.full(80, .1, np.float32)
    monkeypatch.setitem(sys.modules, "soundfile", SimpleNamespace(
        read=lambda *a, **kw: (audio, 48000), write=lambda *a, **kw: None))
    monkeypatch.setitem(sys.modules, "librosa", SimpleNamespace(resample=lambda x, **kw: x))
    monkeypatch.setitem(sys.modules, "scipy.io.wavfile", SimpleNamespace(read=lambda path: (48000, audio)))
    rmvpe = Mock(return_value=SimpleNamespace(infer_from_audio=lambda *a, **kw: np.array([0., 100.])))
    monkeypatch.setitem(sys.modules, "infer.rmvpe", SimpleNamespace(RMVPE=rmvpe))
    monkeypatch.setitem(sys.modules, "infer.hubert", SimpleNamespace(
        HubertModelWithFinalProj=SimpleNamespace(from_pretrained=lambda *a, **kw: Tensor(placements)),
        extract_hubert_features=lambda *a: Tensor(placements)))
    monkeypatch.setitem(sys.modules, "train", SimpleNamespace(utils=SimpleNamespace(HParams=lambda **kw: kw)))
    specs = []
    def spec(name, path):
        specs.append((name, Path(path)))
        return SimpleNamespace(loader=SimpleNamespace(exec_module=lambda module: None))
    def train():
        assert os.environ["CUDA_VISIBLE_DEVICES"] == "0"
        (args.voice_dir / "train-work/assets/weights/model.pth").write_bytes(b"local fixture")
    trainer = SimpleNamespace(main=train)
    monkeypatch.setattr(importlib.util, "spec_from_file_location", spec)
    monkeypatch.setattr(importlib.util, "module_from_spec", lambda spec: trainer)
    monkeypatch.setitem(sys.modules, "faiss", None)
    events = []
    runner["pipeline"](args, lambda stage, **kw: events.append((stage, kw)))
    assert specs == [("vc_worker.train", ROOT / "vc_worker/train.py")]
    assert (args.voice_dir / "train-out/model.pth").read_bytes() == b"local fixture"
    assert (args.voice_dir / "train-work/i18n/test.json").is_file()
    assert placements == [device, device]
    assert rmvpe.call_args.kwargs["device"] == device
    assert torch.cuda.empty_cache.call_count == 2 * int(device == "cuda")
    assert torch.mps.empty_cache.call_count == 2 * int(device == "mps")
    assert events[-1][0] == "index" and events[-1][1]["skipped"] == "faiss_unavailable"


@pytest.mark.parametrize("device", ["cuda", "mps", "cpu"])
def test_train_precision_placement_and_sync(monkeypatch, tmp_path, device):
    """Load the real renamed trainer, then execute one epoch with upstream doubles."""
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "0")
    monkeypatch.setenv("SYSTEM_VERSION_COMPAT", "0")
    torch, placements = adapter_torch(monkeypatch, device)
    torch.backends.cudnn = SimpleNamespace()
    torch.manual_seed = Mock()
    torch.cuda.set_device = Mock()
    monkeypatch.setattr(engines, "pick_device", lambda: device)
    hps = SimpleNamespace(
        model_dir=str(tmp_path), gpus="0", version="v2", if_f0=1, sample_rate="48k",
        pretrainG="", pretrainD="", total_epoch=1,
        data=SimpleNamespace(training_files="filelist", filter_length=1024, hop_length=480),
        train=SimpleNamespace(seed=1, batch_size=1, segment_size=17280, learning_rate=.001,
                              betas=(.8, .99), eps=1e-9, lr_decay=.9),
    )
    # The upstream config supports mapping expansion and attribute access.
    class ModelConfig(dict):
        use_spectral_norm = False
    hps.model = ModelConfig()
    utils = SimpleNamespace(get_hparams=lambda: hps, get_logger=lambda *a: Mock(),
                            latest_checkpoint_path=Mock(side_effect=FileNotFoundError), load_checkpoint=Mock())
    monkeypatch.setitem(sys.modules, "train", SimpleNamespace(utils=utils))
    monkeypatch.setitem(sys.modules, "i18n.i18n", SimpleNamespace(I18nAuto=lambda: lambda x: x))
    scaler = Mock()
    monkeypatch.setitem(sys.modules, "torch.amp", SimpleNamespace(GradScaler=scaler))
    scheduler = Mock()
    torch.optim = SimpleNamespace(AdamW=Mock(), lr_scheduler=SimpleNamespace(ExponentialLR=scheduler))
    for name in ("distributed", "multiprocessing"):
        submodule = SimpleNamespace()
        setattr(torch, name, submodule)
        monkeypatch.setitem(sys.modules, "torch." + name, submodule)
    torch.nn = SimpleNamespace(functional=SimpleNamespace())
    monkeypatch.setitem(sys.modules, "torch.nn", torch.nn)
    monkeypatch.setitem(sys.modules, "torch.nn.parallel", SimpleNamespace(DistributedDataParallel=Mock()))
    monkeypatch.setitem(sys.modules, "torch.utils.data", SimpleNamespace(DataLoader=lambda *a, **kw: []))
    monkeypatch.setitem(sys.modules, "torch.utils.tensorboard", SimpleNamespace(SummaryWriter=Mock()))
    model_args = []
    def model(*a, **kw):
        model_args.append(kw)
        return Tensor(placements)
    monkeypatch.setitem(sys.modules, "infer.module", SimpleNamespace(commons=SimpleNamespace()))
    monkeypatch.setitem(sys.modules, "infer.module.models", SimpleNamespace(
        SynthesizerTrnMs768NSFsid=model, SynthesizerTrnMs768NSFsid_nono=model,
        MultiPeriodDiscriminatorV2=model))
    monkeypatch.setitem(sys.modules, "train.data_utils", SimpleNamespace(**{
        name: Mock() for name in ("DistributedBucketSampler", "TextAudioCollate", "TextAudioCollateMultiNSFsid",
                                  "TextAudioLoader", "TextAudioLoaderMultiNSFsid")}))
    monkeypatch.setitem(sys.modules, "train.losses", SimpleNamespace(**{
        name: Mock() for name in ("discriminator_loss", "feature_loss", "generator_loss", "kl_loss")}))
    monkeypatch.setitem(sys.modules, "train.mel_processing", SimpleNamespace(mel_spectrogram_torch=Mock(),
                                                                           spec_to_mel_torch=Mock()))
    monkeypatch.setitem(sys.modules, "train.process_ckpt", SimpleNamespace(savee=Mock()))
    spec = importlib.util.spec_from_file_location("vc_worker.train", ROOT / "vc_worker/train.py")
    trainer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(trainer)
    peak_rss_bytes = 2 * 1024 ** 2
    if trainer.sys.platform == "win32":
        monkeypatch.setattr(trainer, "rss_bytes", lambda: peak_rss_bytes)
    else:
        monkeypatch.setattr(trainer.resource, "getrusage",
                            lambda who: SimpleNamespace(ru_maxrss=peak_rss_bytes))
    assert os.environ["CUDA_VISIBLE_DEVICES"] == "0"
    assert trainer.training_is_half == (device == "cuda")
    trainer.train_and_evaluate = Mock()
    epochs = []
    trainer.epoch_callback = epochs.append
    trainer.main()
    assert placements == [device, device]
    assert model_args[0]["is_half"] == (device == "cuda")
    scaler.assert_called_once_with("cuda", enabled=device == "cuda")
    trainer.train_and_evaluate.assert_called_once()
    assert torch.cuda.synchronize.call_count == 2 * int(device == "cuda")
    assert torch.mps.synchronize.call_count == 2 * int(device == "mps")
    assert epochs[0]["epoch"] == 1 and epochs[0]["peak_rss_mib"] == 2
    recorded_epoch = json.loads((tmp_path / "epochs.jsonl").read_text())
    assert recorded_epoch["epoch"] == 1 and recorded_epoch["peak_rss_mib"] == 2

    # Evaluate the trainer's autocast expressions with its actual precision globals.
    tree = ast.parse((ROOT / "vc_worker/train.py").read_text())
    contexts = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute) and node.func.attr == "autocast"]
    torch.autocast = Mock(return_value=nullcontext())
    for node in contexts:
        eval(compile(ast.Expression(node), "vc_worker.train", "eval"), trainer.__dict__)
    assert len(contexts) == 5
    assert sum(call.kwargs["enabled"] for call in torch.autocast.call_args_list) == 2 * int(device == "cuda")
    for call in torch.autocast.call_args_list:
        assert call.args == ("cuda",)
        if call.kwargs["enabled"]:
            assert call.kwargs["dtype"] == torch.float16
