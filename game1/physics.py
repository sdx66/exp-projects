"""
physics.py - 物理工具函数
MVP 版本：物理逻辑已集成在 entities.py 的 Projectile 类中。
此文件提供辅助工具。
"""

import config


def vector3_subtract(a, b):
    """向量减法"""
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def vector3_length(v):
    """向量长度"""
    return (v[0] ** 2 + v[1] ** 2 + v[2] ** 2) ** 0.5


def vector3_normalize(v):
    """向量归一化"""
    l = vector3_length(v)
    if l > 0:
        return (v[0] / l, v[1] / l, v[2] / l)
    return (0, 0, 0)


def vector3_distance(a, b):
    """两点距离"""
    return vector3_length(vector3_subtract(a, b))


def distance_2d(a, b):
    """2D 距离（XZ 平面）"""
    dx = a[0] - b[0]
    dz = a[2] - b[2]
    return (dx * dx + dz * dz) ** 0.5


def lerp(a, b, t):
    """线性插值"""
    return a + (b - a) * t


def clamp(v, min_val, max_val):
    """值限制"""
    return max(min_val, min(max_val, v))
