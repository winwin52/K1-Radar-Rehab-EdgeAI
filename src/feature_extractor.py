#!/usr/bin/env python3
"""
Feature extraction module for real-time emotion inference (v2.0).

Pure Python (no hardware dependencies). Shared by:
  - collect_realtime_v3.py  (K1 real-time SPI collection)
  - GUI/npz_replay.py       (Windows NPZ replay test)

Extracts features from a 60-second window of radar data.
Feature count and order are determined dynamically by model.pkl's feature_cols
(current model: 29 features, selected from a larger pool).

Key changes from v1.0 (52-feature model):
  - log_transform: np.log1p() applied before normalization
  - Z-score normalization: (feat - baseline) / running_std
  - Sticky state machine for hysteresis (in rehab_engine.py)
  - ThresholdClassifier wrapper (pleasure_threshold=0.50)
"""

import numpy as np
from scipy.signal import butter, filtfilt, detrend, find_peaks
from scipy.stats import kurtosis, skew

# ═══════════════════════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════════════════════

FS = 10.0                 # frame rate (Hz)
WINDOW_SEC = 60           # window length (seconds)
STEP_SEC = 2              # sliding step (seconds)
BREATH_BAND = (0.1, 0.6)  # respiratory frequency band (Hz)
WIN_SIZE = int(WINDOW_SEC * FS)    # 600 frames
STEP_SIZE = int(STEP_SEC * FS)     # 20 frames
NFFT_BREATH = 8192
RANGE_BINS = 256
N_CHANNELS = 8
PC_COLS = 5

# ═══════════════════════════════════════════════════════════════════════════════
# Signal Processing Helpers
# ═══════════════════════════════════════════════════════════════════════════════

def butter_bandpass(data, lo, hi, fs, order=4):
    """4th-order Butterworth bandpass filter."""
    nyq = 0.5 * fs
    b, a = butter(order, [lo / nyq, hi / nyq], btype='band')
    return filtfilt(b, a, data)


def gini(array):
    """Calculate the Gini coefficient of a numpy array."""
    array = np.abs(np.asarray(array).flatten())
    if np.amin(array) < 0:
        array -= np.amin(array)
    array += 1e-9
    array = np.sort(array)
    index = np.arange(1, array.shape[0] + 1)
    n = array.shape[0]
    return ((np.sum((2 * index - n - 1) * array)) / (n * np.sum(array)))


def sample_entropy(L, m, r):
    """Sample Entropy of a 1-D signal."""
    N = len(L)
    if N < m + 1:
        return 0.0

    def _phi(m_val):
        x = np.array([L[i:i + m_val] for i in range(N - m_val + 1)])
        diff = np.abs(x[:, None, :] - x[None, :, :]).max(axis=2)
        count = (diff <= r).sum() - len(x)
        return count / (len(x) * (len(x) - 1) + 1e-9)

    return -np.log(_phi(m + 1) / (_phi(m) + 1e-9) + 1e-9)


def hjorth_params(sig):
    """Hjorth mobility and complexity parameters."""
    if len(sig) < 3:
        return 0.0, 0.0, 0.0
    act = np.var(sig)
    d1 = np.diff(sig)
    mob = np.sqrt(np.var(d1) / (act + 1e-9)) if act > 1e-9 else 0.0
    if mob < 1e-9:
        return act, mob, 0.0
    d2 = np.diff(d1)
    comp = np.sqrt(np.var(d2) / (np.var(d1) + 1e-9)) / mob if np.var(d1) > 1e-9 else 0.0
    return act, mob, comp


def hurst_exponent(sig):
    """Hurst exponent via R/S analysis."""
    if len(sig) < 8:
        return 0.5
    sig = sig - np.mean(sig)
    z = np.cumsum(sig)
    r = np.max(z) - np.min(z)
    s = np.std(sig)
    return np.log10(r / s + 1e-9) / np.log10(len(sig)) if s > 1e-9 else 0.5


# ═══════════════════════════════════════════════════════════════════════════════
# Phase Extraction
# ═══════════════════════════════════════════════════════════════════════════════

def extract_phase(iq, dist_cm, peak_bin_hint=None):
    """
    Extract respiratory phase signal from multi-channel IQ data.

    Parameters
    ----------
    iq : ndarray (N, 8, 256, 2) int16
        Raw IQ data for N frames, 8 channels, 256 range bins.
    dist_cm : float
        Expected distance from radar (cm). Used to narrow the bin search.
    peak_bin_hint : int or None
        If provided, skip the expensive bin search and use this bin directly.
        This is the fast path for real-time after the initial setup.

    Returns
    -------
    phase_filt : ndarray (N,) float64
        Bandpass-filtered respiratory phase signal.
    peak_bin : int
        The range bin index used (pass as hint on next call).
    """
    if peak_bin_hint is not None:
        peak_bin = peak_bin_hint
    else:
        # --- Full bin search (first call only) ---
        # Decimate by 2x, average channels
        iq_sample = iq[::2, :, :, 0] + 1j * iq[::2, :, :, 1]  # (N/2, 8, 256)
        avg_chan = iq_sample.mean(axis=1)                      # (N/2, 256)

        # Search around expected distance (7.5 cm per bin)
        expected_bin = int(dist_cm / 7.5)
        s_min = max(7, expected_bin - 15)
        s_max = min(250, expected_bin + 15)

        # Score each bin by respiratory energy
        total_vars = np.var(avg_chan, axis=0)
        total_vars_norm = total_vars / (np.max(total_vars) + 1e-9)
        breath_scores = np.zeros(avg_chan.shape[1])

        for b in range(s_min, s_max):
            try:
                sig = np.unwrap(np.angle(avg_chan[:, b]))
                filtered = butter_bandpass(detrend(sig), 0.1, 0.6, FS / 2)
                breath_scores[b] = np.var(filtered) * total_vars_norm[b]
            except Exception:
                continue

        peak_bin = np.argmax(breath_scores)
        if breath_scores[peak_bin] == 0:
            peak_bin = np.argmax(total_vars[s_min:s_max]) + s_min

    # SVD-based phase extraction at peak_bin
    try:
        iq_p = iq[:, :, peak_bin, 0] + 1j * iq[:, :, peak_bin, 1]  # (N, 8)
        iq_c = iq_p - np.mean(iq_p, axis=0)
        _, _, vh = np.linalg.svd(np.conj(iq_c.T) @ iq_c)
        pc1 = (iq_c @ vh.T)[:, 0]
        phase = np.unwrap(np.angle(pc1))
    except np.linalg.LinAlgError:
        # SVD failed (e.g. zero matrix) — fallback to phase of first channel
        iq_1ch = iq[:, 0, peak_bin, 0] + 1j * iq[:, 0, peak_bin, 1]
        phase = np.unwrap(np.angle(iq_1ch))

    return butter_bandpass(detrend(phase), *BREATH_BAND, FS), peak_bin


# ═══════════════════════════════════════════════════════════════════════════════
# Breathing Features (11 features extracted, 5 used by current model)
# ═══════════════════════════════════════════════════════════════════════════════

def extract_breathing_features(phase_sig, fs):
    """
    Extract 11 breathing features from a respiratory phase signal window.

    Returns (in order):
      [br_bpm, br_amplitude, br_rhythm, br_variability, br_rmssd,
       br_psd_total, br_samp_en, br_hj_act, br_hj_mob, br_hj_comp,
       br_phase_deriv_var]
    """
    nfft = NFFT_BREATH
    freqs = np.fft.rfftfreq(nfft, d=1.0 / fs)
    psd = np.abs(np.fft.rfft(
        detrend(phase_sig) * np.hanning(len(phase_sig)), n=nfft)) ** 2
    mask = (freqs >= BREATH_BAND[0]) & (freqs <= BREATH_BAND[1])
    if not np.any(mask):
        return [0.0] * 11

    psd_m = psd[mask]
    freqs_m = freqs[mask]
    peak_idx = np.argmax(psd_m)
    peak_f = freqs_m[peak_idx]
    br_bpm = peak_f * 60.0
    br_psd_total = float(np.sum(psd_m))

    # Rhythm: autocorrelation peak
    sig_dt = detrend(phase_sig)
    corr = np.correlate(sig_dt, sig_dt, mode='full')[len(sig_dt) - 1:]
    min_lag = int(fs / BREATH_BAND[1])
    max_lag = int(fs / BREATH_BAND[0])
    l_peaks, _ = find_peaks(corr[min_lag:max_lag])
    br_rhythm = (float(np.max(corr[min_lag:max_lag][l_peaks]) / (corr[0] + 1e-9))
                 if len(l_peaks) > 0 else 0.0)

    # Peak-to-peak variability
    filtered = butter_bandpass(detrend(phase_sig), *BREATH_BAND, fs)
    p_peaks, _ = find_peaks(filtered, distance=min_lag)
    br_variability = 0.0
    br_rmssd = 0.0
    if len(p_peaks) > 2:
        ipi = np.diff(p_peaks) / fs
        br_variability = float(np.std(ipi) / (np.mean(ipi) + 1e-9))
        br_rmssd = float(np.sqrt(np.mean(np.square(np.diff(ipi)))))

    amplitude = float(np.ptp(filtered))
    ds = filtered[::int(fs / 2)]
    br_samp_en = sample_entropy(ds, m=2, r=0.2 * (np.std(ds) + 1e-9))
    hj_act, hj_mob, hj_comp = hjorth_params(filtered)

    # Phase derivative variance (new in v2.0)
    phase_diff = np.diff(filtered)
    br_phase_deriv_var = float(np.var(np.diff(phase_diff))) if len(phase_diff) > 1 else 0.0

    return [br_bpm, amplitude, br_rhythm, br_variability, br_rmssd,
            br_psd_total, br_samp_en, hj_act, hj_mob, hj_comp,
            br_phase_deriv_var]


# ═══════════════════════════════════════════════════════════════════════════════
# Point Cloud Features (23 features per PC type)
# ═══════════════════════════════════════════════════════════════════════════════

def extract_pc_features(pc_slice, pc_counts_win):
    """
    Extract 23 features from point cloud data within a 60s window.

    Parameters
    ----------
    pc_slice : ndarray (M, 5) or None
        All point cloud points in the window. Columns: [x, y, z, v, snr].
    pc_counts_win : ndarray (600,) int32
        Per-frame point counts.

    Returns (in order):
      [count_avg, count_std, count_max,
       vel_abs_avg, vel_abs_max, vel_std,
       spatial_sway, vel_jitter, snr_avg,
       rhythm, dom_freq, spectral_entropy, spectral_flatness,
       cv, ipi_cv, n_peaks, peak_amp_cv, count_gini,
       mean_y, std_y, mean_z, std_z, std_x]
    """
    sig = pc_counts_win.astype(np.float32)
    avg = float(np.mean(sig))
    std = float(np.std(sig))
    mx = float(np.max(sig))
    sig_dt = sig - avg

    # Rhythm from autocorrelation (lag 5..50 frames)
    rhythm_score = 0.0
    if std > 1e-9:
        corr = np.correlate(sig_dt, sig_dt, mode='full')
        corr = corr[len(corr) // 2:]
        corr /= (corr[0] + 1e-9)
        search = corr[5:50]
        peaks, _ = find_peaks(search)
        rhythm_score = float(np.max(search[peaks])) if len(peaks) > 0 else 0.0

    # Frequency-domain features
    fft_mag = np.abs(np.fft.rfft(sig_dt, n=len(sig_dt)))
    psd = fft_mag ** 2
    dom_freq_idx = np.argmax(fft_mag[1:]) + 1 if len(fft_mag) > 1 else 0
    dom_freq = dom_freq_idx / (len(sig_dt) / FS + 1e-9)

    psd_norm = psd / (np.sum(psd) + 1e-9)
    spectral_entropy = float(-np.sum(psd_norm * np.log2(psd_norm + 1e-9)))

    # Spectral Flatness
    log_mean = np.mean(np.log(psd + 1e-6))
    arith_mean = np.mean(psd) + 1e-6
    spectral_flatness = float(np.exp(log_mean) / arith_mean)

    # Peak stats
    height_thresh = np.mean(sig) + 0.05 * (np.std(sig) + 1e-9)
    count_peaks, _ = find_peaks(sig, height=height_thresh, distance=3)
    if len(count_peaks) >= 2:
        ipi = np.diff(count_peaks).astype(np.float32)
        ipi_cv = float(np.std(ipi) / (np.mean(ipi) + 1e-9))
        n_peaks = float(len(count_peaks))
        peak_amp_cv = float(np.std(sig[count_peaks]) / (np.mean(sig[count_peaks]) + 1e-9))
    else:
        ipi_cv = 1.0
        n_peaks = float(len(count_peaks))
        peak_amp_cv = 0.0

    cv = std / (avg + 1e-9)
    count_gini = float(gini(sig))

    if pc_slice is None or len(pc_slice) == 0:
        return [avg, std, mx, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
                rhythm_score, dom_freq, spectral_entropy, spectral_flatness,
                cv, ipi_cv, n_peaks, peak_amp_cv, count_gini,
                0.0, 0.0, 0.0, 0.0, 0.0]

    pts = np.atleast_2d(pc_slice)
    vels = pts[:, 3]

    avg_abs_vel = float(np.mean(np.abs(vels)))
    max_abs_vel = float(np.max(np.abs(vels)))
    vel_std = float(np.std(vels))
    vel_jitter = float(np.var(np.diff(vels))) if len(vels) > 1 else 0.0
    spatial_sway = float(np.std(pts[:, 0]) + np.std(pts[:, 2]))
    avg_snr = float(np.mean(pts[:, 4]))

    mean_y = float(np.mean(pts[:, 1])) if pts.shape[0] > 0 else 0.0
    std_y = float(np.std(pts[:, 1])) if pts.shape[0] > 0 else 0.0
    mean_z = float(np.mean(pts[:, 2])) if pts.shape[0] > 0 else 0.0
    std_z = float(np.std(pts[:, 2])) if pts.shape[0] > 0 else 0.0
    std_x = float(np.std(pts[:, 0])) if pts.shape[0] > 0 else 0.0

    return [avg, std, mx,
            avg_abs_vel, max_abs_vel, vel_std,
            spatial_sway, vel_jitter, avg_snr,
            rhythm_score, dom_freq, spectral_entropy, spectral_flatness,
            cv, ipi_cv, n_peaks, peak_amp_cv, count_gini,
            mean_y, std_y, mean_z, std_z, std_x]


# ═══════════════════════════════════════════════════════════════════════════════
# Velocity V2 sub-features (for motion & micro PCs)
# ═══════════════════════════════════════════════════════════════════════════════

def _extract_vel_v2(pcs_win):
    """Extract 5 velocity distribution features from a window of PC frames."""
    vels = []
    for p in pcs_win:
        if p is not None and len(p) > 0:
            vels.extend(np.abs(np.array(p)[:, 3]).tolist())
    if len(vels) < 5:
        return [0.0] * 5
    v = np.array(vels)
    q75, q25 = np.percentile(v, [75, 25])
    iqr = float((q75 - q25) / (np.mean(v) + 1e-9))
    upper = v[v > np.median(v)]
    lower = v[v <= np.median(v)]
    asym = float(np.mean(upper) - np.mean(lower)) if len(upper) > 0 and len(lower) > 0 else 0.0
    hist, _ = np.histogram(v, bins=15, range=(0, 1.5))
    p = hist / (np.sum(hist) + 1e-9)
    ent = float(-np.sum(p * np.log2(p + 1e-9)))
    kurt_val = float(kurtosis(v)) if np.std(v) > 1e-9 else 0.0
    return [asym, iqr, kurt_val, ent, float(len(find_peaks(hist)[0]))]


# ═══════════════════════════════════════════════════════════════════════════════
# Window Feature Extraction (dynamic feature count via feature_cols)
# ═══════════════════════════════════════════════════════════════════════════════

def extract_window_features(phase_filt, motion_pcs, micro_pcs, micro_counts,
                            motion_counts, start, end, fs, feature_cols):
    """
    Extract all features for one 60-second window, select by feature_cols.

    Builds a full feature dictionary then selects and orders features
    according to `feature_cols` (from model.pkg['feature_cols']).
    This makes the extractor independent of the model's feature set —
    swap model.pkl and the same code adapts automatically.

    Parameters
    ----------
    phase_filt : ndarray (N,) float64
        Full respiratory phase signal.
    motion_pcs, micro_pcs : ndarray of object
        Per-frame point cloud arrays.
    motion_counts, micro_counts : ndarray of int32
        Per-frame point counts.
    start, end : int
        Frame indices defining the window [start, end).
    fs : float
        Frame rate.
    feature_cols : list of str
        Ordered feature names from model.pkl['feature_cols'].

    Returns
    -------
    feat : ndarray (len(feature_cols),) float32
    """
    # ── Group 1: Breathing (11 features) ──
    b_feat = extract_breathing_features(phase_filt[start:end], fs)

    # ── Group 2: Motion PC (23 features) ──
    m_frames = [f for f in motion_pcs[start:end] if f is not None and len(f) > 0]
    m_feat = extract_pc_features(
        np.vstack(m_frames) if m_frames else None,
        motion_counts[start:end])

    # ── Group 3: Micro PC (23 features) ──
    u_frames = [f for f in micro_pcs[start:end] if f is not None and len(f) > 0]
    u_feat = extract_pc_features(
        np.vstack(u_frames) if u_frames else None,
        micro_counts[start:end])

    # ── Cross-modal ──
    mic_mot_ratio_bounded = float(u_feat[0] / (u_feat[0] + m_feat[0] + 1.0))

    # Breath-micro coherence
    ph = phase_filt[start:end] - np.mean(phase_filt[start:end])
    mc = micro_counts[start:end].astype(np.float32) - np.mean(micro_counts[start:end])
    if np.std(ph) > 1e-9 and np.std(mc) > 1e-9:
        cross = np.correlate(
            ph / (np.std(ph) + 1e-9), mc / (np.std(mc) + 1e-9), mode='full')
        coherence = float(np.max(np.abs(cross)) / len(ph))
    else:
        coherence = 0.0

    # Trends
    half = (end - start) // 2
    mic_trend = float(np.mean(micro_counts[start + half:end]) -
                      np.mean(micro_counts[start:start + half]))
    mot_trend = float(np.mean(motion_counts[start + half:end]) -
                      np.mean(motion_counts[start:start + half]))
    ph_trend = float(np.mean(np.abs(phase_filt[start + half:end])) -
                     np.mean(np.abs(phase_filt[start:start + half])))

    # ── V2 expanded features ──
    mc_win = motion_counts[start:end]
    uc_win = micro_counts[start:end]

    mic_jerk = float(np.mean(np.abs(np.diff(uc_win, n=2)))) if len(uc_win) > 2 else 0.0
    mot_jerk = float(np.mean(np.abs(np.diff(mc_win, n=2)))) if len(mc_win) > 2 else 0.0

    t = np.arange(len(mc_win), dtype=np.float32)
    mic_t_slope = float(np.polyfit(t / len(t), uc_win / (np.std(uc_win) + 1e-9), 1)[0])
    mot_t_slope = float(np.polyfit(t / len(t), mc_win / (np.std(mc_win) + 1e-9), 1)[0])

    segs = np.array_split(uc_win, 10)
    mic_t_cv = float(np.std([s.mean() for s in segs]) / (np.mean(uc_win) + 1e-9))
    segs = np.array_split(mc_win, 10)
    mot_t_cv = float(np.std([s.mean() for s in segs]) / (np.mean(mc_win) + 1e-9))

    mot_active_pct = float(np.mean(mc_win > 0))
    mot_skewness = float(skew(mc_win)) if np.std(mc_win) > 1e-9 else 0.0

    freq_per_motion = float(m_feat[10] / (m_feat[0] + 0.1))

    # Velocity V2 (5 each for motion and micro)
    u_v2 = _extract_vel_v2(micro_pcs[start:end])
    m_v2 = _extract_vel_v2(motion_pcs[start:end])

    # ── Assemble full feature dict ──
    all_feats = {
        # Breathing
        'br_bpm': b_feat[0], 'br_amplitude': b_feat[1], 'br_rhythm': b_feat[2],
        'br_variability': b_feat[3], 'br_rmssd': b_feat[4], 'br_psd_total': b_feat[5],
        'br_samp_en': b_feat[6], 'br_hj_act': b_feat[7], 'br_hj_mob': b_feat[8],
        'br_hj_comp': b_feat[9], 'br_phase_deriv_var': b_feat[10],
        # Cross-modal
        'mic_mot_ratio_bounded': mic_mot_ratio_bounded,
        'breath_motion_coherence': coherence,
        'mic_trend': mic_trend, 'mot_trend': mot_trend, 'ph_trend': ph_trend,
        'mic_jerk': mic_jerk, 'mot_jerk': mot_jerk,
        'mic_temporal_slope': mic_t_slope, 'mot_temporal_slope': mot_t_slope,
        'mic_temporal_cv': mic_t_cv, 'mot_temporal_cv': mot_t_cv,
        'mot_active_pct': mot_active_pct, 'mot_skewness': mot_skewness,
        'freq_per_motion': freq_per_motion,
    }

    # Motion PC features
    m_cols = ['mot_count_avg', 'mot_count_std', 'mot_count_max',
              'mot_vel_abs_avg', 'mot_vel_abs_max', 'mot_vel_std',
              'mot_spatial_sway', 'mot_vel_jitter', 'mot_snr_avg',
              'mot_rhythm', 'mot_dom_freq', 'mot_spectral_entropy',
              'mot_spectral_flatness', 'mot_cv', 'mot_ipi_cv',
              'mot_n_peaks', 'mot_peak_amp_cv', 'mot_count_gini',
              'mot_mean_y', 'mot_std_y', 'mot_mean_z', 'mot_std_z', 'mot_std_x']
    for i, c in enumerate(m_cols):
        all_feats[c] = m_feat[i]

    # Micro PC features
    u_cols = ['mic_count_avg', 'mic_count_std', 'mic_count_max',
              'mic_vel_abs_avg', 'mic_vel_abs_max', 'mic_vel_std',
              'mic_spatial_sway', 'mic_vel_jitter', 'mic_snr_avg',
              'mic_rhythm', 'mic_dom_freq', 'mic_spectral_entropy',
              'mic_spectral_flatness', 'mic_cv', 'mic_ipi_cv',
              'mic_n_peaks', 'mic_peak_amp_cv', 'mic_count_gini',
              'mic_mean_y', 'mic_std_y', 'mic_mean_z', 'mic_std_z', 'mic_std_x']
    for i, c in enumerate(u_cols):
        all_feats[c] = u_feat[i]

    # Velocity V2
    v2_u_cols = ['mic_vel_asym', 'mic_vel_iqr', 'mic_vel_kurt', 'mic_vel_ent', 'mic_vel_modes']
    v2_m_cols = ['mot_vel_asym', 'mot_vel_iqr', 'mot_vel_kurt', 'mot_vel_ent', 'mot_vel_modes']
    for i, c in enumerate(v2_u_cols):
        all_feats[c] = u_v2[i]
    for i, c in enumerate(v2_m_cols):
        all_feats[c] = m_v2[i]

    # ── Select and order based on feature_cols ──
    ordered = [all_feats.get(c, 0.0) for c in feature_cols]
    return np.array(ordered, dtype=np.float32)


# ═══════════════════════════════════════════════════════════════════════════════
# Ring Buffer
# ═══════════════════════════════════════════════════════════════════════════════

class RingBuffer:
    """Fixed-size ring buffers for the last WIN_SIZE frames."""

    def __init__(self, capacity=WIN_SIZE):
        self.capacity = capacity
        self.iq = []
        self.motion_pcs = []
        self.motion_counts = []
        self.micro_pcs = []
        self.micro_counts = []
        self.ts = []

    def append(self, iq_frame, motion, micro, timestamp):
        self.iq.append(iq_frame)
        self.motion_pcs.append(motion)
        self.motion_counts.append(len(motion))
        self.micro_pcs.append(micro)
        self.micro_counts.append(len(micro))
        self.ts.append(timestamp)

        if len(self.iq) > self.capacity:
            self.iq = self.iq[-self.capacity:]
            self.motion_pcs = self.motion_pcs[-self.capacity:]
            self.motion_counts = self.motion_counts[-self.capacity:]
            self.micro_pcs = self.micro_pcs[-self.capacity:]
            self.micro_counts = self.micro_counts[-self.capacity:]
            self.ts = self.ts[-self.capacity:]

    @property
    def n_frames(self):
        return len(self.iq)

    @property
    def has_full_window(self):
        return self.n_frames >= WIN_SIZE

    def get_window_data(self):
        """Return the last WIN_SIZE frames as arrays for feature extraction."""
        iq_arr = np.stack(self.iq[-WIN_SIZE:], axis=0)
        motion_arr = np.array(self.motion_pcs[-WIN_SIZE:], dtype=object)
        micro_arr = np.array(self.micro_pcs[-WIN_SIZE:], dtype=object)
        motion_cnt = np.array(self.motion_counts[-WIN_SIZE:], dtype=np.int32)
        micro_cnt = np.array(self.micro_counts[-WIN_SIZE:], dtype=np.int32)
        return iq_arr, motion_arr, micro_arr, motion_cnt, micro_cnt


# ═══════════════════════════════════════════════════════════════════════════════
# Log-transform mask helper
# ═══════════════════════════════════════════════════════════════════════════════

def build_log_mask(feature_cols):
    """Build a boolean mask for features that need log1p transform.

    Features whose names contain keywords related to counts, velocities,
    PSD, amplitude, or jerk get log-transformed before normalization.
    """
    log_keywords = ['count', 'vel', 'psd', 'amplitude', 'jerk']
    return np.array([any(kw in col for kw in log_keywords) for col in feature_cols])
