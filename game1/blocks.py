"""
blocks.py - 方块类型定义
Minecraft 风格方块系统，所有属性在代码中定义。
每个方块有唯一 ID，3 种面颜色（上/侧/下），以及物理属性。
"""

from dataclasses import dataclass, field
from typing import Optional

# ─── 方块 ID ────────────────────────────────────────────────
# 用整数存储地形数组，节省内存

BLOCK_ID = {
    "air":          0,
    "grass":        1,
    "dirt":         2,
    "stone":        3,
    "sand":         4,
    "water":        5,
    "snow":         6,
    "ice":          7,
    "lava":         8,
    "bedrock":      9,
    "iron_ore":     10,
    "coal_ore":     11,
    "clay":         12,
    "gravel":       13,
    "log":          14,
    "leaves":       15,
}

BLOCK_NAME = {v: k for k, v in BLOCK_ID.items()}

# ─── 方块定义 ──────────────────────────────────────────────

@dataclass(frozen=True)
class BlockDef:
    """方块定义（不可变）"""
    id: int
    name: str
    # 面颜色 (R, G, B) 0-255
    top_color: tuple
    side_color: tuple
    bottom_color: tuple
    # 物理属性
    solid: bool = True
    transparent: bool = False
    emissive: bool = False       # 自发光（如岩浆）
    # 生物群系关联
    biomes: tuple = ()           # 此方块可出现的生物群系
    # 温度范围 (°C)
    temp_min: float = -100.0
    temp_max: float = 100.0
    # 纹理噪声参数（用于动态纹理生成）
    texture_noise: float = 0.0   # 0=纯色, >0=噪声强度

# ─── 方块注册表 ────────────────────────────────────────────

BLOCKS: dict = {}

def _register(defn: BlockDef):
    BLOCKS[defn.id] = defn

# 定义所有方块

_register(BlockDef(
    id=BLOCK_ID["air"], name="空气",
    top_color=(0, 0, 0), side_color=(0, 0, 0), bottom_color=(0, 0, 0),
    solid=False, transparent=True,
    biomes=("all",),
))

_register(BlockDef(
    id=BLOCK_ID["grass"], name="草方块",
    top_color=(76, 175, 80), side_color=(121, 85, 72), bottom_color=(121, 85, 72),
    solid=True, biomes=("forest", "plains", "savanna"),
    temp_min=0.0, temp_max=35.0,
    texture_noise=0.15,
))

_register(BlockDef(
    id=BLOCK_ID["dirt"], name="泥土",
    top_color=(121, 85, 72), side_color=(121, 85, 72), bottom_color=(121, 85, 72),
    solid=True, biomes=("all",),
    texture_noise=0.20,
))

_register(BlockDef(
    id=BLOCK_ID["stone"], name="石头",
    top_color=(145, 145, 145), side_color=(120, 120, 120), bottom_color=(95, 95, 95),
    solid=True, biomes=("all",),
    texture_noise=0.25,
))

_register(BlockDef(
    id=BLOCK_ID["sand"], name="沙子",
    top_color=(219, 207, 163), side_color=(219, 207, 163), bottom_color=(219, 207, 163),
    solid=True, biomes=("desert", "beach", "river"),
    temp_min=10.0, temp_max=50.0,
    texture_noise=0.10,
))

_register(BlockDef(
    id=BLOCK_ID["water"], name="水",
    top_color=(55, 125, 195), side_color=(45, 105, 175), bottom_color=(35, 85, 155),
    solid=False, transparent=True,
    biomes=("ocean", "river", "lake"),
    texture_noise=0.08,
))

_register(BlockDef(
    id=BLOCK_ID["snow"], name="雪",
    top_color=(240, 245, 255), side_color=(220, 228, 240), bottom_color=(200, 210, 230),
    solid=True, biomes=("tundra", "snowy", "mountain"),
    temp_min=-100.0, temp_max=0.0,
    texture_noise=0.05,
))

_register(BlockDef(
    id=BLOCK_ID["ice"], name="冰",
    top_color=(170, 210, 235), side_color=(155, 200, 225), bottom_color=(140, 190, 215),
    solid=True, transparent=True,
    biomes=("tundra", "snowy"),
    temp_min=-100.0, temp_max=0.0,
    texture_noise=0.05,
))

_register(BlockDef(
    id=BLOCK_ID["lava"], name="岩浆",
    top_color=(220, 80, 20), side_color=(200, 60, 10), bottom_color=(180, 50, 5),
    solid=False, transparent=False, emissive=True,
    biomes=("volcano",),
    temp_min=50.0, temp_max=100.0,
    texture_noise=0.30,
))

_register(BlockDef(
    id=BLOCK_ID["bedrock"], name="基岩",
    top_color=(70, 70, 70), side_color=(50, 50, 50), bottom_color=(30, 30, 30),
    solid=True, biomes=("all",),
    texture_noise=0.40,
))

_register(BlockDef(
    id=BLOCK_ID["iron_ore"], name="铁矿",
    top_color=(160, 140, 120), side_color=(140, 120, 100), bottom_color=(120, 100, 80),
    solid=True, biomes=("mountain", "stone"),
    texture_noise=0.35,
))

_register(BlockDef(
    id=BLOCK_ID["coal_ore"], name="煤矿",
    top_color=(80, 80, 80), side_color=(70, 70, 70), bottom_color=(60, 60, 60),
    solid=True, biomes=("mountain", "stone"),
    texture_noise=0.35,
))

_register(BlockDef(
    id=BLOCK_ID["clay"], name="黏土",
    top_color=(160, 160, 180), side_color=(140, 140, 160), bottom_color=(120, 120, 140),
    solid=True, biomes=("river", "beach"),
    texture_noise=0.10,
))

_register(BlockDef(
    id=BLOCK_ID["gravel"], name="砾石",
    top_color=(130, 125, 120), side_color=(110, 105, 100), bottom_color=(90, 85, 80),
    solid=True, biomes=("mountain", "desert"),
    texture_noise=0.30,
))

_register(BlockDef(
    id=BLOCK_ID["log"], name="原木",
    top_color=(120, 90, 50), side_color=(100, 75, 40), bottom_color=(120, 90, 50),
    solid=True, biomes=("forest",),
    texture_noise=0.20,
))

_register(BlockDef(
    id=BLOCK_ID["leaves"], name="树叶",
    top_color=(60, 140, 50), side_color=(50, 120, 40), bottom_color=(40, 100, 30),
    solid=False, transparent=True,
    biomes=("forest",),
    texture_noise=0.25,
))

# ─── 便捷访问 ──────────────────────────────────────────────

def get_block(id: int) -> BlockDef:
    """通过 ID 获取方块定义"""
    return BLOCKS.get(id, BLOCKS[BLOCK_ID["air"]])

def get_block_by_name(name: str) -> BlockDef:
    """通过名称获取方块定义"""
    return BLOCKS.get(BLOCK_ID[name], BLOCKS[BLOCK_ID["air"]])

def is_solid(id: int) -> bool:
    """方块是否实体"""
    return BLOCKS[id].solid

def is_transparent(id: int) -> bool:
    """方块是否透明"""
    return BLOCKS[id].transparent
