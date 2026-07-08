#!/usr/bin/env python3
"""
Rehabilitation inference engine v3.0 — SPI 采集线程 + 推理线程拆分。

═══════════════════════════════════════════════════════════════════════════════
v3.0 的核心改动（只动本文件，其他文件不变）
═══════════════════════════════════════════════════════════════════════════════

  - start() 拆成 _spi_loop() + _inference_loop()，两个线程并行
  - SPI 线程只做采集（每轮 5-15ms，永远 < 雷达固件 100ms 超时阈值
    sendFrameTo，参见 SDK hif.c:1158）
  - 推理线程独立做特征提取 + 推理（耗时 100-400ms，不影响 SPI 响应）
  - 进程内 queue.Queue 连接两个线程，IPC 零开销
  - 主进程协议不变（mp.Queue 上的消息格式保持兼容）

═══════════════════════════════════════════════════════════════════════════════
稳定性补丁
═══════════════════════════════════════════════════════════════════════════════

  - MAX_CONSECUTIVE_TIMEOUTS 从 30 降回 10（修复后 SPI 不应再超时）
  - history_feats 长度上限 MAX_HISTORY（防长会话内存增长）
  - 推理耗时 > INFERENCE_SLOW_MS 告警
  - 基线阶段运动幅度异常告警（用户没好好静坐时提示）
  - 帧队列满了 drop 旧帧并打告警
  - 周期性数据健康度报告（每 30 秒一行）

═══════════════════════════════════════════════════════════════════════════════
v2.0 保留的全部算法功能
═══════════════════════════════════════════════════════════════════════════════

  - ThresholdClassifier (pleasure_threshold=0.50)
  - log1p 变换
  - Z-score 归一化：(feat - personal_baseline) / running_std
  - Sticky State Machine 迟滞

═══════════════════════════════════════════════════════════════════════════════
回调签名保持不变（与 v2.0 兼容）
═══════════════════════════════════════════════════════════════════════════════

    engine.on_frame              = (frame_idx, wall_elapsed_s, z_mean,
                                    emotion, probs, br_bpm, chest_dist_cm)
    engine.on_baseline_progress  = (progress_0_1)
    engine.on_baseline_done      = ()
    engine.on_inference          = (frame_idx, features, probs, label, breathing_bpm)

═══════════════════════════════════════════════════════════════════════════════
项目数据流（三阶段）
═══════════════════════════════════════════════════════════════════════════════

  阶段 1: 0 ~ 60s    缓冲填充       SPI 持续采集，buf 凑满 600 帧，不出特征
  阶段 2: 60 ~ 240s  基线采集       每 2s 提一次特征 → baseline_feats（90 个）
                                    不调模型、不算 Z-score、不输出情绪
                     4 分钟末尾    personal_baseline = mean(baseline_feats)
  阶段 3: 240s+      训练态         每 2s 提特征 → log → Z-score → 模型推理
                                    → sticky machine → 输出情绪标签
"""

import time
import os
import pickle
import threading
import queue

import numpy as np

# scipy 是 ThresholdClassifier 解 pickle 需要（model.pkl 里的特征处理引用）
from scipy.signal import butter, filtfilt, detrend, find_peaks
from scipy.stats import kurtosis, skew

# sklearn 基类用于 ThresholdClassifier 解 pickle
from sklearn.base import BaseEstimator, ClassifierMixin


# ═══════════════════════════════════════════════════════════════════════════════
# ThresholdClassifier (must match train_ml.py definition for pickle)
# ═══════════════════════════════════════════════════════════════════════════════

class ThresholdClassifier(BaseEstimator, ClassifierMixin):
    """Wrapper that biases pleasure predictions above a confidence threshold."""
    def __init__(self, clf, pleasure_threshold=0.50):
        self.clf = clf
        self.pleasure_threshold = pleasure_threshold

    def fit(self, X, y, sample_weight=None):
        self.clf.fit(X, y, sample_weight=sample_weight)
        self.classes_ = self.clf.classes_
        return self

    def predict(self, X):
        probs = self.clf.predict_proba(X)
        preds = np.argmax(probs, axis=1)
        if probs.shape[1] > 2:
            pleasure_mask = (probs[:, 2] > self.pleasure_threshold)
            preds[pleasure_mask] = 2
        return preds

    def predict_proba(self, X):
        return self.clf.predict_proba(X)


# Pure-python feature extraction (shared with collect_realtime_v3.py)
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

SPI_BUS, SPI_DEV, SPI_HZ = 3, 0, 8_000_000
GPIO_CHIP, INT_GPIO = 0, 49
BURST = 3
MAX_CONSECUTIVE_TIMEOUTS = 10   # v3.0: 修复后 SPI 不应再超时，恢复严格阈值
TIMEOUT_WARN_EVERY = 5          # 每多少次超时打一行
BASELINE_MIN_DEFAULT = 4.0

# Sticky state machine thresholds
FRUS_TO_PLEA_THRESHOLD = 5   # frustration→pleasure 需要连续 5 窗（10s）
DEFAULT_SWITCH_THRESHOLD = 2  # 其他切换需要 2 窗（4s）

# ─── v3.0 稳定性参数 ──────────────────────────────────────────────────────────
MAX_HISTORY = 1800            # history_feats 上限（约 1 小时窗口）
INFERENCE_SLOW_MS = 1000      # 推理耗时告警阈值
BASELINE_MOTION_THRESHOLD = 50.0  # 基线阶段单帧运动点数告警阈值
FRAME_Q_MAXSIZE = 200         # SPI→推理 进程内 Queue 容量（20s 余量）
HEALTH_REPORT_S = 2.0         # Phase 10: engine health heartbeat interval


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
# Radar SPI Hardware
# ═══════════════════════════════════════════════════════════════════════════════

class RadarSPI:
    """SPI 通信层。与 v2.0 完全一致，未做修改。"""

    def __init__(self, speed=SPI_HZ):
        import lgpio as sbc
        self.gh = sbc.gpiochip_open(GPIO_CHIP)
        sbc.gpio_claim_input(self.gh, INT_GPIO)
        import spidev
        self.spi = spidev.SpiDev()
        self.spi.open(SPI_BUS, SPI_DEV)
        self.spi.mode = 0b00
        self.spi.max_speed_hz = speed
        self.spi.bits_per_word = 8
        self.spi.cshigh = False
        self._sbc = sbc

    def int_level(self):
        return self._sbc.gpio_read(self.gh, INT_GPIO)

    def wait_int_high(self, ms=1000):
        d = time.monotonic() + ms / 1000.0
        while time.monotonic() < d:
            if self._sbc.gpio_read(self.gh, INT_GPIO):
                return True
            time.sleep(0.0001)
        return False

    def wait_int_low(self, ms=1000):
        d = time.monotonic() + ms / 1000.0
        while time.monotonic() < d:
            if not self._sbc.gpio_read(self.gh, INT_GPIO):
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
    """攒齐 8 个 1d_data 通道 + 可选点云，组装成一帧。与 v2.0 一致。"""

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
# RehabEngine v3.0
# ═══════════════════════════════════════════════════════════════════════════════

class RehabEngine:
    """
    雷达 → 特征提取 → 情绪推理 引擎 (v3.0)。

    v3.0 关键变化:
        - 内部用两个线程：SPI 线程 + 推理线程
        - 两者通过 queue.Queue 解耦
        - SPI 线程保证 10fps，雷达永远不超时
        - 推理线程慢工出细活，不阻塞采集

    所有回调与 v2.0 兼容，外部调用方（run_rehab.py 的 _collector_process）零改动。
    """

    def __init__(self, subject='unknown', dist_cm=160, model_path=None,
                 baseline_min=BASELINE_MIN_DEFAULT):
        self.subject = subject
        self.dist_cm = dist_cm
        self.baseline_min = baseline_min

        # ── Load model ──
        # 注册 ThresholdClassifier 到 __main__，方便子进程 pickle 反序列化
        import __main__
        if not hasattr(__main__, 'ThresholdClassifier'):
            __main__.ThresholdClassifier = ThresholdClassifier

        if model_path is None:
            model_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                      'model.pkl')
        with open(model_path, 'rb') as f:
            model_pkg = pickle.load(f)

        self.model = model_pkg['model']
        # sklearn 1.3→1.5 兼容修复（同 v2.0）
        if hasattr(self.model, 'clf') and hasattr(self.model.clf, 'estimators_'):
            for tree in self.model.clf.estimators_:
                if not hasattr(tree, 'monotonic_cst'):
                    tree.monotonic_cst = None
        elif hasattr(self.model, 'estimators_'):
            for tree in self.model.estimators_:
                if not hasattr(tree, 'monotonic_cst'):
                    tree.monotonic_cst = None

        self.label_encoder = model_pkg['label_encoder']
        self.global_calm_mean = model_pkg['global_calm_mean']
        self.feature_cols = model_pkg['feature_cols']
        self.n_features = len(self.feature_cols)
        self.log_transform = model_pkg.get('log_transform', False)
        self.log_mask = build_log_mask(self.feature_cols) if self.log_transform else None

        # v3.0: 按特征名查找关键索引，避免硬编码 feat[0]
        try:
            self._br_bpm_idx = self.feature_cols.index('br_bpm')
        except ValueError:
            self._br_bpm_idx = 0
        try:
            self._mot_count_idx = self.feature_cols.index('mot_count_avg')
        except ValueError:
            self._mot_count_idx = -1

        # ── Callbacks (set by caller) ──
        self.on_frame = None              # (frame_idx, wall_elapsed_s, z_mean, emotion, probs, br_bpm, chest_dist_cm)
        self.on_inference = None          # (frame_idx, features, probs, label, breathing_bpm)
        self.on_baseline_progress = None  # (progress_0_1)
        self.on_baseline_done = None      # ()
        self.on_health = None             # (dict) Phase 10 diagnostics

        # ── 共享状态（推理线程写、SPI 线程读，CPython 单语句赋值原子，无需锁）──
        self.total_frames = 0
        self.elapsed_seconds = 0.0
        self.personal_baseline = None
        self.breathing_bpm = 15.0
        self.emotion_label = 'baseline'
        self.emotion_probs = [1.0, 0.0, 0.0]
        self.motion_z_mean = 0.0
        self.chest_distance_cm = 0.0
        self.iq_current = None
        self.motion_pc_current = None
        self.micro_pc_current = None

        # ── Phase 10: diagnostic counters (read by health callback) ──
        self.spi_slow_loops = 0
        self.spi_drop_count = 0
        self.spi_consecutive_timeouts = 0
        self.spi_last_frame_ts = 0.0
        self.inference_count = 0
        self.inference_last_ts = 0.0
        self.inference_last_frame = -STEP_SIZE
        self.inference_last_error = ""
        self.inference_slow_count = 0
        self.inference_baseline_windows = 0
        self.inference_history_windows = 0

        # ── v3.0 线程间通信 ──
        self._stop_flag = threading.Event()
        self._frame_q = queue.Queue(maxsize=FRAME_Q_MAXSIZE)
        self._engine_start_time = 0.0  # start() 中设置

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Entry point
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def start(self):
        """启动 SPI 线程 + 推理线程，阻塞直到 stop() 或异常。"""
        self._stop_flag.clear()
        self._engine_start_time = time.time()
        self.total_frames = 0

        t_spi = threading.Thread(target=self._spi_loop,
                                  name='radar-spi', daemon=True)
        t_inf = threading.Thread(target=self._inference_loop,
                                  name='radar-inf', daemon=True)

        print(f"[RehabEngine v3.0] Starting "
              f"(subject={self.subject}, dist={self.dist_cm}cm, "
              f"baseline={self.baseline_min}min)")
        print(f"[RehabEngine v3.0] Features={self.n_features}, "
              f"log_transform={self.log_transform}, "
              f"frame_q_max={FRAME_Q_MAXSIZE}")

        t_spi.start()
        t_inf.start()

        # 主线程仅做监督，等待停止信号
        try:
            while not self._stop_flag.is_set():
                time.sleep(0.5)
                # 两个工作线程都死了就退出
                if not t_spi.is_alive() and not t_inf.is_alive():
                    print("[RehabEngine v3.0] Both worker threads exited.")
                    break
        except KeyboardInterrupt:
            print("\n[RehabEngine v3.0] Interrupted, stopping...")
            self._stop_flag.set()

        # 等线程清理收尾
        t_spi.join(timeout=3.0)
        t_inf.join(timeout=3.0)
        wall_total = time.time() - self._engine_start_time
        print(f"[RehabEngine v3.0] Stopped "
              f"(frames={self.total_frames}, t={wall_total:.0f}s, "
              f"avg_fps={self.total_frames/max(wall_total,1):.1f})")

    def stop(self):
        """从外部线程发停止信号（thread-safe）。"""
        self._stop_flag.set()

    def _emit_health(self, source: str) -> None:
        """Phase 10 diagnostic heartbeat.

        Called from SPI and inference threads. Best-effort only: never raises,
        never blocks on the UI path. Lets backend distinguish display staleness
        from acquisition/inference staleness.
        """
        if not self.on_health:
            return
        now = time.time()
        wall = now - self._engine_start_time if self._engine_start_time > 0 else 0.0
        try:
            self.on_health({
                "source": source,
                "ts": now,
                "wall_s": round(wall, 2),
                "total_frames": int(self.total_frames),
                "avg_fps": round(self.total_frames / max(wall, 1.0), 2),
                "frame_q_size": self._frame_q.qsize(),
                "frame_q_max": FRAME_Q_MAXSIZE,
                "spi_slow_loops": int(self.spi_slow_loops),
                "spi_drops": int(self.spi_drop_count),
                "spi_timeouts": int(self.spi_consecutive_timeouts),
                "spi_last_frame_age_s": round(now - self.spi_last_frame_ts, 2) if self.spi_last_frame_ts else None,
                "inference_count": int(self.inference_count),
                "inference_last_frame": int(self.inference_last_frame),
                "inference_last_age_s": round(now - self.inference_last_ts, 2) if self.inference_last_ts else None,
                "inference_slow_count": int(self.inference_slow_count),
                "inference_last_error": self.inference_last_error,
                "baseline_windows": int(self.inference_baseline_windows),
                "history_windows": int(self.inference_history_windows),
            })
        except Exception:
            pass

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 线程 1: SPI 采集（轻量，每轮 5-15ms）
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _spi_loop(self):
        """
        SPI 采集线程。唯一职责：保持 10fps 采集，把帧扔进 _frame_q。

        永远不做重活，永远不阻塞超过 ~15ms，确保不触发雷达 100ms 超时。
        """
        try:
            radar = RadarSPI(speed=SPI_HZ)
        except Exception as e:
            print(f"[SPI] RadarSPI init failed: {e}")
            self._stop_flag.set()
            return

        acc = FrameAccumulator()
        consecutive_timeouts = 0
        drop_count = 0
        loop_slow_count = 0
        last_health_ts = 0.0
        loop_t_last = time.monotonic()

        try:
            while not self._stop_flag.is_set():
                t_loop_start = time.monotonic()

                # ── 等 INT 拉高 ──
                if not radar.wait_int_high(ms=5000 if self.total_frames == 0 else 2000):
                    consecutive_timeouts += 1
                    if consecutive_timeouts % TIMEOUT_WARN_EVERY == 1:
                        print(f"[SPI] INT timeout x{consecutive_timeouts} "
                              f"(frame={self.total_frames}, "
                              f"t={time.time() - self._engine_start_time:.0f}s)")
                    self.spi_consecutive_timeouts = consecutive_timeouts
                    if consecutive_timeouts >= MAX_CONSECUTIVE_TIMEOUTS:
                        print(f"[SPI] Radar unresponsive after "
                              f"{consecutive_timeouts} timeouts, stopping.")
                        self._stop_flag.set()
                        break
                    continue
                consecutive_timeouts = 0
                self.spi_consecutive_timeouts = 0

                # ── 拉取 + 解析这一批 HIF 帧 ──
                hif_frames = radar.poll(burst=BURST)

                for f in hif_frames:
                    if f['hdr']['msg_id'] != HIF_MSG_ID_PSIC:
                        continue
                    channel, raw = parse_psic_payload(f['payload'])
                    if channel is None:
                        continue

                    if channel == '1d_data':
                        iq = parse_1d_data(raw)
                        if iq is None:
                            continue
                        # 累积满 8 通道 → flush 一帧
                        if acc.is_complete:
                            result = acc.flush()
                            if result:
                                iq_f, mot, mic, ts = result
                                self.total_frames += 1
                                self.spi_last_frame_ts = time.time()
                                frame_idx = self.total_frames

                                # 计算 z_mean (轻量，平均运动点云的 Z 坐标)
                                if mot is not None and len(mot) > 0:
                                    z_mean = float(np.mean(mot[:, 2]))
                                else:
                                    z_mean = 0.0
                                self.motion_z_mean = z_mean

                                # ★ 推送到推理线程（绝对不能阻塞！）
                                try:
                                    self._frame_q.put_nowait(
                                        (iq_f, mot, mic, ts, frame_idx))
                                except queue.Full:
                                    drop_count += 1
                                    self.spi_drop_count = drop_count
                                    if drop_count % 10 == 1:
                                        print(f"[SPI] frame_q full, "
                                              f"dropped {drop_count} frames "
                                              f"(inference 跟不上)")

                                # ★ 给 GUI 的轻量回调（10Hz 推送元数据）
                                if self.on_frame:
                                    wall_t = time.time() - self._engine_start_time
                                    try:
                                        self.on_frame(
                                            frame_idx, wall_t, z_mean,
                                            self.emotion_label,
                                            list(self.emotion_probs),
                                            self.breathing_bpm,
                                            self.chest_distance_cm)
                                    except Exception:
                                        pass
                        acc.channels.append(iq)

                    elif channel == 'motion_point_cloud':
                        pc = parse_float_point_cloud(raw)
                        if pc is not None and len(pc) > 0:
                            acc.motion_pc = pc

                    elif channel == 'micro_point_cloud':
                        mpc = parse_float_point_cloud(raw)
                        if mpc is not None and len(mpc) > 0:
                            acc.micro_pc = mpc

                # ── 每轮耗时观测（超过 80ms 就告警，预警 100ms 红线）──
                loop_ms = (time.monotonic() - t_loop_start) * 1000
                if loop_ms > 80:
                    loop_slow_count += 1
                    self.spi_slow_loops = loop_slow_count
                    if loop_slow_count % 5 == 1:
                        print(f"⚠ [SPI] Slow loop iteration: {loop_ms:.0f}ms "
                              f"(已累计 {loop_slow_count} 次，接近雷达 100ms 阈值)")

                now = time.time()
                if now - last_health_ts >= HEALTH_REPORT_S:
                    last_health_ts = now
                    self._emit_health("spi")

        except Exception as e:
            print(f"[SPI] Loop crashed: {e}")
            import traceback
            traceback.print_exc()
        finally:
            try:
                radar.close()
            except Exception:
                pass
            print(f"[SPI] Loop exit (frames={self.total_frames}, "
                  f"q_drops={drop_count}, slow_loops={loop_slow_count})")
            self._emit_health("spi_exit")
            # SPI 退出意味着采集结束，通知推理线程也退
            self._stop_flag.set()

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 线程 2: 推理（耗时 200-400ms，不影响 SPI）
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _inference_loop(self):
        """
        推理线程。从 _frame_q 取帧 → 累积 RingBuffer → 满足条件就推理。

        三阶段流程：
          阶段 1 (frame 0~599):       buf 填充，不做任何特征提取
          阶段 2 (frame ≥600 且 wall<240s): 每 STEP_SIZE 帧提一次特征 → baseline_feats
          阶段 3 (wall ≥240s):        每 STEP_SIZE 帧提特征 + Z-score + 推理 + sticky
        """
        buf = RingBuffer(capacity=WIN_SIZE)
        baseline_feats = []        # 阶段 2 累积的特征向量
        history_feats = []         # 阶段 3 用于 running std
        personal_baseline = None
        peak_bin = None
        last_inf_frame = -STEP_SIZE
        current_state = 'calm'
        switch_counter = 0
        baseline_done_emitted = False
        baseline_motion_warned = False
        baseline_duration_s = self.baseline_min * 60
        last_stats_frame = 0
        last_health_ts = 0.0

        try:
            while not self._stop_flag.is_set():
                # 从 SPI 线程取一帧（超时 1s 让 stop_flag 能被检查到）
                try:
                    iq_f, mot, mic, ts, frame_idx = self._frame_q.get(timeout=1.0)
                except queue.Empty:
                    continue

                buf.append(iq_f, mot, mic, ts)

                # ── Gate 1: 阶段 1 期间（buf 没满 600 帧）──
                if not buf.has_full_window:
                    continue

                # ── Gate 2: 距上次推理不足 STEP_SIZE 帧 ──
                if frame_idx - last_inf_frame < STEP_SIZE:
                    continue
                last_inf_frame = frame_idx

                # ── 推理本体 ──
                t_inf_start = time.monotonic()
                try:
                    iq_arr, motion_arr, micro_arr, motion_cnt, micro_cnt = \
                        buf.get_window_data()

                    # 相位提取（peak_bin 第一次搜，后续锁定加速）
                    phase_filt, detected_bin = extract_phase(
                        iq_arr, self.dist_cm, peak_bin_hint=peak_bin)
                    if peak_bin is None:
                        peak_bin = detected_bin
                        print(f"[Inference] Phase locked: peak_bin={peak_bin}, "
                              f"chest_dist={peak_bin * 7.5:.0f}cm "
                              f"(expected {self.dist_cm}cm)")
                    self.chest_distance_cm = peak_bin * 7.5

                    feat = extract_window_features(
                        phase_filt, motion_arr, micro_arr,
                        micro_cnt, motion_cnt, 0, WIN_SIZE, FS,
                        self.feature_cols)

                    if len(feat) != self.n_features:
                        print(f"⚠ [Inference] feature dim mismatch: "
                              f"got {len(feat)} expected {self.n_features}")
                        continue

                    # 在 log 变换之前取原始 br_bpm（便于 GUI 显示）
                    self.breathing_bpm = float(feat[self._br_bpm_idx])

                    if self.log_transform:
                        feat = feat.copy()
                        feat[self.log_mask] = np.log1p(feat[self.log_mask])

                    wall_elapsed = time.time() - self._engine_start_time
                    self.elapsed_seconds = wall_elapsed
                    in_baseline = wall_elapsed < baseline_duration_s

                    # ════════════════════════════════════════════════════════
                    # 阶段 2: 基线采集
                    # ════════════════════════════════════════════════════════
                    if in_baseline:
                        baseline_feats.append(feat)
                        self.inference_baseline_windows = len(baseline_feats)

                        # 基线阶段运动告警（每次 session 只警告一次）
                        if (not baseline_motion_warned
                                and len(motion_cnt) > 0):
                            cur_mot_avg = float(np.mean(motion_cnt))
                            if cur_mot_avg > BASELINE_MOTION_THRESHOLD:
                                print(f"⚠ [Baseline] Motion detected "
                                      f"(avg {cur_mot_avg:.0f} points/frame) "
                                      f"at t={wall_elapsed:.0f}s. "
                                      f"Please sit still for accurate baseline.")
                                baseline_motion_warned = True

                        if self.on_baseline_progress:
                            try:
                                self.on_baseline_progress(
                                    wall_elapsed / baseline_duration_s)
                            except Exception:
                                pass

                        # 接近 4 分钟末：构建 personal_baseline
                        if (not baseline_done_emitted
                                and wall_elapsed >= baseline_duration_s - 2.0):
                            if baseline_feats:
                                personal_baseline = np.mean(
                                    baseline_feats, axis=0)
                                history_feats = baseline_feats.copy()
                                print(f"[Baseline] Built from "
                                      f"{len(baseline_feats)} windows "
                                      f"({len(baseline_feats) * 2:.0f}s coverage).")
                            else:
                                personal_baseline = self.global_calm_mean
                                history_feats = []
                                print(f"⚠ [Baseline] No baseline windows! "
                                      f"Falling back to global_calm_mean. "
                                      f"Z-score may be poorly calibrated.")
                            self.personal_baseline = personal_baseline
                            baseline_done_emitted = True
                            if self.on_baseline_done:
                                try:
                                    self.on_baseline_done()
                                except Exception:
                                    pass

                    # ════════════════════════════════════════════════════════
                    # 阶段 3: 训练态推理
                    # ════════════════════════════════════════════════════════
                    else:
                        # 兜底：基线阶段一窗成功也没有（罕见）
                        if personal_baseline is None:
                            if baseline_feats:
                                personal_baseline = np.mean(
                                    baseline_feats, axis=0)
                                history_feats = baseline_feats.copy()
                            else:
                                personal_baseline = self.global_calm_mean
                                history_feats = []
                                print(f"⚠ [Inference] Late fallback to "
                                      f"global_calm_mean.")
                            self.personal_baseline = personal_baseline
                            if not baseline_done_emitted:
                                baseline_done_emitted = True
                                if self.on_baseline_done:
                                    try:
                                        self.on_baseline_done()
                                    except Exception:
                                        pass

                        # history_feats 上限保护（防长会话内存爆炸）
                        history_feats.append(feat)
                        if len(history_feats) > MAX_HISTORY:
                            history_feats = history_feats[-MAX_HISTORY:]
                        self.inference_history_windows = len(history_feats)

                        # Z-score 归一化
                        current_std = np.std(history_feats, axis=0)
                        current_std[current_std < 1e-9] = 1.0
                        feat_norm = np.nan_to_num(
                            (feat - personal_baseline) / current_std,
                            nan=0.0).reshape(1, -1)

                        # 模型推理
                        probs = self.model.predict_proba(feat_norm)[0]
                        raw_idx = int(np.argmax(probs))
                        raw_label = (
                            self.label_encoder.classes_[raw_idx]
                            if hasattr(self.label_encoder, 'classes_')
                            else self.label_encoder.inverse_transform([raw_idx])[0]
                        )

                        # Sticky State Machine（迟滞防抖）
                        target = raw_label
                        if (current_state == 'frustration'
                                and target == 'pleasure'):
                            confirm_threshold = FRUS_TO_PLEA_THRESHOLD
                        elif current_state == target:
                            confirm_threshold = 1
                        else:
                            confirm_threshold = DEFAULT_SWITCH_THRESHOLD

                        if target != current_state:
                            switch_counter += 1
                            if switch_counter >= confirm_threshold:
                                current_state = target
                                switch_counter = 0
                        else:
                            switch_counter = 0

                        # 发布状态（SPI 线程会读这两个字段转发给 GUI）
                        self.emotion_label = current_state
                        self.emotion_probs = list(probs)
                        self.inference_count += 1
                        self.inference_last_ts = time.time()
                        self.inference_last_frame = frame_idx

                        if self.on_inference:
                            try:
                                self.on_inference(
                                    frame_idx, feat, probs, current_state,
                                    self.breathing_bpm)
                            except Exception:
                                pass

                    # 推理耗时告警
                    inf_ms = (time.monotonic() - t_inf_start) * 1000
                    if inf_ms > INFERENCE_SLOW_MS:
                        self.inference_slow_count += 1
                        print(f"⚠ [Inference] Slow window: {inf_ms:.0f}ms "
                              f"at frame {frame_idx}")

                    # 周期性健康度报告（每 ~30s 一行）
                    if frame_idx - last_stats_frame >= int(FS * 30):
                        last_stats_frame = frame_idx
                        wall_now = time.time() - self._engine_start_time
                        phase_name = "Baseline" if in_baseline else "Training"
                        fps = frame_idx / max(wall_now, 1)
                        print(f"[Stats] {phase_name} t={wall_now:.0f}s "
                              f"frame={frame_idx} fps={fps:.1f} "
                              f"mot={float(np.mean(motion_cnt)):.1f}pts "
                              f"mic={float(np.mean(micro_cnt)):.1f}pts "
                              f"br={self.breathing_bpm:.1f}bpm "
                              f"chest={self.chest_distance_cm:.0f}cm "
                              f"emo={self.emotion_label} "
                              f"q={self._frame_q.qsize()}")

                    now_h = time.time()
                    if now_h - last_health_ts >= HEALTH_REPORT_S:
                        last_health_ts = now_h
                        self._emit_health("inference")

                except Exception as e:
                    inf_ms = (time.monotonic() - t_inf_start) * 1000
                    print(f"[Inference] Error at frame {frame_idx} "
                          f"({inf_ms:.0f}ms): {e}")
                    import traceback
                    traceback.print_exc()
                    self.inference_last_error = repr(e)
                    self._emit_health("inference_error")
                    continue

        except Exception as e:
            print(f"[Inference] Loop crashed: {e}")
            import traceback
            traceback.print_exc()
        finally:
            self.inference_baseline_windows = len(baseline_feats)
            self.inference_history_windows = len(history_feats)
            self._emit_health("inference_exit")
            print(f"[Inference] Loop exit "
                  f"(last_inf_frame={last_inf_frame}, "
                  f"baseline_feats={len(baseline_feats)}, "
                  f"history_feats={len(history_feats)})")

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 状态查询接口（与 v2.0 一致）
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def get_latest(self):
        """快照最近一帧的关键状态。"""
        return (self.total_frames,
                time.time() - self._engine_start_time if self._engine_start_time > 0 else 0.0,
                self.motion_z_mean,
                self.emotion_label,
                self.emotion_probs,
                self.breathing_bpm,
                self.chest_distance_cm)

    def get_summary(self):
        return {
            'subject': self.subject,
            'total_frames': self.total_frames,
            'elapsed_s': self.elapsed_seconds,
            'breathing_bpm': self.breathing_bpm,
            'emotion_label': self.emotion_label,
            'emotion_probs': self.emotion_probs,
        }


# ═══════════════════════════════════════════════════════════════════════════════
# CLI runner — 与 v2.0 兼容
# ═══════════════════════════════════════════════════════════════════════════════

def run_cli(subject, dist_cm, baseline_min=BASELINE_MIN_DEFAULT,
            model_path=None, save_csv=True):
    """命令行入口（终端 print 回调）。"""
    from datetime import datetime

    engine = RehabEngine(subject=subject, dist_cm=dist_cm,
                         model_path=model_path, baseline_min=baseline_min)

    csv_file = None
    csv_path = None

    def _on_baseline_progress(pct):
        pass

    def _on_baseline_done():
        nonlocal csv_file, csv_path
        print(f"  [{engine.total_frames // 10}s] 基线采集完成")
        print()
        print("=" * 60)
        print(f"  >>> {baseline_min:.0f}分钟基线完成！请开始抬腿训练！<<<")
        print("=" * 60)
        print()
        hdr = f"{'Time(s)':<10} {'Prediction':<14} " \
              f"{'calm%':<8} {'frus%':<8} {'plea%':<8}"
        print(hdr)
        print('-' * len(hdr))

        if save_csv:
            ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")
            csv_path = os.path.join(os.getcwd(),
                                     f"{subject}_{ts_str}_predictions.csv")
            csv_file = open(csv_path, 'w', encoding='utf-8')
            csv_file.write('time_s,prediction,prob_calm,prob_frus,prob_plea\n')

    def _on_inference(idx, feats, probs, label, br_bpm):
        t_sec = idx / FS
        print(f"{t_sec:<10.1f} {label:<14} "
              f"{probs[0]:<8.2f} {probs[1]:<8.2f} {probs[2]:<8.2f}")
        if csv_file:
            csv_file.write(
                f"{t_sec:.1f},{label},"
                f"{probs[0]:.4f},{probs[1]:.4f},{probs[2]:.4f}\n")
            csv_file.flush()

    engine.on_frame = None
    engine.on_baseline_progress = _on_baseline_progress
    engine.on_baseline_done = _on_baseline_done
    engine.on_inference = _on_inference

    print()
    print("=" * 60)
    print("  实时情绪识别康复系统 v3.0")
    print(f"  受试者: {subject}  |  距离: {dist_cm}cm")
    print(f"  模型: ThresholdClassifier {engine.n_features}特征 3分类")
    print(f"  架构: SPI 线程 + 推理线程拆分（修复雷达 100ms 超时问题）")
    print(f"  基线: {baseline_min}分钟")
    print("=" * 60)
    print()
    print(f">>> 请静坐，保持平静呼吸（{baseline_min:.0f}分钟基线采集）<<<")
    print()

    try:
        engine.start()
    except KeyboardInterrupt:
        pass
    finally:
        if csv_file:
            csv_file.close()

    summary = engine.get_summary()
    print()
    print("=" * 60)
    print(f"  康复 session 结束")
    print(f"  受试者: {subject}  |  总帧数: {summary['total_frames']}  |  "
          f"时长: {summary['elapsed_s']:.0f}s")
    if csv_path:
        print(f"  预测结果: {csv_path}")
    print("=" * 60)
