"""
biomes.py - 生物群系定义
从 world.py 提取以打破 chunk.py ↔ world.py 循环导入
"""

from dataclasses import dataclass
from blocks import BLOCK_ID
import config


@dataclass(frozen=True)
class BiomeDef:
    """生物群系定义"""
    name: str
    temp_min: float
    temp_max: float
    rain_chance: float
    snow_chance: float
    surface_block: int
    underground_block: int
    sky_color: tuple


BIOMES = {
    "desert": BiomeDef(
        name="沙漠", temp_min=25.0, temp_max=50.0,
        rain_chance=0.02, snow_chance=0.0,
        surface_block=BLOCK_ID["sand"], underground_block=BLOCK_ID["sand"],
        sky_color=(180, 200, 220),
    ),
    "plains": BiomeDef(
        name="平原", temp_min=5.0, temp_max=28.0,
        rain_chance=0.12, snow_chance=0.0,
        surface_block=BLOCK_ID["grass"], underground_block=BLOCK_ID["dirt"],
        sky_color=(130, 180, 230),
    ),
    "forest": BiomeDef(
        name="森林", temp_min=5.0, temp_max=25.0,
        rain_chance=0.25, snow_chance=0.0,
        surface_block=BLOCK_ID["grass"], underground_block=BLOCK_ID["dirt"],
        sky_color=(110, 170, 210),
    ),
    "savanna": BiomeDef(
        name="热带草原", temp_min=20.0, temp_max=35.0,
        rain_chance=0.08, snow_chance=0.0,
        surface_block=BLOCK_ID["grass"], underground_block=BLOCK_ID["dirt"],
        sky_color=(160, 190, 220),
    ),
    "tundra": BiomeDef(
        name="苔原", temp_min=-10.0, temp_max=5.0,
        rain_chance=0.10, snow_chance=0.40,
        surface_block=BLOCK_ID["snow"], underground_block=BLOCK_ID["dirt"],
        sky_color=(160, 175, 195),
    ),
    "snowy": BiomeDef(
        name="雪原", temp_min=-60.0, temp_max=-10.0,
        rain_chance=0.05, snow_chance=0.80,
        surface_block=BLOCK_ID["snow"], underground_block=BLOCK_ID["ice"],
        sky_color=(170, 185, 210),
    ),
    "mountain": BiomeDef(
        name="山脉", temp_min=-20.0, temp_max=15.0,
        rain_chance=0.15, snow_chance=0.30,
        surface_block=BLOCK_ID["stone"], underground_block=BLOCK_ID["stone"],
        sky_color=(150, 170, 200),
    ),
    "ocean": BiomeDef(
        name="海洋", temp_min=-10.0, temp_max=30.0,
        rain_chance=0.20, snow_chance=0.0,
        surface_block=BLOCK_ID["water"], underground_block=BLOCK_ID["water"],
        sky_color=(100, 150, 200),
    ),
}