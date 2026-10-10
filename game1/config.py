"""
config.py - 全局常量与可调参数
所有游戏数值集中在此，便于平衡与调试
"""

# ─── 日志 ──────────────────────────────────────────────────
# raylib 日志阈值。"warning"=过滤 INFO 噪声(VAO/Mesh/Texture/Shader 的上传卸载
# 行); "error"=仅保留错误; "none"=静默。默认 warning:保留真正的警告与错误。
LOG_LEVEL = "warning"

# ─── 窗口 ──────────────────────────────────────────────────
WINDOW_WIDTH = 1280
WINDOW_HEIGHT = 720
WINDOW_TITLE = "Seeded Tower Defense"
TARGET_FPS = 240

# ─── 世界 ──────────────────────────────────────────────────
WORLD_HEIGHT = 16      # 地形最大高度
BLOCK_SIZE = 1.0       # 每个方块的世界单位大小

# ─── 区块（无限地形） ────────────────────────────────────
CHUNK_SIZE = 16               # 区块边长（方块数）
RENDER_DISTANCE = 5           # 渲染距离（区块数）
FOG_NEAR = 50.0               # 雾起始距离（世界单位，≥相机距离42保证焦点清晰）
FOG_FAR = 110.0               # 雾完全遮挡距离（≈眼位到地块边缘，满雾压边）
FOG_DENSITY = 0.018           # 全局雾密度（备用）
MAX_LOADED_CHUNKS = 180       # LRU 上限（RENDER_DISTANCE=5 需要 ~80 个，留 2 倍余量）

# ─── 大本营 ──────────────────────────────────────────────────
BASE_HEALTH = 100             # 大本营最大生命值
BASE_RADIUS = 3.0             # 大本营半径
ENEMY_BASE_DAMAGE = 5         # 敌人到达大本营造成的伤害
ENEMY_SPAWN_DISTANCE_MIN = 28  # 刷怪点最小距离
ENEMY_SPAWN_DISTANCE_MAX = 42  # 刷怪点最大距离

# ─── 种子 ──────────────────────────────────────────────────
DEFAULT_SEED = 42      # 默认种子

# ─── 相机 ──────────────────────────────────────────────────
CAMERA_DISTANCE_MIN = 10.0
CAMERA_DISTANCE_MAX = 130.0
CAMERA_ALTITUDE_MIN = 0.1     # 接近水平
CAMERA_ALTITUDE_MAX = 1.4     # 接近正上方 (pi/2 ≈ 1.5708)
CAMERA_MOVE_SPEED = 15.0
CAMERA_ROT_SPEED = 2.5
CAMERA_ZOOM_SPEED = 0.002
CAMERA_2D_ZOOM_MIN = 0.3
CAMERA_2D_ZOOM_MAX = 3.0

# ─── 实体 ──────────────────────────────────────────────────
TOWER_DAMAGE_MULT = 1.5      # 每级强化伤害倍率增量（1.0=不生效，升级形同虚设）
TOWER_RANGE_MULT = 1.1       # 每级强化射程倍率增量（1.0=不生效）
TOWER_COOLDOWN_MULT = 0.9    # 每级强化冷却倍率（越小越快）
MAX_TOWER_LEVEL = 3
UPGRADE_COST_MULT = 1.5      # 升级费用每级倍率

# 敌人波次增长
ENEMY_HEALTH_GROWTH = 1.08   # 每波健康增长（指数倍率，见 game.py:159）
ENEMY_SPEED_GROWTH = 0.0     # 每波速度增长；1.03 会导致波2 即 2 倍速，直接毁掉难度曲线

# 初始金钱与生命
STARTING_MONEY = 200
STARTING_LIVES = 20

# ─── 波次与经济 ────────────────────────────────────────────
WAVE_BASE_COUNT = 5          # 基础敌人数量
WAVE_COUNT_PER_WAVE = 2      # 每波增加数
WAVE_SPAWN_INTERVAL_BASE = 0.8
WAVE_SPAWN_INTERVAL_MIN = 0.2
WAVE_SPAWN_INTERVAL_DECAY = 0.05
ENEMY_REWARD_GROWTH = 0.1    # 奖励 = base*(1+wave*此值)
WAVE_BONUS_BASE = 50
WAVE_BONUS_PER_WAVE = 10
SCORE_PER_WAVE = 100

# ─── 物理 ──────────────────────────────────────────────────
PROJECTILE_SPEED = 15.0
PROJECTILE_LIFETIME = 3.0
PROJECTILE_DRAG = 0.96       # 空气阻力系数（越小减速越快）
PROJECTILE_GRAVITY = -9.8    # 重力加速度（Y轴，负值向下）
PROJECTILE_UPWARD_BIAS = 3.0 # 发射时额外向上补偿(抵消重力)
PROJECTILE_HIT_RADIUS = 0.9  # 抛射体命中半径

# ─── 天气 ──────────────────────────────────────────────────
# 现实递减率: 6.5°C/km (0.0065°C/m)
# 游戏缩放: 1方块 ≈ 154米 → 1.0°C/方块（约154倍缩放，确保地形温度差异显著）
# 这使山峰可达 0°C 以下，产生苔原/雪原生物群系
TEMPERATURE_LAPSE_RATE = 1.0   # °C / 方块高度
RAIN_PARTICLE_COUNT = 100
SNOW_PARTICLE_COUNT = 60

# ─── 渲染 ──────────────────────────────────────────────────
TEXTURE_SIZE = 64            # 动态纹理分辨率
ATLAS_SIZE = 4               # 纹理图集尺寸 (ATLAS_SIZE x ATLAS_SIZE 方块)

# ─── 塔类型 ──────────────────────────────────────────────────
# 塔定义在 entities.py 中，此处仅放颜色
TOWER_COLORS = {
    "stone_thrower":  (150, 150, 150),
    "water_cannon":   (70, 130, 220),
    "sand_trap":      (210, 180, 100),
    "metal_tower":    (180, 180, 190),
}

# ─── 敌人类型 ──────────────────────────────────────────────────
ENEMY_COLORS = {
    "regular": (180, 60, 60),
    "fast":    (220, 150, 40),
    "tank":    (100, 80, 160),
}

# ─── 科技树 ─────────────────────────────────────────────────────
# 每项科技: cost=研究费用, time=研究耗时(秒), requires=前置科技,
# effect=效果键(见 game.py:TECH_EFFECTS)
# 前置链: handcraft → stone_mining → {biomass_energy, metallurgy}
#                                   biomass_energy → water_mechanics
TECH_TREE = {
    "handcraft": {
        "cost": 100, "time": 15.0, "requires": (),
        "effect": "tower_damage",
    },
    "stone_mining": {
        "cost": 150, "time": 20.0, "requires": ("handcraft",),
        "effect": "build_cost",
    },
    "biomass_energy": {
        "cost": 200, "time": 25.0, "requires": ("stone_mining",),
        "effect": None,
    },
    "water_mechanics": {
        "cost": 250, "time": 30.0, "requires": ("biomass_energy",),
        "effect": None,
    },
    "metallurgy": {
        "cost": 300, "time": 35.0, "requires": ("stone_mining",),
        "effect": None,
    },
}

# 科技数值效果
TECH_EFFECTS = {
    "tower_damage": 1.10,   # 塔伤害倍率
    "build_cost": 0.85,     # 建造费用倍率
}
