"""
game.py - 主游戏循环与状态机
管理：游戏状态、波次系统、塔放置、敌人管理、输入处理
"""

import raylib as rl
import math
import os
import config
import gamedata
import save_system
from entities import Tower, Enemy, Pickup, Base, TOWER_TYPES, ENEMY_TYPES
from items import (
    Inventory, MATERIAL_NAMES_CN,
    WEAPON_TYPES, TOOL_TYPES, PROCESS_RECIPES,
    can_process, process, can_craft_weapon, craft_weapon,
    can_craft_tool, craft_tool, dismantle_refund,
    CLASSIC_TOWER_MATERIALS, DISMANTLE_REFUND,
)
from rl_utils import *
from ui_renderer import UIRenderer
from ui_tech_tree import tech_tree_panel
from dataclasses import dataclass, field


# 加工面板按键顺序（1..9,0）：先加工 → 再武器 → 再工具（顺序即 data/recipes.json 中的顺序）
CRAFT_ACTIONS = (
    [("process", k) for k in gamedata.PROCESS_RECIPES]
    + [("weapon", k) for k in gamedata.WEAPONS_RAW]
    + [("tool", k) for k in gamedata.TOOLS_RAW]
)


@dataclass
class SpawnerRuntime:
    """刷怪点运行时状态（存档持久化）。

    mode: dormant（休眠）→ scouting（侦察中）→ active（激活，跑独立波次）
    激活点之间完全独立：各点有自己的 wave_num / 波次进度 / between_wave。
    """
    spawner_id: int
    mode: str = "dormant"  # dormant / scouting / active
    wave_num: int = 0
    spawn_timer: float = 0.0
    spawn_interval: float = 0.5
    enemy_count: int = 0
    max_enemies: int = 0
    enemy_types: list = field(default_factory=list)
    between_wave: bool = True
    between_timer: float = 3.0
    scout_batch_spawned: bool = False
    scouts_pending: int = 0
    message_received: bool = False
    announcement_timer: float = 0.0


class GameState:
    """游戏状态机"""
    MENU = "menu"
    PLAYING = "playing"
    TECH_PANEL = "tech_panel"
    SAVE_MENU = "save_menu"
    GAME_OVER = "game_over"
    VICTORY = "victory"


class Game:
    """游戏主类"""

    def __init__(self, universe, camera, renderer):
        self.universe = universe
        self.camera = camera
        self.renderer = renderer
        self.ui = UIRenderer()

        # 游戏状态
        self.state = GameState.PLAYING
        self.money = config.STARTING_MONEY
        self.wave = 0
        self.score = 0

        # 大本营
        base_y = universe.get_surface_height(universe.base_x, universe.base_z)
        self.base = Base((universe.base_x + 0.5, base_y + 1, universe.base_z + 0.5),
                         max_health=config.BASE_HEALTH)

        # 实体
        self.towers = []
        self.enemies = []
        self.projectiles = []
        self.pickups = []  # 地面掉落物
        # 从宇宙加载预设宝箱
        if hasattr(universe, "loot_pickups"):
            self.pickups.extend(universe.loot_pickups)

        # 刷怪点运行时（独立波次，各点自治）
        self.spawners: dict = {}  # {spawner_id: SpawnerRuntime}
        self._spawner_hash: dict = {}  # {(hx, hz): [spawner_id, ...]} 空间哈希
        self._spawner_hash_dirty: bool = True
        self.wave = 0  # 全局阶段（= max active spawner wave_num），用于 UI 显示
        self.wave_announcement_timer = 0.0  # 最近一次激活公告
        self.wave_delay = 3.0  # 每点波次间隔（秒）

        # 塔放置
        self.selected_tower_type = None
        self.placing_tower = False
        self.placement_position = None

        # 屏幕提示
        self.toast_message = None
        self.toast_timer = 0.0

        # 计时
        self.game_time = 0.0
        self.fps = 0
        self.frame_count = 0
        self.fps_timer = 0.0

        # 科技
        self.unlocked_tech = set()
        self.tech_researching = None
        self.tech_research_timer = 0.0
        self.tech_research_total = 0.0

        # 存档菜单
        self.save_menu_index = 0
        self.save_menu_items = []
        self.save_menu_error = None
        self.save_menu_confirm_delete = None
        self._pending_replace = None

        # 材料/背包/交互
        self.inventory = Inventory()
        self.interaction_mode = "none"  # "none"|"place_classic"|"place_weapon"|"dismantle"
        self.selected_weapon = None
        self.craft_panel_open = False
        self.weapon_panel_open = False
        self.spawner_panel_open = False
        self.spawner_panel_page = 0
        self.craft_timer = 0.0
        self.craft_action = None  # 当前加工/合成动作

        # 初始化刷怪点
        self._init_spawners()

    def _init_spawners(self):
        """构建刷怪点运行时字典，激活最近的 k 个。"""
        self.spawners = {}
        # 材料化基地周边刷怪点（universe.__init__ 已调，这里兜底再调一次）
        awake_ids = self.universe._materialize_base_spawners()
        for sid in self.universe.spawners:
            rt = SpawnerRuntime(spawner_id=sid)
            if sid in awake_ids:
                rt.mode = "active"
                self.universe._ensure_path(sid)
                self._init_spawner_wave(rt, 1)
            self.spawners[sid] = rt
        self._spawner_hash_dirty = True
        self._update_global_phase()

    def _init_spawner_wave(self, rt, wave_num):
        """初始化单个刷怪点的波次（构成来自 data/waves.json）。"""
        wf = gamedata.WAVE_FALLBACK
        wdef = gamedata.wave_def(wave_num)
        rt.wave_num = wave_num
        rt.max_enemies = wdef["count"]
        rt.enemy_count = 0
        rt.spawn_timer = 0.0
        rt.spawn_interval = max(
            wf["interval_min"],
            wf["interval_base"] - wave_num * wf["interval_decay"])
        rt.enemy_types = list(wdef["enemy_types"])
        rt.between_wave = False
        # 公告只在首次激活时设（不在每波重设，避免多独立波次公告频繁闪烁）

    def _restart(self):
        """重启游戏 — 重置所有状态"""
        self.state = GameState.PLAYING
        self.money = config.STARTING_MONEY
        self.wave = 0
        self.score = 0
        # 重置大本营
        self.base.health = self.base.max_health
        self.base.active = True
        self.base.damage_flash = 0.0
        self.towers = []
        self.enemies = []
        self.projectiles = []
        self.pickups = []
        if hasattr(self.universe, "loot_pickups"):
            self.pickups.extend(self.universe.loot_pickups)
        self.wave_delay = 3.0
        self.wave_announcement_timer = 0.0
        self.selected_tower_type = None
        self.placing_tower = False
        self.placement_position = None
        self.game_time = 0.0
        self.fps = 0
        self.frame_count = 0
        self.fps_timer = 0.0
        self.unlocked_tech = set()
        self.tech_researching = None
        self.tech_research_timer = 0.0
        self._spawner_hash = {}
        self._spawner_hash_dirty = True
        self._init_spawners()
        self.inventory.reset()
        self.interaction_mode = "none"
        self.selected_weapon = None
        self.craft_panel_open = False
        self.weapon_panel_open = False
        self.spawner_panel_open = False
        self.spawner_panel_page = 0
        self.craft_timer = 0.0
        self.craft_action = None
        self.save_menu_index = 0
        self.save_menu_items = []
        self.save_menu_error = None
        self.save_menu_confirm_delete = None
        self._pending_replace = None

    def _spawn_enemy_for(self, rt):
        """为指定刷怪点生成一个波次敌人（使用该点自身路径）。"""
        if rt.enemy_count >= rt.max_enemies:
            return

        type_name = rt.enemy_types[rt.enemy_count % len(rt.enemy_types)]
        enemy_type = ENEMY_TYPES.get(type_name)
        if not enemy_type:
            return

        # 确保路径已生成（延迟生成，激活时调过，这里兜底）
        if rt.spawner_id not in self.universe.enemy_paths:
            self.universe._ensure_path(rt.spawner_id)
        path = self.universe.enemy_paths.get(rt.spawner_id)
        if not path or not path.points:
            return

        start = path.points[0]
        wave = rt.wave_num

        eg = gamedata.ENEMY_GLOBAL
        health_mult = eg.get("health_growth", 1.08) ** (wave - 1)
        speed_mult = 1.0 + (wave - 1) * eg.get("speed_growth", 0.0)
        reward = int(enemy_type.base_reward
                     * (1 + wave * eg.get("reward_growth", 0.1)))

        enemy = Enemy(enemy_type, start, path, all_paths=[path],
                       rng=self.universe.rng)
        enemy.home_spawner_id = rt.spawner_id
        enemy.health = int(enemy.health * health_mult)
        enemy.max_health = enemy.health
        enemy.speed = enemy_type.base_speed * speed_mult
        enemy.reward = reward
        self.enemies.append(enemy)
        rt.enemy_count += 1

    def _check_spawner_wave_complete(self, rt):
        """检查单个刷怪点的波次是否完成。"""
        if rt.enemy_count < rt.max_enemies:
            return
        still_active = any(
            e.active and e.home_spawner_id == rt.spawner_id
            for e in self.enemies)
        if still_active:
            return
        rt.between_wave = True
        rt.between_timer = self.wave_delay
        wf = gamedata.WAVE_FALLBACK
        self.money += (wf["bonus_base"] + rt.wave_num * wf["bonus_per_wave"])
        self.score += wf["score_per_wave"] * rt.wave_num

    def _enter_scouting(self, rt):
        """休眠点被触发，进入侦察态。材料化周边刷怪点让侦察兵有目标。"""
        rt.mode = "scouting"
        rt.scout_batch_spawned = False
        rt.message_received = False
        rt.scouts_pending = 0
        # 材料化周边（半径90格），侦察兵派出前周边刷怪点已确定
        sp = self.universe.spawners.get(rt.spawner_id)
        if sp is None:
            # 刷怪点不存在（缺材料化/存档不同步），退回休眠，避免卡在 scouting
            rt.mode = "dormant"
            self._spawner_hash_dirty = True
            return
        radius = gamedata.DISTRIBUTION_CFG.get("materialize_radius", 90)
        self.universe._materialize_around_spawner(sp.x, sp.z, radius)

    def _spawner_noise_direction(self, sp):
        """从刷怪点位置计算噪音方向（朝最大噪音源：基地 + 已建塔）。

        1/dist² 衰减加权。返回 (dx, dz) 或 (0,0)。
        """
        sx, sz = sp.x + 0.5, sp.z + 0.5
        total_dx, total_dz = 0.0, 0.0
        # 基地
        if self.base and self.base.active:
            dx = self.base.position[0] - sx
            dz = self.base.position[2] - sz
            dsq = dx * dx + dz * dz
            if dsq < 1:
                return (dx, dz)
            w = 1.0 / dsq
            total_dx += dx * w
            total_dz += dz * w
        # 塔
        for tower in self.towers:
            dx = tower.position[0] - sx
            dz = tower.position[2] - sz
            dsq = dx * dx + dz * dz
            if dsq < 1:
                return (dx, dz)
            w = 1.0 / dsq
            total_dx += dx * w
            total_dz += dz * w
        return (total_dx, total_dz)

    def _rebuild_spawner_hash(self):
        """重建 dormant 刷怪点空间哈希（格 16）。"""
        self._spawner_hash = {}
        cell = 16
        for sid, rt in self.spawners.items():
            if rt.mode != "dormant":
                continue
            sp = self.universe.spawners.get(sid)
            if sp is None:
                continue  # 存档/运行时不同步，跳过该 id
            hx, hz = sp.x // cell, sp.z // cell
            key = (hx, hz)
            if key not in self._spawner_hash:
                self._spawner_hash[key] = []
            self._spawner_hash[key].append(sid)
        self._spawner_hash_dirty = False

    def _query_spawner_hash(self, x, z, radius, check_dormant=True):
        """查询空间哈希中半径内的 dormant 刷怪点 id 列表。"""
        cell = 16
        r = int(math.ceil(radius / cell))
        hx, hz = int(math.floor(x)) // cell, int(math.floor(z)) // cell
        result = []
        for dx in range(-r, r + 1):
            for dz in range(-r, r + 1):
                for sid in self._spawner_hash.get((hx + dx, hz + dz), []):
                    if sid not in result:
                        result.append(sid)
        return result

    # ─── 刷怪点种类触发半径（来自 data/spawners.json） ─────────
    @staticmethod
    def _trigger_max_radius(key):
        """所有刷怪点种类中某类触发半径的最大值（用于空间哈希查询半径）。"""
        radii = [
            gamedata.spawner_kind(k)["trigger_radii"].get(key, 15.0)
            for k in gamedata.SPAWNERS_CFG.get("kinds", {})]
        return max(radii) if radii else 15.0

    def _kind_radius_of(self, sp, key):
        """刷怪点对象按种类取触发半径。"""
        if sp is None:
            return 15.0
        kind = getattr(sp, "kind", "standard") or "standard"
        return gamedata.spawner_kind(kind)["trigger_radii"].get(key, 15.0)

    def _register_combat(self, x, z):
        """战斗事件：检查附近休眠点是否被触发。"""
        r = self._trigger_max_radius("combat")
        if self._spawner_hash_dirty:
            self._rebuild_spawner_hash()
        for sid in self._query_spawner_hash(x, z, r):
            rt = self.spawners.get(sid)
            if not rt or rt.mode != "dormant":
                continue
            sp = self.universe.spawners.get(sid)
            if sp is None:
                continue
            kr = self._kind_radius_of(sp, "combat")
            dx = sp.x + 0.5 - x
            dz = sp.z + 0.5 - z
            if dx * dx + dz * dz <= kr * kr:
                self._enter_scouting(rt)
                self._spawner_hash_dirty = True

    def _register_build(self, x, z):
        """造塔事件：检查附近休眠点是否被触发。"""
        r = self._trigger_max_radius("build")
        if self._spawner_hash_dirty:
            self._rebuild_spawner_hash()
        for sid in self._query_spawner_hash(x, z, r):
            rt = self.spawners.get(sid)
            if not rt or rt.mode != "dormant":
                continue
            sp = self.universe.spawners.get(sid)
            if sp is None:
                continue
            kr = self._kind_radius_of(sp, "build")
            dx = sp.x + 0.5 - x
            dz = sp.z + 0.5 - z
            if dx * dx + dz * dz <= kr * kr:
                self._enter_scouting(rt)
                self._spawner_hash_dirty = True

    def _update_global_phase(self):
        """更新全局阶段 = max active spawner wave_num。"""
        self.wave = 0
        for rt in self.spawners.values():
            if rt.mode == "active" and rt.wave_num > self.wave:
                self.wave = rt.wave_num

    # ─── 科技 ────────────────────────────────────────────────
    def _has_effect_tech(self, effect_key: str) -> bool:
        """是否已研究"具有某效果键"的科技（按突破点表的 effect 字段查找）。"""
        return any(
            self.has_tech(tid)
            for tid in gamedata.TECH_EFFECT_NODES.get(effect_key, []))

    def has_tech(self, tech_id: str) -> bool:
        """某科技是否已解锁。

        新突破点 ID 直查；legacy ID 经 TECH_LEGACY_MAP 归一后再查
        （旧存档 v3 载入时已迁移，此处兜底）。
        """
        if tech_id in self.unlocked_tech:
            return True
        new_id = gamedata.TECH_LEGACY_MAP.get(tech_id)
        return bool(new_id) and new_id in self.unlocked_tech

    def tech_label(self, tech_id: str) -> str:
        """科技 ID 的显示名：突破点中文名 → legacy 中文名 → 原始 ID 兜底。"""
        bt = gamedata.TECH_BREAKTHROUGHS.get(tech_id)
        if bt:
            return bt.get("name_cn") or tech_id
        legacy = gamedata.TECH_TREE.get(tech_id)
        if legacy:
            return legacy.get("name_cn") or tech_id
        return tech_id

    def tech_damage_mult(self):
        """科技带来的塔伤害倍率（flint_knapping +10%）"""
        return gamedata.TECH_EFFECTS.get("tower_damage", 1.0) if self._has_effect_tech("tower_damage") else 1.0

    def build_cost_mult(self):
        """科技带来的建造费用倍率（hafting_joinery -15%）"""
        return gamedata.TECH_EFFECTS.get("build_cost", 1.0) if self._has_effect_tech("build_cost") else 1.0

    def tower_cost(self, tower_type):
        return int(tower_type.base_cost * self.build_cost_mult())

    def _complete_research(self):
        """研究完成：写入 unlocked 并复位研究状态。

        两条计时路径共用（_update_playing 与 _update_tech_panel）。
        此前该实现缺失，面板打开时研究计时归零会抛 AttributeError。
        """
        if not self.tech_researching:
            return
        self.unlocked_tech.add(self.tech_researching)
        self.tech_researching = None
        self.tech_research_timer = 0.0
        self.tech_research_total = 0.0

    def _research_tech(self, tech_id):
        """开始研究科技（tech_id 为突破点 ID 或 legacy ID）。"""
        import tech_tree as tt
        tree = tt.tech_tree()
        node = tree.nodes.get(tech_id)
        if not node:
            # 尝试 legacy ID 映射
            legacy_map = gamedata.TECH_LEGACY_MAP
            new_id = legacy_map.get(tech_id)
            if new_id:
                tech_id = new_id
                node = tree.nodes.get(tech_id)
        if not node:
            return False
        # 检查是否可研究
        ok, reason = tree.can_research(tech_id, self.unlocked_tech, self.money, self.tech_researching)
        if not ok:
            return False
        self.money -= node["cost"]
        self.tech_researching = tech_id
        self.tech_research_total = node["time"]
        self.tech_research_timer = node["time"]
        return True

    def _tech_gate_ok(self, requires_tech) -> bool:
        """科技前置是否已全部解锁（requires_tech 可为 None/[]/tuple/list）。"""
        if not requires_tech:
            return True
        return all(self.has_tech(t) for t in requires_tech)

    def _craft_tech_missing(self, requires_tech) -> list:
        """返回未解锁的科技前置列表（空 = 已全部解锁）。"""
        if not requires_tech:
            return []
        return [t for t in requires_tech if not self.has_tech(t)]

    def _award_kill(self, enemy):
        """击杀结算：金钱 + 分数 + 掉落物（掉落表来自 data/enemies.json）"""
        self.money += enemy.reward
        self.score += enemy.reward * 2

        drops = gamedata.DROP_TABLES.get(enemy.type_name, [("wood", 1)])
        drop = self.universe.rng.choice(drops)
        # 同类型越强越多：波次越高(同类型血量随 1.08^(wave-1) 成长)掉落越多；
        # 加成只看波次、对所有类型同等，保持各类型原始掉落比例不变。
        amount = max(1, drop[1] + self.wave // 10)
        pickup = Pickup(
            position=(enemy.position[0], enemy.position[1] + 0.3, enemy.position[2]),
            item_type="material",
            item_name=drop[0],
            amount=amount,
            rng=self.universe.rng,
        )
        self.pickups.append(pickup)

    def update(self, dt):
        """更新游戏逻辑"""
        self.game_time += dt

        if self.state == GameState.PLAYING:
            self._update_playing(dt)
        elif self.state == GameState.TECH_PANEL:
            self._update_tech_panel(dt)
        elif self.state == GameState.SAVE_MENU:
            self._update_save_menu(dt)
        elif self.state == GameState.GAME_OVER:
            # 按 Enter 重启
            if rl.IsKeyPressed(rl.KEY_ENTER):
                self._restart()

        # FPS 计数
        self.frame_count += 1
        self.fps_timer += dt
        if self.fps_timer >= 1.0:
            self.fps = self.frame_count
            self.frame_count = 0
            self.fps_timer = 0.0

        # 更新相机（科技树面板打开时暂停）
        if self.state != GameState.TECH_PANEL:
            self.camera.update(dt)

        # 更新天气
        if self.state != GameState.TECH_PANEL:
            self.universe.update_weather(dt)

    def _sync_materialized_spawners(self):
        """同步 universe 新材料化的刷怪点到 game（dormant）。"""
        added = False
        for sid in self.universe.spawners:
            if sid not in self.spawners:
                self.spawners[sid] = SpawnerRuntime(spawner_id=sid)
                added = True
        if added:
            self._spawner_hash_dirty = True

    def _update_playing(self, dt):
        """更新游戏进行中状态"""
        # ── 同步新材料化刷怪点 ──
        self._sync_materialized_spawners()

        # ── 刷怪点管理 ──
        # 激活点：各自独立波次
        for rt in self.spawners.values():
            if rt.mode != "active":
                continue
            if rt.announcement_timer > 0:
                rt.announcement_timer -= dt
                self.wave_announcement_timer = max(
                    self.wave_announcement_timer, rt.announcement_timer)
            if rt.between_wave:
                rt.between_timer -= dt
                if rt.between_timer <= 0:
                    self._init_spawner_wave(rt, rt.wave_num + 1)
            else:
                rt.spawn_timer -= dt
                if (rt.spawn_timer <= 0
                        and rt.enemy_count < rt.max_enemies):
                    self._spawn_enemy_for(rt)
                    rt.spawn_timer = rt.spawn_interval
                self._check_spawner_wave_complete(rt)

        # 休眠点：触发检测（仅正式兵 wave 行为可触发，侦察兵路过不触发）
        presence_r = self._trigger_max_radius("presence")
        if self._spawner_hash_dirty:
            self._rebuild_spawner_hash()
        for enemy in self.enemies:
            if not enemy.active:
                continue
            if enemy.behavior != "wave":
                continue  # 侦察兵/侦察回巢不算触发
            ex, ez = enemy.position[0], enemy.position[2]
            for sid in self._query_spawner_hash(ex, ez, presence_r):
                rt = self.spawners.get(sid)
                if not rt or rt.mode != "dormant":
                    continue
                sp = self.universe.spawners.get(sid)
                if sp is None:
                    continue
                kr = self._kind_radius_of(sp, "presence")
                dx = sp.x + 0.5 - ex
                dz = sp.z + 0.5 - ez
                if dx * dx + dz * dz <= kr * kr:
                    self._enter_scouting(rt)
                    self._spawner_hash_dirty = True
                    break

        # 侦察中点：派出侦察怪 + 最终判定
        for rt in self.spawners.values():
            if rt.mode != "scouting":
                continue
            if not rt.scout_batch_spawned:
                sp = self.universe.spawners.get(rt.spawner_id)
                if sp is None:
                    continue  # 刷怪点缺失，等 hash 重建后再处理
                # 侦察兵配置按刷怪点种类（data/spawners.json）
                kind_cfg = gamedata.spawner_kind(getattr(sp, "kind", "standard"))
                scout_cfg = kind_cfg["scout"]
                scout_type_name = scout_cfg.get("enemy_type", "regular")
                scout_type = ENEMY_TYPES.get(scout_type_name, ENEMY_TYPES["regular"])
                # 计算噪音方向（从刷怪点位置朝最大噪音源）
                noise_dir = self._spawner_noise_direction(sp)
                for _ in range(scout_cfg.get("batch", 3)):
                    scout = Enemy.as_scout(
                        scout_type,
                        (sp.x + 0.5, sp.surface_y + 1, sp.z + 0.5),
                        rt.spawner_id, self.universe.rng,
                        noise_dir=noise_dir, scout_cfg=scout_cfg)
                    self.enemies.append(scout)
                    rt.scouts_pending += 1
                rt.scout_batch_spawned = True
            # 全部侦察怪已决后做最终判定（cleanup 已设 mode 则跳过）
            if rt.scouts_pending <= 0 and rt.scout_batch_spawned:
                if rt.message_received:
                    rt.mode = "active"
                    self.universe._ensure_path(rt.spawner_id)
                    self._init_spawner_wave(rt, max(1, self.wave))
                    rt.announcement_timer = 3.0  # 激活公告
                    self._update_global_phase()
                    self._spawner_hash_dirty = True
                else:
                    rt.mode = "dormant"
                    self._spawner_hash_dirty = True

        # ── 波次公告计时 ──
        if self.wave_announcement_timer > 0:
            self.wave_announcement_timer -= dt

        # ── 屏幕提示计时 ──
        if self.toast_timer > 0:
            self.toast_timer -= dt

        # ── 更新塔（收集新抛射体 + 战斗事件触发休眠点） ──
        new_projectiles = []
        for tower in self.towers:
            proj = tower.update(dt, self.enemies)
            if proj:
                new_projectiles.append(proj)
                self._register_combat(tower.position[0], tower.position[2])
        self.projectiles.extend(new_projectiles)

        # ── 更新敌人（传 towers/base 供侦察怪感知） ──
        for enemy in self.enemies:
            result = enemy.update(dt, self.universe,
                                  towers=self.towers, base=self.base)
            if result == "reached_base":
                killed = self.base.take_damage(gamedata.ENEMY_GLOBAL.get("base_damage", 5))
                if killed:
                    self.state = GameState.GAME_OVER
                    return

        # ── 侦察兵遇到正式兵获取信息（侦察兵获得信息回巢，正式兵不受影响） ──
        scouts = [e for e in self.enemies
                  if e.active and e.behavior == "scout_wander"]
        waves = [e for e in self.enemies
                 if e.active and e.behavior == "wave"]
        if scouts and waves:
            for scout in scouts:
                if not scout.active or scout.behavior != "scout_wander":
                    continue  # 可能已被前面的循环改为回巢
                meet_r = gamedata.scout_cfg_of(scout).get("meet_radius", 8.0)
                meet_r2 = meet_r * meet_r
                for wave in waves:
                    if not wave.active:
                        continue
                    dx = scout.position[0] - wave.position[0]
                    dz = scout.position[2] - wave.position[2]
                    if dx * dx + dz * dz <= meet_r2:
                        scout.has_message = True
                        scout._start_return(self.universe)
                        break

        # ── 更新抛射体 ──
        if self.projectiles:
            active_projectiles = []
            for proj in self.projectiles:
                if not proj.active:
                    continue
                result = proj.update(dt, self.universe, self.enemies)
                if result and result.get("hit"):
                    if result.get("killed"):
                        self._award_kill(result["enemy"])
                    for extra in result.get("extra_kills", []):
                        self._award_kill(extra)
                    e = result.get("enemy")
                    if e:
                        self._register_combat(e.position[0], e.position[2])
                if proj.active:
                    active_projectiles.append(proj)
            self.projectiles = active_projectiles

        # ── 清理非活跃敌人（先回收侦察怪回巢结果） ──
        if not all(enemy.active for enemy in self.enemies):
            for enemy in self.enemies:
                if enemy.active:
                    continue
                if enemy.behavior == "wave":
                    continue
                # 侦察怪：上报回巢结果
                sid = enemy.home_spawner_id
                if sid in self.spawners:
                    rt = self.spawners[sid]
                    if rt.mode == "scouting":
                        if enemy.scout_returned_with_message:
                            rt.message_received = True
                            # 立即激活，不必等全部侦察怪返回
                            rt.mode = "active"
                            self.universe._ensure_path(rt.spawner_id)
                            self._init_spawner_wave(
                                rt, max(1, self.wave))
                            rt.announcement_timer = 3.0  # 激活公告
                            self._update_global_phase()
                            self._spawner_hash_dirty = True
                        elif rt.scouts_pending > 0:
                            rt.scouts_pending -= 1
                            if rt.scouts_pending <= 0:
                                rt.mode = "dormant"
                                self._spawner_hash_dirty = True
            self.enemies = [enemy for enemy in self.enemies if enemy.active]

        # 更新掉落物（上下浮动动画）
        if self.pickups:
            active_pickups = []
            for pickup in self.pickups:
                if pickup.active:
                    pickup.update(dt)
                    active_pickups.append(pickup)
            self.pickups = active_pickups

        # 科技研究
        if self.tech_researching:
            self.tech_research_timer -= dt
            if self.tech_research_timer <= 0:
                self._complete_research()

        # 输入处理
        self._handle_input(dt)

    def _update_tech_panel(self, dt):
        """更新科技树面板状态"""
        panel = tech_tree_panel(self)
        vw, vh = rl.GetScreenWidth(), rl.GetScreenHeight()

        # T 关闭面板（Esc 也已通过 SetExitKey(0) 取消 raylib 默认窗口退出键，
        # 现在 Esc 由各面板自己处理，见各 *_panel 的 process_input）
        if rl.IsKeyPressed(rl.KEY_T):
            panel.close_panel()
            self.state = GameState.PLAYING
            return

        # 处理面板输入
        panel.process_input(vw, vh)

        # 面板关闭时回到游戏状态
        if not panel.open:
            self.state = GameState.PLAYING
            return

        # 面板打开时游戏暂停，处理研究进度
        if self.tech_researching:
            self.tech_research_timer -= dt
            if self.tech_research_timer <= 0:
                self._complete_research()

    def _handle_input(self, dt):
        """处理输入"""
        # 科技树面板打开时，只处理面板输入（在 _update_tech_panel 中处理）
        if self.state == GameState.TECH_PANEL:
            return

        # 刷怪点波次面板打开时（只读，P/Esc 关闭，左右翻页）
        if self.spawner_panel_open:
            if rl.IsKeyPressed(rl.KEY_P) or rl.IsKeyPressed(rl.KEY_ESCAPE):
                self.spawner_panel_open = False
            elif rl.IsKeyPressed(rl.KEY_LEFT):
                self.spawner_panel_page = max(0, self.spawner_panel_page - 1)
            elif rl.IsKeyPressed(rl.KEY_RIGHT):
                self.spawner_panel_page += 1  # 渲染时 clamp
            return

        # 加工面板打开时
        if self.craft_panel_open:
            if rl.IsKeyPressed(rl.KEY_B) or rl.IsKeyPressed(rl.KEY_ESCAPE):
                self.craft_panel_open = False
                return
            # 鼠标点击行（支持超过 10 个数字键的动作）
            if rl.IsMouseButtonPressed(rl.MOUSE_BUTTON_LEFT):
                hit = self.ui.craft_hit_action(self, rl.GetMouseX(), rl.GetMouseY())
                if hit:
                    self._craft_panel_action(*hit)
                    return
            # 1-9,0: 依次对应 CRAFT_ACTIONS（加工→武器→工具）
            idx = None
            for _i in range(10):
                key = rl.KEY_ONE + _i if _i < 9 else rl.KEY_ZERO
                if rl.IsKeyPressed(key):
                    idx = _i
                    break
            if idx is not None and idx < len(CRAFT_ACTIONS):
                self._craft_panel_action(*CRAFT_ACTIONS[idx])
            return

        # 武器库面板打开时（C 键打开，点击选择）
        if self.weapon_panel_open:
            if rl.IsKeyPressed(rl.KEY_C) or rl.IsKeyPressed(rl.KEY_ESCAPE):
                self.weapon_panel_open = False
                return
            if rl.IsMouseButtonPressed(rl.MOUSE_BUTTON_LEFT):
                wname = self.ui.weapon_hit_action(self, rl.GetMouseX(), rl.GetMouseY())
                if wname:
                    self.weapon_panel_open = False
                    self._select_weapon(wname)
                return
            return

        # T 键打开科技树面板（面板打开时再按 T 关闭，见 _update_tech_panel）
        if rl.IsKeyPressed(rl.KEY_T) and self.state == GameState.PLAYING:
            tech_tree_panel(self).open_panel()
            self.state = GameState.TECH_PANEL

        # F5/F8: 存档快捷键（仅游戏中）
        if self.state == GameState.PLAYING:
            if rl.IsKeyPressed(rl.KEY_F5):
                self._quick_save()
            elif rl.IsKeyPressed(rl.KEY_F8):
                self._open_save_menu()

        # 数字键: 游戏内选择塔类型
        if self.state == GameState.PLAYING:
            # 数字键: 选择经典塔
            if rl.IsKeyPressed(rl.KEY_ONE):
                self._select_tower_type("stone_thrower")
            elif rl.IsKeyPressed(rl.KEY_TWO):
                self._select_tower_type("water_cannon")
            elif rl.IsKeyPressed(rl.KEY_THREE):
                self._select_tower_type("sand_trap")
            elif rl.IsKeyPressed(rl.KEY_FOUR):
                self._select_tower_type("metal_tower")
            # 5-8: 选择武器装备放置
            elif rl.IsKeyPressed(rl.KEY_FIVE):
                self._select_weapon("wood_club")
            elif rl.IsKeyPressed(rl.KEY_SIX):
                self._select_weapon("stone_axe")
            elif rl.IsKeyPressed(rl.KEY_SEVEN):
                self._select_weapon("iron_sword")
            elif rl.IsKeyPressed(rl.KEY_EIGHT):
                self._select_weapon("sand_sling")
            # B 键: 打开加工/合成面板
            elif rl.IsKeyPressed(rl.KEY_B):
                self._toggle_craft_panel()
            # C 键: 打开武器库（选择武器装备放置）
            elif rl.IsKeyPressed(rl.KEY_C):
                self.weapon_panel_open = True
                self.interaction_mode = "none"
                self.placing_tower = False
                self.selected_tower_type = None
            # P 键: 打开刷怪点波次面板
            elif rl.IsKeyPressed(rl.KEY_P):
                self._toggle_spawner_panel()
            # X 键: 切换拆除模式
            elif rl.IsKeyPressed(rl.KEY_X):
                self._toggle_dismantle()
            elif rl.IsKeyPressed(rl.KEY_ESCAPE):
                self.placing_tower = False
                self.selected_tower_type = None
                self.interaction_mode = "none"
                self.selected_weapon = None

            # 鼠标点击: 优先级链
            if rl.IsMouseButtonPressed(rl.MOUSE_BUTTON_LEFT):
                if self.interaction_mode == "dismantle":
                    self._try_dismantle_tower()
                elif self.placing_tower and self.selected_tower_type:
                    self._try_place_tower()
                elif self.selected_weapon:
                    self._try_place_weapon_tower()
                else:
                    # 默认: 拾取 → 升级塔 → 采集道具
                    # 拾取优先:掉落物常落在塔脚下,先拾取可避免被升级逻辑截胡
                    picked = self._try_pickup()
                    if not picked:
                        upgraded = self._try_upgrade_tower()
                        if not upgraded:
                            self._try_harvest_prop()

    def _try_upgrade_tower(self) -> bool:
        """点击已有塔进行升级，返回是否成功"""
        wx, wy, wz = self.camera.screen_to_world(rl.GetMouseX(), rl.GetMouseY())
        x, z = math.floor(wx), math.floor(wz)
        for tower in self.towers:
            # 塔位是世界坐标（方块中心），故与格子中心 (x+0.5, z+0.5) 比较
            # 命中范围收紧到塔自身所在格(中心距 <= 0.6)，避免误抓邻近格的点击
            if abs(tower.position[0] - (x + 0.5)) <= 0.6 and abs(tower.position[2] - (z + 0.5)) <= 0.6:
                if tower.level >= config.MAX_TOWER_LEVEL:
                    self.toast("已达最高等级")
                    return True
                cost = tower.upgrade_cost
                if self.money < cost:
                    self.toast(f"金钱不足 (需要 {cost})")
                    return True
                tower.upgrade()
                self.money -= cost
                self.toast(f"升级为 Lv{tower.level}")
                return True
        return False

    def _try_harvest_prop(self):
        """点击采集地表装饰物"""
        wx, wy, wz = self.camera.screen_to_world(rl.GetMouseX(), rl.GetMouseY())
        x, z = math.floor(wx), math.floor(wz)
        prop = self.universe.props_by_cell.get((x, z))
        if prop and prop.prop_type.harvest_material:
            mat_name = prop.prop_type.harvest_material
            mat_amount = prop.prop_type.harvest_amount
            self.inventory.add_material(mat_name, mat_amount)
            # 从全局和区块列表中移除
            prop.harvested = True
            self.universe.remove_prop(prop)
            self.toast(f"+{mat_amount} {MATERIAL_NAMES_CN.get(mat_name, mat_name)}")
        elif prop:
            self.toast("该装饰物不可采集")
        else:
            self.toast("此处没有可采集物")

    def _try_pickup(self) -> bool:
        """点击拾取地面掉落物，返回是否拾取成功

        screen_to_world 返回射线与地形的交点(地面点)，而掉落物悬浮在地面上方。
        相机俯角较斜(~29°)时，瞄准可见的悬浮物品，地面交点会向斜后方偏移约
        1 格(视差)。掉落物的 xz 本就是它的地面投影，因此直接与点击地面点比较，
        并把拾取半径放宽到 1.8 格以吸收视差偏移；选最近的一个，避免漏捡。
        """
        wx, wy, wz = self.camera.screen_to_world(rl.GetMouseX(), rl.GetMouseY())
        PICKUP_RADIUS_SQ = 1.8 * 1.8  # 半径 1.8 格，吸收悬浮视差
        nearest = None
        nearest_d2 = PICKUP_RADIUS_SQ
        for pickup in self.pickups:
            if not pickup.active:
                continue
            dx = pickup.position[0] - wx
            dz = pickup.position[2] - wz
            d2 = dx * dx + dz * dz
            if d2 < nearest_d2:
                nearest = pickup
                nearest_d2 = d2
        if nearest is None:
            return False
        pickup = nearest
        # 收入背包
        if pickup.item_type == "material":
            self.inventory.add_material(pickup.item_name, pickup.amount)
            self.toast(f"+{pickup.amount} {MATERIAL_NAMES_CN.get(pickup.item_name, pickup.item_name)}")
        elif pickup.item_type == "weapon":
            self.inventory.add_weapon(pickup.item_name, pickup.amount)
            wdef = WEAPON_TYPES.get(pickup.item_name)
            label = wdef.label_cn if wdef else pickup.item_name
            self.toast(f"+{pickup.amount} {label}")
        pickup.active = False
        return True

    def _try_dismantle_tower(self):
        """拆除塔并回收材料"""
        wx, wy, wz = self.camera.screen_to_world(rl.GetMouseX(), rl.GetMouseY())
        x, z = math.floor(wx), math.floor(wz)
        for i, tower in enumerate(self.towers):
            if abs(tower.position[0] - (x + 0.5)) <= 1 and abs(tower.position[2] - (z + 0.5)) <= 1:
                # 获取材料配方
                if tower.source and tower.source[0] == "weapon":
                    wdef = WEAPON_TYPES.get(tower.source[1])
                    recipe = wdef.recipe if wdef else {}
                else:
                    recipe = CLASSIC_TOWER_MATERIALS.get(tower.source[1] if tower.source else "", {})
                # 回收材料
                refund = dismantle_refund(recipe)
                for mat, amt in refund.items():
                    self.inventory.add_material(mat, amt)
                self.towers.pop(i)
                self.toast("已拆除，材料已回收")
                return
        self.toast("未找到可拆除的塔")

    def _try_place_weapon_tower(self):
        """放置武器塔"""
        if not self.selected_weapon:
            return
        wx, wy, wz = self.camera.screen_to_world(rl.GetMouseX(), rl.GetMouseY())
        x, z = math.floor(wx), math.floor(wz)
        if not self._is_valid_placement(x, z):
            self.toast("无法在此放置")
            return
        install_fee = self.selected_weapon.install_fee
        if self.money < install_fee:
            self.toast(f"安装费不足 (需要 {install_fee})")
            return
        if self.inventory.remove_weapon(self.selected_weapon.name, 1):
            y = self.universe.get_surface_height(x, z) + 1
            tower = Tower.from_weapon(self.selected_weapon, (x + 0.5, y, z + 0.5),
                                       damage_mult=self.tech_damage_mult())
            self.towers.append(tower)
            self.money -= install_fee
            # 注册塔格（延迟路径绕行）
            self.universe.add_tower_cell(x, z)
            # 造塔触发
            self._register_build(tower.position[0], tower.position[2])
            self.toast(f"已安装 {self.selected_weapon.label_cn} 塔 (-{install_fee}$)")
            self.selected_weapon = None
        else:
            self.toast("背包中没有该武器")

    def _toggle_craft_panel(self):
        """切换加工/合成面板"""
        self.craft_panel_open = not self.craft_panel_open
        if self.craft_panel_open:
            self.interaction_mode = "none"
            self.placing_tower = False
            self.selected_tower_type = None
            self.selected_weapon = None

    def _toggle_spawner_panel(self):
        """切换刷怪点波次面板"""
        self.spawner_panel_open = not self.spawner_panel_open
        if self.spawner_panel_open:
            self.spawner_panel_page = 0
            self.interaction_mode = "none"
            self.placing_tower = False
            self.selected_tower_type = None
            self.selected_weapon = None

    def _toggle_dismantle(self):
        """切换拆除模式"""
        if self.interaction_mode == "dismantle":
            self.interaction_mode = "none"
            self.toast("拆除模式关闭")
        else:
            self.interaction_mode = "dismantle"
            self.placing_tower = False
            self.selected_tower_type = None
            self.selected_weapon = None
            self.toast("拆除模式开启 - 点击塔拆除")

    def _craft_panel_action(self, action_type: str, target: str):
        """加工面板内的操作（科技前置在 data/*.json 的 requires_tech 字段）"""
        if action_type == "process":
            recipe = PROCESS_RECIPES.get(target)
            if not recipe:
                return
            missing = self._craft_tech_missing(recipe.get("requires_tech"))
            if missing:
                self.toast(f"需要科技: {', '.join(missing)}  (按 T 研究)")
                return
            # 检查是否有适用工具
            required_tool = recipe.get("requires_tool")
            use_tool = False
            if required_tool:
                # 必需工具：没有就不能加工
                if not self.inventory.has_tool(required_tool):
                    self.toast(f"需要工具: {TOOL_TYPES[required_tool].label_cn}")
                    return
                use_tool = True
            else:
                # 可选工具：有就加速/增产
                for tname, tdef in TOOL_TYPES.items():
                    if tdef.applies == target and self.inventory.has_tool(tname):
                        use_tool = True
                        break
            if not use_tool:
                # 没有工具时，检查徒手是否可行
                if recipe["hand"] <= 0:
                    self.toast("需要工具才能加工")
                    return
            ok, out_mat, amt = process(target, self.inventory, use_tool=use_tool)
            if ok:
                mode = "工具" if use_tool else "徒手"
                self.toast(f"+{amt} {MATERIAL_NAMES_CN.get(out_mat, out_mat)} ({mode})")
            else:
                self.toast("材料不足或条件不满足")

        elif action_type == "weapon":
            wdef = WEAPON_TYPES.get(target)
            if wdef is None:
                return
            missing = self._craft_tech_missing(wdef.requires_tech)
            if missing:
                self.toast(f"需要科技: {', '.join(missing)}  (按 T 研究)")
                return
            if can_craft_weapon(target, self.inventory):
                craft_weapon(target, self.inventory)
                self.toast(f"合成 {wdef.label_cn} 成功")
            else:
                self.toast(f"无法合成 {wdef.label_cn}: 材料不足")

        elif action_type == "tool":
            tdef = TOOL_TYPES.get(target)
            if tdef is None:
                return
            missing = self._craft_tech_missing(tdef.requires_tech)
            if missing:
                self.toast(f"需要科技: {', '.join(missing)}  (按 T 研究)")
                return
            if can_craft_tool(target, self.inventory):
                craft_tool(target, self.inventory)
                self.toast(f"合成 {tdef.label_cn} 成功")
            else:
                self.toast(f"无法合成 {tdef.label_cn}: 材料不足")

    def _select_tower_type(self, type_name):
        """选择塔类型"""
        tower_type = TOWER_TYPES.get(type_name)
        if not tower_type:
            return

        # 检查科技前置
        if tower_type.requires_tech:
            missing = [t for t in tower_type.requires_tech if not self.has_tech(t)]
            if missing:
                self.toast(f"需要科技: {', '.join(self.tech_label(t) for t in missing)}  (按 T 研究)", 2.5)
                return  # 未解锁

        self.selected_tower_type = tower_type
        self.placing_tower = True

    def _select_weapon(self, weapon_name):
        """选择武器装备放置"""
        wdef = WEAPON_TYPES.get(weapon_name)
        if not wdef:
            return
        missing = self._craft_tech_missing(wdef.requires_tech)
        if missing:
            self.toast(f"需要科技: {', '.join(missing)}  (按 T 研究)")
            return
        if self.inventory.get_weapon(weapon_name) < 1:
            self.toast(f"背包中没有 {wdef.label_cn}")
            return
        self.selected_weapon = wdef
        self.selected_tower_type = None
        self.placing_tower = False
        self.interaction_mode = "none"
        self.toast(f"放置 {wdef.label_cn} (${wdef.install_fee})")

    def toast(self, message, duration=2.0):
        """屏幕提示"""
        self.toast_message = message
        self.toast_timer = duration

    def _try_place_tower(self):
        """尝试放置塔"""
        if not self.placing_tower or not self.selected_tower_type:
            return

        # 获取鼠标位置的世界坐标
        wx, wy, wz = self.camera.screen_to_world(rl.GetMouseX(), rl.GetMouseY())
        x, z = math.floor(wx), math.floor(wz)

        # 检查位置有效性
        if not self._is_valid_placement(x, z):
            return

        # 检查金钱
        cost = self.tower_cost(self.selected_tower_type)
        if self.money < cost:
            return

        # 放置塔（应用科技伤害加成）
        # 用纯噪声查询：放置点击不该触发区块生成 / LRU 淘汰
        y = self.universe._surface_height_pure(x, z) + 1
        tower = Tower(self.selected_tower_type, (x + 0.5, y, z + 0.5),
                      damage_mult=self.tech_damage_mult())
        self.towers.append(tower)
        self.money -= cost
        # 注册塔格（延迟路径绕行）
        self.universe.add_tower_cell(x, z)

        # 造塔触发：检查附近休眠点
        self._register_build(tower.position[0], tower.position[2])

        # 取消放置模式
        self.placing_tower = False
        self.selected_tower_type = None

    def _is_valid_placement(self, x, z):
        """检查放置位置是否有效（无限地形，无边界限制）"""
        # 不能放在海洋/水下（纯噪声判定：与 chunk 水柱阈值 h<0.20 一致，
        # 比 biome 标签的 0.18 略严格，避免塔落在浅水里；也不用 get_biome_safe，
        # 它在区块未加载时默认返回 plains 会漏判）
        if not self.universe._is_land_pure(x, z):
            return False

        # 不能放在大本营上
        if abs(x - self.universe.base_x) <= 2 and abs(z - self.universe.base_z) <= 2:
            return False

        # 不能放在任何路径上（O(1) 查找）
        if self.universe._is_on_path(x, z):
            return False

        # 不能放在已有塔的位置
        for tower in self.towers:
            if abs(tower.position[0] - (x + 0.5)) < 1 and abs(tower.position[2] - (z + 0.5)) < 1:
                return False

        return True

    def render(self):
        """渲染游戏"""
        begin_drawing()

        # 渲染世界（预览/高亮在相机上下文内渲染）
        entities = {
            "towers": self.towers,
            "enemies": self.enemies,
            "projectiles": self.projectiles,
            "pickups": self.pickups,
            "spawners": [(rt, self.universe.spawners.get(rt.spawner_id))
                          for rt in self.spawners.values()
                          if self.universe.spawners.get(rt.spawner_id)],
        }
        # 构建相机内额外渲染回调（放置预览 + 拆除高亮）
        _need_preview = (self.placing_tower and self.selected_tower_type) or self.selected_weapon
        _need_dismantle = (self.interaction_mode == "dismantle")
        camera_extra = None
        if _need_preview or _need_dismantle:
            def camera_extra():
                if _need_preview:
                    self.ui.render_placement_preview(self)
                if _need_dismantle:
                    self.ui.render_dismantle_highlight(self)

        self.renderer.render(self.camera, self.universe, entities, camera_extra)

        # 渲染 HUD
        self.ui.render_hud(self)

        # 渲染波次公告
        if self.wave_announcement_timer > 0:
            self.ui.render_wave_announcement(self)

        # 渲染科技树面板
        if self.state == GameState.TECH_PANEL:
            panel = tech_tree_panel(self)
            from rl_utils import _get_font
            font = _get_font()
            panel.render(font, rl.GetScreenWidth(), rl.GetScreenHeight(),
                         game_money=self.money,
                         unlocked_tech=self.unlocked_tech,
                         researching=self.tech_researching,
                         research_timer=self.tech_research_timer,
                         research_total=self.tech_research_total)

        # 渲染加工面板
        if self.craft_panel_open:
            self.ui.render_craft_panel(self)

        # 渲染武器库面板
        if self.weapon_panel_open:
            self.ui.render_weapon_panel(self)

        # 渲染刷怪点波次面板
        if self.spawner_panel_open:
            self.ui.render_spawner_panel(self)

        # 渲染游戏结束
        if self.state == GameState.GAME_OVER:
            self.ui.render_game_over(self)

        # 渲染存档菜单
        if self.state == GameState.SAVE_MENU:
            self.ui.render_save_menu(self)

        end_drawing()

    # ─── 存档管理 ────────────────────────────────────────────

    def _quick_save(self):
        """F5 快速存档（自动命名）"""
        path = save_system.save_game(self)
        self.toast(f"已存档: {os.path.basename(path)}")

    def _open_save_menu(self):
        """F8 打开存档管理菜单"""
        self.save_menu_items = save_system.list_saves()
        self.save_menu_index = 0
        self.save_menu_error = None
        self.save_menu_confirm_delete = None
        self.state = GameState.SAVE_MENU

    def _update_save_menu(self, dt):
        """存档菜单输入处理。↑↓选择, Enter新建/载入, Del删除, Esc/T关闭。"""
        # Esc/T 返回游戏
        if rl.IsKeyPressed(rl.KEY_ESCAPE) or rl.IsKeyPressed(rl.KEY_T):
            self.state = GameState.PLAYING
            return

        # ↑↓ 选择
        if rl.IsKeyPressed(rl.KEY_UP) or rl.IsKeyPressed(rl.KEY_W):
            if self.save_menu_index > 0:
                self.save_menu_index -= 1
        elif rl.IsKeyPressed(rl.KEY_DOWN) or rl.IsKeyPressed(rl.KEY_S):
            max_idx = 1 + len(self.save_menu_items)  # 0=新建, 1..n=存档
            if self.save_menu_index < max_idx:
                self.save_menu_index += 1

        # Enter 激活
        if rl.IsKeyPressed(rl.KEY_ENTER) or rl.IsKeyPressed(rl.KEY_KP_ENTER):
            if self.save_menu_index == 0:
                # 新建存档
                path = save_system.save_game(self)
                self.save_menu_items = save_system.list_saves()
                self.save_menu_error = None
                self.toast(f"已存档: {os.path.basename(path)}")
            elif self.save_menu_index <= len(self.save_menu_items):
                # 载入存档
                item = self.save_menu_items[self.save_menu_index - 1]
                print(f"[存档] 载入: {os.path.basename(item['path'])}")
                game = save_system.load_game(item["path"])
                if game:
                    self._pending_replace = game
                    self.toast("存档已加载")
                else:
                    self.save_menu_error = "加载失败"
            self.state = GameState.PLAYING
            return

        # Del 删除（二次确认）
        if rl.IsKeyPressed(rl.KEY_DELETE):
            if self.save_menu_confirm_delete is not None:
                path = self.save_menu_confirm_delete
                if save_system.delete_save(path):
                    self.toast("已删除")
                self.save_menu_confirm_delete = None
                self.save_menu_items = save_system.list_saves()
            elif 1 <= self.save_menu_index <= len(self.save_menu_items):
                self.save_menu_confirm_delete = self.save_menu_items[self.save_menu_index - 1]["path"]
            return

        # 鼠标点击
        self._handle_save_menu_click()

    def _handle_save_menu_click(self):
        """存档菜单鼠标处理：点击行选中并激活。"""
        mx, my = rl.GetMouseX(), rl.GetMouseY()
        sw, sh = rl.GetScreenWidth(), rl.GetScreenHeight()

        panel_w = 560
        row_h = 36
        row_gap = 6
        x = (sw - panel_w) // 2
        base_y = sh // 2 - 140

        total = 1 + len(self.save_menu_items)
        for i in range(total):
            y = base_y + i * (row_h + row_gap)
            if x <= mx <= x + panel_w and y <= my <= y + row_h:
                if rl.IsMouseButtonPressed(rl.MOUSE_BUTTON_LEFT):
                    self.save_menu_index = i
                    if i == 0:
                        path = save_system.save_game(self)
                        self.save_menu_items = save_system.list_saves()
                        self.toast(f"已存档: {os.path.basename(path)}")
                    elif self.save_menu_index <= len(self.save_menu_items):
                        item = self.save_menu_items[i - 1]
                        game = save_system.load_game(item["path"])
                        if game:
                            self._pending_replace = game
                            self.toast("存档已加载")
                            self.state = GameState.PLAYING
                        else:
                            self.save_menu_error = "加载失败"
                    return
