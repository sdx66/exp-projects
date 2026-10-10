"""
items.py - 材料/背包/加工/工具/武器定义
材料驱动加工链：原料→精炼→武器/工具
"""

from dataclasses import dataclass, field
from typing import Optional


# ─── 材料定义 ──────────────────────────────────────────────

RAW = {"wood", "stone", "sand", "iron_ore"}
PROCESSED = {"plank", "cobble", "iron_ingot"}
ALL_MATERIALS = RAW | PROCESSED

# 材料颜色（用于 HUD/渲染）
MATERIAL_COLORS = {
    "wood": (140, 100, 50),
    "stone": (145, 145, 145),
    "sand": (219, 207, 163),
    "iron_ore": (160, 140, 120),
    "plank": (180, 130, 70),
    "cobble": (120, 120, 120),
    "iron_ingot": (200, 200, 210),
}

MATERIAL_NAMES_CN = {
    "wood": "木材",
    "stone": "石料",
    "sand": "沙子",
    "iron_ore": "铁矿石",
    "plank": "木板",
    "cobble": "圆石",
    "iron_ingot": "铁锭",
}


# ─── 加工配方 ──────────────────────────────────────────────
# 手：消耗 1 原料 → 产物按 hand 产量
# 工具：消耗 1 原料 → 产物按 tool 产量，更快；某些加工必须有工具
# hand=0 表示徒手不可（如炼铁需熔炉）

PROCESS_RECIPES = {
    "wood->plank": {
        "in": "wood", "out": "plank",
        "hand": 1, "tool": 2,
        "time_hand": 1.0, "time_tool": 0.4,
    },
    "stone->cobble": {
        "in": "stone", "out": "cobble",
        "hand": 1, "tool": 2,
        "time_hand": 1.2, "time_tool": 0.5,
    },
    "iron_ore->ingot": {
        "in": "iron_ore", "out": "iron_ingot",
        "hand": 0, "tool": 1,
        "time_hand": None, "time_tool": 1.5,
        "requires_tool": "furnace",
    },
}


# ─── 工具定义 ──────────────────────────────────────────────
# 工具：一次性合成、永久持有

@dataclass(frozen=True)
class ToolDef:
    name: str
    label_cn: str
    recipe: dict          # 合成配方 {material: amount}
    applies: str          # 适用的加工配方键
    color: tuple


TOOL_TYPES: dict = {
    "saw": ToolDef(
        "saw", "锯子",
        recipe={"plank": 2, "iron_ingot": 1},
        applies="wood->plank",
        color=(180, 180, 190),
    ),
    "hammer": ToolDef(
        "hammer", "锤子",
        recipe={"cobble": 2, "plank": 1},
        applies="stone->cobble",
        color=(120, 120, 120),
    ),
    "furnace": ToolDef(
        "furnace", "熔炉",
        recipe={"cobble": 4},
        applies="iron_ore->ingot",
        color=(180, 100, 50),
    ),
}


# ─── 武器定义 ──────────────────────────────────────────────
# 武器：合成后存入背包，可"装备放置"为塔

@dataclass(frozen=True)
class WeaponDef:
    name: str
    label_cn: str
    recipe: dict          # 精炼材料配方
    damage: float
    range: float
    cooldown: float
    install_fee: int      # 金钱：武器→塔安装费
    special: dict = field(default_factory=dict)
    color: tuple = (128, 128, 128)


WEAPON_TYPES: dict = {
    "wood_club": WeaponDef(
        "wood_club", "木棒",
        recipe={"plank": 2},
        damage=8, range=3.5, cooldown=1.1,
        install_fee=10,
        color=(120, 90, 50),
    ),
    "stone_axe": WeaponDef(
        "stone_axe", "石斧",
        recipe={"plank": 1, "cobble": 2},
        damage=14, range=4.0, cooldown=1.0,
        install_fee=20,
        color=(150, 150, 150),
    ),
    "iron_sword": WeaponDef(
        "iron_sword", "铁剑",
        recipe={"iron_ingot": 2, "plank": 1},
        damage=24, range=5.0, cooldown=0.9,
        install_fee=40,
        special={"bonus_vs": "tank", "bonus_mult": 1.5},
        color=(200, 200, 210),
    ),
    "sand_sling": WeaponDef(
        "sand_sling", "沙弹弓",
        recipe={"sand": 3, "plank": 1},
        damage=6, range=3.5, cooldown=0.5,
        install_fee=15,
        special={"slow_factor": 0.5, "slow_duration": 1.5},
        color=(210, 180, 100),
    ),
}


# ─── 经典塔的材料配方（用于拆除回收） ──────────────────────
# 经典塔仍用金钱购买，但记录材料配方供拆除回收

CLASSIC_TOWER_MATERIALS = {
    "stone_thrower": {"cobble": 3},
    "water_cannon": {"cobble": 2, "plank": 2},
    "sand_trap": {"sand": 4},
    "metal_tower": {"iron_ingot": 2},
}

DISMANTLE_REFUND = 0.5  # 拆除回收比例


# ─── 背包 ──────────────────────────────────────────────────

class Inventory:
    """玩家背包：材料、武器、工具"""

    def __init__(self):
        self.materials: dict[str, int] = {}
        self.weapons: dict[str, int] = {}
        self.tools: set[str] = set()

    def add_material(self, name: str, amount: int = 1):
        self.materials[name] = self.materials.get(name, 0) + amount

    def remove_material(self, name: str, amount: int = 1) -> bool:
        """尝试移除材料，成功返回 True"""
        if self.materials.get(name, 0) >= amount:
            self.materials[name] -= amount
            return True
        return False

    def get_material(self, name: str) -> int:
        return self.materials.get(name, 0)

    def can_craft(self, recipe: dict) -> bool:
        """检查背包材料是否满足配方"""
        for mat, amt in recipe.items():
            if self.materials.get(mat, 0) < amt:
                return False
        return True

    def spend(self, recipe: dict):
        """扣除配方所需材料"""
        for mat, amt in recipe.items():
            self.materials[mat] = self.materials.get(mat, 0) - amt

    def has_tool(self, tool_name: str) -> bool:
        return tool_name in self.tools

    def add_tool(self, tool_name: str):
        self.tools.add(tool_name)

    def add_weapon(self, weapon_name: str, count: int = 1):
        self.weapons[weapon_name] = self.weapons.get(weapon_name, 0) + count

    def remove_weapon(self, weapon_name: str, count: int = 1) -> bool:
        if self.weapons.get(weapon_name, 0) >= count:
            self.weapons[weapon_name] -= count
            return True
        return False

    def get_weapon(self, weapon_name: str) -> int:
        return self.weapons.get(weapon_name, 0)

    def reset(self):
        self.materials = {}
        self.weapons = {}
        self.tools = set()


# ─── 加工/合成辅助函数 ─────────────────────────────────────

def can_process(recipe_key: str, inventory: Inventory) -> bool:
    """检查是否可以执行某加工（有原料、徒手可 or 有工具）"""
    recipe = PROCESS_RECIPES.get(recipe_key)
    if not recipe:
        return False
    if inventory.get_material(recipe["in"]) < 1:
        return False
    if recipe["hand"] > 0:
        return True  # 徒手可加工
    # 需要工具
    required_tool = recipe.get("requires_tool")
    if required_tool:
        return inventory.has_tool(required_tool)
    return False


def process(recipe_key: str, inventory: Inventory, use_tool: bool = False):
    """
    执行加工：消耗 1 原料，产出材料。
    use_tool=True 时产量更高。
    返回 (成功, 产物名, 产物数量)
    """
    recipe = PROCESS_RECIPES.get(recipe_key)
    if not recipe:
        return False, None, 0
    if inventory.get_material(recipe["in"]) < 1:
        return False, None, 0

    if use_tool:
        # 检查是否有对应工具
        required_tool = recipe.get("requires_tool")
        if required_tool and not inventory.has_tool(required_tool):
            return False, None, 0
        amount = recipe["tool"]
    else:
        amount = recipe["hand"]
        if amount <= 0:
            return False, None, 0

    inventory.remove_material(recipe["in"], 1)
    inventory.add_material(recipe["out"], amount)
    return True, recipe["out"], amount


def can_craft_weapon(weapon_name: str, inventory: Inventory) -> bool:
    """检查是否可以合成武器"""
    wdef = WEAPON_TYPES.get(weapon_name)
    if not wdef:
        return False
    return inventory.can_craft(wdef.recipe)


def craft_weapon(weapon_name: str, inventory: Inventory) -> bool:
    """合成武器：消耗材料，武器入背包"""
    wdef = WEAPON_TYPES.get(weapon_name)
    if not wdef or not inventory.can_craft(wdef.recipe):
        return False
    inventory.spend(wdef.recipe)
    inventory.add_weapon(weapon_name)
    return True


def can_craft_tool(tool_name: str, inventory: Inventory) -> bool:
    """检查是否可以合成工具"""
    tdef = TOOL_TYPES.get(tool_name)
    if not tdef:
        return False
    return inventory.can_craft(tdef.recipe)


def craft_tool(tool_name: str, inventory: Inventory) -> bool:
    """合成工具：消耗材料，工具入背包"""
    tdef = TOOL_TYPES.get(tool_name)
    if not tdef or not inventory.can_craft(tdef.recipe):
        return False
    inventory.spend(tdef.recipe)
    inventory.add_tool(tool_name)
    return True


def dismantle_refund(recipe: dict) -> dict:
    """
    计算拆除回收材料。
    返回 {material: amount}，每项 amount = floor(recipe[mat] * DISMANTLE_REFUND)
    """
    result = {}
    for mat, amt in recipe.items():
        refund = int(amt * DISMANTLE_REFUND)
        if refund > 0:
            result[mat] = refund
    return result
