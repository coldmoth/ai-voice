"""Seed-VC tiny MPS spike adapter. GPL-3.0 upstream; personal use only."""
import os
import sys
from types import SimpleNamespace
from .engines import SPIKE


def shift_reference(reference, sr, steps):
    """Return the reference shifted by `steps` semitones; 0 returns the same array."""
    if type(steps) is not int or not -12 <= steps <= 12:
        raise ValueError('pitch_shift must be an integer in -12..12')
    if steps == 0:
        return reference
    import librosa
    return librosa.effects.pitch_shift(reference, sr=sr, n_steps=steps).astype(reference.dtype)


def load(voice):
    ref_s = voice.get('ref_seconds', 5)
    if type(ref_s) is not int or not 3 <= ref_s <= 15:
        raise ValueError('ref_seconds must be an integer in 3..15')
    os.environ['SYSTEM_VERSION_COMPAT'] = '0'
    src = SPIKE / 'src/seed-vc'
    sys.path.insert(0, str(src))
    import torch
    import librosa
    import inference as inf
    inf.device = torch.device('mps')
    previous = os.getcwd()
    try:
        os.chdir(src)  # upstream HiFT config is relative to the engine source root
        args = SimpleNamespace(fp16=False, f0_condition=False, checkpoint=str(SPIKE / 'seed-tiny.pth'),
                               config=str(SPIKE / 'weights/Seed-VC/config_dit_mel_seed_uvit_xlsr_tiny.yml'))
        model, semantic, _, vocoder, camp, mel, mel_args = inf.load_models(args)
    finally:
        os.chdir(previous)
    sr = mel_args['sampling_rate']
    reference = librosa.load(voice['reference'], sr=sr)[0]
    reference = shift_reference(reference, sr, voice.get('pitch_shift', 0))
    if len(reference) < sr * 10:
        raise ValueError('zero-shot reference needs at least 10 seconds')

    class Engine:
        def set_reference(self, seconds):
            if type(seconds) is not int or not 3 <= seconds <= 15:
                raise ValueError('ref_seconds must be an integer in 3..15')
            with torch.inference_mode():
                self.ref = torch.tensor(reference[:sr * seconds])[None].to('mps')
                ref16 = inf.torchaudio.functional.resample(self.ref, sr, 16000)
                self.sem_ref, self.mel_ref = semantic(ref16), mel(self.ref)
                feat = inf.torchaudio.compliance.kaldi.fbank(ref16, num_mel_bins=80, dither=0, sample_frequency=16000)
                self.style = camp((feat - feat.mean(dim=0, keepdim=True)).unsqueeze(0))
                self.prompt, *_ = model.length_regulator(self.sem_ref,
                    ylens=torch.tensor([self.mel_ref.size(2)], device='mps'), n_quantizers=3, f0=None)

        def convert(self, audio, rate, params):
            with torch.inference_mode():
                x = torch.tensor(librosa.resample(audio, orig_sr=rate, target_sr=sr))[None].to('mps')
                sem = semantic(inf.torchaudio.functional.resample(x, sr, 16000))
                m = mel(x)
                cond, *_ = model.length_regulator(sem, ylens=torch.tensor([m.size(2)], device='mps'),
                                                   n_quantizers=3, f0=None)
                cat = torch.cat([self.prompt, cond], dim=1)
                pred = model.cfm.inference(cat, torch.tensor([cat.size(1)], device='mps'),
                                           self.mel_ref, self.style, None, int(params.get('diffusion_steps', 4)), inference_cfg_rate=0.0)
                out = vocoder(pred[:, :, self.mel_ref.size(-1):].float()).squeeze().cpu().numpy()
                torch.mps.synchronize()
                return out, sr
    engine = Engine()
    engine.set_reference(ref_s)
    return engine
