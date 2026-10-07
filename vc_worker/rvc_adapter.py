"""RVC v2/RMVPE adapter migrated from the phase-0 spike (MIT upstream)."""
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from .engines import SPIKE, pick_device


def load(voice):
    os.environ['SYSTEM_VERSION_COMPAT'] = '0'
    sys.path.insert(0, str(SPIKE / 'src/rvc'))
    os.environ['rmvpe_root'] = str(SPIKE / 'weights/VoiceConversionWebUI')
    import torch
    device = pick_device()
    import librosa
    import numpy as np
    from infer.module.models import SynthesizerTrnMs768NSFsid
    from infer.hubert import HubertModelWithFinalProj
    from infer.vc.pipeline import Pipeline
    from infer.rmvpe import RMVPE
    checkpoint = torch.load(Path(voice['model']), map_location='cpu', weights_only=True)
    if checkpoint.get('version') != 'v2' or checkpoint.get('f0', 1) != 1:
        raise ValueError('requires exported RVC v2 f0 checkpoint')
    config = list(checkpoint['config'])
    weights = checkpoint['weight']
    config[-3] = weights['emb_g.weight'].shape[0]
    sr = int(config[-1])
    net = SynthesizerTrnMs768NSFsid(*config, is_half=False)
    del net.enc_q
    net.load_state_dict({k: v for k, v in weights.items() if not k.startswith('enc_q.')}, strict=True)
    net.eval().float().to(device)
    hubert = HubertModelWithFinalProj.from_pretrained(
        str(SPIKE / 'weights/VoiceConversionWebUI/hubert_base'), local_files_only=True).eval().to(device)
    pipe = Pipeline(sr, SimpleNamespace(x_pad=1, x_query=6, x_center=38, x_max=41,
                                       is_half=False, device=device))
    pipe.model_rmvpe = RMVPE(str(SPIKE / 'weights/VoiceConversionWebUI/rmvpe.pt'), False, device=device)
    from .f0_safe import f0_features

    def get_f0(x, p_len, f0_up_key, f0_method, *args, **kwargs):
        # Upstream interpolation raises (and prints) on fully unvoiced blocks.
        if f0_method != 'rmvpe':
            raise ValueError(f'Unsupported F0 method: {f0_method}')
        return f0_features(pipe.model_rmvpe.infer_from_audio(x, thred=0.03), f0_up_key)

    pipe.get_f0 = get_f0
    index = voice.get('index') or ''
    if index and not Path(index).is_file():
        raise ValueError('index must be an existing local file')

    class Engine:
        def convert(self, audio, rate, params):
            with torch.inference_mode():
                x = librosa.resample(audio, orig_sr=rate, target_sr=16000)
                out = pipe.pipeline(hubert, net, 0, x, [0., 0., 0.], params.get('pitch_shift', 0),
                                    'rmvpe', index, params.get('index_rate', 0), 1, sr, 0, 1, 'v2', 0.33)
                if device != 'cpu':
                    getattr(torch, device).synchronize()
                return out.astype(np.float32) / 32768, sr
    return Engine()
