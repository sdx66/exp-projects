"""
world.py - 种子驱动的宇宙生成
确定性生成：同一种子 → 同一宇宙
包含：地形生成、生物群系判定、天气系统、路径规划
"""

import random
import math
from dataclasses import dataclass, field

import config
from noise import fbm_2d, value_noise_2d, generate_heightmap
from blocks import BLOCK_ID, get_block, is_solid, is_transparent

# ─── 生物群系 ──────────────────────────────────────────────

@dataclass(frozen=True)
class BiomeDef:
    """生物群系定义"""
    name: str
    # 温度范围 (°C)
    temp_min: float
    temp_max: float
    # 降水概率 (0-1)
    rain_chance: float
    # 雪概率 (0-1)
    snow_chance: float
    # 表面方块
    surface_block: int
    # 地下方块
    underground_block: int
    # 天空颜色
    sky_color: tuple
    # 雾密度
    fog_density: float

BIOMES = {
    "desert": BiomeDef(
        name="沙漠", temp_min=25.0, temp_max=50.0,
        rain_chance=0.02, snow_chance=0.0,
        surface_block=BLOCK_ID["sand"], underground_block=BLOCK_ID["sand"],
        sky_color=(180, 200, 220), fog_density=0.005,
    ),
    "plains": BiomeDef(
        name="平原", temp_min=5.0, temp_max=28.0,
        rain_chance=0.12, snow_chance=0.0,
        surface_block=BLOCK_ID["grass"], underground_block=BLOCK_ID["dirt"],
        sky_color=(130, 180, 230), fog_density=0.008,
    ),
    "forest": BiomeDef(
        name="森林", temp_min=5.0, temp_max=25.0,
        rain_chance=0.25, snow_chance=0.0,
        surface_block=BLOCK_ID["grass"], underground_block=BLOCK_ID["dirt"],
        sky_color=(110, 170, 210), fog_density=0.010,
    ),
    "savanna": BiomeDef(
        name="热带草原", temp_min=20.0, temp_max=35.0,
        rain_chance=0.08, snow_chance=0.0,
        surface_block=BLOCK_ID["grass"], underground_block=BLOCK_ID["dirt"],
        sky_color=(160, 190, 220), fog_density=0.007,
    ),
    "tundra": BiomeDef(
        name="苔原", temp_min=-10.0, temp_max=5.0,
        rain_chance=0.10, snow_chance=0.40,
        surface_block=BLOCK_ID["snow"], underground_block=BLOCK_ID["dirt"],
        sky_color=(160, 175, 195), fog_density=0.012,
    ),
    "snowy": BiomeDef(
        name="雪原", temp_min=-60.0, temp_max=-10.0,
        rain_chance=0.05, snow_chance=0.80,
        surface_block=BLOCK_ID["snow"], underground_block=BLOCK_ID["ice"],
        sky_color=(170, 185, 210), fog_density=0.015,
    ),
    "mountain": BiomeDef(
        name="山脉", temp_min=-20.0, temp_max=15.0,
        rain_chance=0.15, snow_chance=0.30,
        surface_block=BLOCK_ID["stone"], underground_block=BLOCK_ID["stone"],
        sky_color=(150, 170, 200), fog_density=0.018,
    ),
    "ocean": BiomeDef(
        name="海洋", temp_min=-10.0, temp_max=30.0,
        rain_chance=0.20, snow_chance=0.0,
        surface_block=BLOCK_ID["water"], underground_block=BLOCK_ID["water"],
        sky_color=(100, 150, 200), fog_density=0.006,
    ),
}

# ─── 路径记录 ──────────────────────────────────────────────

@dataclass(frozen=True)
class PathData:
    """敌人路径及预计算元数据。"""
    points: tuple
    length: int
    avg_slope: float


def _make_path_data(points: list) -> PathData:
    """预计算路径坡度，避免敌人生成时重复扫描。"""
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
    intensity: float = 0.0        # 降水强度 0-1
    wind_direction: float = 0.0   # 风向（弧度）
    wind_strength: float = 0.0    # 风力 0-1

# ─── 宇宙 ──────────────────────────────────────────────────

class Universe:
    """
    种子驱动的宇宙。
    同一 seed 生成完全相同的地形、生物群系、天气。
    """

    def __init__(self, seed,
                 world_size: int = None,
                 world_height: int = None):
        # 将任意种子转为整数（字符串取确定性哈希）
        if isinstance(seed, str):
            import zlib
            seed = zlib.crc32(seed.encode("utf-8")) & 0xFFFFFFFF
        self.seed = int(seed)
        self.size = world_size or config.WORLD_SIZE
        self.height = world_height or config.WORLD_HEIGHT
        self.rng = random.Random(seed)

        # 宇宙参数（由种子决定）
        self.base_temperature = 10.0 + self.rng.uniform(-5.0, 15.0)  # -5°C ~ 25°C
        self.moisture_base = self.rng.uniform(0.3, 0.7)
        self.latitude_shift = self.rng.uniform(-0.1, 0.1)  # 纬度偏移

        # 地形数据: terrain[x][z][y] = block_id
        self.terrain: list = []
        # 生物群系: biomes[x][z] = biome_name
        self.biomes: list = []
        # 温度: temperatures[x][z] = °C
        self.temperatures: list = []
        # 最高面高度: surface[x][z] = y
        self.surface_height: list = []

        # 天气
        self.weather = WeatherState()
        self.weather_timer = 0.0

        # 路径（敌人行走路线）
        self.enemy_path: list = []
        self.enemy_paths: list = []  # 多条备选路径

        # 生成世界
        self._generate()

    # ─── 生成 ──────────────────────────────────────────────

    def _generate(self):
        """确定性生成整个宇宙"""
        size = self.size
        height = self.height

        # 生成高度图并变换（增加地形多样性：海洋/平原/山脉）
        raw_heightmap = generate_heightmap(size, size, self.seed,
                                            scale=0.06, octaves=4,
                                            persistence=0.5)
        self.heightmap = []
        for x in range(size):
            row = []
            for z in range(size):
                # 原始噪声值约在 [0.2, 0.8]
                # 应用曲线：低洼区域下沉为海洋，高处隆起为山脉
                v = raw_heightmap[x][z]
                # 偏移并压缩，使约 20% 为海洋，80% 为陆地
                shifted = v * 1.4 - 0.28  # 范围约 [-0.11, 0.84]
                # 幂函数增强对比度
                if shifted > 0:
                    shaped = shifted ** 0.7
                else:
                    shaped = shifted
                row.append(max(0.0, min(1.0, shaped)))
            self.heightmap.append(row)

        # 生成湿度图
        self.moisture_map = generate_heightmap(size, size, self.seed + 9999,
                                                 scale=0.04, octaves=3,
                                                 persistence=0.4)

        # 初始化地形数组
        self.terrain = []
        self.biomes = []
        self.temperatures = []
        self.surface_height = []

        for x in range(size):
            terrain_col = []
            biome_col = []
            temp_col = []
            surface_col = []

            for z in range(size):
                # 地形柱
                terrain_z = []
                h = self.heightmap[x][z]
                moisture = self.moisture_map[x][z]
                altitude = h * self.height

                # 温度 = 基准温度 - 海拔 × 递减率
                temp = self.base_temperature - altitude * config.TEMPERATURE_LAPSE_RATE

                # 生物群系判定
                biome = self._determine_biome(temp, moisture, altitude, h)

                # 放置方块
                for y in range(height):
                    block_id = self._determine_block(x, z, temp, moisture, y, altitude, h, biome)
                    terrain_z.append(block_id)

                # 记录表面高度
                surface_y = altitude
                surface_col.append(int(surface_y))

                terrain_col.append(terrain_z)
                biome_col.append(biome)
                temp_col.append(temp)

            self.terrain.append(terrain_col)
            self.biomes.append(biome_col)
            self.temperatures.append(temp_col)
            self.surface_height.append(surface_col)

        # 生成路径
        self._generate_path()

        # 初始化天气
        self._update_weather()

    def _determine_biome(self, temp: float, moisture: float,
                          altitude: float, normalized_h: float) -> str:
        """
        基于温度、湿度、海拔判定生物群系
        遵循现实地理规律：
        - 低温优先：冷地区即使低海拔也是苔原/雪原
        - 低海拔低湿度 → 海洋
        - 高海拔 → 山脉（但极寒区优先）
        - 高温 + 低湿 → 沙漠
        """
        # 低海拔低湿度 → 海洋（优先于温度判定）
        if normalized_h < 0.18:
            return "ocean"

        # 极寒：雪原（优先于海拔判定）
        if temp < -10.0:
            return "snowy"

        # 寒冷：苔原
        if temp < 3.0:
            return "tundra"

        # 高海拔：山脉
        if altitude > 12.0:
            return "mountain"

        # 高温 + 低湿 → 沙漠
        if temp > 25.0 and moisture < 0.3:
            return "desert"

        # 高温 + 适中湿度 → 热带草原
        if temp > 20.0 and moisture >= 0.3:
            return "savanna"

        # 适温 + 湿润 → 森林
        if temp < 15.0 and moisture > 0.5:
            return "forest"

        # 默认平原
        return "plains"

    def _determine_block(self, x: int, z: int, temp: float, moisture: float, y: int,
                          altitude: float, normalized_h: float,
                          biome: str) -> int:
        """
        确定某高度层放置什么方块
        """
        surface_y = int(altitude)

        # 最底层：基岩
        if y == 0:
            return BLOCK_ID["bedrock"]

        # 水下
        if normalized_h < 0.20:
            if y == surface_y or y == surface_y + 1:
                return BLOCK_ID["sand"]
            if y < surface_y:
                return BLOCK_ID["stone"]
            if y > surface_y + 1:
                if y <= surface_y + 5:
                    return BLOCK_ID["water"]
                return BLOCK_ID["air"]

        # 地表
        if y == surface_y:
            # 根据生物群系选择表面方块
            biome_def = BIOMES.get(biome, BIOMES["plains"])
            block = biome_def.surface_block

            # 高海拔覆雪
            if y > 10 and temp < 2.0:
                block = BLOCK_ID["snow"]

            # 极低海拔覆沙
            if normalized_h < 0.25 and block == BLOCK_ID["grass"]:
                block = BLOCK_ID["sand"]

            return block

        # 地下：2-3 层泥土，再下面是石头
        if y < surface_y and y >= surface_y - 3:
            return BLOCK_ID["dirt"]

        # 深层石头，可能含矿
        if y < surface_y - 3:
            # 矿脉概率（基于噪声）
            noise_val = value_noise_2d(x * 0.5, z * 0.5 + y * 0.3, self.seed + 777)
            if noise_val > 0.85 and y > 4:
                return BLOCK_ID["coal_ore"]
            elif noise_val > 0.90 and y > 6:
                return BLOCK_ID["iron_ore"]
            return BLOCK_ID["stone"]

        return BLOCK_ID["air"]

    def _generate_path(self):
        """
        生成多条敌人路径
        每条路径有不同的地形偏好，让不同敌人走不同路线
        """
        size = self.size
        self.enemy_paths = []

        # 生成 3 条备选路径，每条有不同的噪声种子和偏好
        path_seeds = [
            ("direct", 555, 0.3),   # 路径1：较直，偏好平原
            ("flat",   777, 0.5),   # 路径2：平缓，避开山脉
            ("scenic", 999, 0.7),   # 路径3：蜿蜒，更多变化
        ]

        for idx, (name, noise_seed, noise_amp) in enumerate(path_seeds):
            path_points = self._generate_single_path(noise_seed, noise_amp, idx)
            if path_points:
                self.enemy_paths.append(_make_path_data(path_points))

        # 兼容旧代码：主路径 = 第一条路径的坐标点
        self.enemy_path = self.enemy_paths[0].points if self.enemy_paths else ()

    def _generate_single_path(self, noise_seed, noise_amp, path_index):
        """
        生成单条路径
        noise_seed: 噪声种子，决定路径形状
        noise_amp: 噪声幅度，决定路径弯曲程度
        path_index: 路径索引，用于偏移 Z 起点
        """
        size = self.size
        path = []

        # 起点：左侧中间，每条路径略有 Z 偏移
        start_z = size // 2 + int(path_index * 2 - 2)  # 路径0: z=24, 路径1: z=24, 路径2: z=24+4
        start_z = max(1, min(size - 2, start_z))
        start_x = 1
        end_x = size - 2

        # 路径沿 X 轴前进，Z 随噪声变化
        for x in range(start_x, end_x + 1):
            z = start_z
            # 沿路径微调 Z，使路径更自然
            z_offset = int(value_noise_2d(x * 0.08, path_index * 10, self.seed + noise_seed) * noise_amp * 6)
            z = max(1, min(size - 2, z + z_offset))

            # 避开水域：如果当前 z 是海洋，向上下搜索最近的陆地
            if self.biomes[x][z] == "ocean":
                found = False
                for dz in range(1, size // 2):
                    if not found:
                        z_up = min(size - 2, z + dz)
                        if self.biomes[x][z_up] != "ocean":
                            z = z_up
                            found = True
                    if not found:
                        z_down = max(1, z - dz)
                        if self.biomes[x][z_down] != "ocean":
                            z = z_down
                            found = True
                    if found:
                        break

            surface_y = self.surface_height[x][z]
            path.append((x, surface_y + 1, z))

        return path

    def _update_weather(self):
        """更新天气状态"""
        self.weather_timer += 1
        # 每 60 帧更新一次天气
        if self.weather_timer >= 60:
            self.weather_timer = 0

            # 基于随机选一个采样点的生物群系决定天气
            x = self.rng.randint(0, self.size - 1)
            z = self.rng.randint(0, self.size - 1)
            biome = self.biomes[x][z]
            biome_def = BIOMES.get(biome, BIOMES["plains"])

            # 降水判定
            if self.rng.random() < biome_def.rain_chance:
                self.weather.is_raining = True
                self.weather.intensity = self.rng.uniform(0.3, 1.0)
            else:
                self.weather.is_raining = False
                self.weather.intensity = 0.0

            # 降雪判定
            if self.rng.random() < biome_def.snow_chance:
                self.weather.is_snowing = True
            else:
                self.weather.is_snowing = False

            # 风
            self.weather.wind_direction = self.rng.uniform(0, math.pi * 2)
            self.weather.wind_strength = self.rng.uniform(0.1, 0.8)

    # ─── 查询接口 ──────────────────────────────────────────

    def get_block(self, x: int, y: int, z: int) -> int:
        """获取方块 ID"""
        if 0 <= x < self.size and 0 <= z < self.size and 0 <= y < self.height:
            return self.terrain[x][z][y]
        return BLOCK_ID["air"]

    def set_block(self, x: int, y: int, z: int, block_id: int):
        """设置方块"""
        if 0 <= x < self.size and 0 <= z < self.size and 0 <= y < self.height:
            self.terrain[x][z][y] = block_id

    def get_surface_height(self, x: int, z: int) -> int:
        """获取表面高度"""
        if 0 <= x < self.size and 0 <= z < self.size:
            return self.surface_height[x][z]
        return 0

    def get_surface_height_smooth(self, x: float, z: float) -> float:
        """双线性插值获取浮点坐标处的表面高度（用于敌人贴合地形）"""
        x0 = max(0, min(int(x), self.size - 1))
        z0 = max(0, min(int(z), self.size - 1))
        x1 = min(x0 + 1, self.size - 1)
        z1 = min(z0 + 1, self.size - 1)
        fx = x - x0
        fz = z - z0

        h00 = float(self.surface_height[x0][z0])
        h10 = float(self.surface_height[x1][z0])
        h01 = float(self.surface_height[x0][z1])
        h11 = float(self.surface_height[x1][z1])

        top = h00 * (1.0 - fx) + h10 * fx
        bot = h01 * (1.0 - fx) + h11 * fx
        return top * (1.0 - fz) + bot * fz

    def get_biome(self, x: int, z: int) -> str:
        """获取生物群系"""
        if 0 <= x < self.size and 0 <= z < self.size:
            return self.biomes[x][z]
        return "plains"

    def get_temperature(self, x: int, z: int) -> float:
        """获取温度"""
        if 0 <= x < self.size and 0 <= z < self.size:
            return self.temperatures[x][z]
        return 15.0

    def update_weather(self):
        """每帧调用"""
        if self.weather_timer >= 60:
            self._update_weather()
        else:
            self.weather_timer += 1

    def __repr__(self):
        return f"Universe(seed={self.seed}, size={self.size}, height={self.height})"

# ─── 辅助：打印地形概览 ──────────────────────────────────────

def print_terrain_overview(universe: Universe):
    """在控制台打印地形高度图（调试用）"""
    print(f"\n=== 宇宙概览 (种子: {universe.seed}) ===")
    print(f"世界: {universe.size}x{universe.size}, 高度: {universe.height}")
    print(f"基准温度: {universe.base_temperature:.1f}°C, 湿度: {universe.moisture_base:.2f}")
    print(f"路径长度: {len(universe.enemy_path)} 格\n")

    # 生物群系分布统计
    biome_count = {}
    for x in range(universe.size):
        for z in range(universe.size):
            b = universe.biomes[x][z]
            biome_count[b] = biome_count.get(b, 0) + 1

    print("生物群系分布:")
    for b, count in sorted(biome_count.items(), key=lambda x: -x[1]):
        biome_def = BIOMES.get(b)
        name = biome_def.name if biome_def else b
        pct = count / (universe.size * universe.size) * 100
        print(f"  {name:10s}: {count:5d} ({pct:5.1f}%)")

    # 高度图 ASCII 可视化
    print("\n高度图 (0-15):")
    for z in range(0, universe.size, 3):
        row = ""
        for x in range(0, universe.size, 2):
            h = universe.surface_height[x][z]
            if h < 2:
                row += "~"
            elif h < 5:
                row += "."
            elif h < 8:
                row += "-"
            elif h < 11:
                row += "^"
            elif h < 14:
                row += "A"
            else:
                row += "!"
        print(f"  {row}")
