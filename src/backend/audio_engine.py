"""
Audio engine — K1 sounddevice mixer with three layers.

Layers:
  A. Ambient bed   — long wav, loops seamlessly, smooth crossfade on switch
  B. Cue           — short one-shot wav, triggered on FSM phase change
  C. Voice (TTS)   — short one-shot wav, ducks ambient during playback (Phase 7b)

Design:
  - Single sounddevice OutputStream callback drives all mixing
  - Callback is real-time (high-priority thread); no blocking I/O
  - Wav files loaded once at startup, kept in memory as float32 numpy arrays
  - Lock-free state via atomic-ish writes; readers re-check on each callback

The engine never raises into the audio thread — if a file is missing or audio
device is gone, we just play silence and log a warning.

Public API (called from asyncio context):
  start()                — open output stream
  stop()                 — close output stream
  set_ambient(label)     — crossfade to "calm" / "pleasure" / "frustration" / None
  play_cue(name)         — queue cue_<name>.wav as one-shot
  play_voice(wav_path)   — queue arbitrary wav as one-shot (Phase 7b)
  set_volumes(a/b/c)     — adjust per-layer levels
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Optional

import numpy as np

try:
    import sounddevice as sd
    import soundfile as sf
    HAS_AUDIO = True
except ImportError:
    HAS_AUDIO = False

log = logging.getLogger(__name__)


# ---- Configuration ----------------------------------------------------

SAMPLERATE       = 48000
CHANNELS         = 2
# 4096 frames @ 48 kHz = ~85 ms per callback. K1's RISC-V CPU + Python
# overhead need this much headroom; with 1024 we got buffer underruns
# (audible as crackling/stuttering noise).
BLOCKSIZE        = 4096
AMBIENT_CROSSFADE_S = 1.5
LOOP_CROSSFADE_S    = 0.05
VOICE_DUCK_LEVEL    = 0.35
VOICE_DUCK_FADE_S   = 0.3

# Default mix levels. Cue at 1.0 so it punches clearly through the ambient
# bed (0.5). Ambient kept relatively low so the cue's transient is dominant.
DEFAULT_VOL = {"ambient": 0.5, "cue": 1.0, "voice": 0.9}


# ---- Helpers ----------------------------------------------------------

def _load_wav(path: Path) -> np.ndarray | None:
    """Load wav as float32 stereo. Returns None on failure (engine plays silence)."""
    try:
        data, sr = sf.read(str(path), dtype="float32", always_2d=True)
    except Exception as e:
        log.warning("Failed to load %s: %r", path, e)
        return None
    if sr != SAMPLERATE:
        log.warning("Sample-rate mismatch on %s: %d vs %d (skipping resample, may sound off)",
                    path.name, sr, SAMPLERATE)
    # Normalize channel count
    if data.shape[1] == 1:
        data = np.repeat(data, CHANNELS, axis=1)
    elif data.shape[1] > CHANNELS:
        data = data[:, :CHANNELS]
    return data.astype(np.float32)


# ---- Engine ----------------------------------------------------------

class AudioEngine:
    """Single instance per backend process."""

    def __init__(self, assets_dir: Path,
                 samplerate: int = SAMPLERATE,
                 channels: int = CHANNELS,
                 blocksize: int = BLOCKSIZE):
        self.assets_dir = Path(assets_dir)
        self.samplerate = samplerate
        self.channels = channels
        self.blocksize = blocksize

        # Pre-loaded wavs (lazy load on first use; cached forever)
        self._wav_cache: dict[str, np.ndarray | None] = {}

        # Ambient state (set/read by callback)
        self._ambient_curr: np.ndarray | None = None
        self._ambient_curr_pos: int = 0
        self._ambient_next: np.ndarray | None = None
        self._ambient_next_pos: int = 0
        self._crossfade_pos: int = 0                  # 0 = not crossfading
        self._crossfade_frames: int = int(AMBIENT_CROSSFADE_S * samplerate)
        self._ambient_label: str | None = None

        # Cue / voice one-shots (list of [wav, pos])
        # Multiple cues may overlap; voice is exclusive (only one at a time)
        self._cues: list[list] = []           # [[wav, pos], ...]
        self._voice: list | None = None       # [wav, pos] or None

        # Mix volumes
        self._vol = dict(DEFAULT_VOL)

        # Voice ducking
        self._duck_factor: float = 1.0        # 1.0 = no duck, VOICE_DUCK_LEVEL = full duck
        self._duck_target: float = 1.0

        # Throttle for buffer underrun warnings (avoid log spam)
        self._last_status_log_ts: float = 0.0

        # State lock — only used for non-atomic dict / list mutations
        self._lock = threading.Lock()

        # Stream
        self._stream: Optional[object] = None
        self._available = HAS_AUDIO

        if not HAS_AUDIO:
            log.warning("[Audio] sounddevice/soundfile not installed; engine disabled")

    # ---- Public API ----------------------------------------------

    def start(self) -> bool:
        """Open the persistent output stream. Returns True on success."""
        if not self._available:
            return False
        if self._stream is not None:
            return True
        try:
            self._stream = sd.OutputStream(
                samplerate=self.samplerate,
                channels=self.channels,
                blocksize=self.blocksize,
                dtype="float32",
                callback=self._callback,
                latency="high",   # K1 isn't a low-latency target
            )
            self._stream.start()
            log.info("[Audio] stream started (%d Hz × %d ch, block %d)",
                     self.samplerate, self.channels, self.blocksize)
            return True
        except Exception as e:
            log.error("[Audio] failed to open output stream: %r", e)
            self._stream = None
            self._available = False
            return False

    def stop(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception as e:
                log.warning("[Audio] stream close error: %r", e)
            self._stream = None
            log.info("[Audio] stream stopped")

    def set_ambient(self, label: str | None) -> None:
        """Crossfade to a different ambient bed.

        label: "calm" | "pleasure" | "frustration" | None (silence)
        """
        if not self._available:
            return
        if label == self._ambient_label:
            return     # already on this label, no-op
        log.info("[Audio] ambient: %s → %s", self._ambient_label, label)
        new_wav = None if label is None else self._load("ambient", label)
        with self._lock:
            if self._ambient_curr is None:
                # No active ambient — start immediately, no crossfade
                self._ambient_curr = new_wav
                self._ambient_curr_pos = 0
                self._ambient_next = None
                self._crossfade_pos = 0
            else:
                # Start a crossfade from current → new
                self._ambient_next = new_wav
                self._ambient_next_pos = 0
                self._crossfade_pos = 1
            self._ambient_label = label

    def play_cue(self, name: str) -> None:
        """Queue cue_<name>.wav for one-shot playback."""
        if not self._available:
            return
        wav = self._load("cue", name)
        if wav is None:
            return
        with self._lock:
            self._cues.append([wav, 0])

    def play_voice(self, wav_path: str | Path) -> None:
        """Queue an arbitrary wav as voice (with ambient ducking)."""
        if not self._available:
            return
        wav = _load_wav(Path(wav_path))
        if wav is None:
            return
        with self._lock:
            self._voice = [wav, 0]
            self._duck_target = VOICE_DUCK_LEVEL

    def set_volumes(self, **kwargs) -> None:
        """Update per-layer volumes (ambient/cue/voice)."""
        with self._lock:
            for k, v in kwargs.items():
                if k in self._vol:
                    self._vol[k] = max(0.0, min(1.5, float(v)))

    # ---- Internal: file loading ----------------------------------

    def _load(self, kind: str, name: str) -> np.ndarray | None:
        cache_key = f"{kind}_{name}"
        if cache_key not in self._wav_cache:
            path = self.assets_dir / f"{cache_key}.wav"
            self._wav_cache[cache_key] = _load_wav(path)
            if self._wav_cache[cache_key] is None:
                log.warning("[Audio] asset missing: %s", path)
        return self._wav_cache[cache_key]

    # ---- Internal: sounddevice callback ---------------------------

    def _callback(self, outdata: np.ndarray, frames: int,
                  time_info, status) -> None:
        """Called from the audio thread. MUST NOT block, allocate large
        objects, or raise.

        Mix order:
            1. Read ambient (current + optional crossfade with next)
            2. Apply ambient ducking
            3. Mix in active cues
            4. Mix in active voice
            5. Soft-clip to [-1, 1]
        """
        # sounddevice fills `status` if there was an underrun/overrun in the
        # previous callback — log but don't crash. Throttle the log so we
        # don't spam if underruns are persistent.
        if status:
            now = time.monotonic()
            if now - self._last_status_log_ts > 5.0:
                log.warning("[Audio] callback status: %s", status)
                self._last_status_log_ts = now

        try:
            out = self._mix(frames)
            np.clip(out, -1.0, 1.0, out=out)
            outdata[:] = out
        except Exception as e:
            # Never let an exception escape the callback
            outdata[:] = 0
            log.warning("[Audio] callback error: %r", e)

    def _mix(self, frames: int) -> np.ndarray:
        out = np.zeros((frames, self.channels), dtype=np.float32)
        with self._lock:
            vol = dict(self._vol)
            ambient_curr = self._ambient_curr
            ambient_next = self._ambient_next
            crossfade_pos = self._crossfade_pos
            crossfade_total = self._crossfade_frames
            duck_target = self._duck_target

        # 1. Ambient (with optional crossfade)
        ambient_signal = self._read_ambient_block(frames)

        # Smoothly approach duck target (per-block exponential)
        duck_alpha = min(1.0, frames / (VOICE_DUCK_FADE_S * self.samplerate))
        self._duck_factor += (duck_target - self._duck_factor) * duck_alpha
        out += ambient_signal * vol["ambient"] * self._duck_factor

        # 2. Cues — process all active, drop finished
        out += self._mix_cues(frames) * vol["cue"]

        # 3. Voice
        voice_signal, voice_done = self._read_voice_block(frames)
        if voice_signal is not None:
            out += voice_signal * vol["voice"]
        if voice_done:
            # voice ended → un-duck ambient
            with self._lock:
                self._voice = None
                self._duck_target = 1.0

        return out

    # ---- Internal: per-layer reads --------------------------------

    def _read_ambient_block(self, frames: int) -> np.ndarray:
        """Read `frames` samples of ambient, handling loop + crossfade."""
        out = np.zeros((frames, self.channels), dtype=np.float32)

        # Pure silence if nothing scheduled
        if self._ambient_curr is None:
            return out

        # Active ambient with internal loop-boundary crossfade
        out += self._read_one_ambient(frames, self._ambient_curr,
                                       getattr_pos=lambda: self._ambient_curr_pos,
                                       setattr_pos=self._set_curr_pos)

        # Crossfade with next, if active
        if self._ambient_next is not None and self._crossfade_pos > 0:
            next_block = self._read_one_ambient(
                frames, self._ambient_next,
                getattr_pos=lambda: self._ambient_next_pos,
                setattr_pos=self._set_next_pos,
            )
            # Build per-sample fade-in for "next" and fade-out for "curr"
            t_start = self._crossfade_pos / self._crossfade_frames
            t_end   = min(1.0, (self._crossfade_pos + frames) / self._crossfade_frames)
            fade_next = np.linspace(t_start, t_end, frames, dtype=np.float32)
            fade_curr = 1.0 - fade_next
            # Apply fades (broadcast over stereo)
            out = out * fade_curr[:, None] + next_block * fade_next[:, None]

            self._crossfade_pos += frames
            if self._crossfade_pos >= self._crossfade_frames:
                # Crossfade complete — promote next → curr
                with self._lock:
                    self._ambient_curr = self._ambient_next
                    self._ambient_curr_pos = self._ambient_next_pos
                    self._ambient_next = None
                    self._ambient_next_pos = 0
                    self._crossfade_pos = 0

        return out

    def _read_one_ambient(self, frames: int, wav: np.ndarray,
                          getattr_pos, setattr_pos) -> np.ndarray:
        """Read `frames` from one ambient wav, looping with tiny crossfade."""
        N = len(wav)
        if N == 0:
            return np.zeros((frames, self.channels), dtype=np.float32)
        pos = getattr_pos()
        xfade = int(LOOP_CROSSFADE_S * self.samplerate)

        out = np.zeros((frames, self.channels), dtype=np.float32)
        i = 0
        while i < frames:
            remaining_in_loop = N - pos
            take = min(frames - i, remaining_in_loop)
            out[i:i + take] = wav[pos:pos + take]
            i += take
            pos += take
            if pos >= N:
                pos = 0   # restart loop
                # Note: we don't do per-sample loop crossfade here because the
                # ambient wavs are generated to be loop-seamless (integer cycles).
                # If you swap in a non-seamless wav, set LOOP_CROSSFADE_S > 0 and
                # add the crossfade logic here.
        setattr_pos(pos)
        return out

    def _set_curr_pos(self, p): self._ambient_curr_pos = p
    def _set_next_pos(self, p): self._ambient_next_pos = p

    def _mix_cues(self, frames: int) -> np.ndarray:
        """Mix all active cues into a single block; drop finished ones."""
        if not self._cues:
            return np.zeros((frames, self.channels), dtype=np.float32)
        out = np.zeros((frames, self.channels), dtype=np.float32)
        with self._lock:
            cues = list(self._cues)
            new_cues: list[list] = []
            for entry in cues:
                wav, pos = entry
                N = len(wav)
                take = min(frames, N - pos)
                if take > 0:
                    out[:take] += wav[pos:pos + take]
                    new_pos = pos + take
                    if new_pos < N:
                        new_cues.append([wav, new_pos])
                # else: cue finished, drop
            self._cues = new_cues
        return out

    def _read_voice_block(self, frames: int) -> tuple[np.ndarray | None, bool]:
        """Returns (signal, done). Returns (None, False) if no voice playing."""
        with self._lock:
            v = self._voice
            if v is None:
                return None, False
            wav, pos = v
            N = len(wav)
            take = min(frames, N - pos)
            out = np.zeros((frames, self.channels), dtype=np.float32)
            if take > 0:
                out[:take] = wav[pos:pos + take]
                v[1] = pos + take
            return out, (v[1] >= N)


# ---- Singleton helper ------------------------------------------------

_engine: AudioEngine | None = None


def get_engine() -> AudioEngine | None:
    """Get the global AudioEngine. Returns None if not initialized."""
    return _engine


def init_engine(assets_dir: Path) -> AudioEngine:
    """Create the engine but don't start it yet (call .start() in lifespan)."""
    global _engine
    if _engine is None:
        _engine = AudioEngine(assets_dir)
    return _engine
