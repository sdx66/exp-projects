"""
entities.py - 游戏实体
Tower, Enemy, Projectile 类定义 + 类型定义
塔/敌人类型数据来自 data/towers.json / data/enemies.json（经 gamedata 构建）。
"""

import config
import gamedata
import math
import random
from items import CLASSIC_TOWER_MATERIALS
from blocks import is_solid, BLOCK_ID


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
    """
    def __init__(self, type_name, base_damage, base_range, base_cooldown,
                 base_cost, requires_tech=None,
                 slow_factor=1.0, slow_duration=0.0,
                 splash_radius=0.0, splash_falloff=0.5,
                 bonus_vs=None, bonus_mult=1.0,
                 no_attack=False, weapon_slots=0):
        self.type_name = type_name
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

    @property
    def color(self):
        return gamedata.TOWER_COLORS.get(self.type_name, (128, 128, 128))


# ─── 塔类型（来自 data/towers.json） ──────────────────────────
# 设计意图: stone=基础输出, sand=控场(减速), water=清群(溅射), metal=反坦克(增伤)
TOWER_TYPES = {}
for _name, _d in gamedata.TOWERS_RAW.items():
    TOWER_TYPES[_name] = TowerType(
        _name,
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

    def update(self, dt, enemies, enemy_grid=None):
        """更新塔逻辑，返回新创建的抛射体列表（可为空列表）。

        防御塔(no_attack)自身不攻击；其攻击完全来自挂载武器，
        每把武器独立冷却、独立索敌，同帧可各发一弹（攻击效果叠加）。
        """
        if not self.active:
            return []
        fired = []

        if self.no_attack:
            # 无攻击防御塔：不索敌不开火，仅依赖挂载武器
            if not self.weapon_mounts:
                return []
            for mount in self.weapon_mounts:
                mount["timer"] -= dt
                if mount["timer"] > 0:
                    continue
                wdef = mount["wdef"]
                target = self._find_target(enemies, enemy_grid, wdef.range)
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
            self.target = self._find_target(enemies, enemy_grid, self.range)
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

    def _find_target(self, enemies, enemy_grid=None, rng=None):
        """寻找射程内最近的敌人（有效射程含高度差加成）"""
        if not rng or rng <= 0:
            return None
        best = None
        best_idx = -1
        pos = self.position
        px = pos[0]
        py = pos[1]
        pz = pos[2]
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
        self.path = path                # 实际使用的路径
        self.all_paths = all_paths or [path]  # 所有备选路径
        if self.path is None:
            self.path_points = ()
        elif hasattr(self.path, "points"):
            self.path_points = self.path.points
        else:
            self.path_points = tuple(self.path)
        self.path_index = path_index
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
        # env 派生查找表（env 仅在此处赋值一次，之后只读）
        self._env_terrain = self.env.get("terrain_speed") or {}
        self._env_affinity = self.env.get("surface_affinity") or {}
        self._env_weather = self.env.get("weather_speed") or {}

        # 个人化路径参数（每个敌人略有不同）
        rng = rng or random
        self.wobble_amp = rng.uniform(0.5, 2.0) * self.personality["wobble"]
        self.wobble_freq = rng.uniform(0.3, 1.0)
        self.wobble_phase = rng.uniform(0, math.tau)
        self.time = rng.uniform(0, 10)  # 错开初始相位
        self.path_noise = rng.uniform(-0.3, 0.3)  # 整体法线偏移（限幅后仍 ≤0.45 路径半宽）

        # 寻路状态：变体号 / 分配路径时的 world_version / 过期复查计时
        self.path_variant = 0
        self.path_version = -1
        self.repath_timer = rng.uniform(0, config.PATH_REPATH_CHECK_INTERVAL)
        # 攻击塔 / 挖掘子状态
        self.attack_target = None
        self.attack_timer = 0.0
        self.dig_target = None      # (x, z) 目标柱
        self.dig_progress = 0.0
        self._dug_count = 0         # 本次挖掘会话实际削格数（会话结束统一 bump 版本）
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
            climb_pass, dig_speed = c[1], c[2]
        else:
            climb_pass = max(1, int(math.floor(
                pers.get("climb_limit", 1.5))))
            dig_speed = float(pers.get("dig_speed", 0.0))
            self._pers_cache = (pers, climb_pass, dig_speed)
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
                    # 可挖的高墙（远高于本列）不算立足点，避免顶到墙顶
                    if dig_speed > 0 and h > own + climb_pass:
                        continue
                    if h > top:
                        top = h
        if top < own:
            top = own
        ground_y = top + 1
        if self.position[1] < ground_y:
            self.position[1] = ground_y
        elif self.position[1] > ground_y:
            self.position[1] = max(ground_y, self.position[1] - 12.0 * dt)

    def _update_wave(self, dt, universe, towers_by_cell=None, pathfinder=None):
        """波次怪：沿平滑折线走向大本营；途中遇塔攻击、遇高墙挖掘。"""
        if self.path_index < len(self.path_points):
            target = self.path_points[self.path_index]

            # ── 周期性重估：world_version 变化 → 换用新变体路径 ──
            self.repath_timer -= dt
            if self.repath_timer <= 0:
                self.repath_timer = config.PATH_REPATH_CHECK_INTERVAL
                if (pathfinder is not None
                        and self.path_version != universe.world_version):
                    self._request_repath(universe, pathfinder)
                    if self.path_points:
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
                if tower is not None and tower.active:
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
                        if tower is not None and tower.active:
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
                    if h_ent - h_cur > climb_pass:
                        if dig_speed > 0:
                            self.behavior = "dig"
                            self.dig_target = (ent_cx, ent_cz)
                            self.dig_progress = 0.0
                            self._dug_count = 0
                            self.dig_y = None
                            self.dig_base_y = None
                            self.dig_passed = False
                            # 立即钉住身体高度到起始地表（修复被 _sync_y 顶到墙顶）
                            self.position[1] = h_cur + 1.0
                            return None
                        # 不能挖：理论上 A* 已保证不发生（兜底照常通过）
                pos[0] = nx_w
                pos[2] = nz_w

        self._sync_y(dt, universe)

        # 到达大本营
        if self.path_index >= len(self.path_points):
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
        dist_sq = dx * dx + dz * dz
        reach = float(self.attack_cfg.get("range",
                                          config.PATH_SIEGE_DEFAULT_RANGE))
        if dist_sq > (reach + 1.0) ** 2:
            # 目标超出追击范围：回 wave 重新判定
            self.attack_target = None
            self.behavior = "wave"
            return None
        if dist_sq > (reach * 0.6) ** 2:
            # 贴身移动
            d = dist_sq ** 0.5
            step = self.speed * self.slow_factor * self._get_terrain_speed(universe) * dt
            adv = min(step, d)
            self.position[0] += dx / d * adv
            self.position[2] += dz / d * adv
            self._sync_y(dt, universe)
        self.attack_timer -= dt
        if self.attack_timer <= 0:
            self.attack_timer = float(self.attack_cfg.get("cooldown", 1.2))
            destroyed = tower.take_damage(float(self.attack_cfg.get("damage", 4)))
            if destroyed:
                # 塔倒：游戏侧清理塔格并 bump world_version，这里只切回行走
                self.attack_target = None
                self.behavior = "wave"
                self.path_version = -1
        return None

    def _end_dig(self, universe):
        """结束挖掘会话：本次会话实际削过格则统一推进 world_version。"""
        if self._dug_count > 0:
            universe.bump_world_version()
            self._dug_count = 0
        self.dig_target = None
        self.dig_y = None
        self.dig_base_y = None
        self.dig_passed = False

    def _dig_block_present(self, universe, x, y, z):
        """(x, y, z) 是否为可挖实体方块（非 air/水/岩浆/基岩）。"""
        blk = universe.get_block(x, y, z)
        return blk not in (BLOCK_ID["air"], BLOCK_ID["water"],
                           BLOCK_ID["lava"], BLOCK_ID["bedrock"])

    def _finish_dig(self, universe, pathfinder):
        """结束掘进会话：削过格则统一 bump 版本，回 wave 并请求重估路线。"""
        dug = self._dug_count > 0
        self._end_dig(universe)
        self.behavior = "wave"
        self.path_version = -1
        if dug and pathfinder is not None:
            self._request_repath(universe, pathfinder)

    def _update_dig(self, dt, universe, pathfinder=None):
        """掘进式挖掘前方高墙：只挖"紧邻前方"（脚底平面 base+1）与
        "紧邻上方"（base+2）两块。顺序：先削正前方那块，削空即向前走；
        走进墙列后若头顶还压着方块（base+2），再挖"上方紧挨"的那块；
        两块都空后把目标列通行高度标记为 base（surface_override），上方
        悬空块保留（拱门视觉），会话结束统一 bump 版本。

        相比"掏平整列"旧行为：只削 2 块、悬空墙保留；挖掘全程钉住身体高度
        base+1（修复被 _sync_y 顶到墙顶"爬着挖"）。
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
        cx = math.floor(self.position[0])
        cz = math.floor(self.position[2])
        climb_pass = max(1, int(math.floor(
            self.personality.get("climb_limit", 1.5))))
        # 会话基准面：敌人起始所在格的地表（推进会换格，基准保持权威）
        if self.dig_base_y is None:
            self.dig_base_y = universe.surface_height_at(cx, cz)
        base = self.dig_base_y
        # 掘进全程钉住身体高度在通道内（不 _sync_y，避免被列顶顶起）
        self.position[1] = base + 1.0

        # 早退守卫：尚未打通前，目标列顶-基准 ≤ 可爬高差 → 已可通过，结束
        if not self.dig_passed and universe.surface_height_at(tx, tz) - base <= climb_pass:
            self._finish_dig(universe, pathfinder)
            return None

        in_cell = (math.floor(self.position[0]) == tx
                   and math.floor(self.position[2]) == tz)

        # 阶段 1：削正前方脚底块（base+1）
        if self._dig_block_present(universe, tx, base + 1, tz):
            self.dig_y = base + 1
            self.dig_progress += dig_speed * dt
            while self.dig_progress >= 1.0:
                self.dig_progress -= 1.0
                if self._dig_block_present(universe, tx, base + 1, tz):
                    if universe.dig_front_block(tx, base + 1, tz):
                        self._dug_count += 1
            return None

        # 阶段 2：正前方已空 → 向墙列推进（尚未进入则每帧走一步）
        if not in_cell:
            tcx = tx + 0.5
            tcz = tz + 0.5
            dx = tcx - self.position[0]
            dz = tcz - self.position[2]
            d = (dx * dx + dz * dz) ** 0.5
            if d > 1e-4:
                step = min(self.speed * self.slow_factor * dt, d)
                self.position[0] += dx / d * step
                self.position[2] += dz / d * step
            self.position[1] = base + 1.0
            # 跨入目标列后本帧继续处理上方方块
            if (math.floor(self.position[0]) != tx
                    or math.floor(self.position[2]) != tz):
                return None

        # 阶段 3：已在墙列内 → 若上方还压着方块（base+2），挖"上方紧挨"那块
        if (universe.surface_height_at(tx, tz) - base >= 2
                and self._dig_block_present(universe, tx, base + 2, tz)):
            self.dig_y = base + 2
            self.dig_progress += dig_speed * dt
            while self.dig_progress >= 1.0:
                self.dig_progress -= 1.0
                if self._dig_block_present(universe, tx, base + 2, tz):
                    if universe.dig_front_block(tx, base + 2, tz):
                        self._dug_count += 1
            return None

        # 阶段 4：正前方与上方都空 → 标记通行高度并结束会话
        if not self.dig_passed:
            universe.mark_passage(tx, tz, base)
            self.dig_passed = True
        self._finish_dig(universe, pathfinder)
        return None

    def _request_repath(self, universe, pathfinder):
        """向 pathfinder 取最新变体并就近换路。"""
        sid = self.home_spawner_id
        if sid is None or sid < 0 or sid not in universe.spawners:
            self.path_version = universe.world_version
            return
        variants = pathfinder.get_variants(sid, self.type_name)
        if not variants:
            return  # 寻路失败：保持旧路，下个周期再试
        path = Enemy.pick_path(self.type_def, variants,
                               pathfinder.path_occupancy, universe.rng)
        if path is None:
            return
        self.path = path
        self.all_paths = variants
        self.path_variant = variants.index(path)
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
                raw_heading = noise_heading + random.uniform(
                    -noise_jitter, noise_jitter)
                self.wander_heading = self._water_safe_heading(
                    universe, raw_heading)
            else:
                # 无噪音源：纯随机 + 避水
                raw_heading = self.wander_heading + random.uniform(
                    -wander_turn, wander_turn)
                self.wander_heading = self._water_safe_heading(
                    universe, raw_heading)
            self.wander_turn_timer = turn_interval

        # 沿 heading 移动
        terrain_speed = self._get_terrain_speed(universe)
        speed = self.speed * self.slow_factor * terrain_speed * dt
        nx = self.position[0] + math.cos(self.wander_heading) * speed
        nz = self.position[2] + math.sin(self.wander_heading) * speed

        # 避免走入海洋：目标格不是陆地则跳过本步（纯噪声，卸载区也正确）
        cell_x, cell_z = math.floor(nx), math.floor(nz)
        if universe._is_land_pure(cell_x, cell_z):
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
            speed = self.speed * self.slow_factor * dt
            inv_dist = speed / dist_xz
            self.position[0] += dx * inv_dist
            self.position[2] += dz * inv_dist

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
        """更新抛射体物理"""
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

        # 应用空气阻力（时间基，60fps 为基准）
        d = config.PROJECTILE_DRAG ** (dt * 60.0)
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
