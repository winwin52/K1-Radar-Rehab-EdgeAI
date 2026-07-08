#!/usr/bin/env python3
"""
Real-time emotion inference during leg-raise rehabilitation training (v2.0).

Runs on K1 MUSE Pi Pro. Reads SPI radar data, extracts features from
60-second sliding windows, and predicts emotion (calm/frustration/pleasure)
using a pre-trained ThresholdClassifier model (29 features).

Enhancements over v1.0:
  - log1p transform on count/vel/psd/amplitude/jerk features
  - Z-score normalization: (feat - baseline) / running_std
  - Sticky State Machine hysteresis (frus→plea needs 5 windows)

Pipeline:
  1. Prompt user to sit still for 4 minutes (60s buffer fill + 180s effective baseline)
  2. Collect radar frames, build personal calm baseline (90 windows)
  3. Prompt user to start leg exercises
  4. Every 2s: extract features from last 60s -> predict -> print + CSV

Usage:
    python collect_realtime_v3.py --subject w --dist 140
    Ctrl+C to stop.
"""

import time, sys, os, signal, struct, argparse, pickle
from datetime import datetime

import numpy as np
import spidev
import lgpio as sbc

# Pure-python feature extraction engine (no hardware deps)
from feature_extractor import (
    extract_phase, extract_window_features, build_log_mask, RingBuffer,
    FS, WIN_SIZE, STEP_SIZE, RANGE_BINS, N_CHANNELS, PC_COLS,
)

# ═══════════════════════════════════════════════════════════════════════════════
# HIF Protocol Constants
# ═══════════════════════════════════════════════════════════════════════════════

MAGIC = 0xA5
HDR_WIRE = 7
CHK_WIRE = 5
HIF_MSG_ID_PSIC = 0xC6
HIF_TYPE_TO_DEVICE = 1
HIF_FLAG_REQ = 0x01
HIF_FLAG_CHECK = 0x04

# SPI
SPI_BUS, SPI_DEV, SPI_HZ = 3, 0, 8_000_000
GPIO_CHIP, INT_GPIO = 0, 49
BURST = 3
MAX_CONSECUTIVE_TIMEOUTS = 10
BASELINE_MIN = 4.0   # 4 min sit → 60s buffer fill + 180s effective baseline (=90 windows)

running = True


def _stop(sig, frame):
    global running
    running = False
    print("\nStopping...")


signal.signal(signal.SIGINT, _stop)
signal.signal(signal.SIGTERM, _stop)

# ═══════════════════════════════════════════════════════════════════════════════
# HIF Protocol Helpers
# ═══════════════════════════════════════════════════════════════════════════════

def calc_check8(magic, hdr_bytes):
    s = magic + sum(hdr_bytes)
    return (~s) & 0xFF


def calc_check32(data):
    total = 0
    for i in range(0, len(data), 4):
        w = int.from_bytes(data[i:i + 4].ljust(4, b'\x00'), 'little')
        total += w
        total = (total & 0xFFFFFFFF) + (total >> 32)
    return (~total & 0xFFFFFFFF).to_bytes(4, 'little')


def build_poll():
    h = bytearray(6)
    h[0] = MAGIC; h[1] = 0; h[2] = 0x15; h[3] = 0x0C; h[4] = 4; h[5] = 0
    h[1] = calc_check8(MAGIC, h[2:6])
    pl = bytes([1, 0, BURST & 0xFF, (BURST >> 8) & 0xFF])
    c32 = calc_check32(bytes(h[2:6]) + pl)
    return bytes(h) + pl + c32


def parse_hif_header(raw6):
    flags = raw6[2] & 0x3F
    return {
        'msg_id': raw6[3],
        'length': raw6[4] | ((raw6[5] & 0x0F) << 8),
        'more': (flags >> 5) & 1,
        'check': (flags >> 4) & 1,
    }


def hif_check8_ok(raw6):
    s = raw6[0] + raw6[2] + raw6[3] + raw6[4] + raw6[5]
    return raw6[1] == ((~s) & 0xFF)


# ═══════════════════════════════════════════════════════════════════════════════
# SPI Radar
# ═══════════════════════════════════════════════════════════════════════════════

class RadarSPI:
    def __init__(self, speed=SPI_HZ):
        self.gh = sbc.gpiochip_open(GPIO_CHIP)
        sbc.gpio_claim_input(self.gh, INT_GPIO)
        self.spi = spidev.SpiDev()
        self.spi.open(SPI_BUS, SPI_DEV)
        self.spi.mode = 0b00
        self.spi.max_speed_hz = speed
        self.spi.bits_per_word = 8
        self.spi.cshigh = False

    def int_level(self):
        return sbc.gpio_read(self.gh, INT_GPIO)

    def wait_int_high(self, ms=1000):
        d = time.monotonic() + ms / 1000.0
        while running and time.monotonic() < d:
            if sbc.gpio_read(self.gh, INT_GPIO):
                return True
            time.sleep(0.0001)
        return False

    def wait_int_low(self, ms=1000):
        d = time.monotonic() + ms / 1000.0
        while running and time.monotonic() < d:
            if not sbc.gpio_read(self.gh, INT_GPIO):
                return True
            time.sleep(0.0001)
        return False

    def poll(self, burst=BURST):
        self.spi.xfer2(list(build_poll()))
        self.wait_int_low(50)
        self.wait_int_high(200)
        time.sleep(0.002)

        chunk = bytes(self.spi.xfer2([0x00] * 4096))
        hif_frames = []
        pos = 0

        while pos < len(chunk) and chunk[pos] == MAGIC:
            if pos + HDR_WIRE > len(chunk):
                break
            raw6 = chunk[pos:pos + 6]
            hdr = parse_hif_header(raw6)
            if not hif_check8_ok(raw6):
                break
            N = hdr['length']
            if N > 4000 or N == 0:
                break
            payload = chunk[pos + HDR_WIRE: pos + HDR_WIRE + N]
            wire_len = HDR_WIRE + N + (CHK_WIRE if hdr['check'] else 0)
            hif_frames.append({'hdr': hdr, 'payload': payload})
            pos += wire_len
            if hdr['more'] == 0:
                break
        return hif_frames

    def close(self):
        self.spi.close()


# ═══════════════════════════════════════════════════════════════════════════════
# PSIC Parser
# ═══════════════════════════════════════════════════════════════════════════════

def parse_psic_payload(payload):
    if len(payload) < 6:
        return None, None
    name_end = payload.find(b'\x00', 5)
    if name_end < 0:
        return None, None
    channel = payload[5:name_end].decode('ascii', errors='replace')
    raw = payload[name_end + 1:]
    return channel, raw


def parse_1d_data(raw, expected_len=RANGE_BINS * 4):
    if len(raw) < expected_len:
        return None
    return np.frombuffer(raw[:expected_len], dtype=np.int16).reshape(-1, 2)


def parse_float_point_cloud(raw, cols=PC_COLS):
    n_floats = len(raw) // 4
    if n_floats == 0 or n_floats % cols != 0:
        return None
    return np.frombuffer(raw, dtype=np.float32).reshape(-1, cols)


# ═══════════════════════════════════════════════════════════════════════════════
# Frame Accumulator
# ═══════════════════════════════════════════════════════════════════════════════

class FrameAccumulator:
    def __init__(self):
        self.reset()

    def reset(self):
        self.channels = []
        self.motion_pc = None
        self.micro_pc = None

    @property
    def is_complete(self):
        return len(self.channels) == N_CHANNELS

    def flush(self):
        if not self.is_complete:
            return None
        iq_frame = np.stack(self.channels, axis=0)
        motion = (self.motion_pc if self.motion_pc is not None and len(self.motion_pc) > 0
                  else np.zeros((0, PC_COLS), dtype=np.float32))
        micro = (self.micro_pc if self.micro_pc is not None and len(self.micro_pc) > 0
                 else np.zeros((0, PC_COLS), dtype=np.float32))
        ts = time.time()
        self.reset()
        return iq_frame, motion, micro, ts


# ═══════════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    """CLI entry point — delegates to RehabEngine."""
    from rehab_engine import run_cli

    parser = argparse.ArgumentParser(
        description='Real-time emotion inference for leg-raise rehabilitation (v2.0)'
    )
    parser.add_argument('--subject', type=str, required=True,
                        help='Subject name / ID')
    parser.add_argument('--dist', type=int, required=True,
                        help='Distance from radar (cm)')
    parser.add_argument('--baseline-min', type=float, default=BASELINE_MIN,
                        help=f'Baseline duration in minutes (default: {BASELINE_MIN})')
    args = parser.parse_args()

    run_cli(subject=args.subject, dist_cm=args.dist,
            baseline_min=args.baseline_min)
