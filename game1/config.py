"""
config.py - 全局常量与可调参数

数值的“唯一事实来源”现在是 data/*.json（由 gamedata.py 在启动时读取）：
  - 科技树/科技效果   → data/tech.json
  - 配方/工具/武器/材料 → data/recipes.json
  - 敌人类型/掉落      → data/enemies.json
  - 刷怪点种类/侦察兵  → data/spawners.json
  - 波次构成          → data/waves.json
  - 塔属性           → data/towers.json

本文件只保留纯工程/渲染常量（窗口、相机、物理、存档版本等）与无法归入
上述六类的少量数值（初始金钱、升级倍率等）。
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
MAX_LOADED_CHUNKS = 240       # LRU 上限（RENDER_DISTANCE=5 需要 ~80 个，留约 3 倍余量供按需重载）

# ─── 大本营 ──────────────────────────────────────────────────
BASE_HEALTH = 100             # 大本营最大生命值
BASE_RADIUS = 3.0             # 大本营半径
# 敌人到达大本营的伤害见 data/enemies.json -> global.base_damage

# ─── 种子 ──────────────────────────────────────────────────
DEFAULT_SEED = 42      # 默认种子

# ─── 存档 ──────────────────────────────────────────────────
# 存档文件格式版本。反序列化按此做迁移；未知字段一律 .get() 兜底。
# v1→v2: 新增 per-spawner 运行时状态 + 侦察怪；旧全局波次映射到最近激活点。
# v2→v3: 粗格编码 spawner id + enemy_paths dict by spawner_id。
# v3→v4: 科技树 v2 数据（263 成果 + 770 突破点，5 时代），旧 12 科技 ID 别名映射。
SAVE_VERSION = 4

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

# ─── 实体（升级曲线等通用参数） ─────────────────────────────
# 塔的基础属性见 data/towers.json；敌人基础属性见 data/enemies.json
TOWER_DAMAGE_MULT = 1.5      # 每级强化伤害倍率增量（1.0=不生效，升级形同虚设）
TOWER_RANGE_MULT = 1.1       # 每级强化射程倍率增量（1.0=不生效）
TOWER_COOLDOWN_MULT = 0.9    # 每级强化冷却倍率（越小越快）
MAX_TOWER_LEVEL = 3
UPGRADE_COST_MULT = 1.5      # 升级费用每级倍率

# 初始金钱与生命
STARTING_MONEY = 200
STARTING_LIVES = 20

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