"""Offline RVC pipeline. Run only with the isolated .venv-vc interpreter."""
import os
os.environ['SYSTEM_VERSION_COMPAT'] = '0'
os.environ['HF_HUB_OFFLINE'] = '1'
os.environ['TRANSFORMERS_OFFLINE'] = '1'
os.environ['PYTORCH_ENABLE_MPS_FALLBACK'] = '0'

import argparse
import contextlib
import importlib.util
import json
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--voice-dir', required=True, type=Path)
    parser.add_argument('--rvc-src', required=True, type=Path)
    parser.add_argument('--pretrained-dir', required=True, type=Path)
    parser.add_argument('--epochs', type=int, default=200)
    parser.add_argument('--batch', type=int, default=4)
    args = parser.parse_args()
    args.voice_dir = args.voice_dir.resolve()
    args.rvc_src = args.rvc_src.resolve()
    args.pretrained_dir = args.pretrained_dir.resolve()
    protocol = sys.stdout

    def emit(stage, **values):
        print(json.dumps(dict(stage=stage, total_epochs=args.epochs, **values)),
              file=protocol, flush=True)

    # Third-party prints must never pollute the JSON-lines channel.
    with contextlib.redirect_stdout(sys.stderr):
        pipeline(args, emit)


def weight(root, name):
    candidates = [root / name, root / 'VoiceConversionWebUI' / name,
                  root / 'VoiceConversionWebUI/pretrained_v2' / name,
                  root.parent / name]
    for path in candidates:
        if path.exists():
            return path
    raise FileNotFoundError('Required local training weights are missing')


def resume_epoch(work, epochs, torch, utils):
    if not list(work.glob('G_*.pth')) or not list(work.glob('D_*.pth')):
        return 0
    checkpoint = torch.load(utils.latest_checkpoint_path(str(work), 'G_*.pth'),
                            map_location='cpu', weights_only=True)
    iteration = int(checkpoint['iteration'])
    if iteration >= epochs:
        # A smaller preset must start fresh rather than skip the final export.
        for pattern in ('G_*.pth', 'D_*.pth'):
            for path in work.glob(pattern):
                path.unlink()
        return 0
    return max(0, iteration - 1)


def check_dataset_scale(dataset, clips):
    """Fail early if the clips would reach the trainer outside [-1, 1]."""
    from scipy.io.wavfile import read
    import numpy as np
    for path in clips[:3] + clips[-1:]:
        _, data = read(str(path) + '.wav')
        peak = float(np.abs(data.astype(np.float32)).max()) if len(data) else 0.0
        if data.dtype != np.float32 or peak > 1.0:
            raise ValueError('Training clips are not float32 in [-1, 1]')


def pipeline(args, emit):
    # Also support the app's direct script launch, without importing app code.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from vc_worker.engines import pick_device
    sys.path.insert(0, str(args.rvc_src))
    import torch
    import numpy as np
    import soundfile as sf
    import librosa
    device = pick_device()
    torch.set_default_dtype(torch.float32)
    work = args.voice_dir / 'train-work'
    output = args.voice_dir / 'train-out'
    dataset = work / 'dataset'
    dataset.mkdir(parents=True, exist_ok=True)
    output.mkdir(exist_ok=True)
    sources = sorted((args.voice_dir / 'dataset').glob('*.wav'))
    if not sources:
        raise ValueError('Empty dataset')
    emit('prepare', progress=0)
    clips = []
    for number, source in enumerate(sources):
        audio, sr = sf.read(source, dtype='float32')
        if audio.ndim > 1:
            audio = audio.mean(axis=1)
        # Match the spike's six-second clips, without discarding the tail.
        for offset in range(0, len(audio), sr * 6):
            chunk = audio[offset:offset + sr * 6]
            path = dataset / str(len(clips))
            # Upstream's loader reads these with scipy and does not rescale, so
            # they must be float32 in [-1, 1]; PCM_16 would arrive as +-32768.
            sf.write(str(path) + '.wav',
                     np.clip(librosa.resample(chunk, orig_sr=sr, target_sr=48000), -1.0, 1.0),
                     48000, subtype='FLOAT')
            clips.append(path)
        emit('prepare', progress=(number + 1) / len(sources))
    check_dataset_scale(dataset, clips)

    from infer.rmvpe import RMVPE
    emit('pitch', progress=0)
    rmvpe = RMVPE(str(weight(args.pretrained_dir, 'rmvpe.pt')),
                             False, device=device)
    with torch.inference_mode():
        for i, path in enumerate(clips):
            audio, sr = sf.read(str(path) + '.wav', dtype='float32')
            a16 = librosa.resample(audio, orig_sr=sr, target_sr=16000)
            # Same RMVPE branch and coarse quantization as upstream get_f0,
            # without importing its inference Pipeline (which requires faiss).
            pitchf = rmvpe.infer_from_audio(a16, thred=0.03)
            unvoiced = pitchf == 0
            if (~unvoiced).any():
                pitchf[unvoiced] = np.interp(np.where(unvoiced)[0],
                                            np.where(~unvoiced)[0], pitchf[~unvoiced])
            mel = 1127 * np.log(1 + pitchf / 700)
            mel_min = 1127 * np.log(1 + 50 / 700)
            mel_max = 1127 * np.log(1 + 1100 / 700)
            voiced = mel > 0
            mel[voiced] = (mel[voiced] - mel_min) * 254 / (mel_max - mel_min) + 1
            pitch = np.rint(np.clip(mel, 1, 255)).astype(np.int32)
            np.save(str(path) + '.pitch.npy', pitch)
            np.save(str(path) + '.pitchf.npy', pitchf)
            emit('pitch', progress=(i + 1) / len(clips))
    del rmvpe
    if device != 'cpu':
        getattr(torch, device).empty_cache()

    from infer.hubert import HubertModelWithFinalProj, extract_hubert_features
    emit('features', progress=0)
    hubert = HubertModelWithFinalProj.from_pretrained(
        str(weight(args.pretrained_dir, 'hubert_base')),
        local_files_only=True).eval().float().to(device)
    rows = []
    with torch.inference_mode():
        for i, path in enumerate(clips):
            audio, sr = sf.read(str(path) + '.wav', dtype='float32')
            a16 = librosa.resample(audio, orig_sr=sr, target_sr=16000)
            features = extract_hubert_features(
                hubert, torch.tensor(a16)[None].to(device), 'v2').squeeze(0).cpu().numpy()
            np.save(str(path) + '.npy', features)
            rows.append('|'.join([str(path) + suffix for suffix in
                                  ('.wav', '.npy', '.pitch.npy', '.pitchf.npy')] + ['0']))
            emit('features', progress=(i + 1) / len(clips))
    del hubert, features
    if device != 'cpu':
        getattr(torch, device).empty_cache()
    (work / 'filelist.txt').write_text('\n'.join(rows) + '\n', encoding='utf-8')

    cfg = json.loads((args.rvc_src / 'configs/v2/48k.json').read_text(encoding='utf-8'))
    cfg['train'].update(epochs=args.epochs, batch_size=args.batch,
                        segment_size=17280, log_interval=1000)
    cfg['data']['training_files'] = str(work / 'filelist.txt')
    cfg.update(model_dir=str(work), experiment_dir=str(work),
               save_every_epoch=10, name='model', total_epoch=args.epochs,
               pretrainG=str(weight(args.pretrained_dir, 'f0G48k.pth')),
               pretrainD=str(weight(args.pretrained_dir, 'f0D48k.pth')),
               version='v2', gpus='0', sample_rate='48k', if_f0=1, if_latest=1,
               save_every_weights='0', if_cache_data_in_gpu=0)
    (work / 'config.json').write_text(json.dumps(cfg), encoding='utf-8')
    # Upstream locale lookup and exported weights use cwd-relative paths.
    if not (work / 'i18n').exists():
        if sys.platform == 'win32':
            import shutil
            shutil.copytree(args.rvc_src / 'i18n', work / 'i18n')
        else:
            (work / 'i18n').symlink_to(args.rvc_src / 'i18n', target_is_directory=True)
    (work / 'assets/weights').mkdir(parents=True, exist_ok=True)
    os.chdir(work)
    from train import utils
    utils.get_hparams = lambda: utils.HParams(**cfg)
    start_epoch = resume_epoch(work, args.epochs, torch, utils)
    emit('train', epoch=start_epoch, resumed=True)
    spec = importlib.util.spec_from_file_location(
        'vc_worker.train', Path(__file__).resolve().with_name('train.py'))
    trainer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(trainer)
    trainer.epoch_callback = lambda event: emit('train', **event)
    trainer.main()
    os.replace(work / 'assets/weights/model.pth', output / 'model.pth')

    emit('index', progress=0)
    try:
        import faiss
    except ImportError:
        emit('index', progress=1, skipped='faiss_unavailable',
             warning='faiss unavailable; model saved without an index')
        return
    # Flat L2 is supported by RVC retrieval and works for small datasets too.
    index = faiss.IndexFlatL2(768)
    for path in clips:
        features = np.load(str(path) + '.npy').astype('float32')
        index.add(np.ascontiguousarray(features))
    faiss.write_index(index, str(output / 'model.index'))
    emit('index', progress=1)


if __name__ == '__main__':
    main()
