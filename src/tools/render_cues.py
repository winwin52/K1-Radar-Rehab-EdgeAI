#!/usr/bin/env python3
"""
High-quality rep-cue renderer.

Each cue is a short percussive sound played when the FSM enters a phase:
  cue_lift   — ascending arpeggio (C-E-G), bright bell-like timbre
  cue_hold   — sustained high tone, soft vibraphone
  cue_lower  — descending arpeggio (G-E-C)
  cue_rest   — soft single chime

Uses FM synthesis for a "bell / vibraphone" character (richer than pure sines),
ADSR envelope for snappy attack and tail, and a short reverb tail.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import soundfile as sf


SAMPLERATE = 48000


# ---- Synth primitives -------------------------------------------------

def fm_tone(freq: float, duration_s: float,
            mod_ratio: float = 3.0, mod_depth: float = 2.0,
            decay_s: float = 1.2) -> np.ndarray:
    """FM synthesis — carrier modulated by sine, with exponential decay.

    Bell sound: 1:3 or 1:5 ratio, depth drops exponentially → bright initial
    attack + warm sustain. decay_s now LONGER so the cue audibly rings into
    the ambient bed instead of vanishing in <0.3s.
    """
    n = int(duration_s * SAMPLERATE)
    t = np.arange(n) / SAMPLERATE
    mod_env = np.exp(-t / 0.5)        # was 0.35; slower modulator decay
    modulator = mod_depth * mod_env * np.sin(2 * np.pi * freq * mod_ratio * t)
    carrier = np.sin(2 * np.pi * freq * t + modulator)
    amp_env = np.exp(-t / decay_s)
    return (carrier * amp_env).astype(np.float32)


def adsr_envelope(n: int, attack_s: float, decay_s: float,
                  sustain_level: float, release_s: float) -> np.ndarray:
    sr = SAMPLERATE
    a, d, r = int(attack_s * sr), int(decay_s * sr), int(release_s * sr)
    if a + d + r > n:
        scale = n / max(1, a + d + r)
        a, d = int(a * scale), int(d * scale)
        r = max(1, n - a - d)
    s_len = max(0, n - a - d - r)
    env = np.zeros(n, dtype=np.float32)
    if a > 0: env[:a] = np.linspace(0, 1, a)
    if d > 0: env[a:a+d] = np.linspace(1, sustain_level, d)
    if s_len > 0: env[a+d:a+d+s_len] = sustain_level
    if r > 0:
        peak = sustain_level if s_len > 0 else (env[a+d-1] if d > 0 else 1.0)
        env[-r:] = np.linspace(peak, 0, r)
    return env


def short_reverb(audio: np.ndarray, n_taps: int = 6,
                 decay: float = 0.55, max_delay_ms: float = 80) -> np.ndarray:
    """Cheap multi-tap reverb for cues — adds 'space' without much CPU.

    Sums N delayed copies with exponential decay. Sounds spacious enough for
    short percussive cues (real Schroeder reverb is overkill here).
    """
    n = len(audio)
    out = audio.copy()
    np.random.seed(7)  # deterministic
    for i in range(n_taps):
        delay_ms = max_delay_ms * (i + 1) / n_taps * np.random.uniform(0.8, 1.0)
        d = int(delay_ms * SAMPLERATE / 1000)
        amp = decay ** (i + 1)
        if d < n:
            out[d:] += audio[:n-d] * amp
    return out


def mono_to_stereo(mono: np.ndarray, width: float = 0.85) -> np.ndarray:
    """Subtle stereo width via L/R sample offset."""
    offset = int(0.003 * SAMPLERATE)  # 3 ms
    n = len(mono)
    left = mono.copy()
    right = np.zeros(n, dtype=np.float32)
    right[offset:] = mono[:n-offset] * width
    return np.column_stack([left, right]).astype(np.float32)


def normalize(audio: np.ndarray, peak: float = 0.75) -> np.ndarray:
    m = float(np.max(np.abs(audio)))
    if m < 1e-9:
        return audio
    return audio * (peak / m)


# ---- Four cues ---------------------------------------------------------

def make_lift() -> np.ndarray:
    """Ascending C5-E5-G5 arpeggio with bell timbre, ~1.2s total with ring-out."""
    note_dur = 0.32
    overlap_s = 0.08
    note_n = int(note_dur * SAMPLERATE)
    olap_n = int(overlap_s * SAMPLERATE)
    # Extra tail so the last note can ring out
    total_n = note_n + (note_n - olap_n) * 2 + int(0.5 * SAMPLERATE)
    out = np.zeros(total_n, dtype=np.float32)
    for i, f in enumerate([523.25, 659.25, 783.99]):
        start = i * (note_n - olap_n)
        tone_len = note_n + int(0.4 * SAMPLERATE)   # let each note ring
        if start + tone_len > total_n:
            tone_len = total_n - start
        tone = fm_tone(f, tone_len / SAMPLERATE, mod_ratio=4,
                       mod_depth=2.5, decay_s=0.9)
        env = adsr_envelope(tone_len, 0.005, 0.04, 0.7, 0.4)
        out[start:start + tone_len] += tone * env
    out = short_reverb(out, n_taps=5, decay=0.55, max_delay_ms=80)
    return mono_to_stereo(normalize(out, 0.9))


def make_hold() -> np.ndarray:
    """Sustained vibraphone-like G5, ~1.5s with longer ring."""
    duration = 1.4
    n = int(duration * SAMPLERATE)
    t = np.arange(n) / SAMPLERATE
    f0 = 783.99
    vibrato = 1.0 + 0.003 * np.sin(2 * np.pi * 4.5 * t)
    mod_env = np.exp(-t / 0.5)
    modulator = 1.8 * mod_env * np.sin(2 * np.pi * f0 * 4 * vibrato * t)
    carrier = np.sin(2 * np.pi * f0 * vibrato * t + modulator)
    env = adsr_envelope(n, 0.05, 0.1, 0.75, 0.4)
    audio = carrier * env
    audio = short_reverb(audio, n_taps=6, decay=0.6, max_delay_ms=90)
    return mono_to_stereo(normalize(audio, 0.85))


def make_lower() -> np.ndarray:
    """Descending G5-E5-C5 arpeggio with ring-out."""
    note_dur = 0.32
    overlap_s = 0.08
    note_n = int(note_dur * SAMPLERATE)
    olap_n = int(overlap_s * SAMPLERATE)
    total_n = note_n + (note_n - olap_n) * 2 + int(0.5 * SAMPLERATE)
    out = np.zeros(total_n, dtype=np.float32)
    for i, f in enumerate([783.99, 659.25, 523.25]):
        start = i * (note_n - olap_n)
        tone_len = note_n + int(0.4 * SAMPLERATE)
        if start + tone_len > total_n:
            tone_len = total_n - start
        tone = fm_tone(f, tone_len / SAMPLERATE, mod_ratio=4,
                       mod_depth=2.0, decay_s=0.95)
        env = adsr_envelope(tone_len, 0.005, 0.04, 0.7, 0.4)
        out[start:start + tone_len] += tone * env
    out = short_reverb(out, n_taps=5, decay=0.55, max_delay_ms=80)
    return mono_to_stereo(normalize(out, 0.9))


def make_rest() -> np.ndarray:
    """Soft single chime F5 with octave below, ~1s."""
    duration = 1.0
    n = int(duration * SAMPLERATE)
    t = np.arange(n) / SAMPLERATE
    f0 = 698.46
    mod_env = np.exp(-t / 0.4)
    modulator = 1.0 * mod_env * np.sin(2 * np.pi * f0 * 3 * t)
    carrier = np.sin(2 * np.pi * f0 * t + modulator)
    sub = 0.3 * np.sin(2 * np.pi * f0 / 2 * t) * np.exp(-t / 0.7)
    audio = carrier * np.exp(-t / 0.55) + sub
    audio = short_reverb(audio, n_taps=5, decay=0.5, max_delay_ms=80)
    return mono_to_stereo(normalize(audio, 0.7))


# ---- Main -------------------------------------------------------------

def main():
    out_dir = Path(__file__).resolve().parent.parent / "assets"
    out_dir.mkdir(parents=True, exist_ok=True)

    cues = {
        "lift":  make_lift,
        "hold":  make_hold,
        "lower": make_lower,
        "rest":  make_rest,
    }

    print(f"Rendering 4 cues with FM bell synthesis + multi-tap reverb")
    print(f"  Output: {out_dir}\n")

    for name, factory in cues.items():
        stereo = factory()
        path = out_dir / f"cue_{name}.wav"
        sf.write(path, stereo, SAMPLERATE, subtype="PCM_16")
        duration = len(stereo) / SAMPLERATE
        size_kb = path.stat().st_size // 1024
        peak = float(np.max(np.abs(stereo)))
        print(f"  {path.name:<22} {duration:.2f}s  {size_kb:>3} KB   peak {peak:.2f}")

    print("\n[OK] Done.")


if __name__ == "__main__":
    main()
