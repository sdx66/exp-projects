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
from chunk import Chunk, base_center_height, natural_height
from noise import fbm_levels_2d
from blocks import BLOCK_ID, BLOCK_NAME, get_block, is_solid, is_transparent
from props import Prop


# ─── 路径记录 ──────────────────────────────────────────────

@dataclass(frozen=True)
class PathData:
    """敌人路径及预计算元数据。

    points: 世界坐标折线 (x, y, z)，已平滑；dig_cells: 沿线需要挖掘才能
    通过的格（相邻点爬升超过该类型爬坡上限，A* 已折算挖方时间代价）。
    """
    points: tuple
    length: int
    avg_slope: float
    dig_cells: frozenset = frozenset()


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


def _make_path_data(points: list, dig_cells=frozenset()) -> PathData:
    """预计算路径坡度。"""
    if not points:
        return PathData((), 0, 0.0)
    slope_sum = 0
    for i in range(len(points) - 1):
        slope_sum += abs(points[i + 1][1] - points[i][1])
    return PathData(tuple(points), len(points), slope_sum / max(1, len(points) - 1),
                    frozenset(dig_cells))


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
        self.max_chunks = config.MAX_LOADED_CHUNKS
        self._loaded_chunk_count = 0
        self._chunk_order: OrderedDict = OrderedDict()  # LRU（move_to_end O(1)）

        # 全局道具列表（从已加载区块聚合）
        self.props: set = set()
        self.props_by_cell: dict = {}  # (x, z) -> Prop
        self._harvested_cells: set = set()  # 持久化采集状态，区块重生成后不复活
        self.loot_pickups: list = []
        self._height_cache: dict = {}
        self._height_cache_max = 200000

        # 天气
        self.weather = WeatherState()
        self.weather_timer = 0.0

        # 世界版本号：塔/方块增减时 +1，敌人据此检测"路线可能已改变"
        self.world_version = 0

        # 地形编辑覆盖层（柱面模型：挖掘=削低目标柱地表，不做 3D 隧道）
        self.block_edits: dict = {}       # (x, y, z) -> block_id（挖穿=air）
        self.surface_override: dict = {}  # (x, z) -> 挖掘后的地表 y
        self._edits_by_chunk: dict = {}   # (cx, cz) -> set[(x, y, z)]，区块重生成后重放
        self.dirty_chunks: set = set()    # 待渲染重建的区块（game 每帧转给 renderer）
        self._biome_cache: dict = {}      # (x, z) -> (biome, surface_block) 纯噪声缓存
        self._biome_cache_max = 200000

        # 刷怪点 / 路径（确定性，seed 决定；spawner_id = 粗格编码）
        self.spawners: dict = {}          # {spawner_id: Spawner}
        self.enemy_paths: dict = {}        # {spawner_id: PathData}（延迟生成，仅激活的有）
        self._path_cells: set = set()      # 预计算的路径格集合 O(1) 查询
        self._materialized_coarse: set = set()  # 已材料化的粗格，防重复
        self._tower_cells: set = set()     # 已放置塔的格子，延迟路径绕行用
        self._wall_cells: set = set()     # 防御塔(城墙)格，寻路近乎不可通行

        # 材料化基地周边刷怪点（纯噪声，不触发区块生成）
        awake_ids = self._materialize_base_spawners()
        # 关键顺序：先为清醒刷怪点生成路径，再散布道具/宝箱，
        # 否则 _path_cells 为空，_is_on_path 检查失效，道具会刷在敌人路线上。
        for sid in awake_ids:
            self._ensure_path(sid)
        self._awake_ids = awake_ids

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
        # 重放该区块的地形编辑（挖掘痕迹在区块 LRU 淘汰重生成后不丢失）
        self._replay_chunk_edits(chunk)
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

    def set_chunk_limit(self, limit: int):
        """调整常驻区块上限；用于按当前可见区放大旋转工作集。"""
        limit = max(1, int(limit))
        if limit == self.max_chunks:
            return
        self.max_chunks = limit
        while len(self.chunks) > self.max_chunks and self._chunk_order:
            self._evict_oldest()

    def _evict_if_needed(self):
        """当区块数超过上限时，淘汰最旧的区块"""
        while len(self.chunks) >= self.max_chunks and self._chunk_order:
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

    def add_tower_cell(self, x: int, z: int, is_wall: bool = False) -> None:
        """注册已放置塔的格子（路线阻塞源），并推进世界版本。

        is_wall=True 表示防御塔（城墙类）：除进入塔格集合外，还登记为墙格，
        寻路时给予近乎不可通行的巨额代价（连续横竖/对角成墙即无法从缝隙穿过）。
        """
        if (x, z) not in self._tower_cells:
            self._tower_cells.add((x, z))
            self.world_version += 1
        if is_wall and (x, z) not in self._wall_cells:
            self._wall_cells.add((x, z))

    def remove_tower_cell(self, x: int, z: int) -> None:
        """注销塔格（拆除/被摧毁），并推进世界版本。"""
        changed = (x, z) in self._tower_cells or (x, z) in self._wall_cells
        if (x, z) in self._tower_cells:
            self._tower_cells.discard((x, z))
        if (x, z) in self._wall_cells:
            self._wall_cells.discard((x, z))
        if changed:
            self.world_version += 1

    # ─── 地形编辑覆盖层（柱面挖掘模型） ────────────────────

    def _replay_chunk_edits(self, chunk: Chunk) -> None:
        """区块生成/重生成后，重放落入其范围的挖掘/放置编辑。"""
        # _edits_by_chunk 以区块索引 (cx, cz) 为键（见 dig_surface/break_block/
        # place_block），故这里用 chunk.cx/chunk.cz 而非世界原点 gx/gz 查询。
        edits = self._edits_by_chunk.get((chunk.cx, chunk.cz))
        if not edits:
            return
        for (x, y, z) in edits:
            # chunk.gx/gz 已是世界原点（=cx*size），本地坐标 = 世界坐标 - 原点
            lx = x - chunk.gx
            lz = z - chunk.gz
            if 0 <= lx < chunk.size and 0 <= lz < chunk.size:
                chunk.set_block(lx, y, lz, self.block_edits[(x, y, z)])
        # 该列若有覆盖层编辑，列顶以 surface_override 为权威（挖低或垫高都适用；
        # 同列多次编辑/挖后再放也一致）。区块重生成后渲染锚定才不会回退。
        for (x, z), ov in self.surface_override.items():
            lx = x - chunk.gx
            lz = z - chunk.gz
            if 0 <= lx < chunk.size and 0 <= lz < chunk.size:
                chunk.surface_height[lx][lz] = ov

    def surface_height_at(self, x: int, z: int) -> int:
        """统一地表查询入口：挖掘覆盖层优先，否则纯噪声高度。

        游戏逻辑（敌人 Y 锚定、寻路、挖控行为）必须走这里，
        不能直接用 _surface_height_pure / chunk.get_surface_height。
        """
        # 覆盖层为空（从未挖掘）时跳过逐格 tuple 构造与 dict 查找
        if self.surface_override:
            ov = self.surface_override.get((x, z))
            if ov is not None:
                return ov
        return self._surface_height_pure(x, z)

    def surface_height_footprint(self, x: float, z: float, r: float = 0.2) -> int:
        """足迹覆盖列的地表最大值（覆盖层感知），供敌人 Y 锚定。"""
        ix0 = math.floor(x - r)
        ix1 = math.floor(x + r)
        iz0 = math.floor(z - r)
        iz1 = math.floor(z + r)
        top = 0
        for ix in range(ix0, ix1 + 1):
            for iz in range(iz0, iz1 + 1):
                h = self.surface_height_at(ix, iz)
                if h > top:
                    top = h
        return top

    def _mark_chunks_dirty(self, x: int, z: int) -> None:
        """标脏受影响区块（列在边界时邻块的面剔除也受影响）。"""
        cs = config.CHUNK_SIZE
        cx = x // cs
        cz = z // cs
        dirty = {(cx, cz)}
        lx = x - cx * cs
        lz = z - cz * cs
        if lx == 0:
            dirty.add((cx - 1, cz))
        elif lx == cs - 1:
            dirty.add((cx + 1, cz))
        if lz == 0:
            dirty.add((cx, cz - 1))
        elif lz == cs - 1:
            dirty.add((cx, cz + 1))
        if len(dirty) > 1:
            for dx in (-1, 1):
                for dz in (-1, 1):
                    if ((dx < 0 and lx == 0) or (dx > 0 and lx == cs - 1)) \
                            and ((dz < 0 and lz == 0) or (dz > 0 and lz == cs - 1)):
                        dirty.add((cx + dx, cz + dz))
        self.dirty_chunks.update(dirty)

    def dig_front_block(self, x: int, y: int, z: int) -> bool:
        """削掉 (x, z) 柱上高度 y 的方块（任意高度，敌人掘进自下而上用）。

        柱面模型：只削方块、不做隧道；海洋柱不挖（不模拟水位）；基岩不挖。
        若削到的是当前列顶（y == surface_height_at），则向下扫描求新的最高
        实体方块作为列顶并更新 surface_override；内部掏洞不改列顶。
        不在此处推进 world_version：挖掘逐格连续（一次会话几十格），逐格
        bump 会让全体变体缓存失效、小地图重烘焙，敌人路线反复横跳；会话
        结束由调用方（entities._update_dig）统一 bump_world_version。
        渲染仍逐格标脏 dirty_chunks，视觉实时。
        """
        if not self._is_land_pure(x, z):
            return False
        if y < 2:
            return False  # 基岩不可挖
        if self.get_block(x, y, z) in (BLOCK_ID["air"], BLOCK_ID["water"],
                                       BLOCK_ID["lava"], BLOCK_ID["bedrock"]):
            return False
        self.block_edits[(x, y, z)] = BLOCK_ID["air"]
        cs = config.CHUNK_SIZE
        cx = x // cs
        cz = z // cs
        self._edits_by_chunk.setdefault((cx, cz), set()).add((x, y, z))
        chunk = self.chunks.get((cx, cz))
        if chunk is not None:
            lx = x - cx * cs
            lz = z - cz * cs
            chunk.set_block(lx, y, lz, BLOCK_ID["air"])
        # 削到当前列顶：重算列顶（跳过已空/液体，找新的最高实体方块）。
        # 内部掏洞（y < 列顶）不影响列顶，surface_override 保持权威。
        if y == self.surface_height_at(x, z):
            new_y = y - 1
            while new_y >= 2 and self.get_block(x, new_y, z) in (
                    BLOCK_ID["air"], BLOCK_ID["water"], BLOCK_ID["lava"]):
                new_y -= 1
            if new_y < 2:
                new_y = 2  # 基岩面兜底（基岩不可挖，列顶不低于 2）
            self.surface_override[(x, z)] = new_y
            if chunk is not None:
                lx = x - cx * cs
                lz = z - cz * cs
                if chunk.surface_height[lx][lz] >= y:
                    chunk.surface_height[lx][lz] = new_y
        self._mark_chunks_dirty(x, z)
        return True

    def dig_surface(self, x: int, z: int) -> bool:
        """挖掉 (x, z) 柱的地表方块（削低 1 格）。返回是否实际挖掘。

        柱面模型：只削地表、不做隧道；海洋柱不挖（不模拟水位）。
        不在此处推进 world_version：挖掘是逐格连续的（一次会话几十格），
        逐格 bump 会让全体变体缓存失效、小地图重烘焙，敌人路线反复横跳。
        会话结束由调用方（entities._update_dig）统一 bump_world_version。
        渲染仍逐格标脏 dirty_chunks，视觉实时。

        现为 dig_front_block 的列顶薄封装（削当前列顶那一块 = 等价削顶 1 格）。
        """
        return self.dig_front_block(x, self.surface_height_at(x, z), z)

    def mark_passage(self, x: int, z: int, y: int) -> None:
        """把 (x, z) 列的通行高度标记为 y（打通通道用）。

        与 dig_front_block 挖掉 Front/Above 配合：敌人"开洞"后，该列实体方块
        可能仍悬在 y+2 以上（拱门视觉），但通行/锚定按 y 处理。写 surface_override
        并标脏 chunk 触发 mesh 重建（暴露面由 6 面邻接掩码自动呈现）。
        不 bump world_version：会话结束由调用方统一 bump。
        """
        self.surface_override[(x, z)] = y
        cs = config.CHUNK_SIZE
        cx = x // cs
        cz = z // cs
        chunk = self.chunks.get((cx, cz))
        if chunk is not None:
            lx = x - cx * cs
            lz = z - cz * cs
            if chunk.surface_height[lx][lz] > y:
                chunk.surface_height[lx][lz] = y
        self._mark_chunks_dirty(x, z)

    # ─── 玩家挖/放方块 API（柱面模型：作用于列顶） ──────────

    def top_block_id(self, x: int, z: int) -> int:
        """列顶方块 ID（覆盖层感知，点击处区块必然已加载）。"""
        y = self.surface_height_at(x, z)
        return self.get_block(x, y, z)

    def break_block(self, x: int, z: int) -> int:
        """玩家挖掉 (x, z) 列顶方块（削低 1 格）。返回被挖掉的方块 ID，
        0 表示未挖（海洋/基岩/不可挖方块）。

        与 dig_surface 同一柱面模型，但返回方块 ID 供上层映射材料；
        世界版本由调用方批量 bump（见 game._try_dig_block）。
        """
        if not self._is_land_pure(x, z):
            return 0
        cur = self.surface_height_at(x, z)
        if cur <= 1:
            return 0  # 基岩不可挖
        top_id = self.get_block(x, cur, z)
        if top_id in (BLOCK_ID["air"], BLOCK_ID["water"],
                      BLOCK_ID["lava"], BLOCK_ID["bedrock"]):
            return 0
        new_y = cur - 1
        self.surface_override[(x, z)] = new_y
        self.block_edits[(x, cur, z)] = BLOCK_ID["air"]
        cs = config.CHUNK_SIZE
        cx = x // cs
        cz = z // cs
        self._edits_by_chunk.setdefault((cx, cz), set()).add((x, cur, z))
        chunk = self.chunks.get((cx, cz))
        if chunk is not None:
            lx = x - cx * cs
            lz = z - cz * cs
            chunk.set_block(lx, cur, lz, BLOCK_ID["air"])
            if chunk.surface_height[lx][lz] >= cur:
                chunk.surface_height[lx][lz] = new_y
        self._mark_chunks_dirty(x, z)
        return top_id

    def place_block(self, x: int, z: int, block_id: int) -> bool:
        """玩家在 (x, z) 列顶放置方块（垫高 1 格）。返回是否成功。

        允许放在挖低后的柱上继续堆（列顶=surface_override，权威）。
        限高：不超过该列自然地表 + MAX_BLOCK_STACK，且不超世界高度。
        世界版本由调用方批量 bump。
        """
        if not self._is_land_pure(x, z):
            return False
        cur = self.surface_height_at(x, z)
        new_y = cur + 1
        if new_y >= self.height:
            return False
        natural = self._surface_height_pure(x, z)
        if new_y > natural + config.MAX_BLOCK_STACK:
            return False
        self.surface_override[(x, z)] = new_y
        self.block_edits[(x, new_y, z)] = block_id
        cs = config.CHUNK_SIZE
        cx = x // cs
        cz = z // cs
        self._edits_by_chunk.setdefault((cx, cz), set()).add((x, new_y, z))
        chunk = self.get_chunk(cx, cz)
        lx = x - cx * cs
        lz = z - cz * cs
        chunk.set_block(lx, new_y, lz, block_id)
        chunk.surface_height[lx][lz] = new_y
        self._mark_chunks_dirty(x, z)
        return True

    def bump_world_version(self) -> None:
        """推进世界版本（批量地形变更如挖掘会话结束后调用一次）。

        使寻路变体与敌人路线惰性重估、小地图重烘焙。
        """
        self.world_version += 1

    def get_block(self, x: int, y: int, z: int) -> int:
        cs = config.CHUNK_SIZE
        cx = x // cs
        cz = z // cs
        chunk = self.get_chunk(cx, cz)
        return chunk.get_block(x - cx * cs, y, z - cz * cs)

    def set_block(self, x: int, y: int, z: int, block_id: int):
        cx, cz, lx, lz = self._to_local(x, z)
        chunk = self.get_chunk(cx, cz)
        chunk.set_block(lx, y, lz, block_id)

    def get_surface_height(self, x: int, z: int) -> int:
        cs = config.CHUNK_SIZE
        cx = x // cs
        cz = z // cs
        chunk = self.get_chunk(cx, cz)
        return chunk.get_surface_height(x - cx * cs, z - cz * cs)

    def get_surface_height_smooth(self, x: float, z: float) -> float:
        cx = math.floor(x) // config.CHUNK_SIZE
        cz = math.floor(z) // config.CHUNK_SIZE
        lx = math.floor(x) - cx * config.CHUNK_SIZE
        lz = math.floor(z) - cz * config.CHUNK_SIZE
        chunk = self.get_chunk(cx, cz)
        return chunk.get_surface_height_smooth(lx, lz)

    def get_biome(self, x: int, z: int) -> str:
        cs = config.CHUNK_SIZE
        cx = x // cs
        cz = z // cs
        chunk = self.get_chunk(cx, cz)
        return chunk.get_biome(x - cx * cs, z - cz * cs)

    def get_temperature(self, x: int, z: int) -> float:
        cs = config.CHUNK_SIZE
        cx = x // cs
        cz = z // cs
        chunk = self.get_chunk(cx, cz)
        return chunk.get_temperature(x - cx * cs, z - cz * cs)

    # ─── 安全查询（不触发生成，用于热路径）─────────────────

    def get_block_safe(self, x: int, y: int, z: int) -> int:
        """获取方块，不触发生成。未加载返回 air(0)。"""
        cs = config.CHUNK_SIZE
        cx = x // cs
        cz = z // cs
        chunk = self.chunks.get((cx, cz))
        if chunk is None:
            return 0
        return chunk.get_block(x - cx * cs, y, z - cz * cs)

    def get_surface_height_safe(self, x: int, z: int) -> int:
        """获取表面高度，不触发生成。未加载返回默认值。"""
        cs = config.CHUNK_SIZE
        cx = x // cs
        cz = z // cs
        chunk = self.chunks.get((cx, cz))
        if chunk is None:
            return 5  # 默认平原高度
        return chunk.get_surface_height(x - cx * cs, z - cz * cs)

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

    def get_surface_top_over_footprint_pure(self, x: float, z: float,
                                            r: float = 0.5) -> int:
        """足迹覆盖列的地表最大值（纯噪声 + 挖掘覆盖层，不加载区块）。

        与 get_surface_top_over_footprint 语义一致，但用 surface_height_at
        （surface_override 优先 + _surface_height_pure），不存在"未加载区块
        返回默认 5"的退化；供相机眼位避让使用 —— 开局时地形区块尚未生成
        也能得到精确高度，避免相机埋进山体。
        """
        ix0 = math.floor(x - r)
        ix1 = math.floor(x + r)
        iz0 = math.floor(z - r)
        iz1 = math.floor(z + r)
        top = 0
        for ix in range(ix0, ix1 + 1):
            for iz in range(iz0, iz1 + 1):
                h = self.surface_height_at(ix, iz)
                if h > top:
                    top = h
        return top

    def get_biome_safe(self, x: int, z: int) -> str:
        """获取群系，不触发生成。未加载返回 plains。"""
        cs = config.CHUNK_SIZE
        cx = x // cs
        cz = z // cs
        chunk = self.chunks.get((cx, cz))
        if chunk is None:
            return "plains"
        return chunk.get_biome(x - cx * cs, z - cz * cs)

    # ─── 刷怪点 / 路径生成 ──────────────────────────────────

    # ─── 纯噪声地形查询（不加载区块，游戏逻辑与 chunk LRU 脱钩） ───

    def _get_height_with_base_override(self, x: int, z: int) -> float:
        """归一化高度（含大本营区域平整），与 chunk 完全一致，不加载区块。

        chunk 在 BASE_RADIUS+3 内取"周围自然高度的平均值"填满该区域
        （不强制海拔、不设下限），_surface_height_pure / _is_land_pure
        必须复用此逻辑。
        """
        key = (x, z)
        cached = self._height_cache.get(key)
        if cached is not None:
            return cached
        dist_to_base = math.sqrt((x - self.base_x) ** 2
                                 + (z - self.base_z) ** 2)
        if dist_to_base < config.BASE_RADIUS + 3:
            h = base_center_height(self.base_x, self.base_z, self.seed)
        else:
            h = natural_height(x, z, self.seed)
        self._height_cache[key] = h
        if len(self._height_cache) > self._height_cache_max:
            self._height_cache.pop(next(iter(self._height_cache)))
        return h

    def _surface_height_pure(self, x: int, z: int) -> int:
        """纯噪声地表高度（与 chunk.get_surface_height 完全一致，不加载区块）。"""
        h = self._get_height_with_base_override(x, z)
        return int(h * self.height)

    def _is_land_pure(self, x: int, z: int) -> bool:
        """纯噪声陆地判定（与 chunk ocean 阈值 h<0.20 一致，不加载区块）。"""
        return self._get_height_with_base_override(x, z) >= 0.20

    def _biome_pure(self, x: int, z: int) -> str:
        """纯噪声群系判定（镜像 chunk._generate 的掩码链，不加载区块）。

        后写的掩码覆盖先写的（森林可覆盖雪山/山地），逐级 if 复刻该语义。
        """
        key = (x, z)
        cached = self._biome_cache.get(key)
        if cached is not None:
            return cached[0]
        biome, _ = self._biome_and_block_pure(x, z)
        return biome

    def _surface_block_pure(self, x: int, z: int) -> str:
        """纯噪声表面方块名（草/沙/雪/石…，含雪盖与低地沙规则）。"""
        key = (x, z)
        cached = self._biome_cache.get(key)
        if cached is not None:
            return cached[1]
        _, block = self._biome_and_block_pure(x, z)
        return block

    def _biome_and_block_pure(self, x: int, z: int) -> tuple:
        """一次算出 (群系, 表面方块名) 并写入缓存。"""
        key = (x, z)
        h = self._get_height_with_base_override(x, z)
        altitude = h * self.height
        temp = self.base_temperature - altitude * config.TEMPERATURE_LAPSE_RATE
        m_cfg = config.NOISE_CFG["moisture"]
        moisture = fbm_levels_2d(x, z, self.seed + m_cfg.get("base_seed", 0),
                                 m_cfg["levels"])
        # 基地范围内湿度同样被填平（与 chunk._generate 一致）
        if math.hypot(x - self.base_x, z - self.base_z) < config.BASE_RADIUS + 3:
            moisture = fbm_levels_2d(self.base_x, self.base_z,
                                     self.seed + m_cfg.get("base_seed", 0),
                                     m_cfg["levels"])
        # 群系：与 chunk._generate 掩码覆盖顺序一致（后者覆盖前者）
        biome = None
        if h < 0.004:
            biome = "ocean"
        if temp < -10.0 and h >= 0.18:
            biome = "snowy"
        if -10.0 <= temp < 3.0 and h >= 0.18:
            biome = "tundra"
        if altitude > config.MOUNTAIN_ALT_RATIO * self.height and h >= 0.18:
            biome = "mountain"
        if temp > 25.0 and moisture < 0.3 and h >= 0.18:
            biome = "desert"
        if temp > 20.0 and moisture >= 0.3 and h >= 0.18:
            biome = "savanna"
        if temp < 15.0 and moisture > 0.5 and h >= 0.18:
            biome = "forest"
        if biome is None:
            biome = "plains"
        # 表面方块：海洋柱为沙；其余按群系映射 + 雪盖 + 低地草→沙
        if h < 0.005:
            block = "sand"
        else:
            biome_def = BIOMES.get(biome, BIOMES["plains"])
            block = BLOCK_NAME.get(biome_def.surface_block, "dirt")
            surface_y = int(altitude)
            if (surface_y > config.SNOW_SURFACE_RATIO * self.height
                    and temp < 2.0):
                block = "snow"
            if h < 0.25 and block == "grass":
                block = "sand"
        if len(self._biome_cache) >= self._biome_cache_max:
            self._biome_cache.pop(next(iter(self._biome_cache)))
        self._biome_cache[key] = (biome, block)
        return biome, block

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
        # 排除大本营所在粗格：基地恒为平整陆地，否则会生成退化路径的
        # 刷怪点在 (0,0)，敌人出生即抵达营地。
        if math.hypot(cx - self.base_x, cz - self.base_z) < gamedata.SPAWNER_GRID:
            return False
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
        # 保险：排除大本营所在粗格（防绕过 _spawner_roll 直接材料化）
        if math.hypot(sx - self.base_x, sz - self.base_z) < gamedata.SPAWNER_GRID:
            return False
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
            ix = math.floor(px)
            iz = math.floor(pz)
            self._path_cells.add((ix, iz))
            for dx in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    self._surface_height_pure(ix + dx, iz + dz)

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

            # 噪声偏移（垂直于方向）；多级噪声参数见 config.NOISE_CFG["path"]
            p_cfg = config.NOISE_CFG["path"]
            noise_val = fbm_levels_2d(cx * 0.06, cz * 0.06 + path_index * 10,
                                       noise_seed, p_cfg["levels"])
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

        # 按群系预筛候选道具（每个区块只算一次，避免 256 格重复遍历 PROP_TYPES）。
        # 顺序与逐格 [pt for pt in PROP_TYPES.values() if biome in pt.biomes]
        # 完全一致，RNG 调用序列不变。
        candidates_by_biome = {}
        for biome_name in BIOMES:
            candidates_by_biome[biome_name] = [
                pt for pt in PROP_TYPES.values() if biome_name in pt.biomes]

        surface = chunk.surface_height
        biomes = chunk.biomes
        path_cells = self._path_cells
        harvested_cells = self._harvested_cells
        chunk_height = chunk.height
        gx = chunk.gx
        gz = chunk.gz

        for lx in range(chunk.size):
            srow = surface[lx]
            brow = biomes[lx]
            x = gx + lx
            for lz in range(chunk.size):
                surface_y = srow[lz]
                h_norm = surface_y / chunk_height
                if h_norm < 0.20:
                    continue

                z = gz + lz
                # 跳过路径
                if (x, z) in path_cells:
                    continue

                # 跳过已采集的格子（持久化）
                if (x, z) in harvested_cells:
                    continue

                biome = brow[lz]
                candidates = candidates_by_biome.get(biome)
                if candidates is None:
                    candidates = [pt for pt in PROP_TYPES.values()
                                  if biome in pt.biomes]
                    candidates_by_biome[biome] = candidates

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
        """获取归一化自然高度 [0, 1]（无大本营平整）。

        实现见 chunk.natural_height —— chunk 表面与纯噪声查询共用同一公式，
        刷怪点与路径的 Y 锚定因此不会错位。
        """
        return natural_height(x, z, self.seed)

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