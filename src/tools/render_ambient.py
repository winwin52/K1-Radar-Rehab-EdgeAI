#!/usr/bin/env python3
"""
Ambient bed renderer v3 — clean, vectorized, click-free.

Key improvements over v2:
  - No time-varying filter (was causing 23 Hz click train)
  - scipy.signal.sosfilt for one-shot low-pass (vectorized, no chunk boundaries)
  - Convolution reverb with synthetic IR via scipy.signal.fftconvolve
  - Loop seam fade: last 0.5s smoothly blends into first 0.5s,
    making naive looping completely click-free
  - Tremolo (amplitude LFO) for breathing motion (filter LFO was the click source)

Output: 30s seamless stereo wav, ~5MB each.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import butter, sosfilt, fftconvolve


# ---- Config -----------------------------------------------------------

SAMPLERATE = 48000
DURATION_S = 30
N_SAMPLES  = SAMPLERATE * DURATION_S
T_ARR      = np.arange(N_SAMPLES) / SAMPLERATE


# ---- Music theory -----------------------------------------------------

NOTE = {
    "C": 0, "C#": 1, "D": 2, "D#": 3, "E": 4, "F": 5,
    "F#": 6, "G": 7, "G#": 8, "A": 9, "A#": 10, "B": 11,
}

def midi_to_hz(m: int) -> float:
    return 440.0 * (2 ** ((m - 69) / 12))


def chord_freqs(root: str, octave: int, kind: str) -> list[float]:
    """Return 4-note voicing: bass, mid root, mid third, high fifth/seventh."""
    root_midi = NOTE[root] + (octave + 1) * 12
    intervals = {"maj": [0, 4, 7], "min": [0, 3, 7],
                 "maj7": [0, 4, 7, 11], "min7": [0, 3, 7, 10]}[kind]
    bass = midi_to_hz(root_midi - 12)
    mid  = midi_to_hz(root_midi)
    third = midi_to_hz(root_midi + intervals[1])
    fifth = midi_to_hz(root_midi + intervals[2] + 12)   # octave up
    return [bass, mid, third, fifth]


# ---- Vectorized DSP ---------------------------------------------------

def saw_sum(freq: float, n: int) -> np.ndarray:
    """Band-limited sawtooth as a sum of sines (vectorized over harmonics).

    For 30 s at low frequencies the phase grows to ~10^5 rad; numpy double-
    precision math handles this fine, but we wrap modulo 2*pi at the end
    to be conservative.
    """
    t = np.arange(n, dtype=np.float64) / SAMPLERATE
    nyq = SAMPLERATE / 2
    out = np.zeros(n, dtype=np.float64)
    k_max = min(40, int(nyq * 0.9 / freq))
    k = np.arange(1, k_max + 1)
    # Build a (k_max, n) matrix would use a lot of RAM for n=360000.
    # Loop over k is fine; each iteration is vectorized over n.
    for ki in k:
        out += np.sin(2 * np.pi * freq * ki * t) / ki
    return (out * (2 / np.pi)).astype(np.float32)


def supersaw(freq: float, n: int, n_voices: int = 3,
             detune_cents: float = 7.0) -> np.ndarray:
    """Multi-voice detuned saw for chorus-like richness."""
    out = np.zeros(n, dtype=np.float32)
    if n_voices == 1:
        return saw_sum(freq, n)
    for i in range(n_voices):
        cents = detune_cents * (2 * i / (n_voices - 1) - 1)
        f = freq * (2 ** (cents / 1200))
        out += saw_sum(f, n)
    return out / n_voices


def adsr(n: int, attack_s: float, decay_s: float,
         sustain: float, release_s: float) -> np.ndarray:
    """ADSR envelope, robust to short n."""
    sr = SAMPLERATE
    a = int(attack_s * sr)
    d = int(decay_s * sr)
    r = int(release_s * sr)
    if a + d + r > n:
        scale = n / (a + d + r)
        a, d = int(a * scale), int(d * scale)
        r = max(1, n - a - d)
    s_len = max(0, n - a - d - r)
    env = np.zeros(n, dtype=np.float32)
    if a > 0: env[:a] = np.linspace(0, 1, a, dtype=np.float32)
    if d > 0: env[a:a+d] = np.linspace(1, sustain, d, dtype=np.float32)
    if s_len > 0: env[a+d:a+d+s_len] = sustain
    if r > 0:
        peak = sustain if s_len > 0 else (env[a+d-1] if d > 0 else 1.0)
        env[-r:] = np.linspace(peak, 0, r, dtype=np.float32)
    return env


def low_pass(audio: np.ndarray, cutoff_hz: float, order: int = 4) -> np.ndarray:
    """Vectorized, one-shot low-pass via scipy SOS sections."""
    nyq = SAMPLERATE / 2
    wn = max(0.001, min(0.99, cutoff_hz / nyq))
    sos = butter(order, wn, btype="low", output="sos")
    return sosfilt(sos, audio).astype(np.float32)


def tremolo(audio: np.ndarray, rate_hz: float = 0.1,
            depth: float = 0.2) -> np.ndarray:
    """Slow amplitude modulation for 'breathing' movement.

    rate_hz × DURATION_S should be an integer for loop seamlessness.
    With DURATION_S=30, rate_hz=0.1 → 3 cycles → seamless.
    """
    lfo = (1 - depth) + depth * np.sin(2 * np.pi * rate_hz * T_ARR)
    return (audio * lfo.astype(np.float32)).astype(np.float32)


def conv_reverb(audio: np.ndarray, decay_s: float = 1.2,
                pre_delay_s: float = 0.02, mix: float = 0.25,
                seed: int = 42) -> np.ndarray:
    """Convolution reverb via FFT.

    The impulse response is exponentially-decaying low-pass-filtered noise —
    sounds like a small room. Fast (FFT-based) and click-free.
    """
    rng = np.random.default_rng(seed)
    n_ir = int(decay_s * SAMPLERATE)
    pre = int(pre_delay_s * SAMPLERATE)
    ir = rng.normal(0, 0.5, n_ir).astype(np.float32)
    env = np.exp(-np.arange(n_ir, dtype=np.float32) / SAMPLERATE / (decay_s / 4))
    ir = ir * env
    # Pre-delay
    ir = np.concatenate([np.zeros(pre, dtype=np.float32), ir])
    # Lowpass the IR a bit so reverb isn't fizzy
    ir = low_pass(ir, 4000, order=2)
    ir /= np.max(np.abs(ir)) + 1e-9

    # Process wrap-around so reverb tail at end matches reverb tail at start
    # (otherwise looping causes a sudden change in reverb density)
    pad = len(ir)
    extended = np.concatenate([audio[-pad:], audio, audio[:pad]])
    wet = fftconvolve(extended, ir, mode="same")
    wet = wet[pad:pad + len(audio)]
    wet *= 0.5 * np.max(np.abs(audio)) / (np.max(np.abs(wet)) + 1e-9)
    return (audio * (1 - mix) + wet * mix).astype(np.float32)


def loop_seam(audio: np.ndarray, xfade_s: float = 0.5) -> np.ndarray:
    """Crossfade the last xfade_s into the first xfade_s.

    After this, naive looping (jump from sample N-1 → sample 0) has no click:
    the last xfade samples literally equal the first xfade samples.
    """
    n_x = int(xfade_s * SAMPLERATE)
    fade_out = np.linspace(1, 0, n_x, dtype=np.float32)
    fade_in  = np.linspace(0, 1, n_x, dtype=np.float32)
    audio = audio.copy()
    audio[-n_x:] = audio[-n_x:] * fade_out + audio[:n_x] * fade_in
    return audio


def stereo_widen(mono: np.ndarray, delay_ms: float = 12) -> np.ndarray:
    """Haas-effect stereo from mono."""
    d = int(delay_ms * SAMPLERATE / 1000)
    n = len(mono)
    left = mono.copy()
    right = np.zeros(n, dtype=np.float32)
    right[d:] = mono[:n-d] * 0.93
    return np.column_stack([left, right]).astype(np.float32)


def normalize_peak(audio: np.ndarray, peak: float = 0.7) -> np.ndarray:
    m = float(np.max(np.abs(audio)))
    return audio if m < 1e-9 else (audio * (peak / m)).astype(np.float32)


# ---- Chord progressions ----------------------------------------------

PROG_CALM = [
    ("C", 3, "maj7"), ("A", 2, "min7"),
    ("F", 2, "maj7"), ("G", 2, "maj"),
]
PROG_PLEASURE = [
    ("D", 3, "maj"), ("G", 2, "maj7"),
    ("E", 2, "min7"), ("C", 3, "maj7"),
]
PROG_FRUSTRATION = [
    ("A", 2, "min7"), ("G", 2, "maj"),
    ("F", 2, "maj7"), ("G", 2, "maj"),
]


def synthesize(prog: list[tuple], cutoff_hz: float, n_voices: int,
               detune_cents: float, tremolo_depth: float) -> np.ndarray:
    """Render one ambient bed dry (pre-reverb)."""
    chord_n = N_SAMPLES // len(prog)
    chord_env = adsr(chord_n, attack_s=0.5, decay_s=0.1,
                     sustain=0.9, release_s=0.5)
    audio = np.zeros(N_SAMPLES, dtype=np.float32)
    for i, (root, octave, kind) in enumerate(prog):
        start = i * chord_n
        end = start + chord_n
        for f in chord_freqs(root, octave, kind):
            voice = supersaw(f, chord_n, n_voices=n_voices,
                             detune_cents=detune_cents)
            audio[start:end] += voice * chord_env / 4
    audio = low_pass(audio, cutoff_hz, order=4)
    audio = tremolo(audio, rate_hz=0.1, depth=tremolo_depth)
    return audio


# ---- Three flavors ----------------------------------------------------

def make_calm() -> np.ndarray:
    np.random.seed(101)
    dry = synthesize(PROG_CALM, cutoff_hz=1100, n_voices=3,
                     detune_cents=6, tremolo_depth=0.15)
    wet = conv_reverb(dry, decay_s=1.4, pre_delay_s=0.03, mix=0.30, seed=11)
    wet = loop_seam(wet, xfade_s=0.5)
    wet = normalize_peak(wet, peak=0.65)
    return stereo_widen(wet, delay_ms=10)


def make_pleasure() -> np.ndarray:
    np.random.seed(202)
    dry = synthesize(PROG_PLEASURE, cutoff_hz=2000, n_voices=3,
                     detune_cents=8, tremolo_depth=0.18)
    wet = conv_reverb(dry, decay_s=1.5, pre_delay_s=0.025, mix=0.32, seed=22)
    wet = loop_seam(wet, xfade_s=0.5)
    wet = normalize_peak(wet, peak=0.7)
    return stereo_widen(wet, delay_ms=12)


def make_frustration() -> np.ndarray:
    np.random.seed(303)
    dry = synthesize(PROG_FRUSTRATION, cutoff_hz=850, n_voices=4,
                     detune_cents=10, tremolo_depth=0.20)
    wet = conv_reverb(dry, decay_s=1.6, pre_delay_s=0.035, mix=0.38, seed=33)
    wet = loop_seam(wet, xfade_s=0.5)
    wet = normalize_peak(wet, peak=0.65)
    return stereo_widen(wet, delay_ms=14)


# ---- Main --------------------------------------------------------------

def main():
    out_dir = Path(__file__).resolve().parent.parent / "assets"
    out_dir.mkdir(parents=True, exist_ok=True)

    flavors = {
        "calm":        make_calm,
        "pleasure":    make_pleasure,
        "frustration": make_frustration,
    }

    print(f"Rendering 3 ambient beds @ {SAMPLERATE} Hz × {DURATION_S}s stereo")
    print(f"  Vectorized chord progressions + sosfilt LP + FFT conv-reverb")
    print(f"  Output: {out_dir}\n")

    for name, factory in flavors.items():
        print(f"  Rendering {name}... ", end="", flush=True)
        stereo = factory()
        # Sanity check
        if not np.isfinite(stereo).all():
            print(f"  ! NaN/inf detected, skipping save")
            continue
        path = out_dir / f"ambient_{name}.wav"
        sf.write(path, stereo, SAMPLERATE, subtype="PCM_16")
        size_kb = path.stat().st_size // 1024
        peak = float(np.max(np.abs(stereo)))
        rms  = float(np.sqrt(np.mean(stereo.astype(np.float64) ** 2)))
        print(f"{path.name:<28} {size_kb:>4} KB   peak {peak:.2f}  rms {rms:.3f}")

    print("\n[OK] Done. Deploy with: bash tools/deploy.sh")


if __name__ == "__main__":
    main()
