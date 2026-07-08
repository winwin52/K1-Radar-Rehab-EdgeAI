"""
Process C — pygame HDMI screen renderer.

Subscribes to backend's ZMQ state stream and renders the current state to
fullscreen on the K1's 7" HDMI touchscreen.

Run:
    python -m screen.app                  # dev: windowed, 1024x600
    python -m screen.app --fullscreen     # production on K1
    python -m screen.app --size 800x480   # smaller HDMI panels
"""

import argparse
import json
import os
import socket
import threading
from io import BytesIO

import pygame
import zmq

try:
    import qrcode
    HAS_QRCODE = True
except ImportError:
    HAS_QRCODE = False


# ---- Defaults ---------------------------------------------------------

DEFAULT_W, DEFAULT_H = 1024, 600
ZMQ_ENDPOINT = "tcp://127.0.0.1:5555"
HTTP_PORT = 8000

# Color palette — high-contrast, easy on the eyes from 1-2 m away
COLOR_BG_IDLE  = (15, 25, 40)
COLOR_BG_WORK  = (10, 35, 30)
COLOR_BG_ERROR = (60, 15, 15)
COLOR_FG       = (235, 240, 245)
COLOR_ACCENT   = (90, 180, 200)
COLOR_MUTED    = (130, 145, 165)

# Direct font file paths — tried first because pygame.font.SysFont() silently
# falls back to a Latin-only default when a name like "Microsoft YaHei" isn't
# found, producing tofu/squares for Chinese characters.
CJK_FONT_FILES = [
    # Linux (Bianbu / Ubuntu / Debian)
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",  # Bianbu fallback
    # macOS
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    # Windows
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/simsun.ttc",
]

# SysFont names (fallback if no direct file matches)
CJK_FONT_SYSNAMES = [
    "Noto Sans CJK SC",
    "Noto Sans CJK",
    "Source Han Sans CN",
    "Microsoft YaHei",
    "WenQuanYi Micro Hei",
    "PingFang SC",
    "Noto Serif CJK SC",
]


# ---- Utilities --------------------------------------------------------

def get_local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def _font_renders_chinese(font: pygame.font.Font, size: int) -> bool:
    """Verify the font actually has CJK glyphs by rendering '中' and checking width.

    pygame.font.SysFont() silently returns the default Latin-only font when
    asked for a name it can't find — that font renders '中' as a tiny
    missing-glyph placeholder (width well under size * 0.4 px).
    """
    try:
        surf = font.render("中", True, (255, 255, 255))
        # A real CJK glyph is roughly square at the requested size.
        return surf.get_width() >= size * 0.4
    except Exception:
        return False


def load_chinese_font(size: int) -> pygame.font.Font:
    """Load a Chinese-capable font of the requested size.

    Strategy:
      1. Try known font files directly (most reliable across Linux + Windows).
      2. Fall back to pygame.font.SysFont() with a CJK render validation.
      3. Last resort: default font (won't render Chinese but won't crash).
    """
    # 1. Direct file paths
    for path in CJK_FONT_FILES:
        if not os.path.exists(path):
            continue
        try:
            f = pygame.font.Font(path, size)
            if _font_renders_chinese(f, size):
                return f
        except Exception:
            continue

    # 2. SysFont with CJK validation
    for name in CJK_FONT_SYSNAMES:
        try:
            f = pygame.font.SysFont(name, size)
            if f is not None and _font_renders_chinese(f, size):
                return f
        except Exception:
            continue

    # 3. Last resort — Latin only, but doesn't crash
    print("[Screen] WARN: no CJK font found, Chinese may not render")
    return pygame.font.Font(None, size)


def make_qr_surface(url: str, target_size: int) -> pygame.Surface:
    """Generate a QR code rendered as a pygame Surface of target_size px square."""
    if not HAS_QRCODE:
        # Fallback: white square so layout still works
        s = pygame.Surface((target_size, target_size))
        s.fill((255, 255, 255))
        return s
    qr = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=2,
    )
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    surf = pygame.image.load(buf)
    return pygame.transform.smoothscale(surf, (target_size, target_size))


# ---- State receiver (background thread, sync ZMQ) ---------------------

class StateReceiver:
    """
    Background thread polling a ZMQ SUB socket.
    Stores the most recent state dict atomically (Python attribute assignment).
    """

    def __init__(self, endpoint: str):
        self.endpoint = endpoint
        self.latest_state: dict | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        ctx = zmq.Context.instance()
        sock = ctx.socket(zmq.SUB)
        sock.connect(self.endpoint)
        sock.setsockopt(zmq.SUBSCRIBE, b"state")
        # Small RCVHWM keeps memory bounded if subscriber is slow.
        # State changes are infrequent (event-driven), no backlog risk.
        sock.setsockopt(zmq.RCVHWM, 100)

        poller = zmq.Poller()
        poller.register(sock, zmq.POLLIN)

        while not self._stop.is_set():
            # 50ms poll = at most 50ms latency between a ZMQ publish and the
            # main render loop picking up the new state. Tighter than 50ms
            # adds CPU for no perceptual benefit.
            socks = dict(poller.poll(timeout=50))
            if sock in socks:
                try:
                    parts = sock.recv_multipart()
                    if len(parts) >= 2:
                        # Drain any queued messages — only the newest matters
                        while sock.poll(0):
                            parts = sock.recv_multipart()
                        topic, payload = parts[0], parts[1]
                        self.latest_state = json.loads(payload.decode("utf-8"))
                except Exception as e:
                    print(f"[Screen] bad state payload: {e!r}")
        sock.close(linger=0)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)


# ---- Renderers --------------------------------------------------------

class Renderer:
    def __init__(self, screen: pygame.Surface):
        self.screen = screen
        self.w, self.h = screen.get_size()
        # Font sizes scaled to height
        self.font_xl    = load_chinese_font(max(48, int(self.h * 0.11)))
        self.font_lg    = load_chinese_font(max(28, int(self.h * 0.055)))
        self.font_md    = load_chinese_font(max(22, int(self.h * 0.04)))
        self.font_sm    = load_chinese_font(max(18, int(self.h * 0.032)))
        # Huge countdown font — cached because pygame.font.Font construction is
        # not free, and we redraw on every state change.
        self.font_countdown = load_chinese_font(max(80, int(self.h * 0.22)))
        # Pre-render QR for IDLE page (URL doesn't change at runtime)
        self.ip = get_local_ip()
        self.url = f"http://{self.ip}:{HTTP_PORT}"
        qr_size = min(int(self.h * 0.55), 380)
        self.qr_surf = make_qr_surface(self.url, qr_size)

    def _center_blit(self, surf: pygame.Surface, y: int) -> None:
        self.screen.blit(surf, (self.w // 2 - surf.get_width() // 2, y))

    # ---- Phase 9: pixel mountain / companion helpers -----------------

    _THEMES = {
        "calm": {
            "bg": (16, 29, 48), "sky": (23, 42, 68),
            "far": (48, 69, 94), "near": (74, 101, 118),
            "path": (210, 180, 120), "accent": (90, 180, 200),
            "text": (235, 240, 245), "muted": (145, 160, 178),
            "companion": (245, 230, 190),
        },
        "frustration": {
            "bg": (38, 28, 34), "sky": (64, 43, 49),
            "far": (96, 66, 72), "near": (128, 84, 74),
            "path": (236, 170, 100), "accent": (240, 199, 94),
            "text": (248, 238, 224), "muted": (190, 160, 145),
            "companion": (255, 215, 170),
        },
        "pleasure": {
            "bg": (17, 38, 34), "sky": (25, 58, 50),
            "far": (54, 92, 72), "near": (84, 130, 82),
            "path": (240, 205, 120), "accent": (140, 220, 125),
            "text": (238, 248, 236), "muted": (150, 180, 158),
            "companion": (250, 235, 180),
        },
    }

    def _theme(self, state: dict | None) -> dict:
        emo = (state or {}).get("emotion") or {}
        return self._THEMES.get(emo.get("label") or "calm", self._THEMES["calm"])

    def _draw_bg(self, state: dict | None) -> dict:
        """Draw a pixel-art mountain background and return the active palette."""
        pal = self._theme(state)
        self.screen.fill(pal["bg"])
        # sky panel
        pygame.draw.rect(self.screen, pal["sky"], (0, 0, self.w, int(self.h * 0.72)))
        # pixel-art sun / moon
        sun_c = pal["accent"]
        pygame.draw.rect(self.screen, sun_c,
                         (int(self.w * 0.78), int(self.h * 0.10), 34, 34))
        pygame.draw.rect(self.screen, pal["sky"],
                         (int(self.w * 0.78) + 8, int(self.h * 0.10) - 2, 34, 34))
        # far mountains
        far_y = int(self.h * 0.45)
        pts1 = [(0, far_y + 70), (int(self.w*0.18), far_y-60),
                (int(self.w*0.35), far_y+50), (int(self.w*0.55), far_y-85),
                (int(self.w*0.78), far_y+60), (self.w, far_y-40),
                (self.w, self.h), (0, self.h)]
        pygame.draw.polygon(self.screen, pal["far"], pts1)
        # near mountains
        near_y = int(self.h * 0.58)
        pts2 = [(0, near_y + 60), (int(self.w*0.22), near_y-90),
                (int(self.w*0.46), near_y+55), (int(self.w*0.66), near_y-110),
                (int(self.w*0.88), near_y+65), (self.w, near_y-45),
                (self.w, self.h), (0, self.h)]
        pygame.draw.polygon(self.screen, pal["near"], pts2)
        # ground
        pygame.draw.rect(self.screen, (12, 18, 24), (0, int(self.h * 0.78), self.w, self.h))
        return pal

    def _draw_mountain_path(self, state: dict | None, y: int | None = None) -> None:
        """Draw the mountain journey progress: path + small climber."""
        pal = self._theme(state)
        g = (state or {}).get("gamification") or {}
        pct = max(0.0, min(1.0, float(g.get("progress_pct") or 0.0)))
        y = y if y is not None else int(self.h * 0.74)
        x0, x1 = int(self.w * 0.12), int(self.w * 0.88)
        # pixelated path: 12 chunky segments
        n = 12
        for i in range(n):
            a = i / n
            b = (i + 0.65) / n
            xa = int(x0 + (x1 - x0) * a)
            xb = int(x0 + (x1 - x0) * b)
            ya = y - int(46 * a)
            yb = y - int(46 * b)
            color = pal["path"] if a <= pct else (55, 60, 68)
            pygame.draw.line(self.screen, color, (xa, ya), (xb, yb), 6)
        # milestone flags at 25/50/75/100%
        for m in (0.25, 0.5, 0.75, 1.0):
            x = int(x0 + (x1 - x0) * m)
            yy = y - int(46 * m)
            col = pal["accent"] if pct >= m else (80, 88, 96)
            pygame.draw.rect(self.screen, col, (x - 3, yy - 22, 5, 22))
            pygame.draw.rect(self.screen, col, (x + 2, yy - 22, 18, 10))
        # climber
        cx = int(x0 + (x1 - x0) * pct)
        cy = y - int(46 * pct)
        self._draw_companion(cx - 18, cy - 44, state, scale=3)

    def _draw_companion(self, x: int, y: int, state: dict | None, scale: int = 4) -> None:
        """Draw a small pixel-art person. Mood follows emotion label."""
        pal = self._theme(state)
        emo = ((state or {}).get("emotion") or {}).get("label") or "calm"
        P = scale
        skin = pal["companion"]
        body = pal["accent"]
        dark = (18, 22, 28)
        # head (blocky circle)
        pygame.draw.rect(self.screen, skin, (x+4*P, y+0*P, 8*P, 7*P))
        pygame.draw.rect(self.screen, skin, (x+3*P, y+2*P, 10*P, 4*P))
        # hair / outline
        pygame.draw.rect(self.screen, dark, (x+4*P, y+0*P, 8*P, 1*P))
        # eyes / expression
        if emo == "frustration":
            pygame.draw.rect(self.screen, dark, (x+6*P, y+3*P, 1*P, 1*P))
            pygame.draw.rect(self.screen, dark, (x+10*P, y+3*P, 1*P, 1*P))
            pygame.draw.rect(self.screen, dark, (x+7*P, y+5*P, 3*P, 1*P))  # flat mouth
        elif emo == "pleasure":
            pygame.draw.rect(self.screen, dark, (x+6*P, y+2*P, 1*P, 1*P))
            pygame.draw.rect(self.screen, dark, (x+10*P, y+2*P, 1*P, 1*P))
            pygame.draw.rect(self.screen, dark, (x+7*P, y+4*P, 3*P, 1*P))
            pygame.draw.rect(self.screen, dark, (x+8*P, y+5*P, 1*P, 1*P))
        else:
            pygame.draw.rect(self.screen, dark, (x+6*P, y+3*P, 1*P, 1*P))
            pygame.draw.rect(self.screen, dark, (x+10*P, y+3*P, 1*P, 1*P))
        # body and limbs
        pygame.draw.rect(self.screen, body, (x+6*P, y+8*P, 5*P, 8*P))
        pygame.draw.rect(self.screen, body, (x+4*P, y+9*P, 2*P, 6*P))
        pygame.draw.rect(self.screen, body, (x+11*P, y+9*P, 2*P, 6*P))
        pygame.draw.rect(self.screen, pal["text"], (x+6*P, y+16*P, 2*P, 6*P))
        pygame.draw.rect(self.screen, pal["text"], (x+9*P, y+16*P, 2*P, 6*P))

    def _draw_progress_card(self, state: dict | None, x: int, y: int, w: int) -> None:
        """Necessary HDMI information: rehab progress + title/elevation."""
        pal = self._theme(state)
        g = (state or {}).get("gamification") or {}
        pct = max(0.0, min(1.0, float(g.get("progress_pct") or 0.0)))
        pygame.draw.rect(self.screen, (0, 0, 0), (x, y, w, 74), border_radius=10)
        pygame.draw.rect(self.screen, pal["accent"], (x, y, w, 74), 1, border_radius=10)
        title = g.get("title") or "新手徒步者"
        elev = int(g.get("elevation_m") or 0)
        target = int(g.get("target_m") or 0)
        stage = g.get("stage") or "山脚"
        t = self.font_sm.render(f"{stage} · {title} · {elev}/{target}m", True, pal["text"])
        self.screen.blit(t, (x + 14, y + 10))
        # bar
        bx, by, bh = x + 14, y + 44, 12
        bw = w - 28
        pygame.draw.rect(self.screen, (45, 52, 62), (bx, by, bw, bh), border_radius=6)
        pygame.draw.rect(self.screen, pal["accent"], (bx, by, int(bw * pct), bh), border_radius=6)

    def render_idle(self, state: dict | None) -> None:
        pal = self._draw_bg(state)
        # Mountain identity first; QR second. Idle should feel like "come start
        # today's path", not like a device control panel.
        title = self.font_lg.render("今天也一起走一段山路", True, pal["text"])
        self._center_blit(title, int(self.h * 0.08))
        self._draw_mountain_path(state, y=int(self.h * 0.60))

        qr_y = int(self.h * 0.18)
        qr_x = int(self.w * 0.06)
        self.screen.blit(self.qr_surf, (qr_x, qr_y))
        url_text = self.font_sm.render(self.url, True, pal["accent"])
        self.screen.blit(url_text, (qr_x, qr_y + self.qr_surf.get_height() + 10))
        hint = self.font_sm.render("手机扫码控制 · HDMI 陪伴显示", True, pal["muted"])
        self.screen.blit(hint, (qr_x, qr_y + self.qr_surf.get_height() + 38))

        # If a celebration survives into IDLE, make it visible.
        celeb = (state or {}).get("celebration") or {}
        if celeb:
            gained = int(celeb.get("elevation_gained") or 0)
            msg = f"刚刚完成 +{gained}m"
            if celeb.get("new_milestones"):
                msg += f" · 解锁 {max(celeb['new_milestones'])}m 里程碑"
            surf = self.font_md.render(msg, True, pal["accent"])
            self.screen.blit(surf, (int(self.w * 0.48), int(self.h * 0.23)))
        else:
            welcome = self.font_md.render("扫码开始今天的康复旅程", True, pal["accent"])
            self.screen.blit(welcome, (int(self.w * 0.48), int(self.h * 0.23)))

        footer = self.font_sm.render("必要信息在屏幕上，详细数据在手机网页", True, pal["muted"])
        self._center_blit(footer, self.h - int(self.h * 0.08))

    def render_working(self, state: dict | None) -> None:
        """Dispatch by FSM sub_state for richer per-phase visuals."""
        # Phase 9: empathy intercept takes visual priority. The patient chooses
        # on the phone; HDMI simply shows that the system noticed and cares.
        if (state or {}).get("empathy_request"):
            self._render_empathy(state)
            self._render_sensing_indicator(state)
            return

        sub = (state or {}).get("sub_state") or ""
        progress = (state or {}).get("progress") or {}
        if sub == "BASELINE":
            self._render_baseline(state, progress)
        elif sub.startswith("TRAINING.REP_"):
            self._render_rep(state, progress, sub)
        elif sub == "TRAINING.SET_REST":
            self._render_set_rest(state, progress)
        elif sub == "SUMMARY":
            self._render_summary(state, progress)
        else:
            self._render_generic_working(state, progress, sub)

        # Sensing indicator overlay — top-right of every WORKING screen
        self._render_sensing_indicator(state)

    # ---- Specific phase renderers -------------------------------

    def _render_baseline(self, state: dict | None, progress: dict) -> None:
        pal = self._draw_bg(state)
        title = self.font_lg.render("先感受一下今天的状态", True, pal["text"])
        self._center_blit(title, int(self.h * 0.12))
        hint = self.font_md.render("基线采集中 · 手机可随时暂停", True, pal["muted"])
        self._center_blit(hint, int(self.h * 0.24))
        self._draw_mountain_path(state, y=int(self.h * 0.62))
        self._draw_progress_card(state, int(self.w * 0.25), int(self.h * 0.70), int(self.w * 0.50))

        countdown = progress.get("countdown_s")
        if countdown is not None:
            mins = int(countdown // 60)
            secs = int(countdown % 60)
            cd_surf = self.font_md.render(f"剩余 {mins:02d}:{secs:02d}", True, pal["accent"])
            self._center_blit(cd_surf, int(self.h * 0.34))
        if progress.get("paused"):
            tag = self.font_md.render("已暂停", True, (240, 199, 94))
            self._center_blit(tag, int(self.h * 0.43))
        self._render_emotion_strip(state)

    def _render_rep(self, state: dict | None, progress: dict, sub: str) -> None:
        pal = self._draw_bg(state)

        # Necessary content 1: rehab progress (set / rep)
        cset = progress.get("current_set", "-")
        sets_total = progress.get("sets_total", "-")
        crep = progress.get("current_rep", "-")
        reps_total = progress.get("reps_total", "-")
        counter = self.font_md.render(
            f"第 {cset}/{sets_total} 组   第 {crep}/{reps_total} 步",
            True, pal["muted"]
        )
        self._center_blit(counter, int(self.h * 0.055))

        # Necessary content 2: rehab action. Big, but not command-like; it is a
        # "step on the mountain" prompt. Countdown is demoted to small text.
        labels = {
            "TRAINING.REP_LIFT":  ("迈步 ↑", (110, 220, 130)),
            "TRAINING.REP_HOLD":  ("稳住 ⊙", (240, 199, 94)),
            "TRAINING.REP_LOWER": ("落脚 ↓", (110, 220, 130)),
            "TRAINING.REP_REST":  ("歇一下",   pal["muted"]),
        }
        label, color = labels.get(sub, (sub, pal["accent"]))
        big = self.font_xl.render(label, True, color)
        self._center_blit(big, int(self.h * 0.18))

        # Pixel climber and journey path are the main visual anchor.
        self._draw_mountain_path(state, y=int(self.h * 0.60))
        self._draw_progress_card(state, int(self.w * 0.24), int(self.h * 0.68), int(self.w * 0.52))

        countdown = progress.get("countdown_s")
        if countdown is not None:
            cd_surf = self.font_sm.render(f"{countdown:.1f}s", True, pal["muted"])
            self.screen.blit(cd_surf, (int(self.w * 0.86), int(self.h * 0.18)))

        if progress.get("paused"):
            tag = self.font_md.render("已暂停", True, (240, 199, 94))
            self._center_blit(tag, int(self.h * 0.42))

        # Necessary content 3: live emotion, kept compact at bottom.
        self._render_emotion_strip(state)


    def _render_set_rest(self, state: dict | None, progress: dict) -> None:
        pal = self._draw_bg(state)
        title = self.font_lg.render("到达一个小平台，休息一下", True, pal["text"])
        self._center_blit(title, int(self.h * 0.10))
        self._draw_mountain_path(state, y=int(self.h * 0.56))
        self._draw_progress_card(state, int(self.w * 0.18), int(self.h * 0.64), int(self.w * 0.64))

        countdown = progress.get("countdown_s")
        if countdown is not None:
            cd_surf = self.font_md.render(f"休息 {int(countdown)}s", True, pal["accent"])
            self._center_blit(cd_surf, int(self.h * 0.28))
        cset = progress.get("current_set", "-")
        sets_total = progress.get("sets_total", "-")
        hint = self.font_md.render(f"已完成 {cset}/{sets_total} 组", True, pal["muted"])
        self._center_blit(hint, int(self.h * 0.36))
        if progress.get("paused"):
            tag = self.font_md.render("已暂停", True, (240, 199, 94))
            self._center_blit(tag, int(self.h * 0.45))
        self._render_emotion_strip(state)

    def _render_summary(self, state: dict | None, progress: dict) -> None:
        pal = self._draw_bg(state)
        title = self.font_lg.render("今天这段山路完成了", True, pal["text"])
        self._center_blit(title, int(self.h * 0.10))
        self._draw_mountain_path(state, y=int(self.h * 0.54))

        celeb = (state or {}).get("celebration") or {}
        gained = int(celeb.get("elevation_gained") or 0)
        if gained:
            msg = f"本次 +{gained}m"
        else:
            msg = "数据已保存"
        if celeb.get("new_milestones"):
            msg += f" · 解锁 {max(celeb['new_milestones'])}m"
        if celeb.get("title_change"):
            msg += f" · 晋升 {celeb['title_change'][-1]}"
        surf = self.font_md.render(msg, True, pal["accent"])
        self._center_blit(surf, int(self.h * 0.28))
        self._draw_progress_card(state, int(self.w * 0.18), int(self.h * 0.66), int(self.w * 0.64))
        footer = self.font_sm.render("辛苦了，详细报告请在手机端查看", True, pal["muted"])
        self._center_blit(footer, self.h - int(self.h * 0.08))

    def _render_generic_working(self, state: dict | None, progress: dict,
                                 sub: str) -> None:
        self.screen.fill(COLOR_BG_WORK)
        title = self.font_xl.render("工作中", True, COLOR_FG)
        self._center_blit(title, int(self.h * 0.2))
        sub_surf = self.font_lg.render(sub or "-", True, COLOR_ACCENT)
        self._center_blit(sub_surf, int(self.h * 0.5))

    def _render_empathy(self, state: dict | None) -> None:
        """Sustained-frustration scene: display-only, patient chooses on phone."""
        pal = self._draw_bg(state)
        self._draw_companion(int(self.w * 0.45), int(self.h * 0.18), state, scale=6)
        title = self.font_lg.render("我注意到你今天有点累", True, pal["text"])
        self._center_blit(title, int(self.h * 0.46))
        req = (state or {}).get("empathy_request") or {}
        share = req.get("share")
        if share is not None:
            sub = f"沮丧持续占比约 {int(float(share) * 100)}%"
        else:
            sub = "我们可以换个节奏"
        sub_surf = self.font_md.render(sub, True, pal["muted"])
        self._center_blit(sub_surf, int(self.h * 0.56))
        hint = self.font_md.render("请在手机上选择：继续 / 少一点 / 休息一分钟", True, pal["accent"])
        self._center_blit(hint, int(self.h * 0.66))
        self._render_emotion_strip(state)

    # ---- Emotion strip (Phase 5) --------------------------------

    # Map label → (Chinese name, color)
    _EMOTION_STYLE = {
        "calm":        ("平静",  (90, 180, 200)),
        "frustration": ("沮丧",  (217, 100, 100)),
        "pleasure":    ("愉悦",  (140, 200, 110)),
    }

    def _render_emotion_strip(self, state: dict | None) -> None:
        """
        Bottom strip showing live emotion + breathing rate.

        Compact, single line, ~6% of screen height. Drawn at the very
        bottom so it doesn't compete with the main rep guidance.
        """
        emo = (state or {}).get("emotion")
        if not emo or not emo.get("label"):
            return
        label = emo["label"]
        zh_name, color = self._EMOTION_STYLE.get(label, (label, COLOR_FG))
        probs = emo.get("probs") or [0.0, 0.0, 0.0]
        br = emo.get("br_bpm")

        # Background strip
        strip_h = int(self.h * 0.085)
        strip_y = self.h - strip_h
        pygame.draw.rect(self.screen, (0, 0, 0, 180),
                         (0, strip_y, self.w, strip_h))
        # Subtle separator line
        pygame.draw.line(self.screen, (50, 60, 75),
                         (0, strip_y), (self.w, strip_y), 1)

        text_y = strip_y + strip_h // 2 - self.font_sm.get_height() // 2

        # Left: 情绪 [name]
        prefix = self.font_sm.render("情绪 ", True, COLOR_MUTED)
        name = self.font_sm.render(zh_name, True, color)
        x = int(self.w * 0.05)
        self.screen.blit(prefix, (x, text_y))
        self.screen.blit(name, (x + prefix.get_width(), text_y))

        # Middle: probability bars
        bar_x = int(self.w * 0.30)
        bar_w = int(self.w * 0.40)
        bar_h = max(6, int(strip_h * 0.18))
        bar_gap = max(2, int(strip_h * 0.06))
        bar_total_h = bar_h * 3 + bar_gap * 2
        bar_y0 = strip_y + (strip_h - bar_total_h) // 2
        for i, lbl in enumerate(("calm", "frustration", "pleasure")):
            _, lc = self._EMOTION_STYLE[lbl]
            p = float(probs[i]) if i < len(probs) else 0.0
            p = max(0.0, min(1.0, p))
            y = bar_y0 + i * (bar_h + bar_gap)
            # Track
            pygame.draw.rect(self.screen, (40, 50, 65),
                             (bar_x, y, bar_w, bar_h))
            # Fill
            pygame.draw.rect(self.screen, lc,
                             (bar_x, y, int(bar_w * p), bar_h))

        # Right side: 距离 + 呼吸 — stacked / two rows to keep them readable
        # at the strip height.
        chest = emo.get("chest_dist_cm")
        right_x = self.w - int(self.w * 0.05)
        if br is not None or chest is not None:
            parts = []
            if chest is not None:
                parts.append(f"距 {chest:.0f} cm")
            if br is not None:
                parts.append(f"呼吸 {br:.1f}")
            label_text = "  ".join(parts)
            label_surf = self.font_sm.render(label_text, True, COLOR_MUTED)
            self.screen.blit(label_surf,
                             (right_x - label_surf.get_width(), text_y))

    # ---- Sensing indicator (top-right of WORKING screens) -------

    def _render_sensing_indicator(self, state: dict | None) -> None:
        """Tiny top-right radar health badge: ● 雷达 mode + fps."""
        s = (state or {}).get("sensing")
        if not s:
            return
        running = bool(s.get("running"))
        mode    = s.get("mode") or "?"
        fps     = s.get("fps_approx", 0)
        error   = s.get("error")

        if error:
            dot_color  = (217, 100, 100)
            text       = f"雷达异常 {error[:20]}"
            text_color = (217, 100, 100)
        elif running:
            dot_color  = (95, 200, 115) if mode == "real" else (240, 199, 94)
            mode_zh    = "雷达" if mode == "real" else "模拟"
            text       = f"{mode_zh} {fps:.1f}fps"
            text_color = COLOR_MUTED
        else:
            dot_color  = (130, 145, 165)
            text       = "感知停止"
            text_color = COLOR_MUTED

        # Tiny dot + text in top-right corner
        text_surf = self.font_sm.render(text, True, text_color)
        x = self.w - text_surf.get_width() - int(self.w * 0.04)
        y = int(self.h * 0.025)
        pygame.draw.circle(self.screen, dot_color,
                           (x - 12, y + text_surf.get_height() // 2), 5)
        self.screen.blit(text_surf, (x, y))

    def render_error(self, state: dict | None) -> None:
        self.screen.fill(COLOR_BG_ERROR)
        title = self.font_xl.render("⚠ 错误", True, COLOR_FG)
        self._center_blit(title, int(self.h * 0.2))
        msg = (state or {}).get("error_msg") or "未知错误"
        body = self.font_lg.render(msg, True, COLOR_FG)
        self._center_blit(body, int(self.h * 0.5))

    def render_waiting(self) -> None:
        self.screen.fill(COLOR_BG_IDLE)
        title = self.font_lg.render("等待连接后端...", True, COLOR_MUTED)
        self._center_blit(title, self.h // 2 - 30)
        hint = self.font_sm.render(f"后端: {ZMQ_ENDPOINT}", True, COLOR_MUTED)
        self._center_blit(hint, self.h // 2 + 20)


# ---- Main loop --------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Rehab K1 screen process")
    parser.add_argument("--fullscreen", action="store_true",
                        help="Fullscreen mode (use on K1 in production)")
    parser.add_argument("--size", default=f"{DEFAULT_W}x{DEFAULT_H}",
                        help="Window size WxH (windowed mode only)")
    parser.add_argument("--zmq", default=ZMQ_ENDPOINT,
                        help="Backend ZMQ PUB endpoint")
    args = parser.parse_args()

    pygame.init()
    pygame.display.set_caption("Rehab K1 Screen")

    if args.fullscreen:
        # FULLSCREEN | DOUBLEBUF picks current display resolution
        screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN | pygame.DOUBLEBUF)
    else:
        try:
            w, h = map(int, args.size.split("x"))
        except Exception:
            w, h = DEFAULT_W, DEFAULT_H
        screen = pygame.display.set_mode((w, h))

    clock = pygame.time.Clock()

    receiver = StateReceiver(args.zmq)
    receiver.start()

    renderer = Renderer(screen)

    print(f"[Screen] Started ({screen.get_size()}, fullscreen={args.fullscreen})")
    print(f"[Screen] Subscribing to {args.zmq}")

    # Render-only-on-state-change loop:
    # pygame software rendering of CJK text on RISC-V CPU is slow (~100-300ms
    # per full redraw). Redrawing at 30 FPS leads to render backlog and
    # perceived lag. Instead we compute a fingerprint of the visually-relevant
    # state fields; only redraw when it changes. With FSM publishing every
    # 0.5s, that's ~2 redraws/s during a session — plenty of CPU headroom.
    def state_fingerprint(s: dict | None) -> tuple:
        if s is None:
            return ("waiting",)
        prog = s.get("progress") or {}
        cd = prog.get("countdown_s")
        # Round countdown to 0.1s so we still update every tick
        cd_q = round(cd, 1) if cd is not None else None
        # Emotion: only the label + bucketed-to-5% probs (cheap dedup) +
        # bucketed br_bpm + chest distance. Avoids re-rendering on every
        # floating-point jitter.
        emo = s.get("emotion") or {}
        emo_key = (
            emo.get("label"),
            tuple(round(p, 2) for p in (emo.get("probs") or [])),
            round(emo["br_bpm"], 1) if emo.get("br_bpm") is not None else None,
            round(emo["chest_dist_cm"], 0) if emo.get("chest_dist_cm") is not None else None,
        )
        sens = s.get("sensing") or {}
        sens_key = (
            sens.get("mode"),
            sens.get("running"),
            round(sens.get("fps_approx", 0.0), 1),
            sens.get("error"),
        )
        g = s.get("gamification") or {}
        g_key = (
            round(float(g.get("progress_pct") or 0.0), 3),
            g.get("title"),
            g.get("stage"),
            int(g.get("elevation_m") or 0),
            int(g.get("target_m") or 0),
        )
        celeb = s.get("celebration") or {}
        celeb_key = (
            int(celeb.get("elevation_gained") or 0),
            tuple(celeb.get("new_milestones") or []),
            tuple(celeb.get("title_change") or []),
        )
        empathy = s.get("empathy_request") or {}
        empathy_key = (
            empathy.get("ts"),
            empathy.get("reason"),
            round(float(empathy.get("share") or 0.0), 2) if empathy else None,
        )
        return (
            s.get("state"),
            s.get("sub_state"),
            s.get("patient"),
            s.get("error_msg"),
            prog.get("current_set"),
            prog.get("current_rep"),
            cd_q,
            prog.get("paused"),
            emo_key,
            sens_key,
            g_key,
            celeb_key,
            empathy_key,
        )

    last_key: tuple | None = None
    running = True
    try:
        while running:
            for ev in pygame.event.get():
                if ev.type == pygame.QUIT:
                    running = False
                elif ev.type == pygame.KEYDOWN:
                    if ev.key == pygame.K_ESCAPE:
                        running = False

            state = receiver.latest_state
            key = state_fingerprint(state)

            if key != last_key:
                # State actually changed → redraw
                last_key = key
                if state is None:
                    renderer.render_waiting()
                else:
                    s = state.get("state", "IDLE")
                    if s == "IDLE":
                        renderer.render_idle(state)
                    elif s == "WORKING":
                        renderer.render_working(state)
                    elif s == "ERROR":
                        renderer.render_error(state)
                    else:
                        renderer.render_waiting()
                pygame.display.flip()

            # 50 fps event-loop cadence; rendering itself only happens on
            # change. This keeps input (Esc/quit) responsive without burning
            # CPU on identical redraws.
            clock.tick(50)
    finally:
        receiver.stop()
        pygame.quit()
        print("[Screen] Bye.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
