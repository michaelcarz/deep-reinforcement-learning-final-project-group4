# 🦿 Bipedal Robot Resilience — 雙足機器人容錯控制

> 一隻雙足機器人，在走路走到一半時突然壞了一條腿，它沒有被重新編程，卻自己學會了跛著走下去。

使用 PPO 深度強化學習，讓 MuJoCo Humanoid-v4 雙足機器人在關節突發鎖死後，自主湧現出代償性的跛行步態。

## 🎬 影片展示

| 場景 | 說明 | 結果 |
|------|------|------|
| 正常行走 | 所有關節健康，穩定直立步行 | 1000 步 ✅ |
| 鎖住左髖後行走 | 起步時左髖關節已鎖死 | 1000 步 ✅ |
| 走路途中突然鎖住左髖 | 行走中左髖突然壞掉，自主重新平衡 | 1000 步 ✅ |

影片位於 `videos_hd/` 目錄。

## 🔧 技術架構

- **物理引擎**: MuJoCo 3.x + Gymnasium 1.0.0 (Humanoid-v4)
- **RL 演算法**: PPO (Stable-Baselines3 2.4.1)
- **神經網路**: PyTorch, MLP [256, 256]
- **觀測空間**: 376 維 → 410 維（+健康向量 17 維 + 上一步動作 17 維）

### 核心設計

1. **Health Vector** — 17 維向量，告訴機器人每個關節的健康狀態
2. **Action Masking Gate** — 壞掉的關節在物理層面歸零控制信號
3. **漸進式退化** — 關節先半損（50步），再完全鎖死
4. **兩階段課程學習** — Phase 1 學走路（z≥1.0m）→ Phase 2 學帶傷走路（z≥0.5m）

## 📊 評估結果

| 場景 | V1 (CUDA) | V2 (MPS 平滑版) |
|------|:---------:|:---------------:|
| 正常行走 | 980.6 / 1000 | 854.8 / 1000 |
| 靜態鎖死 | 380.9 / 1000 (38.8%) | 257.8 / 1000 (30.2%) |
| 動態衝擊 | 664.2 / 1000 (67.7%) | 567.9 / 1000 (66.4%) |
| **綜合韌性** | **53.3%** | **48.3%** |

## 📁 檔案說明

| 檔案 | 說明 |
|------|------|
| `cybernetic_resilience_smooth.py` | 核心訓練腳本（環境封裝 + PPO 訓練流程） |
| `record_best_hd.py` | HD 720p 影片錄製腳本 |
| `final_report.md` | 完整技術報告 |
| `training_dialogue_and_commands_log.md` | 16 次實驗的完整訓練歷程記錄 |
| `ppo_humanoid_healthy_mac_baseline.zip` | Phase 1 健康步態模型權重 |
| `ppo_cybernetic_resilience_mac_polished.zip` | Phase 2 最終容錯模型（打磨版） |
| `vec_normalize_mac_*.pkl` | 觀測正規化統計 |

## 🚀 快速開始

```bash
# 建立虛擬環境
python3 -m venv .venv
source .venv/bin/activate

# 安裝依賴
pip install gymnasium[mujoco] stable-baselines3 torch numpy imageio imageio-ffmpeg Pillow

# 錄製影片
python record_best_hd.py
```

## 📝 訓練硬體

| 平台 | GPU | 訓練步數 | 時間 |
|------|-----|---------|------|
| Windows (V1) | RTX 4070 Ti (CUDA) | 100M 步 | ~27.5 小時 |
| macOS (V2) | Apple Silicon (MPS) | 16M 步 | ~6.2 小時 |
