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
from blocks import is_solid


# ─── 塔类型定义 ────────────────────────────────────────────

class TowerType:
    """塔类型定义

    定位字段（让每种塔不可替代）:
      slow_factor / slow_duration  — 命中后减速(sand_trap)
      splash_radius / splash_falloff — 命中后范围溅射衰减伤害(water_cannon)
      bonus_vs / bonus_mult        — 对特定敌人类型增伤(metal_tower 打坦克)
    """
    def __init__(self, type_name, base_damage, base_range, base_cooldown,
                 base_cost, requires_tech=None,
                 slow_factor=1.0, slow_duration=0.0,
                 splash_radius=0.0, splash_falloff=0.5,
                 bonus_vs=None, bonus_mult=1.0):
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


class Tower:
    """防御塔"""

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
        # 来源：("classic", type_name) 或 ("weapon", weapon_name)
        self.source = ("classic", tower_type.type_name)
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

    def update(self, dt, enemies):
        """更新塔逻辑，返回新创建的抛射体或 None"""
        if not self.active:
            return None

        self.cooldown_timer -= dt
        if self.cooldown_timer <= 0:
            # 寻找目标
            self.target = self._find_target(enemies)
            if self.target:
                proj = self._fire()
                self.cooldown_timer = self.cooldown
                return proj
        return None

    def _find_target(self, enemies):
        """寻找射程内最近的敌人"""
        best = None
        range_sq = self.range * self.range
        best_dist_sq = range_sq
        for enemy in enemies:
            if not enemy.active:
                continue
            dx = enemy.position[0] - self.position[0]
            dz = enemy.position[2] - self.position[2]
            dist_sq = dx * dx + dz * dz
            if dist_sq <= best_dist_sq:
                best_dist_sq = dist_sq
                best = enemy
        return best

    def _fire(self):
        """发射抛射体"""
        if not self.target:
            return
        dx = self.target.position[0] - self.position[0]
        dy = self.target.position[1] - (self.position[1] + 1.0)  # 瞄准敌人中心
        dz = self.target.position[2] - self.position[2]
        dist = (dx * dx + dy * dy + dz * dz) ** 0.5
        if dist > 0:
            dx /= dist
            dy /= dist
            dz /= dist
        # 创建抛射体 — 把塔的定位属性传下去
        proj = Projectile(
            position=(self.position[0], self.position[1] + 1.0, self.position[2]),
            velocity=(dx * config.PROJECTILE_SPEED,
                      dy * config.PROJECTILE_SPEED + config.PROJECTILE_UPWARD_BIAS,  # 额外向上补偿重力
                      dz * config.PROJECTILE_SPEED),
            damage=self.damage,
            source=self,
            splash_radius=self.type_def.splash_radius,
            splash_falloff=self.type_def.splash_falloff,
            slow_factor=self.type_def.slow_factor,
            slow_duration=self.type_def.slow_duration,
            bonus_vs=self.type_def.bonus_vs,
            bonus_mult=self.type_def.bonus_mult,
        )
        return proj

    def upgrade(self):
        """升级塔"""
        if self.level < config.MAX_TOWER_LEVEL:
            self.level += 1
            self.damage = self.type_def.base_damage * (config.TOWER_DAMAGE_MULT ** (self.level - 1)) * self.damage_mult
            self.range = self.type_def.base_range * (config.TOWER_RANGE_MULT ** (self.level - 1))
            self.cooldown = self.type_def.base_cooldown * (config.TOWER_COOLDOWN_MULT ** (self.level - 1))

    @property
    def display_color(self):
        """获取塔的显示颜色（经典塔用 TOWER_COLORS，武器塔用 WeaponDef.color）

        始终返回 4 元组 (r, g, b, a)。raylib/cffi 的 Color 结构体有 4 个字段，
        若传入 3 元组，缺失的 alpha 字段会被 cffi 填成 0（完全透明），
        塔虽然画出来了却完全看不见。故此处统一补满 alpha=255。
        """
        if self.source and self.source[0] == "weapon":
            from items import WEAPON_TYPES
            wdef = WEAPON_TYPES.get(self.source[1])
            if wdef:
                c = wdef.color
                return c if len(c) == 4 else (*c, 255)
        c = gamedata.TOWER_COLORS.get(self.type_name, (128, 128, 128))
        return c if len(c) == 4 else (*c, 255)

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
    )


class Enemy:
    """敌人 — 支持多路径选择、地形感知、个性差异"""

    # 敌人性格参数：direct(偏好直线), terrain_aware(避开难走地形), aggressive(偏好开阔)
    # 来自 data/enemies.json 每类型的 personality 字段
    PERSONALITIES = gamedata.ENEMY_PERSONALITIES

    TERRAIN_SPEED_MULTS = gamedata.TERRAIN_SPEED_MULTS

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

        # 个人化路径参数（每个敌人略有不同）
        rng = rng or random
        self.wobble_amp = rng.uniform(0.5, 2.0) * self.personality["wobble"]
        self.wobble_freq = rng.uniform(0.3, 1.0)
        self.wobble_phase = rng.uniform(0, math.tau)
        self.time = rng.uniform(0, 10)  # 错开初始相位
        self.path_noise = rng.uniform(-0.3, 0.3)  # 整体法线偏移（限幅后仍 ≤0.45 路径半宽）

        # 行为状态机：wave / scout_wander / scout_return
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
    def pick_path(enemy_type, all_paths, existing_enemies=None, rng=None):
        """
        根据敌人性格、地形偏好和拥挤程度选择路径
        """
        if not all_paths or len(all_paths) == 1:
            return all_paths[0] if all_paths else None

        personality = Enemy.PERSONALITIES.get(enemy_type.type_name, Enemy.PERSONALITIES["regular"])

        # 计算每条路径的得分（路径长度/坡度由 world.PathData 预计算）
        best_idx = 0
        best_score = -float("inf")
        for i, path in enumerate(all_paths):
            score = 0.0

            # 1. 路径长度（直线型敌人偏好短路径）
            score += personality["direct"] * (-path.length / 50.0)

            # 2. 地形平坦度（地形感知型敌人偏好平坦路径）
            score += personality["terrain_aware"] * (-path.avg_slope / 2.0)

            # 3. 拥挤程度（避开已有敌人多的路径）
            if existing_enemies:
                enemy_count = sum(1 for e in existing_enemies if e.path is path)
                score += -enemy_count * 0.3

            # 4. 随机因素
            rng = rng or random
            score += rng.uniform(-0.5, 0.5)

            if score > best_score:
                best_score = score
                best_idx = i

        return all_paths[best_idx]

    def update(self, dt, universe, towers=None, base=None):
        """更新敌人移动 — 按行为分派：wave / scout_wander / scout_return"""
        if not self.active:
            return

        self.time += dt

        # 更新减速效果
        if self.slow_timer > 0:
            self.slow_timer -= dt
            if self.slow_timer <= 0:
                self.slow_factor = 1.0

        if self.behavior == "scout_wander":
            return self._update_scout_wander(dt, universe, towers, base)
        elif self.behavior == "scout_return":
            return self._update_scout_return(dt, universe)
        else:
            return self._update_wave(dt, universe)

    def _sync_y(self, dt, universe):
        """同步 Y 到地形高度（所有行为共用）。

        足迹半径用敌人实际半宽 0.2（盒宽 0.4）：只把敌人盒子真正压住的列算立足点。
        纯噪声高度，不依赖 chunk 加载状态，卸载与否结果一致。
        """
        x, y, z = self.position
        ix0 = int(math.floor(x - 0.2))
        ix1 = int(math.floor(x + 0.2))
        iz0 = int(math.floor(z - 0.2))
        iz1 = int(math.floor(z + 0.2))
        top = 0
        for ix in range(ix0, ix1 + 1):
            for iz in range(iz0, iz1 + 1):
                h = universe._surface_height_pure(ix, iz)
                if h > top:
                    top = h
        ground_y = top + 1
        if self.position[1] < ground_y:
            self.position[1] = ground_y
        elif self.position[1] > ground_y:
            self.position[1] = max(ground_y, self.position[1] - 12.0 * dt)

    def _update_wave(self, dt, universe):
        """波次怪：沿路径走向大本营（原路径跟随逻辑）。"""
        if self.path_index < len(self.path_points):
            target = self.path_points[self.path_index]

            # 个人化偏移：正弦摆动 + 固定偏移，沿路径段法线方向施加
            # （原实现只偏移全局 Z，对南北向路径不产生横向偏移；改为沿法线后
            #   对任意朝向路径都有效，且限幅在路径半宽内，敌人不再走出路面）
            # 最后几格内线性收敛到 0，避免横向噪声把敌人甩到大本营外面凭空消失
            wobble = math.sin(self.time * self.wobble_freq + self.wobble_phase) * self.wobble_amp
            total = len(self.path_points)
            tail = min(8, total)
            remain = max(0, total - self.path_index - 1)
            converge = 1.0 if tail == 0 else min(1.0, remain / tail)

            # 路径段方向 → 垂直单位向量
            prev = None
            seg = None
            if 0 < self.path_index < len(self.path_points):
                prev = self.path_points[self.path_index - 1]
                seg = self.path_points[self.path_index]
            elif self.path_index + 1 < len(self.path_points):
                prev = self.path_points[self.path_index]
                seg = self.path_points[self.path_index + 1]
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
            # 限幅到 0.3：敌人盒 0.4 宽(半宽0.2)，0.3 偏移使敌人完全落在 1x1 路径格
            # 内(格半宽0.5)，不伸入相邻格——相邻格是玩家可放塔的位置，故不会穿塔
            offset = max(-0.3, min(0.3, offset))
            tx = target[0] + nx * offset
            tz = target[2] + nz * offset

            terrain_speed = self._get_terrain_speed(universe)

            dx = tx - self.position[0]
            dz = tz - self.position[2]
            dist_sq = dx * dx + dz * dz
            if dist_sq < 0.25:
                self.path_index += 1
            else:
                dist_xz = dist_sq ** 0.5
                speed = self.speed * self.slow_factor * terrain_speed * dt
                inv_dist = speed / dist_xz
                self.position[0] += dx * inv_dist
                self.position[2] += dz * inv_dist

        self._sync_y(dt, universe)

        # 到达大本营
        if self.path_index >= len(self.path_points):
            self.active = False
            return "reached_base"

        return None

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
        """根据当前地形获取速度乘数"""
        x, z = math.floor(self.position[0]), math.floor(self.position[2])
        biome = universe.get_biome_safe(x, z)
        return self.TERRAIN_SPEED_MULTS.get(biome, 1.0)

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

    def update(self, dt, universe, enemies):
        """更新抛射体物理"""
        if not self.active:
            return None

        # 应用重力
        self.velocity[1] += config.PROJECTILE_GRAVITY * dt

        # 应用空气阻力（时间基，60fps 为基准）
        d = config.PROJECTILE_DRAG ** (dt * 60.0)
        self.velocity[0] *= d
        self.velocity[1] *= d
        self.velocity[2] *= d

        # 更新位置
        self.position[0] += self.velocity[0] * dt
        self.position[1] += self.velocity[1] * dt
        self.position[2] += self.velocity[2] * dt

        # 生命周期
        self.lifetime -= dt
        if self.lifetime <= 0:
            self.active = False
            return None

        # 碰撞检测：地形
        x, y, z = math.floor(self.position[0]), math.floor(self.position[1]), math.floor(self.position[2])
        block_id = universe.get_block_safe(x, y, z)
        if is_solid(block_id):
            self.active = False
            return None

        # 碰撞检测：敌人（保持 0.9 半径，但用平方距离避免开方）
        hit_radius = config.PROJECTILE_HIT_RADIUS
        hit_radius_sq = hit_radius * hit_radius
        px, py, pz = self.position
        for enemy in enemies:
            if not enemy.active:
                continue
            ex, ey, ez = enemy.position
            dx = px - ex
            dy = py - ey
            dz = pz - ez

            # 粗筛：任一轴超出半径就不可能是球形命中
            if abs(dx) > hit_radius or abs(dy) > hit_radius or abs(dz) > hit_radius:
                continue

            if dx * dx + dy * dy + dz * dz < hit_radius_sq:
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
                    for other in enemies:
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
