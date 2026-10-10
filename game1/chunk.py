"""
chunk.py - 区块系统
无限地形的核心：每个区块 16x16 方块，按需确定性生成。
使用 numpy 向量化生成：256 格点 + 16 高度层一次性计算。
"""

import numpy as np
import config
from noise import fbm_2d_vec, value_noise_2d_vec
from blocks import BLOCK_ID
from biomes import BIOMES


# 生物群系 → 表面方块 ID 映射（预计算，避免热路径字典查找）
BIOME_SURFACE_BLOCK = {name: b.surface_block for name, b in BIOMES.items()}


class Chunk:
    """
    16x16 地形区块。
    确定性生成：同一种子 + 同一坐标 → 同一区块。
    生成使用 numpy 向量化，单区块 ~1ms（纯 Python 约 35ms）。
    """

    def __init__(self, cx: int, cz: int, seed: int, height: int,
                 base_temperature: float, moisture_base: float,
                 base_x: int, base_z: int):
        self.cx = cx
        self.cz = cz
        self.size = config.CHUNK_SIZE
        self.height = height
        self.seed = seed
        self.base_temperature = base_temperature
        self.moisture_base = moisture_base
        self.base_x = base_x
        self.base_z = base_z

        # 全局偏移
        self.gx = cx * self.size
        self.gz = cz * self.size

        # 数据
        self.terrain_flat = bytearray(self.size * self.size * self.height)
        self.biomes = []
        self.temperatures = []
        self.surface_height = []
        self.props = []

        self._generate()

    def _generate(self):
        """确定性生成本区块的地形（numpy 向量化）"""
        size = self.size
        height = self.height
        gx = self.gx
        gz = self.gz

        # ─── 1. 坐标网格 ──────────────────────────────────
        x_vals = gx + np.arange(size, dtype=np.float64)
        z_vals = gz + np.arange(size, dtype=np.float64)
        x, z = np.meshgrid(x_vals, z_vals, indexing='ij')  # [size, size]

        # ─── 2. 高度图（4 八度 FBM）──────────────────────
        h_raw = fbm_2d_vec(x * 0.06, z * 0.06, self.seed,
                           octaves=4, persistence=0.5)
        h_raw = h_raw * 1.4 - 0.28
        h = h_raw.copy()
        mask = h_raw > 0
        h[mask] = h_raw[mask] ** 0.7
        h = np.clip(h, 0.0, 1.0)

        # ─── 3. 湿度（3 八度 FBM）────────────────────────
        moisture = fbm_2d_vec(x * 0.04, z * 0.04, self.seed + 9999,
                              octaves=3, persistence=0.4)

        # ─── 4. 高度 & 温度 ───────────────────────────────
        altitude = h * height
        temp = self.base_temperature - altitude * config.TEMPERATURE_LAPSE_RATE

        # ─── 5. 大本营区域覆盖 ────────────────────────────
        dist_to_base = np.sqrt((x - self.base_x) ** 2 + (z - self.base_z) ** 2)
        in_base_area = dist_to_base < config.BASE_RADIUS + 3
        h = np.where(in_base_area, 0.3, h)
        altitude = np.where(in_base_area, 0.3 * height, altitude)
        temp = np.where(in_base_area, 15.0, temp)
        moisture = np.where(in_base_area, 0.5, moisture)

        # ─── 6. 生物群系判定（向量化 if-else 链）─────────
        biome_arr = np.full((size, size), "plains", dtype=object)
        biome_arr[h < 0.18] = "ocean"
        snow_mask = (temp < -10.0) & (h >= 0.18)
        biome_arr[snow_mask] = "snowy"
        tundra_mask = (temp < 3.0) & (temp >= -10.0) & (h >= 0.18)
        biome_arr[tundra_mask] = "tundra"
        mountain_mask = (altitude > 12.0) & (h >= 0.18)
        biome_arr[mountain_mask] = "mountain"
        desert_mask = (temp > 25.0) & (moisture < 0.3) & (h >= 0.18)
        biome_arr[desert_mask] = "desert"
        savanna_mask = (temp > 20.0) & (moisture >= 0.3) & (h >= 0.18)
        biome_arr[savanna_mask] = "savanna"
        forest_mask = (temp < 15.0) & (moisture > 0.5) & (h >= 0.18)
        biome_arr[forest_mask] = "forest"

        # ─── 7. 矿脉噪声（全 3D 预计算）─────────────────
        # ore_noise[lx, lz, y] = value_noise_2d(x*0.5, z*0.5 + y*0.3, seed+777)
        x_ore = x * 0.5  # [size, size]
        z_ore = z * 0.5  # [size, size]
        y_ore = (np.arange(height, dtype=np.float64) * 0.3)[None, None, :]  # [1, 1, height]
        ore_noise = value_noise_2d_vec(
            np.broadcast_to(x_ore[:, :, None], (size, size, height)),
            np.broadcast_to(z_ore[:, :, None], (size, size, height)) + y_ore,
            self.seed + 777
        )  # [size, size, height]

        # ─── 8. 表面高度 & 表面方块 ──────────────────────
        surface_y_arr = np.floor(altitude).astype(np.int32)  # [size, size]

        # 表面方块：按生物群系映射
        surface_blocks = np.zeros((size, size), dtype=np.uint8)
        for biome_name, block_id in BIOME_SURFACE_BLOCK.items():
            surface_blocks[biome_arr == biome_name] = block_id

        # 高海拔寒冷 → 雪覆盖
        snow_cover = (surface_y_arr > 10) & (temp < 2.0)
        surface_blocks[snow_cover] = BLOCK_ID['snow']

        # 低海拔草地 → 沙
        low_grass = (h < 0.25) & (surface_blocks == BLOCK_ID['grass'])
        surface_blocks[low_grass] = BLOCK_ID['sand']

        # ─── 9. 构建 3D 区块数组 ──────────────────────────
        # blocks[lx, lz, y]
        blocks = np.zeros((size, size, height), dtype=np.uint8)

        for y in range(height):
            row = np.zeros((size, size), dtype=np.uint8)
            col_pos = y - surface_y_arr  # [size, size]

            if y == 0:
                row[:] = BLOCK_ID['bedrock']
            else:
                is_ocean = h < 0.20

                # --- 海洋柱 ---
                if np.any(is_ocean):
                    ocean_col = is_ocean
                    # 海底石
                    row[(col_pos < 0) & ocean_col] = BLOCK_ID['stone']
                    # 海滩（表面 + 1）
                    beach = (col_pos >= 0) & (col_pos <= 1) & ocean_col
                    row[beach] = BLOCK_ID['sand']
                    # 水体（表面+2 到 表面+5）
                    water = (col_pos >= 2) & (col_pos <= 6) & ocean_col
                    row[water] = BLOCK_ID['water']

                # --- 非海洋柱 ---
                non_ocean = ~is_ocean

                # 深层石头 + 矿脉
                deep = (col_pos < -3) & non_ocean
                if np.any(deep):
                    row[deep] = BLOCK_ID['stone']
                    coal_m = deep & (ore_noise[:, :, y] > 0.85)
                    iron_m = deep & (ore_noise[:, :, y] > 0.90)
                    if y > 4:
                        row[coal_m] = BLOCK_ID['coal_ore']
                    if y > 6:
                        row[iron_m] = BLOCK_ID['iron_ore']

                # 泥土层
                dirt = (col_pos >= -3) & (col_pos < 0) & non_ocean
                row[dirt] = BLOCK_ID['dirt']

                # 地表方块
                surf = (col_pos == 0) & non_ocean
                if np.any(surf):
                    row[surf] = surface_blocks[surf]

            blocks[:, :, y] = row

        # ─── 10. 写入平坦数组 ─────────────────────────────
        # terrain_flat 布局: [lx * (size * height) + lz * height + y]
        # blocks 布局:       [lx, lz, y]
        # C 顺序 reshape 正好匹配
        self.terrain_flat = bytearray(blocks.reshape(-1))

        # ─── 11. 存储元数据 ───────────────────────────────
        self.biomes = biome_arr.tolist()
        self.temperatures = temp.tolist()
        self.surface_height = surface_y_arr.tolist()

    def _determine_biome(self, temp, moisture, altitude, normalized_h):
        """生物群系判定（标量版，备用）"""
        if normalized_h < 0.18:
            return "ocean"
        if temp < -10.0:
            return "snowy"
        if temp < 3.0:
            return "tundra"
        if altitude > 12.0:
            return "mountain"
        if temp > 25.0 and moisture < 0.3:
            return "desert"
        if temp > 20.0 and moisture >= 0.3:
            return "savanna"
        if temp < 15.0 and moisture > 0.5:
            return "forest"
        return "plains"

    def _determine_block(self, x, z, temp, moisture, y, altitude,
                         normalized_h, biome):
        """确定方块类型（标量版，备用）"""
        surface_y = int(altitude)

        if y == 0:
            return BLOCK_ID["bedrock"]

        if normalized_h < 0.20:
            if y == surface_y or y == surface_y + 1:
                return BLOCK_ID["sand"]
            if y < surface_y:
                return BLOCK_ID["stone"]
            if y > surface_y + 1:
                if y <= surface_y + 5:
                    return BLOCK_ID["water"]
                return BLOCK_ID["air"]

        if y == surface_y:
            biome_def = BIOMES.get(biome, BIOMES["plains"])
            block = biome_def.surface_block
            if y > 10 and temp < 2.0:
                block = BLOCK_ID["snow"]
            if normalized_h < 0.25 and block == BLOCK_ID["grass"]:
                block = BLOCK_ID["sand"]
            return block

        if y < surface_y and y >= surface_y - 3:
            return BLOCK_ID["dirt"]

        if y < surface_y - 3:
            from noise import value_noise_2d
            noise_val = value_noise_2d(x * 0.5, z * 0.5 + y * 0.3,
                                        self.seed + 777)
            if noise_val > 0.85 and y > 4:
                return BLOCK_ID["coal_ore"]
            elif noise_val > 0.90 and y > 6:
                return BLOCK_ID["iron_ore"]
            return BLOCK_ID["stone"]

        return BLOCK_ID["air"]

    # ─── 查询接口 ──────────────────────────────────────────

    def get_block(self, lx: int, y: int, lz: int) -> int:
        """获取本地坐标的方块（平坦数组访问）"""
        if 0 <= lx < self.size and 0 <= lz < self.size and 0 <= y < self.height:
            return self.terrain_flat[lx * (self.size * self.height) + lz * self.height + y]
        return BLOCK_ID["air"]

    def set_block(self, lx: int, y: int, lz: int, block_id: int):
        """设置方块"""
        if 0 <= lx < self.size and 0 <= lz < self.size and 0 <= y < self.height:
            self.terrain_flat[lx * (self.size * self.height) + lz * self.height + y] = block_id

    def get_surface_height(self, lx: int, lz: int) -> int:
        """获取本地坐标的表面高度"""
        if 0 <= lx < self.size and 0 <= lz < self.size:
            return self.surface_height[lx][lz]
        return 0

    def get_surface_height_smooth(self, fx: float, fz: float) -> float:
        """双线性插值获取表面高度"""
        lx = max(0, min(self.size - 1, int(fx)))
        lz = max(0, min(self.size - 1, int(fz)))
        lx1 = min(lx + 1, self.size - 1)
        lz1 = min(lz + 1, self.size - 1)
        tfx = fx - int(fx)
        tfz = fz - int(fz)

        h00 = float(self.surface_height[lx][lz])
        h10 = float(self.surface_height[lx1][lz])
        h01 = float(self.surface_height[lx][lz1])
        h11 = float(self.surface_height[lx1][lz1])

        top = h00 * (1.0 - tfx) + h10 * tfx
        bot = h01 * (1.0 - tfx) + h11 * tfx
        return top * (1.0 - tfz) + bot * tfz

    def get_biome(self, lx: int, lz: int) -> str:
        if 0 <= lx < self.size and 0 <= lz < self.size:
            return self.biomes[lx][lz]
        return "plains"

    def get_temperature(self, lx: int, lz: int) -> float:
        if 0 <= lx < self.size and 0 <= lz < self.size:
            return self.temperatures[lx][lz]
        return 15.0

    @property
    def is_loaded(self):
        return True

    def __repr__(self):
        return f"Chunk(cx={self.cx}, cz={self.cz}, size={self.size})"
