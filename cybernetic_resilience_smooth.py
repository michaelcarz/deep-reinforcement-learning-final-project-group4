import gymnasium as gym
import numpy as np
from stable_baselines3 import PPO
from stable_baselines3.common.vec_env import SubprocVecEnv, DummyVecEnv, VecNormalize
from stable_baselines3.common.callbacks import BaseCallback, CheckpointCallback, CallbackList
from stable_baselines3.common.monitor import Monitor
from typing import Callable
import os

def linear_schedule(initial_value: float, final_value: float) -> Callable[[float], float]:
    """ Linear learning rate schedule. """
    def func(progress_remaining: float) -> float:
        return progress_remaining * (initial_value - final_value) + final_value
    return func

class FaultTolerantHumanoidWrapperOptimized(gym.Wrapper):
    """
    Optimized Custom Gym Wrapper for Humanoid-v4 to simulate hardware failures (joint locking)
    AND incorporate advanced biomechanical constraints for smooth walking (Action Smoothing, Torso Stabilization).
    """
    def __init__(self, env, enable_smoothing=True, enable_torso_stabilization=True, use_ema_filter=False):
        super().__init__(env)
        self.stage = 1
        
        # Smoothness & stabilization toggles
        self.enable_smoothing = enable_smoothing
        self.enable_torso_stabilization = enable_torso_stabilization
        self.use_ema_filter = use_ema_filter
        
        # Humanoid-v4 has a 17-dimensional continuous action space
        self.action_dim = self.env.action_space.shape[0]
        
        # 1. Hardware Health Vector (State Awareness)
        self.health_vector = np.ones(self.action_dim, dtype=np.float32)
        
        # Observation: 376 (base) + 17 (health) + 17 (prev_action) = 410-dimensional observation
        obs_space = self.env.observation_space
        low = np.concatenate([obs_space.low,
                               np.zeros(self.action_dim, dtype=np.float32),
                               -np.ones(self.action_dim, dtype=np.float32)]).astype(np.float32)
        high = np.concatenate([obs_space.high,
                                np.ones(self.action_dim, dtype=np.float32),
                                np.ones(self.action_dim, dtype=np.float32)]).astype(np.float32)
        self.observation_space = gym.spaces.Box(low=low, high=high, dtype=np.float32)
        
        # Leg joints (hips and knees) for simulated failures
        self.leg_joint_indices = [3, 4, 5, 6, 7, 8, 9, 10]
        
        self.step_count = 0
        self.shock_step = -1
        self.locked_joints_this_episode = []
        
        # Store original physics parameters for Domain Randomization
        self.original_mass = self.env.unwrapped.model.body_mass.copy()
        self.original_friction = self.env.unwrapped.model.geom_friction.copy()
        
        self.global_env_steps = 0
        self.prev_action = np.zeros(self.action_dim, dtype=np.float32)
        self.prev_executed_action = np.zeros(self.action_dim, dtype=np.float32)

    def set_global_steps(self, global_steps: int):
        self.global_env_steps = global_steps

    def set_curriculum_stage(self, stage: int):
        self.stage = stage

    def _get_obs(self, obs):
        return np.concatenate([obs, self.health_vector, self.prev_action])

    def reset(self, **kwargs):
        obs, info = self.env.reset(**kwargs)
        self.step_count = 0
        self.health_vector = np.ones(self.action_dim, dtype=np.float32)
        self.locked_joints_this_episode = []
        
        # Domain Randomization (Sim-to-Real Robustness)
        mass_factor = np.random.uniform(0.95, 1.05, size=self.original_mass.shape)
        self.env.unwrapped.model.body_mass[:] = self.original_mass * mass_factor
        
        friction_factor = np.random.uniform(0.95, 1.05, size=self.original_friction.shape)
        self.env.unwrapped.model.geom_friction[:] = self.original_friction * friction_factor
        
        if self.stage == 1:
            pass
        elif self.stage == 2:
            num_locked = np.random.choice([1, 2])
            self.locked_joints_this_episode = np.random.choice(self.leg_joint_indices, num_locked, replace=False)
            self.shock_step = 0
        elif self.stage == 3:
            self.shock_step = np.random.randint(100, 501)
            num_locked = np.random.choice([1, 2])
            self.locked_joints_this_episode = np.random.choice(self.leg_joint_indices, num_locked, replace=False)
            
        self.prev_action = np.zeros(self.action_dim, dtype=np.float32)
        self.prev_executed_action = np.zeros(self.action_dim, dtype=np.float32)
        
        return self._get_obs(obs), info

    def step(self, action):
        self.step_count += 1
        
        # Progressive Hardware Degradation Logic
        if self.stage in [2, 3]:
            if self.step_count >= self.shock_step:
                steps_since_shock = self.step_count - self.shock_step
                if steps_since_shock < 50:
                    current_health = 0.5
                else:
                    current_health = 0.0
                for j in self.locked_joints_this_episode:
                    self.health_vector[j] = current_health
                
        # 1. Smooth the policy action using a low-pass Exponential Moving Average (EMA) filter
        if self.use_ema_filter:
            alpha = 0.3  # Weight of the new action. Lower values make transitions smoother.
            smooth_action = alpha * action + (1 - alpha) * self.prev_executed_action
        else:
            smooth_action = action
            
        # 2. Action Masking Gate (Physical Restriction)
        masked_action = smooth_action * self.health_vector
        self.prev_executed_action = masked_action.copy()
        
        # Execute action in the environment
        obs, reward, terminated, truncated, info = self.env.step(masked_action)
        
        # Cap maximum forward reward to prioritize stability over sprinting
        forward_reward = info.get('reward_linvel', info.get('forward_reward', 0.0))
        speed_cap_reward = 1.875
        if forward_reward > speed_cap_reward:
            reward -= (forward_reward - speed_cap_reward)
        
        # 3. Reward Shaping: Smoothness Penalty (Action Rate Penalty)
        smoothing_penalty = 0.0
        if self.enable_smoothing:
            # Penalize the difference between consecutive actions to prevent joint jittering
            action_diff = action - self.prev_action
            smoothing_penalty = 0.05 * np.sum(np.square(action_diff))
            reward -= smoothing_penalty
            
        # 4. Reward Shaping: Torso Stabilization Penalty
        torso_penalty = 0.0
        if self.enable_torso_stabilization:
            # Get torso translational and rotational velocities from MuJoCo unwrapped data
            qvel = self.env.unwrapped.data.qvel
            torso_vel_y = qvel[1]      # Lateral (side-to-side shaking)
            torso_vel_z = qvel[2]      # Vertical (up-and-down oscillation)
            torso_rot_vel = qvel[3:6]  # Roll, Pitch, Yaw velocities
            
            # Penalize excessive shaking and rotation (keeps body upright and steady)
            torso_penalty = 0.5 * (torso_vel_y**2 + torso_vel_z**2) + 0.1 * np.sum(np.square(torso_rot_vel))
            reward -= torso_penalty
        
        # 5. Reward Shaping: Compensation Survival Bonus
        compensation_bonus = 0.0
        if any(h < 1.0 for h in self.health_vector) and not terminated:
            compensation_bonus = 3.0
            reward += compensation_bonus
            
        # Update metrics for TensorBoard logging
        info["smoothing_penalty"] = float(smoothing_penalty)
        info["torso_penalty"] = float(torso_penalty)
        info["compensation_bonus"] = float(compensation_bonus)
        info["penalty_weight"] = 1.0  # Indicates active optimized shaping
        
        # Update prev_action AFTER calculations
        self.prev_action = action.copy()
        
        return self._get_obs(obs), reward, terminated, truncated, info

class MetricsLoggerCallback(BaseCallback):
    """ logs custom biomechanical metrics to TensorBoard. """
    def __init__(self, verbose=0):
        super().__init__(verbose)
        
    def _on_step(self) -> bool:
        self.training_env.env_method("set_global_steps", self.num_timesteps)
        infos = self.locals.get("infos", [])
        if len(infos) > 0:
            torso_penalties = [info.get("torso_penalty", 0.0) for info in infos if "torso_penalty" in info]
            smoothing_penalties = [info.get("smoothing_penalty", 0.0) for info in infos if "smoothing_penalty" in info]
            compensation_bonuses = [info.get("compensation_bonus", 0.0) for info in infos if "compensation_bonus" in info]
            
            if torso_penalties:
                self.logger.record("custom_metrics/torso_penalty", np.mean(torso_penalties))
            if smoothing_penalties:
                self.logger.record("custom_metrics/smoothing_penalty", np.mean(smoothing_penalties))
            if compensation_bonuses:
                self.logger.record("custom_metrics/compensation_bonus", np.mean(compensation_bonuses))
        return True

class ResilienceCallback(BaseCallback):
    """ Handles Phase 2: Resilience Fine-Tuning Stages. """
    def __init__(self, total_timesteps, verbose=0):
        super().__init__(verbose)
        self.phase2_total_timesteps = total_timesteps
        self.current_stage = 2
        self.start_timesteps = 0
        
    def _on_training_start(self):
        self.start_timesteps = self.num_timesteps

    def _on_step(self) -> bool:
        current_phase_timesteps = self.num_timesteps - self.start_timesteps
        progress = current_phase_timesteps / self.phase2_total_timesteps
        new_stage = 2 if progress < 0.50 else 3
        if new_stage != self.current_stage:
            self.current_stage = new_stage
            if self.verbose > 0:
                print(f"--- Transitioning to Curriculum Stage {self.current_stage} ---")
            self.training_env.env_method("set_curriculum_stage", self.current_stage)
        return True

def make_env(rank, z_range=(0.5, 2.0)):
    def _init():
        env = gym.make("Humanoid-v4", healthy_z_range=z_range)
        env = FaultTolerantHumanoidWrapperOptimized(env)
        env = Monitor(env)
        return env
    return _init

def create_vec_env(num_envs, load_path=None, training=True, z_range=(0.5, 2.0)):
    unwrapped_vec_env = SubprocVecEnv([make_env(i, z_range=z_range) for i in range(num_envs)])
    if load_path is not None:
        vec_env = VecNormalize.load(load_path, unwrapped_vec_env)
        if not training:
            vec_env.training = False
            vec_env.norm_reward = False
    else:
        vec_env = VecNormalize(
            unwrapped_vec_env,
            norm_obs=True,
            norm_reward=True,
            clip_obs=10.,
            clip_reward=10.
        )
    return vec_env

if __name__ == "__main__":
    # MAC MPS GPU Specific configurations:
    # On Apple Silicon Macs, SubprocVecEnv works best with fewer environments (e.g. 2 or 4) to avoid CPU bottlenecks.
    # Total timesteps are scaled down since we are running locally, to let the user see progress quickly.
    TOTAL_TIMESTEPS_PHASE_1 = 5_000_000   # Scaled down for quick feedback
    TOTAL_TIMESTEPS_PHASE_2 = 10_000_000  # Fine-tuning under fault
    NUM_ENVS = 2                          # Balanced for Apple Silicon
    
    print("=== STARTING OPTIMIZED SMOOTH WALK TRAINING PIPELINE (MAC MPS GPU) ===")
    # Crucial: Load pretrained normalization statistics to align baseline observations!
    if os.path.exists("vec_normalize_phase1.pkl"):
        print("Loading pretrained normalization statistics to align baseline observations...")
        vec_env = create_vec_env(NUM_ENVS, load_path="vec_normalize_phase1.pkl", training=True, z_range=(1.0, 2.0))
    else:
        print("Starting with fresh normalization statistics...")
        vec_env = create_vec_env(NUM_ENVS, z_range=(1.0, 2.0))
    vec_env.env_method("set_curriculum_stage", 1)
    
    # PPO optimized for Apple Silicon MPS
    model = PPO(
        policy="MlpPolicy",
        env=vec_env,
        n_steps=512,
        batch_size=64,
        n_epochs=10,
        gae_lambda=0.95,
        gamma=0.99,
        learning_rate=linear_schedule(1e-4, 1e-5),
        ent_coef=0.0,
        clip_range=0.2,
        target_kl=0.05,
        use_sde=False,
        verbose=1,
        tensorboard_log=os.path.expanduser("~/tensorboard_logs/cybernetic_resilience_mac/"),
        policy_kwargs=dict(net_arch=[256, 256]),
        device="mps"  # CRITICAL: Use Apple Silicon Metal Performance Shaders GPU!
    )
    
    # Load base model weights if available
    base_model_path = "ppo_humanoid_healthy_baseline"
    if os.path.exists(base_model_path + ".zip"):
        print(f"Loading pretrained baseline weights from {base_model_path}...")
        model.set_parameters(PPO.load(base_model_path, device="mps").get_parameters())
        
    checkpoint_callback = CheckpointCallback(
        save_freq=125000,
        save_path="./models/checkpoints_mac/",
        name_prefix="ppo_humanoid_mac"
    )
    metrics_callback = MetricsLoggerCallback()
    
    print("Phase 1: Fine-tuning baseline on Mac GPU with smoothness constraints...")
    model.learn(
        total_timesteps=TOTAL_TIMESTEPS_PHASE_1,
        callback=CallbackList([checkpoint_callback, metrics_callback]),
        tb_log_name="PPO_MacPhase1_Healthy"
    )
    
    model.save("ppo_humanoid_healthy_mac_baseline")
    vec_env.save("vec_normalize_mac_phase1.pkl")
    vec_env.close()
    
    print("Phase 2: Fine-tuning under joint locking fault...")
    vec_env = create_vec_env(NUM_ENVS, load_path="vec_normalize_mac_phase1.pkl", training=True, z_range=(0.5, 2.0))
    
    custom_objects = {
        "learning_rate": linear_schedule(2.5e-5, 2.5e-6),
        "device": "mps"
    }
    model = PPO.load("ppo_humanoid_healthy_mac_baseline", env=vec_env, custom_objects=custom_objects)
    vec_env.env_method("set_curriculum_stage", 2)
    
    resilience_callback = ResilienceCallback(total_timesteps=TOTAL_TIMESTEPS_PHASE_2, verbose=1)
    callback_list = CallbackList([checkpoint_callback, resilience_callback, metrics_callback])
    
    model.learn(
        total_timesteps=TOTAL_TIMESTEPS_PHASE_2,
        callback=callback_list,
        tb_log_name="PPO_MacPhase2_Resilience",
        reset_num_timesteps=False
    )
    
    model.save("ppo_cybernetic_resilience_mac_final")
    vec_env.save("vec_normalize_mac_final.pkl")
    print("Optimized Walk Training Complete on Mac!")
    vec_env.close()
