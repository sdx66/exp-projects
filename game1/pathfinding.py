"""
pathfinding.py - 敌人寻路系统

表面网格 A*（8 邻接）+ 类型化代价画像 + 约束感知平滑 + 变体管理。

设计要点：
  - 代价单位 = 秒（走过这条边预计花费的时间），使"绕远 vs 拆塔 vs 挖方"
    天然可比：A* 最小化总时间。
  - 边代价 = 距离/移动速度倍率 + 爬升软代价 + 挖方时间 + 拆塔时间×(1-意愿)
    + 拥挤惩罚 + 变体分流惩罚。
  - 硬约束：上行超过爬坡上限且不可挖 → 禁行；下行超过坠落上限 → 禁行；
    海洋禁行。这保证"高差足够大一定绕行"。
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
                 "max_height", "corridor")

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
        self.land = hn >= 0.20

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

        ocean = hn < 0.004
        _assign(ocean, "ocean")
        _assign((temp < -10.0) & (hn >= 0.18), "snowy")
        _assign((temp >= -10.0) & (temp < 3.0) & (hn >= 0.18), "tundra")
        _assign((altitude > config.MOUNTAIN_ALT_RATIO * universe.height)
                & (hn >= 0.18), "mountain")
        _assign((temp > 25.0) & (moist < 0.3) & (hn >= 0.18), "desert")
        _assign((temp > 20.0) & (moist >= 0.3) & (hn >= 0.18), "savanna")
        _assign((temp < 15.0) & (moist > 0.5) & (hn >= 0.18), "forest")
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

    __slots__ = ("climb_pass", "fall_max", "dig_speed", "crowd_cost",
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
        self.fall_max = config.PATH_FALL_LIMIT
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
    fall_max = profile.fall_max
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
            if diag and wall_cells:
                # 禁止沿两面墙的对角夹缝斜向穿越（corner cut）。
                # 斜向走 (cx,cz)→(cx+ddx,cz+ddz) 时，两面墙若恰好占据
                # (cx+ddx,cz) 与 (cx,cz+ddz)，则走廊在该处被对角连续墙
                # 封死 —— 不加此检查会让"横竖连续和对角连续"的防御塔
                # 中间留出一条可挤过的对角缝。
                if ((grid.x0 + cx + ddx, grid.z0 + cz) in wall_cells
                        or (grid.x0 + cx, grid.z0 + cz + ddz) in wall_cells):
                    continue
            nh = heights[ni]
            dh = nh - ch
            if dh > 0:
                if dh > climb_pass:
                    if dig_speed <= 0:
                        continue  # 爬不上去也不能挖 → 绕行
                    need = dh - climb_pass
                    t = base_d * (cur_time1 + time1[ni]) * 0.5 + need / dig_speed
                else:
                    t = (base_d * (cur_time1 + time1[ni]) * 0.5
                         + dh * climb_cost)
            elif dh < 0:
                if -dh > fall_max:
                    continue  # 崖太深 → 绕行
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


def _los_ok(grid: CorridorGrid, climb_pass: int, fall_max: int,
            tower_cells, wall_cells=(), a=None, b=None) -> bool:
    """格 a→b 直线是否可走（不跨海洋/塔/超限高差）。

    用 DDA 精确枚举穿过格（与敌人运行时进入格检测一致），逐格校验。
    额外禁止沿两面墙的对角夹缝斜穿（与 A* 的 corner-cut 检查一致），
    否则平滑拉直可能把两段折线拉成一条斜穿对角墙缝的直线。
    """
    heights = grid.heights.ravel()
    land = grid.land.ravel()
    prev_h = None
    prev_cell = None
    for (cx, cz) in _walk_cells(a, b):
        i = grid.idx(cx, cz)
        if i < 0 or not land[i]:
            return False
        if tower_cells and (cx, cz) in tower_cells:
            return False  # 塔格保持为路径顶点（攻击逻辑需要）
        if wall_cells and prev_cell is not None:
            pcx, pcz = prev_cell
            ddx, ddz = cx - pcx, cz - pcz
            if ddx != 0 and ddz != 0:
                # 斜向跨格：两侧相邻格若为墙，则该斜线穿过对角夹缝
                if ((pcx + ddx, pcz) in wall_cells
                        or (pcx, pcz + ddz) in wall_cells):
                    return False
        prev_cell = (cx, cz)
        h = heights[i]
        if prev_h is not None:
            dh = h - prev_h
            if dh > climb_pass or -dh > fall_max:
                return False
        prev_h = h
    return True


def smooth_cells(grid: CorridorGrid, cells: list, climb_pass: int,
                 fall_max: int, tower_cells, wall_cells=()) -> tuple:
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
        while j > i + 1 and not _los_ok(grid, climb_pass, fall_max,
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
        if (_los_ok(grid, climb_pass, fall_max, tower_cells, wall_cells,
                    _cell_of(pts[k - 1]), _cell_of((ax_, az_)))
                and _los_ok(grid, climb_pass, fall_max, tower_cells, wall_cells,
                            _cell_of((ax_, az_)), _cell_of((bx_, bz_)))
                and _los_ok(grid, climb_pass, fall_max, tower_cells, wall_cells,
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
            dz = wz - last[1]
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

    注意：land 参数当前未被使用——插值对所有在界内的邻居列一视同仁，
    仅对越界邻居回退为主列高度。docstring 原述"不可通行列取主列值"
    与实现不符，故此处如实标注。保留 land 参数以兼容调用方。
    """
    h00 = heights[ix * h + iz]
    h10 = h01 = h11 = h00
    if ix + 1 < w:
        h10 = heights[(ix + 1) * h + iz]
    if iz + 1 < h:
        h01 = heights[ix * h + iz + 1]
    if ix + 1 < w and iz + 1 < h:
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
        if self._budget > 0:
            self._budget -= 1
            paths = self._build_variants(sid, type_name)
            self.variants[key] = {"version": version, "paths": paths}
            return paths
        if entry is not None:
            return entry["paths"]  # stale 或上次失败的空结果，敌人稍后重估
        # 从未有路径：同步补算（出生必须有路）
        paths = self._build_variants(sid, type_name)
        self.variants[key] = {"version": version, "paths": paths}
        return paths

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
                    # 攻城仍无解 → 升级到上限宽度走廊（海湾/山脊的出路可能
                    # 落在容忍度推导的走廊外；detour_tolerance 是代价偏好，
                    # 硬无路时放宽优先于让敌人退回不懂地形的 legacy 路径）
                    wide_grid = self._get_grid(
                        sid, config.PATH_CORRIDOR_MAX_HALF)
                    if wide_grid is not None and wide_grid is not grid:
                        wide_profile = CostProfile(
                            type_name, u.weather, wide_grid,
                            tower_cells=u._tower_cells,
                            wall_cells=u._wall_cells,
                            occupancy=self.occupancy,
                            divert=divert,
                            variant_rng=rng, siege=True)
                        wide_profile.build_time1()
                        cells = astar(wide_grid, wide_profile, start, goal,
                                      node_budget=config.PATH_ASTAR_NODE_BUDGET * 2)
                        if cells is not None:
                            widened = True
                            grid = wide_grid
                            self.widened.add((sid, type_name))
                if cells is None:
                    if not paths:
                        self._log_path_failure(sid, type_name)
                    break
            dig_cells = self._extract_dig_cells(grid, cells, profile.climb_pass)
            pts = smooth_cells(grid, cells, profile.climb_pass,
                               profile.fall_max, u._tower_cells, u._wall_cells)
            if len(pts) < 2:
                pts = tuple(
                    (x + 0.5, float(grid.heights.ravel()[grid.idx(x, z)]) + 1.0,
                     z + 0.5) for x, z in cells)
            slope_sum = sum(abs(pts[i + 1][1] - pts[i][1])
                            for i in range(len(pts) - 1))
            pd = PathData(pts, len(pts), slope_sum / max(1, len(pts) - 1),
                          frozenset(dig_cells))
            paths.append(pd)
            # 路径格并入全局路径格集合（道具避让等语义沿用）
            u._path_cells.update(cells)
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
        """相邻格上行超过通行上限的目标格 = 需要挖掘的格。"""
        heights = grid.heights.ravel()
        digs = set()
        for i in range(len(cells) - 1):
            x0, z0 = cells[i]
            x1, z1 = cells[i + 1]
            h0 = heights[grid.idx(x0, z0)]
            h1 = heights[grid.idx(x1, z1)]
            if h1 - h0 > climb_pass:
                digs.add((x1, z1))
        return digs

    def render_paths(self) -> dict:
        """渲染用：{(sid, type_name, idx): PathData}（仅缓存中的变体）。"""
        out = {}
        for (sid, type_name), entry in self.variants.items():
            if not entry["paths"]:
                continue
            for i, pd in enumerate(entry["paths"]):
                out[(sid, type_name, i)] = pd
        return out
