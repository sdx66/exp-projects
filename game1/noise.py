"""
noise.py - 程序化噪声生成
纯 Python + numpy 混合实现，可种子化，确定性输出。
使用值噪声 + 多级（多八度）FBM，生成自然地形。

提供三组 API（每组都有标量 / 向量化版本）：
  - 单级值噪声：value_noise_2d, value_noise_2d_vec, value_noise_3d
  - 多级噪声：fbm_levels_2d, fbm_levels_2d_vec, fbm_levels_3d
    levels = [(频率倍率, 振幅, 种子偏移), ...]，级数任意：
    0 级 → 常数 0；1 级 → 等价单级噪声；N 级 → N 级加权叠加。
    每级频率/振幅/种子偏移完全独立，不限于几何级数。
  - 经典多八度：fbm_2d, fbm_2d_vec, fbm_3d
    几何级数（octaves/persistence/frequency/lacunarity）的薄包装，
    内部调 fbm_levels_*，输出与包装前逐位一致。

标量版用于路径生成等低频调用；向量化版用于区块生成热路径。
噪声调参见 config.NOISE_CFG。
"""

import math
import numpy as np

# ─── 哈希函数（标量版，带 FIFO 缓存）─────────────────────
_HASH_CACHE_MAX = 200000
_hash_cache: dict = {}

UINT32_MAX = np.uint64(0xFFFFFFFF)


def _hash_2d(x: int, y: int, seed: int) -> float:
    """2D 哈希 → [0, 1)，带格点 FIFO 缓存（标量版）

    与 _hash_2d_vec 逐位一致：每步 & 0xFFFFFFFF（32 位掩码）。
    """
    key = (x, y, seed)
    cached = _hash_cache.get(key)
    if cached is not None:
        return cached
    s = seed & 0xFFFFFFFF
    h = (x * 374761393 + y * 668265263 + s * 1442695040) & 0xFFFFFFFF
    h = ((h ^ (h >> 13)) * 1274126177) & 0xFFFFFFFF
    h = (h ^ (h >> 16)) & 0xFFFFFFFF
    result = h / 0xFFFFFFFF
    _hash_cache[key] = result
    if len(_hash_cache) > _HASH_CACHE_MAX:
        _hash_cache.pop(next(iter(_hash_cache)))
    return result


def _hash_2d_vec(ix, iy, seed):
    """向量化 2D 哈希 → [0, 1)
    ix, iy: numpy 整数数组（格点坐标）
    seed: 标量 int
    uint32 运算按模 2^32 回绕，与逐位掩码结果一致。
    """
    ix = np.asarray(ix, dtype=np.uint32)
    iy = np.asarray(iy, dtype=np.uint32)
    s = np.uint32(((seed & 0xFFFFFFFF) * 1442695040) & 0xFFFFFFFF)

    h = (ix * np.uint32(374761393)
         + iy * np.uint32(668265263)
         + s)
    h = h ^ (h >> np.uint32(13))
    h = h * np.uint32(1274126177)
    h = h ^ (h >> np.uint32(16))

    return h.astype(np.float64) / np.float64(0xFFFFFFFF)


def _hash_3d(x: int, y: int, z: int, seed: int) -> float:
    """3D 哈希 → [0, 1) — 备用，当前未使用

    与 _hash_2d 逐位一致：每步 & 0xFFFFFFFF。
    """
    s = seed & 0xFFFFFFFF
    h = (x * 374761393 + y * 668265263 + z * 2246822519
         + s * 1442695040) & 0xFFFFFFFF
    h = ((h ^ (h >> 13)) * 1274126177) & 0xFFFFFFFF
    h = (h ^ (h >> 16)) & 0xFFFFFFFF
    return h / 0xFFFFFFFF


# ─── 平滑插值 ──────────────────────────────────────────────

def _smooth(t: float) -> float:
    """三次 Hermite 平滑：6t^5 - 15t^4 + 10t^3"""
    return t * t * t * (t * (t * 6 - 15) + 10)


def _smooth_vec(t):
    """向量化平滑插值：6t^5 - 15t^4 + 10t^3"""
    return t * t * t * (t * (t * 6 - 15) + 10)


# ─── 值噪声 ────────────────────────────────────────────────

def value_noise_2d(x: float, y: float, seed: int) -> float:
    """2D 值噪声，输出 [0, 1)。基于网格哈希 + 平滑插值。"""
    ix, iy = math.floor(x), math.floor(y)
    fx, fy = x - ix, y - iy

    sx = _smooth(fx)
    sy = _smooth(fy)

    v00 = _hash_2d(ix, iy, seed)
    v10 = _hash_2d(ix + 1, iy, seed)
    v01 = _hash_2d(ix, iy + 1, seed)
    v11 = _hash_2d(ix + 1, iy + 1, seed)

    v0 = v00 + (v10 - v00) * sx
    v1 = v01 + (v11 - v01) * sx
    return v0 + (v1 - v0) * sy


def value_noise_2d_vec(x, y, seed):
    """向量化 2D 值噪声，输出 [0, 1)
    x, y: numpy 浮点数组（噪声坐标）
    seed: 标量 int
    使用 np.floor（比 int() 截断更正确，负数坐标不会产生越界平滑步）。
    """
    x = np.asarray(x, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)

    ix = np.floor(x).astype(np.int32)
    iy = np.floor(y).astype(np.int32)
    fx = x - ix
    fy = y - iy

    sx = _smooth_vec(fx)
    sy = _smooth_vec(fy)

    min_ix = int(ix.min())
    max_ix = int(ix.max()) + 1
    min_iy = int(iy.min())
    max_iy = int(iy.max()) + 1
    gw = max_ix - min_ix + 1
    gh = max_iy - min_iy + 1
    if gw * gh <= 2_000_000 and gw * gh <= 4 * ix.size:
        gx = np.arange(min_ix, max_ix + 1, dtype=np.int32)
        gy = np.arange(min_iy, max_iy + 1, dtype=np.int32)
        grid = _hash_2d_vec(gx[:, None], gy[None, :], seed)
        c0 = ix - min_ix
        r0 = iy - min_iy
        v00 = grid[c0, r0]
        v10 = grid[c0 + 1, r0]
        v01 = grid[c0, r0 + 1]
        v11 = grid[c0 + 1, r0 + 1]
    else:
        v00 = _hash_2d_vec(ix, iy, seed)
        v10 = _hash_2d_vec(ix + 1, iy, seed)
        v01 = _hash_2d_vec(ix, iy + 1, seed)
        v11 = _hash_2d_vec(ix + 1, iy + 1, seed)

    v0 = v00 + (v10 - v00) * sx
    v1 = v01 + (v11 - v01) * sx
    return v0 + (v1 - v0) * sy


def value_noise_3d(x: float, y: float, z: float, seed: int) -> float:
    """3D 值噪声，输出 [0, 1) — 备用，当前未使用"""
    ix, iy, iz = math.floor(x), math.floor(y), math.floor(z)
    fx, fy, fz = x - ix, y - iy, z - iz

    sx = _smooth(fx)
    sy = _smooth(fy)
    sz = _smooth(fz)

    v000 = _hash_3d(ix, iy, iz, seed)
    v100 = _hash_3d(ix+1, iy, iz, seed)
    v010 = _hash_3d(ix, iy+1, iz, seed)
    v110 = _hash_3d(ix+1, iy+1, iz, seed)
    v001 = _hash_3d(ix, iy, iz+1, seed)
    v101 = _hash_3d(ix+1, iy+1, iz+1, seed)
    v011 = _hash_3d(ix, iy+1, iz+1, seed)
    v111 = _hash_3d(ix+1, iy+1, iz+1, seed)

    v00 = v000 + (v100 - v000) * sx
    v01 = v001 + (v101 - v001) * sx
    v10 = v010 + (v110 - v010) * sx
    v11 = v011 + (v111 - v011) * sx

    v0 = v00 + (v10 - v00) * sy
    v1 = v01 + (v11 - v01) * sy

    return v0 + (v1 - v0) * sz


# ─── 多级噪声（每级参数自由定制，级数任意） ──────────────────
#
# 一级 = (频率倍率, 振幅, 种子偏移)
#   频率倍率：作用在调用方已缩放的坐标上（x * freq）
#   振幅：    该级贡献的权重（归一化时计入振幅总和）
#   种子偏移：加到基础 seed 上，隔离各级哈希
#
# levels 数量任意：0 级 → 常数 0；1 级 → 等价单级噪声；N 级 → N 级叠加。
# 输出已归一化到 [0, 1)。

def fbm_levels_2d(x: float, y: float, seed: int, levels) -> float:
    """多级 2D 噪声，输出 [0, 1)。levels 级数任意。

    值噪声插值在此内联（逐算术步骤与 value_noise_2d 完全一致，
    只省掉逐级函数调用开销；标量噪声是寻路/敌人生成的热路径）。
    """
    total = 0.0
    max_val = 0.0
    floor = math.floor
    hash2d = _hash_2d
    for freq, amp, soff in levels:
        nx = x * freq
        ny = y * freq
        ix = floor(nx)
        iy = floor(ny)
        s = seed + soff
        v00 = hash2d(ix, iy, s)
        v10 = hash2d(ix + 1, iy, s)
        v01 = hash2d(ix, iy + 1, s)
        v11 = hash2d(ix + 1, iy + 1, s)
        fx = nx - ix
        fy = ny - iy
        # 内联 _smooth：t*t*t*(t*(t*6-15)+10)，与 _smooth(fx/fy) 逐位一致
        sx = fx * fx * fx * (fx * (fx * 6 - 15) + 10)
        sy = fy * fy * fy * (fy * (fy * 6 - 15) + 10)
        v0 = v00 + (v10 - v00) * sx
        v1 = v01 + (v11 - v01) * sx
        total += (v0 + (v1 - v0) * sy) * amp
        max_val += amp
    return total / max_val if max_val > 0 else 0.0


def fbm_levels_2d_vec(x, y, seed, levels):
    """向量化多级 2D 噪声，输出 [0, 1)
    x, y: numpy 浮点数组（调用方已缩放的噪声坐标）
    levels: [(频率倍率, 振幅, 种子偏移), ...]，级数任意
    """
    x = np.asarray(x, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)

    total = np.zeros_like(x)
    max_val = 0.0
    for freq, amp, soff in levels:
        total += value_noise_2d_vec(x * freq, y * freq, seed + soff) * amp
        max_val += amp
    return total / max_val if max_val > 0 else total


# ─── 逐点参数版多级噪声（振幅/频率由外部噪声场逐点调制） ─────
#
# 与 fbm_levels_2d 相同的叠加结构，但 mod_levels 指定的层级其振幅乘
# amp_scale、频率乘 freq_scale —— 两个缩放量可以是标量，也可以是与
# x/y 同形状的 numpy 数组（"每一点的参数都不一样"）。
# 典型用法：先用一张低频噪声场 P 算出 amp_scale/freq_scale，再调本函数，
# 实现"地形的噪声参数本身也是噪声"。amp_scale = freq_scale = 1.0 时
# 与 fbm_levels_2d 逐位一致（乘 1.0 是 IEEE 精确运算）。

def fbm_levels_param_2d(x: float, y: float, seed: int, levels,
                        mod_levels, amp_scale, freq_scale) -> float:
    """多级 2D 噪声，指定层级逐点调制振幅/频率。输出 [0, 1)。

    levels: [(频率倍率, 振幅, 种子偏移), ...]
    mod_levels: 参与调制的层级索引集合（如 (1, 2)；其余层级保持原参数）
    amp_scale / freq_scale: 标量缩放量

    值噪声插值在此内联（与 fbm_levels_2d 相同的理由，逐位一致）。
    """
    mod = set(mod_levels)
    total = 0.0
    max_val = 0.0
    floor = math.floor
    hash2d = _hash_2d
    for i, (freq, amp, soff) in enumerate(levels):
        if i in mod:
            f = freq * freq_scale
            a = amp * amp_scale
        else:
            f = freq
            a = amp
        nx = x * f
        ny = y * f
        ix = floor(nx)
        iy = floor(ny)
        s = seed + soff
        v00 = hash2d(ix, iy, s)
        v10 = hash2d(ix + 1, iy, s)
        v01 = hash2d(ix, iy + 1, s)
        v11 = hash2d(ix + 1, iy + 1, s)
        fx = nx - ix
        fy = ny - iy
        sx = fx * fx * fx * (fx * (fx * 6 - 15) + 10)
        sy = fy * fy * fy * (fy * (fy * 6 - 15) + 10)
        v0 = v00 + (v10 - v00) * sx
        v1 = v01 + (v11 - v01) * sx
        total += (v0 + (v1 - v0) * sy) * a
        max_val += a
    return total / max_val if max_val > 0 else 0.0


def fbm_levels_param_2d_vec(x, y, seed, levels,
                            mod_levels, amp_scale, freq_scale):
    """向量化逐点参数版多级 2D 噪声，输出 [0, 1)。

    x, y: numpy 浮点数组（调用方已缩放的噪声坐标）
    amp_scale / freq_scale: 标量或与 x 同形状的 numpy 数组
    （与标量版逐位一致：同序叠加，归一化分母同步逐点缩放）
    """
    x = np.asarray(x, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)
    amp_scale = np.asarray(amp_scale, dtype=np.float64)
    freq_scale = np.asarray(freq_scale, dtype=np.float64)

    mod = set(mod_levels)
    total = np.zeros_like(x)
    max_val = np.zeros_like(x)
    for i, (freq, amp, soff) in enumerate(levels):
        if i in mod:
            f = freq * freq_scale
            a = amp * amp_scale
        else:
            f = freq
            a = amp
        total += value_noise_2d_vec(x * f, y * f, seed + soff) * a
        max_val = max_val + a
    safe = np.where(max_val > 0, max_val, 1.0)
    return np.where(max_val > 0, total / safe, 0.0)


def fbm_levels_3d(x: float, y: float, z: float, seed: int, levels) -> float:
    """多级 3D 噪声，输出 [0, 1) — 备用，当前未使用"""
    total = 0.0
    max_val = 0.0
    for freq, amp, soff in levels:
        total += value_noise_3d(x * freq, y * freq, z * freq, seed + soff) * amp
        max_val += amp
    return total / max_val if max_val > 0 else 0.0


def _geometric_levels(octaves: int, persistence: float = 0.5,
                      frequency: float = 1.0, lacunarity: float = 2.0):
    """几何级数 levels：第 0 级返回 (frequency, 1.0, 0)，其余逐级
    频率 ×lacunarity、振幅 ×persistence、种子偏移 +1000。

    octaves <= 0 返回空表（等价于经典 FBM 的常数 0 分支）。
    供 fbm_2d / fbm_2d_vec / fbm_3d 包装使用，保证经典多八度行为不变。
    """
    if octaves <= 0:
        return []
    levels = [(frequency, 1.0, 0)]
    amp = persistence
    freq = frequency * lacunarity
    for i in range(1, octaves):
        levels.append((freq, amp, i * 1000))
        freq *= lacunarity
        amp *= persistence
    return levels


# ─── 经典多八度 FBM（几何级数薄包装，输出不变） ──────────────

def fbm_2d(x: float, y: float, seed: int,
           octaves: int = 4, persistence: float = 0.5,
           frequency: float = 1.0, lacunarity: float = 2.0) -> float:
    """2D 多八度 FBM（几何级数），输出 [0, 1)。

    等价于 fbm_levels_2d(x, y, seed, _geometric_levels(...))。
    新的调用点请直接使用 fbm_levels_2d 并在 config.NOISE_CFG 中逐级定制参数。
    """
    levels = _geometric_levels(octaves, persistence, frequency, lacunarity)
    return fbm_levels_2d(x, y, seed, levels)


def fbm_2d_vec(x, y, seed, octaves=4, persistence=0.5,
               frequency=1.0, lacunarity=2.0):
    """向量化 2D 多八度 FBM（几何级数），输出 [0, 1)
    x, y: numpy 浮点数组
    """
    levels = _geometric_levels(octaves, persistence, frequency, lacunarity)
    return fbm_levels_2d_vec(x, y, seed, levels)


def fbm_3d(x: float, y: float, z: float, seed: int,
           octaves: int = 3, persistence: float = 0.5,
           frequency: float = 1.0, lacunarity: float = 2.0) -> float:
    """3D 多八度 FBM（几何级数） — 备用，当前未使用"""
    levels = _geometric_levels(octaves, persistence, frequency, lacunarity)
    return fbm_levels_3d(x, y, z, seed, levels)


# ─── 高度图生成 ────────────────────────────────────────────

def generate_heightmap(width: int, height: int, seed: int,
                        scale: float = 0.05, octaves: int = 4,
                        persistence: float = 0.5, levels=None) -> list:
    """生成 2D 高度图，返回 list[list[float]]，每个值在 [0, 1)

    levels 为 None 时走经典多八度（octaves/persistence）；
    传入 [(频率倍率, 振幅, 种子偏移), ...] 则使用多级噪声（级数任意）。
    """
    grid = []
    for y in range(height):
        row = []
        for x in range(width):
            px, py = x * scale, y * scale
            if levels is None:
                h = fbm_2d(px, py, seed, octaves=octaves, persistence=persistence)
            else:
                h = fbm_levels_2d(px, py, seed, levels)
            row.append(h)
        grid.append(row)
    return grid
