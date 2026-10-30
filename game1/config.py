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
FPS_UPDATE_INTERVAL = 0.25    # FPS 统计/刷新窗口（秒）

# ─── 世界 ──────────────────────────────────────────────────
WORLD_HEIGHT = 2048    # 地形最大高度（方块数）
BLOCK_SIZE = 1.0       # 每个方块的世界单位大小

# ─── 区块（无限地形） ────────────────────────────────────
CHUNK_SIZE = 16               # 区块边长（方块数）
RENDER_DISTANCE = 5           # 渲染距离（区块数，2D 模式用）
# 3D 雾：以默认相机距离为基准（距离42下雾=50/110，保持原观感）；
# 拉远时雾远近随相机距离等比放大，但以 CAMERA_VIEW_DISTANCE_MAX 为上限。
# CAMERA_VIEW_DISTANCE_MAX 同时封顶 3D 地形加载/剔除半径，避免拉远到 2000+ 时
# 一次性生成上万区块网格卡死帧率（超界被满雾盖住，无露边）。
FOG_NEAR = 50.0               # 基准雾起始距离（世界单位）
FOG_FAR = 110.0               # 基准雾完全遮挡距离（世界单位）
CAMERA_DISTANCE_DEFAULT = 42.0  # 相机初始/参考距离，雾缩放基准
CAMERA_VIEW_DISTANCE_MAX = 450.0  # 3D 地形加载/雾远上限（世界单位，防卡顿）
FOG_DENSITY = 0.018           # 全局雾密度（备用）
MAX_LOADED_CHUNKS = 1440      # 3D 视锥加载需要覆盖相机到目标之间的区块
MAX_CHUNK_MESHES = 1024
MAX_PROP_MESHES = 1024
CACHE_VISIBLE_FACTOR = 3      # 缓存至少覆盖当前可见区旋转一周的工作集

# 3D 区块流式加载：每帧用于生成/建网格的时间预算（毫秒）。
# 缺口按距离由近到远逐帧补齐，避免一次卡死数百毫秒；首帧仍一次性建完。
CHUNK_BUILD_BUDGET_MS = 4.0

# ─── 大本营 ──────────────────────────────────────────────────
BASE_HEALTH = 100             # 大本营最大生命值
BASE_RADIUS = 3.0             # 大本营半径
# 敌人到达大本营的伤害见 data/enemies.json -> global.base_damage

# ─── 种子 ──────────────────────────────────────────────────
DEFAULT_SEED = 42      # 默认种子

# ─── 噪声（多级 FBM，每级参数自由定制） ──────────────────────
# 每级 = (频率倍率, 振幅权重, 种子偏移)，级数任意：
#   0 级 → 常数 0；1 级 → 等价单级值噪声；N 级 → N 级加权叠加。
# 频率倍率作用在"调用点传入的坐标"上：
#   height / moisture 调用点传原始世界坐标 → 此处频率即有效绝对频率（每世界格）；
#   ore 调用点传切平面坐标 (x*0.5, z*0.5 + y*0.3)、
#   path 传 (cx*0.06, cz*0.06 + path_index*10) → 频率 1.0 表示沿用该坐标本身。
# 种子偏移加在 base_seed 之上；振幅权重之和为 0 时该噪声退化为常数 0。
# 改动此表会改变同种子下的世界生成结果（预期行为）；存档不存地形，无需迁移。
#
# height 额外三级管线（chunk.natural_height* 按序执行，均可独立关闭）：
#   ① warp   域扭曲：两组噪声把采样坐标推离原位，让大陆边界/山脉走向
#            呈流动状。strength=0 或无此键 → 精确跳过（逐位还原旧公式）。
#   ② param  参数噪声：低频场 P ∈ [0,1) 逐点调制高度 levels 中
#            mod_levels 指定层级的振幅/频率——
#              amp_scale  = 1 + amp_depth  * (2P-1)
#              freq_scale = 1 + freq_depth * (2P-1)
#            P 低的区域细节振幅被压低 → 地形平缓；两个 depth 均为 0
#            或无此键 → 精确退化为普通 FBM。
#   ③ ridged 山脊变换：r = (1 - |2n-1|)^sharpness，与原高度 n 按下式混合——
#              P'     = clamp((P - param_lo)/(param_hi - param_lo), 0, 1)
#              blend  = strength * P'^param_pow * M^mask_pow
#              n      = n*(1-blend) + r*blend
#            其中 M 是独立噪声场（mask_levels）。P（平缓度）与 M（山脊
#            在哪）联合控制：P ≤ param_lo 处 blend 精确为 0，地形不会锐利。
#   warp/param/ridged 的种子偏移与 levels 内偏移(0/1000/2000)、
#   moisture(9999)、ore(777) 均不重叠。
NOISE_CFG = {
    # 高度：3 级 —— 大陆尺度起伏(0.00005) + 区域起伏(0.005) + 细节(0.05)
    "height": {
        "levels": [(0.00005, 0.5, 0), (0.005, 0.1, 1000), (0.05, 0.002, 2000)],
        # ① 域扭曲：最大位移 ≈ strength（世界格）。频率取大陆~区域之间，
        #    让扭曲成"大片流动"而非逐格抖动。
        "warp": {
            "strength": 2000.0,
            "seed_off_x": 31000,
            "seed_off_z": 32000,
            "levels": [(0.0002, 1.0, 0), (0.001, 0.5, 1000)],
        },
        # ② 参数噪声：低频平缓度场 P。mod_levels=(1,2) 调制区域级+细节级
        #    （大陆级 0 保持稳定，否则整片大陆基准会抖动）。
        #    P=0 处细节振幅 ×0.1（很平），P=1 处 ×1.9（很碎）；频率 ±30%。
        "param": {
            "seed_off": 33000,
            "levels": [(0.00008, 1.0, 0), (0.0004, 0.6, 1000)],
            "mod_levels": (1, 2),
            "amp_depth": 0.9,
            "freq_depth": 0.3,
        },
        # ③ 山脊变换：r = (1 - |2n-1|)^sharpness，与原高度 n 按下式混合——
        #              P' = clamp((P - param_lo) / (param_hi - param_lo), 0, 1)
        #              blend   = strength * P'^param_pow * M^mask_pow
        #              n       = n*(1-blend) + r*blend
        #            其中 M 是独立噪声场（mask_levels）。P' 重映射把
        #            "平缓的地方不会锐利"变成硬保证：P ≤ param_lo 处
        #            blend 精确为 0（山脊完全关闭）；P ≥ param_hi 处由 M
        #            决定山脊实际长在哪。strength=0 或无此键 → 精确跳过。
        #            strength 建议 ≤ 1。
        "ridged": {
            "mask_seed_off": 34000,
            "mask_levels": [(0.00015, 1.0, 0)],
            "sharpness": 2.2,
            "strength": 0.65,
            "param_lo": 0.15,
            "param_hi": 0.55,
            "param_pow": 1.2,
            "mask_pow": 1.0,
        },
    },
    # 湿度：3 级，等价原 fbm_2d(octaves=3, persistence=0.4, base=0.04)
    "moisture": {
        "base_seed": 9999,
        # 第三级振幅写成 0.4 * 0.4 而非 0.16：浮点乘积累积值为
        # 0.16000000000000003，字面量 0.16 与之相差约 3e-17。
        "levels": [(0.04, 1.0, 0), (0.08, 0.4, 1000), (0.16, 0.4 * 0.4, 2000)],
    },
    # 矿脉：单级（坐标切平面在 chunk._generate）
    "ore": {
        "base_seed": 777,
        "levels": [(1.0, 1.0, 0)],
    },
    # 路径偏移：单级（种子由调用点传入）
    "path": {
        "levels": [(1.0, 1.0, 0)],
    },
}

# ─── 存档 ──────────────────────────────────────────────────
# 存档文件格式版本。反序列化按此做迁移；未知字段一律 .get() 兜底。
# v1→v2: 新增 per-spawner 运行时状态 + 侦察怪；旧全局波次映射到最近激活点。
# v2→v3: 粗格编码 spawner id + enemy_paths dict by spawner_id。
# v3→v4: 科技树 v2 数据（263 成果 + 770 突破点，5 时代），旧 12 科技 ID 别名映射。
# v4→v5: 寻路系统——地形编辑覆盖层/变体路径/敌人攻击与挖掘状态/塔体 HP。
SAVE_VERSION = 5

# ─── 相机 ──────────────────────────────────────────────────
CAMERA_DISTANCE_MIN = 5.0     # 拉近下限：贴近地面/海面
CAMERA_DISTANCE_MAX = 2100.0  # 拉远上限：比 WORLD_HEIGHT=2048 略多，能框住最高地形
CAMERA_ALTITUDE_MIN = -0.8    # 负值：相机可低于目标、斜向上看（≈ -46°）
CAMERA_ALTITUDE_MAX = 1.55    # 接近正上方 (pi/2 ≈ 1.5708)
CAMERA_TERRAIN_CLEARANCE = 0.2
CAMERA_MOVE_SPEED = 15.0
CAMERA_ROT_SPEED = 2.5
CAMERA_ZOOM_SPEED = 0.002   # 3D 滚轮指数缩放系数：每格 ×exp(0.002*100)=×1.221
CAMERA_SPAWN_RAISE_MAX = 512.0   # 开局眼位相对基地地表的抬升上限（格），防御极端地形
                                 # （500 种子实测最大需 ~343，此处留余量）
CAMERA_SPAWN_KEEP = 1.0          # 眼位偏离开局位置 < 此值（格）时保持开局下限全额
CAMERA_SPAWN_RELEASE = 8.0       # 偏离至 ≥ 此值（格）时开局下限完全淡出（线性）
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

# ─── 玩家地形编辑（挖/放方块） ─────────────────────────────
PLAYER_DIG_COOLDOWN = 0.35   # 玩家连挖间隔（秒/格）
MAX_BLOCK_STACK = 6          # 放置方块的高度上限（该列自然地表 + N 格）

# ─── 物理 ──────────────────────────────────────────────────
PROJECTILE_SPEED = 15.0
PROJECTILE_LIFETIME = 3.0
PROJECTILE_DRAG = 0.96       # 空气阻力系数（越小减速越快）
PROJECTILE_GRAVITY = -9.8    # 重力加速度（Y轴，负值向下）
PROJECTILE_UPWARD_BIAS = 3.0 # 发射时额外向上补偿(抵消重力)
PROJECTILE_HIT_RADIUS = 0.9  # 抛射体命中半径
PROJECTILE_COLLISION_STEP = 0.25   # 抛射体飞行段碰撞采样步长（世界单位）；
                                   # 从上一帧位置到新位置按此步长线性插值采样
                                   # is_solid 查询，命中即停止，避免丢帧时一步跨过
                                   # 薄墙导致的"穿进又传出"。典型帧 2 次采样，
                                   # 丢帧峰值 ≤8 次，get_block_safe 为 dict 查表。

# 塔射击的高度差加成（高打低优势）：
# drop = 塔.y − 敌人.y，> 0 表示塔比敌人高（高地打低地）。
# 平地（drop = 0）与上坡（drop ≤ 0）行为完全不变。
TOWER_ELEV_MAX_DROP = 12.0        # 计入加成的高度差上限（格），封顶防山地失衡
TOWER_ELEV_RANGE_PER_BLOCK = 0.25  # 每下落 1 格，有效射程加成（格，线性累加）；
                                   # 12 格封顶 = +3.0 格射程
TOWER_ELEV_DMG_PER_BLOCK = 0.04   # 每下落 1 格，威力加成（乘性，1 + drop×此值）；
                                   # 12 格封顶 = +48% 伤害
                                   # 任一键设为 0 即关闭对应加成。

# ─── 寻路（工程常量；类型偏好数值在 data/enemies.json） ──────
PATH_ASTAR_NODE_BUDGET = 24000   # 单次 A* 展开节点上限（超限失败→放宽重试/兜底）
PATH_CORRIDOR_MAX_HALF = 48      # 搜索走廊最大半宽（格，限制离直线的绕行范围）
PATH_FALL_LIMIT = 4              # 下行高差硬上限（格，超过必须绕行——"高差大必绕行"）
PATH_CLIMB_SOFT_COST = 0.30      # 每格爬升的额外时间代价（秒，×terrain_aware）
PATH_TOWER_CELL_COST_MIN = 1.5   # 塔格代价下限（秒；实际≈拆塔时间×(1-tower_hostility)）
PATH_WALL_CELL_COST = 1.0e9      # 防御塔(城墙)格代价：近乎不可通行，连续成墙即无法穿过
                                 # （只有完全无路可绕/预算耗尽时才会强攻拆墙）
PATH_CROWD_CELL_COST = 0.35      # 每个占用者的额外边代价（秒，×crowd_avoidance）
PATH_VARIANT_COUNT = 3           # 每刷怪点×类型缓存的路径变体数
PATH_VARIANT_DIVERSION = 1.2     # 变体间沿已有变体路径格的附加代价（秒）
PATH_SMOOTH_CORNER_ANGLE = 0.44  # 弦切圆角的转角阈值（弧度，≈25°）
PATH_SMOOTH_CORNER_CUT = 0.35    # 弦切比例（0-0.5，越大越圆）
PATH_MIN_POINT_DIST = 0.35       # 平滑后路径点最小间距（世界单位）
PATH_REPATH_CHECK_INTERVAL = 2.0 # 敌人检查 world_version 是否过期的周期（秒）
PATH_REPATH_BUDGET_PER_FRAME = 1 # 每帧重算 A* 的次数上限（摊帧防卡顿）
PATH_SIEGE_DEFAULT_RANGE = 1.5   # 攻击塔近战判定距离（格）
ENEMY_COLLIDE_RADIUS = 0.35      # 敌人间分离半径（世界单位）
ENEMY_COLLIDE_STRENGTH = 6.0     # 分离推挤强度（重叠深度的倍率/秒）

# ─── 天气 ──────────────────────────────────────────────────
# 递减率随 WORLD_HEIGHT 反向缩放：高度 16→2048（×128）取 1/128，
# 使"底部→顶部"的温度跨度与旧地形一致（基准温度 5~25°C 覆盖全部群系）。
TEMPERATURE_LAPSE_RATE = 0.0078125   # °C / 方块高度（= 1/128）
RAIN_PARTICLE_COUNT = 100
SNOW_PARTICLE_COUNT = 60

# ─── 生物群系阈值 ───────────────────────────────────────────
# 海拔类阈值写成 WORLD_HEIGHT 的比例，随高度自适应。
# 比例取自旧地形（高度 16）的绝对值：12/16=0.75、10/16=0.625，
# 语义为"世界最高 25% / 37.5% 才算高山、雪盖"。
MOUNTAIN_ALT_RATIO = 0.75     # altitude > 0.75 * WORLD_HEIGHT → mountain
SNOW_SURFACE_RATIO = 0.625    # surface_y > 0.625 * WORLD_HEIGHT 且 temp < 2°C → 雪盖

# ─── 渲染 ──────────────────────────────────────────────────
TEXTURE_SIZE = 64            # 动态纹理分辨率
ATLAS_SIZE = 4               # 纹理图集尺寸 (ATLAS_SIZE x ATLAS_SIZE 方块)