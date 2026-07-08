#!/usr/bin/env python3
"""
Stand-alone audio engine smoke test — exercises set_ambient + play_cue.

Run on K1:
    source ~/radar/bin/activate
    python3 tools/audio_test.py

Plays each ambient bed for ~6s and triggers each cue between them.
Total runtime ~30s. Listen on K1 audio output.
"""

import sys
import time
from pathlib import Path

# Add project root so we can import backend.audio_engine
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.audio_engine import AudioEngine

ASSETS = Path(__file__).resolve().parent.parent / "assets"


def main():
    eng = AudioEngine(ASSETS)
    ok = eng.start()
    if not ok:
        print("FAIL: audio engine did not start")
        sys.exit(1)
    print("[Test] engine started")

    # 1. Calm ambient
    print("[Test] set_ambient(calm)")
    eng.set_ambient("calm")
    time.sleep(5)

    # 2. Play lift+hold+lower+rest cue sequence (simulates a rep)
    print("[Test] play_cue(lift)")
    eng.play_cue("lift")
    time.sleep(0.7)
    print("[Test] play_cue(hold)")
    eng.play_cue("hold")
    time.sleep(1.2)
    print("[Test] play_cue(lower)")
    eng.play_cue("lower")
    time.sleep(0.7)
    print("[Test] play_cue(rest)")
    eng.play_cue("rest")
    time.sleep(1.0)

    # 3. Crossfade to pleasure
    print("[Test] set_ambient(pleasure)  -> 1.5s crossfade")
    eng.set_ambient("pleasure")
    time.sleep(6)

    # 4. Crossfade to frustration
    print("[Test] set_ambient(frustration) -> 1.5s crossfade")
    eng.set_ambient("frustration")
    time.sleep(6)

    # 5. Fade out
    print("[Test] set_ambient(None) -> silence")
    eng.set_ambient(None)
    time.sleep(2.5)

    eng.stop()
    print("[Test] done")


if __name__ == "__main__":
    main()
