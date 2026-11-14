"""
pathfinding.py - 敌人寻路系统

表面网格 A*（8 邻接）+ 类型化代价画像 + 约束感知平滑 + 变体管理。

设计要点：
  - 代价单位 = 秒（走过这条边预计花费的时间），使"绕远 vs 拆塔 vs 挖方"
    天然可比：A* 最小化总时间。
  - 边代价 = 距离/移动速度倍率 + 爬升软代价 + 挖方时间 + 拆塔时间×(1-意愿)
    + 拥挤惩罚 + 变体分流惩罚。
  - 硬约束：上行超过爬坡上限且不可挖 → 禁行；海洋禁行；下行超过类型
    fall_limit → 禁行。这保证"高差足够大一定绕行"（下行硬禁与运行时
    _sync_y 重力下落 + fall_limit 跨格硬限对齐，BUG-102）。
  - 搜索限制在刷怪点→大本营直线的走廊内（半宽由类型绕远容忍推导）。
  - 平滑 = string-pulling（LOS 不跨越禁行边/塔/挖方格）+ 转角弦切圆角。
  - Pathfinder 按 (刷怪点×类型) 缓存多条变体；world_version 变化后惰性重算，
    每帧预算限流，避免卡帧。
"""

import heapq
import math
import random

import numpy as np

import config
import gamedata
from chunk import natural_height_vec, base_center_height
from noise import fbm_levels_2d_vec
from world import PathData

_SQRT2 = math.sqrt(2.0)

# ─── 走廊网格（走廊预计算，与敌人类型无关，可跨类型共享） ──────────


class CorridorGrid:
    """刷怪点→大本营走廊的 numpy 预计算。

    持有走廊范围内每格的地表高度 / 可通行 / 群系码 / 表面方块码，
    A* 循环只做数组查表；biome/block 码表供 CostProfile 构建速度数组。
    """

    __slots__ = ("x0", "z0", "w", "h", "heights", "land", "biome_codes",
                 "biome_names", "blk_codes", "blk_names", "half_width",
                 "max_height", "corridor", "no_dig_mask", "dig_floor")

    def __init__(self, universe, sx, sz, bx, bz, half_width):
        self.half_width = half_width
        L = math.hypot(bx - sx, bz - sz)
        if L < 1e-6:
            ax0 = az0 = bx0 = bz0 = 0.0
        else:
            ux = (bx - sx) / L
            uz = (bz - sz) / L
        pad = half_width + 2
        self.x0 = int(math.floor(min(sx, bx) - pad))
        self.z0 = int(math.floor(min(sz, bz) - pad))
        x1 = int(math.ceil(max(sx, bx) + pad))
        z1 = int(math.ceil(max(sz, bz) + pad))
        self.w = x1 - self.x0 + 1
        self.h = z1 - self.z0 + 1

        xs = np.arange(self.x0, x1 + 1, dtype=np.float64)
        zs = np.arange(self.z0, z1 + 1, dtype=np.float64)
        X, Z = np.meshgrid(xs, zs, indexing="ij")

        # 走廊成员掩码：到起点→终点线段的垂直距离 ≤ half_width
        if L < 1e-6:
            corridor = np.ones(X.shape, dtype=bool)
        else:
            vx = bx - sx
            vz = bz - sz
            L2 = vx * vx + vz * vz
            t = ((X - sx) * vx + (Z - sz) * vz) / L2
            t = np.clip(t, 0.0, 1.0)
            px = sx + vx * t
            pz = sz + vz * t
            corridor = (X - px) ** 2 + (Z - pz) ** 2 <= half_width ** 2
        self.corridor = corridor

        # 高度（含大本营平整）——与 _get_height_with_base_override 一致
        hn = natural_height_vec(X, Z, universe.seed)
        dist = np.hypot(X - universe.base_x, Z - universe.base_z)
        in_base = dist < config.BASE_RADIUS + 3
        if in_base.any():
            hn = np.where(in_base, base_center_height(
                universe.base_x, universe.base_z, universe.seed), hn)
        heights = np.floor(hn * universe.height).astype(np.int32)
        # 叠加玩家/敌人地形编辑覆盖层：挖低/垫高后的列顶是运行时权威高度，
        # A* 的爬坡/坠落/挖方决策与敌人逐帧 surface_height_at 保持一致。
        if universe.surface_override:
            for (x, z), y in universe.surface_override.items():
                i = self.idx(x, z)
                if i >= 0:
                    heights.flat[i] = y
        self.heights = heights
        self.max_height = universe.height
        self.land = hn >= 0.005
        # 大本营 5×5 禁挖区（与 world.dig_forbidden_at 同框）：A* 不规划
        # "挖上去进大本营"的边——那种挖掘会被守卫拒绝，敌人只能从地表
        # 接近。不 slam 这条边的话，深隧道会一路挖到基地边上再靠攀爬
        # 兜底钻出地基（"地面完好从地下钻出来"）。掩码数组供 A* 热循环
        # O(1) 查表。
        self.no_dig_mask = np.zeros(hn.shape, dtype=bool)
        for dx in range(-2, 3):
            for dz in range(-2, 3):
                i = self.idx(universe.base_x + dx, universe.base_z + dz)
                if i >= 0:
                    self.no_dig_mask.flat[i] = True

        # 大本营平台挖掘守卫（D2）：平台圆盘内每列的"开挖台阶高度下限"。
        # 与 world.Universe.enemy_dig_floor 同源（同一半径、同一基准面），
        # A* 热循环直接查表判定"这次上行挖掘会不会把敌人锁死在平台边缘"。
        # -1 = 该列无守卫。开挖后台阶高度 = 当前格高度+1（见 _update_dig 的
        # mark_passage(target, base+1) 语义），低于下限即禁行。
        plat = universe.base_platform_height()
        dig_floor = np.full(hn.shape, -1, dtype=np.int64)
        guard = (np.hypot(X - universe.base_x, Z - universe.base_z)
                 < config.BASE_DIG_GUARD_RADIUS)
        if guard.any():
            dig_floor[guard] = plat - config.BASE_DIG_GUARD_FLOOR
        self.dig_floor = dig_floor

        # 湿度（含大本营填平）
        m_cfg = config.NOISE_CFG["moisture"]
        moist = fbm_levels_2d_vec(X, Z, universe.seed + m_cfg.get("base_seed", 0),
                                  m_cfg["levels"])
        if in_base.any():
            m_base = fbm_levels_2d_vec(
                np.array([float(universe.base_x)]),
                np.array([float(universe.base_z)]),
                universe.seed + m_cfg.get("base_seed", 0), m_cfg["levels"])[0]
            moist = np.where(in_base, m_base, moist)

        altitude = hn * universe.height
        temp = universe.base_temperature - altitude * config.TEMPERATURE_LAPSE_RATE

        # 群系码（覆盖顺序与 chunk._generate 一致：后写覆盖先写）
        biome_names = ["plains"]
        codes = np.zeros(hn.shape, dtype=np.uint8)

        def _assign(mask, name):
            nonlocal codes
            if not mask.any():
                return
            if name in biome_names:
                idx = biome_names.index(name)
            else:
                idx = len(biome_names)
                biome_names.append(name)
            codes[mask] = idx

        # 海洋阈值 h<0.005 与 chunk._generate / world._biome_and_block_pure /
        # ui_minimap.compute_surface_blocks 一致（BUG-125 同步）。森林同样只
        # 覆盖温带低地：temp>=3 下界 + 海拔上界，与 chunk.py L295-297 逐字一致，
        # 否则高湿度的 snowy/tundra/mountain 格被改标 forest，A* 以 forest=1.0
        # 计价而非 snowy 0.8 / tundra 0.85 / mountain 0.6，系统性错估通行时间。
        ocean = hn < 0.005
        _assign(ocean, "ocean")
        _assign((temp < -10.0) & (hn >= 0.18), "snowy")
        _assign((temp >= -10.0) & (temp < 3.0) & (hn >= 0.18), "tundra")
        _assign((altitude > config.MOUNTAIN_ALT_RATIO * universe.height)
                & (hn >= 0.18), "mountain")
        _assign((temp > 25.0) & (moist < 0.3) & (hn >= 0.18), "desert")
        _assign((temp > 20.0) & (moist >= 0.3) & (hn >= 0.18), "savanna")
        _assign((temp < 15.0) & (temp >= 3.0) & (moist > 0.5) & (hn >= 0.18)
                & (altitude <= config.MOUNTAIN_ALT_RATIO * universe.height),
                "forest")
        self.biome_names = biome_names
        self.biome_codes = codes

        # 表面方块码（海洋柱=沙；雪盖；低地草→沙）
        from blocks import BLOCK_NAME, BLOCK_ID
        from biomes import BIOMES
        blk_names = ["grass"]
        blk_codes = np.zeros(hn.shape, dtype=np.uint8)

        def _assign_blk(mask, name):
            nonlocal blk_codes
            if not mask.any():
                return
            if name in blk_names:
                idx = blk_names.index(name)
            else:
                idx = len(blk_names)
                blk_names.append(name)
            blk_codes[mask] = idx

        biome_surface = {}
        for bn in biome_names:
            bd = BIOMES.get(bn)
            biome_surface[bn] = BLOCK_NAME.get(
                bd.surface_block, "dirt") if bd else "dirt"
        surf = np.empty(hn.shape, dtype=object)
        for i, bn in enumerate(biome_names):
            surf[codes == i] = biome_surface[bn]
        snow_cover = ((heights > config.SNOW_SURFACE_RATIO * universe.height)
                      & (temp < 2.0))
        surf = np.where(snow_cover, "snow", surf)
        surf = np.where((hn < 0.25) & (surf == "grass"), "sand", surf)
        surf = np.where(ocean, "sand", surf)
        for name in np.unique(surf):
            _assign_blk(surf == name, str(name))
        self.blk_names = blk_names
        self.blk_codes = blk_codes

    def in_bounds(self, x, z):
        return (self.x0 <= x < self.x0 + self.w
                and self.z0 <= z < self.z0 + self.h)

    def idx(self, x, z):
        """世界格 → 扁平数组索引（越界返回 -1）。"""
        dx = x - self.x0
        dz = z - self.z0
        if dx < 0 or dz < 0 or dx >= self.w or dz >= self.h:
            return -1
        return dx * self.h + dz


# ─── 代价画像 ──────────────────────────────────────────────

class CostProfile:
    """A* 边代价画像：由敌人类型偏好 + 天气 + 占用快照构建。

    所有代价单位为秒。同一走廊网格可配不同画像跑多次 A*（变体）。
    """

    __slots__ = ("climb_pass", "fall_limit", "dig_speed", "crowd_cost",
                 "climb_cost", "tower_cost", "wall_cost", "weather_mult", "rng",
                 "tower_cells", "wall_cells", "occupancy", "divert", "time1",
                 "terrain_mults", "affinity_mults", "siege", "grid")

    def __init__(self, type_name, weather, grid,
                 tower_cells=(), wall_cells=(), occupancy=None, divert=None,
                 variant_rng=None, siege=False):
        pers = gamedata.ENEMY_PERSONALITIES.get(
            type_name, gamedata.ENEMY_PERSONALITIES["regular"])
        env = gamedata.ENEMY_ENVIRONMENTS.get(type_name, {})
        ws = env.get("weather_speed") or {}
        self.grid = grid
        # 爬坡上限：整格通行上限（分数部分向下取整，2.5 → 可直接爬 2 格）
        self.climb_pass = max(1, int(math.floor(pers.get("climb_limit", 1.5))))
        # 下落上限：可下落高差上限（格，超出则下行该边禁行）。按类型
        # personality.fall_limit 读取，与运行时 _sync_y 重力下落对齐。
        self.fall_limit = max(1, int(math.floor(pers.get("fall_limit", 3))))
        self.dig_speed = float(pers.get("dig_speed", 0.0))
        self.crowd_cost = (config.PATH_CROWD_CELL_COST
                           * float(pers.get("crowd_avoidance", 0.5)))
        self.climb_cost = (config.PATH_CLIMB_SOFT_COST
                           * float(pers.get("terrain_aware", 0.5)))
        # 拆塔时间代价：hp/DPS 按 tower_hostility 折减；敌意高 → 几乎无代价
        atk = gamedata.ENEMY_ATTACKS.get(type_name, {})
        dps = (float(atk.get("damage", 4))
               / max(0.1, float(atk.get("cooldown", 1.2))))
        hostility = float(pers.get("tower_hostility", 0.35))
        default_hp = 60.0
        self.tower_cost = max(
            config.PATH_TOWER_CELL_COST_MIN * (1.0 - hostility),
            default_hp / max(0.1, dps) * (1.0 - hostility))
        self.tower_cost *= float(pers.get("aggressive", 0.5)) * 0.5 + 0.75
        self.siege = siege
        if siege:
            self.tower_cost = 0.0
        # 墙格代价：防御塔（城墙）近乎不可通行 —— 连续横竖/对角成墙时，
        # 敌人只能绕行；只有完全无路可绕（被墙围死）时才会选择强攻拆墙。
        # siege 态同样归零（强攻优先于绕行）。
        self.wall_cost = 0.0 if siege else float(config.PATH_WALL_CELL_COST)
        # 天气倍率：满强度倍率随 intensity 线性插值，雨雪取乘积
        rain = float(ws.get("rain", 1.0))
        snow = float(ws.get("snow", 1.0))
        wm = 1.0
        if weather is not None:
            if weather.is_raining and rain != 1.0:
                wm *= 1.0 + (rain - 1.0) * min(1.0, weather.intensity)
            if weather.is_snowing and snow != 1.0:
                wm *= 1.0 + (snow - 1.0) * min(1.0, weather.intensity)
        self.weather_mult = wm
        # 群系/表面方块倍率（类型覆盖 → 全局默认 → 1.0）
        base_ts = gamedata.TERRAIN_SPEED_MULTS
        type_ts = env.get("terrain_speed") or {}
        self.terrain_mults = [type_ts.get(b, base_ts.get(b, 1.0))
                              for b in grid.biome_names]
        aff = env.get("surface_affinity") or {}
        self.affinity_mults = [aff.get(b, 1.0) for b in grid.blk_names]
        self.tower_cells = tower_cells
        self.wall_cells = wall_cells
        self.occupancy = occupancy or {}
        self.divert = divert or {}
        self.rng = variant_rng

    def build_time1(self):
        """构建走廊内每格"单位距离耗时"数组（含天气，不含拥挤/塔）。"""
        tmult = np.asarray(self.terrain_mults, dtype=np.float64)
        amult = np.asarray(self.affinity_mults, dtype=np.float64)
        mult = tmult[self.grid.biome_codes] * amult[self.grid.blk_codes]
        mult = mult * self.weather_mult
        mult = np.maximum(mult, 0.05)
        self.time1 = (1.0 / mult).ravel()
        return self.time1

    def max_time1(self):
        """启发式用的最慢耗时（保证 h 可采纳）。"""
        return float(self.time1.max()) if hasattr(self, "time1") else 1.0


# ─── A* ────────────────────────────────────────────────────

# 8 邻接偏移 (dx, dz, 代价, 是否对角)
_NEIGHBORS = ((1, 0, 1.0, False), (-1, 0, 1.0, False), (0, 1, 1.0, False),
              (0, -1, 1.0, False), (1, 1, _SQRT2, True), (1, -1, _SQRT2, True),
              (-1, 1, _SQRT2, True), (-1, -1, _SQRT2, True))


def astar(grid: CorridorGrid, profile: CostProfile,
          start, goal, node_budget=None) -> list | None:
    """走廊网格 A*。返回格子路径 [(x, z), ...]（含起终点）或 None。

    profile.time1 必须已构建。塔格代价/拥挤/分流在循环内查字典。
    """
    if node_budget is None:
        node_budget = config.PATH_ASTAR_NODE_BUDGET
    sx, sz = start
    gx, gz = goal
    start_i = grid.idx(sx, sz)
    goal_i = grid.idx(gx, gz)
    if start_i < 0 or goal_i < 0:
        return None
    land = grid.land.ravel()
    heights = grid.heights.ravel()
    corridor = grid.corridor.ravel()
    no_dig_mask = grid.no_dig_mask.ravel()
    dig_floor = grid.dig_floor.ravel()
    time1 = profile.time1
    grid_w = grid.h  # 扁平索引步长（dx 方向一行的长度）

    # 可采纳启发式：octile × 最快耗时
    min_t1 = float(time1.min())
    oct_dx = abs(gx - sx)
    oct_dz = abs(gz - sz)
    h0 = (max(oct_dx, oct_dz) + ( _SQRT2 - 1.0) * min(oct_dx, oct_dz)) * min_t1

    tower_cells = profile.tower_cells
    tower_cost = profile.tower_cost
    wall_cells = profile.wall_cells
    wall_cost = profile.wall_cost
    occupancy = profile.occupancy
    crowd_cost = profile.crowd_cost
    divert = profile.divert
    climb_pass = profile.climb_pass
    fall_limit = profile.fall_limit
    dig_speed = profile.dig_speed
    climb_cost = profile.climb_cost

    g_score = {start_i: 0.0}
    came = {start_i: -1}
    open_heap = [(h0, 0.0, start_i)]
    closed = set()
    expansions = 0

    while open_heap:
        f, g, cur = heapq.heappop(open_heap)
        if cur in closed:
            continue
        closed.add(cur)
        expansions += 1
        if expansions > node_budget:
            return None
        if cur == goal_i:
            # 回溯
            path = []
            node = cur
            while node != -1:
                path.append((grid.x0 + node // grid_w,
                             grid.z0 + node % grid_w))
                node = came[node]
            path.reverse()
            return path

        cx = cur // grid_w
        cz = cur % grid_w
        ch = heights[cur]
        cur_time1 = time1[cur]
        cur_g = g
        for ddx, ddz, base_d, diag in _NEIGHBORS:
            rx = cx + ddx          # 局部格坐标
            rz = cz + ddz
            if rx < 0 or rz < 0 or rx >= grid.w or rz >= grid.h:
                continue
            ni = rx * grid_w + rz
            if ni in closed or not land[ni] or not corridor[ni]:
                continue
            if diag:
                # 禁止对角夹缝斜向穿越（corner cut）。
                # 斜向走 (cx,cz)→(cx+ddx,cz+ddz) 会擦过 (cx+ddx,cz) 与
                # (cx,cz+ddz) 两正交格的角。若其中任一格不可通行（墙、
                # 水域或禁行走廊），此斜步会切过障碍角——必须拒绝。
                # 原实现只对 wall_cells 检查，未检查陆地/走廊硬约束，
                # 导致 [陆 水 / 水 陆] 2×2 夹缝中可斜穿海洋/禁行区。
                oi1 = (cx + ddx) * grid_w + cz
                oi2 = cx * grid_w + (cz + ddz)
                if (not land[oi1] or not corridor[oi1]
                        or not land[oi2] or not corridor[oi2]):
                    continue
                if wall_cells and ((grid.x0 + cx + ddx, grid.z0 + cz) in wall_cells
                                   or (grid.x0 + cx, grid.z0 + cz + ddz) in wall_cells):
                    continue
                # 扫过格高度也需检查（BUG-051）：擦过的正交格若是过高/过深的
                # 崖体，斜步会切过实心岩体，故按爬升阈值拒绝上爬、按运行时
                # 语义放行下坠（BUG-102：下坠无硬上限，只有软代价）。
                h1 = heights[oi1]
                h2 = heights[oi2]
                if (h1 - ch > climb_pass or h2 - ch > climb_pass):
                    continue
            nh = heights[ni]
            dh = nh - ch
            if dh > 0:
                if dh > climb_pass:
                    if dig_speed <= 0:
                        continue  # 爬不上去也不能挖 → 绕行
                    if no_dig_mask[ni]:
                        continue  # 大本营 5×5 禁挖区：不能"挖上去"→ 绕行
                    # 平台挖掘守卫（D2）：在平台圆盘内开挖会产生"当前格+1"
                    # 的台阶，若低于平台下限则会把敌人锁在平台边缘回不去
                    # （运行时 enemy_dig_refused 同源拒绝）→ 该边禁行。
                    df = dig_floor[ni]
                    if df >= 0 and ch + 1 < df:
                        continue
                    need = dh - climb_pass
                    # 可攀段（climb_pass）同样要计爬升软代价，否则挖坡路线被
                    # 系统性低估、劣于绕行也可能被选中（BUG-052）。
                    t = (base_d * (cur_time1 + time1[ni]) * 0.5
                         + climb_pass * climb_cost + need / dig_speed)
                else:
                    t = (base_d * (cur_time1 + time1[ni]) * 0.5
                         + dh * climb_cost)
            elif dh < 0:
                # 下行：超过类型 fall_limit 硬禁行（与运行时 _sync_y 重力下落
                # + 跨格 fall_limit 硬限对齐，BUG-102 改版）。落差 ≤ fall_limit
                # 的下坡/浅坑正常通行（运行时抛物线落到底），超过则该边视为墙，
                # A* 绕行——避免敌人从过高高台"滑翔"式强坠。
                if -dh > fall_limit:
                    continue
                t = base_d * (cur_time1 + time1[ni]) * 0.5
            else:
                t = base_d * (cur_time1 + time1[ni]) * 0.5
            cell = (grid.x0 + rx, grid.z0 + rz)  # 世界格（塔/拥挤/分流查表用）
            if tower_cells and cell in tower_cells:
                t += tower_cost
            if wall_cells and cell in wall_cells:
                t += wall_cost  # 墙格巨额附加代价：连续成墙即被迫绕行
            if crowd_cost > 0.0:
                c = occupancy.get(cell)
                if c:
                    t += c * crowd_cost
            if divert:
                d = divert.get(cell)
                if d:
                    t += d
            ng = cur_g + t
            old = g_score.get(ni)
            if old is None or ng < old:
                g_score[ni] = ng
                came[ni] = cur
                hdx = abs(gx - (grid.x0 + rx))
                hdz = abs(gz - (grid.z0 + rz))
                hh = (max(hdx, hdz) + (_SQRT2 - 1.0) * min(hdx, hdz)) * min_t1
                heapq.heappush(open_heap, (ng + hh, ng, ni))

    return None


# ─── 平滑 ──────────────────────────────────────────────────

def _walk_cells(a, b):
    """枚举线段 a→b 穿过的格子序列（DDA，按穿过顺序，含起点；对角同时跨界
    时直接给出对角格——与敌人逐帧"进入格"检测的语义一致）。"""
    ax, az = a
    bx, bz = b
    x = math.floor(ax)
    z = math.floor(az)
    ex = math.floor(bx)
    ez = math.floor(bz)
    dx = bx - ax
    dz = bz - az
    yield (x, z)
    if dx == 0.0 and dz == 0.0:
        return
    step_x = 1 if dx > 0 else -1
    step_z = 1 if dz > 0 else -1
    t_dx = abs(1.0 / dx) if dx != 0.0 else math.inf
    t_dz = abs(1.0 / dz) if dz != 0.0 else math.inf
    if dx != 0.0:
        t_x = ((x + 1 - ax) if dx > 0 else (ax - x)) * t_dx
    else:
        t_x = math.inf
    if dz != 0.0:
        t_z = ((z + 1 - az) if dz > 0 else (az - z)) * t_dz
    else:
        t_z = math.inf
    while (x, z) != (ex, ez):
        if t_x < t_z:
            x += step_x
            t_x += t_dx
        elif t_z < t_x:
            z += step_z
            t_z += t_dz
        else:
            x += step_x
            z += step_z
            t_x += t_dx
            t_z += t_dz
        yield (x, z)


def _los_ok(grid: CorridorGrid, climb_pass: int, fall_limit: int,
            tower_cells, wall_cells=(), a=None, b=None) -> bool:
    """格 a→b 直线是否可走（不跨海洋/塔/超爬升阈值/超下落上限）。

    用 DDA 精确枚举穿过格（与敌人运行时进入格检测一致），逐格校验。
    额外禁止对角夹缝斜穿：斜率恰为 ±1 时 DDA 直接跨对角、跳过两正交格，
    若任一正交格不可通行（海洋/禁行走廊/墙），斜线即切过其角——与 A*
    的 2×2 夹缝 corner-cut 检查（land+corridor+wall）保持一致，否则平滑
    拉直可把两段折线拉成斜穿水/禁行/墙角的直线。
    下坠同样硬限（BUG-102 改版）：相邻两格落差 > fall_limit（下坠方向）
    即拒绝，与 astar 下行硬禁行、运行时跨格 fall_limit 检查对齐；
    上爬仍限 climb_pass（不越过未挖的墙）。
    """
    heights = grid.heights.ravel()
    land = grid.land.ravel()
    corridor = grid.corridor.ravel()
    # 实走的是格心连线（平滑路径点均为 x+0.5/z+0.5），而 _walk_cells 直接以
    # 整数格坐标走线会扫描"格角"而非"格心"穿越的格子集合（例 (0,0)→(3,1)
    # 漏检 (2,1) 却多扫 (2,0)），可错放或误拒直线。故统一用格心坐标走线。
    prev_h = None
    prev_cell = None
    for (cx, cz) in _walk_cells((a[0] + 0.5, a[1] + 0.5), (b[0] + 0.5, b[1] + 0.5)):
        i = grid.idx(cx, cz)
        if i < 0 or not land[i]:
            return False
        if tower_cells and (cx, cz) in tower_cells:
            return False  # 塔格保持为路径顶点（攻击逻辑需要）
        if prev_cell is not None:
            pcx, pcz = prev_cell
            ddx, ddz = cx - pcx, cz - pcz
            if ddx != 0 and ddz != 0:
                # 斜向跨格 (pcx,pcz)→(cx,cz)：检查被擦过的两正交格
                # (pcx+ddx,pcz) 与 (pcx,pcz+ddz)。任一侧不可通行即拒。
                o1 = grid.idx(pcx + ddx, pcz)
                o2 = grid.idx(pcx, pcz + ddz)
                if (o1 < 0 or not land[o1] or not corridor[o1]
                        or o2 < 0 or not land[o2] or not corridor[o2]):
                    return False
                if wall_cells and ((pcx + ddx, pcz) in wall_cells
                                   or (pcx, pcz + ddz) in wall_cells):
                    return False
                # 被擦过格的高度同样受高差阈值约束（BUG-053/BUG-102）：
                # 上爬限 climb_pass（防 LOS 切过不可攀的崖角）；下坠限
                # fall_limit（与 astar 下行硬禁行一致，不切过超高落差）。
                h1 = heights[o1]
                h2 = heights[o2]
                if (h1 - prev_h > climb_pass
                        or h2 - prev_h > climb_pass
                        or prev_h - h1 > fall_limit
                        or prev_h - h2 > fall_limit):
                    return False
        prev_cell = (cx, cz)
        h = heights[i]
        if prev_h is not None:
            dh = h - prev_h
            if dh > climb_pass:
                return False
            if prev_h - h > fall_limit:
                return False  # 下坠落差超 fall_limit：硬禁（对齐运行时）
        prev_h = h
    return True


def smooth_cells(grid: CorridorGrid, cells: list, climb_pass: int,
                 fall_limit: int, tower_cells, wall_cells=()) -> tuple:
    """A* 格路径 → 平滑世界点折线 ((x, y, z), ...)。

    1) string-pulling 拉直（LOS 约束：不跨海/塔/超限高差）
    2) 大转角弦切圆角（仅在约束允许处切）
    3) 逐点重锚 Y（双线性插值走廊高度）+ 去密
    """
    if len(cells) < 2:
        return tuple()
    # 1. 拉直
    pulled = [cells[0]]
    i = 0
    n = len(cells)
    while i < n - 1:
        j = n - 1
        while j > i + 1 and not _los_ok(grid, climb_pass, fall_limit,
                                        tower_cells, wall_cells,
                                        cells[i], cells[j]):
            j -= 1
        pulled.append(cells[j])
        i = j

    # 2. 转角弦切
    pts = [(x + 0.5, z + 0.5) for x, z in pulled]
    cut = config.PATH_SMOOTH_CORNER_CUT
    ang_min = config.PATH_SMOOTH_CORNER_ANGLE
    shaped = [pts[0]]
    for k in range(1, len(pts) - 1):
        px, pz = pts[k - 1]
        vx, vz = pts[k]
        nx_, nz_ = pts[k + 1]
        a1 = math.atan2(vz - pz, vx - px)
        a2 = math.atan2(nz_ - vz, nx_ - vx)
        turn = abs((a2 - a1 + math.pi) % (2 * math.pi) - math.pi)
        if turn < ang_min:
            shaped.append((vx, vz))
            continue
        # 弦切点：向转角两侧各收 cut 比例
        d1 = math.hypot(vx - px, vz - pz)
        d2 = math.hypot(nx_ - vx, nz_ - vz)
        if d1 < 0.3 or d2 < 0.3:
            shaped.append((vx, vz))
            continue
        ax_ = vx + (px - vx) * cut
        az_ = vz + (pz - vz) * cut
        bx_ = vx + (nx_ - vx) * cut
        bz_ = vz + (nz_ - vz) * cut
        # 弦切点须仍可走（LOS 检查两段新短线 + 弦本身），否则保留原顶点
        if (_los_ok(grid, climb_pass, fall_limit, tower_cells, wall_cells,
                    _cell_of(pts[k - 1]), _cell_of((ax_, az_)))
                and _los_ok(grid, climb_pass, fall_limit, tower_cells, wall_cells,
                            _cell_of((ax_, az_)), _cell_of((bx_, bz_)))
                and _los_ok(grid, climb_pass, fall_limit, tower_cells, wall_cells,
                            _cell_of((bx_, bz_)), _cell_of(pts[k + 1]))):
            shaped.append((ax_, az_))
            shaped.append((bx_, bz_))
        else:
            shaped.append((vx, vz))
    shaped.append(pts[-1])

    # 3. 世界点 + Y 锚定 + 去密
    heights = grid.heights.ravel()
    land = grid.land.ravel()
    out = []
    min_d2 = config.PATH_MIN_POINT_DIST ** 2
    last = None
    for (wx, wz) in shaped:
        if last is not None:
            dx = wx - last[0]
            dz = wz - last[2]  # 点是 (x, y+1, z)，z 分量是 [2]（原误用 [1] 高度分量，去密失效）
            if dx * dx + dz * dz < min_d2:
                continue
        gx = wx - grid.x0
        gz = wz - grid.z0
        ix = int(math.floor(gx))
        iz = int(math.floor(gz))
        if ix < 0 or iz < 0 or ix >= grid.w or iz >= grid.h:
            continue
        i00 = ix * grid.h + iz
        if not land[i00]:
            continue
        y = _bilinear_y(heights, land, grid.w, grid.h, ix, iz,
                        gx - ix, gz - iz)
        p = (wx, float(y + 1), wz)
        out.append(p)
        last = p
    if not out:
        return tuple()
    return tuple(out)


def _cell_of(pt):
    return (int(math.floor(pt[0])), int(math.floor(pt[1])))


def _bilinear_y(heights, land, w, h, ix, iz, fx, fz):
    """走廊网格内双线性插值地表高度。

    不可通行（非陆地/越界）的邻居列取主列高度 h00（docstring 原述
    "不可通行列取主列值"），避免海岸格的插值 Y 被海底高度拉低。
    """
    h00 = heights[ix * h + iz]
    h10 = h01 = h11 = h00
    if ix + 1 < w and land[(ix + 1) * h + iz]:
        h10 = heights[(ix + 1) * h + iz]
    if iz + 1 < h and land[ix * h + iz + 1]:
        h01 = heights[ix * h + iz + 1]
    if ix + 1 < w and iz + 1 < h and land[(ix + 1) * h + iz + 1]:
        h11 = heights[(ix + 1) * h + iz + 1]
    return (h00 * (1 - fx) * (1 - fz) + h10 * fx * (1 - fz)
            + h01 * (1 - fx) * fz + h11 * fx * fz)


# ─── 变体管理器 ────────────────────────────────────────────

class Pathfinder:
    """变体缓存 + 惰性重算 + 每帧预算。

    game 每帧调 begin_frame()；出生/敌人过期重估走 get_variants()。
    occupancy 由 game 每帧刷新（{(x,z): 敌人数}）。
    """

    def __init__(self, universe):
        self.universe = universe
        self.variants: dict = {}   # (sid, type_name) -> {"version", "paths"}
        self.occupancy: dict = {}  # {(x,z): 敌人数}，game 每帧刷新（A* 拥挤代价）
        self.path_occupancy: dict = {}  # {id(PathData): 敌人数}，game 每帧刷新（选路拥挤）
        self.tower_cells: set = set()   # 与 universe._tower_cells 同步引用
        self.wall_cells: set = set()    # 与 universe._wall_cells 同步引用（防御塔格）
        self._grid_cache: dict = {}     # (sid, half) -> CorridorGrid（宽度因类型 detour 而异）
        self.widened: set = set()       # (sid, type_name) 升级加宽走廊后才有路的键
        self._fail_logged: dict = {}    # (sid, type_name) -> 已告警的 world_version
        self._budget = 0
        self._seen_version = -1
        # D3 局部重规划缓存：((sx,sz), type_name) -> (world_version, PathData|None)
        # 有界（PATH_LOCAL_GRID_CACHE_MAX），FIFO 淘汰；版本变化整体失效。
        self._local_cache: dict = {}
        # 升级阶梯记忆：(sid, type_name) -> 上次成功的最小走廊半宽。
        # 地形版本变化后优先按它重试，避免每次 bump 都全阶梯重跑（深层
        # 阶梯是秒级开销，挖掘频繁的坑底世界依赖此记忆）。
        self._pref_half: dict = {}

    def begin_frame(self):
        self._budget = config.PATH_REPATH_BUDGET_PER_FRAME
        self.tower_cells = self.universe._tower_cells
        self.wall_cells = self.universe._wall_cells
        # world_version 变化（塔/方块增减）→ 全部变体与走廊网格惰性失效
        if self._seen_version != self.universe.world_version:
            self._seen_version = self.universe.world_version
            self.invalidate()

    def invalidate(self):
        """world_version 变化后：全部变体与走廊网格标脏（惰性重算）。"""
        self._grid_cache.clear()
        self._local_cache.clear()
        for key, entry in self.variants.items():
            entry["version"] = -1

    def _corridor_half_width(self, sx, sz, bx, bz, detour_tolerance):
        L = math.hypot(bx - sx, bz - sz)
        r = max(1.0, float(detour_tolerance))
        half = 4 + int(math.ceil(L * math.sqrt(max(0.0, r * r - 1.0)) / 3.0))
        return max(3, min(config.PATH_CORRIDOR_MAX_HALF, half))

    def _get_grid(self, sid: int, half: int):
        """取 (sid, half) 走廊网格，缺失则构建。

        half 由调用方按该类型的 detour_tolerance 预先算好——不同类型
        各用自己的走廊宽度，不能互相覆盖（曾因按 sid 缓存导致 tank
        被先算的 regular 窄走廊顶掉唯一陆路）。
        """
        key = (sid, half)
        grid = self._grid_cache.get(key)
        if grid is not None:
            return grid
        sp = self.universe.spawners.get(sid)
        if sp is None:
            return None
        u = self.universe
        grid = CorridorGrid(u, sp.x, sp.z, u.base_x, u.base_z, half)
        self._grid_cache[key] = grid
        # FIFO 有界（D1b 放宽走廊后同一点会缓存多个宽度，防内存膨胀）
        while len(self._grid_cache) > config.PATH_GRID_CACHE_MAX:
            oldest = next(iter(self._grid_cache))
            if oldest == key:
                break
            del self._grid_cache[oldest]
        return grid

    def get_variants(self, sid: int, type_name: str) -> list:
        """取该刷怪点×类型的路径变体（过期则预算内重算）。

        返回可能为 stale 列表或空（调用方兜底）。仅当该键从未计算过时
        才同步补算（保证出生有路）；失败结果同样缓存到当前版本，
        避免无解场景每帧重跑 A*。
        """
        key = (sid, type_name)
        version = self.universe.world_version
        entry = self.variants.get(key)
        if entry is not None and entry["version"] == version:
            return entry["paths"]
        # 硬组合（升级阶梯才有路的组合）版本变化时不立即重建：它们每次
        # 重算都要跑秒级阶梯 A*，而挖掘频繁的世界（敌人自己挖台阶）每个
        # 挖掘会话都会 bump 版本，逐 bump 重算会把帧时间吃光（坑底/高山
        # 世界的仿真因此慢到不可用）。沿用旧变体，直到首段不可走
        # （_request_repath 里 _approach_ok 失败 → rebuild_key 强制重算）
        # 或停滞看门狗介入（局部优先）。普通组合保持原有"版本变化即重算"。
        if (entry is not None and entry["paths"]
                and key in self._pref_half):
            return entry["paths"]
        if self._budget > 0:
            self._budget -= 1
            paths = self._build_variants(sid, type_name)
            self.variants[key] = {"version": version, "paths": paths}
            # 变体已写回缓存，此时重建路径格集合才包含本次路线（BUG-110）
            self._rebuild_path_cells()
            return paths
        if entry is not None:
            return entry["paths"]  # stale 或上次失败的空结果，敌人稍后重估
        # 从未有路径：同步补算（出生必须有路）
        paths = self._build_variants(sid, type_name)
        self.variants[key] = {"version": version, "paths": paths}
        # 同步补算分支同样写回后再重建（BUG-110）
        self._rebuild_path_cells()
        return paths

    def _rebuild_path_cells(self):
        """按当前存活路线整体重建 u._path_cells（BUG-110）。

        原实现在 _build_variants 循环内重建，但那时本次刚算出的变体尚未写回
        self.variants（get_variants/rebuild_key 在调用返回后才赋值），且
        enemy_paths 也不含变体路线——结果路径格集合里只有上一版本的陈旧格、
        缺本次路线的格子，道具/物品避让会躲开旧路却正好压在新路上（BUG-055
        想消除的缺陷以另一种形式复现）。改为在变体写回缓存后统一调用本方法：
        清空后从 enemy_paths 各点 + self.variants 全部路线重新收集，成为
        唯一事实来源。
        """
        u = self.universe
        fresh = set()
        for route in u.enemy_paths.values():
            fresh.update((math.floor(x), math.floor(z))
                         for x, _y, z in route.points)
        for entry in self.variants.values():
            for variant in entry.get("paths") or []:
                fresh.update((math.floor(x), math.floor(z))
                             for x, _y, z in variant.points)
        u._path_cells.clear()
        u._path_cells.update(fresh)

    def _build_variants(self, sid, type_name) -> list:
        u = self.universe
        sp = u.spawners.get(sid)
        if sp is None:
            return []
        pers = gamedata.ENEMY_PERSONALITIES.get(
            type_name, gamedata.ENEMY_PERSONALITIES["regular"])
        detour = pers.get("detour_tolerance", 1.5)
        half = self._corridor_half_width(sp.x, sp.z, u.base_x, u.base_z,
                                         detour)
        grid = self._get_grid(sid, half)
        if grid is None:
            return []
        start = (sp.x, sp.z)
        goal = (u.base_x, u.base_z)
        if not grid.land.ravel()[grid.idx(*goal)]:
            # 大本营格必须可走（正常世界生成保证），保险兜底
            return []
        if not grid.land.ravel()[grid.idx(*start)]:
            return []

        paths = []
        divert = {}
        widened = False
        for k in range(config.PATH_VARIANT_COUNT):
            seed_str = f"{u.seed}|{sid}|{type_name}|{k}"
            rng = random.Random(seed_str)
            profile = CostProfile(
                type_name, u.weather, grid,
                tower_cells=u._tower_cells,
                wall_cells=u._wall_cells,
                occupancy=self.occupancy,
                divert=divert,
                variant_rng=rng)
            profile.build_time1()
            cells = astar(grid, profile, start, goal)
            if cells is None:
                if not paths:
                    # 常规失败 → 攻城模式重试（塔格零代价，穿塔自动触发攻击）
                    siege_profile = CostProfile(
                        type_name, u.weather, grid,
                        tower_cells=u._tower_cells,
                        wall_cells=u._wall_cells,
                        occupancy=self.occupancy,
                        divert=divert,
                        variant_rng=rng, siege=True)
                    siege_profile.build_time1()
                    cells = astar(grid, siege_profile, start, goal,
                                  node_budget=config.PATH_ASTAR_NODE_BUDGET * 2)
                if cells is None and not widened:
                    # D1b 攻城仍无解 → 走廊升级阶梯逐级放宽重试。
                    # 实测（反向 BFS 穷举交叉验证）seed3091 有 23 个组合
                    # 地形可达却被判"无解"，逐项放宽实验证明瓶颈是走廊半宽
                    # 与节点预算两者（half=160 需 9.6 万展开、360 需 36 万，
                    # 见 _probe_escalation_need.py 与 config 阶梯注释）。
                    # detour_tolerance 是代价偏好，硬无路时放宽优先于让
                    # 敌人退回不懂地形的 legacy 路径。深层升级仅在本版本
                    # 首次构建时发生，结果随变体缓存复用。
                    # 性能：记住该 (sid,type) 上次成功的最小半宽（_pref_half），
                    # 地形版本变化后优先按它重试——挖掘频繁的世界里阶梯全量
                    # 重跑是秒级开销（坑底世界每次 bump 都会重算），命中
                    # 记忆宽度可把多次尝试压成一次。记忆宽度失败再走全阶梯。
                    ladder = list(config.PATH_CORRIDOR_ESCALATION)
                    pref = self._pref_half.get((sid, type_name))
                    if pref is not None:
                        ladder.sort(key=lambda hb: 0 if hb[0] == pref else 1
                                    if hb[0] < pref else 2)
                    for esc_half, esc_budget in ladder:
                        if esc_half <= grid.half_width:
                            continue
                        wide_grid = self._get_grid(sid, esc_half)
                        if wide_grid is None or wide_grid is grid:
                            continue
                        wide_profile = CostProfile(
                            type_name, u.weather, wide_grid,
                            tower_cells=u._tower_cells,
                            wall_cells=u._wall_cells,
                            occupancy=self.occupancy,
                            divert=divert,
                            variant_rng=rng, siege=True)
                        wide_profile.build_time1()
                        cells = astar(wide_grid, wide_profile, start, goal,
                                      node_budget=esc_budget)
                        if cells is not None:
                            widened = True
                            grid = wide_grid
                            self.widened.add((sid, type_name))
                            self._pref_half[(sid, type_name)] = esc_half
                            break
                if cells is None:
                    if not paths:
                        self._log_path_failure(sid, type_name)
                    break
            dig_cells = self._extract_dig_cells(grid, cells, profile.climb_pass)
            if self._has_forbidden_dig_edge(grid, cells, profile.climb_pass):
                # BUG-101 规划一致性防线：A* 理论上不可能产出"挖进禁挖区"
                # 的边，此处若触发说明规划/运行时守卫脱节——弃用该变体。
                print(f"[pathfinding] 警告: {type_name}@sid={sid} 规划含禁挖区"
                      f"挖掘边，变体 {k} 已弃用（version={u.world_version}）")
                break
            pts = smooth_cells(grid, cells, profile.climb_pass,
                               profile.fall_limit, u._tower_cells, u._wall_cells)
            if len(pts) < 2:
                pts = tuple(
                    (x + 0.5, float(grid.heights.ravel()[grid.idx(x, z)]) + 1.0,
                     z + 0.5) for x, z in cells)
            slope_sum = sum(abs(pts[i + 1][1] - pts[i][1])
                            for i in range(len(pts) - 1))
            pd = PathData(pts, len(pts), slope_sum / max(1, len(pts) - 1),
                          frozenset(dig_cells))
            paths.append(pd)
            # 路径格集合的重建已移出本循环：_build_variants 运行期间本次变体
            # 尚未写回 self.variants，循环内重建会漏掉当前路线（BUG-110），
            # 改由 get_variants/rebuild_key 写回缓存后调用 _rebuild_path_cells。
            # 下一变体沿此路径加罚 → 走出不同路线
            for c in cells:
                divert[c] = divert.get(c, 0.0) + config.PATH_VARIANT_DIVERSION
            if k == 0 and cells:
                # 变体 1 额外加随机扰动（种子稳定）
                for (jx, jz) in cells:
                    if rng.random() < 0.25:
                        divert[(jx, jz)] = divert.get(
                            (jx, jz), 0.0) + rng.uniform(0.3, 1.0)
        if paths and not widened:
            self.widened.discard((sid, type_name))
        return paths

    def _log_path_failure(self, sid, type_name):
        """完全无解时告警一次（同版本不重复）。敌人将退回 legacy 路径。"""
        v = self.universe.world_version
        if self._fail_logged.get((sid, type_name)) == v:
            return
        self._fail_logged[(sid, type_name)] = v
        sp = self.universe.spawners.get(sid)
        print(f"[pathfinding] 无解: seed={self.universe.seed} "
              f"sid={sid}({sp.x if sp else '?'},{sp.z if sp else '?'}) "
              f"类型={type_name} version={v}（升级走廊仍不可达，"
              f"疑似孤岛刷怪点）")

    @staticmethod
    def _extract_dig_cells(grid, cells, climb_pass) -> set:
        """相邻格上行超过通行上限的目标格 = 需要挖掘的格。

        BUG-101 一致化：跳过禁挖区（大本营 5×5）内的格。A* 主循环本身就
        不产生"挖进禁挖区"的边（no_dig_mask，见 astar），这里是防御性兜底，
        保证 dig_cells（渲染挖掘标记 / 道具避让用）永远不把禁挖格标成待挖。
        D2 同理跳过会触发平台守卫的格（astar 已禁行，这里保证标记一致）。
        """
        heights = grid.heights.ravel()
        ndm = grid.no_dig_mask.ravel()
        dfl = grid.dig_floor.ravel()
        digs = set()
        for i in range(len(cells) - 1):
            x0, z0 = cells[i]
            x1, z1 = cells[i + 1]
            h0 = heights[grid.idx(x0, z0)]
            h1 = heights[grid.idx(x1, z1)]
            if h1 - h0 > climb_pass:
                i1 = grid.idx(x1, z1)
                if i1 >= 0 and ndm[i1]:
                    continue
                if i1 >= 0 and dfl[i1] >= 0 and h0 + 1 < dfl[i1]:
                    continue
                digs.add((x1, z1))
        return digs

    @staticmethod
    def _has_forbidden_dig_edge(grid, cells, climb_pass) -> bool:
        """规划出的路线是否包含"挖到拒挖列"的边（理论上不应发生）。

        BUG-101：A* 主循环对所有上行超限边检查 dig 约束并拒绝——
          1. 挖进大本营 5×5 禁挖区（no_dig_mask）；
          2. 目标格非陆地/海洋柱（land 掩码直接排除该格）。
        D2：平台守卫区内会破坏回环台阶的开挖（astar 的 dig_floor 检查）。
        此检查是规划层的一致性断言：一旦出现，说明规划与运行时
        的守卫（dig_block_id / enemy_dig_refused）脱节，该变体必须作废
        而不是下发（否则敌人会走到"需要挖的经过那里，然后挖不动"的死路）。
        """
        heights = grid.heights.ravel()
        land = grid.land.ravel()
        ndm = grid.no_dig_mask.ravel()
        dfl = grid.dig_floor.ravel()
        for i in range(len(cells) - 1):
            x0, z0 = cells[i]
            x1, z1 = cells[i + 1]
            i1 = grid.idx(x1, z1)
            if i1 < 0 or not land[i1]:
                continue
            h0 = heights[grid.idx(x0, z0)]
            h1 = heights[i1]
            if h1 - h0 <= climb_pass:
                continue   # 不是挖掘边
            # D2 断言：平台区开挖会把敌人锁在平台边缘
            if dfl[i1] >= 0 and h0 + 1 < dfl[i1]:
                return True
            # BUG-101 断言：挖进大本营 5×5 禁挖区
            if ndm[i1]:
                return True
        return False

    def render_paths(self) -> dict:
        """渲染用：{(sid, type_name, idx): PathData}（仅缓存中的变体）。"""
        out = {}
        for (sid, type_name), entry in self.variants.items():
            if not entry["paths"]:
                continue
            for i, pd in enumerate(entry["paths"]):
                out[(sid, type_name, i)] = pd
        return out

    def rebuild_key(self, sid: int, type_name: str) -> list:
        """强制同步重算该 (sid, type) 的全部变体并覆盖缓存，返回新列表。

        BUG-103：敌人重寻路时若缓存变体"首段全部不可走"（通常是历史版本
        记账漏洞：mark_passage 改低列顶未 bump world_version，缓存路径
        过期却仍被视为当前版本），需要跳过缓存/预算直接按当前地形重算，
        用新路线判断敌人能否继续走；重算结果同样缓存（含无解）。
        """
        version = self.universe.world_version
        paths = self._build_variants(sid, type_name)
        self.variants[(sid, type_name)] = {"version": version, "paths": paths}
        # 强制重算已写回缓存，重建路径格集合（BUG-110）
        self._rebuild_path_cells()
        return paths

    # ─── 局部重规划（D3：从敌人当前格而非刷怪点出发） ──────────

    def plan_from(self, start, type_name: str):
        """从任意世界格规划到大本营的局部路径（D2/D3 卡死自救）。

        与 _build_variants 的区别：起点是敌人当前格而非刷怪点，结果不进
        变体缓存、不影响变体分流；按 ((起点格, 类型), 版本) 小缓存去重
        （同一敌人看门狗重试不会重复跑 A*）。

        背景：spawner 侧路线的首段接入检查（_approach_ok）从敌人当前位置
        出发，一旦敌人已挖到平台边缘/半山腰，所有"从刷怪点出发"的路线都
        无法就近接入 → 旧实现直接清路径原地等待。实测证明该局面下从脚下
        出发局部必有路（seed3091 (-3,-1)、seed79 (0,3) 均为 5 格路）。

        返回 PathData 或 None（无解）。失败结果同样入缓存（同版本不重算）。
        """
        u = self.universe
        sx, sz = start
        gx, gz = u.base_x, u.base_z
        key = ((sx, sz), type_name)
        hit = self._local_cache.get(key)
        if hit is not None and hit[0] == u.world_version:
            return hit[1]

        path = None
        if (sx, sz) != (gx, gz):
            L = math.hypot(gx - sx, gz - sz)
            base_half = max(4, min(config.PATH_LOCAL_CORRIDOR_MAX,
                                   8 + int(L * 0.6)))
            # 局部规划同样逐级放宽：实测 seed148 坑底 tank 从 (54,20) 回
            # 基地需 half=160（绕坑壁 353 格），基础半宽无解。
            attempts = [(base_half, config.PATH_ASTAR_NODE_BUDGET)]
            for esc_half, esc_budget in config.PATH_LOCAL_ESCALATION:
                if esc_half > base_half:
                    attempts.append((esc_half, esc_budget))
            for half, budget in attempts:
                grid = CorridorGrid(u, sx, sz, gx, gz, half)
                start_i = grid.idx(sx, sz)
                goal_i = grid.idx(gx, gz)
                land = grid.land.ravel()
                if not (start_i >= 0 and goal_i >= 0
                        and land[start_i] and land[goal_i]):
                    continue
                profile = CostProfile(
                    type_name, u.weather, grid,
                    tower_cells=u._tower_cells,
                    wall_cells=u._wall_cells,
                    occupancy=self.occupancy)
                profile.build_time1()
                cells = astar(grid, profile, (sx, sz), (gx, gz),
                              node_budget=budget)
                if cells is None:
                    # 攻城重试（塔/墙零代价），与 _build_variants 同款回退
                    siege_profile = CostProfile(
                        type_name, u.weather, grid,
                        tower_cells=u._tower_cells,
                        wall_cells=u._wall_cells,
                        occupancy=self.occupancy, siege=True)
                    siege_profile.build_time1()
                    cells = astar(grid, siege_profile, (sx, sz), (gx, gz),
                                  node_budget=budget * 2)
                if cells is None or self._has_forbidden_dig_edge(
                        grid, cells, profile.climb_pass):
                    continue
                pts = smooth_cells(grid, cells, profile.climb_pass,
                                   profile.fall_limit,
                                   u._tower_cells, u._wall_cells)
                if len(pts) < 2:
                    pts = tuple(
                        (x + 0.5,
                         float(grid.heights.ravel()[grid.idx(x, z)]) + 1.0,
                         z + 0.5) for x, z in cells)
                slope_sum = sum(abs(pts[i + 1][1] - pts[i][1])
                                for i in range(len(pts) - 1))
                path = PathData(pts, len(pts),
                                slope_sum / max(1, len(pts) - 1),
                                frozenset(self._extract_dig_cells(
                                    grid, cells, profile.climb_pass)))
                break

        # 入缓存（有界 FIFO；含 None 失败结果，同版本不重算）
        if len(self._local_cache) >= config.PATH_LOCAL_GRID_CACHE_MAX:
            self._local_cache.pop(next(iter(self._local_cache)))
        self._local_cache[key] = (u.world_version, path)
        return path

    # ─── L0 可达性判定（供 Game 守卫/UI/诊断共用） ─────────────

    def reachability(self, sid: int, type_name: str) -> bool:
        """该 (刷怪点×类型) 当前是否可达大本营（与出兵判定同一口径）。

        结果与变体缓存同版本：有缓存直接查，无缓存触发补算（受每帧预算
        约束，预算耗尽返回上次已知值——Game 守卫调用频率低，通常立即补上）。
        """
        entry = self.variants.get((sid, type_name))
        version = self.universe.world_version
        if entry is not None and entry["version"] == version:
            return bool(entry["paths"])
        if entry is not None and self._budget <= 0:
            return bool(entry["paths"])  # 预算耗尽：先按上次结果（帧内稍后再算）
        paths = self.get_variants(sid, type_name)
        return bool(paths)

    def blocked_types(self, sid: int) -> list:
        """该刷怪点当前不可达的类型列表（UI/诊断用）。"""
        u = self.universe
        if sid not in u.spawners:
            return []
        return [t for t in gamedata.ENEMY_PERSONALITIES
                if not self.reachability(sid, t)]
