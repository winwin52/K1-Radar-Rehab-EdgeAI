/**
 * Rehab K1 control console — Vue 3 SPA.
 *
 * Single page, multiple panels switched by `panel` ref:
 *   - 'home'             : tabs (patients / history) + actions
 *   - 'patient_form'     : create patient
 *   - 'plan_editor'      : choose default vs custom plan, then start
 *   - 'patient_history'  : sessions for one patient
 *
 * When device is WORKING, panels are bypassed — a dedicated live monitor
 * card is shown instead.
 *
 * Data flow:
 *   - REST: GET /api/patients, /api/history, GET /api/plans/default
 *   - REST: POST /api/patients, /api/session/start|stop|pause|...
 *   - WebSocket /ws/live: pushes device state changes (live updates)
 *
 * ─── Control-link hardening (Phase 8) ────────────────────────────────
 * Mobile browsers (Android in-app webviews, WeChat, some Safari contexts)
 * silently dismiss `confirm()` / `alert()` so the previous version made
 * pause/stop/abort buttons feel dead. This rewrite:
 *
 *   1. Custom Vue confirm modal + toast — never relies on window.confirm/alert
 *   2. Per-button busy state (no global lock that can deadlock the whole UI)
 *   3. 5s fetch timeout — a hung request can't permanently disable a button
 *   4. Optimistic UI — pause/abort immediately reflect in the local state
 *      before WS confirms, with reconciliation when the WS frame arrives
 *   5. Status echo from HTTP — control endpoints return the new device
 *      status so we don't depend on the WS frame
 *   6. Console.log every control action (visible in mobile Safari dev tools)
 */

const { createApp, ref, computed, reactive, watch, onMounted, onBeforeUnmount } = Vue;

// ----- Constants -----
const FSM_LABELS = {
  'BASELINE':              '基线采集',
  'TRAINING.REP_LIFT':     '抬腿 ↑',
  'TRAINING.REP_HOLD':     '保持 ⊙',
  'TRAINING.REP_LOWER':    '放下 ↓',
  'TRAINING.REP_REST':     '准备',
  'TRAINING.SET_REST':     '组间休息',
  'SUMMARY':               '完成总结',
  'ABORTED':               '已中止',
};

const STATE_LABELS = {
  IDLE: '待机',
  WORKING: '工作中',
  ERROR: '错误',
  UNKNOWN: '未知',
};

const SIDE_LABELS = {
  left: '左膝',
  right: '右膝',
  bilateral: '双侧',
};

// HTTP timeout for control commands. Anything longer than this and we
// assume the request is wedged and free the button.
const FETCH_TIMEOUT_MS = 5000;

createApp({
  setup() {
    // ───── Device state mirror (from WS / HTTP echo) ─────
    const state       = ref('UNKNOWN');
    const subState    = ref(null);
    const patient     = ref(null);
    const sessionId   = ref(null);
    const startedAt   = ref(null);
    const errorMsg    = ref(null);
    const progress    = ref(null);
    const emotion     = ref(null);
    const sensing     = ref(null);
    const gamification = ref(null);       // Phase 9: mountain journey snapshot
    const celebration  = ref(null);       // Phase 9: last session reward payload
    const empathyRequest = ref(null);     // Phase 9: sustained-frustration choice
    const elapsed     = ref('00:00');

    // Local optimistic flags — these flip immediately on user action so
    // the UI feels responsive, then get reconciled when the server pushes
    // the authoritative state. Cleared on each `applyStatus`.
    const pendingPause = ref(false);     // user tapped 暂停, awaiting confirmation
    const pendingResume = ref(false);    // user tapped 继续
    const pendingAbort = ref(false);     // user tapped 终止/结束

    const coachTrigger = ref(null);

    const emotionNames = ['calm', 'frustration', 'pleasure'];
    const emotionZh    = {calm: '平静', frustration: '沮丧', pleasure: '愉悦'};

    // ───── UI navigation state ─────
    const panel = ref('home');
    const tab   = ref('patients');
    const selectedPatient = ref('');
    const historyFilter   = ref('');

    // AI assessment modal
    const assessmentModal = ref({
      open: false, patient: '', sessionId: '',
      loading: false, html: '', error: null,
    });

    // ───── Connection meta ─────
    const deviceName  = ref('k1-rehab');
    const deviceIp    = ref('-');
    const wsConnected = ref(false);

    // Per-button busy registry — replaces the old global `busy` flag.
    // Key is the action name (e.g. 'pause'). True ⇒ that button is in-flight.
    const busy = reactive({});

    // ───── Custom confirm modal — replaces window.confirm() which is
    // suppressed by some mobile webviews / WeChat / Android in-app browsers.
    // Usage: const yes = await confirmDialog('确定要终止吗?', '终止');
    // ───────────────────────────────────────────────────────────────
    const confirmState = reactive({
      open: false, message: '', confirmLabel: '确定', cancelLabel: '取消',
      danger: false, _resolve: null,
    });

    function confirmDialog(message, confirmLabel = '确定',
                           { danger = false, cancelLabel = '取消' } = {}) {
      return new Promise((resolve) => {
        confirmState.message      = message;
        confirmState.confirmLabel = confirmLabel;
        confirmState.cancelLabel  = cancelLabel;
        confirmState.danger       = danger;
        confirmState._resolve     = resolve;
        confirmState.open         = true;
      });
    }

    function confirmYes() {
      const r = confirmState._resolve; confirmState.open = false;
      confirmState._resolve = null;
      if (r) r(true);
    }
    function confirmNo() {
      const r = confirmState._resolve; confirmState.open = false;
      confirmState._resolve = null;
      if (r) r(false);
    }

    // ───── Toast notification system — replaces window.alert() ─────
    // Toasts auto-dismiss after 4s; up to 3 stacked at once.
    const toasts = ref([]);
    let _toastId = 0;
    function toast(message, kind = 'info', durationMs = 4000) {
      const id = ++_toastId;
      toasts.value.push({ id, message, kind });
      // Auto-dismiss
      setTimeout(() => {
        toasts.value = toasts.value.filter(t => t.id !== id);
      }, durationMs);
    }
    function dismissToast(id) {
      toasts.value = toasts.value.filter(t => t.id !== id);
    }

    // ───── Data ─────
    const patients = ref([]);
    const history  = ref([]);
    const defaultPlan = ref({});

    // ───── Forms ─────
    const patientForm = ref({
      name: '', injury_side: 'left', injury_date: '',
      stage_weeks_post_injury: 0, rehab_cycle_total_weeks: 12,
      doctor: '', notes: '',
    });
    const planMode = ref('default');
    const planForm = ref({
      sets: 3, reps_per_set: 12, lift_s: 2.0, hold_s: 3.0, lower_s: 2.0,
      rest_between_rep_s: 2.0, rest_between_set_s: 30, baseline_min: 4,
    });

    // ───── Connection internals ─────
    let ws = null;
    let elapsedTimer = null;
    let reconnectTimer = null;

    // ───── Computed ─────
    const stateLabel = computed(() => STATE_LABELS[state.value] || state.value);
    const fsmLabel   = computed(() => FSM_LABELS[subState.value] || subState.value || '-');
    const fsmSlug    = computed(() =>
      (subState.value || '').toLowerCase().replace(/\./g, '-')
    );

    // Authoritative paused state — derived from server progress; falls back
    // to optimistic flag during the round-trip window. This is what the
    // template binds to.
    const isPaused = computed(() => {
      if (pendingPause.value) return true;
      if (pendingResume.value) return false;
      return !!(progress.value && progress.value.paused);
    });

    // When user tapped abort/stop, show a banner while we wait for the
    // device to actually transition out of WORKING.
    const abortingBanner = computed(() => pendingAbort.value && state.value === 'WORKING');

    const emotionLabel = computed(() => {
      if (!emotion.value || !emotion.value.label) return '-';
      return emotionZh[emotion.value.label] || emotion.value.label;
    });

    function emoProbPct(idx) {
      const p = (emotion.value && emotion.value.probs && emotion.value.probs[idx]) || 0;
      return Math.round(p * 100);
    }

    const sensingDotClass = computed(() => {
      if (!sensing.value) return 'idle';
      if (sensing.value.error)     return 'error';
      if (!sensing.value.running)  return 'idle';
      return sensing.value.mode === 'real' ? 'running-real' : 'running-mock';
    });

    const sensingText = computed(() => {
      if (!sensing.value) return '感知未启动';
      if (sensing.value.error) return `雷达异常: ${sensing.value.error}`;
      if (!sensing.value.running) return '感知已停止';
      const mode = sensing.value.mode === 'real' ? '雷达' : '模拟';
      const fps  = (sensing.value.fps_approx || 0).toFixed(1);
      return `${mode} ${fps} fps`;
    });

    const sensingDiagText = computed(() => {
      const s = sensing.value;
      if (!s) return '';
      const parts = [];
      if (s.last_frame_age_s != null) parts.push(`帧 ${Number(s.last_frame_age_s).toFixed(1)}s 前`);
      if (s.last_inference_age_s != null) parts.push(`推理 ${Number(s.last_inference_age_s).toFixed(1)}s 前`);
      const h = s.engine_health || {};
      if (h.frame_q_size != null) parts.push(`q ${h.frame_q_size}/${h.frame_q_max || '-'}`);
      if (h.spi_slow_loops != null) parts.push(`slow ${h.spi_slow_loops}`);
      return parts.join(' · ');
    });

    const planEstimateS = computed(() => {
      if (planMode.value === 'default') {
        const p = defaultPlan.value;
        const rc = (p.lift_s||0) + (p.hold_s||0) + (p.lower_s||0) + (p.rest_between_rep_s||0);
        return (p.baseline_min||0) * 60 + rc * (p.reps_per_set||0) * (p.sets||0)
             + (p.rest_between_set_s||0) * Math.max(0, (p.sets||0) - 1);
      }
      const p = planForm.value;
      const rc = p.lift_s + p.hold_s + p.lower_s + p.rest_between_rep_s;
      return (p.baseline_min||0) * 60 + rc * (p.reps_per_set||0) * (p.sets||0)
           + (p.rest_between_set_s||0) * Math.max(0, (p.sets||0) - 1);
    });

    const planErrors = computed(() => {
      if (planMode.value !== 'custom') return [];
      const p = planForm.value;
      const errs = [];
      if (p.sets < 1 || p.sets > 10) errs.push('组数 1-10');
      if (p.reps_per_set < 1 || p.reps_per_set > 50) errs.push('单组次数 1-50');
      if (p.lift_s < 0.1 || p.lift_s > 10) errs.push('上抬 0.1-10s');
      if (p.hold_s < 0.1 || p.hold_s > 30) errs.push('保持 0.1-30s');
      if (p.lower_s < 0.1 || p.lower_s > 10) errs.push('下降 0.1-10s');
      if (p.rest_between_rep_s < 0 || p.rest_between_rep_s > 30) errs.push('次间 0-30s');
      if (p.rest_between_set_s < 0 || p.rest_between_set_s > 300) errs.push('组间 0-300s');
      if (p.baseline_min < 0 || p.baseline_min > 10) errs.push('基线 0-10min');
      return errs;
    });

    const planWarning = computed(() => {
      const p = planMode.value === 'custom' ? planForm.value : defaultPlan.value;
      const bm = p.baseline_min;
      if (bm == null || bm < 1) return null;
      if (bm === 1) return '基线仅 1 分钟,全部用于 buffer 填充,无个性化数据 → 退化到全局基线 (识别准确率会降几个点)';
      if (bm === 2) return '基线 2 分钟仅有 30 个个性化窗口 (推荐 90),准确率略受影响';
      if (bm === 3) return '基线 3 分钟有 60 个个性化窗口 (推荐 90),可用';
      return null;
    });

    // ───── Journey helpers (Phase 9) ─────
    const journeyText = computed(() => {
      const g = gamification.value;
      if (!g) return '山路尚未开始';
      return `${g.title || '新手徒步者'} · ${Math.round(g.elevation_m || 0)}m / ${Math.round(g.target_m || 0)}m`;
    });

    function patientJourneyTitle(p) {
      const j = p.journey || {};
      return j.current_title || '新手徒步者';
    }

    function patientElevationText(p) {
      const j = p.journey || {};
      const elev = Math.round(j.total_elevation_m || 0);
      const weeks = p.rehab_cycle_total_weeks || 12;
      const target = weeks * 500;
      return `${elev}m / ${target}m`;
    }

    function patientStreakText(p) {
      const j = p.journey || {};
      const n = j.streak_days || 0;
      return n > 0 ? `连续 ${n} 天 🔥` : '尚未开始';
    }

    const empathyPromptText = computed(() => {
      if (!empathyRequest.value) return '';
      const share = Math.round((empathyRequest.value.share || 0) * 100);
      return `我注意到你今天有点累（沮丧占比约 ${share}%）。要不要调整一下节奏？`;
    });

    // ───── Sync helpers ─────
    function applyStatus(s) {
      const prevState = state.value;
      state.value     = s.state || 'UNKNOWN';
      subState.value  = s.sub_state;
      patient.value   = s.patient;
      sessionId.value = s.session_id;
      startedAt.value = s.started_at;
      errorMsg.value  = s.error_msg;
      progress.value  = s.progress;
      emotion.value   = s.emotion;
      sensing.value   = s.sensing;
      gamification.value = s.gamification;
      celebration.value  = s.celebration;
      empathyRequest.value = s.empathy_request;

      // Reconcile optimistic flags with authoritative state.
      // If the server confirms what we predicted, clear the flag.
      if (progress.value) {
        if (pendingPause.value && progress.value.paused)  pendingPause.value  = false;
        if (pendingResume.value && !progress.value.paused) pendingResume.value = false;
      }
      // Abort optimism clears once the device leaves WORKING.
      if (state.value !== 'WORKING' && pendingAbort.value) {
        pendingAbort.value = false;
      }
      updateElapsed();
    }

    function updateElapsed() {
      if (!startedAt.value) { elapsed.value = '00:00'; return; }
      const sec = Math.max(0, Math.floor(Date.now() / 1000 - startedAt.value));
      const m = String(Math.floor(sec / 60)).padStart(2, '0');
      const s = String(sec % 60).padStart(2, '0');
      elapsed.value = `${m}:${s}`;
    }

    // ───── HTTP helpers ─────
    /**
     * Fetch with timeout + JSON parse + control-friendly error.
     * Throws Error with .message = human-readable Chinese reason.
     */
    async function api(method, path, body, { timeoutMs = FETCH_TIMEOUT_MS } = {}) {
      const ctrl = new AbortController();
      const timer = setTimeout(() => ctrl.abort(), timeoutMs);
      const opts = {
        method,
        headers: {'Content-Type': 'application/json'},
        signal: ctrl.signal,
      };
      if (body !== undefined) opts.body = JSON.stringify(body);
      try {
        const r = await fetch(path, opts);
        if (!r.ok) {
          let detail = `HTTP ${r.status}`;
          try { const j = await r.json(); detail = j.detail || detail; } catch {}
          throw new Error(detail);
        }
        return r.json();
      } catch (e) {
        if (e.name === 'AbortError') {
          throw new Error('请求超时 — 网络不稳或后端无响应');
        }
        throw e;
      } finally {
        clearTimeout(timer);
      }
    }

    // ───── Loaders ─────
    async function loadDeviceInfo() {
      try {
        const d = await api('GET', '/api/device/info');
        if (d.device_name) deviceName.value = d.device_name;
        if (d.ip)          deviceIp.value   = d.ip;
      } catch (e) { console.warn('device info fail:', e); }
    }

    async function loadPatients() {
      try {
        const d = await api('GET', '/api/patients');
        patients.value = d.patients || [];
      } catch (e) { console.warn('patients fail:', e); }
    }

    async function loadHistory() {
      try {
        const q = historyFilter.value ? `?patient=${encodeURIComponent(historyFilter.value)}` : '';
        const d = await api('GET', '/api/history' + q);
        history.value = d.sessions || [];
      } catch (e) { console.warn('history fail:', e); }
    }

    async function loadDefaultPlan() {
      try {
        const d = await api('GET', '/api/plans/default');
        defaultPlan.value = d.plan;
        planForm.value = {...planForm.value, ...d.plan};
      } catch (e) { console.warn('default plan fail:', e); }
    }

    // ───── WebSocket ─────
    function connectWs() {
      if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
      const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
      ws = new WebSocket(`${proto}//${location.host}/ws/live`);
      ws.onopen  = () => { wsConnected.value = true; };
      ws.onerror = () => { wsConnected.value = false; };
      ws.onclose = () => {
        wsConnected.value = false;
        reconnectTimer = setTimeout(connectWs, 2000);
      };
      ws.onmessage = (ev) => {
        try {
          const d = JSON.parse(ev.data);
          if (d.heartbeat) return;
          if (d.state) applyStatus(d);
        } catch {}
      };
    }

    // ───── Form actions ─────
    function openPatientForm() {
      patientForm.value = {
        name: '', injury_side: 'left', injury_date: '',
        stage_weeks_post_injury: 0, doctor: '', notes: '',
      };
      panel.value = 'patient_form';
    }

    async function createPatient() {
      busy.createPatient = true;
      try {
        await api('POST', '/api/patients', patientForm.value);
        await loadPatients();
        panel.value = 'home';
        tab.value = 'patients';
        toast('已创建患者档案', 'success');
      } catch (e) {
        toast('创建失败: ' + e.message, 'error');
      } finally { busy.createPatient = false; }
    }

    function openPlanForPatient(name) {
      selectedPatient.value = name;
      planMode.value = 'default';
      panel.value = 'plan_editor';
    }

    function viewHistory(name) {
      selectedPatient.value = name;
      historyFilter.value = name;
      panel.value = 'patient_history';
      loadHistory();
    }

    // ───── AI assessment modal -----------------------------

    function escapeHtml(s) {
      return String(s).replace(/[&<>"']/g, (c) => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;',
        '"': '&quot;', "'": '&#39;',
      }[c]));
    }

    function markdownToHtml(md) {
      const lines = md.split('\n');
      const out = [];
      let inPara = false;
      function flushPara() {
        if (inPara) { out.push('</p>'); inPara = false; }
      }
      for (const raw of lines) {
        const line = raw.trimEnd();
        if (line.startsWith('## ')) {
          flushPara();
          out.push(`<h3>${escapeHtml(line.slice(3))}</h3>`);
        } else if (line.startsWith('# ')) {
          flushPara();
          out.push(`<h2>${escapeHtml(line.slice(2))}</h2>`);
        } else if (line.startsWith('**') && line.endsWith('**')) {
          flushPara();
          out.push(`<p><strong>${escapeHtml(line.slice(2, -2))}</strong></p>`);
        } else if (!line.trim()) {
          flushPara();
        } else {
          if (!inPara) { out.push('<p>'); inPara = true; }
          else { out.push('<br>'); }
          out.push(escapeHtml(line));
        }
      }
      flushPara();
      return out.join('\n');
    }

    async function viewAssessment(p, sid) {
      assessmentModal.value = {
        open: true, patient: p, sessionId: sid,
        loading: true, html: '', error: null,
      };
      try {
        const r = await fetch(`/api/history/${encodeURIComponent(p)}/${sid}/assessment`);
        if (r.status === 404) {
          assessmentModal.value.loading = false;
          assessmentModal.value.error = '评估报告尚未生成。可点击"重新生成"。';
          return;
        }
        if (!r.ok) {
          throw new Error(`HTTP ${r.status}`);
        }
        const data = await r.json();
        assessmentModal.value.loading = false;
        assessmentModal.value.html = markdownToHtml(data.content || '');
      } catch (e) {
        assessmentModal.value.loading = false;
        assessmentModal.value.error = '加载失败: ' + e.message;
      }
    }

    function closeAssessment() {
      assessmentModal.value.open = false;
    }

    async function regenerateAssessment() {
      const p = assessmentModal.value.patient;
      const s = assessmentModal.value.sessionId;
      if (!p || !s) return;
      busy.regenerate = true;
      try {
        await api('POST', `/api/history/${encodeURIComponent(p)}/${s}/regenerate`);
        await new Promise(r => setTimeout(r, 2000));
        await viewAssessment(p, s);
      } catch (e) {
        toast('重新生成失败: ' + e.message, 'error');
      } finally {
        busy.regenerate = false;
      }
    }

    // ───── Session control ──────────────────────────────────────────
    // Each control function follows the same pattern:
    //   1. Optional confirm via custom modal (NEVER window.confirm)
    //   2. Mark per-button busy
    //   3. Optimistically update local state
    //   4. POST; on success, applyStatus from echoed status
    //   5. On failure, roll back optimistic state + show toast
    //   6. Always clear busy in finally
    // ──────────────────────────────────────────────────────────────

    async function startSession() {
      const body = { patient_name: selectedPatient.value };
      if (planMode.value === 'custom') body.plan_override = planForm.value;
      busy.start = true;
      console.log('[ctrl] startSession', body);
      try {
        const r = await api('POST', '/api/session/start', body);
        if (r.status) applyStatus(r.status);
      } catch (e) {
        toast('启动失败: ' + e.message, 'error');
      } finally { busy.start = false; }
    }

    async function stopSession() {
      const yes = await confirmDialog('确认结束当前 Session?', '结束',
                                      { danger: true });
      if (!yes) return;
      busy.stop = true;
      pendingAbort.value = true;
      console.log('[ctrl] stopSession');
      try {
        const r = await api('POST', '/api/session/stop');
        if (r.status) applyStatus(r.status);
        toast('已发出结束信号', 'info');
      } catch (e) {
        pendingAbort.value = false;
        toast('结束失败: ' + e.message, 'error');
      } finally { busy.stop = false; }
    }

    async function abortSession() {
      const yes = await confirmDialog(
        '确认强制中止? 已采集的数据将保留,但训练记录会标记为"中止"。',
        '强制中止', { danger: true });
      if (!yes) return;
      busy.abort = true;
      pendingAbort.value = true;
      console.log('[ctrl] abortSession');
      try {
        const r = await api('POST', '/api/session/abort');
        if (r.status) applyStatus(r.status);
        toast('已强制中止', 'info');
      } catch (e) {
        pendingAbort.value = false;
        toast('中止失败: ' + e.message, 'error');
      } finally { busy.abort = false; }
    }

    async function pauseSession() {
      busy.pause = true;
      pendingPause.value = true;
      pendingResume.value = false;
      console.log('[ctrl] pauseSession');
      try {
        const r = await api('POST', '/api/session/pause');
        if (r.status) applyStatus(r.status);
      } catch (e) {
        pendingPause.value = false;
        toast('暂停失败: ' + e.message, 'error');
      } finally { busy.pause = false; }
    }

    async function resumeSession() {
      busy.resume = true;
      pendingResume.value = true;
      pendingPause.value = false;
      console.log('[ctrl] resumeSession');
      try {
        const r = await api('POST', '/api/session/resume');
        if (r.status) applyStatus(r.status);
      } catch (e) {
        pendingResume.value = false;
        toast('继续失败: ' + e.message, 'error');
      } finally { busy.resume = false; }
    }

    async function skipSet() {
      const yes = await confirmDialog('跳过本组,直接进入组间休息?', '跳过');
      if (!yes) return;
      busy.skip = true;
      console.log('[ctrl] skipSet');
      try {
        const r = await api('POST', '/api/session/skip_set');
        if (r.status) applyStatus(r.status);
        toast('已跳过本组', 'info');
      } catch (e) {
        toast('操作失败: ' + e.message, 'error');
      } finally { busy.skip = false; }
    }

    async function chooseEmpathy(choice) {
      if (!empathyRequest.value) return;
      busy.empathy = true;
      console.log('[ctrl] empathyChoice', choice);
      try {
        const r = await api('POST', '/api/session/empathy_choice', {choice});
        if (r.status) applyStatus(r.status);
        const map = {
          continue: '好，我们继续按当前节奏走。',
          reduce_2: '收到，今天少一点也没关系。',
          rest_1m: '收到，先休息一分钟。',
          skip_set: '收到，先结束这一组。',
        };
        toast(map[choice] || '已处理', 'info');
      } catch (e) {
        toast('处理失败: ' + e.message, 'error');
      } finally {
        busy.empathy = false;
      }
    }

    // ───── Format helpers ─────
    function sideLabel(side) { return SIDE_LABELS[side] || side || '-'; }
    function fmtDate(ts) {
      if (!ts) return '-';
      const d = new Date(ts * 1000);
      return `${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
    }
    function fmtSessionId(sid) {
      const m = sid.match(/^(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})$/);
      if (!m) return sid;
      return `${m[1]}-${m[2]}-${m[3]} ${m[4]}:${m[5]}`;
    }
    function downloadUrl(s) {
      return `/api/history/${encodeURIComponent(s.patient)}/${s.session_id}/download`;
    }

    // ───── React to state changes ─────
    watch(state, (newS, oldS) => {
      if (oldS === 'WORKING' && newS === 'IDLE') {
        loadHistory();
        loadPatients();
        panel.value = 'home';
        // Clear any stale optimistic flags from the previous session.
        pendingPause.value = false;
        pendingResume.value = false;
        pendingAbort.value = false;
      }
    });

    // ───── Lifecycle ─────
    onMounted(async () => {
      await Promise.all([
        loadDeviceInfo(),
        loadPatients(),
        loadHistory(),
        loadDefaultPlan(),
      ]);
      connectWs();
      elapsedTimer = setInterval(updateElapsed, 1000);
    });

    onBeforeUnmount(() => {
      if (ws) { ws.onclose = null; ws.close(); }
      if (elapsedTimer) clearInterval(elapsedTimer);
      if (reconnectTimer) clearTimeout(reconnectTimer);
    });

    return {
      // state mirrors
      state, subState, patient, sessionId, errorMsg, elapsed, progress,
      emotion, sensing, gamification, celebration, empathyRequest, coachTrigger,
      // UI nav
      panel, tab, selectedPatient, historyFilter,
      assessmentModal,
      // meta
      deviceName, deviceIp, wsConnected, busy,
      // pending / banner
      pendingPause, pendingResume, pendingAbort, abortingBanner,
      // confirm + toast
      confirmState, confirmYes, confirmNo,
      toasts, dismissToast,
      // data
      patients, history, defaultPlan,
      // forms
      patientForm, planMode, planForm,
      // computed
      stateLabel, fsmLabel, fsmSlug, isPaused, planErrors, planEstimateS,
      planWarning, journeyText, empathyPromptText, sensingDiagText,
      emotionLabel, sensingDotClass, sensingText,
      // constants (used by template)
      emotionNames, emotionZh,
      // helpers
      emoProbPct,
      patientJourneyTitle, patientElevationText, patientStreakText,
      // actions
      openPatientForm, createPatient,
      openPlanForPatient, viewHistory,
      viewAssessment, closeAssessment, regenerateAssessment,
      startSession, stopSession, abortSession,
      pauseSession, resumeSession, skipSet,
      chooseEmpathy,
      loadHistory,
      // formatters
      sideLabel, fmtDate, fmtSessionId, downloadUrl,
    };
  }
}).mount('#app');
