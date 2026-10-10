"""
noise.py - 程序化噪声生成
纯 Python + numpy 混合实现，可种子化，确定性输出。
使用值噪声 + 多八度 FBM，生成自然地形。

提供两套 API：
  - 标量版（value_noise_2d, fbm_2d）：单点查询，用于路径生成等低频调用
  - 向量化版（value_noise_2d_vec, fbm_2d_vec）：批量查询，用于区块生成热路径
"""

import math
import numpy as np

# ─── 哈希函数（标量版，带 FIFO 缓存）─────────────────────
_HASH_CACHE_MAX = 200000
_hash_cache: dict = {}

UINT32_MAX = np.uint64(0xFFFFFFFF)


def _hash_2d(x: int, y: int, seed: int) -> float:
    """2D 哈希 → [0, 1)，带格点 FIFO 缓存（标量版）"""
    key = (x, y, seed)
    cached = _hash_cache.get(key)
    if cached is not None:
        return cached
    h = x * 374761393 + y * 668265263 + seed * 1442695040
    h = (h ^ (h >> 13)) * 1274126177
    h = h ^ (h >> 16)
    result = (h & 0xFFFFFFFF) / 0xFFFFFFFF
    _hash_cache[key] = result
    if len(_hash_cache) > _HASH_CACHE_MAX:
        _hash_cache.pop(next(iter(_hash_cache)))
    return result


def _hash_2d_vec(ix, iy, seed):
    """向量化 2D 哈希 → [0, 1)
    ix, iy: numpy 整数数组（格点坐标）
    seed: 标量 int
    使用 uint64 + 32 位掩码保证确定性。
    """
    ix = np.asarray(ix, dtype=np.uint64)
    iy = np.asarray(iy, dtype=np.uint64)
    s = np.uint64(seed & 0xFFFFFFFF)

    h = (ix * np.uint64(374761393)
         + iy * np.uint64(668265263)
         + s * np.uint64(1442695040)) & UINT32_MAX
    h = (h ^ (h >> np.uint64(13))) & UINT32_MAX
    h = (h * np.uint64(1274126177)) & UINT32_MAX
    h = (h ^ (h >> np.uint64(16))) & UINT32_MAX

    return h.astype(np.float64) / np.float64(0xFFFFFFFF)


def _hash_3d(x: int, y: int, z: int, seed: int) -> float:
    """3D 哈希 → [0, 1) — 备用，当前未使用"""
    h = x * 374761393 + y * 668265263 + z * 2246822519 + seed * 1442695040
    h = (h ^ (h >> 13)) * 1274126177
    h = h ^ (h >> 16)
    return (h & 0xFFFFFFFF) / 0xFFFFFFFF


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


# ─── 分形布朗运动 (FBM) ────────────────────────────────────

def fbm_2d(x: float, y: float, seed: int,
           octaves: int = 4, persistence: float = 0.5,
           frequency: float = 1.0, lacunarity: float = 2.0) -> float:
    """2D FBM：多八度噪声叠加，输出 [0, 1)"""
    total = 0.0
    amplitude = 1.0
    max_val = 0.0

    for i in range(octaves):
        total += value_noise_2d(x * frequency, y * frequency, seed + i * 1000) * amplitude
        max_val += amplitude
        amplitude *= persistence
        frequency *= lacunarity

    return total / max_val if max_val > 0 else 0.0


def fbm_2d_vec(x, y, seed, octaves=4, persistence=0.5,
               frequency=1.0, lacunarity=2.0):
    """向量化 2D FBM，输出 [0, 1)
    x, y: numpy 浮点数组
    """
    x = np.asarray(x, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)

    total = np.zeros_like(x)
    amplitude = 1.0
    max_val = 0.0

    for i in range(octaves):
        total += value_noise_2d_vec(x * frequency, y * frequency, seed + i * 1000) * amplitude
        max_val += amplitude
        amplitude *= persistence
        frequency *= lacunarity

    return total / max_val if max_val > 0 else total


def fbm_3d(x: float, y: float, z: float, seed: int,
           octaves: int = 3, persistence: float = 0.5,
           frequency: float = 1.0, lacunarity: float = 2.0) -> float:
    """3D FBM：多八度噪声叠加 — 备用，当前未使用"""
    total = 0.0
    amplitude = 1.0
    max_val = 0.0

    for i in range(octaves):
        total += value_noise_3d(x * frequency, y * frequency, z * frequency, seed + i * 1000) * amplitude
        max_val += amplitude
        amplitude *= persistence
        frequency *= lacunarity

    return total / max_val if max_val > 0 else 0.0


# ─── 高度图生成 ────────────────────────────────────────────

def generate_heightmap(width: int, height: int, seed: int,
                        scale: float = 0.05, octaves: int = 4,
                        persistence: float = 0.5) -> list:
    """生成 2D 高度图，返回 list[list[float]]，每个值在 [0, 1)"""
    grid = []
    for y in range(height):
        row = []
        for x in range(width):
            h = fbm_2d(x * scale, y * scale, seed,
                       octaves=octaves, persistence=persistence)
            row.append(h)
        grid.append(row)
    return grid
