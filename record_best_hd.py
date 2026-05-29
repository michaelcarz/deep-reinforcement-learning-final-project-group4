"""
record_best_hd.py
─────────────────────────────────────────────
高品質 720p HD 影片錄製（白話中文版）
- 每個場景最多 N_ATTEMPTS 輪，取存活最久的
- 標題用白話中文，明確指出鎖住哪個關節
- 暗角遮蓋地板網格邊緣
"""

import os
import sys
import subprocess
import numpy as np
import gymnasium as gym
from stable_baselines3 import PPO
from stable_baselines3.common.vec_env import DummyVecEnv, VecNormalize
from PIL import Image, ImageDraw, ImageFont

# ─── Constants ────────────────────────────────────────────────────────────────
BASE = os.path.dirname(os.path.abspath(__file__))
VIDEO_DIR = os.path.join(BASE, "videos_hd")
os.makedirs(VIDEO_DIR, exist_ok=True)

WIDTH, HEIGHT = 1280, 720
FPS = 30
MAX_STEPS = 1000
N_ATTEMPTS = 8

# ─── 中文關節名稱對照表 ──────────────────────────────────────────────────────
JOINT_NAMES_ZH = {
    3: "右髖關節(前後)",  4: "右髖關節(內外)",  5: "右髖關節(上下)",  6: "右膝關節",
    7: "左髖關節(前後)",  8: "左髖關節(內外)",  9: "左髖關節(上下)", 10: "左膝關節",
}
JOINT_NAMES_SHORT = {
    3: "右髖", 4: "右髖", 5: "右髖", 6: "右膝",
    7: "左髖", 8: "左髖", 9: "左髖", 10: "左膝",
}

def locked_joints_description(locked_joints):
    """把鎖住的關節列表轉成白話中文描述"""
    if not locked_joints or len(locked_joints) == 0:
        return ""
    names = []
    seen = set()
    for j in locked_joints:
        short = JOINT_NAMES_SHORT.get(j, f"關節{j}")
        if short not in seen:
            names.append(short)
            seen.add(short)
    return "、".join(names)


# ─── Wrapper ──────────────────────────────────────────────────────────────────
class FaultTolerantHumanoidWrapperOptimized(gym.Wrapper):
    def __init__(self, env, enable_smoothing=True, enable_torso_stabilization=True, use_ema_filter=False):
        super().__init__(env)
        self.stage = 1
        self.enable_smoothing = enable_smoothing
        self.enable_torso_stabilization = enable_torso_stabilization
        self.use_ema_filter = use_ema_filter
        self.action_dim = self.env.action_space.shape[0]
        self.health_vector = np.ones(self.action_dim, dtype=np.float32)

        obs_space = self.env.observation_space
        low = np.concatenate([obs_space.low,
                               np.zeros(self.action_dim, dtype=np.float32),
                               -np.ones(self.action_dim, dtype=np.float32)]).astype(np.float32)
        high = np.concatenate([obs_space.high,
                                np.ones(self.action_dim, dtype=np.float32),
                                np.ones(self.action_dim, dtype=np.float32)]).astype(np.float32)
        self.observation_space = gym.spaces.Box(low=low, high=high, dtype=np.float32)

        self.leg_joint_indices = [3, 4, 5, 6, 7, 8, 9, 10]
        self.step_count = 0
        self.shock_step = -1
        self.locked_joints_this_episode = []

        self.original_mass = self.env.unwrapped.model.body_mass.copy()
        self.original_friction = self.env.unwrapped.model.geom_friction.copy()

        self.global_env_steps = 0
        self.prev_action = np.zeros(self.action_dim, dtype=np.float32)
        self.prev_executed_action = np.zeros(self.action_dim, dtype=np.float32)

    def set_global_steps(self, global_steps): self.global_env_steps = global_steps
    def set_curriculum_stage(self, stage): self.stage = stage
    def _get_obs(self, obs): return np.concatenate([obs, self.health_vector, self.prev_action])

    def reset(self, **kwargs):
        obs, info = self.env.reset(**kwargs)
        self.step_count = 0
        self.health_vector = np.ones(self.action_dim, dtype=np.float32)
        self.locked_joints_this_episode = []

        mass_factor = np.random.uniform(0.95, 1.05, size=self.original_mass.shape)
        self.env.unwrapped.model.body_mass[:] = self.original_mass * mass_factor
        friction_factor = np.random.uniform(0.95, 1.05, size=self.original_friction.shape)
        self.env.unwrapped.model.geom_friction[:] = self.original_friction * friction_factor

        if self.stage == 1:
            pass
        elif self.stage == 2:
            num_locked = np.random.choice([1, 2])
            self.locked_joints_this_episode = np.random.choice(
                self.leg_joint_indices, num_locked, replace=False)
            self.shock_step = 0
        elif self.stage == 3:
            self.shock_step = np.random.randint(100, 250)
            num_locked = np.random.choice([1, 2])
            self.locked_joints_this_episode = np.random.choice(
                self.leg_joint_indices, num_locked, replace=False)

        self.prev_action = np.zeros(self.action_dim, dtype=np.float32)
        self.prev_executed_action = np.zeros(self.action_dim, dtype=np.float32)
        return self._get_obs(obs), info

    def step(self, action):
        self.step_count += 1
        if self.stage in [2, 3]:
            if self.step_count >= self.shock_step:
                steps_since_shock = self.step_count - self.shock_step
                current_health = 0.5 if steps_since_shock < 50 else 0.0
                for j in self.locked_joints_this_episode:
                    self.health_vector[j] = current_health

        if self.use_ema_filter:
            smooth_action = 0.3 * action + 0.7 * self.prev_executed_action
        else:
            smooth_action = action

        masked_action = smooth_action * self.health_vector
        self.prev_executed_action = masked_action.copy()
        obs, reward, terminated, truncated, info = self.env.step(masked_action)

        forward_reward = info.get('reward_linvel', info.get('forward_reward', 0.0))
        if forward_reward > 1.875:
            reward -= (forward_reward - 1.875)

        smoothing_penalty = 0.0
        if self.enable_smoothing:
            action_diff = action - self.prev_action
            smoothing_penalty = 0.05 * np.sum(np.square(action_diff))
            reward -= smoothing_penalty

        torso_penalty = 0.0
        if self.enable_torso_stabilization:
            qvel = self.env.unwrapped.data.qvel
            torso_penalty = 0.5 * (qvel[1]**2 + qvel[2]**2) + 0.1 * np.sum(np.square(qvel[3:6]))
            reward -= torso_penalty

        compensation_bonus = 0.0
        if any(h < 1.0 for h in self.health_vector) and not terminated:
            compensation_bonus = 3.0
            reward += compensation_bonus

        info.update({
            "smoothing_penalty": float(smoothing_penalty),
            "torso_penalty": float(torso_penalty),
            "compensation_bonus": float(compensation_bonus),
            "penalty_weight": 1.0,
        })
        self.prev_action = action.copy()
        return self._get_obs(obs), reward, terminated, truncated, info


# ─── Font Loading ─────────────────────────────────────────────────────────────
def load_fonts():
    font_candidates = [
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Light.ttc",
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
    ]
    font_path = None
    for fp in font_candidates:
        if os.path.exists(fp):
            font_path = fp
            break
    try:
        return {
            "title_lg": ImageFont.truetype(font_path, 34),
            "title_md": ImageFont.truetype(font_path, 24),
            "title_sm": ImageFont.truetype(font_path, 18),
            "body": ImageFont.truetype(font_path, 16),
            "small": ImageFont.truetype(font_path, 14),
            "hero": ImageFont.truetype(font_path, 52),
            "hero_sub": ImageFont.truetype(font_path, 28),
        }
    except Exception:
        default = ImageFont.load_default()
        return {k: default for k in ["title_lg", "title_md", "title_sm", "body", "small", "hero", "hero_sub"]}

FONTS = load_fonts()


# ─── Vignette (暗角) ──────────────────────────────────────────────────────────
_vignette_cache = {}

def get_vignette(w, h):
    """建立暗角遮罩，遮蓋邊緣的地板網格"""
    key = (w, h)
    if key in _vignette_cache:
        return _vignette_cache[key]

    vignette = Image.new('RGBA', (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(vignette)

    # 底部漸層暗角（遮蓋地板網格）
    for y in range(h - 120, h):
        progress = (y - (h - 120)) / 120
        alpha = int(80 * progress * progress)  # Quadratic fade
        draw.line([(0, y), (w, y)], fill=(10, 10, 15, alpha))

    # 左右邊緣暗角
    for x in range(60):
        progress = 1 - x / 60
        alpha = int(50 * progress * progress)
        draw.line([(x, 0), (x, h)], fill=(10, 10, 15, alpha))
        draw.line([(w - 1 - x, 0), (w - 1 - x, h)], fill=(10, 10, 15, alpha))

    # 頂部微暗角
    for y in range(40):
        progress = 1 - y / 40
        alpha = int(30 * progress * progress)
        draw.line([(0, y), (w, y)], fill=(10, 10, 15, alpha))

    _vignette_cache[key] = vignette
    return vignette


# ─── Overlay ──────────────────────────────────────────────────────────────────
def add_hd_overlay(frame, title_main, title_sub, step, color_rgb,
                   is_done=False, health_vector=None, locked_joints=None,
                   shock_active=False):
    """加上白話中文的專業 Overlay"""
    pil_img = Image.fromarray(frame).convert('RGBA')
    w, h = pil_img.size

    # 暗角
    pil_img = Image.alpha_composite(pil_img, get_vignette(w, h)).convert('RGB')
    draw = ImageDraw.Draw(pil_img)

    # ── 頂部半透明橫幅 ──
    banner = Image.new('RGBA', (w, h), (0, 0, 0, 0))
    bd = ImageDraw.Draw(banner)
    for y in range(85):
        alpha = int(190 * (1 - y / 85) ** 0.8)
        bd.line([(0, y), (w, y)], fill=(12, 12, 18, alpha))
    pil_img = Image.alpha_composite(pil_img.convert('RGBA'), banner).convert('RGB')
    draw = ImageDraw.Draw(pil_img)

    # 主標題（白話中文）
    draw.text((24, 10), title_main, font=FONTS["title_lg"], fill=(255, 255, 255))

    # 副標題
    if title_sub:
        draw.text((24, 50), title_sub, font=FONTS["title_sm"], fill=(190, 200, 215))

    # ── 右上角：步數 ──
    draw.text((w - 200, 12), f"第 {step} 步", font=FONTS["title_sm"], fill=(200, 210, 230))
    draw.text((w - 200, 38), f"已走 {step} / {MAX_STEPS}", font=FONTS["title_md"], fill=color_rgb)

    # ── 右側：關節狀態面板 ──
    if health_vector is not None and locked_joints is not None and len(locked_joints) > 0:
        hud_x, hud_y = w - 210, 95
        draw.text((hud_x, hud_y), "🦿 關節狀態", font=FONTS["body"], fill=(180, 190, 210))
        hud_y += 24

        for idx in sorted(locked_joints):
            hv = health_vector[idx]
            name = JOINT_NAMES_ZH.get(idx, f"關節 {idx}")

            if hv >= 1.0:
                status = "✅ 正常"
                bar_color = (60, 200, 100)
                text_color = (150, 210, 160)
            elif hv >= 0.5:
                status = "⚠️ 半損"
                bar_color = (255, 180, 50)
                text_color = (255, 200, 100)
            else:
                status = "🔒 鎖死"
                bar_color = (255, 60, 60)
                text_color = (255, 100, 100)

            draw.text((hud_x, hud_y), f"{name}", font=FONTS["small"], fill=text_color)
            hud_y += 18
            # 健康條
            bar_x = hud_x
            draw.rectangle([bar_x, hud_y, bar_x + 180, hud_y + 8], fill=(40, 40, 50))
            fill_w = int(180 * hv)
            if fill_w > 0:
                draw.rectangle([bar_x, hud_y, bar_x + fill_w, hud_y + 8], fill=bar_color)
            draw.text((bar_x + 185, hud_y - 4), status, font=FONTS["small"], fill=text_color)
            hud_y += 20

    # ── 衝擊警告 ──
    if shock_active:
        pulse = int(200 + 55 * np.sin(step * 0.4))
        draw.text((24, h - 55), "⚡ 關節正在損壞中...",
                  font=FONTS["body"], fill=(255, min(255, pulse), 50))

    # ── 跌倒提示 ──
    if is_done and step < MAX_STEPS:
        fell_ov = Image.new('RGBA', (w, h), (0, 0, 0, 0))
        fd = ImageDraw.Draw(fell_ov)
        fd.rectangle([0, h // 2 - 50, w, h // 2 + 50], fill=(30, 0, 0, 160))
        pil_img = Image.alpha_composite(pil_img.convert('RGBA'), fell_ov).convert('RGB')
        draw = ImageDraw.Draw(pil_img)
        draw.text((w // 2 - 120, h // 2 - 25), "💥 機器人跌倒了",
                  font=FONTS["title_lg"], fill=(255, 80, 80))

    # ── 底部進度條 ──
    bar_h = 5
    draw.rectangle([0, h - bar_h, w, h], fill=(30, 30, 40))
    pw = int(w * step / MAX_STEPS)
    if pw > 0:
        draw.rectangle([0, h - bar_h, pw, h], fill=color_rgb)

    return np.array(pil_img)


def create_title_card(line1, line2, color_rgb, line3=None, duration_frames=90):
    """建立場景標題卡"""
    frames = []
    for f in range(duration_frames):
        img = Image.new('RGB', (WIDTH, HEIGHT), (10, 10, 16))
        draw = ImageDraw.Draw(img)
        alpha = min(1.0, f / 25)  # 淡入

        cx, cy = WIDTH // 2, HEIGHT // 2

        # 裝飾線
        lw = int(280 * alpha)
        lc = tuple(int(c * alpha * 0.6) for c in color_rgb)
        draw.line([(cx - lw, cy - 60), (cx + lw, cy - 60)], fill=lc, width=2)

        # 主標題
        tc = tuple(int(255 * alpha) for _ in range(3))
        bbox = draw.textbbox((0, 0), line1, font=FONTS["hero"])
        tw = bbox[2] - bbox[0]
        draw.text(((WIDTH - tw) // 2, cy - 50), line1, font=FONTS["hero"], fill=tc)

        # 副標題
        bbox2 = draw.textbbox((0, 0), line2, font=FONTS["hero_sub"])
        tw2 = bbox2[2] - bbox2[0]
        sub_c = tuple(int(c * alpha) for c in color_rgb)
        draw.text(((WIDTH - tw2) // 2, cy + 20), line2, font=FONTS["hero_sub"], fill=sub_c)

        # 第三行（關節資訊等）
        if line3:
            bbox3 = draw.textbbox((0, 0), line3, font=FONTS["title_sm"])
            tw3 = bbox3[2] - bbox3[0]
            l3c = tuple(int(180 * alpha) for _ in range(3))
            draw.text(((WIDTH - tw3) // 2, cy + 60), line3, font=FONTS["title_sm"], fill=l3c)

        draw.line([(cx - lw, cy + 95), (cx + lw, cy + 95)], fill=lc, width=2)
        frames.append(np.array(img))

    # 停留
    frames.extend([frames[-1]] * 15)
    return frames


# ─── FFmpeg Writer ────────────────────────────────────────────────────────────
class FFmpegWriter:
    def __init__(self, output_path, width, height, fps=30, crf=18):
        try:
            import imageio_ffmpeg
            ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        except ImportError:
            ffmpeg_exe = "ffmpeg"

        self.proc = subprocess.Popen(
            [ffmpeg_exe, '-y', '-f', 'rawvideo', '-vcodec', 'rawvideo',
             '-pix_fmt', 'rgb24', '-s', f'{width}x{height}', '-r', str(fps),
             '-i', '-', '-c:v', 'libx264', '-preset', 'slow', '-crf', str(crf),
             '-pix_fmt', 'yuv420p', '-movflags', '+faststart', output_path],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.frame_count = 0

    def write_frame(self, frame):
        self.proc.stdin.write(frame.tobytes())
        self.frame_count += 1

    def write_frames(self, frames):
        for f in frames:
            self.write_frame(f)

    def close(self):
        self.proc.stdin.close()
        self.proc.wait()


# ─── Simulate ─────────────────────────────────────────────────────────────────
def simulate_once(sc):
    """跑一輪模擬，收集所有畫面"""
    def make_env():
        env = gym.make("Humanoid-v4", render_mode="rgb_array",
                       healthy_z_range=sc["z_range"], width=WIDTH, height=HEIGHT)
        # 擴大地板範圍，避免走遠後地板網格邊緣露出
        floor_geom_id = 0  # 第一個 geom 通常是地板
        env.unwrapped.model.geom_size[floor_geom_id] = [200, 200, 0.1]
        env = FaultTolerantHumanoidWrapperOptimized(env)
        env.set_curriculum_stage(sc["stage"])
        return env

    raw_env = make_env()
    venv = DummyVecEnv([lambda: raw_env])
    vec_env = VecNormalize.load(os.path.join(BASE, sc["norm_path"]), venv)
    vec_env.training = False
    vec_env.norm_reward = False
    model = PPO.load(os.path.join(BASE, sc["model_path"]), env=vec_env, device="cpu")

    obs = vec_env.reset()
    step = 0
    frames = []
    locked_joints = list(raw_env.locked_joints_this_episode) if hasattr(raw_env, 'locked_joints_this_episode') else []

    # 根據實際鎖住的關節，動態生成白話標題
    joint_desc = locked_joints_description(locked_joints)
    if sc["stage"] == 1:
        title_main = "正常行走"
        title_sub = "所有關節健康，穩定直立步行"
    elif sc["stage"] == 2:
        title_main = f"鎖住{joint_desc}後行走"
        title_sub = f"起步時 {joint_desc} 已鎖死，用剩餘關節代償"
    elif sc["stage"] == 3:
        title_main = "行走中突然鎖住關節"
        title_sub = f"走到一半 {joint_desc} 突然鎖死，嘗試重新平衡"

    while step < MAX_STEPS:
        action, _ = model.predict(obs, deterministic=True)
        obs, _, dones, _ = vec_env.step(action)
        done = dones[0]
        step += 1

        frame = raw_env.render()
        if frame is None:
            continue
        frame = np.array(frame, dtype=np.uint8)
        hv = raw_env.health_vector.copy()

        shock_active = False
        if sc["stage"] == 3 and hasattr(raw_env, "shock_step") and raw_env.shock_step >= 0:
            if step >= raw_env.shock_step and any(h < 1.0 for h in hv):
                shock_active = True
            # Update title when shock happens
            if step == raw_env.shock_step:
                title_main = f"⚡ {joint_desc} 突然鎖死！"

        overlay = add_hd_overlay(
            frame, title_main, title_sub, step, sc["color"],
            is_done=done, health_vector=hv, locked_joints=locked_joints,
            shock_active=shock_active)
        frames.append(overlay)

        if done:
            for _ in range(45):
                fell = add_hd_overlay(
                    frame, title_main, title_sub, step, sc["color"],
                    is_done=True, health_vector=hv, locked_joints=locked_joints)
                frames.append(fell)
            break

    vec_env.close()
    return frames, step, locked_joints, title_main


# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    print("=" * 60)
    print("  雙足機器人韌性測試 — HD 720p 影片錄製")
    print(f"  解析度: {WIDTH}×{HEIGHT} | 編碼: H.264")
    print(f"  每個場景最多嘗試 {N_ATTEMPTS} 次，取最佳結果")
    print("=" * 60)

    SCENARIOS = [
        {
            "name": "scenario_1_healthy",
            "stage": 1,
            "z_range": (1.0, 2.0),
            "model_path": "ppo_humanoid_healthy_mac_baseline",
            "norm_path": "vec_normalize_mac_phase1.pkl",
            "color": (80, 220, 120),
            "min_steps": 900,
            "card_line1": "正常行走",
            "card_line2": "所有關節健康，穩定直立步行",
        },
        {
            "name": "scenario_2_static_failure",
            "stage": 2,
            "z_range": (0.5, 2.0),
            "model_path": "ppo_cybernetic_resilience_mac_polished",
            "norm_path": "vec_normalize_mac_polished.pkl",
            "color": (220, 80, 100),
            "min_steps": 250,
            "card_line1": "鎖住關節後行走",
            "card_line2": "起步時關節已鎖死",
        },
        {
            "name": "scenario_3_dynamic_shock",
            "stage": 3,
            "z_range": (0.5, 2.0),
            "model_path": "ppo_cybernetic_resilience_mac_polished",
            "norm_path": "vec_normalize_mac_polished.pkl",
            "color": (230, 170, 50),
            "min_steps": 400,
            "card_line1": "走路途中突然鎖死",
            "card_line2": "行走中關節突然壞掉",
        },
    ]

    best_results = {}

    for sc in SCENARIOS:
        print(f"\n{'─' * 50}")
        print(f"[場景] {sc['card_line1']}")
        print(f"  目標: ≥ {sc['min_steps']} 步")

        best_frames = None
        best_steps = 0
        best_locked = []
        best_title = ""

        for attempt in range(N_ATTEMPTS):
            frames, steps, locked, title = simulate_once(sc)
            jd = locked_joints_description(locked)
            status = "✅" if steps >= sc["min_steps"] else "  "
            print(f"    第 {attempt+1}/{N_ATTEMPTS} 次: {steps:4d} 步 | 鎖住: {jd or '無'} {status}")

            if steps > best_steps:
                best_steps = steps
                best_frames = frames
                best_locked = locked
                best_title = title

            if steps >= sc["min_steps"]:
                break

        best_results[sc["name"]] = {
            "frames": best_frames,
            "steps": best_steps,
            "locked": best_locked,
            "title": best_title,
        }
        jd = locked_joints_description(best_locked)
        print(f"  → 最佳: {best_steps} 步 | 鎖住: {jd or '無'}")

    # ─── 寫入影片 ─────────────────────────────────────────────────────────────
    print(f"\n{'═' * 60}")
    print("  正在輸出 HD 影片...")

    combined_path = os.path.join(VIDEO_DIR, "combined_all_scenarios_hd.mp4")
    all_writer = FFmpegWriter(combined_path, WIDTH, HEIGHT, FPS)

    for i, sc in enumerate(SCENARIOS):
        result = best_results[sc["name"]]
        jd = locked_joints_description(result["locked"])

        # 標題卡的文字
        if sc["stage"] == 1:
            card1 = "正常行走"
            card2 = "所有關節健康，穩定直立步行"
            card3 = None
        elif sc["stage"] == 2:
            card1 = f"鎖住{jd}後行走"
            card2 = f"起步時 {jd} 已鎖死"
            card3 = f"鎖死關節: {', '.join([JOINT_NAMES_ZH.get(j, str(j)) for j in result['locked']])}"
        elif sc["stage"] == 3:
            card1 = f"走路途中鎖住{jd}"
            card2 = f"行走中 {jd} 突然壞掉"
            card3 = f"鎖死關節: {', '.join([JOINT_NAMES_ZH.get(j, str(j)) for j in result['locked']])}"

        # 標題卡
        title_frames = create_title_card(card1, card2, sc["color"], card3)
        all_writer.write_frames(title_frames)

        # 場景影片
        sc_path = os.path.join(VIDEO_DIR, sc["name"] + "_hd.mp4")
        sc_writer = FFmpegWriter(sc_path, WIDTH, HEIGHT, FPS)
        sc_writer.write_frames(title_frames)

        for frame in result["frames"]:
            sc_writer.write_frame(frame)
            all_writer.write_frame(frame)
        sc_writer.close()

        size_mb = os.path.getsize(sc_path) / 1024 / 1024
        print(f"  → {sc['name']}_hd.mp4 ({size_mb:.1f} MB, {result['steps']} 步)")

        # 場景間隔
        if i < len(SCENARIOS) - 1:
            spacer = np.zeros((HEIGHT, WIDTH, 3), dtype=np.uint8)
            for _ in range(FPS):
                all_writer.write_frame(spacer)

    all_writer.close()
    size_mb = os.path.getsize(combined_path) / 1024 / 1024

    print(f"  → combined_all_scenarios_hd.mp4 ({size_mb:.1f} MB)")
    print(f"\n{'═' * 60}")
    print("  錄製完成！")
    print("═" * 60)
    for sc in SCENARIOS:
        r = best_results[sc["name"]]
        pct = r["steps"] / MAX_STEPS * 100
        jd = locked_joints_description(r["locked"])
        print(f"  {r['title']:25s}: {r['steps']:4d} 步 ({pct:.0f}%) [{jd or '無'}]")


if __name__ == "__main__":
    main()
