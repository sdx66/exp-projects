"""
noise.py - 程序化噪声生成
纯 Python 实现，可种子化，确定性输出。
使用值噪声 + 多八度 FBM，生成自然地形。
"""

# ─── 哈希函数 ──────────────────────────────────────────────
# 将 (x, y, z, seed) 映射到 [0, 1) 的确定性伪随机值
# 基于整数混合，无浮点误差

def _hash_2d(x: int, y: int, seed: int) -> float:
    """2D 哈希 → [0, 1)"""
    h = x * 374761393 + y * 668265263 + seed * 1442695040
    h = (h ^ (h >> 13)) * 1274126177
    h = h ^ (h >> 16)
    return (h & 0xFFFFFFFF) / 0xFFFFFFFF

def _hash_3d(x: int, y: int, z: int, seed: int) -> float:
    """3D 哈希 → [0, 1)"""
    h = x * 374761393 + y * 668265263 + z * 2246822519 + seed * 1442695040
    h = (h ^ (h >> 13)) * 1274126177
    h = h ^ (h >> 16)
    return (h & 0xFFFFFFFF) / 0xFFFFFFFF

# ─── 平滑插值 ──────────────────────────────────────────────

def _smooth(t: float) -> float:
    """三次 Hermite 平滑：6t^5 - 15t^4 + 10t^3"""
    return t * t * t * (t * (t * 6 - 15) + 10)

# ─── 值噪声 ────────────────────────────────────────────────

def value_noise_2d(x: float, y: float, seed: int) -> float:
    """
    2D 值噪声，输出 [0, 1)
    基于网格哈希 + 平滑插值
    """
    ix, iy = int(x), int(y)
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

def value_noise_3d(x: float, y: float, z: float, seed: int) -> float:
    """
    3D 值噪声，输出 [0, 1)
    """
    ix, iy, iz = int(x), int(y), int(z)
    fx, fy, fz = x - ix, y - iy, z - iz

    sx = _smooth(fx)
    sy = _smooth(fy)
    sz = _smooth(fz)

    v000 = _hash_3d(ix, iy, iz, seed)
    v100 = _hash_3d(ix+1, iy, iz, seed)
    v010 = _hash_3d(ix, iy+1, iz, seed)
    v110 = _hash_3d(ix+1, iy+1, iz, seed)
    v001 = _hash_3d(ix, iy, iz+1, seed)
    v101 = _hash_3d(ix+1, iy, iz+1, seed)
    v011 = _hash_3d(ix, iy+1, iz+1, seed)
    v111 = _hash_3d(ix+1, iy+1, iz+1, seed)

    # 线性插值 X
    v00 = v000 + (v100 - v000) * sx
    v01 = v001 + (v101 - v001) * sx
    v10 = v010 + (v110 - v010) * sx
    v11 = v011 + (v111 - v011) * sx

    # 线性插值 Y
    v0 = v00 + (v10 - v00) * sy
    v1 = v01 + (v11 - v01) * sy

    # 线性插值 Z
    return v0 + (v1 - v0) * sz

# ─── 分形布朗运动 (FBM) ────────────────────────────────────

def fbm_2d(x: float, y: float, seed: int,
           octaves: int = 4, persistence: float = 0.5,
           frequency: float = 1.0, lacunarity: float = 2.0) -> float:
    """
    2D FBM：多八度噪声叠加
    输出 [0, 1)
    octaves:     八度数（越多越精细，但越慢）
    persistence: 衰减率（0-1，越小衰减越快）
    frequency:   基础频率
    lacunarity:  频率倍增因子
    """
    total = 0.0
    amplitude = 1.0
    max_val = 0.0

    for i in range(octaves):
        total += value_noise_2d(x * frequency, y * frequency, seed + i * 1000) * amplitude
        max_val += amplitude
        amplitude *= persistence
        frequency *= lacunarity

    return total / max_val if max_val > 0 else 0.0

def fbm_3d(x: float, y: float, z: float, seed: int,
           octaves: int = 3, persistence: float = 0.5,
           frequency: float = 1.0, lacunarity: float = 2.0) -> float:
    """
    3D FBM：多八度噪声叠加
    输出 [0, 1)
    """
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
    """
    生成 2D 高度图
    返回 list[list[float]]，每个值在 [0, 1)
    scale: 噪声缩放（越小 = 地形越平坦，越大 = 地形越崎岖）
    """
    grid = []
    for y in range(height):
        row = []
        for x in range(width):
            h = fbm_2d(x * scale, y * scale, seed,
                       octaves=octaves, persistence=persistence)
            row.append(h)
        grid.append(row)
    return grid
