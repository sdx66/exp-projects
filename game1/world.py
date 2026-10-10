"""
world.py - 种子驱动的无限宇宙生成
区块制地形：按需生成、缓存、确定性。
大本营概念：敌人从刷怪点沿路径走向大本营。
"""

import random
import math
from collections import OrderedDict
from dataclasses import dataclass, field

import config
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

        # 路径
        self.enemy_paths: list = []
        self._path_cells: set = set()  # 预计算的路径格集合 O(1) 查询
        self.spawn_points: list = []

        # 生成路径（触发相关区块生成）
        self._generate_paths()

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

    def get_biome_safe(self, x: int, z: int) -> str:
        """获取群系，不触发生成。未加载返回 plains。"""
        cx, cz, lx, lz = self._to_local(x, z)
        chunk = self.get_chunk_safe(cx, cz)
        if chunk is None:
            return "plains"
        return chunk.get_biome(lx, lz)

    # ─── 路径生成 ──────────────────────────────────────────

    def _generate_paths(self):
        """生成从刷怪点到大本营的路径"""
        self.enemy_paths = []
        self._path_cells = set()

        # 5 个刷怪点，均匀分布在大本营周围
        num_paths = 5
        for i in range(num_paths):
            angle = i * (2 * math.pi / num_paths)
            dist = self.rng.uniform(
                config.ENEMY_SPAWN_DISTANCE_MIN,
                config.ENEMY_SPAWN_DISTANCE_MAX)
            spawn_x = math.floor(self.base_x + math.cos(angle) * dist)
            spawn_z = math.floor(self.base_z + math.sin(angle) * dist)

            # 确保刷怪点不在海洋
            spawn_x, spawn_z = self._find_land(spawn_x, spawn_z)

            path_points = self._generate_path_to_base(
                spawn_x, spawn_z, i, self.seed + 555 + i * 222)
            if path_points:
                self.enemy_paths.append(_make_path_data(path_points))
                self.spawn_points.append(path_points[0])
                # 预计算路径格集合（世界坐标 → 方块索引，_is_on_path 按整数查询）
                for px, py, pz in path_points:
                    self._path_cells.add((math.floor(px), math.floor(pz)))

    def _generate_path_to_base(self, start_x: int, start_z: int,
                                path_index: int, noise_seed: int) -> list:
        """
        从起点沿噪声路径走向大本营。
        返回路径点列表 [(x, y, z), ...]
        """
        path = []
        cx, cz = start_x, start_z
        base_x, base_z = self.base_x, self.base_z

        max_steps = 120  # 安全上限
        steps = 0
        visited = set()

        while steps < max_steps:
            steps += 1

            # 检查是否到达大本营
            dx_base = base_x - cx
            dz_base = base_z - cz
            dist_base = (dx_base ** 2 + dz_base ** 2) ** 0.5

            if dist_base < 2:
                break

            # 归一化方向
            dx = dx_base / dist_base
            dz = dz_base / dist_base

            # 噪声偏移（垂直于方向）
            noise_val = value_noise_2d(cx * 0.06, cz * 0.06 + path_index * 10,
                                        noise_seed)
            noise_offset = (noise_val - 0.5) * 5.0
            # 垂直方向
            px = -dz * noise_offset
            pz = dx * noise_offset

            # 计算下一步
            step_x = int(round(dx + px * 0.1))
            step_z = int(round(dz + pz * 0.1))

            new_x = cx + step_x
            new_z = cz + step_z

            # 避免重复访问
            if (new_x, new_z) in visited:
                # 尝试稍微偏转
                new_x = cx + int(round(dx + px * 0.2))
                new_z = cz + int(round(dz + pz * 0.2))

            visited.add((new_x, new_z))
            cx, cz = new_x, new_z

            # 检查是否在海洋，如果是则找陆地
            cx, cz = self._find_land(cx, cz)

            # 记录路径点（世界坐标：方块中心 = 索引 + 0.5，与大本营/路面的绘制基准一致）
            surface_y = self.get_surface_height(cx, cz)
            path.append((cx + 0.5, surface_y + 1, cz + 0.5))

        # 确保最后一点是大本营中心（对齐 _render_base_3d 的 base_x + 0.5，
        # 否则敌人终点落在方块角点、比大本营中心偏 0.707）
        if path:
            base_surface_y = self.get_surface_height(base_x, base_z)
            path.append((base_x + 0.5, base_surface_y + 1, base_z + 0.5))

        return path

    def _find_land(self, x: int, z: int) -> tuple:
        """
        从 (x, z) 搜索最近的非海洋格子。
        支持无限坐标。
        """
        if self.get_biome(x, z) != "ocean":
            return x, z
        for d in range(1, 15):
            for sign in (1, -1):
                for dx_d, dz_d in [(sign * d, 0), (0, sign * d),
                                    (sign * d, sign * d), (sign * d, -sign * d)]:
                    nx, nz = x + dx_d, z + dz_d
                    if self.get_biome(nx, nz) != "ocean":
                        return nx, nz
        return x, z

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
    print(f"路径数: {len(universe.enemy_paths)}")
    for i, p in enumerate(universe.enemy_paths):
        print(f"  路径{i+1}: {len(p.points)} 格, 起点 {p.points[0]}, 坡度 {p.avg_slope:.2f}")
    print(f"总道具数: {len(universe.props)}")
    print(f"世界宝箱: {len(universe.loot_pickups)}")