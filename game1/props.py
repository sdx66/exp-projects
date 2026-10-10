"""
props.py - 地表装饰物系统
独立的装饰物层，不写入 terrain[x][z][y] 数组。
支持按生物群系散布、确定性生成、采集与移除。
"""

from dataclasses import dataclass
from typing import Optional
from blocks import BLOCK_ID


@dataclass(frozen=True)
class PropType:
    """装饰物类型定义"""
    name: str
    shape: str                              # 渲染形状
    color: tuple                            # 主色 (R,G,B)
    biomes: tuple                           # 可生成的生物群系
    spawn_chance: float                     # 每个格子生成概率
    harvest_material: Optional[str] = None  # 采集所得材料名，None=纯装饰
    harvest_amount: int = 0
    min_size: float = 0.4
    max_size: float = 0.8


@dataclass
class Prop:
    """单个装饰物实例"""
    x: int
    z: int
    y: float
    prop_type: PropType
    size: float
    harvested: bool = False


# ─── 装饰物类型注册表 ──────────────────────────────────────

# 复用 blocks.py 的颜色定义
LOG_COLOR = (100, 75, 40)
LEAVES_COLOR = (60, 140, 50)
STONE_COLOR = (145, 145, 145)
DARK_STONE_COLOR = (95, 95, 95)
IRON_ORE_COLOR = (160, 140, 120)
COAL_ORE_COLOR = (70, 70, 70)
GRASS_COLOR = (76, 175, 80)
SAND_COLOR = (219, 207, 163)
CACTUS_COLOR = (60, 130, 60)
DEAD_TREE_COLOR = (80, 70, 55)
SNOW_COLOR = (240, 245, 255)
BUSH_COLOR = (50, 120, 40)
FLOWER_COLORS = [(255, 200, 50), (220, 80, 120), (180, 100, 220), (255, 255, 255)]


PROP_TYPES: dict = {
    # ── 可采集 ──
    "tree": PropType(
        name="tree", shape="tree", color=LEAVES_COLOR,
        biomes=("forest", "savanna"), spawn_chance=0.10,
        harvest_material="wood", harvest_amount=2,
        min_size=1.0, max_size=2.0,
    ),
    "oak_sparse": PropType(
        name="oak_sparse", shape="tree", color=LEAVES_COLOR,
        biomes=("plains",), spawn_chance=0.02,
        harvest_material="wood", harvest_amount=2,
        min_size=1.0, max_size=1.5,
    ),
    "rock": PropType(
        name="rock", shape="rock", color=STONE_COLOR,
        biomes=("mountain", "tundra", "snowy", "savanna", "desert"),
        spawn_chance=0.05,
        harvest_material="stone", harvest_amount=2,
        min_size=0.5, max_size=1.0,
    ),
    "ore_vein": PropType(
        name="ore_vein", shape="ore_vein", color=IRON_ORE_COLOR,
        biomes=("mountain",), spawn_chance=0.02,
        harvest_material="iron_ore", harvest_amount=1,
        min_size=0.6, max_size=0.9,
    ),
    "coal_vein": PropType(
        name="coal_vein", shape="ore_vein", color=COAL_ORE_COLOR,
        biomes=("mountain", "tundra"), spawn_chance=0.015,
        harvest_material="iron_ore", harvest_amount=1,  # 煤矿也产铁（简化）
        min_size=0.6, max_size=0.8,
    ),
    "cactus": PropType(
        name="cactus", shape="cactus", color=CACTUS_COLOR,
        biomes=("desert", "savanna"), spawn_chance=0.06,
        harvest_material="wood", harvest_amount=1,  # 沙漠缺木，仙人掌替代
        min_size=1.0, max_size=2.0,
    ),
    "sand_mound": PropType(
        name="sand_mound", shape="sand_mound", color=SAND_COLOR,
        biomes=("desert",), spawn_chance=0.08,
        harvest_material="sand", harvest_amount=2,
        min_size=0.6, max_size=1.5,
    ),
    "bush": PropType(
        name="bush", shape="bush", color=BUSH_COLOR,
        biomes=("forest", "plains", "savanna"), spawn_chance=0.06,
        harvest_material="wood", harvest_amount=1,
        min_size=0.4, max_size=0.8,
    ),
    "dead_tree": PropType(
        name="dead_tree", shape="dead_tree", color=DEAD_TREE_COLOR,
        biomes=("tundra", "snowy"), spawn_chance=0.04,
        harvest_material="wood", harvest_amount=1,
        min_size=1.0, max_size=1.5,
    ),

    # ── 纯装饰（不可采集） ──
    "grass_tuft": PropType(
        name="grass_tuft", shape="grass_tuft", color=GRASS_COLOR,
        biomes=("plains", "forest", "savanna"), spawn_chance=0.20,
        min_size=0.2, max_size=0.5,
    ),
    "flower": PropType(
        name="flower", shape="flower", color=(255, 200, 50),
        biomes=("plains", "savanna"), spawn_chance=0.05,
        min_size=0.15, max_size=0.3,
    ),
    "snow_patch": PropType(
        name="snow_patch", shape="snow_patch", color=SNOW_COLOR,
        biomes=("tundra", "snowy", "mountain"), spawn_chance=0.08,
        min_size=0.4, max_size=1.2,
    ),
}


def get_harvestable_types() -> dict:
    """返回所有可采集的装饰物类型"""
    return {k: v for k, v in PROP_TYPES.items() if v.harvest_material is not None}


def get_all_types() -> dict:
    """返回所有装饰物类型"""
    return PROP_TYPES
