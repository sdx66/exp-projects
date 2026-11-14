"""
entities.py - 游戏实体
Tower, Enemy, Projectile 类定义 + 类型定义
塔/敌人类型数据来自 data/towers.json / data/enemies.json（经 gamedata 构建）。
"""

import config
import gamedata
import math
import random
from items import CLASSIC_TOWER_MATERIALS, item_volume, WEAPON_TYPES, TOOL_TYPES
from blocks import is_solid, get_block as _block_def, BLOCK_ID


class BaseResearchLane:
    """基地手研通道：承载 station=false（无需研究塔）的科技研究。

    与 Tower 的研究槽字段同名（research_current/timer/total/queue/blocked），
    因此 game 的 _tick_research/_start_next_in_queue/_complete_research/
    _cancel_research 可无差别复用；tower=None 表示这不是一座塔。
    """

    __slots__ = ("research_current", "research_timer", "research_total",
                 "research_queue", "research_blocked_reason")

    def __init__(self):
        self.research_current = None
        self.research_timer = 0.0
        self.research_total = 0.0
        self.research_queue = []
        self.research_blocked_reason = None

    def lane_dict(self):
        """研究通道快照（供科技树面板渲染/ETA 汇总；tower=None 表示手研）。"""
        return {
            "tower": None,
            "hand": True,
            "current": self.research_current,
            "timer": float(self.research_timer),
            "total": float(self.research_total),
            "queue": list(self.research_queue),
            "blocked": self.research_blocked_reason,
        }


# ─── 塔类型定义 ────────────────────────────────────────────

class TowerType:
    """塔类型定义

    定位字段（让每种塔不可替代）:
      slow_factor / slow_duration  — 命中后减速(sand_trap)
      splash_radius / splash_falloff — 命中后范围溅射衰减伤害(water_cannon)
      bonus_vs / bonus_mult        — 对特定敌人类型增伤(metal_tower 打坦克)
    防御塔字段:
      no_attack   — True 表示塔自身永不索敌开火（仅高血量挡路，攻击来自挂载武器）
      weapon_slots— 可挂载武器槽位数（0 = 不可挂载）
    仓储塔字段:
      storage_capacity — 固定仓储容量（空间单位；0 = 非仓储塔）
    加工塔字段:
      processing_capacity — 固定加工容量（空间单位；0 = 非加工塔）
    研究塔字段:
      research_attr — 基础学识属性（等级制，每级 +config.RESEARCH_ATTR_PER_LEVEL；
                       0 = 非研究塔）。研究塔也带 storage_capacity（存工具/材料）。
    电力字段（电站/电池，来自 data/power.json）:
      power_kind — None/"station"/"battery"（0 非电力塔）。
      电站: power_class(输出电压等级)/rated_kw/output_voltage_v/fuel_types/efficiency/condition_kind。
      电池: cell_voltage_v/cell_energy_j/cell_max_current_a。
    显示字段:
      name_cn — 中文显示名（来自 towers.json/power.json 的 name_cn；缺省回退 type_name）。
    """
    def __init__(self, type_name, base_damage, base_range, base_cooldown,
                 base_cost, requires_tech=None,
                 slow_factor=1.0, slow_duration=0.0,
                 splash_radius=0.0, splash_falloff=0.5,
                 bonus_vs=None, bonus_mult=1.0,
                 no_attack=False, weapon_slots=0, storage_capacity=0,
                 processing_capacity=0, research_attr=0,
                 power_kind=None, power_class=None, rated_kw=0.0,
                 output_voltage_v=0.0, fuel_types=None, efficiency=1.0,
                 condition_kind=None, cell_voltage_v=0.0,
                 cell_energy_j=0.0, cell_max_current_a=0.0,
                 mining_cfg=None, name_cn=None):
        self.type_name = type_name
        # 中文显示名（缺省回退 type_name，保证 HUD 等展示路径始终可读）
        self.name_cn = name_cn or type_name
        self.base_damage = base_damage
        self.base_range = base_range
        self.base_cooldown = base_cooldown
        self.base_cost = base_cost
        self.requires_tech = requires_tech or []
        self.slow_factor = slow_factor
        self.slow_duration = slow_duration
        self.splash_radius = splash_radius
        self.splash_falloff = splash_falloff
        self.bonus_vs = bonus_vs
        self.bonus_mult = bonus_mult
        # 防御塔(no_attack): 塔自身永不索敌开火，攻击只来自挂载的武器
        self.no_attack = bool(no_attack)
        # 武器挂载槽位上限（0 = 不可挂载）
        self.weapon_slots = int(weapon_slots or 0)
        # 仓储容量（0 = 非仓储塔）
        self.storage_capacity = int(storage_capacity or 0)
        # 加工容量（0 = 非加工塔）
        self.processing_capacity = int(processing_capacity or 0)
        # 研究塔基础学识属性（0 = 非研究塔）
        self.research_attr = int(research_attr or 0)
        # 电力塔字段（None/0 = 非电力塔）
        self.power_kind = power_kind
        self.power_class = power_class
        self.rated_kw = float(rated_kw or 0.0)
        self.output_voltage_v = float(output_voltage_v or 0.0)
        self.fuel_types = list(fuel_types) if fuel_types else None
        self.efficiency = float(efficiency or 1.0)
        self.condition_kind = condition_kind
        self.cell_voltage_v = float(cell_voltage_v or 0.0)
        self.cell_energy_j = float(cell_energy_j or 0.0)
        self.cell_max_current_a = float(cell_max_current_a or 0.0)
        # 开采塔配置（空 = 非开采塔）
        self.mining_cfg = dict(mining_cfg or {})

    @property
    def is_power(self):
        """是否为电力塔（电站或电池）。"""
        return self.power_kind in ("station", "battery")

    @property
    def is_research(self):
        """是否为研究塔（research_attr > 0）。"""
        return self.research_attr > 0

    @property
    def is_mining(self):
        """是否为开采塔（mining_cfg 非空）。"""
        return bool(self.mining_cfg)

    def mining_attr(self, key, default=0):
        """取开采塔配置字段（footprint_min/max/ring/mine_range/...）。"""
        return self.mining_cfg.get(key, default)

    @property
    def color(self):
        return gamedata.TOWER_COLORS.get(self.type_name, (128, 128, 128))

    def body_dims(self, footprint=None):
        """塔身 3D 造型 (w, h, d, y_off) — 实塔渲染与放置预览/拆除高亮共用。

        y_off 是塔身中心相对 pos[1]（=地表顶面 surface+1）的抬升：
        经典/武器塔半嵌入地表，仓储/加工箱与城墙立在顶面上。
        几何只此一份来源，改这里即可，否则预览与实塔会再次错位。
        footprint=(m,n) 为开采塔实际足迹（默认取配置最小值）。
        """
        if self.is_mining:
            m, n = footprint or (self.mining_attr("footprint_min", 3),
                                 self.mining_attr("footprint_min", 3))
            # 开采塔：足迹平台（m×n 矮箱）+ 中央井架，立在顶面上
            return (m * 0.9, 1.4, n * 0.9, 0.7)
        if self.is_research:
            # 研究塔：比仓储/加工矮箱更高，便于与普通仓储区分（仍可挂 1 武器）
            return (0.6, 1.6, 0.6, 0.8)
        if self.is_power:
            # 电力塔（电站/电池）：略宽矮箱（区分于普通塔/仓储）
            return (0.7, 1.2, 0.7, 0.6)
        if self.storage_capacity or self.processing_capacity:
            return (0.65, 1.1, 0.65, 0.55)
        if self.no_attack:
            return (0.6, 2.2, 0.6, 1.1)
        return (0.5, 1.0, 0.5, 0.0)


# ─── 塔类型（来自 data/towers.json） ──────────────────────────
# 设计意图: stone=基础输出, sand=控场(减速), water=清群(溅射), metal=反坦克(增伤)
TOWER_TYPES = {}
for _name, _d in gamedata.TOWERS_RAW.items():
    TOWER_TYPES[_name] = TowerType(
        _name,
        name_cn=_d.get("name_cn"),
        base_damage=_d["base_damage"],
        base_range=_d["base_range"],
        base_cooldown=_d["base_cooldown"],
        base_cost=_d["base_cost"],
        requires_tech=list(_d.get("requires_tech", [])),
        slow_factor=_d.get("slow_factor", 1.0),
        slow_duration=_d.get("slow_duration", 0.0),
        splash_radius=_d.get("splash_radius", 0.0),
        splash_falloff=_d.get("splash_falloff", 0.5),
        bonus_vs=_d.get("bonus_vs"),
        bonus_mult=_d.get("bonus_mult", 1.0),
        no_attack=_d.get("no_attack", False),
        weapon_slots=_d.get("weapon_slots", 0),
        storage_capacity=_d.get("storage_capacity", 0),
        processing_capacity=_d.get("processing_capacity", 0),
        research_attr=_d.get("research_attr", 0),
        mining_cfg=gamedata.TOWER_MINING_CFG.get(_name),
    )

# ─── 电力塔类型（发电站 / 电池，来自 data/power.json） ────────
# 并入 TOWER_TYPES（与经典塔同构），供放置/渲染/存档统一处理。
for _sname, _sd in gamedata.STATIONS_RAW.items():
    TOWER_TYPES[_sname] = TowerType(
        _sname,
        name_cn=_sd.get("name_cn"),
        base_damage=0, base_range=0, base_cooldown=1.0,
        base_cost=_sd["base_cost"],
        requires_tech=list(_sd.get("requires_tech", [])),
        no_attack=True, weapon_slots=0,
        power_kind="station",
        power_class=_sd.get("output_class", "LV"),
        rated_kw=_sd.get("rated_kw", 0),
        output_voltage_v=_sd.get("voltage_v", 0),
        fuel_types=_sd.get("fuel_types"),
        efficiency=_sd.get("efficiency", 1.0),
        condition_kind=_sd.get("condition"),
    )
for _bname, _bd in gamedata.BATTERIES_RAW.items():
    TOWER_TYPES[_bname] = TowerType(
        _bname,
        name_cn=_bd.get("name_cn"),
        base_damage=0, base_range=0, base_cooldown=1.0,
        base_cost=_bd["base_cost"],
        requires_tech=list(_bd.get("requires_tech", [])),
        no_attack=True, weapon_slots=0,
        power_kind="battery",
        power_class=_bd.get("voltage_class", "LV"),
        cell_voltage_v=_bd.get("cell_voltage_v", 0),
        cell_energy_j=_bd.get("cell_energy_j", 0),
        cell_max_current_a=_bd.get("cell_max_current_a", 0),
    )


# ─── 敌人类型定义 ──────────────────────────────────────────

class EnemyType:
    """敌人类型定义"""
    def __init__(self, type_name, base_health, base_speed, base_reward):
        self.type_name = type_name
        self.base_health = base_health
        self.base_speed = base_speed
        self.base_reward = base_reward

    @property
    def color(self):
        return gamedata.ENEMY_COLORS.get(self.type_name, (200, 50, 50))


# 敌人类型（来自 data/enemies.json 的 base_health/base_speed/base_reward）
# 基础血量已 ÷2(原值 50/30/150 → 25/15/75): 修复"打不死敌人"的数值失衡。
# 原值下基础塔 10 DPS 打不动 50 HP 敌人,÷2 后 2 塔 ~50% 击杀率,难度均衡。
ENEMY_TYPES = {}
for _name, _d in gamedata.ENEMIES_RAW.items():
    ENEMY_TYPES[_name] = EnemyType(
        _name,
        base_health=_d["base_health"],
        base_speed=_d["base_speed"],
        base_reward=_d["base_reward"],
    )


class EnemyGrid:
    __slots__ = ("cell_size", "inv", "cells")

    def __init__(self, enemies, cell_size=4.0):
        self.cell_size = cell_size
        inv = self.inv = 1.0 / cell_size
        cells = {}
        get = cells.get
        floor = math.floor
        for i, enemy in enumerate(enemies):
            if not enemy.active:
                continue
            p = enemy.position
            key = (int(floor(p[0] * inv)), int(floor(p[2] * inv)))
            bucket = get(key)
            if bucket is None:
                cells[key] = [i]
            else:
                bucket.append(i)
        self.cells = cells

    def query(self, x, z, radius):
        """返回覆盖 (x±radius, z±radius) 的敌人索引列表。

        单格命中时直接返回内部桶（不拷贝）：调用方只允许遍历，不许修改
        （现有调用方 Tower._find_target / Projectile.update / game 分离循环
        均只读；splash 路径经 sorted() 已复制）。
        """
        inv = self.inv
        x0 = int(math.floor((x - radius) * inv))
        x1 = int(math.floor((x + radius) * inv))
        z0 = int(math.floor((z - radius) * inv))
        z1 = int(math.floor((z + radius) * inv))
        cells = self.cells
        if x0 == x1 and z0 == z1:
            # 单格快路径：跳过列表分配与 extend（分离/索敌/命中查询的常见情形）
            bucket = cells.get((x0, z0))
            return bucket if bucket is not None else []
        result = []
        for cx in range(x0, x1 + 1):
            for cz in range(z0, z1 + 1):
                bucket = cells.get((cx, cz))
                if bucket:
                    result.extend(bucket)
        return result


# ─── 体积桶共享操作（仓储塔 storage 与加工塔 processing 共用） ──
# 桶结构 {"materials": {}, "weapons": {}, "tools": set()}，容量按 item_volume 计。

def _bucket_used(bucket: dict) -> int:
    """桶内已占用空间合计（每件物品按 item_volume 计，工具每件占 6）。"""
    used = 0
    for name, count in bucket["materials"].items():
        used += item_volume(name) * count
    for name, count in bucket["weapons"].items():
        used += item_volume(name) * count
    for name in bucket["tools"]:
        used += item_volume(name)
    return used


def _bucket_kind(name: str) -> str:
    """按物品名归桶：weapon → weapons，tool → tools，其余为材料。"""
    if name in WEAPON_TYPES:
        return "weapons"
    if name in TOOL_TYPES:
        return "tools"
    return "materials"


def _bucket_deposit(bucket: dict, cap: int, name: str, count: int = 1) -> int:
    """存入最多 count 件 name，受剩余空间截断，返回实际存入件数。

    工具为永久唯一物：已存入则不再重复收纳（返回 0）。
    """
    free = cap - _bucket_used(bucket)
    kind = _bucket_kind(name)
    if kind == "tools":
        if name in bucket["tools"] or free < item_volume(name):
            return 0
        bucket["tools"].add(name)
        return 1
    vol = item_volume(name)
    n = min(count, free // vol)
    if n <= 0:
        return 0
    bucket[kind][name] = bucket[kind].get(name, 0) + n
    return n


def _bucket_withdraw(bucket: dict, name: str, count: int = 1) -> int:
    """取出最多 count 件 name，返回实际取出件数。"""
    kind = _bucket_kind(name)
    if kind == "tools":
        if name in bucket["tools"]:
            bucket["tools"].discard(name)
            return 1
        return 0
    b = bucket[kind]
    have = b.get(name, 0)
    n = min(count, have)
    if n <= 0:
        return 0
    b[name] = have - n
    if b[name] <= 0:
        del b[name]
    return n


def _segment_blocked(universe, x0, y0, z0, x1, y1, z1) -> bool:
    """线段 (x0,y0,z0)→(x1,y1,z1) 是否穿过实心方块。

    与 Projectile._terrain_swept 同采样口径：按 PROJECTILE_COLLISION_STEP
    步长线性插值采样，floor 落入的方块格用 is_solid 判定，终点必被采样。
    用于索敌时的"弹道可及性"预筛：直线弹道上只要有实心方块，抛射体必然
    命中地形停下（子弹浪费），该目标应被跳过——修复"塔打地下/墙后敌人
    永远打不到"的锁死行为。

    universe 为 None 时恒返回 False（旧调用约定：不做视线检查）。
    """
    if universe is None:
        return False
    step = config.PROJECTILE_COLLISION_STEP
    dx = x1 - x0
    dy = y1 - y0
    dz = z1 - z0
    seg = (dx * dx + dy * dy + dz * dz) ** 0.5
    if seg <= 0.0:
        n = 1
    else:
        n = min(128, max(1, int(seg / step) + 1))
    gbs = universe.get_block_safe
    floor = math.floor
    solid = is_solid
    for i in range(n):
        t = i / (n - 1) if n > 1 else 1.0
        x = x0 + dx * t
        y = y0 + dy * t
        z = z0 + dz * t
        if solid(gbs(floor(x), floor(y), floor(z))):
            return True
    return False


class Tower:
    """塔（含攻击塔与无攻击的防御塔/城墙）"""

    def __init__(self, tower_type, position, level=1, damage_mult=1.0):
        self.type_name = tower_type.type_name
        self.type_def = tower_type
        self.position = position  # (x, y, z)
        self.level = level
        self.damage_mult = damage_mult  # 科技加成(如 handcraft +10%)
        self.damage = tower_type.base_damage * (config.TOWER_DAMAGE_MULT ** (level - 1)) * damage_mult
        self.range = tower_type.base_range * (config.TOWER_RANGE_MULT ** (level - 1))
        self.cooldown = tower_type.base_cooldown * (config.TOWER_COOLDOWN_MULT ** (level - 1))
        self.cooldown_timer = 0.0
        self.target = None
        self.active = True
        # 防御塔标志与挂载槽位（从类型定义带入）
        self.no_attack = bool(getattr(tower_type, "no_attack", False))
        self.weapon_slots = int(getattr(tower_type, "weapon_slots", 0) or 0)
        # 仓储（storage_capacity>0 的仓储塔才有）：与 Inventory 同构
        self.storage_capacity = int(getattr(tower_type, "storage_capacity", 0) or 0)
        self.storage = {"materials": {}, "weapons": {}, "tools": set()}
        # 加工（processing_capacity>0 的加工塔才有）：桶结构与仓储同构，共享体积池
        self.processing_capacity = int(getattr(tower_type, "processing_capacity", 0) or 0)
        self.processing = {"materials": {}, "weapons": {}, "tools": set()}
        # 在制加工任务：{配方key: 已计时间}；输入在开工时已消耗，完工才产出
        self.process_jobs = {}
        # 在制任务的开工快照：{配方key: {"yield": n, "duration": t}}
        # 中途取走/换选工具不影响在制批次，故产量/耗时在开工时冻结。
        self.process_meta = {}
        # 手动线当前选中的配方（仅无 requires_tool 的配方；None = 停机）
        self.manual_recipe = None
        # 挂载武器列表：[{"wdef": WeaponDef, "damage_mult": float, "timer": float}]
        # 每把武器独立索敌/开火，攻击效果叠加。
        self.weapon_mounts = []
        # 塔体生命值（敌人近战拆塔的目标；每级强化 +30%）
        self.max_hp = gamedata.TOWER_HP.get(tower_type.type_name, 100) \
            * (1.3 ** (level - 1))
        self.hp = self.max_hp
        # 来源：("classic", type_name) 或 ("weapon", weapon_name)
        self.source = ("classic", tower_type.type_name)
        # display_color 缓存：(source, color)；source 变化时自动失效
        self._display_color_cache = None
        # 材料配方（用于拆除回收）
        self.material_recipe = dict(CLASSIC_TOWER_MATERIALS.get(tower_type.type_name, {}))
        # 研究塔学识属性（等级制：基础 + 每级增量）
        self.research_attr = int(getattr(tower_type, "research_attr", 0) or 0)
        # ─── 研究塔队列状态（is_research 才有意义） ─────────────
        # research_current: 当前正在研究的科技 id（None=空闲）
        # research_timer / research_total: 当前项剩余秒 / 总秒
        # research_queue: FIFO 待研究科技 id 列表（仅当塔空闲时启动队首）
        # research_blocked_reason: 队首无法启动的原因（None=可启动）；逐帧重试
        self.research_current = None
        self.research_timer = 0.0
        self.research_total = 0.0
        self.research_queue = []
        self.research_blocked_reason = None
        # 传送带导出白名单：仓储塔手工勾选要输出到传送带的材料名（默认空 = 不导出）。
        # 加工塔不在此列（自动输出其 processed 产物）；研究塔默认不导出。
        self.belt_export = set()
        # ─── 电力塔字段（电站/电池） ──────────────────────────
        self.power_kind = getattr(tower_type, "power_kind", None)
        self.power_class = getattr(tower_type, "power_class", None)
        self.rated_kw = float(getattr(tower_type, "rated_kw", 0.0) or 0.0)
        self.output_voltage_v = float(getattr(tower_type, "output_voltage_v", 0.0) or 0.0)
        self.fuel_types = list(getattr(tower_type, "fuel_types", None) or []) or None
        self.efficiency = float(getattr(tower_type, "efficiency", 1.0) or 1.0)
        self.condition_kind = getattr(tower_type, "condition_kind", None)
        self.cell_voltage_v = float(getattr(tower_type, "cell_voltage_v", 0.0) or 0.0)
        self.cell_energy_j = float(getattr(tower_type, "cell_energy_j", 0.0) or 0.0)
        self.cell_max_current_a = float(getattr(tower_type, "cell_max_current_a", 0.0) or 0.0)
        # 电站燃料桶（燃电站用；经传送带/背包存入）
        self.power_fuel = {}
        # 电池储能（焦）与组归属（本帧求解后回写）
        self.charge_j = 0.0
        self.group_id = None
        self.group_kind = None
        # 电网本帧状态（UI 显示）
        self.in_grid = False
        self.generation_w = 0.0
        self.charge_power_w = 0.0
        self.discharge_power_w = 0.0
        self.grid_message = None
        # ─── 开采塔字段（is_mining 才有意义） ─────────────────
        # footprint: 放置时选定的足迹 (m, n)，锚点=position 所在格居中。
        # mine_start_surface: {列(x,z): 放置面高度}，开采深度相对它算。
        # mine_shaft: {列(x,z): {"front": y 当前挖到的格, "timer": 计时}}。
        self.footprint = (int(tower_type.mining_attr("footprint_min", 3)),
                          int(tower_type.mining_attr("footprint_min", 3)))
        self.mine_start_surface = {}
        self.mine_shaft = {}
        self._mining_surface_modified = False  # 本次会话是否改过列顶（统一 bump）
        self.mining_reason = None  # 全塔暂停原因（None=运行；文本=停采原因）

    @property
    def is_research(self):
        """是否为研究塔（research_attr > 0）。"""
        return self.research_attr > 0

    @property
    def is_power(self):
        """是否为电力塔（电站或电池）。"""
        return self.power_kind in ("station", "battery")

    @property
    def is_mining(self):
        """是否为开采塔（mining_cfg 非空）。"""
        return bool(getattr(self.type_def, "mining_cfg", None))

    def research_attribute(self) -> int:
        """研究塔当前学识属性值（基础 + (等级-1) × 每级增量）。"""
        return self.research_attr + (self.level - 1) * config.RESEARCH_ATTR_PER_LEVEL

    def lane_dict(self):
        """研究通道快照（供科技树面板渲染/ETA 汇总）。"""
        return {
            "tower": self,
            "current": self.research_current,
            "timer": float(self.research_timer),
            "total": float(self.research_total),
            "queue": list(self.research_queue),
            "blocked": self.research_blocked_reason,
        }

    # ─── 开采塔（竖井） ─────────────────────────────────────

    @property
    def footprint_w(self) -> int:
        return int(self.footprint[0])

    @property
    def footprint_h(self) -> int:
        return int(self.footprint[1])

    def footprint_cells(self):
        """足迹全部格 (x, z)，锚点=position 所在格居中。"""
        ax = math.floor(self.position[0])
        az = math.floor(self.position[2])
        m, n = self.footprint_w, self.footprint_h
        return [(ax + dx, az + dz)
                for dx in range(-(m // 2), m - (m // 2))
                for dz in range(-(n // 2), n - (n // 2))]

    def shaft_cells(self):
        """内部竖井格 = 足迹去掉外围 ring 圈（默认 ring=1）。

        以足迹格包围盒计算边缘距离，奇/偶足迹都正确。
        """
        ring = int(self.type_def.mining_attr("ring", 1) or 1)
        cells = self.footprint_cells()
        if not cells:
            return []
        xs = [c[0] for c in cells]
        zs = [c[1] for c in cells]
        x0, x1 = min(xs), max(xs)
        z0, z1 = min(zs), max(zs)
        out = []
        for (x, z) in cells:
            edx = min(x - x0, x1 - x)
            edz = min(z - z0, z1 - z)
            if edx >= ring and edz >= ring:
                out.append((x, z))
        return out

    def mining_tool_stats(self):
        """返回 (max_tier, max_speed)：塔内仓储挂载的采掘工具最高档与速度系数。

        无采掘工具返回 (0, 0.0)。多把工具取最高档；同档取最快速度。
        """
        best_tier, best_speed = 0, 0.0
        for tool in self.storage.get("tools", ()):  # type: ignore
            tdef = TOOL_TYPES.get(tool)
            if tdef is None or tdef.mining_speed <= 0:
                continue
            if tdef.mining_tier > best_tier or (tdef.mining_tier == best_tier and tdef.mining_speed > best_speed):
                best_tier, best_speed = tdef.mining_tier, tdef.mining_speed
        return best_tier, best_speed

    def mine_duration(self, tool_speed: float) -> float:
        """开采一块的基础耗时（随升级提速、随工具速度缩短）。"""
        base = float(self.type_def.mining_attr("base_mine_time", 2.0) or 2.0)
        mult = float(self.type_def.mining_attr("mine_speed_mult", 1.0) or 1.0)
        speed_up = 1.0 - 0.12 * (self.level - 1)
        return max(0.2, base * mult / max(0.01, tool_speed) * speed_up)

    def _column_stop_reason(self, universe, x, z):
        """返回该列停采原因（None=可继续开采）。

        条件（对应需求）：
          A. 下方只剩一层实体方块（front 下一格 y-1 为非基岩实体且 y-2 不是实体
             或已是基岩下方）→ 停（保护底层，不挖穿）；
          B. 超出开采范围（放置面高度 - front ≥ mine_range）→ 停；
          C. 下一格不可挖（水/岩浆/基岩/空气）→ 停；
          D. 工具档位不足以挖该方块（mining_tier > 工具档）→ 停。
        """
        ring_range = int(self.type_def.mining_attr("mine_range", 24) or 24)
        start = self.mine_start_surface.get((x, z))
        if start is None:
            return "未初始化"
        col = self.mine_shaft.get((x, z))
        front = col["front"] if col else start
        # B. 超开采范围
        if start - front >= ring_range:
            return f"已达开采深度上限 {ring_range}"
        # 下一块
        nx, ny, nz = x, front, z
        # 先探测下一格是否存在可挖方块
        if ny < 2:
            return "已达基岩层"
        nid = universe.get_block(nx, ny, nz)
        from blocks import BLOCK_ID as _bid
        if nid in (_bid["air"], _bid["water"], _bid["lava"], _bid["bedrock"]):
            return "已挖空/不可挖层"
        # A. 下方只剩一层实体（front-1 实体 且 front-2 非实体或<2）→ 保护底层
        if front - 1 >= 2:
            below_id = universe.get_block(nx, front - 1, nz)
            below_solid = below_id not in (_bid["air"], _bid["water"], _bid["lava"], _bid["bedrock"])
            if below_solid:
                below2_id = universe.get_block(nx, front - 2, nz)
                below2_solid = below2_id not in (_bid["air"], _bid["water"], _bid["lava"], _bid["bedrock"])
                if not below2_solid:
                    return "下方仅剩一层，停止开采"
        # D. 工具档位不足
        need_tier = _block_def(nid).mining_tier
        tool_tier, _ = self.mining_tool_stats()
        if need_tier > tool_tier:
            return f"需采掘档位 {need_tier} (当前 {tool_tier})"
        return None

    def _update_mining(self, dt, universe):
        """开采塔竖井开采（每帧调用）。"""
        if not self.is_mining:
            return
        from blocks import BLOCK_ID as _bid, get_block as _get_block
        # 全塔工具/仓储先决
        tool_tier, tool_speed = self.mining_tool_stats()
        if tool_speed <= 0:
            self.mining_reason = "无采掘工具"
            return
        # 初始化各列（放置面）
        for (x, z) in self.shaft_cells():
            if (x, z) not in self.mine_start_surface:
                self.mine_start_surface[(x, z)] = int(universe.surface_height_at(x, z))
        reason = None
        progress = False
        global_stop = False  # 全塔级停因（仓储类），不受其他列 progress 掩盖
        for (x, z) in self.shaft_cells():
            col = self.mine_shaft.setdefault((x, z), {"front": None, "timer": 0.0})
            if col["front"] is None:
                # 首块 = 该列放置面地表顶方块（从顶向下逐块采）
                col["front"] = self.mine_start_surface[(x, z)]
            # 满仓：全塔暂停（仓储无空间）
            if self.storage_free() <= 0:
                reason = "仓储已满"
                global_stop = True
                break
            stop = self._column_stop_reason(universe, x, z)
            if stop:
                if reason is None:
                    reason = stop
                continue
            col["timer"] -= dt
            if col["timer"] > 0:
                progress = True
                continue
            # 先非破坏性 peek 当前格（R1-B2：原实现先 dig_block_id（方块已从世界
            # 移除）后查容量，装不下时方块已销毁——注释承诺"装不下则暂停、
            # 不消耗方块"与代码顺序矛盾。改为先 peek 决定收获内容，容量
            # 装得下才真正削方块；满仓停因不消耗任何方块。）
            block_id = universe.get_block(x, col["front"], z)
            if block_id == 0:
                # front 已是空气（或 peek 语义下不可采）：跳过该格继续向下，
                # 避免同格无限空转卡死（BUG-034）。重置计时使下一格按正常节奏开采。
                col["front"] -= 1
                col["timer"] = self.mine_duration(tool_speed)
                progress = True
                continue
            bdef = _get_block(block_id)
            mat = bdef.harvest_material
            amt = bdef.harvest_amount or 1
            # 采集前确认余量装得下本块收获：否则挖了却被 deposit() 截断丢弃
            # （BUG-033 / R1-B2）。装不下则全塔暂停并给出停因，不消耗方块。
            if mat and amt * item_volume(mat) > self.storage_free():
                reason = "仓储空间不足"
                global_stop = True
                break
            # 容量确认后才真正削方块。dig_block_id 返回 0 = 守卫拒绝（非纯
            # 陆地/大本营禁区/基岩/水/岩浆等）：液体/基岩层与 peek 判据不一致
            # 时按 BUG-034 语义跳过推进，不误耗方块。
            dug = universe.dig_block_id(x, col["front"], z)
            if dug == 0:
                col["front"] -= 1
                col["timer"] = self.mine_duration(tool_speed)
                progress = True
                continue
            # BUG-130：本格确有方块被削 → 该列列顶可能下移。
            # 标记本次会话改过地形，待列循环结束后统一 bump world_version。
            # 依据 world.py dig_block_id 契约（L436-438）：挖掘逐格连续，不
            # 在 dig_block_id 内推进 world_version，会话结束由调用方（开采塔）
            # 统一 bump_world_version，避免逐格 bump 让变体缓存/小地图反复失效。
            self._mining_surface_modified = True
            if mat:
                self.deposit(mat, amt)
            col["front"] -= 1
            col["timer"] = self.mine_duration(tool_speed)
            progress = True
        # BUG-130：本次调用若确实削掉过方块，统一 bump 一次 world_version，
        # 让 pathfinder 版本门控缓存（corridor 高度 / variants）按开采后地形
        # 失效重算。digs 由 mine_duration 间隔，不会每帧触发，无抖动/刷屏风险。
        if self._mining_surface_modified:
            universe.bump_world_version()
            self._mining_surface_modified = False

        # mining_reason 是"全塔暂停原因"（None=运行中）。仓储类是全塔级停因，
        # 直接生效；其余为单列停因，只要还有任一列在推进就不覆盖成"停"（BUG-032）。
        if global_stop:
            self.mining_reason = reason
        else:
            self.mining_reason = None if progress else reason

    @staticmethod
    def from_weapon(weapon_def, position, damage_mult=1.0):
        """从武器定义创建塔"""
        tower_type = _weapon_to_tower_type(weapon_def)
        tower = Tower(tower_type, position, damage_mult=damage_mult)
        tower.source = ("weapon", weapon_def.name)
        tower.material_recipe = dict(weapon_def.recipe)
        return tower

    def mount_weapon(self, weapon_def, damage_mult=1.0):
        """挂载武器到防御塔的槽位。槽位已满或无槽位时返回 False。"""
        if not self.weapon_slots or len(self.weapon_mounts) >= self.weapon_slots:
            return False
        self.weapon_mounts.append({
            "wdef": weapon_def,
            "damage_mult": damage_mult,
            "timer": 0.0,
        })
        return True

    def unmount_weapon(self, index=-1):
        """从指定槽位取下武器，返回其 WeaponDef（供返还背包）。

        index=-1 取最后挂载的那把（LIFO，即玩家最近一次装上去的）。
        无挂载或下标越界返回 None。取下的武器不带走 damage_mult——
        重新挂载时会按当时的 tech_damage_mult 重新结算。
        """
        if not self.weapon_mounts:
            return None
        i = index if index >= 0 else len(self.weapon_mounts) + index
        if not (0 <= i < len(self.weapon_mounts)):
            return None
        return self.weapon_mounts.pop(i)["wdef"]

    def unmount_all(self):
        """取下全部挂载武器，按原顺序返回 WeaponDef 列表。"""
        out = [m["wdef"] for m in self.weapon_mounts]
        self.weapon_mounts.clear()
        return out

    # ─── 仓储（仓储塔） ──────────────────────────────────────
    # 体积桶操作已抽为模块级 _bucket_* 共享辅助（与加工塔 processing 共用）。

    def storage_used(self) -> int:
        """已占用空间合计。非仓储塔恒为 0。"""
        if not self.storage_capacity:
            return 0
        return _bucket_used(self.storage)

    def storage_free(self) -> int:
        """剩余可用的空间单位数。"""
        return max(0, self.storage_capacity - self.storage_used())

    def _storage_kind(self, name: str) -> str:
        """按物品名归桶：weapon → weapons，tool → tools，其余为材料。"""
        return _bucket_kind(name)

    def can_store(self, name: str, count: int = 1) -> bool:
        """能否再存入 count 件 name（容量足够即 True）。"""
        if not self.storage_capacity or count <= 0:
            return False
        return item_volume(name) * count <= self.storage_free()

    def deposit(self, name: str, count: int = 1) -> int:
        """存入最多 count 件 name，受剩余空间截断，返回实际存入件数。

        工具为永久唯一物：已存入则不再重复收纳（返回 0）。
        """
        if not self.storage_capacity or count <= 0:
            return 0
        return _bucket_deposit(self.storage, self.storage_capacity, name, count)

    def withdraw(self, name: str, count: int = 1) -> int:
        """取出最多 count 件 name，返回实际取出件数。"""
        if not self.storage_capacity or count <= 0:
            return 0
        return _bucket_withdraw(self.storage, name, count)

    # ─── 加工（加工塔） ──────────────────────────────────────

    def processing_used(self) -> int:
        """加工塔已占用空间合计。非加工塔恒为 0。"""
        if not self.processing_capacity:
            return 0
        return _bucket_used(self.processing)

    def processing_free(self) -> int:
        """剩余可用的空间单位数。"""
        return max(0, self.processing_capacity - self.processing_used())

    def processing_can_store(self, name: str, count: int = 1) -> bool:
        """能否再存入 count 件 name（容量足够即 True）。"""
        if not self.processing_capacity or count <= 0:
            return False
        return item_volume(name) * count <= self.processing_free()

    def processing_deposit(self, name: str, count: int = 1) -> int:
        """存入最多 count 件 name（工具即装配），受剩余空间截断，返回实际件数。"""
        if not self.processing_capacity or count <= 0:
            return 0
        return _bucket_deposit(self.processing, self.processing_capacity, name, count)

    def processing_withdraw(self, name: str, count: int = 1) -> int:
        """取出最多 count 件 name（取走工具会停用其配方，在制任务照常完工），返回件数。"""
        if not self.processing_capacity or count <= 0:
            return 0
        return _bucket_withdraw(self.processing, name, count)

    # ─── 加工查询（窗口 UI 与加工 tick 共用） ─────────────────

    def process_yield(self, rkey) -> int:
        """配方本批产量：工具配方恒为 tool 产量；无工具配方看增益工具是否已装配。

        在制批次使用开工时冻结的产量（取走/换选工具不影响在制任务）。
        """
        if rkey in self.process_jobs and rkey in self.process_meta:
            return int(self.process_meta[rkey].get("yield", 0))
        rec = gamedata.PROCESS_RECIPES.get(rkey)
        if rec is None:
            return 0
        if rec.get("requires_tool"):
            return int(rec.get("tool") or 0)
        n = int(rec.get("hand") or 0)
        for tool in self.processing["tools"]:
            tdef = TOOL_TYPES.get(tool)
            if tdef is not None and tdef.applies == rkey:
                return int(rec.get("tool") or n)
        return n

    def process_duration(self, rkey) -> float:
        """配方本批耗时：工具配方恒为 time_tool；无工具配方装配增益工具后提速。

        在制批次使用开工时冻结的耗时（取走/换选工具不影响在制任务）。
        """
        if rkey in self.process_jobs and rkey in self.process_meta:
            return float(self.process_meta[rkey].get("duration", 0.0))
        rec = gamedata.PROCESS_RECIPES.get(rkey)
        if rec is None:
            return 0.0
        if rec.get("requires_tool"):
            return float(rec.get("time_tool") or 0.0)
        t = float(rec.get("time_hand") or 0.0)
        for tool in self.processing["tools"]:
            tdef = TOOL_TYPES.get(tool)
            if tdef is not None and tdef.applies == rkey:
                return float(rec.get("time_tool") or t)
        return t

    def _processing_reserved(self) -> int:
        """在制批次全部完工后将要占用的空间（跨批次预留）。

        用于开工/交付时的容量判定：所有在制批次产出的总和也必须放得下，
        避免逐个校验导致的多批叠加超容量。
        """
        recipes = gamedata.PROCESS_RECIPES
        total = 0
        for rkey in self.process_jobs:
            rec = recipes.get(rkey)
            if rec is None:
                continue
            total += self.process_yield(rkey) * item_volume(rec["out"])
        return total

    def can_start_process(self, rkey) -> bool:
        """开工条件：原料≥1、产量>0、（消耗输入腾位后）产物放得下。

        容量判定计入在制批次预留空间（跨批次预留），避免多批同时开工后
        产物总和超过容量而卡死。
        """
        rec = gamedata.PROCESS_RECIPES.get(rkey)
        if rec is None:
            return False
        if self.processing["materials"].get(rec["in"], 0) < 1:
            return False
        out_n = self.process_yield(rkey)
        if out_n <= 0:
            return False
        need = out_n * item_volume(rec["out"]) - item_volume(rec["in"])
        return need <= self.processing_free() - self._processing_reserved()

    def process_block_reason(self, rkey):
        """当前不能开工的原因："input" 缺原料 / "space" 产物空间不足 / None 可开工。"""
        rec = gamedata.PROCESS_RECIPES.get(rkey)
        if rec is None:
            return "input"
        if self.processing["materials"].get(rec["in"], 0) < 1:
            return "input"
        return None if self.can_start_process(rkey) else "space"

    def _start_process(self, rkey, rec):
        """开工：消耗 1 件输入，登记在制任务（任务 key 即配方 key）。

        开工时冻结本批产量/耗时并存入 process_meta，中途取走/换选工具
        不影响在制批次。
        """
        b = self.processing["materials"]
        b[rec["in"]] = b.get(rec["in"], 0) - 1
        if b[rec["in"]] <= 0:
            del b[rec["in"]]
        self.process_meta[rkey] = {
            "yield": self.process_yield(rkey),
            "duration": self.process_duration(rkey),
        }
        self.process_jobs[rkey] = 0.0

    def _update_processing(self, dt):
        """加工塔随时间加工（每帧调用）。

        任务来源：塔内每把工具自动启用其对应配方（自动线）——既有
        `requires_tool` 必需工具配方（bloomery→铁矿→铁块），也有 `applies`
        增益工具配方（石锯 applies 木材→木板 / 锤 applies 石料→圆石 /
        核精炼装置 applies 铀矿→核燃料）。早先自动线只认 requires_tool，
        增益工具放进去不开工（面板显示"停工"），要玩家手动点选配方才生产
        一点——现补上 applies 反查：装上工具即自动加工对应配方（无工具时
        面板手动点选一条自由配方作手动线）。输入开工即消耗，完工产出放回
        同一体积池；产物暂放不下时任务停在完工态等空间。开工后中途取走
        工具/换选配方不影响在制批次（视为已开工的工作）。
        """
        if not self.processing_capacity:
            return
        recipes = gamedata.PROCESS_RECIPES

        # 在制任务推进（含无主旧任务：换选/取工具不让半成品消失）
        for rkey in self.process_jobs:
            self.process_jobs[rkey] += dt

        # 期望任务集：
        # 1) 自动线：工具反查配方（requires_tool 必需工具 + applies 增益工具）
        # 2) 手动线：窗口点选的一条无工具配方
        want = {}
        for tool in self.processing["tools"]:
            tdef = TOOL_TYPES.get(tool)
            if tdef is None:
                continue
            rkey = tdef.applies
            if rkey and rkey in recipes:
                want[rkey] = recipes[rkey]
        for rkey, rec in recipes.items():
            rt = rec.get("requires_tool")
            if rt:
                if rt in self.processing["tools"]:
                    want[rkey] = rec
            elif rkey == self.manual_recipe:
                want[rkey] = rec
        for rkey, rec in want.items():
            if rkey not in self.process_jobs and self.can_start_process(rkey):
                self._start_process(rkey, rec)

        # 完工产出：放得下才交付，否则保持完工态等待空间
        for rkey in list(self.process_jobs):
            rec = recipes.get(rkey)
            if rec is None:
                # 配方已不存在（理论不该发生）：丢弃任务避免卡死
                del self.process_jobs[rkey]
                self.process_meta.pop(rkey, None)
                continue
            if self.process_jobs[rkey] < self.process_duration(rkey):
                continue
            out_n = self.process_yield(rkey)
            if out_n <= 0 or self._processing_reserved() > self.processing_free():
                continue
            del self.process_jobs[rkey]
            self.process_meta.pop(rkey, None)
            m = self.processing["materials"]
            m[rec["out"]] = m.get(rec["out"], 0) + out_n

    def cancel_all_process_jobs(self):
        """取消全部在制任务并返还已消耗的输入（拆除返还用，调用方负责腾空间）。"""
        recipes = gamedata.PROCESS_RECIPES
        refunded = {}
        for rkey in list(self.process_jobs):
            rec = recipes.get(rkey)
            if rec is not None:
                name = rec["in"]
                refunded[name] = refunded.get(name, 0) + 1
            del self.process_jobs[rkey]
            self.process_meta.pop(rkey, None)
        self.manual_recipe = None
        return refunded

    def update(self, dt, enemies, enemy_grid=None, universe=None):
        """更新塔逻辑，返回新创建的抛射体列表（可为空列表）。

        防御塔(no_attack)自身不攻击；其攻击完全来自挂载武器，
        每把武器独立冷却、独立索敌，同帧可各发一弹（攻击效果叠加）。
        universe 供开采塔竖井采掘（传 None 则不采掘）。
        """
        if not self.active:
            return []
        fired = []

        if self.no_attack:
            # 加工塔：随时间加工（与挂载武器互不干扰）
            if self.processing_capacity:
                self._update_processing(dt)
            # 开采塔：竖井采掘（需 universe）
            if self.is_mining and universe is not None:
                self._update_mining(dt, universe)
            # 无攻击防御塔：不索敌不开火，仅依赖挂载武器
            if not self.weapon_mounts:
                return []
            for mount in self.weapon_mounts:
                mount["timer"] -= dt
                if mount["timer"] > 0:
                    continue
                wdef = mount["wdef"]
                target = self._find_target(enemies, enemy_grid, wdef.range,
                                           universe=universe)
                if target:
                    proj = self._fire(target, wdef.range,
                                      wdef.damage * mount["damage_mult"],
                                      special=wdef.special,
                                      cooldown=wdef.cooldown)
                    if proj:
                        fired.append(proj)
                    mount["timer"] = wdef.cooldown
            return fired

        # 攻击塔：原有索敌/开火逻辑
        self.cooldown_timer -= dt
        if self.cooldown_timer <= 0:
            self.target = self._find_target(enemies, enemy_grid, self.range,
                                            universe=universe)
            if self.target:
                proj = self._fire(self.target, self.range, self.damage,
                                  special=self._classic_special(),
                                  cooldown=self.cooldown)
                if proj:
                    fired.append(proj)
                self.cooldown_timer = self.cooldown
        return fired

    def _classic_special(self):
        """经典攻击塔的定位属性集合（供 _fire 统一注入抛射体）。"""
        return {
            "slow_factor": self.type_def.slow_factor,
            "slow_duration": self.type_def.slow_duration,
            "splash_radius": self.type_def.splash_radius,
            "splash_falloff": self.type_def.splash_falloff,
            "bonus_vs": self.type_def.bonus_vs,
            "bonus_mult": self.type_def.bonus_mult,
        }

    def _elevation_drop(self, target):
        """塔对目标的"高打低"高度差（格）。

        drop = 塔.y − 目标.y，> 0 表示塔比目标高（高地优势）。
        塔与敌人的 y 均为"脚下地面 + 1"，差值即相对地形高度差。
        返回值已按 TOWER_ELEV_MAX_DROP 封顶，且下坡为 0（上坡无惩罚）。
        """
        drop = self.position[1] - target.position[1]
        if drop <= 0:
            return 0.0
        return min(config.TOWER_ELEV_MAX_DROP, drop)

    def _effective_range(self, target, rng):
        """有效射程：基础射程 + 每下落 1 格的加成（高打低）。"""
        return rng + config.TOWER_ELEV_RANGE_PER_BLOCK * self._elevation_drop(target)

    def _find_target(self, enemies, enemy_grid=None, rng=None, universe=None):
        """寻找射程内最近的可及敌人（有效射程含高度差加成）。

        universe 非 None 时做弹道可及性（视线）检查：炮口→敌人中心的直线
        弹道被实心方块挡住的目标直接跳过（抛射体必然命中地形停下，子弹
        只会在那里浪费），宁可不打也不把火力锁死在打不到的目标上。
        universe 为 None 保持旧行为（不做视线检查）。
        """
        if not rng or rng <= 0:
            return None
        best = None
        best_idx = -1
        pos = self.position
        px = pos[0]
        py = pos[1]
        pz = pos[2]
        # 炮口高度（弹道可及性检测的起点；防御塔的炮口更高）
        barrel_y = self._barrel_y()
        if enemy_grid is not None:
            # 网格粗筛用最大可能射程，防止高度差加成后的目标被粗筛漏掉
            query_rng = rng + (
                config.TOWER_ELEV_RANGE_PER_BLOCK * config.TOWER_ELEV_MAX_DROP)
            candidates = enemy_grid.query(px, pz, query_rng)
        else:
            candidates = range(len(enemies))
        best_dist_sq = float("inf")
        epb = config.TOWER_ELEV_RANGE_PER_BLOCK
        epmax = config.TOWER_ELEV_MAX_DROP
        for idx in candidates:
            enemy = enemies[idx]
            if not enemy.active:
                continue
            epos = enemy.position
            dx = epos[0] - px
            # 高度差加成内联（与 _elevation_drop/_effective_range 逐位一致）：
            # drop = py - 目标y；≤0 无加成；min(封顶, drop) 逐位同值
            drop = py - epos[1]
            if drop <= 0:
                drop = 0.0
            elif drop > epmax:
                drop = epmax
            eff = rng + epb * drop
            # 轴向粗筛：|dx| > eff ⇒ dx² > eff² ⇒ dist² > eff²，必被下方跳过
            if dx > eff or -dx > eff:
                continue
            dz = epos[2] - pz
            if dz > eff or -dz > eff:
                continue
            dist_sq = dx * dx + dz * dz
            eff_sq = eff * eff
            if dist_sq > eff_sq:
                continue
            # 弹丸可达上限（R9-T2）：即使高度差加成把 eff 撑得很大，抛射体
            # 物理上也够不着超出 config.PROJECTILE_REACH_CEILING 的目标——
            # 只按 _effective_range+LOS 过滤会选到"射程内但弹丸必然半路坠地"
            # 的敌人，塔持续空射。该上限为平地实测最大有效命中（≈11.1~11.5
            # 格）减去安全余量；声明射程（含加成）突破它直接跳过。
            reach_ceil = config.PROJECTILE_REACH_CEILING
            if dist_sq >= reach_ceil * reach_ceil:
                continue
            # 弹道可及性：炮口→敌人中心的直线被实心方块挡住 → 打不到，
            # 跳过（防空耗子弹；地下/墙后敌人不再锁死塔的火力）。
            if (universe is not None
                    and _segment_blocked(universe, px, barrel_y, pz,
                                         epos[0], epos[1], epos[2])):
                continue
            if best is None:
                best_dist_sq = dist_sq
                best = enemy
                best_idx = idx
            elif dist_sq < best_dist_sq or (dist_sq == best_dist_sq
                                            and idx > best_idx):
                best_dist_sq = dist_sq
                best = enemy
                best_idx = idx
        return best

    def _barrel_y(self):
        """炮口/弹丸起始高度（防御塔更高，与其塔身高度一致）"""
        return self.position[1] + (1.6 if self.no_attack else 1.0)

    def _fire(self, target, rng, damage, special=None, cooldown=None):
        """发射抛射体：向 target 方向生成一枚弹丸。

        target/rng/damage 由调用方指定（经典塔传自身属性，
        防御塔挂载武器传武器定义属性）；special 为定位属性字典
        （slow_*/splash_*/bonus_*），缺省为无特效。

        高度差威力加成：塔比目标每下落 1 格，伤害 ×(1 + drop×DMG_PER_BLOCK)，
        封顶于 TOWER_ELEV_MAX_DROP。平地/上坡不变。
        """
        if target is None or not rng or rng <= 0 or not damage:
            return None
        drop = self._elevation_drop(target)
        if drop > 0:
            damage *= 1.0 + config.TOWER_ELEV_DMG_PER_BLOCK * drop
        special = special or {}
        dx = target.position[0] - self.position[0]
        dy = target.position[1] - self._barrel_y()  # 瞄准敌人中心
        dz = target.position[2] - self.position[2]
        dist = (dx * dx + dy * dy + dz * dz) ** 0.5
        if dist > 0:
            dx /= dist
            dy /= dist
            dz /= dist
        # 创建抛射体 — 把定位属性传下去
        proj = Projectile(
            position=(self.position[0], self._barrel_y(), self.position[2]),
            velocity=(dx * config.PROJECTILE_SPEED,
                      dy * config.PROJECTILE_SPEED + config.PROJECTILE_UPWARD_BIAS,  # 额外向上补偿重力
                      dz * config.PROJECTILE_SPEED),
            damage=damage,
            source=self,
            splash_radius=float(special.get("splash_radius", 0.0)),
            splash_falloff=float(special.get("splash_falloff", 0.5)),
            slow_factor=float(special.get("slow_factor", 1.0)),
            slow_duration=float(special.get("slow_duration", 0.0)),
            bonus_vs=special.get("bonus_vs"),
            bonus_mult=float(special.get("bonus_mult", 1.0)),
        )
        return proj

    def take_damage(self, amount):
        """受到伤害。返回是否被摧毁。"""
        self.hp -= amount
        if self.hp <= 0:
            self.hp = 0
            self.active = False
            return True
        return False

    def upgrade(self):
        """升级塔"""
        if self.level < config.MAX_TOWER_LEVEL:
            self.level += 1
            self.damage = self.type_def.base_damage * (config.TOWER_DAMAGE_MULT ** (self.level - 1)) * self.damage_mult
            self.range = self.type_def.base_range * (config.TOWER_RANGE_MULT ** (self.level - 1))
            self.cooldown = self.type_def.base_cooldown * (config.TOWER_COOLDOWN_MULT ** (self.level - 1))
            self.max_hp = gamedata.TOWER_HP.get(self.type_name, 100) * (1.3 ** (self.level - 1))
            self.hp = self.max_hp

    @property
    def display_color(self):
        """获取塔的显示颜色（经典塔用 TOWER_COLORS，武器塔用 WeaponDef.color）

        始终返回 4 元组 (r, g, b, a)。raylib/cffi 的 Color 结构体有 4 个字段，
        若传入 3 元组，缺失的 alpha 字段会被 cffi 填成 0（完全透明），
        塔虽然画出来了却完全看不见。故此处统一补满 alpha=255。

        结果按 source 缓存（source/类型在建成后不变；每帧渲染热点访问）。
        """
        cached = self._display_color_cache
        if cached is not None and cached[0] == self.source:
            return cached[1]
        if self.source and self.source[0] == "weapon":
            from items import WEAPON_TYPES
            wdef = WEAPON_TYPES.get(self.source[1])
            if wdef:
                c = wdef.color
                c = c if len(c) == 4 else (*c, 255)
            else:
                c = gamedata.TOWER_COLORS.get(self.type_name, (128, 128, 128))
                c = c if len(c) == 4 else (*c, 255)
        else:
            c = gamedata.TOWER_COLORS.get(self.type_name, (128, 128, 128))
            c = c if len(c) == 4 else (*c, 255)
        self._display_color_cache = (self.source, c)
        return c

    @property
    def upgrade_cost(self):
        """升级费用"""
        return int(self.type_def.base_cost * (config.UPGRADE_COST_MULT ** (self.level - 1)))


def _weapon_to_tower_type(weapon_def) -> TowerType:
    """
    将 WeaponDef 映射为 TowerType，复用 Tower 的所有机制。
    """
    return TowerType(
        type_name=weapon_def.name,
        name_cn=weapon_def.label_cn,
        base_damage=weapon_def.damage,
        base_range=weapon_def.range,
        base_cooldown=weapon_def.cooldown,
        base_cost=weapon_def.install_fee,
        slow_factor=weapon_def.special.get("slow_factor", 1.0),
        slow_duration=weapon_def.special.get("slow_duration", 0.0),
        splash_radius=weapon_def.special.get("splash_radius", 0.0),
        splash_falloff=weapon_def.special.get("splash_falloff", 0.5),
        bonus_vs=weapon_def.special.get("bonus_vs", None),
        bonus_mult=weapon_def.special.get("bonus_mult", 1.0),
        no_attack=False,          # 武器塔恒可攻击
        weapon_slots=0,           # 武器塔不挂武器
    )


class Enemy:
    """敌人 — 支持多路径选择、地形感知、个性差异"""

    # 敌人性格参数：direct(偏好直线), terrain_aware(避开难走地形), aggressive(偏好开阔)
    # 来自 data/enemies.json 每类型的 personality 字段
    PERSONALITIES = gamedata.ENEMY_PERSONALITIES

    TERRAIN_SPEED_MULTS = gamedata.TERRAIN_SPEED_MULTS
    # 类型级环境响应（天气/群系覆盖/表面方块亲和）与近战攻击配置
    ENVIRONMENTS = gamedata.ENEMY_ENVIRONMENTS
    ATTACKS = gamedata.ENEMY_ATTACKS

    def __init__(self, enemy_type, position, path, all_paths=None, path_index=0, rng=None):
        self.type_name = enemy_type.type_name
        self.type_def = enemy_type
        self.position = list(position)  # [x, y, z]
        self.vy = 0.0                   # 垂直速度（格/秒，负值向下；重力下落用）
        self.path = path                # 实际使用的路径
        self.all_paths = all_paths or [path]  # 所有备选路径
        if self.path is None:
            self.path_points = ()
        elif hasattr(self.path, "points"):
            self.path_points = self.path.points
        else:
            self.path_points = tuple(self.path)
        self.path_index = path_index
        # BUG-105：_request_repath 无解清空路径（path_points=()）时置真，
        # 表示"原地等待"而非"已抵达大本营"，避免到达判定误判为 reached_base。
        self.waiting_for_path = False
        self.health = enemy_type.base_health
        self.max_health = enemy_type.base_health
        self.speed = enemy_type.base_speed
        self.reward = enemy_type.base_reward
        self.active = True
        self.slow_timer = 0.0
        self.slow_factor = 1.0

        # 个性参数
        self.personality = self.PERSONALITIES.get(self.type_name, self.PERSONALITIES["regular"])
        self.env = self.ENVIRONMENTS.get(self.type_name, {})
        self.attack_cfg = self.ATTACKS.get(self.type_name, {})
        # 性格派生参数缓存：(personality 字典引用, climb_pass, dig_speed)。
        # 按引用同一性判失效：读档整体替换 personality 字典后自动重算。
        self._pers_cache = None
        # 下落上限缓存：(personality 字典引用, fall_limit)。与 _pers_cache
        # 同款按引用判失效；单独缓存避免改动既有三元组读取点。
        self._fall_cache = None
        # env 派生查找表（env 仅在此处赋值一次，之后只读）
        self._env_terrain = self.env.get("terrain_speed") or {}
        self._env_affinity = self.env.get("surface_affinity") or {}
        self._env_weather = self.env.get("weather_speed") or {}

        # 个人化路径参数（每个敌人略有不同）
        rng = rng or random
        self.rng = rng  # 保存注入的随机源，运行期决策复用以保证种子可复现
        self.wobble_amp = rng.uniform(0.5, 2.0) * self.personality["wobble"]
        self.wobble_freq = rng.uniform(0.3, 1.0)
        self.wobble_phase = rng.uniform(0, math.tau)
        self.time = rng.uniform(0, 10)  # 错开初始相位
        self.path_noise = rng.uniform(-0.3, 0.3)  # 整体法线偏移（限幅后仍 ≤0.45 路径半宽）

        # 寻路状态：变体号 / 分配路径时的 world_version / 过期复查计时
        self.path_variant = 0
        self.path_version = -1
        self.repath_timer = rng.uniform(0, config.PATH_REPATH_CHECK_INTERVAL)
        # D2/D3 停滞看门狗（瞬态，不落存档）：净位移 + 挖掘进度双判。
        # 窗口 PATH_WAIT_STUCK_SECONDS 内两者都无推进 → 强制重规划（局部
        # 优先）；同版本重试 PATH_WAIT_MAX_ATTEMPTS 次仍无进展 → 清空路径
        # 原地待援（blocked），地形版本变化后自动恢复。
        self._stall_pos = None      # 窗口锚点（世界坐标 xz）
        self._stall_t = 0.0         # 窗口起点（自身时钟秒）
        self._stall_dig = (0.0, 0)  # 窗口起点挖掘进度签名
        self._stall_tries = 0       # 同版本内重试次数
        self._stall_version = -1    # 重试配额所属 world_version
        self._force_local = False   # 下次重规划局部优先（打破采纳同一变体空转）
        self.blocked = False        # 重试耗尽仍无进展（供 Game 汇总/UI）
        # 攻击塔 / 挖掘子状态
        self.attack_target = None
        self.attack_timer = 0.0
        self.dig_target = None      # (x, z) 目标柱
        self.dig_progress = 0.0
        self._dug_count = 0         # 本次挖掘会话实际削格数（会话结束统一 bump 版本）
        self._dig_marked_passage = False  # 会话是否改低了目标列通行高度（同样要 bump）
        self.dig_y = None           # 掘进会话当前削的高度（base+1 或 base+2）
        self.dig_base_y = None      # 会话基准面：敌人起始所在格的地表高度
        self.dig_passed = False     # 会话是否已打通（只挖 Front/Above 两块后置真）

        # 行为状态机：wave / scout_wander / scout_return / attack_tower / dig
        self.behavior = "wave"
        self.home_spawner_id = -1
        self.has_message = False
        self.scout_returned_with_message = False
        self.wander_heading = rng.uniform(0, math.tau)
        self.wander_turn_timer = 0.0
        self.wander_lifetime = 0.0
        self.return_path = ()
        self.return_path_index = 0
        # 侦察兵配置（as_scout 时按刷怪点种类注入；无配置用标准种类兜底）
        self.scout_cfg = None

    @staticmethod
    def as_scout(enemy_type, position, home_spawner_id, rng,
                 noise_dir=None, scout_cfg=None):
        """创建侦察怪（scout_wander 行为）—— 由休眠刷怪点触发派出。

        noise_dir: (dx, dz) 噪音方向（朝最大噪音源），若有则初始 heading 偏向它。
        scout_cfg: 刷怪点种类的侦察兵属性（data/spawners.json），None 用标准种类。
        """
        cfg = scout_cfg or gamedata.STANDARD_SCOUT
        scout = Enemy(enemy_type, position, path=None, rng=rng)
        scout.scout_cfg = cfg
        scout.behavior = "scout_wander"
        scout.home_spawner_id = home_spawner_id
        jitter = cfg.get("noise_jitter", 0.5)
        if noise_dir and (noise_dir[0] or noise_dir[1]):
            # 初始方向偏向噪音源 + 随机扰动
            base_heading = math.atan2(noise_dir[1], noise_dir[0])
            scout.wander_heading = base_heading + rng.uniform(-jitter, jitter)
        else:
            scout.wander_heading = rng.uniform(0, math.tau)
        scout.wander_turn_timer = 0.0  # 立刻触发首次换向（含避水纠正）
        scout.wander_lifetime = cfg.get("wander_duration", 60.0)
        scout.speed = enemy_type.base_speed * cfg.get("wander_speed_mult", 0.8)
        return scout

    @staticmethod
    def pick_path(enemy_type, all_paths, occupied_counts=None, rng=None):
        """根据敌人性格、地形偏好和拥挤程度在变体路径中选择。

        all_paths: list[PathData]（同一刷怪点×类型的变体集）
        occupied_counts: {id(PathData): 使用该变体的活跃敌人数}
        返回选中的 PathData（空集返回 None）。
        """
        if not all_paths:
            return None
        if len(all_paths) == 1:
            return all_paths[0]

        personality = Enemy.PERSONALITIES.get(
            enemy_type.type_name, Enemy.PERSONALITIES["regular"])
        occupied_counts = occupied_counts or {}
        rng = rng or random
        crowd_avoid = float(personality.get("crowd_avoidance", 0.5))

        best_idx = 0
        best_score = -float("inf")
        for i, path in enumerate(all_paths):
            score = 0.0
            # 1. 路径长度（直线型敌人偏好短路径）
            score += float(personality["direct"]) * (-path.length / 50.0)
            # 2. 地形平坦度（地形感知型敌人偏好平缓路径）
            score += float(personality["terrain_aware"]) * (-path.avg_slope / 2.0)
            # 3. 拥挤程度（避开已有敌人多的变体）
            score += -occupied_counts.get(id(path), 0) * 0.5 * crowd_avoid
            # 4. 随机因素
            score += rng.uniform(-0.5, 0.5)

            if score > best_score:
                best_score = score
                best_idx = i

        return all_paths[best_idx]

    def update(self, dt, universe, towers=None, base=None,
               towers_by_cell=None, pathfinder=None):
        """更新敌人移动 — 按行为分派：wave / attack_tower / dig / scout_*"""
        if not self.active:
            return

        self.time += dt

        # 更新减速效果
        if self.slow_timer > 0:
            self.slow_timer -= dt
            if self.slow_timer <= 0:
                self.slow_factor = 1.0

        if self.behavior == "scout_wander":
            return self._update_scout_wander(dt, universe, towers or [], base)
        elif self.behavior == "scout_return":
            return self._update_scout_return(dt, universe)
        elif self.behavior == "attack_tower":
            return self._update_attack_tower(dt, universe, pathfinder)
        elif self.behavior == "dig":
            return self._update_dig(dt, universe, pathfinder)
        else:
            return self._update_wave(dt, universe, towers_by_cell, pathfinder)

    def _fall_limit(self):
        """可下落高差上限（格）：来自 personality.fall_limit，按类型区分。

        与 climb_pass 对称：落差超过此值的下行边，寻路已禁行，运行时跨格
        检测兜底拒绝跨入（防旧路/漏判导致强坠）。缓存照 _pers_cache 模式。
        """
        pers = self.personality
        c = self._fall_cache
        if c is not None and c[0] is pers:
            return c[1]
        fl = max(1, int(math.floor(pers.get("fall_limit", 3))))
        self._fall_cache = (pers, fl)
        return fl

    def _sync_y(self, dt, universe):
        """同步 Y 到地形高度（所有行为共用）。

        足迹半径用敌人实际半宽 0.2（盒宽 0.4）：只把敌人盒子真正压住的列算立足点。
        覆盖层感知：挖掘过的柱取削低后的地表。

        防"贴墙被顶到墙顶"：足迹内若有比本列高超过爬升阈值（climb_pass）的相邻格
        （即敌人即将掘进的"墙"），不计入立足点——敌人贴近高墙时保持本列地表高度
        （掘进会话会钉在 base+1），而不是被 _sync_y 瞬间抬到墙顶。
        """
        x, y, z = self.position
        ox = math.floor(x)
        oz = math.floor(z)
        sha = universe.surface_height_at
        own = sha(ox, oz)
        pers = self.personality
        c = self._pers_cache
        if c is not None and c[0] is pers:
            climb_pass = c[1]
        else:
            climb_pass = max(1, int(math.floor(
                pers.get("climb_limit", 1.5))))
            self._pers_cache = (pers, climb_pass,
                                float(pers.get("dig_speed", 0.0)))
        ix0 = math.floor(x - 0.2)
        ix1 = math.floor(x + 0.2)
        iz0 = math.floor(z - 0.2)
        iz1 = math.floor(z + 0.2)
        if ix0 == ix1 and iz0 == iz1:
            # 快路径：足迹未跨格 → 循环体只覆盖 (ox, oz) 一格。
            # 该格 h = own，永不可能触发"高墙不计立足点"（h > own + climb_pass
            # 需 climb_pass < 0），故 top 恒为 own，与一般路径逐位一致。
            top = own
        else:
            top = 0
            for ix in range(ix0, ix1 + 1):
                for iz in range(iz0, iz1 + 1):
                    h = sha(ix, iz)
                    # 远高于本列的高墙（无论能否挖）都不算立足点，避免
                    # 贴墙/被推挤时 _sync_y 把身体顶到墙顶（“瞬移上墙”）。
                    if h > own + climb_pass:
                        continue
                    if h > top:
                        top = h
        if top < own:
            top = own
        ground_y = top + 1
        # 拱顶站立：mark_passage 会把整列 surface_override 降到洞底，但
        # 站在该列上方悬垂实体上的敌人不能跟着沉下去（会穿过完好方块）。
        # 仅当脚下格是实体、身体所在格不是实体时，把该实体顶面当支撑面；
        # 被挤进实心柱体时仍走原有攀爬逻辑，避免“站在墙里”。
        if self.position[1] > ground_y + 0.05:
            fy = math.floor(self.position[1] - 0.001)
            if fy >= ground_y \
                    and is_solid(universe.get_block(ox, fy, oz)) \
                    and not is_solid(universe.get_block(ox, fy + 1, oz)):
                ground_y = fy + 1
        if self.position[1] < ground_y:
            self.vy = 0.0  # 瞬登/攀爬上台阶：垂直速度归零
            if ground_y - self.position[1] <= climb_pass + 1:
                self.position[1] = ground_y  # 台阶（≤爬升阈值+1）：保持瞬登
            else:
                # 大高差（隧道列顶改写/被挤进崖体）：沿壁攀爬而非瞬移。
                # 瞬移会让敌人穿过完整方块凭空弹到地表（"从地里冒出来"，
                # 且没有挖开痕迹——挖掘视觉只属于挖掘会话）。速率与下降一致。
                self.position[1] = min(ground_y, self.position[1] + 12.0 * dt)
        elif self.position[1] > ground_y:
            # 重力下落（抛物线，取代旧版 12 格/秒匀速"滑翔"）。
            # fall_limit 硬限不在本分支：下方地面比脚下低超过 fall_limit 的格，
            # 寻路已禁行、运行时由跨格检测（wave/scout/attack 推进前查
            # _fall_limit）拒绝跨入；本分支只负责已跨入后的物理下落。
            self.vy += config.GRAVITY * dt
            new_y = self.position[1] + self.vy * dt
            if new_y <= ground_y:
                new_y = ground_y
                self.vy = 0.0  # 落地：垂直速度归零（无坠落伤害）
            self.position[1] = new_y

    def _stand_base_y(self, universe):
        """当前实际站立基准面（挖掘会话用）。

        普通情况就是当前列地表；站在拱顶/邻居高台上时，surface_height_at
        可能是洞底，需在足迹内找脚下实体顶面（且身体格非实体），避免把
        通道底当基准、也避免把半空中的插值高度当基准。
        """
        ox = math.floor(self.position[0])
        oz = math.floor(self.position[2])
        own = universe.surface_height_at(ox, oz)
        y = self.position[1]
        if y <= own + 1.05:
            return own
        fy = math.floor(y - 0.001)
        ix0 = math.floor(self.position[0] - 0.2)
        ix1 = math.floor(self.position[0] + 0.2)
        iz0 = math.floor(self.position[2] - 0.2)
        iz1 = math.floor(self.position[2] + 0.2)
        for ix in range(ix0, ix1 + 1):
            for iz in range(iz0, iz1 + 1):
                if is_solid(universe.get_block(ix, fy, iz)) \
                        and not is_solid(universe.get_block(ix, fy + 1, iz)):
                    return max(own, fy)
        return own

    def can_step_aside_to(self, universe, nx, nz):
        """分离推挤是否允许把身体移到 (nx, nz)。

        只允许上坡高差不超过 climb_pass；正在挖掘的敌人完全不可被推挤
        （会话基准面/目标柱依赖其所在格）。旧实现只改 x/z，整排敌人会把
        前排挤进高墙格，随后 _sync_y 把身体抬到墙顶（“瞬移到墙顶”）。
        """
        if self.behavior == "dig":
            return False
        ox = math.floor(self.position[0])
        oz = math.floor(self.position[2])
        dxc = math.floor(nx)
        dzc = math.floor(nz)
        if ox == dxc and oz == dzc:
            return True
        pers = self.personality
        c = self._pers_cache
        if c is not None and c[0] is pers:
            climb_pass = c[1]
        else:
            climb_pass = max(1, int(math.floor(
                pers.get("climb_limit", 1.5))))
            self._pers_cache = (pers, climb_pass,
                                float(pers.get("dig_speed", 0.0)))
        own = universe.surface_height_at(ox, oz)
        # 站在拱顶时列顶是洞底，用脚下实际高度作为当前面。
        stand = max(own, int(math.floor(self.position[1] - 1e-6)))
        dest = universe.surface_height_at(dxc, dzc)
        # BUG-#39：下降侧对称校验——与运行时跨格 fall_limit 硬限对齐，推挤
        # 不允许把身体推进"比当前格低超过 fall_limit"的下坡格（否则制造落差
        # > fall_limit 的触发几何，落入下坡死锁）。fall_limit 复用
        # self._fall_limit()（同款 personality 读法 + _fall_cache）。
        fall_limit = self._fall_limit()
        return (dest - stand <= climb_pass) and (stand - dest <= fall_limit)

    def _update_wave(self, dt, universe, towers_by_cell=None, pathfinder=None):
        """波次怪：沿平滑折线走向大本营；途中遇塔攻击、遇高墙挖掘。"""
        # ── 周期性重估：world_version 变化 → 换用新变体路径 ──
        # BUG-105/BUG-127：本块原先位于下方"路径非空"守卫之内，一旦路径被
        # 清空（原地等待 waiting_for_path）就再也不运行，敌人既走不动也
        # 无法重估。提到守卫之外使其每帧都跑：原地等待时等基地重新可达即
        # 自动恢复寻路；读档的 attack_tower/dig 敌人（空路径 + path_version
        # =-1）返回 wave 后下一帧也能在此触发 _request_repath。
        #
        # D2/D3 停滞看门狗（全状态版）：覆盖 waiting 与"挖掘-断路-重寻路"
        # 空转两类停滞。判据 = 净位移与挖掘进度在 PATH_WAIT_STUCK_SECONDS 内
        # 均无推进（长挖掘会话 XZ 钉住属正常，见 _dug_count/dig_progress）。
        # 命中后强制过期重规划，且下次重规划局部优先（_force_local，打破
        # "采纳同一变体→再空转"的循环）；同一 world_version 内重试配额
        # PATH_WAIT_MAX_ATTEMPTS，耗尽则清空路径原地待援（blocked=True），
        # 地形版本变化后自动恢复。没有该看门狗时，这类空转在游戏里是
        # 永久静默的（只有仿真脚本能发现）。
        px, pz = self.position[0], self.position[2]
        dig_sig = (round(self.dig_progress, 3), self._dug_count)
        if self._stall_pos is None:
            self._stall_pos = (px, pz)
            self._stall_t = self.time
            self._stall_dig = dig_sig
        else:
            ddx = px - self._stall_pos[0]
            ddz = pz - self._stall_pos[1]
            if ddx * ddx + ddz * ddz >= 0.25 or dig_sig != self._stall_dig:
                self._stall_pos = (px, pz)
                self._stall_t = self.time
                self._stall_dig = dig_sig
                self.blocked = False
            elif (self.time - self._stall_t
                    >= config.PATH_WAIT_STUCK_SECONDS):
                if self._stall_version != universe.world_version:
                    self._stall_version = universe.world_version
                    self._stall_tries = 0
                if self._stall_tries < config.PATH_WAIT_MAX_ATTEMPTS:
                    self._stall_tries += 1
                    self._stall_t = self.time
                    self._stall_pos = (px, pz)
                    # 强制过期 + 立即触发：本帧走下面的周期重估分支；
                    # 局部优先可避开"同一变体被反复采纳"的空转。
                    self.path_version = -1
                    self.repath_timer = 0.0
                    self._force_local = True
                elif not self.blocked:
                    self.blocked = True
                    # 停止空转：清空路径原地待援（到达判定据此跳过，BUG-105）
                    self.path = None
                    self.all_paths = []
                    self.path_points = ()
                    self.waiting_for_path = True
                    self.path_version = universe.world_version
                    print(f"[enemy] 阻塞: {self.type_name}@"
                          f"({self.position[0]:.1f},{self.position[2]:.1f}) "
                          f"sid={self.home_spawner_id} 停滞重试 "
                          f"{config.PATH_WAIT_MAX_ATTEMPTS} 次仍无进展，"
                          f"原地待援（地形变化后自动重估）")
        self.repath_timer -= dt
        if self.repath_timer <= 0:
            self.repath_timer = config.PATH_REPATH_CHECK_INTERVAL
            if (pathfinder is not None
                    and self.path_version != universe.world_version):
                self._request_repath(universe, pathfinder)

        if self.path_index < len(self.path_points):
            # 就近接入后 path_index 已在界内；保留 min 钳制作为重估换路后的
            # 目标刷新（BUG-105 把重估上提，此处统一取当前有效路点）。
            target = self.path_points[min(
                self.path_index, len(self.path_points) - 1)]

            # ── 前方/脚下塔格检测 → 进入攻击 ──
            # 换路锚定后绑定只读局部（position 是同一列表对象，写入等价）
            pos = self.position
            pp = self.path_points
            px = pos[0]
            pz = pos[2]
            if towers_by_cell:
                ax = math.floor(px)
                az = math.floor(pz)
                tower = towers_by_cell.get((ax, az))
                # 同层判定：隧道/拱洞里的敌人隔着完整地面，不攻击地表的塔
                if tower is not None and tower.active \
                        and abs(tower.position[1] - self.position[1]) <= 1.5:
                    reach = float(self.attack_cfg.get(
                        "range", config.PATH_SIEGE_DEFAULT_RANGE)) + 0.2
                    dx = tower.position[0] - px
                    dz = tower.position[2] - pz
                    if dx * dx + dz * dz <= reach * reach:
                        self.behavior = "attack_tower"
                        self.attack_target = tower
                        self.attack_timer = 0.0
                        return None

            # 个人化偏移：正弦摆动 + 固定偏移，沿路径段法线方向施加
            # 最后几格内线性收敛到 0，避免横向噪声把敌人甩到大本营外面凭空消失
            wobble = math.sin(self.time * self.wobble_freq + self.wobble_phase) * self.wobble_amp
            total = len(pp)
            tail = min(8, total)
            remain = max(0, total - self.path_index - 1)
            converge = 1.0 if tail == 0 else min(1.0, remain / tail)

            # 路径段方向 → 垂直单位向量
            prev = None
            seg = None
            if 0 < self.path_index < total:
                prev = pp[self.path_index - 1]
                seg = pp[self.path_index]
            elif self.path_index + 1 < total:
                prev = pp[self.path_index]
                seg = pp[self.path_index + 1]
            if prev is not None and seg is not None:
                sdx = seg[0] - prev[0]
                sdz = seg[2] - prev[2]
                sdist = (sdx * sdx + sdz * sdz) ** 0.5
                if sdist > 1e-6:
                    nx = -sdz / sdist
                    nz = sdx / sdist
                else:
                    nx, nz = 0.0, 1.0
            else:
                nx, nz = 0.0, 1.0

            offset = (self.path_noise + wobble * 0.3) * converge
            # 限幅到 0.3：敌人盒 0.4 宽(半宽0.2)，0.3 偏移使敌人完全落在路径
            # 走廊内，不会伸进相邻格的塔体
            offset = max(-0.3, min(0.3, offset))
            tx_w = target[0] + nx * offset
            tz_w = target[2] + nz * offset

            dx = tx_w - px
            dz = tz_w - pz
            dist_sq = dx * dx + dz * dz
            if dist_sq < 0.25:
                self.path_index += 1
            else:
                # 类型化速度：群系覆盖 × 表面方块亲和 × 天气
                terrain_speed = self._get_terrain_speed(universe)
                surface_mult = self._get_surface_mult(universe)
                weather_mult = self._get_weather_mult(universe)
                speed = (self.speed * self.slow_factor * terrain_speed
                         * surface_mult * weather_mult)
                dist = dist_sq ** 0.5
                step = speed * dt
                inv = step / dist
                if inv >= 1.0:
                    nx_w, nz_w = tx_w, tz_w
                else:
                    nx_w = px + dx * inv
                    nz_w = pz + dz * inv

                # ── 进入新格检测（平滑段可跨几十格，必须逐帧查踏入格）──
                cur_cx = math.floor(px)
                cur_cz = math.floor(pz)
                ent_cx = math.floor(nx_w)
                ent_cz = math.floor(nz_w)
                if (ent_cx, ent_cz) != (cur_cx, cur_cz):
                    if towers_by_cell:
                        tower = towers_by_cell.get((ent_cx, ent_cz))
                        # 同层判定（同 wave 脚下检测）：跨层（如隧道里经过
                        # 地表塔格）不进入攻击，照常通行
                        if tower is not None and tower.active \
                                and abs(tower.position[1] - self.position[1]) <= 1.5:
                            self.behavior = "attack_tower"
                            self.attack_target = tower
                            self.attack_timer = 0.0
                            return None
                    pers = self.personality
                    c = self._pers_cache
                    if c is not None and c[0] is pers:
                        climb_pass = c[1]
                        dig_speed = c[2]
                    else:
                        climb_pass = max(1, int(math.floor(
                            pers.get("climb_limit", 1.5))))
                        dig_speed = float(pers.get("dig_speed", 0.0))
                        self._pers_cache = (pers, climb_pass, dig_speed)
                    h_cur = universe.surface_height_at(cur_cx, cur_cz)
                    h_ent = universe.surface_height_at(ent_cx, ent_cz)
                    # fall_limit 硬限：目标格比脚下低超过 fall_limit → 视为墙
                    # 拒绝跨入（与 A* 下行硬禁行、运行时重力下落对齐，BUG-102）。
                    # 断路重寻路（改道），与 climb_pass 的"挖不动"拒绝路径一致。
                    if h_cur - h_ent > self._fall_limit():
                        # D5 偏移瞄准副作用：法线摆动偏移（±0.3）会把瞄准点
                        # 从路径点侧移，可能让"这一步"落进相邻的非法落差格；
                        # 而 A*/_approach_ok 校验的是**无偏移直线**（路径点
                        # 本身可达）。若因此每帧断路→重规划→投影回同一路径点
                        # →再偏移，敌人会"有路径却零位移"永久空转（实测
                        # seed3091 高山区域 23 个组合）。修法：先退回无偏移
                        # 瞄准重试本步（同格内或合法跨格都接受），仍非法才
                        # 断路重规划。
                        retried = False
                        ddx0 = target[0] - px
                        ddz0 = target[2] - pz
                        dist0 = (ddx0 * ddx0 + ddz0 * ddz0) ** 0.5
                        if dist0 > 1e-6:
                            step0 = min(speed * dt, dist0)
                            nx0 = px + ddx0 / dist0 * step0
                            nz0 = pz + ddz0 / dist0 * step0
                            c0x = math.floor(nx0)
                            c0z = math.floor(nz0)
                            if (c0x, c0z) == (cur_cx, cur_cz):
                                nx_w, nz_w = nx0, nz0
                                retried = True
                            else:
                                h0 = universe.surface_height_at(c0x, c0z)
                                if (h_cur - h0 <= self._fall_limit()
                                        and h0 - h_cur <= climb_pass):
                                    nx_w, nz_w = nx0, nz0
                                    retried = True
                        if not retried:
                            self.path_version = -1
                            if pathfinder is not None:
                                self._request_repath(universe, pathfinder)
                            return None
                    if h_ent - h_cur > climb_pass:
                        if (universe.dig_forbidden_at(ent_cx, ent_cz)
                                or not universe._is_land_pure(ent_cx, ent_cz)
                                or universe.enemy_dig_refused(
                                    ent_cx, ent_cz, h_cur + 1)):
                            # "挖不动/不该挖"的地方（大本营 5×5 禁挖区 / 海洋
                            # 非陆地柱 / D2 平台守卫区低于平台-1 的开挖，
                            # dig_front_block 或 _update_dig 守卫必拒）：不挖
                            # 也不穿。穿过去会靠锚定从地基里攀爬钻出（"地面
                            # 完好从地下钻出来"）或进入注定空转的挖掘会话；
                            # A* 的 no_dig_mask / land / dig_floor 约束已不
                            # 规划这种边（BUG-101/D2），这里属旧路/平滑侧漏
                            # 兜底——断路并立即强制重寻路（若重寻路也无解，
                            # _request_repath 会清空路径让敌人原地等待）。
                            self.path_version = -1
                            if pathfinder is not None:
                                self._request_repath(universe, pathfinder)
                            return None
                        if dig_speed > 0:
                            # 基准面必须在触发当帧快照：下一帧再按“当前格”
                            # 采样会把拥挤推挤后的高柱地表当成通道底，导致
                            # 挖高处方块/被顶到墙顶（_update_dig 只保留旧存档
                            # 兜底）。_stand_base_y 会区分拱顶站立与半空。
                            base_y = self._stand_base_y(universe)
                            self.behavior = "dig"
                            self.dig_target = (ent_cx, ent_cz)
                            self.dig_progress = 0.0
                            self._dug_count = 0
                            self.dig_y = None
                            self.dig_base_y = base_y
                            self.dig_passed = False
                            # 立即钉住身体高度到起始地表（修复被 _sync_y 顶到墙顶）
                            self.position[1] = base_y + 1.0
                            return None
                        # 不能挖：理论上 A* 已保证不发生（兜底照常通过）
                pos[0] = nx_w
                pos[2] = nz_w

        # BUG-104 原地升高（盘山路/坑内兜底）：沿路径把"地表仍高于自己
        # +爬坡阈值"的当前列逐级上台阶挖穿，直到与列顶平齐再继续走。
        # 挖掘每次只抬 1 层（_update_dig 的 base+1 台阶语义），多列/单列
        # 高崖可由此逐级爬上——否则敌人会陷在柱/坑内永远"走不动"。
        if (self.path_points and self.behavior == "wave"):
            pers = self.personality
            c = self._pers_cache
            if c is not None and c[0] is pers:
                climb_pass = c[1]
                dig_speed = c[2]
            else:
                climb_pass = max(1, int(math.floor(
                    pers.get("climb_limit", 1.5))))
                dig_speed = float(pers.get("dig_speed", 0.0))
                self._pers_cache = (pers, climb_pass, dig_speed)
            if dig_speed > 0:
                ox = math.floor(self.position[0])
                oz = math.floor(self.position[2])
                stand = self._stand_base_y(universe)
                if (universe.surface_height_at(ox, oz) - stand > climb_pass
                        and not self._dig_refused(universe, ox, oz,
                                                  stand + 1)):
                    self.behavior = "dig"
                    self.dig_target = (ox, oz)
                    self.dig_progress = 0.0
                    self._dug_count = 0
                    self.dig_y = None
                    self.dig_base_y = stand
                    self.dig_passed = False
                    return None

        self._sync_y(dt, universe)

        # 到达大本营——只认地表层抵达：地基下方隧道走到目标格不算攻入，
        # 敌人先被锚定拉回地基面（目标格列顶受 3×3 禁挖守卫，必然可爬回
        # 地表），到位后才结算，防"从大本营下方进入大本营"
        # BUG-105：_request_repath 无解清空路径时 waiting_for_path 为真，
        # 空路径（0>=0）不代表抵达——原地等待，跳过移动与到达结算。
        if not self.waiting_for_path and self.path_index >= len(self.path_points):
            base_y = universe.surface_height_at(
                universe.base_x, universe.base_z) + 1.0
            if self.position[1] < base_y - 2.0:
                self._sync_y(dt, universe)
                return None
            self.active = False
            return "reached_base"

        return None

    # ─── 攻击塔 / 挖掘状态 ──────────────────────────────────

    def _update_attack_tower(self, dt, universe, pathfinder=None):
        """近战拆塔：贴身并按 attack 配置周期伤害，塔倒后回 wave。"""
        tower = self.attack_target
        if tower is None or not tower.active:
            self.attack_target = None
            self.behavior = "wave"
            self.path_version = -1  # 塔已不在，尽快重估路线
            return None
        dx = tower.position[0] - self.position[0]
        dz = tower.position[2] - self.position[2]
        dy = tower.position[1] - self.position[1]
        dist_sq = dx * dx + dz * dz
        reach = float(self.attack_cfg.get("range",
                                          config.PATH_SIEGE_DEFAULT_RANGE))
        if abs(dy) > 1.5 or dist_sq > (reach + 1.0) ** 2:
            # 目标超出追击范围或不在同层（锚定跨层变化）：回 wave 重新判定
            self.attack_target = None
            self.behavior = "wave"
            return None
        if dist_sq > (reach * 0.6) ** 2:
            # 贴身移动（R1-B3：与 wave 移动一致，补上 surface/weather 倍率）
            d = dist_sq ** 0.5
            step = (self.speed * self.slow_factor
                    * self._get_terrain_speed(universe)
                    * self._get_surface_mult(universe)
                    * self._get_weather_mult(universe)) * dt
            adv = min(step, d)
            nx = self.position[0] + dx / d * adv
            nz = self.position[2] + dz / d * adv
            # 追击不经跨入检测：前方若抬升超过爬升阈值，不穿岩追击——
            # 断路回 wave 重寻路（A* 会给出含挖掘的可行围攻路线）
            c = self._pers_cache
            if c is not None and c[0] is self.personality:
                climb_pass = c[1]
            else:
                climb_pass = max(1, int(math.floor(
                    self.personality.get("climb_limit", 1.5))))
            if (universe.surface_height_at(math.floor(nx), math.floor(nz))
                    - universe.surface_height_at(math.floor(self.position[0]),
                                                 math.floor(self.position[2]))
                    > climb_pass):
                self.attack_target = None
                self.behavior = "wave"
                self.path_version = -1
                return None
            # fall_limit 硬限：追击前方下落落差超限 → 同样不跨（视为墙），
            # 弃攻回 wave 重寻路，与上爬拒绝对称（BUG-102）。
            if (universe.surface_height_at(math.floor(self.position[0]),
                                           math.floor(self.position[2]))
                    - universe.surface_height_at(math.floor(nx), math.floor(nz))
                    > self._fall_limit()):
                self.attack_target = None
                self.behavior = "wave"
                self.path_version = -1
                return None
            self.position[0] = nx
            self.position[2] = nz
            self._sync_y(dt, universe)
            # 移动后重新计算与塔距离：伤害只在实际射程内生效，
            # 避免进入攻击态（reach+0.2 探测圈）首帧就从探测圈边缘开火
            dx = tower.position[0] - self.position[0]
            dz = tower.position[2] - self.position[2]
            dist_sq = dx * dx + dz * dz
        self.attack_timer -= dt
        if self.attack_timer <= 0 and dist_sq <= reach * reach:
            self.attack_timer = float(self.attack_cfg.get("cooldown", 1.2))
            destroyed = tower.take_damage(float(self.attack_cfg.get("damage", 4)))
            if destroyed:
                # 塔倒：游戏侧清理塔格并 bump world_version，这里只切回行走
                self.attack_target = None
                self.behavior = "wave"
                self.path_version = -1
        return None

    def _end_dig(self, universe):
        """结束挖掘会话：本次会话实际改变了地形（削过格或改低了通行高度）
        则统一推进 world_version（BUG-103：mark_passage 单独降列顶也要 bump，
        否则寻路缓存不失效，敌人会拿到过期路径就地断路）。"""
        if self._dug_count > 0 or getattr(self, "_dig_marked_passage", False):
            universe.bump_world_version()
            self._dug_count = 0
            self._dig_marked_passage = False
        self.dig_target = None
        self.dig_y = None
        self.dig_base_y = None
        self.dig_passed = False

    def on_removed(self, universe):
        """敌人被移除时清理未结束的挖掘会话（削过格则统一推进版本）。"""
        if self.dig_target is not None or self._dug_count > 0:
            self._end_dig(universe)
            self.behavior = "wave"  # 避免死亡单位被当成侦察兵处理

    def _dig_block_present(self, universe, x, y, z):
        """(x, y, z) 是否为可挖实体方块（非 air/水/岩浆/基岩）。"""
        blk = universe.get_block(x, y, z)
        return blk not in (BLOCK_ID["air"], BLOCK_ID["water"],
                           BLOCK_ID["lava"], BLOCK_ID["bedrock"])

    @staticmethod
    def _dig_refused(universe, x: int, z: int, stand_y=None) -> bool:
        """(x, z) 列是否属于"挖不动/不该挖"的地方（dig_front_block 必定拒绝，
        或开挖会触发平台守卫）。

        BUG-101/104：与 world.dig_block_id 的守卫对齐——
          1. 大本营 5×5 禁挖区：任何深度不可挖（dig_forbidden_at）；
          2. 非陆地/海洋柱：_is_land_pure 失败，dig_block_id 直接拒绝。
        D2（平台挖掘守卫）：stand_y = 本次挖掘将标记的通行高度（_update_dig
        调用方传 base+1；原地盘山路调用方传 stand+1）。平台圆盘内该高度低于
        平台高度-1 的开挖会把自己锁在平台边缘（回环爬升 >climb_pass 且
        平台内禁挖 → 永久等待），与 pathfinding.astar 的 dig_floor 同源拒绝。
        其余拒绝（基岩 y<2、空气/水/岩浆方块）由 _dig_block_present /
        y>=2 守卫覆盖，不会走到 dig_front_block。
        命中即代表"需要挖的经过那里，然后挖不动/不该挖"——敌人必须改道，
        绝不能原地空转（旧实现会在 dig_progress 循环里永远转圈）。
        """
        if universe.dig_forbidden_at(x, z):
            return True
        if not universe._is_land_pure(x, z):
            return True
        if stand_y is not None and universe.enemy_dig_refused(x, z, stand_y):
            return True
        return False

    def _abort_dig_refused(self, universe, pathfinder):
        """目标列"挖不动"：结束会话、断路并强制重寻路（BUG-101）。

        与 wave 级守卫（进入禁挖区断路）语义一致，这里覆盖所有
        dig_block_id 拒绝原因（大本营 5×5 / 海洋柱 / 基岩 / 未来新增
        规则），保证任何"需要挖的经过那里，然后挖不动"的情况都不会
        让敌人空转：重寻路有解 → 绕行；无解 → 清路径原地等待。
        """
        self._end_dig(universe)
        self.behavior = "wave"
        self.path_version = -1
        if pathfinder is not None:
            self._request_repath(universe, pathfinder)

    def _finish_dig(self, universe, pathfinder):
        """结束掘进会话：地形有变（削过格或改低通行高度）则统一 bump 版本、
        回 wave 并请求重估路线（BUG-103 使 mark_passage 单独降列顶也触发重估）。"""
        changed = self._dug_count > 0 or getattr(self, "_dig_marked_passage", False)
        self._end_dig(universe)
        self.behavior = "wave"
        self.path_version = -1
        if changed and pathfinder is not None:
            self._request_repath(universe, pathfinder)

    def _update_dig(self, dt, universe, pathfinder=None):
        """掘进式爬坡挖掘前方高墙：只挖"紧邻上方头顶"（base+2）与
        "头顶上方第二块"（base+3）两块空间，然后把目标列通行高度标为
        base+1——敌人因此**上抬一层**（BUF-104 盘山路：挖一格升一格，
        连续多格即可沿山坡/崖壁逐级爬升，不再"隧道永远在自己高度"）。
        顺序：先削前方头部空间，削空即向前走；走进墙列后若头顶还压着
        方块（base+3）再挖；两块都空后把目标列通行高度标记为 base+1
        （surface_override），上方悬空块保留（拱门视觉），会话结束统一
        bump 版本。

        相比旧行为（削 base+1/base+2、通行高=base 的等高层隧道）：
        base+1 实心块保留 = 迈上一格的台阶面；敌人从 base 站到 base+1，
        通道截面（base+2/base+3 两块）真实挖空，仍是可见小通道。
        """
        if self.dig_target is None:
            self._end_dig(universe)
            self.behavior = "wave"
            return None
        tx, tz = self.dig_target
        dig_speed = float(self.personality.get("dig_speed", 0.0))
        if dig_speed <= 0:
            self._end_dig(universe)
            self.behavior = "wave"
            return None
        climb_pass = max(1, int(math.floor(
            self.personality.get("climb_limit", 1.5))))
        # 会话基准面：触发时已快照；旧存档没有该字段时按当前站立面兜底。
        if self.dig_base_y is None:
            self.dig_base_y = self._stand_base_y(universe)
        base = self.dig_base_y
        # 掘进全程钉住身体高度在通道内（不 _sync_y，避免被列顶顶起）
        self.position[1] = float(base) + 2.0

        # 早退守卫：尚未打通前，目标列顶-基准 ≤ 可爬高差 → 台阶已可上，结束
        # （保持 <= climb_pass：1 格台无需挖掘即可走；≥2 才进入本会话，
        # 否则 2 格台会每帧触发-早退无限循环）
        if not self.dig_passed and universe.surface_height_at(tx, tz) - base <= climb_pass:
            self._finish_dig(universe, pathfinder)
            return None

        # BUG-101/104：目标列属于"挖不动"的地方（大本营 5×5 / 海洋非陆地柱）
        # → 立即断路并请求重寻路，绝不进入挖掘会话空转（方块在、却削不掉）。
        if self._dig_refused(universe, tx, tz, base + 1):
            self._abort_dig_refused(universe, pathfinder)
            return None

        in_cell = (math.floor(self.position[0]) == tx
                   and math.floor(self.position[2]) == tz)

        # 阶段 1：削前方头顶块（base+2）
        if self._dig_block_present(universe, tx, base + 2, tz):
            self.dig_y = base + 2
            self.dig_progress += dig_speed * dt
            while self.dig_progress >= 1.0:
                self.dig_progress -= 1.0
                if self._dig_block_present(universe, tx, base + 2, tz):
                    if not universe.dig_front_block(tx, base + 2, tz):
                        # 兜底：方块在但拒挖（任何禁挖原因）→ 改道，不空转
                        self._abort_dig_refused(universe, pathfinder)
                        return None
                    self._dug_count += 1
            return None

        # 阶段 2：正前方头部已空 → 向墙列推进（尚未进入则每帧走一步）
        if not in_cell:
            tcx = tx + 0.5
            tcz = tz + 0.5
            dx = tcx - self.position[0]
            dz = tcz - self.position[2]
            d = (dx * dx + dz * dz) ** 0.5
            if d > 1e-4:
                # 与其他移动路径一致：挖掘推进也受地形速度倍率影响；
                # R1-B3：与 wave 移动一致，补上 surface/weather 倍率。
                step = min(self.speed * self.slow_factor
                           * self._get_terrain_speed(universe)
                           * self._get_surface_mult(universe)
                           * self._get_weather_mult(universe) * dt, d)
                self.position[0] += dx / d * step
                self.position[2] += dz / d * step
            self.position[1] = float(base) + 2.0
            # 跨入目标列后本帧继续处理上方方块
            if (math.floor(self.position[0]) != tx
                    or math.floor(self.position[2]) != tz):
                return None

        # 阶段 3：已在墙列内 → 若头顶还压着方块（base+3），挖"头上第二块"
        if (universe.surface_height_at(tx, tz) - base >= 3
                and self._dig_block_present(universe, tx, base + 3, tz)):
            self.dig_y = base + 3
            self.dig_progress += dig_speed * dt
            while self.dig_progress >= 1.0:
                self.dig_progress -= 1.0
                if self._dig_block_present(universe, tx, base + 3, tz):
                    if not universe.dig_front_block(tx, base + 3, tz):
                        # 兜底：方块在但拒挖（任何禁挖原因）→ 改道，不空转
                        self._abort_dig_refused(universe, pathfinder)
                        return None
                    self._dug_count += 1
            return None

        # 阶段 4：头部两块都空 → 标记通行高度 base+1（台阶）并结束会话。
        # base+1 实心块 = 迈上一格的台阶面（盘山路：每挖一格升一格）；
        # 上方悬空块保留（拱门视觉）。
        if not self.dig_passed:
            if universe.mark_passage(tx, tz, base + 1):
                # BUG-103：passage 实际改低也算地形变化（即使本会话没削块），
                # 由 _end_dig 统一 bump world_version
                self._dig_marked_passage = True
            self.dig_passed = True
        self._finish_dig(universe, pathfinder)
        return None

    def _request_repath(self, universe, pathfinder):
        """向 pathfinder 取最新变体并就近换路。"""

        # BUG-103：只下发"首段可通行"的变体——当前格到接入点的首段若
        # 立刻需要"挖进禁挖区/海洋柱"（运行时逐帧断路守卫必拒），敌人
        # 会每帧被断路且位置不更新，原地"走不动"（有路径但零位移）。
        def _approach_ok(path):
            from pathfinding import _walk_cells
            pts = path.points
            if not pts:
                return True
            px, pz = self.position[0], self.position[2]
            # 就近投影（与下方接入一致）：目标 = 最近段终点
            best_seg = 0
            best_d2 = float("inf")
            for i in range(len(pts) - 1):
                ax, az = pts[i][0], pts[i][2]
                bx, bz = pts[i + 1][0], pts[i + 1][2]
                abx, abz = bx - ax, bz - az
                L2 = abx * abx + abz * abz
                if L2 < 1e-9:
                    t = 0.0
                else:
                    t = ((px - ax) * abx + (pz - az) * abz) / L2
                    t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
                qx = ax + abx * t
                qz = az + abz * t
                dx = px - qx
                dz = pz - qz
                d2 = dx * dx + dz * dz
                if d2 < best_d2:
                    best_d2 = d2
                    best_seg = i
            tgt = pts[min(best_seg + 1, len(pts) - 1)]
            climb_pass = max(1, int(math.floor(
                self.personality.get("climb_limit", 1.5))))
            # BUG-#39（敌人下坡死锁）：下降侧与上爬对称的安全网补齐。
            # 原先本函数只拒上爬，对首段含落差 > fall_limit 下坡的候选恒返回
            # True → 运行时（_update_wave 内 h_cur - h_ent > _fall_limit()
            # 断路）每帧断路重寻路 → 又选回同一变体 → 位置永不更新（局部硬
            # 冻结）。fall_limit 用 self._fall_limit()（与运行时同一读法：
            # personality.fall_limit 地板取整+下限 1，且带 _fall_cache），
            # 与 pathfinding.py 下行硬禁行、运行时断路语义对齐：落差超限即
            # 拒绝该候选。
            fall_limit = self._fall_limit()
            h_prev = None
            for (wx, wz) in _walk_cells((px, pz), (tgt[0], tgt[2])):
                h_ent = universe.surface_height_at(wx, wz)
                # 下降校验：上一格比当前格高出超过 fall_limit → 该候选不可走
                # （与运行时"当前格高 - 目标格高 > fall_limit ⇒ 断路拒跨入"一致）
                if h_prev is not None and h_prev - h_ent > fall_limit:
                    return False
                if h_prev is not None and h_ent - h_prev > climb_pass:
                    if (universe.dig_forbidden_at(wx, wz)
                            or not universe._is_land_pure(wx, wz)):
                        return False
                    # D2 平台守卫（与 astar 的 dig_floor、_update_dig 的
                    # _dig_refused 同源）：此处上爬需开挖，通行高度 = h_prev+1；
                    # 平台区内低于平台-1 的开挖必被运行时守卫拒绝。若不在此
                    # 拒绝，敌人会"接入→跨入→挖被拒→中止→再接入"无限循环
                    # （实测 seed79 tank 在崖壁 (4,891) 对 (3,5) 循环 4 万+ 次）。
                    if universe.enemy_dig_refused(wx, wz, h_prev + 1):
                        return False
                h_prev = h_ent
            return True

        sid = self.home_spawner_id
        if sid is None or sid < 0 or sid not in universe.spawners:
            self.path_version = universe.world_version
            return
        # 看门狗标记的"局部优先"：停滞时先试从脚下规划，避开"采纳同一
        # 变体→再空转"的循环；局部无解再落回常规变体流程。
        path = None
        variants = []
        local_from_here = False
        if self._force_local and hasattr(pathfinder, "plan_from"):
            self._force_local = False
            path = pathfinder.plan_from(
                (math.floor(self.position[0]),
                 math.floor(self.position[2])),
                self.type_name)
            local_from_here = path is not None
        if path is None:
            variants = pathfinder.get_variants(sid, self.type_name)
            if variants:
                # BUG-103：优先选中项，其余变体按序兜底，只下发首段可通的路径
                picked = Enemy.pick_path(self.type_def, variants,
                                         pathfinder.path_occupancy,
                                         universe.rng)
                order = variants
                if picked is not None:
                    order = [picked] + [v for v in variants if v is not picked]
                for cand in order:
                    if _approach_ok(cand):
                        path = cand
                        break
                if path is None:
                    # 缓存变体首段全部不可走：可能是历史版本记账漏洞留下的
                    # 过期缓存（mark_passage 改低列顶未 bump world_version，
                    # BUG-103）→ 强制按当前地形重算一次，用新路线再验；
                    # 新路线仍不可用才试局部。
                    fresh = pathfinder.rebuild_key(sid, self.type_name) \
                        if hasattr(pathfinder, "rebuild_key") else None
                    for cand in (fresh or []):
                        if _approach_ok(cand):
                            path = cand
                            break
        # D3 局部重规划兜底：spawner 侧无解，或全部候选首段不可走（典型：
        # 敌人已挖到平台边缘/半山腰，任何"从刷怪点出发"的路线都无法就近
        # 接入）。从敌人当前格直接 A* 到大本营——实测证明该局面下局部
        # 常有路（seed3091 (-3,-1)、seed79 (0,3) 均为 5 格路），只是
        # 旧实现从不从"脚下"规划，直接清路径原地等待。
        if path is None and hasattr(pathfinder, "plan_from"):
            local = pathfinder.plan_from(
                (math.floor(self.position[0]),
                 math.floor(self.position[2])),
                self.type_name)
            if local is not None:
                # 局部路径从敌人当前格起步：首段可达性由 A* 的边约束
                # 保证（与运行时进格校验同源），不能再过 _approach_ok 的
                # 直线复接检查——敌人恰好站在格边界时（位置 x 为整数），
                # 到最近段端点的直线会擦过相邻危险格（如落差 6 的悬崖边），
                # 把合法局部路径误判不可走（实测 seed148 坑底 tank 因此
                # 永远等待）。
                path = local
                local_from_here = True
        if path is None:
            # 局部也无解（例如敌人/玩家挖掘把大本营禁挖区四周挖成深坑，
            # 基地变成不可达孤岛）→ 丢弃当前路径原地等待，而不是保留旧路
            # 反复走到"需要挖进禁挖区"的位置被运行时 dig_forbidden_at
            # 断路、再请求重寻路——无限抖动/空挖（BUG-101）。
            # 此后看门狗（_update_wave）会限时重试；任何地形编辑
            # （world_version bump）都会触发重估，一旦基地重新可达
            # （含玩家挖通新路线）自动恢复。
            self.path = None
            self.all_paths = []
            self.path_points = ()
            # BUG-105：标记"原地等待"，到达判定据此跳过，不再误判为抵达大本营
            self.waiting_for_path = True
            self.path_version = universe.world_version
            return
        self.path = path
        # BUG-105：拿到真实路径即解除"原地等待"标记，恢复到达判定
        self.waiting_for_path = False
        # 停滞看门狗：拿到新路径即视为脱离阻塞（若随后仍无进展，
        # 看门狗会按配额再次介入直至 blocked）。
        self.blocked = False
        # BUG-106：fresh 兜底分支选出的 path 来自 rebuild_key 重算，通常不
        # 等于（PathData 按值比较）缓存 variants 中任何一项，直接
        # variants.index(path) 会抛 ValueError 崩掉敌人更新循环。先确保
        # 选中的 path 一定在 variants 里（不在则补进副本），再取下标。
        if path not in variants:
            variants = list(variants) + [path]
        self.all_paths = variants
        self.path_variant = variants.index(path)
        # 局部路径（从脚下起步）直接把敌人真实位置作为首点、从第 1 个路点
        # 开始走——不做就近投影。否则敌人站在格边界时，投影可能选中
        # "绕行后回到敌人附近"的后段，朝错误方向穿越崖边被断路，再规划
        # 又选回同一后段 → 永久空转（实测 seed3091 (36,36) fast 在
        # (26,26) 格边界循环，看门狗介入 3 次仍不动）。
        if local_from_here:
            self.path_points = ((self.position[0], self.position[1],
                                 self.position[2]),) + tuple(path.points)
            self.path_index = 1 if len(self.path_points) > 1 else 0
            self.path_version = universe.world_version
            return
        pts = path.points
        # 就近接入：把当前位置投影到各段，取最近段的终点为新目标
        # （平滑路径段可跨几十格，最近顶点会把敌人拉回路径起点）
        px, pz = self.position[0], self.position[2]
        best_seg = 0
        best_d2 = float("inf")
        for i in range(len(pts) - 1):
            ax, az = pts[i][0], pts[i][2]
            bx, bz = pts[i + 1][0], pts[i + 1][2]
            abx = bx - ax
            abz = bz - az
            L2 = abx * abx + abz * abz
            if L2 < 1e-9:
                t = 0.0
            else:
                t = ((px - ax) * abx + (pz - az) * abz) / L2
                t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
            qx = ax + abx * t
            qz = az + abz * t
            dx = px - qx
            dz = pz - qz
            d2 = dx * dx + dz * dz
            if d2 < best_d2:
                best_d2 = d2
                best_seg = i
        self.path_index = min(best_seg + 1, len(pts) - 1)
        self.path_points = pts
        self.path_version = universe.world_version

    # ─── 类型化环境速度 ────────────────────────────────────

    def _get_surface_mult(self, universe):
        """表面方块亲和（脚下方块类型 → 速度倍率）。"""
        pos = self.position
        x = math.floor(pos[0])
        z = math.floor(pos[2])
        # 覆盖层为空（未挖掘）时跳过逐格 tuple 构造与成员测试
        if universe.surface_override and (x, z) in universe.surface_override:
            blk = "dirt"  # 挖开的柱暴露亚表层
        else:
            blk = universe._surface_block_pure(x, z)
        return self._env_affinity.get(blk, 1.0)

    def _get_weather_mult(self, universe):
        """天气速度倍率（雨/雪随强度连续插值，取乘积）。"""
        ws = self._env_weather
        if not ws:
            return 1.0
        w = universe.weather
        wm = 1.0
        rain = float(ws.get("rain", 1.0))
        snow = float(ws.get("snow", 1.0))
        intensity = min(1.0, getattr(w, "intensity", 0.0))
        if w.is_raining and rain != 1.0:
            wm *= 1.0 + (rain - 1.0) * intensity
        if w.is_snowing and snow != 1.0:
            wm *= 1.0 + (snow - 1.0) * intensity
        return wm

    def _noise_direction(self, towers, base):
        """计算噪音方向（指向噪音最大的源）。

        噪音源 = 塔 + 大本营。1/dist² 衰减加权合成方向。
        返回 (dx, dz) 单位不归一化（方向 + 强度）。
        """
        total_dx, total_dz = 0.0, 0.0
        sources = []
        if base and base.active:
            sources.append((base.position[0], base.position[2]))
        for tower in towers:
            if not tower.active:
                continue  # 已被摧毁的塔当帧不再感知/产生噪音
            sources.append((tower.position[0], tower.position[2]))
        if not sources:
            return (0.0, 0.0)
        for sx, sz in sources:
            dx = sx - self.position[0]
            dz = sz - self.position[2]
            dist_sq = dx * dx + dz * dz
            if dist_sq < 1.0:
                return (dx, dz)  # 极近，直接朝它
            w = 1.0 / dist_sq  # 1/dist² 衰减
            total_dx += dx * w
            total_dz += dz * w
        return (total_dx, total_dz)

    def _water_safe_heading(self, universe, heading):
        """给定目标 heading，若前方落水则偏转找沿岸方向（不朝海里走）。

        沿 heading 探测前方 PROBE_DIST 格，若任一格是海，
        依次尝试 ±30°/60°/90°/120°/150° 偏转，取第一个前方全陆地的方向。
        全失败则返回原 heading（走到海边再被避水逻辑拦住）。
        """
        cfg = self.scout_cfg or gamedata.STANDARD_SCOUT
        probe = cfg.get("probe_dist", 4)
        cx, cz = math.floor(self.position[0]), math.floor(self.position[2])

        def direction_clear(h):
            dx = math.cos(h)
            dz = math.sin(h)
            for s in range(1, probe + 1):
                ix = cx + int(round(dx * s))
                iz = cz + int(round(dz * s))
                if not universe._is_land_pure(ix, iz):
                    return False
            return True

        if direction_clear(heading):
            return heading
        for deg in (30, 60, 90, 120, 150, -30, -60, -90, -120, -150):
            rad = math.radians(deg)
            h2 = heading + rad
            if direction_clear(h2):
                return h2
        return heading

    def _update_scout_wander(self, dt, universe, towers, base):
        """侦察怪游荡：噪音导向移动，感知塔/大本营后带消息回巢。

        噪音源 = 塔 + 大本营。距离越近噪音越大（1/dist² 衰减）。
        换向时 heading 偏向噪音方向 + 随机扰动，非纯随机。
        """
        cfg = self.scout_cfg or gamedata.STANDARD_SCOUT
        turn_interval = cfg.get("turn_interval", 1.2)
        noise_jitter = cfg.get("noise_jitter", 0.5)
        wander_turn = cfg.get("wander_turn", 1.0)
        # 换向计时
        self.wander_turn_timer -= dt
        if self.wander_turn_timer <= 0:
            noise = self._noise_direction(towers, base)
            if noise and (noise[0] or noise[1]):
                # 噪音导向：heading 偏向噪音源 + 扰动，再避水偏转
                noise_heading = math.atan2(noise[1], noise[0])
                raw_heading = noise_heading + self.rng.uniform(
                    -noise_jitter, noise_jitter)
                self.wander_heading = self._water_safe_heading(
                    universe, raw_heading)
            else:
                # 无噪音源：纯随机 + 避水
                raw_heading = self.wander_heading + self.rng.uniform(
                    -wander_turn, wander_turn)
                self.wander_heading = self._water_safe_heading(
                    universe, raw_heading)
            self.wander_turn_timer = turn_interval

        # 沿 heading 移动（R1-B3：与 wave 移动一致，补上 surface/weather 倍率）
        terrain_speed = self._get_terrain_speed(universe)
        speed = (self.speed * self.slow_factor * terrain_speed
                 * self._get_surface_mult(universe)
                 * self._get_weather_mult(universe)) * dt
        nx = self.position[0] + math.cos(self.wander_heading) * speed
        nz = self.position[2] + math.sin(self.wander_heading) * speed

        # 避免走入海洋：目标格不是陆地则跳过本步（纯噪声，卸载区也正确）；
        # 台阶限制：前方抬升超过爬升阈值则不走（等下次换向绕行），不穿岩爬坡
        cell_x, cell_z = math.floor(nx), math.floor(nz)
        if universe._is_land_pure(cell_x, cell_z):
            climb_pass = max(1, int(math.floor(
                self.personality.get("climb_limit", 1.5))))
            cur_h = universe.surface_height_at(
                math.floor(self.position[0]), math.floor(self.position[2]))
            ent_h = universe.surface_height_at(cell_x, cell_z)
            # 台阶限制：前方抬升超 climb_pass 不走；下落落差超 fall_limit
            # 同样不走（视为墙，等下次换向绕行，BUG-102）。不穿岩爬坡/强坠。
            if (ent_h - cur_h <= climb_pass
                    and cur_h - ent_h <= self._fall_limit()):
                self.position[0] = nx
                self.position[2] = nz

        self._sync_y(dt, universe)

        # 感知检查：塔 + 大本营
        perception = cfg.get("perception_radius", 20.0)
        perception_sq = perception * perception
        found = False
        if base:
            bd = ((self.position[0] - base.position[0]) ** 2 +
                  (self.position[2] - base.position[2]) ** 2)
            if bd < perception_sq:
                found = True
        if not found and towers:
            for tower in towers:
                if not tower.active:
                    continue  # 已被摧毁的塔当帧不再感知
                td = ((self.position[0] - tower.position[0]) ** 2 +
                      (self.position[2] - tower.position[2]) ** 2)
                if td < perception_sq:
                    found = True
                    break

        if found:
            self.has_message = True
            self._start_return(universe)
            return None

        # 超时检查
        self.wander_lifetime -= dt
        if self.wander_lifetime <= 0:
            self._start_return(universe)
            return None

        return None

    def _start_return(self, universe):
        """开始回巢：生成从当前位置到 home 刷怪点的路径。"""
        spawner = universe.spawners[self.home_spawner_id]
        dx = self.position[0] - (spawner.x + 0.5)
        dz = self.position[2] - (spawner.z + 0.5)
        if dx * dx + dz * dz < 2.0:
            # 已在 home 附近，直接消失
            self.active = False
            self.scout_returned_with_message = self.has_message
            return
        path_points = universe._generate_path(
            math.floor(self.position[0]), math.floor(self.position[2]),
            spawner.x, spawner.z,
            self.home_spawner_id * 100 + 7,
            universe.seed + 777 + self.home_spawner_id * 333)
        self.return_path = tuple(path_points) if path_points else ()
        self.return_path_index = 0
        self.behavior = "scout_return"

    def _update_scout_return(self, dt, universe):
        """侦察怪回巢：沿路径回到刷怪点，到达后消失。"""
        if self.return_path_index >= len(self.return_path):
            self.active = False
            self.scout_returned_with_message = self.has_message
            return None

        target = self.return_path[self.return_path_index]
        dx = target[0] - self.position[0]
        dz = target[2] - self.position[2]
        dist_sq = dx * dx + dz * dz

        if dist_sq < 0.25:
            self.return_path_index += 1
            if self.return_path_index >= len(self.return_path):
                self.active = False
                self.scout_returned_with_message = self.has_message
                return None
        else:
            dist_xz = dist_sq ** 0.5
            # 回程速度与游荡一致：叠加地形速度倍率（BUG-029）；
            # R1-B3：与 wave 移动一致，补上 surface/weather 倍率。
            speed = (self.speed * self.slow_factor
                     * self._get_terrain_speed(universe)
                     * self._get_surface_mult(universe)
                     * self._get_weather_mult(universe)) * dt
            # 限幅：一步不超过目标路点，避免过冲后往复振荡永不推进（BUG-030）
            inv_dist = min(speed / dist_xz, 1.0)
            nx = self.position[0] + dx * inv_dist
            nz = self.position[2] + dz * inv_dist
            cell_x, cell_z = math.floor(nx), math.floor(nz)
            # 避免走入海洋（同游荡）：目标格不是陆地则跳过本步（BUG-031）
            if universe._is_land_pure(cell_x, cell_z):
                # 台阶限制（同游荡）：前方抬升超过爬升阈值则不穿岩爬坡；
                # 下落落差超过 fall_limit 同样不跨（视为墙，BUG-102）
                climb_pass = max(1, int(math.floor(
                    self.personality.get("climb_limit", 1.5))))
                cur_h = universe.surface_height_at(
                    math.floor(self.position[0]), math.floor(self.position[2]))
                ent_h = universe.surface_height_at(cell_x, cell_z)
                if (ent_h - cur_h <= climb_pass
                        and cur_h - ent_h <= self._fall_limit()):
                    self.position[0] = nx
                    self.position[2] = nz
                else:
                    # 回程遇不可爬/不可下台阶：跳到下一路点（侦察怪无挖掘能力，
                    # 跳步避免永久卡死；回归行为无敌方目标，影响可忽略）
                    self.return_path_index += 1
            else:
                # BUG-104：插值落点非纯陆地（回程直线跨过海洋/海岸格，或
                # 地形在生成回程路后变化）→ 原先无 else 分支：既不移动也不
                # 推进 return_path_index，本分支仅在 dist_sq>=0.25 时到达，
                # 且回程无生命周期/转向超时，侦察怪会永久冻结在原地，永不
                # 回巢、永不置 scout_returned_with_message。跳到下一路点，
                # 与上方不可爬台阶的恢复一致，让回程得以继续或终止。
                self.return_path_index += 1

        self._sync_y(dt, universe)
        return None

    def _get_terrain_speed(self, universe):
        """根据当前地形获取速度乘数（类型覆盖 → 全局默认 → 1.0）"""
        pos = self.position
        x, z = math.floor(pos[0]), math.floor(pos[2])
        biome = universe.get_biome_safe(x, z)
        return self._env_terrain.get(biome,
                                     self.TERRAIN_SPEED_MULTS.get(biome, 1.0))

    def take_damage(self, amount):
        """受到伤害"""
        self.health -= amount
        if self.health <= 0:
            self.active = False
            return True
        return False

    def apply_slow(self, factor, duration):
        """应用减速效果"""
        self.slow_factor = min(self.slow_factor, factor)
        self.slow_timer = max(self.slow_timer, duration)


class Projectile:
    """抛射体"""

    def __init__(self, position, velocity, damage, source=None,
                 splash_radius=0.0, splash_falloff=0.5,
                 slow_factor=1.0, slow_duration=0.0,
                 bonus_vs=None, bonus_mult=1.0):
        self.position = list(position)  # [x, y, z]
        self.velocity = list(velocity)  # [vx, vy, vz]
        self.damage = damage
        self.source = source
        self.active = True
        self.lifetime = config.PROJECTILE_LIFETIME
        # 定位效果（由 Tower._fire 从 TowerType 注入）
        self.splash_radius = splash_radius
        self.splash_falloff = splash_falloff
        self.slow_factor = slow_factor
        self.slow_duration = slow_duration
        self.bonus_vs = bonus_vs
        self.bonus_mult = bonus_mult

    def _terrain_swept(self, prev_x, prev_y, prev_z, universe):
        """沿上一帧位置→当前位置的线段做 swept 碰撞检测。

        按 PROJECTILE_COLLISION_STEP 步长线性插值采样，命中任一实体方块
        即停止（active=False，返回 True）。终点必被采样（t=1），因此是原
        "只查终点格"逻辑的严格超集：平地薄墙不再能因单帧大步长被跳过。

        返回 True 表示已命中并停止（调用方直接 return None）。
        """
        step = config.PROJECTILE_COLLISION_STEP
        pos = self.position
        dx = pos[0] - prev_x
        dy = pos[1] - prev_y
        dz = pos[2] - prev_z
        seg = (dx * dx + dy * dy + dz * dz) ** 0.5
        if seg <= 0.0:
            n = 1
        else:
            n = min(128, max(1, int(seg / step) + 1))
        gbs = universe.get_block_safe
        floor = math.floor
        solid = is_solid
        for i in range(n):
            t = i / (n - 1) if n > 1 else 1.0
            x = prev_x + dx * t
            y = prev_y + dy * t
            z = prev_z + dz * t
            if solid(gbs(floor(x), floor(y), floor(z))):
                self.active = False
                return True
        return False

    def update(self, dt, universe, enemies, enemy_grid=None):
        """更新抛射体物理（倍速不变性：内部子步进）。

        游戏倍速（game.Game.time_scale，GAME_SPEEDS 最高 8x）把 sim_dt 整体
        缩放——叠加 main.py 的 1/30 帧时间下限，单步 dt 最大可达 0.267 秒；
        重力/空气阻力按欧拉法单步积分的离散误差随 dt 增大，高倍速下弹道
        整体下压变形：1x 时能命中的近距离弹在 8x 下也插进地里（子弹"打地"
        回归），且单步大位移会直接穿过敌身——命中判定此前只查帧末位置，
        高倍速下漏检。此处把 dt 拆成不超过 PROJECTILE_PHYSICS_MAX_STEP
        （1/60 秒）的子步逐段积分：弹道/地形碰撞/命中判定与 1x 完全一致，
        每子步位移 ≤0.25 格（远小于 0.9 命中半径），穿身漏检一并消除。
        1x@240fps 时 dt=1/240 小于上限 → 单步直通，行为逐位不变。

        返回值契约不变：命中敌人返回 {"hit": True, ...}；命中地形/寿命耗尽
        /未命中返回 None（active 相应置 False 或保持 True）。
        """
        if not self.active:
            return None
        if dt <= config.PROJECTILE_PHYSICS_MAX_STEP:
            return self._update_step(dt, universe, enemies, enemy_grid)
        n = max(1, int(math.ceil(dt / config.PROJECTILE_PHYSICS_MAX_STEP)))
        sub = dt / n
        for _ in range(n):
            result = self._update_step(sub, universe, enemies, enemy_grid)
            # 命中敌人在子步内即刻返回；地形命中/寿命耗尽由 active=False 表达
            if result is not None:
                return result
            if not self.active:
                return None
        return None

    def _update_step(self, dt, universe, enemies, enemy_grid=None):
        """单步物理：重力 → 空气阻力 → 位移 → 寿命 → 地形扫掠 → 敌人命中。

        dt 必须已由 update 拆分为不超过 PROJECTILE_PHYSICS_MAX_STEP 的子步
        （直接大 dt 调用即回到高倍速打地/穿身的老问题）。"""
        if not self.active:
            return None

        pos = self.position
        vel = self.velocity
        # 上一帧位置（用于 swept 碰撞：避免丢帧时一步跨过薄墙）
        prev_x = pos[0]
        prev_y = pos[1]
        prev_z = pos[2]

        # 应用重力
        vel[1] += config.PROJECTILE_GRAVITY * dt

        # 应用空气阻力（时间基：config.PROJECTILE_DRAG 为每秒保留率）
        d = config.PROJECTILE_DRAG ** dt
        vel[0] *= d
        vel[1] *= d
        vel[2] *= d

        # 更新位置
        pos[0] += vel[0] * dt
        pos[1] += vel[1] * dt
        pos[2] += vel[2] * dt

        # 生命周期
        self.lifetime -= dt
        if self.lifetime <= 0:
            self.active = False
            return None

        # 碰撞检测：地形（swept——沿上一帧→新位置的线段采样，命中即停）
        if self._terrain_swept(prev_x, prev_y, prev_z, universe):
            return None

        # 碰撞检测：敌人（保持 0.9 半径，但用平方距离避免开方）
        hit_radius = config.PROJECTILE_HIT_RADIUS
        hit_radius_sq = hit_radius * hit_radius
        px, py, pz = self.position
        enemy = None
        if enemy_grid is None:
            for candidate in enemies:
                if not candidate.active:
                    continue
                ex, ey, ez = candidate.position
                dx = px - ex
                if dx > hit_radius or -dx > hit_radius:
                    continue
                dy = py - ey
                if dy > hit_radius or -dy > hit_radius:
                    continue
                dz = pz - ez
                if dz > hit_radius or -dz > hit_radius:
                    continue

                if dx * dx + dy * dy + dz * dz < hit_radius_sq:
                    enemy = candidate
                    break
        else:
            best_idx = -1
            hr = hit_radius
            for idx in enemy_grid.query(px, pz, hr):
                candidate = enemies[idx]
                if not candidate.active:
                    continue
                ex, ey, ez = candidate.position
                # 轴向粗筛：|d| > hr ⇔ d > hr 或 -d > hr（浮点取负精确），
                # 与 abs(d) > hr 完全等价，省掉每候选 3 次内建调用
                dx = px - ex
                if dx > hr or -dx > hr:
                    continue
                dy = py - ey
                if dy > hr or -dy > hr:
                    continue
                dz = pz - ez
                if dz > hr or -dz > hr:
                    continue
                if dx * dx + dy * dy + dz * dz < hit_radius_sq:
                    if best_idx < 0 or idx < best_idx:
                        best_idx = idx
            if best_idx >= 0:
                enemy = enemies[best_idx]

        if enemy is not None:
            # 增伤判定（如 metal_tower 打坦克 2x）
            dmg = self.damage
            if self.bonus_vs and enemy.type_name == self.bonus_vs:
                dmg *= self.bonus_mult
            killed = enemy.take_damage(dmg)

            # 减速（sand_trap）
            if self.slow_duration > 0 and self.slow_factor < 1.0:
                enemy.apply_slow(self.slow_factor, self.slow_duration)

            # 溅射（water_cannon）：按距离线性衰减
            extra_kills = []
            if self.splash_radius > 0:
                sr = self.splash_radius
                sr_sq = sr * sr
                if enemy_grid is None:
                    splash_candidates = range(len(enemies))
                else:
                    splash_candidates = sorted(
                        enemy_grid.query(px, pz, sr))
                for oi in splash_candidates:
                    other = enemies[oi]
                    if other is enemy or not other.active:
                        continue
                    odx = px - other.position[0]
                    ody = py - other.position[1]
                    odz = pz - other.position[2]
                    od_sq = odx * odx + ody * ody + odz * odz
                    if od_sq > sr_sq:
                        continue
                    dist = od_sq ** 0.5
                    falloff = 1.0 - dist / sr if dist > 0 else 1.0
                    odmg = dmg * self.splash_falloff * falloff
                    if odmg <= 0:
                        continue
                    if other.take_damage(odmg):
                        extra_kills.append(other)

            self.active = False
            result = {"hit": True, "killed": killed, "enemy": enemy}
            if extra_kills:
                result["extra_kills"] = extra_kills
            return result

        return None


class Pickup:
    """地面掉落物 — 可被点击拾取"""

    def __init__(self, position, item_type, item_name, amount=1, rng=None):
        """
        position: (x, y, z)
        item_type: "material" 或 "weapon"
        item_name: 材料名或武器名
        amount: 数量
        rng: 可选的随机数生成器（种子可复现）
        """
        self.position = list(position)  # [x, y, z]
        self.item_type = item_type
        self.item_name = item_name
        self.amount = amount
        self.active = True
        rand = rng or random
        self.bob_phase = rand.uniform(0, math.tau)
        self.bob_speed = 2.0
        self.time = rand.uniform(0, 10)

    def update(self, dt):
        self.time += dt
        self.bob_phase += dt * self.bob_speed
        return self.active


class Base:
    """大本营 — 敌人攻击的目标"""

    def __init__(self, position, max_health=100):
        self.position = list(position)  # [x, y, z]
        self.max_health = max_health
        self.health = max_health
        self.active = True
        self.damage_flash = 0.0  # 受伤闪烁计时

    def take_damage(self, amount):
        """受到伤害"""
        self.health = max(0, self.health - amount)
        self.damage_flash = 0.5
        if self.health <= 0:
            self.active = False
            return True  # 已摧毁
        return False

    def update(self, dt):
        if self.damage_flash > 0:
            self.damage_flash -= dt
        return self.active
