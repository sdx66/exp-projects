"""
chunk.py - 区块系统
无限地形的核心：每个区块 16x16 方块，按需确定性生成。
使用 numpy 向量化生成：256 格点 + 16 高度层一次性计算。
"""

import math

import numpy as np
import config
from noise import (fbm_levels_2d, fbm_levels_2d_vec,
                   fbm_levels_param_2d, fbm_levels_param_2d_vec)
from blocks import BLOCK_ID
from biomes import BIOMES


# 生物群系 → 表面方块 ID 映射（预计算，避免热路径字典查找）
BIOME_SURFACE_BLOCK = {name: b.surface_block for name, b in BIOMES.items()}


# ─── 自然高度（大本营平整前的地形高度） ──────────────────────
# chunk._generate 与 world._get_height_normalized 共用同一实现，
# 保证"区块表面"与"纯噪声查询"永不脱钩（刷怪点与路径 Y 锚定依赖此一致性）。
#
# 管线（参数全部来自 config.NOISE_CFG["height"]，见 config 内注释）：
#   ① 域扭曲   噪声场推动采样坐标
#   ② 参数噪声 低频场 P 调制高度 FBM 各级振幅/频率
#   ③ 高度 FBM 逐点参数化叠加
#   ④ 山脊混合 r=(1-|2n-1|)^sharpness，强度 = strength·P^a·M^b 联合控制
#   ⑤ 高度曲线 ×1.4−0.28 → 正部^0.7 → clip（改造前原有步骤）
# ①②④ 皆可经配置关闭；全关时逐位还原旧的 3 级 FBM 公式。
# 标量版与向量化版必须逐算术步骤镜像（保证逐位一致）。

def natural_height_vec(x, z, seed):
    """归一化自然高度 [0, 1]，向量化。x/z 为原始世界坐标。"""
    cfg = config.NOISE_CFG["height"]
    x = np.asarray(x, dtype=np.float64)
    z = np.asarray(z, dtype=np.float64)

    # ① 域扭曲：两组独立噪声 → x/z 各自的位移量（[-strength, +strength]）
    warp_cfg = cfg.get("warp")
    if warp_cfg is not None and warp_cfg.get("strength", 0.0) != 0.0:
        w_levels = warp_cfg["levels"]
        wx = fbm_levels_2d_vec(x, z, seed + warp_cfg["seed_off_x"], w_levels)
        wz = fbm_levels_2d_vec(x, z, seed + warp_cfg["seed_off_z"], w_levels)
        xs = x + (wx * 2.0 - 1.0) * warp_cfg["strength"]
        zs = z + (wz * 2.0 - 1.0) * warp_cfg["strength"]
    else:
        xs, zs = x, z

    # ② 参数噪声：P 在原始（未扭曲）坐标上取值 —— 平缓度是"地面属性"，
    #    不应随采样点被推走而错位。
    #    P 同时供 ④ 的山脊混合使用；param 整体缺席时 P 无定义，
    #    ④ 的控制项退化为 M^mask_pow。
    param_cfg = cfg.get("param")
    ridged_cfg = cfg.get("ridged")
    ridged_on = ridged_cfg is not None and ridged_cfg.get("strength", 0.0) != 0.0
    param_on = (param_cfg is not None
                and (param_cfg.get("amp_depth", 0.0) != 0.0
                     or param_cfg.get("freq_depth", 0.0) != 0.0))
    p = None
    if param_cfg is not None and (param_on or ridged_on):
        p = fbm_levels_2d_vec(x, z, seed + param_cfg["seed_off"],
                              param_cfg["levels"])

    # ③ 高度 FBM（param_on 时逐点调制振幅/频率；否则普通 FBM）
    if param_on:
        amp_scale = 1.0 + param_cfg["amp_depth"] * (p * 2.0 - 1.0)
        freq_scale = 1.0 + param_cfg["freq_depth"] * (p * 2.0 - 1.0)
        n = fbm_levels_param_2d_vec(xs, zs, seed, cfg["levels"],
                                    param_cfg["mod_levels"],
                                    amp_scale, freq_scale)
    else:
        n = fbm_levels_2d_vec(xs, zs, seed, cfg["levels"])

    # ④ 山脊混合：blend = strength · P'^param_pow · M^mask_pow
    #    P' 是 P 的重映射：P ≤ param_lo 处 blend 精确为 0（平缓区硬保证）；
    #    M 决定山脊长在哪
    if ridged_on:
        m = fbm_levels_2d_vec(x, z, seed + ridged_cfg["mask_seed_off"],
                              ridged_cfg["mask_levels"])
        r = 1.0 - np.abs(n * 2.0 - 1.0)
        r = r ** ridged_cfg["sharpness"]
        if p is None:
            control = m ** ridged_cfg["mask_pow"]
        else:
            plo = ridged_cfg.get("param_lo", 0.0)
            phi = ridged_cfg.get("param_hi", 1.0)
            if phi > plo:
                pn = np.clip((p - plo) / (phi - plo), 0.0, 1.0)
            else:
                pn = p
            control = pn ** ridged_cfg["param_pow"] * m ** ridged_cfg["mask_pow"]
        blend = ridged_cfg["strength"] * control
        n = n * (1.0 - blend) + r * blend

    # ⑤ 高度曲线（与改造前逐位相同）
    h_raw = n * 1.4 - 0.28
    h = h_raw.copy()
    mask = h_raw > 0
    h[mask] = h_raw[mask] ** 0.7
    return np.clip(h, 0.0, 1.0)


def natural_height(x: float, z: float, seed: int) -> float:
    """归一化自然高度 [0, 1]，标量版（与向量化版逐算术步骤镜像）。"""
    cfg = config.NOISE_CFG["height"]

    # ① 域扭曲
    warp_cfg = cfg.get("warp")
    if warp_cfg is not None and warp_cfg.get("strength", 0.0) != 0.0:
        w_levels = warp_cfg["levels"]
        wx = fbm_levels_2d(x, z, seed + warp_cfg["seed_off_x"], w_levels)
        wz = fbm_levels_2d(x, z, seed + warp_cfg["seed_off_z"], w_levels)
        xs = x + (wx * 2.0 - 1.0) * warp_cfg["strength"]
        zs = z + (wz * 2.0 - 1.0) * warp_cfg["strength"]
    else:
        xs, zs = x, z

    # ② 参数噪声（P 在原始坐标上取值；缺席时 ④ 退化为 M^mask_pow）
    param_cfg = cfg.get("param")
    ridged_cfg = cfg.get("ridged")
    ridged_on = ridged_cfg is not None and ridged_cfg.get("strength", 0.0) != 0.0
    param_on = (param_cfg is not None
                and (param_cfg.get("amp_depth", 0.0) != 0.0
                     or param_cfg.get("freq_depth", 0.0) != 0.0))
    p = None
    if param_cfg is not None and (param_on or ridged_on):
        p = fbm_levels_2d(x, z, seed + param_cfg["seed_off"],
                          param_cfg["levels"])

    # ③ 高度 FBM
    if param_on:
        amp_scale = 1.0 + param_cfg["amp_depth"] * (p * 2.0 - 1.0)
        freq_scale = 1.0 + param_cfg["freq_depth"] * (p * 2.0 - 1.0)
        n = fbm_levels_param_2d(xs, zs, seed, cfg["levels"],
                                param_cfg["mod_levels"],
                                amp_scale, freq_scale)
    else:
        n = fbm_levels_2d(xs, zs, seed, cfg["levels"])

    # ④ 山脊混合
    if ridged_on:
        m = fbm_levels_2d(x, z, seed + ridged_cfg["mask_seed_off"],
                          ridged_cfg["mask_levels"])
        r = 1.0 - abs(n * 2.0 - 1.0)
        r = r ** ridged_cfg["sharpness"]
        if p is None:
            control = m ** ridged_cfg["mask_pow"]
        else:
            plo = ridged_cfg.get("param_lo", 0.0)
            phi = ridged_cfg.get("param_hi", 1.0)
            if phi > plo:
                pn = max(0.0, min(1.0, (p - plo) / (phi - plo)))
            else:
                pn = p
            control = pn ** ridged_cfg["param_pow"] * m ** ridged_cfg["mask_pow"]
        blend = ridged_cfg["strength"] * control
        n = n * (1.0 - blend) + r * blend

    # ⑤ 高度曲线
    h_raw = n * 1.4 - 0.28
    if h_raw > 0:
        return max(0.0, min(1.0, h_raw ** 0.7))
    return max(0.0, min(1.0, h_raw))


# 大本营平整高度的缓存：key = (base_x, base_z, seed)。
# 平均一圈需要上百次标量噪声采样（~1ms），而 _get_height_with_base_override
# 会被刷怪点/路径逻辑逐格调用，不缓存会把这成本乘上调用次数。
_BASE_CENTER_HEIGHT_CACHE: dict = {}


def base_center_height(base_x: int, base_z: int, seed: int) -> float:
    """大本营平整区的高度：取周围自然高度的平均值。

    取样半径与平整半径相同（BASE_RADIUS + 3）—— 平台高度即该圆盘内
    自然高度的均值，再据此把圆盘内平整成同一高度。
    chunk._generate 与 world._get_height_with_base_override 共用同一实现。
    """
    key = (base_x, base_z, seed)
    cached = _BASE_CENTER_HEIGHT_CACHE.get(key)
    if cached is not None:
        return cached

    radius = config.BASE_RADIUS + 3
    r = int(math.ceil(radius))
    total = 0.0
    count = 0
    for dx in range(-r, r + 1):
        for dz in range(-r, r + 1):
            if math.hypot(dx, dz) < radius:
                total += natural_height(base_x + dx, base_z + dz, seed)
                count += 1
    value = total / count
    _BASE_CENTER_HEIGHT_CACHE[key] = value
    return value


class Chunk:
    """
    16x16 地形区块。
    确定性生成：同一种子 + 同一坐标 → 同一区块。
    生成使用 numpy 向量化。
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
        self.terrain_flat = bytearray()
        self.biomes = []
        self.temperatures = []
        self.surface_height = []
        self.props = []
        self.max_y = 0
        self._ocean_cols = None
        self._ore_ready = False

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

        # ─── 2. 高度图（多级噪声，参数见 config.NOISE_CFG["height"]）
        h = natural_height_vec(x, z, self.seed)

        # ─── 3. 湿度（多级噪声，见 config.NOISE_CFG["moisture"]）
        m_cfg = config.NOISE_CFG["moisture"]
        moisture = fbm_levels_2d_vec(x, z, self.seed + m_cfg.get("base_seed", 0),
                                     m_cfg["levels"])

        # ─── 4. 大本营区域平整 ────────────────────────────
        # 不强制海拔：取大本营中心的自然高度/湿度填满半径内，基地坐落在
        # 其自然地形高度上。温度由平整后的高度推导，不再单独覆盖。
        dist_to_base = np.sqrt((x - self.base_x) ** 2 + (z - self.base_z) ** 2)
        in_base_area = dist_to_base < config.BASE_RADIUS + 3
        h_base = base_center_height(self.base_x, self.base_z, self.seed)
        m_base = fbm_levels_2d(self.base_x, self.base_z,
                               self.seed + m_cfg.get("base_seed", 0),
                               m_cfg["levels"])
        h = np.where(in_base_area, h_base, h)
        moisture = np.where(in_base_area, m_base, moisture)
        self._ocean_cols = h < 0.005

        # ─── 5. 高度 & 温度 ───────────────────────────────
        altitude = h * height
        temp = self.base_temperature - altitude * config.TEMPERATURE_LAPSE_RATE

        # ─── 6. 生物群系判定（向量化 if-else 链）─────────
        biome_arr = np.full((size, size), "plains", dtype=object)
        biome_arr[h < 0.004] = "ocean"
        snow_mask = (temp < -10.0) & (h >= 0.18)
        biome_arr[snow_mask] = "snowy"
        tundra_mask = (temp < 3.0) & (temp >= -10.0) & (h >= 0.18)
        biome_arr[tundra_mask] = "tundra"
        mountain_mask = (altitude > config.MOUNTAIN_ALT_RATIO * height) & (h >= 0.18)
        biome_arr[mountain_mask] = "mountain"
        desert_mask = (temp > 25.0) & (moisture < 0.3) & (h >= 0.18)
        biome_arr[desert_mask] = "desert"
        savanna_mask = (temp > 20.0) & (moisture >= 0.3) & (h >= 0.18)
        biome_arr[savanna_mask] = "savanna"
        forest_mask = (temp < 15.0) & (moisture > 0.5) & (h >= 0.18)
        biome_arr[forest_mask] = "forest"

        # ─── 7. 表面高度 & 表面方块 ──────────────────────
        surface_y_arr = np.floor(altitude).astype(np.int32)  # [size, size]
        max_y = min(height, int(surface_y_arr.max()) + 7)
        max_y = max(1, max_y)
        self.max_y = max_y

        # 表面方块：按生物群系映射
        surface_blocks = np.zeros((size, size), dtype=np.uint8)
        for biome_name, block_id in BIOME_SURFACE_BLOCK.items():
            surface_blocks[biome_arr == biome_name] = block_id

        # 高海拔寒冷 → 雪覆盖
        snow_cover = (surface_y_arr > config.SNOW_SURFACE_RATIO * height) & (temp < 2.0)
        surface_blocks[snow_cover] = BLOCK_ID['snow']

        # 低海拔草地 → 沙
        low_grass = (h < 0.25) & (surface_blocks == BLOCK_ID['grass'])
        surface_blocks[low_grass] = BLOCK_ID['sand']

        # ─── 9. 构建 3D 区块数组 ──────────────────────────
        # blocks[lx, lz, y]
        blocks = np.zeros((size, size, height), dtype=np.uint8)
        b = blocks[:, :, :max_y]
        # col_pos 用 int16：高度 <= WORLD_HEIGHT(2048)，差值不会溢出；
        # 内存带宽减半，且 y=0 最终会被基岩覆盖，无需 valid 掩码。
        yy = np.arange(max_y, dtype=np.int16)[None, None, :]
        col_pos = yy - surface_y_arr.astype(np.int16, copy=False)[:, :, None]
        ocean_cols = self._ocean_cols
        if ocean_cols.any():
            is_ocean = ocean_cols[:, :, None]
            non_ocean = ~is_ocean
            b[(col_pos < 0) & is_ocean] = BLOCK_ID['stone']
            b[(col_pos >= 0) & (col_pos <= 1) & is_ocean] = BLOCK_ID['sand']
            b[(col_pos >= 2) & (col_pos <= 6) & is_ocean] = BLOCK_ID['water']
            deep = (col_pos < -3) & non_ocean
            dirt = (col_pos >= -3) & (col_pos < 0) & non_ocean
            surf = (col_pos == 0) & non_ocean
        else:
            # 常见情况：无海洋列，跳过全部海洋掩码/赋值（省约 0.5ms）。
            deep = col_pos < -3
            dirt = (col_pos >= -3) & (col_pos < 0)
            surf = col_pos == 0
        b[deep] = BLOCK_ID['stone']
        b[dirt] = BLOCK_ID['dirt']
        np.copyto(b, surface_blocks[:, :, None], where=surf)
        b[:, :, 0] = BLOCK_ID['bedrock']

        # ─── 10. 写入平坦数组 ─────────────────────────────
        # terrain_flat 布局: [lx * (size * height) + lz * height + y]
        # blocks 布局:       [lx, lz, y]
        # C 顺序 reshape 正好匹配
        self.terrain_flat = bytearray(blocks.reshape(-1))

        # ─── 11. 存储元数据 ───────────────────────────────
        self.biomes = biome_arr.tolist()
        self.temperatures = temp.tolist()
        self.surface_height = surface_y_arr.tolist()

    def _ensure_ore(self):
        """Generate ore blocks on first deep-block access."""
        if self._ore_ready:
            return
        size = self.size
        height = self.height
        max_y = self.max_y
        if max_y <= 4 or self._ocean_cols is None:
            self._ore_ready = True
            return

        surface_y = np.asarray(self.surface_height, dtype=np.int32)
        x_vals = self.gx + np.arange(size, dtype=np.float64)
        z_vals = self.gz + np.arange(size, dtype=np.float64)
        x, z = np.meshgrid(x_vals, z_vals, indexing='ij')
        ore_cfg = config.NOISE_CFG["ore"]
        yy = np.arange(max_y, dtype=np.int32)[None, None, :]
        y_ore = (np.arange(max_y, dtype=np.float64) * 0.3)[None, None, :]
        ore_noise = fbm_levels_2d_vec(
            np.broadcast_to((x * 0.5)[:, :, None], (size, size, max_y)),
            np.broadcast_to((z * 0.5)[:, :, None], (size, size, max_y)) + y_ore,
            self.seed + ore_cfg.get("base_seed", 0),
            ore_cfg["levels"],
        )
        deep = ((yy - surface_y[:, :, None] < -3)
                & (yy != 0)
                & ~self._ocean_cols[:, :, None])
        blocks = np.frombuffer(self.terrain_flat, dtype=np.uint8).reshape(
            size, size, height)
        b = blocks[:, :, :max_y]
        unmodified = b == BLOCK_ID["stone"]
        coal = deep & (ore_noise > 0.85) & (yy > 4) & unmodified
        iron = deep & (ore_noise > 0.90) & (yy > 6) & unmodified
        b[coal] = BLOCK_ID["coal_ore"]
        b[iron] = BLOCK_ID["iron_ore"]
        self._ore_ready = True

    def _is_deep_ore_cell(self, lx, y, lz):
        if self._ore_ready or y <= 0:
            return False
        surface_y = self.surface_height[lx][lz]
        if y > surface_y - 4:
            return False
        return not self._ocean_cols[lx, lz]

    # 注：标量版 _determine_biome/_determine_block 已删除——它们从未被调用，
    # 且阈值（ocean h<0.18/0.20，水 y<=surface+5）与现行向量路径
    # （_generate 中 h<0.004/0.005，col_pos<=6）不一致，一旦误用会几乎全判为海洋。

    # ─── 查询接口 ──────────────────────────────────────────

    def get_block(self, lx: int, y: int, lz: int) -> int:
        """获取本地坐标的方块（平坦数组访问�?"""
        if 0 <= lx < self.size and 0 <= lz < self.size and 0 <= y < self.height:
            if self._is_deep_ore_cell(lx, y, lz):
                self._ensure_ore()
            return self.terrain_flat[lx * (self.size * self.height) + lz * self.height + y]
        return BLOCK_ID["air"]

    def set_block(self, lx: int, y: int, lz: int, block_id: int):
        """设置方块"""
        if 0 <= lx < self.size and 0 <= lz < self.size and 0 <= y < self.height:
            if self._is_deep_ore_cell(lx, y, lz):
                self._ensure_ore()
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
