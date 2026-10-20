"""
world.py - 种子驱动的无限宇宙生成
区块制地形：按需生成、缓存、确定性。
大本营概念：敌人从刷怪点沿路径走向大本营。
"""

import random
import math
import zlib
from collections import OrderedDict
from dataclasses import dataclass, field

import config
import gamedata
from biomes import BIOMES, BiomeDef
from chunk import Chunk
from noise import fbm_2d, value_noise_2d, generate_heightmap
from blocks import BLOCK_ID, get_block, is_solid, is_transparent
from props import Prop


# ─── 路径记录 ──────────────────────────────────────────────

@dataclass(frozen=True)
class PathData:
    """敌人路径及预计算元数据。"""
    points: tuple
    length: int
    avg_slope: float


@dataclass(frozen=True)
class Spawner:
    """刷怪点（确定性，由 seed 决定位置；运行时状态见 game.SpawnerRuntime）。

    id 与 universe.enemy_paths[id] 对齐，0 = 离大本营最近。
    kind: 刷怪点种类（data/spawners.json），由所在生物群系决定。
    """
    id: int
    x: int          # 世界格坐标
    z: int
    surface_y: int  # 格表面高度
    biome: str      # 格生物群系
    kind: str = "standard"  # 刷怪点种类


def _make_path_data(points: list) -> PathData:
    """预计算路径坡度。"""
    if not points:
        return PathData((), 0, 0.0)
    slope_sum = 0
    for i in range(len(points) - 1):
        slope_sum += abs(points[i + 1][1] - points[i][1])
    return PathData(tuple(points), len(points), slope_sum / max(1, len(points) - 1))


# ─── 天气状态 ──────────────────────────────────────────────

@dataclass
class WeatherState:
    """当前天气状态"""
    is_raining: bool = False
    is_snowing: bool = False
    intensity: float = 0.0
    wind_direction: float = 0.0
    wind_strength: float = 0.0


# ─── 宇宙 ──────────────────────────────────────────────────

class Universe:
    """
    种子驱动的无限宇宙。
    区块按需生成，确定性输出。
    大本营在原点 (0, 0)，敌人沿路径从刷怪点走向大本营。
    """

    def __init__(self, seed):
        # 种子处理
        if isinstance(seed, str):
            import zlib
            seed = zlib.crc32(seed.encode("utf-8")) & 0xFFFFFFFF
        self.seed = int(seed)
        self.height = config.WORLD_HEIGHT
        self.rng = random.Random(seed)

        # 宇宙参数（由种子决定）
        self.base_temperature = 10.0 + self.rng.uniform(-5.0, 15.0)
        self.moisture_base = self.rng.uniform(0.3, 0.7)

        # 大本营位置（世界原点）
        self.base_x = 0
        self.base_z = 0

        # 区块缓存: (cx, cz) -> Chunk
        self.chunks: dict = {}
        self._loaded_chunk_count = 0
        self._chunk_order: OrderedDict = OrderedDict()  # LRU（move_to_end O(1)）

        # 全局道具列表（从已加载区块聚合）
        self.props: set = set()
        self.props_by_cell: dict = {}  # (x, z) -> Prop
        self._harvested_cells: set = set()  # 持久化采集状态，区块重生成后不复活
        self.loot_pickups: list = []

        # 天气
        self.weather = WeatherState()
        self.weather_timer = 0.0

        # 刷怪点 / 路径（确定性，seed 决定；spawner_id = 粗格编码）
        self.spawners: dict = {}          # {spawner_id: Spawner}
        self.enemy_paths: dict = {}        # {spawner_id: PathData}（延迟生成，仅激活的有）
        self._path_cells: set = set()      # 预计算的路径格集合 O(1) 查询
        self._materialized_coarse: set = set()  # 已材料化的粗格，防重复
        self._tower_cells: set = set()     # 已放置塔的格子，延迟路径绕行用

        # 材料化基地周边刷怪点（纯噪声，不触发区块生成）
        self._materialize_base_spawners()

        # 生成世界宝箱
        self._scatter_loot()

        # 初始化天气
        self._update_weather()

    # ─── 区块管理 ──────────────────────────────────────────

    def get_chunk(self, cx: int, cz: int) -> Chunk:
        """获取区块，不存在则生成。LRU 淘汰超出上限的旧区块。"""
        key = (cx, cz)
        if key in self.chunks:
            # O(1) 标记为最近访问
            self._chunk_order.move_to_end(key)
            return self.chunks[key]

        # 新块生成前，检查是否需要淘汰
        self._evict_if_needed()

        chunk = Chunk(
            cx=cx, cz=cz,
            seed=self.seed, height=self.height,
            base_temperature=self.base_temperature,
            moisture_base=self.moisture_base,
            base_x=self.base_x, base_z=self.base_z,
        )
        self.chunks[key] = chunk
        self._chunk_order[key] = True
        self._loaded_chunk_count += 1
        # 生成道具
        props = self._scatter_props_for_chunk(cx, cz)
        # 聚合道具到全局列表
        for prop in props:
            cell_key = (prop.x, prop.z)
            if not prop.harvested and cell_key not in self._harvested_cells:
                self.props.add(cell_key)
                self.props_by_cell[cell_key] = prop
        # 材料化该区块覆盖的粗格刷怪点
        self._materialize_chunk_spawners(cx, cz)
        return chunk

    def _evict_if_needed(self):
        """当区块数超过上限时，淘汰最旧的区块"""
        max_chunks = config.MAX_LOADED_CHUNKS
        while len(self.chunks) >= max_chunks and self._chunk_order:
            self._evict_oldest()

    def _evict_oldest(self):
        """淘汰最旧的区块，清理关联道具"""
        key, _ = self._chunk_order.popitem(last=False)
        chunk = self.chunks.pop(key)
        self._loaded_chunk_count -= 1
        # 清理该区块的道具
        for prop in chunk.props:
            cell = (prop.x, prop.z)
            if cell in self.props_by_cell:
                del self.props_by_cell[cell]
                self.props.discard(cell)

    def get_visible_chunks(self, cam_x: float, cam_z: float,
                           distance: int = None) -> list:
        """获取可见区块列表"""
        distance = distance or config.RENDER_DISTANCE
        ccx = math.floor(cam_x) // config.CHUNK_SIZE
        ccz = math.floor(cam_z) // config.CHUNK_SIZE
        result = []
        for dcx in range(-distance, distance + 1):
            for dcz in range(-distance, distance + 1):
                # 圆形裁剪，减少角落区块
                dist_sq = dcx * dcx + dcz * dcz
                if dist_sq <= distance * distance:
                    cx, cz = ccx + dcx, ccz + dcz
                    result.append((cx, cz))
        return result

    def generate_visible_chunks(self, cam_x: float, cam_z: float,
                                 distance: int = None):
        """确保可见区块已生成"""
        for cx, cz in self.get_visible_chunks(cam_x, cam_z, distance):
            self.get_chunk(cx, cz)

    # ─── 全局坐标查询 ──────────────────────────────────────

    def _to_local(self, x: int, z: int):
        """全局坐标 → (cx, cz, lx, lz)"""
        cx = x // config.CHUNK_SIZE
        cz = z // config.CHUNK_SIZE
        lx = x - cx * config.CHUNK_SIZE
        lz = z - cz * config.CHUNK_SIZE
        return cx, cz, lx, lz

    def get_chunk_safe(self, cx: int, cz: int) -> Chunk | None:
        """获取已加载区块，不触发生成。用于面剔除的跨区块邻居查询。"""
        return self.chunks.get((cx, cz))

    def add_tower_cell(self, x: int, z: int) -> None:
        """注册已放置塔的格子（延迟路径生成时绕行用）。"""
        self._tower_cells.add((x, z))

    def get_block(self, x: int, y: int, z: int) -> int:
        cx, cz, lx, lz = self._to_local(x, z)
        chunk = self.get_chunk(cx, cz)
        return chunk.get_block(lx, y, lz)

    def set_block(self, x: int, y: int, z: int, block_id: int):
        cx, cz, lx, lz = self._to_local(x, z)
        chunk = self.get_chunk(cx, cz)
        chunk.set_block(lx, y, lz, block_id)

    def get_surface_height(self, x: int, z: int) -> int:
        cx, cz, lx, lz = self._to_local(x, z)
        chunk = self.get_chunk(cx, cz)
        return chunk.get_surface_height(lx, lz)

    def get_surface_height_smooth(self, x: float, z: float) -> float:
        cx = math.floor(x) // config.CHUNK_SIZE
        cz = math.floor(z) // config.CHUNK_SIZE
        lx = math.floor(x) - cx * config.CHUNK_SIZE
        lz = math.floor(z) - cz * config.CHUNK_SIZE
        chunk = self.get_chunk(cx, cz)
        return chunk.get_surface_height_smooth(lx, lz)

    def get_biome(self, x: int, z: int) -> str:
        cx, cz, lx, lz = self._to_local(x, z)
        chunk = self.get_chunk(cx, cz)
        return chunk.get_biome(lx, lz)

    def get_temperature(self, x: int, z: int) -> float:
        cx, cz, lx, lz = self._to_local(x, z)
        chunk = self.get_chunk(cx, cz)
        return chunk.get_temperature(lx, lz)

    # ─── 安全查询（不触发生成，用于热路径）─────────────────

    def get_block_safe(self, x: int, y: int, z: int) -> int:
        """获取方块，不触发生成。未加载返回 air(0)。"""
        cx, cz, lx, lz = self._to_local(x, z)
        chunk = self.get_chunk_safe(cx, cz)
        if chunk is None:
            return 0
        return chunk.get_block(lx, y, lz)

    def get_surface_height_safe(self, x: int, z: int) -> int:
        """获取表面高度，不触发生成。未加载返回默认值。"""
        cx, cz, lx, lz = self._to_local(x, z)
        chunk = self.get_chunk_safe(cx, cz)
        if chunk is None:
            return 5  # 默认平原高度
        return chunk.get_surface_height(lx, lz)

    def get_surface_height_smooth_safe(self, x: float, z: float) -> float:
        """获取平滑表面高度，不触发生成。"""
        cx = math.floor(x) // config.CHUNK_SIZE
        cz = math.floor(z) // config.CHUNK_SIZE
        lx = math.floor(x) - cx * config.CHUNK_SIZE
        lz = math.floor(z) - cz * config.CHUNK_SIZE
        chunk = self.get_chunk_safe(cx, cz)
        if chunk is None:
            return 5.0
        return chunk.get_surface_height_smooth(lx, lz)

    def get_surface_top_over_footprint(self, x: float, z: float, r: float = 0.5) -> int:
        """返回敌人足迹（以 (x,z) 为中心、边长 ~2r 的方形）覆盖到的所有格
        表面高度的最大值。

        用途：敌人 Y 锚定。取足迹覆盖的最高列顶，可保证敌人底面 (Y-1) 永不
        低于其覆盖的任何实体方块顶部，从而不再切入山体/崖壁（原双线性插值
        在陡崖处会给出低于相邻高列顶的中间值，导致敌人穿入实体方块）。
        未加载区块沿用 get_surface_height_safe 的默认值 5，不触发生成。
        """
        ix0 = math.floor(x - r)
        ix1 = math.floor(x + r)
        iz0 = math.floor(z - r)
        iz1 = math.floor(z + r)
        top = 0
        for ix in range(ix0, ix1 + 1):
            for iz in range(iz0, iz1 + 1):
                h = self.get_surface_height_safe(ix, iz)
                if h > top:
                    top = h
        return top

    def get_biome_safe(self, x: int, z: int) -> str:
        """获取群系，不触发生成。未加载返回 plains。"""
        cx, cz, lx, lz = self._to_local(x, z)
        chunk = self.get_chunk_safe(cx, cz)
        if chunk is None:
            return "plains"
        return chunk.get_biome(lx, lz)

    # ─── 刷怪点 / 路径生成 ──────────────────────────────────

    # ─── 纯噪声地形查询（不加载区块，游戏逻辑与 chunk LRU 脱钩） ───

    def _get_height_with_base_override(self, x: int, z: int) -> float:
        """归一化高度（含大本营区域覆盖），与 chunk 完全一致，不加载区块。

        chunk 在 BASE_RADIUS+3 内强制 h=0.3（平整大本营区域），
        _surface_height_pure / _is_land_pure 必须复用此逻辑。
        """
        h = self._get_height_normalized(x, z)
        dist_to_base = math.sqrt((x - self.base_x) ** 2
                                 + (z - self.base_z) ** 2)
        if dist_to_base < config.BASE_RADIUS + 3:
            return 0.3
        return h

    def _surface_height_pure(self, x: int, z: int) -> int:
        """纯噪声地表高度（与 chunk.get_surface_height 完全一致，不加载区块）。"""
        h = self._get_height_with_base_override(x, z)
        return int(h * self.height)

    def _is_land_pure(self, x: int, z: int) -> bool:
        """纯噪声陆地判定（与 chunk ocean 阈值 h<0.20 一致，不加载区块）。"""
        return self._get_height_with_base_override(x, z) >= 0.20

    # ─── 刷怪点确定性涌现（粗格网） ──────────────────────────

    @staticmethod
    def _spawner_id_for(gx: int, gz: int) -> int:
        """粗格坐标 → 确定性刷怪点 id（稳定、与材料化顺序无关）。"""
        return (gx + 32768) * 65536 + (gz + 32768)

    def _spawner_roll(self, gx: int, gz: int) -> bool:
        """粗格 (gx,gz) 是否产生刷怪点。纯函数，与扫描顺序无关。

        掷骰用独立 Random(hash(seed, gx, gz))，确保每个粗格独立确定。
        命中密度 + 中心格陆地 + 3×3 多数陆地（防 1 格小岛）。
        """
        cell_rng = random.Random(
            zlib.crc32(f"{self.seed}|{gx}|{gz}".encode("utf-8")))
        if cell_rng.random() >= gamedata.DISTRIBUTION_CFG["density"]:
            return False
        # 粗格中心世界坐标
        cx = gx * gamedata.SPAWNER_GRID
        cz = gz * gamedata.SPAWNER_GRID
        if not self._is_land_pure(cx, cz):
            return False
        # 3×3 多数陆地
        land_count = 0
        for dx in (-1, 0, 1):
            for dz in (-1, 0, 1):
                if self._is_land_pure(cx + dx, cz + dz):
                    land_count += 1
        return land_count >= 5

    def _materialize_spawner_at_coarse(self, gx: int, gz: int) -> bool:
        """在粗格 (gx,gz) 材料化刷怪点（若未材料化）。返回是否新增。"""
        sid = self._spawner_id_for(gx, gz)
        if sid in self.spawners:
            return False
        # 粗格中心
        sx = gx * gamedata.SPAWNER_GRID
        sz = gz * gamedata.SPAWNER_GRID
        # 保险：确保落在可站立陆地（中心格已验证，但再保险一次）
        if not self._is_land_pure(sx, sz):
            return False
        surface_y = self._surface_height_pure(sx, sz)
        # biome 从 chunk 取（材料化时 chunk 已加载），若未加载则用 "plains"
        chunk_key = (sx // config.CHUNK_SIZE, sz // config.CHUNK_SIZE)
        chunk = self.chunks.get(chunk_key)
        if chunk:
            lx = sx - chunk_key[0] * config.CHUNK_SIZE
            lz = sz - chunk_key[1] * config.CHUNK_SIZE
            biome = chunk.get_biome(lx, lz)
        else:
            biome = "plains"
        # 刷怪点种类：由生物群系决定（data/spawners.json）
        kind = gamedata.spawner_kind_for(biome)
        self.spawners[sid] = Spawner(
            id=sid, x=sx, z=sz, surface_y=surface_y, biome=biome, kind=kind)
        return True

    def _materialize_base_spawners(self) -> list:
        """开局材料化基地周边刷怪点，返回清醒 id 列表（最近 awake_count 个）。"""
        self.spawners.clear()
        self.enemy_paths.clear()
        self._path_cells.clear()
        self._materialized_coarse.clear()

        dist = gamedata.DISTRIBUTION_CFG
        grid = dist["grid"]
        r = dist["base_materialize_radius"]
        # 基地所在的粗格中心半径（粗格索引）
        bgx = self.base_x // grid
        bgz = self.base_z // grid
        r_gx = int(r / grid) + 1
        for gx in range(bgx - r_gx, bgx + r_gx + 1):
            for gz in range(bgz - r_gx, bgz + r_gx + 1):
                if (gx, gz) in self._materialized_coarse:
                    continue
                if self._spawner_roll(gx, gz):
                    self._materialized_coarse.add((gx, gz))
                    self._materialize_spawner_at_coarse(gx, gz)

        # 按距大本营升序排序，取前 awake_count 个清醒
        sorted_ids = sorted(
            self.spawners.keys(),
            key=lambda sid: (
                self.spawners[sid].x - self.base_x) ** 2
                + (self.spawners[sid].z - self.base_z) ** 2)
        awake = sorted_ids[:dist.get("awake_count", 3)]
        return awake

    def _materialize_chunk_spawners(self, cx: int, cz: int) -> None:
        """chunk 加载时材料化其覆盖的粗格刷怪点（纯噪声，不淘汰）。"""
        grid = gamedata.SPAWNER_GRID
        # chunk 覆盖的世界坐标范围
        x_min = cx * config.CHUNK_SIZE
        z_min = cz * config.CHUNK_SIZE
        x_max = x_min + config.CHUNK_SIZE - 1
        z_max = z_min + config.CHUNK_SIZE - 1
        # 覆盖的粗格范围
        gx_min = x_min // grid
        gx_max = x_max // grid
        gz_min = z_min // grid
        gz_max = z_max // grid
        for gx in range(gx_min, gx_max + 1):
            for gz in range(gz_min, gz_max + 1):
                if (gx, gz) in self._materialized_coarse:
                    continue
                if self._spawner_roll(gx, gz):
                    self._materialized_coarse.add((gx, gz))
                    self._materialize_spawner_at_coarse(gx, gz)

    def _materialize_around_spawner(self, sx: int, sz: int,
                                    radius: int = 90) -> int:
        """材料化以 (sx,sz) 为中心、半径 radius 内的粗格刷怪点。

        侦察激活时调用，让侦察兵有明确目标。返回新增数量。
        """
        grid = gamedata.SPAWNER_GRID
        r_gx = int(radius / grid) + 1
        gx_center = sx // grid
        gz_center = sz // grid
        added = 0
        for gx in range(gx_center - r_gx, gx_center + r_gx + 1):
            for gz in range(gz_center - r_gx, gz_center + r_gx + 1):
                if (gx, gz) in self._materialized_coarse:
                    continue
                if self._spawner_roll(gx, gz):
                    self._materialized_coarse.add((gx, gz))
                    if self._materialize_spawner_at_coarse(gx, gz):
                        added += 1
        return added

    def _ensure_path(self, spawner_id: int) -> None:
        """延迟生成刷怪点→大本营路径（纯噪声，不加载区块；绕开已有塔）。"""
        if spawner_id in self.enemy_paths:
            return
        sp = self.spawners.get(spawner_id)
        if not sp:
            return
        path_points = self._generate_path_to_base(
            sp.x, sp.z, spawner_id,
            self.seed + 555 + spawner_id * 222)
        if not path_points:
            # 兜底：直线路径
            surface_y = self._surface_height_pure(sp.x, sp.z)
            base_y = self._surface_height_pure(self.base_x, self.base_z)
            path_points = [(sp.x + 0.5, surface_y + 1, sp.z + 0.5),
                           (self.base_x + 0.5, base_y + 1, self.base_z + 0.5)]
        self.enemy_paths[spawner_id] = _make_path_data(path_points)
        # 预计算路径格集合
        for px, py, pz in path_points:
            self._path_cells.add((math.floor(px), math.floor(pz)))

    def _is_walkable_pure(self, x: int, z: int) -> bool:
        """纯噪声可通行判定：陆地且无塔（不加载区块）。"""
        if not self._is_land_pure(x, z):
            return False
        return (x, z) not in self._tower_cells

    def _generate_path(self, start_x: int, start_z: int,
                       target_x: int, target_z: int,
                       path_index: int, noise_seed: int) -> list:
        """从起点沿噪声路径走向目标点。

        返回路径点列表 [(x, y, z), ...]，终点追加目标格中心。
        刷怪点→大本营路径与侦察怪→回巢路径共用此游走器。
        """
        path = []
        cx, cz = start_x, start_z

        max_steps = gamedata.DISTRIBUTION_CFG["path_max_steps"]
        steps = 0
        visited = set()

        while steps < max_steps:
            steps += 1

            dx_t = target_x - cx
            dz_t = target_z - cz
            dist_t = (dx_t ** 2 + dz_t ** 2) ** 0.5

            if dist_t < 2:
                break

            dx = dx_t / dist_t
            dz = dz_t / dist_t

            # 噪声偏移（垂直于方向）
            noise_val = value_noise_2d(cx * 0.06, cz * 0.06 + path_index * 10,
                                        noise_seed)
            noise_offset = (noise_val - 0.5) * 5.0
            px = -dz * noise_offset
            pz = dx * noise_offset

            step_x = int(round(dx + px * 0.1))
            step_z = int(round(dz + pz * 0.1))

            new_x = cx + step_x
            new_z = cz + step_z

            # 噪声步落入海洋/塔时，依次尝试：直走 → 左垂直 → 右垂直 → 找陆地
            if not self._is_walkable_pure(new_x, new_z):
                # 1) 直走步
                sx, sz = int(round(dx)), int(round(dz))
                new_x, new_z = cx + sx, cz + sz
                if not self._is_walkable_pure(new_x, new_z):
                    # 2) 左垂直（绕开水域/塔）
                    sx, sz = int(round(-dz)), int(round(dx))
                    new_x, new_z = cx + sx, cz + sz
                    if not self._is_walkable_pure(new_x, new_z):
                        # 3) 右垂直
                        sx, sz = int(round(dz)), int(round(-dx))
                        new_x, new_z = cx + sx, cz + sz
                        if not self._is_walkable_pure(new_x, new_z):
                            # 4) 朝目标方向找陆地（兜底）
                            new_x, new_z = self._find_land_toward(
                                cx, cz, target_x, target_z)

            if (new_x, new_z) in visited:
                new_x = cx + int(round(dx + px * 0.2))
                new_z = cz + int(round(dz + pz * 0.2))
                if not self._is_walkable_pure(new_x, new_z):
                    new_x, new_z = self._find_land_toward(
                        new_x, new_z, target_x, target_z)

            visited.add((new_x, new_z))
            cx, cz = new_x, new_z

            # 世界坐标：方块中心 = 索引 + 0.5（纯噪声高度）
            surface_y = self._surface_height_pure(cx, cz)
            path.append((cx + 0.5, surface_y + 1, cz + 0.5))

        # 确保最后一点是目标格中心（对齐渲染基准）
        if path:
            target_surface_y = self._surface_height_pure(target_x, target_z)
            path.append((target_x + 0.5, target_surface_y + 1, target_z + 0.5))

        return path

    def _generate_path_to_base(self, start_x: int, start_z: int,
                                path_index: int, noise_seed: int) -> list:
        """从起点沿噪声路径走向大本营（_generate_path 的特例）。"""
        return self._generate_path(
            start_x, start_z, self.base_x, self.base_z, path_index, noise_seed)

    def _find_land(self, x: int, z: int) -> tuple:
        """从 (x, z) 搜索最近的可通行格子。"""
        if self._is_walkable_pure(x, z):
            return x, z
        for d in range(1, 15):
            for sign in (1, -1):
                for dx_d, dz_d in [(sign * d, 0), (0, sign * d),
                                    (sign * d, sign * d), (sign * d, -sign * d)]:
                    nx, nz = x + dx_d, z + dz_d
                    if self._is_walkable_pure(nx, nz):
                        return nx, nz
        return x, z

    def _find_land_toward(self, x: int, z: int,
                           tx: int, tz: int) -> tuple:
        """从 (x, z) 搜索最近的非海洋格子，优先朝目标 (tx, tz) 方向。

        用于路径游走：落水后拉回陆地时不反向跳（避免路径振荡卡死）。
        搜索半径 3（walker 每步最多走 1 格，3 足够找邻岸陆地）。
        评分 = 距目标 + 2×距当前（避免跳到远处陆地导致瞬移）。
        """
        if self._is_walkable_pure(x, z):
            return x, z
        best = None
        best_score = float("inf")
        for d in range(1, 4):
            for sign in (1, -1):
                for dx_d, dz_d in [(sign * d, 0), (0, sign * d),
                                    (sign * d, sign * d), (sign * d, -sign * d)]:
                    nx, nz = x + dx_d, z + dz_d
                    if not self._is_walkable_pure(nx, nz):
                        continue
                    dist_target = (nx - tx) ** 2 + (nz - tz) ** 2
                    dist_from_here = (nx - x) ** 2 + (nz - z) ** 2
                    score = dist_target + 2 * dist_from_here
                    if score < best_score:
                        best_score = score
                        best = (nx, nz)
        return best if best else (x, z)

    def _is_on_path(self, x: int, z: int, margin: int = 1) -> bool:
        """检查格子是否在任何路径上（O(1) 预计算集合查询）"""
        if margin <= 1:
            return (x, z) in self._path_cells
        for dx in range(-margin, margin + 1):
            for dz in range(-margin, margin + 1):
                if (x + dx, z + dz) in self._path_cells:
                    return True
        return False

    # ─── 道具生成（按区块） ──────────────────────────────────

    def _scatter_props_for_chunk(self, cx: int, cz: int):
        """为指定区块生成装饰物（使用区块独立 RNG，确保确定性）"""
        from props import PROP_TYPES

        chunk = self.chunks[(cx, cz)]
        props = []
        # 区块独立 RNG：同种子+同坐标→同一组道具，不依赖加载顺序
        chunk_rng = random.Random(self.seed ^ (cx * 7919 + cz * 104729))

        for lx in range(chunk.size):
            for lz in range(chunk.size):
                x = chunk.gx + lx
                z = chunk.gz + lz

                # 跳过海洋（复用区块已存的高度值，不再调 fbm）
                surface_y = chunk.get_surface_height(lx, lz)
                h_norm = surface_y / chunk.height
                if h_norm < 0.20:
                    continue

                # 跳过路径
                if self._is_on_path(x, z):
                    continue

                # 跳过已采集的格子（持久化）
                if (x, z) in self._harvested_cells:
                    continue

                biome = chunk.get_biome(lx, lz)

                # 收集该群系的装饰物类型
                candidates = [pt for pt in PROP_TYPES.values()
                              if biome in pt.biomes]

                for pt in candidates:
                    if chunk_rng.random() < pt.spawn_chance:
                        size_val = chunk_rng.uniform(pt.min_size, pt.max_size)
                        prop = Prop(
                            x=x, z=z, y=surface_y + 1.0,
                            prop_type=pt, size=size_val,
                        )
                        props.append(prop)

        chunk.props = props
        return props

    def _get_height_normalized(self, x: int, z: int) -> float:
        """获取归一化高度值"""
        h_raw = fbm_2d(x * 0.06, z * 0.06, self.seed,
                        octaves=4, persistence=0.5)
        h_raw = h_raw * 1.4 - 0.28
        if h_raw > 0:
            return max(0.0, min(1.0, h_raw ** 0.7))
        return max(0.0, min(1.0, h_raw))

    # ─── 世界宝箱 ──────────────────────────────────────────

    def _scatter_loot(self):
        """生成世界宝箱（在大本营附近区域）"""
        from entities import Pickup

        self.loot_pickups = []

        loot_tables = {
            "forest": [("material", "wood", 3), ("material", "plank", 2),
                       ("weapon", "wood_club", 1)],
            "desert": [("material", "sand", 5), ("material", "iron_ore", 1),
                       ("weapon", "sand_sling", 1)],
            "mountain": [("material", "iron_ore", 2), ("material", "cobble", 3),
                         ("weapon", "stone_axe", 1)],
            "savanna": [("material", "wood", 2), ("material", "sand", 3),
                        ("weapon", "wood_club", 1)],
            "plains": [("material", "wood", 2), ("material", "stone", 2),
                       ("weapon", "wood_club", 1)],
            "tundra": [("material", "stone", 3), ("material", "iron_ore", 1)],
            "snowy": [("material", "iron_ore", 1), ("material", "stone", 2)],
            "ocean": [],
        }

        loot_count = self.rng.randint(10, 18)
        attempts = 0
        while len(self.loot_pickups) < loot_count and attempts < 300:
            attempts += 1
            # 在大本营周围搜索
            angle = self.rng.uniform(0, math.tau)
            dist = self.rng.uniform(10, 45)
            x = math.floor(self.base_x + math.cos(angle) * dist)
            z = math.floor(self.base_z + math.sin(angle) * dist)

            if self.get_biome(x, z) == "ocean":
                continue
            if self._is_on_path(x, z):
                continue

            biome = self.get_biome(x, z)
            drops = loot_tables.get(biome, [])
            if not drops:
                continue

            drop = self.rng.choice(drops)
            surface_y = self.get_surface_height(x, z)
            pickup = Pickup(
                position=(x + 0.5, surface_y + 0.5, z + 0.5),
                item_type=drop[0],
                item_name=drop[1],
                amount=drop[2],
                rng=self.rng,
            )
            self.loot_pickups.append(pickup)

    # ─── 天气 ──────────────────────────────────────────────

    def _update_weather(self, dt=1.0/60):
        self.weather_timer += dt
        if self.weather_timer >= 60.0:
            self.weather_timer = 0

            # 基于大本营附近的生物群系决定天气
            biome = self.get_biome(self.base_x, self.base_z)
            biome_def = BIOMES.get(biome, BIOMES["plains"])

            if self.rng.random() < biome_def.rain_chance:
                self.weather.is_raining = True
                self.weather.intensity = self.rng.uniform(0.3, 1.0)
            else:
                self.weather.is_raining = False
                self.weather.intensity = 0.0

            if self.rng.random() < biome_def.snow_chance:
                self.weather.is_snowing = True
            else:
                self.weather.is_snowing = False

            self.weather.wind_direction = self.rng.uniform(0, math.pi * 2)
            self.weather.wind_strength = self.rng.uniform(0.1, 0.8)

    def update_weather(self, dt=1.0/60):
        """每帧调用"""
        self._update_weather(dt)

    # ─── 采集后清理 ────────────────────────────────────────

    def remove_prop(self, prop):
        """从全局列表中移除已采集的道具"""
        cell = (prop.x, prop.z)
        self._harvested_cells.add(cell)  # 持久化：区块重生成后不复活
        if cell in self.props_by_cell:
            del self.props_by_cell[cell]
        self.props.discard(cell)
        # 也从区块中移除
        cx = prop.x // config.CHUNK_SIZE
        cz = prop.z // config.CHUNK_SIZE
        if (cx, cz) in self.chunks:
            chunk = self.chunks[(cx, cz)]
            if prop in chunk.props:
                chunk.props.remove(prop)

    def __repr__(self):
        return (f"Universe(seed={self.seed}, infinite, "
                f"chunks={len(self.chunks)}, base=({self.base_x},{self.base_z}))")


# ─── 辅助：打印地形概览 ──────────────────────────────────────

def print_terrain_overview(universe: Universe):
    """在控制台打印地形概览（调试用）"""
    print(f"\n=== 宇宙概览 (种子: {universe.seed}) ===")
    print(f"无限地形, 区块大小: {config.CHUNK_SIZE}x{config.CHUNK_SIZE}")
    print(f"高度: {universe.height}, 基准温度: {universe.base_temperature:.1f}°C")
    print(f"大本营: ({universe.base_x}, {universe.base_z})")
    print(f"已加载区块: {len(universe.chunks)}")
    print(f"刷怪点: {len(universe.spawners)} 个（材料化）")
    for sid in sorted(universe.spawners.keys(),
                       key=lambda s: (
                           universe.spawners[s].x - universe.base_x) ** 2
                           + (universe.spawners[s].z - universe.base_z) ** 2):
        sp = universe.spawners[sid]
        d = math.hypot(sp.x - universe.base_x, sp.z - universe.base_z)
        path_info = ""
        if sid in universe.enemy_paths:
            path_info = f" 路径 {len(universe.enemy_paths[sid].points)} 格"
        print(f"  spawner[{sid}]: ({sp.x},{sp.z}) 距基地 {d:.1f} "
              f"biome={sp.biome}{path_info}")
    print(f"总道具数: {len(universe.props)}")
    print(f"世界宝箱: {len(universe.loot_pickups)}")