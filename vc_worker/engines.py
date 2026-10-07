"""Lazy loaders. Engine code and checkpoints stay outside the main application."""
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
SPIKE = Path(os.environ['AI_VOICE_VC_RUNTIME']) / 'spike' if os.environ.get('AI_VOICE_VC_RUNTIME') else ROOT / 'state/vc-spike'


def pick_device(platform: str = sys.platform) -> str:
    import torch
    if torch.cuda.is_available():
        return 'cuda'
    if platform == 'darwin':
        if torch.backends.mps.is_available():
            return 'mps'
        raise RuntimeError('MPS unavailable; CPU fallback is disabled')
    return 'cpu'


def load_engine(voice):
    kind = voice.get('kind')
    if kind not in ('imported', 'trained', 'zeroshot'):
        raise ValueError('kind must be imported, trained or zeroshot')
    required = 'reference' if kind == 'zeroshot' else 'model'
    if not isinstance(voice.get(required), str) or not Path(voice[required]).is_file():
        raise ValueError(f'{required} must be an existing local file')
    os.environ['SYSTEM_VERSION_COMPAT'] = '0'
    os.environ['HF_HOME'] = str(SPIKE / 'hf')
    os.environ['HF_HUB_OFFLINE'] = '1'
    os.environ['TRANSFORMERS_OFFLINE'] = '1'
    for name, directory in [('XDG_CACHE_HOME', 'cache'), ('TORCH_HOME', 'cache/torch'),
                            ('MPLCONFIGDIR', 'cache/matplotlib'), ('NUMBA_CACHE_DIR', 'cache/numba')]:
        os.environ[name] = str(SPIKE / directory)
    import torch
    pick_device()
    # All engine loads, including upstream auxiliary checkpoints, must be safe.
    original = torch.load
    def safe_load(*args, **kwargs):
        kwargs['weights_only'] = True
        return original(*args, **kwargs)
    torch.load = safe_load
    try:
        if kind == 'zeroshot':
            from .seed_adapter import load
        else:
            from .rvc_adapter import load
        return load(voice)
    finally:
        torch.load = original
