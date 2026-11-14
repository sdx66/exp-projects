"""
game.py - 主游戏循环与状态机
管理：游戏状态、波次系统、塔放置、敌人管理、输入处理
"""

import raylib as rl
import math
import os
import time
import config
import gamedata
import save_system
import settings
from entities import (Tower, Enemy, Pickup, Base, EnemyGrid,
                      TOWER_TYPES, ENEMY_TYPES, BaseResearchLane)
from items import (
    Inventory, MATERIAL_NAMES_CN,
    WEAPON_TYPES, TOOL_TYPES, PROCESS_RECIPES,
    can_process, process, can_craft_weapon, craft_weapon,
    can_craft_tool, craft_tool, dismantle_refund,
    CLASSIC_TOWER_MATERIALS, DISMANTLE_REFUND,
)
from blocks import BLOCK_ID, BLOCK_NAME, get_block_by_name, get_placeable_blocks
import belts as belts_mod
from belts import Belt, update_belts
import power as power_mod
from power import CableSegment, solve_power
from rl_utils import *
from ui_renderer import UIRenderer
from ui_tech_tree import tech_tree_panel
from ui_minimap import Minimap
import ui_config
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
    # L0 可达性守卫（D1a，瞬态不落存档）：当前不可达大本营的敌人类型。
    # 由 _spawn_enemy_for / reflight 维护；全部类型受阻时该点不出兵，
    # UI 显示"路径受阻"。读档后为空集，由出兵判定/本次会话重估重建。
    blocked_types: set = field(default_factory=set)


class GameState:
    """游戏状态机"""
    MENU = "menu"
    PLAYING = "playing"
    TECH_PANEL = "tech_panel"
    SAVE_MENU = "save_menu"
    GAME_OVER = "game_over"
    VICTORY = "victory"


def separate_enemies(enemies, universe, dt):
    """敌人间碰撞分离（对称推挤，地形感知）。

    只改 x/z，但目标格高于自身爬升阈值时取消该侧位移：旧实现仅按空间
    距离推挤，整排敌人会把前排挤进高墙格，随后 _sync_y 把身体抬到墙顶
    （“瞬移到墙顶”）。挖掘中的敌人固定不推，避免破坏挖掘会话基准面。
    """
    if len(enemies) <= 1:
        return
    sep_grid = EnemyGrid(enemies)
    radius = config.ENEMY_COLLIDE_RADIUS
    min_d = radius * 2.0
    min_d2 = min_d * min_d
    push = config.ENEMY_COLLIDE_STRENGTH * dt
    grid_query = sep_grid.query
    for enemy in enemies:
        if not enemy.active:
            continue
        ex, ez = enemy.position[0], enemy.position[2]
        for idx in grid_query(ex, ez, min_d):
            other = enemies[idx]
            if other is enemy or not other.active:
                continue
            op = other.position
            dx = ex - op[0]
            # |dx| > min_d ⇒ d² > min_d²，必然被下方 d2 >= min_d2 跳过
            if dx > min_d or -dx > min_d:
                continue
            dz = ez - op[2]
            d2 = dx * dx + dz * dz
            if d2 >= min_d2 or d2 < 1e-8:
                continue
            d = d2 ** 0.5
            overlap = (min_d - d) * 0.5 * push
            nx_ = dx / d
            nz_ = dz / d
            ex_now = enemy.position[0]
            ez_now = enemy.position[2]
            if enemy.can_step_aside_to(universe, ex_now + nx_ * overlap,
                                       ez_now + nz_ * overlap):
                enemy.position[0] = ex_now + nx_ * overlap
                enemy.position[2] = ez_now + nz_ * overlap
            opx = op[0]
            opz = op[2]
            if other.can_step_aside_to(universe, opx - nx_ * overlap,
                                       opz - nz_ * overlap):
                op[0] = opx - nx_ * overlap
                op[2] = opz - nz_ * overlap


class Game:
    """游戏主类"""

    def __init__(self, universe, camera, renderer):
        self.universe = universe
        self.camera = camera
        self.renderer = renderer
        self.ui = UIRenderer()

        # 小地图（右上角；构造时同步烘焙，摊入开局世界生成耗时）
        self.minimap = Minimap(universe)
        # 玩家设置（持久化 settings.json；跨对局生效，与种子/存档无关）
        self.settings = settings.load()
        self.minimap_visible = bool(self.settings["minimap_enabled"])

        # 设置面板（F10 打开；行选择 + 滑块编辑）
        self.settings_panel_open = False
        self.settings_row = 0          # 当前选中行（0..n-1）
        self._settings_rows = ("show_attack_routes", "minimap_enabled",
                               "route_opacity")

        # 游戏状态
        self.state = GameState.PLAYING
        self.money = config.STARTING_MONEY
        self.wave = 0
        self.score = 0

        # 大本营（逻辑位置与渲染锚点一致，均用纯生成高度——
        # get_surface_height 会被敌人挖掘 mark_passage 改写，导致基地"沉没"）
        base_y = universe._surface_height_pure(universe.base_x, universe.base_z)
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

        # 寻路管理器（变体缓存/拥挤/重算预算）+ 塔格 → 塔对象映射（敌人攻击目标查询）
        from pathfinding import Pathfinder
        self.pathfinder = Pathfinder(universe)
        self._towers_by_cell: dict = {}

        # 刷怪点运行时（独立波次，各点自治）
        self.spawners: dict = {}  # {spawner_id: SpawnerRuntime}
        self._spawner_hash: dict = {}  # {(hx, hz): [spawner_id, ...]} 空间哈希
        self._spawner_hash_dirty: bool = True
        # L0 reflight 记账：上次刷新 blocked_types 时的 world_version
        self._spawner_reach_version: int = -1
        self.wave = 0  # 全局阶段（= max active spawner wave_num），用于 UI 显示
        self.wave_announcement_timer = 0.0  # 最近一次激活公告
        self.wave_delay = 3.0  # 每点波次间隔（秒）

        # 塔放置
        self.selected_tower_type = None
        self.placing_tower = False
        self.placement_position = None
        # 开采塔待放足迹（m×n，3..7；[ / ] 调整）
        self.selected_mining_footprint = (3, 3)

        # 传送带放置
        self.belts: list = []            # list[Belt]
        self._belt_by_cell: dict = {}    # {(x,z): Belt}
        self.placing_belt = False
        self.selected_belt = None        # belt 类型名（belt/splitter/bridge）
        self.belt_direction = 1          # 放置方向（默认 E；R 旋转）

        # 电力（电线 + 电站/电池）
        self.wires: list = []            # list[CableSegment]
        self._wire_by_cell: dict = {}    # {(x,z): CableSegment}
        self.placing_wire = False
        self.selected_power = None       # 电力类型名（电线/电站/电池）
        self.power_kind_sel = "wire"     # "wire"|"station"|"battery"
        self.power_state = None          # 最近一次 solve_power 结果（UI 显示）
        # 电力塔选择（电站/电池）按类型名；电线有方向概念但电线无向，统一 R 不用。

        # 屏幕提示
        self.toast_message = None
        self.toast_timer = 0.0

        # 计时
        self.game_time = 0.0
        self.fps = 0.0
        self.frame_count = 0
        self._fps_window_start = time.perf_counter()

        # 游戏倍速（会话级偏好：1x/1.5x/2x/3x/4x/6x/8x，见 config.GAME_SPEEDS）。
        # 只缩放世界模拟（_update_playing 收到的 sim_dt），相机/UI 保持真实时间。
        self.time_scale = 1.0
        self._speed_index = 0

        # 科技（研究状态按塔存放，见 Tower.research_* 与 game.research_towers()；
        # station=false 的科技无需研究塔，走基地手研通道 base_research）
        self.unlocked_tech = set()
        self.base_research = BaseResearchLane()

        # 存档菜单
        self.save_menu_index = 0
        self.save_menu_items = []
        self.save_menu_scroll = 0
        self.save_menu_error = None
        self.save_menu_confirm_delete = None
        self._pending_replace = None

        # 材料/背包/交互
        self.inventory = Inventory()
        self.interaction_mode = "none"  # "none"|"place_classic"|"place_weapon"|"dismantle"|"unmount"|"dig"|"place"|"belt"|"power"
        self.selected_weapon = None
        self.selected_block_id = None   # 放置模式当前方块 ID（默认 dirt）
        self.dig_hold_timer = 0.0       # 连挖冷却计时
        self._terrain_edit_pending = False  # 本次地形编辑待批量 bump world_version
        self.craft_panel_open = False
        self.weapon_panel_open = False
        self.spawner_panel_open = False
        self.spawner_panel_page = 0
        self.storage_panel_open = False
        self.storage_tower = None   # 仓储窗口当前对应的塔
        self.processing_panel_open = False
        self.processing_tower = None  # 加工窗口当前对应的塔
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
        # 清空塔格注册（旧版残留会导致重开后路径永久绕行）
        for cell in list(self.universe._tower_cells):
            self.universe.remove_tower_cell(*cell)
        self._towers_by_cell = {}
        self.wave_delay = 3.0
        self.wave_announcement_timer = 0.0
        self.selected_tower_type = None
        self.placing_tower = False
        self.placement_position = None
        self.belts = []
        self._belt_by_cell = {}
        self.placing_belt = False
        self.selected_belt = None
        self.belt_direction = 1
        self.wires = []
        self._wire_by_cell = {}
        self.placing_wire = False
        self.selected_power = None
        self.power_kind_sel = "wire"
        self.power_state = None
        self.game_time = 0.0
        self.fps = 0.0
        self.frame_count = 0
        self._fps_window_start = time.perf_counter()
        # 会话级偏好：重开保持当前倍速（不随 _restart 重置回 1x）
        self.unlocked_tech = set()
        self.base_research = BaseResearchLane()
        # 重置屏幕提示，避免 GAME_OVER 重开后残留上个回合的 toast
        self.toast_message = None
        self.toast_timer = 0.0
        self._spawner_hash = {}
        self._spawner_hash_dirty = True
        self._init_spawners()
        self.inventory.reset()
        self.interaction_mode = "none"
        self.selected_weapon = None
        self.selected_block_id = None
        self.dig_hold_timer = 0.0
        self._terrain_edit_pending = False
        self.craft_panel_open = False
        self.weapon_panel_open = False
        self.spawner_panel_open = False
        self.spawner_panel_page = 0
        self.storage_panel_open = False
        self.storage_tower = None
        self.processing_panel_open = False
        self.processing_tower = None
        self.craft_timer = 0.0
        self.craft_action = None
        self.save_menu_index = 0
        self.save_menu_items = []
        self.save_menu_scroll = 0
        self.save_menu_error = None
        self.save_menu_confirm_delete = None
        self._pending_replace = None

    def _type_reachable(self, rt, type_name) -> bool:
        """该刷怪点×类型当前能否到达大本营（L0 守卫，同步维护阻塞集合）。

        结果与出兵判定同一口径（Pathfinder.reachability = 变体缓存非空），
        缓存后为 O(1) 查表；首次/版本变化后的重算受每帧预算摊帧。
        """
        ok = self.pathfinder.reachability(rt.spawner_id, type_name)
        if ok:
            rt.blocked_types.discard(type_name)
        else:
            rt.blocked_types.add(type_name)
        return ok

    def _spawn_enemy_for(self, rt):
        """为指定刷怪点生成一个波次敌人（按类型偏好从变体集中选路）。"""
        if rt.enemy_count >= rt.max_enemies:
            return

        # L0 守卫（D1a）：轮换顺序内跳过当前不可达的类型——不可达组合
        # 出生即"原地等待"（重寻路无解），不盲目出兵。所有类型都受阻时
        # 本 tick 不出（波次暂停，UI 标记"路径受阻"，地形变化自动恢复）。
        n_types = len(rt.enemy_types)
        type_name = None
        for i in range(n_types):
            cand = rt.enemy_types[(rt.enemy_count + i) % n_types]
            if self._type_reachable(rt, cand):
                type_name = cand
                break
        if type_name is None:
            return
        enemy_type = ENEMY_TYPES.get(type_name)
        if not enemy_type:
            return

        # 从寻路管理器取该刷怪点×类型的变体路径；无则兜底 legacy 噪声路径
        variants = self.pathfinder.get_variants(rt.spawner_id, type_name)
        path = None
        if variants:
            path = Enemy.pick_path(enemy_type, variants,
                                   self.pathfinder.path_occupancy,
                                   self.universe.rng)
        if path is None:
            if rt.spawner_id not in self.universe.enemy_paths:
                self.universe._ensure_path(rt.spawner_id)
            path = self.universe.enemy_paths.get(rt.spawner_id)
            if not path or not path.points:
                return
            variants = [path]

        # 路径点 Y 是平滑阶段的双线性插值，跨越高差时可能比真实列顶低/高
        # 数格（出生即埋在地下爬出，或从空中掉落）。出生 Y 必须以列顶为准。
        start = self.universe.spawn_point_for(path)
        wave = rt.wave_num

        eg = gamedata.ENEMY_GLOBAL
        health_mult = eg.get("health_growth", 1.08) ** (wave - 1)
        speed_mult = 1.0 + (wave - 1) * eg.get("speed_growth", 0.0)
        # R9-T3：奖励成长与血量/速度同为 wave-1 口径——原实现用 wave
        # （wave1 regular=11 而非 10，+10%），与相邻行不对称系笔误。
        # wave>=1 时 wave-1>=0，安全。
        reward = int(enemy_type.base_reward
                     * (1 + (wave - 1) * eg.get("reward_growth", 0.1)))

        enemy = Enemy(enemy_type, start, path, all_paths=variants,
                      rng=self.universe.rng)
        enemy.home_spawner_id = rt.spawner_id
        enemy.path_variant = variants.index(path)
        enemy.path_version = self.universe.world_version
        enemy.health = int(enemy.health * health_mult)
        enemy.max_health = enemy.health
        enemy.speed = enemy_type.base_speed * speed_mult
        enemy.reward = reward
        # #2 掉落波次口径：记录生成时所属刷怪点的 wave，供击杀掉落加成使用。
        # 掉落量与奖励/血量/速度必须同一波次口径（生成时 rt.wave_num），
        # 不能回落全局 self.wave（= max active spawner）——多刷怪点独立波次
        # 时，击杀低波次点的敌人掉落加成会被高波次点推高（口径脱节）。
        enemy.spawn_wave = wave
        self.enemies.append(enemy)
        rt.enemy_count += 1

    def _check_spawner_wave_complete(self, rt):
        """检查单个刷怪点的波次是否完成。"""
        if rt.enemy_count < rt.max_enemies:
            return
        # 统计仍在推进波次的敌人。侦察兵（scout_*）也带 home_spawner_id，若一并
        # 计入会让波次奖励/推进被残留侦察怪卡住（BUG-044）。但攻塔（attack_tower）
        # 与挖地（dig）的敌人仍存活且归属本刷怪点，若不计数会过早判波次完成、
        # 发奖励并让下一波与仍在围攻/挖地的残敌重叠（BUG-126）。
        still_active = any(
            e.active and e.home_spawner_id == rt.spawner_id
            and getattr(e, "behavior", "wave") in ("wave", "attack_tower", "dig")
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
        sp_hash = self._spawner_hash
        if not sp_hash:
            return []
        cell = 16
        r = int(math.ceil(radius / cell))
        hx, hz = int(math.floor(x)) // cell, int(math.floor(z)) // cell
        get = sp_hash.get
        result = []
        for dx in range(-r, r + 1):
            for dz in range(-r, r + 1):
                bucket = get((hx + dx, hz + dz))
                if bucket:
                    result.extend(bucket)
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

    def _register_edit(self, x, z):
        """挖/放方块事件：像造塔一样唤醒 edit 半径内的休眠刷怪点进入侦察。

        连挖/连放同一区域时，刷怪点被唤醒一次后即非 dormant，不会重复触发。
        """
        r = self._trigger_max_radius("edit")
        if self._spawner_hash_dirty:
            self._rebuild_spawner_hash()
        for sid in self._query_spawner_hash(x, z, r):
            rt = self.spawners.get(sid)
            if not rt or rt.mode != "dormant":
                continue
            sp = self.universe.spawners.get(sid)
            if sp is None:
                continue
            kr = self._kind_radius_of(sp, "edit")
            # 编辑点是格心 (x+0.5,z+0.5)，刷怪点中心是 (sp.x+0.5,sp.z+0.5)，
            # 故中心距 dx = sp.x - x（旧代码多算 +0.5，把格心当世界坐标，BUG-045）。
            dx = sp.x - x
            dz = sp.z - z
            if dx * dx + dz * dz <= kr * kr:
                self._enter_scouting(rt)
                self._spawner_hash_dirty = True

    def _notify_scouts_of_edit(self, x, z):
        """挖/放方块瞬间：edit_radius 内的侦察兵获得信息并立刻带消息回巢。

        游荡态直接 _start_return；回巢态只补标 has_message（到达即上报激活）。
        距离以编辑点 (x+0.5, z+0.5) 到侦察兵世界坐标为平方距离判定。
        """
        ex, ez = x + 0.5, z + 0.5
        for enemy in self.enemies:
            if not enemy.active:
                continue
            if enemy.behavior not in ("scout_wander", "scout_return"):
                continue
            cfg = getattr(enemy, "scout_cfg", None) or gamedata.STANDARD_SCOUT
            radius = float(cfg.get("edit_radius", 12.0))
            dx = enemy.position[0] - ex
            dz = enemy.position[2] - ez
            if dx * dx + dz * dz <= radius * radius:
                enemy.has_message = True
                if enemy.behavior == "scout_wander":
                    enemy._start_return(self.universe)

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

    def tower_cost(self, tower_type, footprint=None):
        """塔造价。开采塔按足迹面积缩放（3×3=基础价，越大越贵）。

        footprint=(m,n) 为开采塔待放足迹；其他塔忽略。
        """
        cost = tower_type.base_cost * self.build_cost_mult()
        if getattr(tower_type, "is_mining", False):
            m, n = footprint or self.selected_mining_footprint
            cost *= (int(m) * int(n)) / 9.0
        return int(cost)

    def research_towers(self):
        """所有研究塔（每座是一条独立研究通道）。"""
        return [t for t in self.towers if getattr(t, "is_research", False)]

    def _active_research(self):
        """全通道（研究塔 + 基地手研）当前在研科技 id 集合。"""
        out = {t.research_current for t in self.research_towers()
               if getattr(t, "research_current", None)}
        if getattr(self.base_research, "research_current", None):
            out.add(self.base_research.research_current)
        return out

    def _queued_research(self):
        """全通道（研究塔 + 基地手研）排队中科技 id 集合。"""
        out = set()
        for t in self.research_towers():
            out.update(getattr(t, "research_queue", ()) or ())
        out.update(getattr(self.base_research, "research_queue", ()) or ())
        return out

    def research_lanes(self):
        """研究通道快照（科技树面板/HUD/ETA 用）。

        研究塔每座一条通道；基地手研通道（tower=None）仅在已有内容
        （在研/排队/阻塞）时出现，避免空转常驻一行。
        """
        lanes = [t.lane_dict() for t in self.research_towers()]
        b = self.base_research
        if (getattr(b, "research_current", None)
                or getattr(b, "research_queue", None)
                or getattr(b, "research_blocked_reason", None)):
            lanes.append(b.lane_dict())
        return lanes

    def _tick_research(self, dt):
        """推进全部研究通道（每塔独立计时 + 基地手研，并行互不阻塞）。

        在研通道：递减计时，归零 → 完成并自动启动队首；
        空闲但有队首：尝试启动（前置失效 → 移除无效项并退款；缺材料/工具等
        站点条件 → 置 blocked_reason 逐帧重试）。
        读档后可能出现 current 为空而队列非空，同样由该分支恢复。
        """
        for tower in self.research_towers() + [self.base_research]:
            if tower.research_current:
                tower.research_timer -= dt
                if tower.research_timer <= 0:
                    self._complete_research(tower)
            elif tower.research_queue:
                self._start_next_in_queue(tower)

    def _complete_research(self, tower):
        """通道内当前研究完成：解锁 + 复位槽位 + 完成提示 + 启动队首。"""
        if not getattr(tower, "research_current", None):
            return
        import tech_tree as tt
        done = tower.research_current
        tower.research_current = None
        tower.research_timer = 0.0
        tower.research_total = 0.0
        self.unlocked_tech.add(done)
        self.toast(f"科技研究完成：{tt.tech_tree().node_name(done)}")
        self._start_next_in_queue(tower)

    def _station_satisfied(self, tech_id, tower=None):
        """检查站点的研究条件，返回 (ok, reason)。

        station=false（手研）：无需研究塔/学识/工具，但若该科技定义了
          mat_req（研发材料），需背包持有（检查不消耗，与塔内一致）。
        station=true 且 tower 给定 → 只查该塔；tower=None → 任意一座满足即可。
        条件：学识 ≥ attr_req，且塔内存有所需工具(tool_req)与材料(mat_req)。
        材料只检查不消耗（消耗留待后续）。
        """
        import tech_tree as tt
        tree = tt.tech_tree()
        if not tree.node_station(tech_id):
            # 手研：无塔/学识/工具要求；材料从背包检查
            _attr, _tool, mat_req = tree.node_requirements(tech_id)
            if mat_req and not all(
                    self.inventory.get_material(m) >= amt
                    for m, amt in mat_req.items()):
                parts = "、".join(
                    f"{MATERIAL_NAMES_CN.get(m, m)}x{n}" for m, n in mat_req.items())
                return False, f"需要材料: {parts}"
            return True, ""
        attr_req, tool_req, mat_req = tree.node_requirements(tech_id)
        candidates = [tower] if tower is not None else self.research_towers()
        for t in candidates:
            if not getattr(t, "is_research", False):
                continue
            if t.research_attribute() < attr_req:
                continue
            tools = t.storage.get("tools", ())
            if not all(tool in tools for tool in tool_req):
                continue
            mats = t.storage.get("materials", {})
            if not all(mats.get(mat, 0) >= amt for mat, amt in mat_req.items()):
                continue
            return True, ""
        # 收集缺项供提示（用已验证字库字符）
        parts = []
        if attr_req > 0:
            parts.append(f"学识≥{attr_req}")
        if tool_req:
            parts.append("工具:" + ",".join(TOOL_TYPES.get(x, x).label_cn if x in TOOL_TYPES else x
                                            for x in tool_req))
        if mat_req:
            parts.append("材料:" + ",".join(
                f"{MATERIAL_NAMES_CN.get(m, m)}x{n}" for m, n in mat_req.items()))
        return False, "需要研究塔(" + ";".join(parts) + ")"

    def _pick_research_tower(self, tech_id):
        """从满足 station 条件的研究塔中选目标塔（时间感知并行分摊）。

        优先级（最大化并行吞吐、降低 makespan）：
          1) 排除队列已满的塔（防误点连锁扣钱，与 _can_rehome 同上限）；
          2) 时间负载最小——空闲塔为 0 优先，忙塔按"在研剩余 + 队列预估时长"
             升序（按时间而非项目个数均分，塔越多收益越明显）；
          3) 时间相同时学识高者优先（兼顾后续高学识科技的 station 门槛）。
        手动点通道条指派目标塔的路径不受影响（tower 显式传入）。
        """
        best = None
        best_key = None
        for t in self.research_towers():
            if not self._station_satisfied(tech_id, t)[0]:
                continue
            if self._lane_load(t) >= config.RESEARCH_QUEUE_MAX:
                continue
            key = (self._lane_time_load(t), -t.research_attribute())
            if best_key is None or key < best_key:
                best, best_key = t, key
        return best

    def _lane_time_load(self, tower):
        """时间感知负载：在研项剩余时间 + 队列各项预估时长之和（秒）。

        仅用于 _pick_research_tower（选塔热路径）以最大化并行吞吐——按"项目
        个数"均分在塔多时会浪费空闲（5 塔实测 1.18x makespan，按时间 1.03x）。
        重排/队列上限仍用计数版 _lane_load（其收敛性证明依赖负载为整数）。
        """
        import tech_tree as tt
        nodes = tt.tech_tree().nodes
        total = 0.0
        if getattr(tower, "research_current", None):
            rem = getattr(tower, "research_timer", 0.0)
            total += rem if rem and rem > 0 else float(
                nodes.get(tower.research_current, {}).get("time", 0) or 0)
        for tid in (getattr(tower, "research_queue", ()) or ()):
            total += float(nodes.get(tid, {}).get("time", 0) or 0)
        return total

    def _find_research_lane(self, tech_id):
        """返回在研/排队该科技的研究通道（研究塔或基地手研；无则 None）。"""
        for t in self.research_towers():
            if (getattr(t, "research_current", None) == tech_id
                    or tech_id in (getattr(t, "research_queue", ()) or ())):
                return t
        b = self.base_research
        if (getattr(b, "research_current", None) == tech_id
                or tech_id in (getattr(b, "research_queue", ()) or ())):
            return b
        return None

    def _refund_tech(self, tech_id):
        """退还该科技研究费用（全额）。"""
        import tech_tree as tt
        cost = tt.tech_tree().nodes.get(tech_id, {}).get("cost", 0)
        self.money += int(cost)
        return int(cost)

    def _research_tech(self, tech_id, tower=None):
        """把科技加入研究通道队列；通道空闲则立即开始。

        station=false（无需研究塔）→ 基地手研通道 base_research，与塔无关；
        station=true → 研究塔队列（tower=None 自动分摊到空闲/最短队列塔）。
        入队即扣钱；取消/拆塔可退款。前置前瞻：要求 当前已解锁 ∪ 目标通道队列
        中更靠前的项（会先完成解锁）满足 requires，否则拒绝入队——防止错序
        导致队列前端永久阻塞。真正开始研究时再以"当下已解锁集合"复验（见
        _start_next_in_queue）。
        """
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
        # 已解锁 / 全局唯一（在研或已排队）
        if tech_id in self.unlocked_tech:
            self.toast("已解锁")
            return False
        if tech_id in self._active_research():
            self.toast("正在研究中")
            return False
        if tech_id in self._queued_research():
            self.toast("已在队列中")
            return False
        # 资金
        cost = node.get("cost", 0)
        if self.money < cost:
            self.toast(f"资金不足 (需要 {cost})")
            return False
        # 通道选择：station=false → 基地手研；否则研究塔
        station = tree.node_station(tech_id)
        if station:
            towers = self.research_towers()
            if tower is None:
                if not towers:
                    self.toast("需要一座研究塔")
                    return False
                tower = self._pick_research_tower(tech_id)
                if tower is None:
                    st_ok, st_reason = self._station_satisfied(tech_id)
                    self.toast(st_reason if not st_ok else "需要一座研究塔")
                    return False
            elif tower not in self.towers or not getattr(tower, "is_research", False):
                self.toast("需要一座研究塔")
                return False
            lane = tower
        else:
            # station=false：不占研究塔，忽略 tower 参数统一走基地手研
            lane = self.base_research
        # 前置前瞻（D9）：requires ⊆ unlocked ∪ {本通道队列更靠前的项 ∪ 当前在研项
        # （当前项先于一切队列项完成、会解锁其前置）}
        ahead = set(getattr(lane, "research_queue", ()) or ())
        if getattr(lane, "research_current", None):
            ahead.add(lane.research_current)
        missing = [r for r in (node.get("requires", []) or [])
                   if r not in self.unlocked_tech and r not in ahead]
        if missing:
            self.toast("需要前置: " + "、".join(tree.node_name(m) for m in missing[:3])
                       + "（请先解锁或先排入本通道队列）")
            return False
        # station 条件（学识/工具/材料）校验到目标通道（station=false 恒满足）
        st_ok, st_reason = self._station_satisfied(tech_id, lane)
        if not st_ok:
            self.toast(st_reason)
            return False
        # 队列上限（当前在研 + 排队总数，防误点连锁扣钱）
        total = len(lane.research_queue) + (1 if lane.research_current else 0)
        if total >= config.RESEARCH_QUEUE_MAX:
            self.toast(f"该研究通道队列已满 ({config.RESEARCH_QUEUE_MAX})")
            return False
        self.money -= int(cost)
        lane.research_queue.append(tech_id)
        if not getattr(lane, "research_current", None):
            self._start_next_in_queue(lane)
        return True

    def _start_next_in_queue(self, tower):
        """开始时刻校验队首（以当下 unlocked_tech 为准）并启动；失败分两类：

        前置缺失 → 若该前置正被"其它通道"在研/排队（会先完成解锁），保留为
          blocked "等待前置: X(正在Tn/手研)" 等待（逐帧重试自愈）；否则该项目
          已失效（前置被取消或数据异常，永远无法解锁）→ 移除该无效项并全额
          退款，继续处理下一个队首，直到队列清空或有项开工。
        station 条件不足（工具/材料被取走）→ blocked 具体原因，不丢弃不退款，
          逐帧重试（D6）。解锁集只增不减，配合入队前瞻，正常数据下开始时刻
          前置必然满足；本分支是异常数据/取消操作的兜底。
        """
        import tech_tree as tt
        tree = tt.tech_tree()
        tower.research_blocked_reason = None
        while tower.research_queue and not tower.research_current:
            front = tower.research_queue[0]
            missing = tree.missing_requires(front, self.unlocked_tech)
            if missing:
                # 缺失前置能否在"别的通道"推进？本通道队列里的不算：排在
                # 阻塞队首之后永远轮不到；当前项/更靠前项不存在（总是队首
                # 才会走到这里）。
                # 能推进 → 保留等待并标明在哪条通道（区别于"前置彻底无人
                # 研究"的死项，避免玩家误以为卡死）；否则 → 移除退款。
                waiting_src = None
                for r in missing:
                    lane = self._find_research_lane(r)
                    if lane is not None and lane is not tower:
                        waiting_src = lane
                        break
                if waiting_src is not None:
                    if waiting_src is self.base_research:
                        where = "手研"
                    elif waiting_src in self.research_towers():
                        where = "T%d" % (self.research_towers().index(waiting_src) + 1)
                    else:
                        where = "其他通道"
                    tower.research_blocked_reason = (
                        "等待前置: " + tree.node_name(waiting_src.research_current
                                                      or (waiting_src.research_queue[0]
                                                          if waiting_src.research_queue else "?"))
                        + f"(正在{where})")
                    return
                dropped = tower.research_queue.pop(0)
                self._refund_tech(dropped)
                self.toast(f"已移除并退款：{tree.node_name(dropped)}（前置未在研究）")
                continue
            st_ok, st_reason = self._station_satisfied(front, tower)
            if not st_ok:
                tower.research_blocked_reason = st_reason
                return
            tower.research_queue.pop(0)
            node = tree.nodes.get(front, {})
            tower.research_current = front
            tower.research_total = float(node.get("time", 0))
            tower.research_timer = tower.research_total

    def _cancel_research(self, tech_id=None, tower=None):
        """取消某科技的研究（在研或排队均可），全额退款。

        tech_id=None → 取消 tower 的当前在研项。返回是否取消成功。
        """
        import tech_tree as tt
        tree = tt.tech_tree()
        if tech_id is None:
            if tower is None or not getattr(tower, "research_current", None):
                return False
            tech_id = tower.research_current
            tower.research_current = None
            tower.research_timer = 0.0
            tower.research_total = 0.0
            self._refund_tech(tech_id)
            self._start_next_in_queue(tower)
            self.toast(f"已取消研究：{tree.node_name(tech_id)}（退款）")
            return True
        lane = self._find_research_lane(tech_id)
        if lane is None:
            return False
        if lane.research_current == tech_id:
            lane.research_current = None
            lane.research_timer = 0.0
            lane.research_total = 0.0
        else:
            try:
                lane.research_queue.remove(tech_id)
            except ValueError:
                return False
        self._refund_tech(tech_id)
        self._start_next_in_queue(lane)
        self.toast(f"已取消排队：{tree.node_name(tech_id)}（退款）")
        return True

    def _cancel_tower_research(self, tower, refund=True):
        """塔被拆/毁：取消该塔 current+queue 的全部研究（默认全额退款）。"""
        cancelled = []
        if getattr(tower, "research_current", None):
            cancelled.append(tower.research_current)
            tower.research_current = None
            tower.research_timer = 0.0
            tower.research_total = 0.0
        cancelled.extend(getattr(tower, "research_queue", ()) or ())
        tower.research_queue = []
        tower.research_blocked_reason = None
        if not cancelled:
            return
        if refund:
            for tid in cancelled:
                self._refund_tech(tid)
        import tech_tree as tt
        names = "、".join(tt.tech_tree().node_name(t) for t in cancelled[:3])
        if len(cancelled) > 3:
            names += f" 等{len(cancelled)}项"
        self.toast(f"研究中断：{names}" + ("（已退款）" if refund else ""))

    # ─── 研究队列重排（增建/拆除研究塔时） ──────────────────────

    def _lane_load(self, tower):
        """研究通道负载：在研 1 分 + 排队项数（与 _pick_research_tower 一致）。"""
        return ((1 if getattr(tower, "research_current", None) else 0)
                + len(getattr(tower, "research_queue", ()) or ()))

    def _can_rehome(self, tech_id, lane):
        """tech_id 可否迁入 lane：不超队列上限 + lane 满足站点条件 + 前置可满足。

        前置可满足 = requires ⊆ unlocked ∪ {lane 在研} ∪ lane 队列（队列为
        FIFO，排前面的项会先完成解锁）。满足后该项迁入必然可直接开工，
        不会因重排产生新的永久阻塞队列（跨通道的前置等待交给既有逐帧
        自动恢复机制兜底）。
        """
        if self._lane_load(lane) >= config.RESEARCH_QUEUE_MAX:
            return False
        if not self._station_satisfied(tech_id, lane)[0]:
            return False
        import tech_tree as tt
        node = tt.tech_tree().nodes.get(tech_id, {})
        requires = node.get("requires", []) or []
        if not requires:
            return True
        ahead = set(getattr(lane, "research_queue", ()) or ())
        if getattr(lane, "research_current", None):
            ahead.add(lane.research_current)
        return all(r in self.unlocked_tech or r in ahead for r in requires)

    def _donor_chain_ok(self, donor, index):
        """源通道链保护：donor.queue[index] 移走后，其后排队项不依赖它。"""
        import tech_tree as tt
        tree = tt.tech_tree()
        tid = donor.research_queue[index]
        for later in donor.research_queue[index + 1:]:
            if tid in (tree.nodes.get(later, {}).get("requires", []) or []):
                return False
        return True

    def _first_rehomeable(self, donor, acceptor):
        """donor 队首→队尾找第一个可迁往 acceptor 的排队项（受源链保护）。"""
        for i, tid in enumerate(donor.research_queue):
            if not self._can_rehome(tid, acceptor):
                continue
            if not self._donor_chain_ok(donor, i):
                continue
            return tid
        return None

    def _rebalance_research_lanes(self):
        """增建研究塔后调用：把排队项从负载重的塔摊到更轻的满足条件塔。

        只移动排队项（在研项不动、不打断源通道前置链）；要求目标塔负载
        至少比源通道轻 2（仅差 1 的移动不改变负载平方和
        ((la+1)²+(ld−1)²−la²−ld² = 2(la−ld)+2 = 0)，会在两塔之间来回搬运
        永不收敛——旧实现实测打满 10000 次 guard 仍不终止且发生在放塔的
        同一帧内）、满足站点条件、不超队列上限、前置可满足。每次移动都使
        各通道负载的平方和严格下降（≤ −2），必然终止，且最终各通道负载
        相差不超过 1（被前置链/站点条件阻止的情况除外；差 1 已算均衡）。
        结束后空转通道立即尝试启动队首（迁入即开工）。
        """
        lanes = self.research_towers()
        if len(lanes) < 2:
            return
        moved_any = False
        guard = 0
        while True:
            guard += 1
            if guard > 10000:
                break
            moved = False
            for donor in sorted(lanes, key=self._lane_load, reverse=True):
                if not donor.research_queue:
                    continue
                ld = self._lane_load(donor)
                for acceptor in sorted(lanes, key=self._lane_load):
                    if acceptor is donor:
                        continue
                    # 只迁往"至少轻 2"的通道（差 1 的移动平方和不下降，
                    # 会来回搬运永不收敛，见函数注释）。
                    if self._lane_load(acceptor) + 1 >= ld:
                        break
                    tid = self._first_rehomeable(donor, acceptor)
                    if tid is None:
                        continue
                    donor.research_queue.remove(tid)
                    acceptor.research_queue.append(tid)
                    moved = moved_any = True
                    break
                if moved:
                    break
            if not moved:
                break
        if moved_any:
            for lane in lanes:
                if not lane.research_current and lane.research_queue:
                    self._start_next_in_queue(lane)

    def _remove_research_lane(self, tower, refund=True):
        """研究塔被拆/毁：优先把其研究迁移到其它研究塔，无处承接才取消。

        在研项：按剩余时间迁到空闲（无在研）且满足站点条件、负载最轻的塔；
        排队项：逐个迁到负载最轻且满足站点条件的塔（依赖已迁出/在研/在队
        前置的项只可能落到同一条通道，前置链自动跟随）。
        迁移失败项按 refund 决定是否退款。返回 (migrated, cancelled)。
        """
        import tech_tree as tt
        tree = tt.tech_tree()
        lanes = [t for t in self.research_towers()
                 if t is not tower and getattr(t, "active", True)]
        migrated, cancelled = [], []
        # 1) 在研项：迁到空闲塔续跑（不打断其它在研项），保留剩余时间
        if getattr(tower, "research_current", None):
            cur = tower.research_current
            rem, tot = tower.research_timer, tower.research_total
            idle = [t for t in lanes if not getattr(t, "research_current", None)]
            home = None
            for t in sorted(idle, key=lambda t: (self._lane_load(t),
                                                 -t.research_attribute())):
                if self._station_satisfied(cur, t)[0]:
                    home = t
                    break
            if home is not None:
                home.research_current = cur
                home.research_timer = rem
                home.research_total = tot
                migrated.append(cur)
            else:
                cancelled.append(cur)
                if refund:
                    self._refund_tech(cur)
            tower.research_current = None
            tower.research_timer = 0.0
            tower.research_total = 0.0
        # 2) 排队项：FIFO 逐个迁移到负载最轻的满足条件塔
        for tid in list(getattr(tower, "research_queue", ()) or ()):
            home = None
            for t in sorted(lanes, key=lambda t: (self._lane_load(t),
                                                  -t.research_attribute())):
                if self._can_rehome(tid, t):
                    home = t
                    break
            if home is None:
                cancelled.append(tid)
                if refund:
                    self._refund_tech(tid)
            else:
                home.research_queue.append(tid)
                migrated.append(tid)
        tower.research_queue = []
        tower.research_blocked_reason = None
        # 3) 空转目标塔立即尝试启动队首（迁入即开工）
        for lane in lanes:
            if not getattr(lane, "research_current", None) and lane.research_queue:
                self._start_next_in_queue(lane)
        # 4) 提示
        if migrated:
            names = "、".join(tree.node_name(t) for t in migrated[:3])
            if len(migrated) > 3:
                names += f" 等{len(migrated)}项"
            self.toast(f"研究迁移：{names}（已移至其他研究塔）")
        if cancelled:
            names = "、".join(tree.node_name(t) for t in cancelled[:3])
            if len(cancelled) > 3:
                names += f" 等{len(cancelled)}项"
            self.toast(f"研究中断：{names}" + ("（已退款）" if refund else ""))
        return migrated, cancelled

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
        # #2 掉落波次口径：加成用击杀者生成时所属刷怪点的 wave（与 reward/
        # 血量/速度同源），而非全局 self.wave——多刷怪点独立波次时按各自点
        # 的 wave 结算，避免低波次点的击杀被高波次点推高掉落（口径一致）。
        wave = getattr(enemy, "spawn_wave", 0) or self.wave
        amount = max(1, drop[1] + wave // 10)
        pickup = Pickup(
            position=(enemy.position[0], enemy.position[1] + 0.3, enemy.position[2]),
            item_type="material",
            item_name=drop[0],
            amount=amount,
            rng=self.universe.rng,
        )
        self.pickups.append(pickup)

    def update(self, dt):
        """更新游戏逻辑

        倍速：仅缩放世界模拟时间（sim_dt = dt × time_scale）——`_update_playing`
        内部所有计时（波次/塔/敌人/抛射物/传送带/电力/研究/背包冷却）都消费传入
        的 dt，故只改这一处即可整体缩放。相机与 UI 仍用真实 dt（操作时间），
        保证输入跟手性不随倍速变化。暂停态（科技面板/存档菜单）世界与研究推进
        完全冻结（面板内不 tick 研究）。
        """
        sim_dt = dt * self.time_scale
        self.game_time += sim_dt

        # 先更新相机再处理本帧输入：点击/悬停取格与渲染的幽灵预览共用
        # 同一份相机状态，缩放/旋转平滑期间不再"预览绿、点击落偏一格"。
        # （科技树/存档菜单打开时均暂停——R10-U1：SAVE_MENU 期间相机继续
        # 推进并接受输入会与存档菜单导航键 W/S/↑/↓ 同帧碰撞、菜单背后世界
        # 可见漂移，回 PLAYING 后相机已偏位。两伪暂停态统一冻结相机。）
        if (self.state != GameState.TECH_PANEL
                and self.state != GameState.SAVE_MENU):
            self.camera.update(dt)

        if self.state == GameState.PLAYING:
            self._update_playing(sim_dt)
        elif self.state == GameState.TECH_PANEL:
            self._update_tech_panel(dt)
        elif self.state == GameState.SAVE_MENU:
            self._update_save_menu(dt)
        elif self.state == GameState.GAME_OVER:
            # 按 Enter 重启
            if rl.IsKeyPressed(rl.KEY_ENTER):
                self._restart()

        # FPS 计数：按真实墙钟时间统计，不能使用被 main 限制过的 dt，
        # 否则首帧长时间生成地形会被少算，显示值会虚高。
        self.frame_count += 1
        now = time.perf_counter()
        elapsed = now - self._fps_window_start
        if elapsed >= config.FPS_UPDATE_INTERVAL:
            self.fps = self.frame_count / elapsed
            self.frame_count = 0
            self._fps_window_start = now

        # 更新天气（R9-T4：SAVE_MENU 期间天气继续推进会让菜单背后无声切换
        # 天气、恢复 PLAYING 后敌人按新天气取速度倍率——玩家未"经历"该转变
        # 却被带入。与 TECH_PANEL 一样冻结，两伪暂停态口径一致。）
        if (self.state != GameState.TECH_PANEL
                and self.state != GameState.SAVE_MENU):
            self.universe.update_weather(sim_dt)

    def _cycle_time_scale(self, delta=1):
        """切换游戏倍速挡位（循环：8x → 下一档回 1x）。

        只改世界模拟倍率（见 update），对游戏内容/存档无副作用；重开游戏
        （_restart）保持当前挡位（会话级偏好）。
        """
        n = len(config.GAME_SPEEDS)
        self._speed_index = (self._speed_index + delta) % n
        self.time_scale = config.GAME_SPEEDS[self._speed_index]
        label = f"{self.time_scale:g}x"
        self.toast(f"倍速: {label}")

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
        # ── 寻路帧预算 + 占用快照 ──
        self.pathfinder.begin_frame()
        occ = {}
        po = {}
        for e in self.enemies:
            if not e.active:
                continue
            key = (math.floor(e.position[0]), math.floor(e.position[2]))
            occ[key] = occ.get(key, 0) + 1
            if e.behavior == "wave" and e.path is not None:
                pk = id(e.path)
                po[pk] = po.get(pk, 0) + 1
        self.pathfinder.occupancy = occ
        self.pathfinder.path_occupancy = po
        # 塔格 → 塔对象映射（敌人攻击目标查询用）。开采塔按足迹映射（敌人
        # 攻击任一格即锁定该塔），其余塔单格。
        self._towers_by_cell = {}
        for t in self.towers:
            if not t.active:
                continue
            if getattr(t, "is_mining", False):
                for (cx, cz) in t.footprint_cells():
                    self._towers_by_cell[(cx, cz)] = t
            else:
                self._towers_by_cell[(math.floor(t.position[0]),
                                      math.floor(t.position[2]))] = t

        # ── 同步新材料化刷怪点 ──
        self._sync_materialized_spawners()

        # ── L0 reflight（D1a）：世界版本变化后刷新受阻刷怪点的可达性 ──
        # 玩家拆墙/挖通新路线后，被标记 blocked 的类型在此自动解除
        # （reachability 缓存已随版本失效，重算受每帧预算 ≤1 次摊帧，
        # 多个受阻点会在后续帧内陆续刷新，UI 状态随之更新）。
        if (self._spawner_reach_version != self.universe.world_version):
            self._spawner_reach_version = self.universe.world_version
            for rt in self.spawners.values():
                if rt.mode == "active" and rt.blocked_types:
                    for t in list(rt.blocked_types):
                        self._type_reachable(rt, t)

        # ── 刷怪点管理 ──
        # 激活点：各自独立波次
        for rt in self.spawners.values():
            if rt.mode != "active":
                continue
            if rt.between_wave:
                rt.between_timer -= dt
                if rt.between_timer <= 0:
                    self._init_spawner_wave(rt, rt.wave_num + 1)
                    # 已激活刷怪点推进波次时刷新全局阶段（否则 self.wave 冻结）
                    self._update_global_phase()
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
        sp_hash = self._spawner_hash
        if sp_hash:
            # 帧内按敌人格哈希键记忆查询结果：本循环期间 sp_hash 不被修改
            #（_enter_scouting 只置脏标记，重建发生在下一帧开头），
            # 同格敌人共享一次窗口扫描。sid 列表只读，dormant 过滤在下方照旧
            # 逐敌执行（先触发者改掉 rt.mode 后，同格后续敌人不再命中）。
            cell = 16
            r = int(math.ceil(presence_r / cell))
            floor = math.floor
            hget = sp_hash.get
            qmemo = {}
            mget = qmemo.get
            for enemy in self.enemies:
                if not enemy.active:
                    continue
                if enemy.behavior != "wave":
                    continue  # 侦察兵/侦察回巢不算触发
                ex, ez = enemy.position[0], enemy.position[2]
                hx = int(floor(ex)) // cell
                hz = int(floor(ez)) // cell
                ck = (hx, hz)
                near = mget(ck)
                if near is None:
                    near = []
                    for dx in range(-r, r + 1):
                        for dz in range(-r, r + 1):
                            bucket = hget((hx + dx, hz + dz))
                            if bucket:
                                near.extend(bucket)
                    qmemo[ck] = near
                for sid in near:
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
                    # R8-Q1：出生 Y 用实时 surface_height_at（覆盖层感知）而非
                    # 材料化时的 surface_y 快照——否则材料化后挖低/垫高该处
                    # 地形，侦察怪出生 Y 陈旧（靠 _sync_y 每帧 ~12/s 纠正），
                    # 与波次敌实时出生路径不一致。
                    scout = Enemy.as_scout(
                        scout_type,
                        (sp.x + 0.5,
                         self.universe.surface_height_at(sp.x, sp.z) + 1,
                         sp.z + 0.5),
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
                    self.wave_announcement_timer = 3.0  # 全局公告计时（独立单次递减）
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
        # Tower.update 返回列表：防御塔挂载多把武器时同帧可各发一弹（攻击叠加）
        new_projectiles = []
        tower_grid = EnemyGrid(self.enemies) if self.towers else None
        for tower in self.towers:
            projs = tower.update(dt, self.enemies, tower_grid,
                                 universe=self.universe)
            if projs:
                new_projectiles.extend(projs)
                self._register_combat(tower.position[0], tower.position[2])
        self.projectiles.extend(new_projectiles)

        # ── 更新传送带物流（段间转运 + 塔↔带） ──
        if self.belts:
            update_belts(dt, self.belts, self.towers)

        # ── 更新电力系统（发电/输电/储能；燃料已由传送带/背包送达） ──
        if self.wires:
            self.power_state = solve_power(dt, self.wires, self.towers,
                                           demand_w=0.0, universe=self.universe)
        else:
            self.power_state = None

        # ── 更新敌人（传 towers/base 供侦察怪感知；塔格映射与寻路供 wave 态） ──
        for enemy in self.enemies:
            result = enemy.update(dt, self.universe,
                                  towers=self.towers, base=self.base,
                                  towers_by_cell=self._towers_by_cell,
                                  pathfinder=self.pathfinder)
            if result == "reached_base":
                killed = self.base.take_damage(gamedata.ENEMY_GLOBAL.get("base_damage", 5))
                if killed:
                    self.state = GameState.GAME_OVER
                    return

        # ── 敌人间碰撞分离（地形感知，对称推挤，基于空间哈希） ──
        separate_enemies(self.enemies, self.universe, dt)

        # ── 被摧毁的塔：研究优先迁往其它研究塔，无处承接才取消（退款）再注销塔格、移除 ──
        destroyed = [t for t in self.towers if not t.active]
        if destroyed:
            for t in destroyed:
                self._remove_research_lane(t, refund=True)
                if getattr(t, "is_mining", False):
                    for (cx, cz) in t.footprint_cells():
                        self.universe.remove_tower_cell(cx, cz)
                else:
                    self.universe.remove_tower_cell(math.floor(t.position[0]),
                                                    math.floor(t.position[2]))
            self.towers = [t for t in self.towers if t.active]
            self.toast("一座塔被摧毁了！")

        # ── 侦察兵遇到正式兵获取信息（侦察兵获得信息回巢，正式兵不受影响） ──
        # 常见情形（无侦察兵）只做一次无分配的扫描
        scouts = []
        for e in self.enemies:
            if e.active and e.behavior == "scout_wander":
                scouts.append(e)
        if scouts:
            waves = [e for e in self.enemies
                     if e.active and e.behavior == "wave"]
            if waves:
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
            proj_grid = EnemyGrid(self.enemies)
            active_projectiles = []
            for proj in self.projectiles:
                if not proj.active:
                    continue
                result = proj.update(dt, self.universe, self.enemies, proj_grid)
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
                # 死亡/移除时若仍在挖掘，统一结束会话（削过格则 bump 版本）
                enemy.on_removed(self.universe)
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
                            self.wave_announcement_timer = 3.0  # 全局公告计时（独立单次递减）
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

        # 科技研究（按研究塔通道逐帧计时；塔之间并行互不阻塞）
        self._tick_research(dt)

        # 连挖冷却计时
        if self.dig_hold_timer > 0:
            self.dig_hold_timer -= dt

        # 输入处理
        self._handle_input(dt)

        # 地形编辑批量推进世界版本（一次会话只 bump 一次，寻路变体惰性重估）
        if self._terrain_edit_pending:
            self.universe.bump_world_version()
            self._terrain_edit_pending = False

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

        # 面板打开时游戏完全冻结：世界与研究推进都暂停（状态快照，切走即停）

    def _handle_input(self, dt):
        """处理输入"""
        # 设置面板打开时（F10）：独占键盘，只处理面板输入
        if self.settings_panel_open:
            self._update_settings_panel()
            return

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

        # 武器库面板打开时（H 键打开，点击选择）
        if self.weapon_panel_open:
            if rl.IsKeyPressed(rl.KEY_H) or rl.IsKeyPressed(rl.KEY_ESCAPE):
                self.weapon_panel_open = False
                return
            if rl.IsMouseButtonPressed(rl.MOUSE_BUTTON_LEFT):
                wname = self.ui.weapon_hit_action(self, rl.GetMouseX(), rl.GetMouseY())
                if wname:
                    self.weapon_panel_open = False
                    self._select_weapon(wname)
                return
            return

        # 仓储面板打开时（点击仓储塔打开；E/Esc/J 关闭，左键 1 个右键全部）
        if self.storage_panel_open:
            # 仓储塔已不在场上（被拆/被毁）→ 关窗
            if self.storage_tower is None or self.storage_tower not in self.towers:
                self._close_storage_panel()
                return
            # 关窗键 V→J：原 V 已是相机 2D/3D 切换，按 V 关窗会误触相机翻转（R6-003）。
            # E 已让给相机偏航（R9-T1），关窗只保留 Esc/J。
            if (rl.IsKeyPressed(rl.KEY_ESCAPE)
                    or rl.IsKeyPressed(rl.KEY_J)):
                self._close_storage_panel()
                return
            hx, hy = rl.GetMouseX(), rl.GetMouseY()
            hit = self.ui.storage_hit_action(self, hx, hy)
            if rl.IsMouseButtonPressed(rl.MOUSE_BUTTON_LEFT):
                if hit:
                    self._storage_panel_action(*hit, count=1)
                else:
                    # 点在面板外：关窗
                    bx, by, bw, bh = self.ui.storage_panel_rect(self)
                    if not (bx <= hx <= bx + bw and by <= hy <= by + bh):
                        self._close_storage_panel()
                return
            if rl.IsMouseButtonPressed(rl.MOUSE_BUTTON_RIGHT) and hit:
                self._storage_panel_action(*hit, count="all")
            return

        # 加工面板打开时（点击加工塔打开；E/Esc/G 关闭，左键 1 个右键全部）
        if self.processing_panel_open:
            # 加工塔已不在场上（被拆/被毁）→ 关窗
            if self.processing_tower is None or self.processing_tower not in self.towers:
                self._close_processing_panel()
                return
            # 加工面板关窗：Esc/G（E 已让给相机偏航，R9-T1）。
            if (rl.IsKeyPressed(rl.KEY_ESCAPE)
                    or rl.IsKeyPressed(rl.KEY_G)):
                self._close_processing_panel()
                return
            hx, hy = rl.GetMouseX(), rl.GetMouseY()
            hit = self.ui.processing_hit_action(self, hx, hy)
            if rl.IsMouseButtonPressed(rl.MOUSE_BUTTON_LEFT):
                if hit:
                    self._processing_panel_action(*hit, count=1)
                else:
                    # 点在面板外：关窗
                    bx, by, bw, bh = self.ui.processing_panel_rect(self)
                    if not (bx <= hx <= bx + bw and by <= hy <= by + bh):
                        self._close_processing_panel()
                return
            if rl.IsMouseButtonPressed(rl.MOUSE_BUTTON_RIGHT) and hit:
                self._processing_panel_action(*hit, count="all")
            return

        # T 键打开科技树面板（面板打开时再按 T 关闭，见 _update_tech_panel）
        if rl.IsKeyPressed(rl.KEY_T) and self.state == GameState.PLAYING:
            tech_tree_panel(self).open_panel()
            self.state = GameState.TECH_PANEL

        # F5/F8: 存档快捷键（仅游戏中）；F10: 设置面板
        if self.state == GameState.PLAYING:
            if rl.IsKeyPressed(rl.KEY_F5):
                self._quick_save()
            elif rl.IsKeyPressed(rl.KEY_F8):
                self._open_save_menu()
            elif rl.IsKeyPressed(rl.KEY_F10):
                self._open_settings_panel()
            elif rl.IsKeyPressed(rl.KEY_SEMICOLON):
                # 下一档倍速（; 全库未占用，且避开相机/挖矿足迹独占分支）
                self._cycle_time_scale(+1)
            elif rl.IsKeyPressed(rl.KEY_COMMA):
                # 上一档倍速
                self._cycle_time_scale(-1)

        # 数字键: 游戏内选择塔类型
        if self.state == GameState.PLAYING:
            # 电力模式：1/2/... 选电力类型（电线/电站/电池），Esc 退出（独占键盘）
            if self.interaction_mode == "power":
                _opts = self._power_options()
                for i in range(min(len(_opts), 9)):
                    if rl.IsKeyPressed(rl.KEY_ONE + i) and i < len(_opts):
                        self._select_power(_opts[i])
                        break
                else:
                    # Esc 或 Y 退出电力模式（原 E 被本分支遮蔽，只能进不能出
                    # BUG-035；E 已让给相机左偏航，R9-T1）
                    if rl.IsKeyPressed(rl.KEY_ESCAPE) or rl.IsKeyPressed(rl.KEY_Y):
                        self._toggle_power_mode()
            # 布带模式：1/2/3 选带型，R 旋转，K/Esc 退出（独占键盘）
            elif self.interaction_mode == "belt":
                if rl.IsKeyPressed(rl.KEY_ONE):
                    self._select_belt("belt")
                elif rl.IsKeyPressed(rl.KEY_TWO):
                    self._select_belt("splitter")
                elif rl.IsKeyPressed(rl.KEY_THREE):
                    self._select_belt("bridge")
                elif rl.IsKeyPressed(rl.KEY_R):
                    self.belt_direction = (self.belt_direction + 1) % 4
                elif rl.IsKeyPressed(rl.KEY_K) or rl.IsKeyPressed(rl.KEY_ESCAPE):
                    self._toggle_belt_mode()
            # 放置模式下：1-4 选方块（泥土/石头/沙子/原木）
            elif self.interaction_mode == "place":
                _placeable = get_placeable_blocks()
                if rl.IsKeyPressed(rl.KEY_ONE) and len(_placeable) > 0:
                    self._select_block(_placeable[0].id)
                elif rl.IsKeyPressed(rl.KEY_TWO) and len(_placeable) > 1:
                    self._select_block(_placeable[1].id)
                elif rl.IsKeyPressed(rl.KEY_THREE) and len(_placeable) > 2:
                    self._select_block(_placeable[2].id)
                elif rl.IsKeyPressed(rl.KEY_FOUR) and len(_placeable) > 3:
                    self._select_block(_placeable[3].id)
                # F/Esc 退出放置模式（原分支独占键盘，F/Esc 被遮蔽无法退出 BUG-009）
                elif rl.IsKeyPressed(rl.KEY_F) or rl.IsKeyPressed(rl.KEY_ESCAPE):
                    self._toggle_place()
            # 开采塔放置中：[/] 调宽(米)、[-/=] 调长(高)，范围 3..7
            elif (self.placing_tower and self.selected_tower_type
                    and getattr(self.selected_tower_type, "is_mining", False)):
                _fmin = int(self.selected_tower_type.mining_attr("footprint_min", 3))
                _fmax = int(self.selected_tower_type.mining_attr("footprint_max", 7))
                m, n = self.selected_mining_footprint
                _changed = False
                if rl.IsKeyPressed(rl.KEY_ESCAPE):
                    # 开采塔放置分支独占键盘，Esc 原被遮蔽无法取消（BUG-036）
                    self.placing_tower = False
                    self.selected_tower_type = None
                elif rl.IsKeyPressed(rl.KEY_LEFT_BRACKET):
                    m = m - 1 if m > _fmin else _fmax
                    _changed = True
                elif rl.IsKeyPressed(rl.KEY_RIGHT_BRACKET):
                    m = m + 1 if m < _fmax else _fmin
                    _changed = True
                elif rl.IsKeyPressed(rl.KEY_MINUS):
                    n = n - 1 if n > _fmin else _fmax
                    _changed = True
                elif rl.IsKeyPressed(rl.KEY_EQUAL):
                    n = n + 1 if n < _fmax else _fmin
                    _changed = True
                if _changed:
                    self.selected_mining_footprint = (m, n)
                    self.toast(f"开采塔足迹 {m}×{n} "
                               f"(${self.tower_cost(self.selected_tower_type)})")
            # 数字键: 选择经典塔
            elif rl.IsKeyPressed(rl.KEY_ONE):
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
            # 9/0: 选择防御塔（无攻击，可挂载武器）
            elif rl.IsKeyPressed(rl.KEY_NINE):
                self._select_tower_type("palisade_wall")
            elif rl.IsKeyPressed(rl.KEY_ZERO):
                self._select_tower_type("city_wall")
            # .（句点）: 选择星堡（无攻击要塞；原仅鼠标点选，补回键盘热键 R9-004）
            elif rl.IsKeyPressed(rl.KEY_PERIOD):
                self._select_tower_type("star_fort")
            # J: 选择仓储塔（无攻击，固定容量仓库）。
            # 原 V 与相机 2D/3D 切换冲突，改用 J（R6-003）
            elif rl.IsKeyPressed(rl.KEY_J):
                self._select_tower_type("storage_tower")
            # G: 选择加工塔（无攻击，放入物品随时间加工）
            elif rl.IsKeyPressed(rl.KEY_G):
                self._select_tower_type("processing_tower")
            # L: 选择研究塔（station 科技的研究条件塔；无科技前置）。
            # 原 S 与相机后退（WASD）冲突，改用 L（BUG-081）
            elif rl.IsKeyPressed(rl.KEY_L):
                self._select_tower_type("research_tower")
            # N: 选择开采塔（竖井采掘，可挂工具/武器；需 steam:sE11）。
            # 原 D 与相机右移（WASD）冲突，改用 N（BUG-081）
            elif rl.IsKeyPressed(rl.KEY_N):
                self._select_tower_type("mining_tower")
            # B 键: 打开加工/合成面板
            elif rl.IsKeyPressed(rl.KEY_B):
                self._toggle_craft_panel()
            # H 键: 打开武器库（选择武器装备放置；C 已让给相机下潜）
            elif rl.IsKeyPressed(rl.KEY_H):
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
            # U 键: 切换收起武器模式（把防御塔上挂着的武器取回背包）
            elif rl.IsKeyPressed(rl.KEY_U):
                self._toggle_unmount()
            # R 键: 切换挖掘模式
            elif rl.IsKeyPressed(rl.KEY_R):
                self._toggle_dig()
            # F 键: 切换放置模式
            elif rl.IsKeyPressed(rl.KEY_F):
                self._toggle_place()
            # K 键: 切换布带模式
            elif rl.IsKeyPressed(rl.KEY_K):
                self._toggle_belt_mode()
            # Y 键: 切换电力模式（电线/电站/电池）。
            # 原 E 与相机左偏航 IsKeyDown(KEY_E) 双重绑定——按 E 进/出电力
            # 模式的同时视线左转（按住连续偏航），违反 ui_config.py 自述
            # 不变量"热键须避开相机 Q/E 偏航"（R9-T1）。E 改为相机独占。
            elif rl.IsKeyPressed(rl.KEY_Y):
                self._toggle_power_mode()
            # M 键: 开关小地图
            elif rl.IsKeyPressed(rl.KEY_M):
                self.minimap_visible = not self.minimap_visible
            elif rl.IsKeyPressed(rl.KEY_ESCAPE):
                self.placing_tower = False
                self.selected_tower_type = None
                self.interaction_mode = "none"
                self.selected_weapon = None

            # 鼠标点击: 优先级链（布带 → 塔栏 → 小地图 → 挖掘 → 放置 → 拆除 → 收起武器 → 放置塔 → 武器 → 拾取）
            if rl.IsMouseButtonPressed(rl.MOUSE_BUTTON_LEFT):
                # 顶栏倍速分段优先（顶栏点击原本不参与世界交互，插在最前安全）
                if self.ui.speed_bar_hit(rl.GetMouseX(), rl.GetMouseY()):
                    self._cycle_time_scale(+1)
                elif self.interaction_mode == "power":
                    # 电力类型选择栏点击优先；小地图点击仅跳转相机（BUG-037）；
                    # 否则放置当前选中电力类型
                    pname = self.ui.power_bar_hit(rl.GetMouseX(), rl.GetMouseY())
                    if pname:
                        self._select_power(pname)
                    elif self.minimap_visible and self.minimap.handle_click(
                            self, rl.GetMouseX(), rl.GetMouseY()):
                        pass
                    else:
                        self._try_place_power()
                elif self.interaction_mode == "belt":
                    # 带型选择栏点击优先；小地图点击仅跳转相机（BUG-037）；否则放带
                    bname = self.ui.belt_bar_hit(rl.GetMouseX(), rl.GetMouseY())
                    if bname:
                        self._select_belt(bname)
                    elif self.minimap_visible and self.minimap.handle_click(
                            self, rl.GetMouseX(), rl.GetMouseY()):
                        pass
                    else:
                        self._try_place_belt()
                else:
                    # 塔选择栏优先：点中分段按钮即选中该塔类型（含无热键的星形要塞）
                    tname = self.ui.tower_hit_type(rl.GetMouseX(), rl.GetMouseY())
                    if tname:
                        self._select_tower_type(tname)
                    elif self.minimap_visible and self.minimap.handle_click(
                            self, rl.GetMouseX(), rl.GetMouseY()):
                        # 点击落在小地图上：仅跳转相机。同帧置连挖冷却，
                        # 避免下方 hold 分支在按下帧挖到光标下世界格（BUG-038）。
                        self.dig_hold_timer = config.PLAYER_DIG_COOLDOWN
                    elif self.interaction_mode == "dig":
                        self._try_dig_block()
                        # 点击帧同步启动连挖冷却，避免同帧 hold 分支再挖一次
                        # （IsMouseButtonDown && dig_hold_timer<=0 会在按下同帧触发）。
                        self.dig_hold_timer = config.PLAYER_DIG_COOLDOWN
                    elif self.interaction_mode == "place":
                        # 方块选择栏点击优先；否则放方块
                        bname = self.ui.block_bar_hit(rl.GetMouseX(), rl.GetMouseY())
                        if bname:
                            self._select_block(bname)
                        else:
                            self._try_place_block()
                    elif self.interaction_mode == "dismantle":
                        self._try_dismantle_tower()
                    elif self.interaction_mode == "unmount":
                        self._try_unmount_weapon_tower()
                    elif self.placing_tower and self.selected_tower_type:
                        self._try_place_tower()
                    elif self.selected_weapon:
                        self._try_place_weapon_tower()
                    else:
                        # 默认: 拾取 → 加工塔开窗 → 仓储塔开窗 → 升级塔 → 采集道具
                        # 拾取优先:掉落物常落在塔脚下,先拾取可避免被升级逻辑截胡
                        picked = self._try_pickup()
                        # Shift+点击 = 强制升级：跳过拾取/加工开窗/仓储开窗，
                        # 让加工塔/仓储塔（默认点击会开面板，平时升不了级）也能升级。
                        if self._shift_down():
                            self._try_upgrade_tower()
                        elif not picked:
                            if self._try_open_processing():
                                pass
                            elif self._try_open_storage():
                                pass
                            else:
                                upgraded = self._try_upgrade_tower()
                                if not upgraded:
                                    if not self._try_rotate_belt():
                                        self._try_harvest_prop()

            # 按住左键连挖（dig 模式）；光标在小地图上时不挖（同 BUG-038 的
            # 按下帧守卫，避免按住小地图挖到其下世界格）（BUG-107）。
            if (self.interaction_mode == "dig"
                    and rl.IsMouseButtonDown(rl.MOUSE_BUTTON_LEFT)
                    and not self._cursor_on_minimap()
                    and self.dig_hold_timer <= 0.0):
                self._try_dig_block()
                self.dig_hold_timer = config.PLAYER_DIG_COOLDOWN

    def _cursor_on_minimap(self):
        """光标是否落在小地图矩形内（仅在小地图可见时可能为 True）。

        与 ui_minimap.Minimap.handle_click 的命中判定一致：
        get_rect() 返回 (mx, my, size, _)，矩形为 [mx, mx+size) × [my, my+size)。
        """
        if not self.minimap_visible:
            return False
        mx, my, size, _ = self.minimap.get_rect()
        mouse_x = rl.GetMouseX()
        mouse_y = rl.GetMouseY()
        return mx <= mouse_x < mx + size and my <= mouse_y < my + size

    def _tower_at_cell(self, x, z, tol=0.6):
        """返回占据格子 (x, z) 的塔，无则 None。

        塔位是世界坐标（方块中心），故与格子中心 (x+0.5, z+0.5) 比较。
        tol 为命中容差：点选/升级收紧到 0.6（避免误抓邻格），挂载/拆除放宽到 1.0。
        开采塔按足迹命中（任一格都算），其余塔单格。
        边界用严格小于：相邻格恰距 1.0，用 <= 会连邻格一起命中，
        导致挂载/拆除/卸下隔一格生效，故此处一律 < tol（tol=1.0 仅命中自身格）。
        """
        for tower in self.towers:
            if not tower.active:
                continue
            if getattr(tower, "is_mining", False):
                for (cx, cz) in tower.footprint_cells():
                    if abs(cx - x) < tol and abs(cz - z) < tol:
                        return tower
            elif (abs(tower.position[0] - (x + 0.5)) < tol
                    and abs(tower.position[2] - (z + 0.5)) < tol):
                return tower
        return None

    def _try_upgrade_tower(self) -> bool:
        """点击已有塔进行升级，返回是否成功"""
        wx, wy, wz = self.camera.screen_to_world(rl.GetMouseX(), rl.GetMouseY())
        x, z = math.floor(wx), math.floor(wz)
        tower = self._tower_at_cell(x, z)
        if tower is None:
            return False
        if tower.level >= config.MAX_TOWER_LEVEL:
            self.toast("已达最高等级")
            return True
        cost = tower.upgrade_cost
        if self.money < cost:
            self.toast(f"金钱不足 (需要 {cost})")
            return True
        tower.upgrade()
        self.money -= cost
        msg = f"升级为 Lv{tower.level}"
        if tower.no_attack and tower.weapon_slots:
            # 防御塔：附上挂载摘要，便于确认攻击来源
            names = [m["wdef"].label_cn for m in tower.weapon_mounts]
            msg += (f" | 挂载 {len(names)}/{tower.weapon_slots}"
                    + (f"（{', '.join(names)}）" if names else ""))
        self.toast(msg)
        return True

    # ─── 仓储塔窗口 ──────────────────────────────────────────

    def _try_open_storage(self) -> bool:
        """默认点击链：命中仓储塔则打开其仓储窗口，返回是否命中。"""
        wx, wy, wz = self.camera.screen_to_world(rl.GetMouseX(), rl.GetMouseY())
        x, z = math.floor(wx), math.floor(wz)
        tower = self._tower_at_cell(x, z)
        if tower is None or tower.storage_capacity <= 0:
            return False
        self.storage_panel_open = True
        self.storage_tower = tower
        return True

    def _close_storage_panel(self):
        self.storage_panel_open = False
        self.storage_tower = None

    def _storage_item_cn(self, kind, name) -> str:
        if kind == "materials":
            return MATERIAL_NAMES_CN.get(name, name)
        if kind == "weapons":
            return WEAPON_TYPES[name].label_cn
        return TOOL_TYPES[name].label_cn

    def _storage_panel_action(self, action, kind, name, count=1):
        """仓储窗口动作：kind ∈ materials/weapons/tools。

        count=1（左键）或 "all"（右键，整类全部存/取）。
        """
        tower = self.storage_tower
        if tower is None:
            return
        inv = self.inventory
        cn = self._storage_item_cn(kind, name)

        # 传送带输出：勾选/取消该材料导出到传送带（仅材料；仓储塔手工配置）
        if action == "belt_export":
            if kind != "materials":
                return
            if name in tower.belt_export:
                tower.belt_export.discard(name)
                self.toast(f"已关闭 {cn} 传送带输出")
            else:
                tower.belt_export.add(name)
                self.toast(f"已开启 {cn} 传送带输出")
            return

        if action == "deposit":
            # 背包持有量（工具唯一，按 1 件计）
            if kind == "materials":
                have = inv.get_material(name)
            elif kind == "weapons":
                have = inv.get_weapon(name)
            else:
                have = 1 if name in inv.tools else 0
            if count == "all":
                count = have
            if kind == "tools":
                if have <= 0:
                    self.toast("背包里没有该工具")
                    return
                if name in tower.storage["tools"]:
                    self.toast("该工具已在塔内")
                    return
                if not tower.can_store(name, 1):
                    self.toast("塔内空间不足")
                    return
                inv.tools.discard(name)
                tower.deposit(name, 1)
                self.toast(f"已存入 {cn}")
                return
            count = min(count, have)
            if count <= 0:
                self.toast("背包里没有该物品")
                return
            stored = tower.deposit(name, count)
            if stored <= 0:
                self.toast("塔内空间不足")
                return
            if kind == "materials":
                inv.remove_material(name, stored)
            else:
                inv.remove_weapon(name, stored)
            if stored < count:
                self.toast(f"空间不足，仅存入 {cn} x{stored}")
            else:
                self.toast(f"已存入 {cn} x{stored}")
        else:  # withdraw
            if kind == "materials":
                have = tower.storage["materials"].get(name, 0)
            elif kind == "weapons":
                have = tower.storage["weapons"].get(name, 0)
            else:
                have = 1 if name in tower.storage["tools"] else 0
            if count == "all":
                count = have
            count = min(count, have)
            if count <= 0:
                self.toast("塔内没有该物品")
                return
            taken = tower.withdraw(name, count)
            if taken <= 0:
                return
            if kind == "materials":
                inv.add_material(name, taken)
            elif kind == "weapons":
                inv.add_weapon(name, taken)
            else:
                inv.add_tool(name)
            self.toast(f"已取出 {cn} x{taken}")

    # ─── 加工塔窗口 ──────────────────────────────────────────

    def _try_open_processing(self) -> bool:
        """默认点击链：命中加工塔则打开其加工窗口，返回是否命中。"""
        wx, wy, wz = self.camera.screen_to_world(rl.GetMouseX(), rl.GetMouseY())
        x, z = math.floor(wx), math.floor(wz)
        tower = self._tower_at_cell(x, z)
        if tower is None or tower.processing_capacity <= 0:
            return False
        self.processing_panel_open = True
        self.processing_tower = tower
        return True

    def _close_processing_panel(self):
        self.processing_panel_open = False
        self.processing_tower = None

    def _process_recipe_cn(self, rkey) -> str:
        """配方的中文摘要，如 '木材→木板'（产物数量由窗口进度行单独标注）。"""
        rec = gamedata.PROCESS_RECIPES.get(rkey)
        if rec is None:
            return rkey
        in_cn = MATERIAL_NAMES_CN.get(rec["in"], rec["in"])
        out_cn = MATERIAL_NAMES_CN.get(rec["out"], rec["out"])
        return f"{in_cn}→{out_cn}"

    def _processing_panel_action(self, action, kind, name, count=1):
        """加工窗口动作：kind ∈ materials/weapons/tools/recipe。

        deposit/withdraw 与仓储窗口同语义（count=1 左键 / "all" 右键整类）；
        kind="recipe" 为手动配方点选（再点一次已选配方 = 停机）。
        """
        tower = self.processing_tower
        if tower is None:
            return
        if action == "select_recipe":
            rec = gamedata.PROCESS_RECIPES.get(name)
            if rec is None:
                return
            # 科技前置门控：未解锁（如核燃料循环 eX2）时拒绝选中，防止徒手白嫖
            missing = self._craft_tech_missing(rec.get("requires_tech"))
            if missing:
                self.toast(f"需要科技: {', '.join(missing)}  (按 T 研究)")
                return
            if rec.get("requires_tool"):
                tool_cn = TOOL_TYPES[rec["requires_tool"]].label_cn
                self.toast(f"该配方需要先装配工具: {tool_cn}")
                return
            if tower.manual_recipe == name:
                tower.manual_recipe = None
                self.toast("已停止手动加工")
            else:
                tower.manual_recipe = name
                self.toast(f"开始加工: {self._process_recipe_cn(name)}")
            return

        inv = self.inventory
        cn = self._storage_item_cn(kind, name)

        if action == "deposit":
            # 背包持有量（工具唯一，按 1 件计）
            if kind == "materials":
                have = inv.get_material(name)
            elif kind == "weapons":
                have = inv.get_weapon(name)
            else:
                have = 1 if name in inv.tools else 0
            if count == "all":
                count = have
            if kind == "tools":
                if have <= 0:
                    self.toast("背包里没有该工具")
                    return
                if name in tower.processing["tools"]:
                    self.toast("该工具已在塔内")
                    return
                if not tower.processing_can_store(name, 1):
                    self.toast("塔内空间不足")
                    return
                inv.tools.discard(name)
                tower.processing_deposit(name, 1)
                self.toast(f"已装配 {cn}")
                return
            count = min(count, have)
            if count <= 0:
                self.toast("背包里没有该物品")
                return
            stored = tower.processing_deposit(name, count)
            if stored <= 0:
                self.toast("塔内空间不足")
                return
            if kind == "materials":
                inv.remove_material(name, stored)
            else:
                inv.remove_weapon(name, stored)
            if stored < count:
                self.toast(f"空间不足，仅存入 {cn} x{stored}")
            else:
                self.toast(f"已存入 {cn} x{stored}")
        else:  # withdraw
            if kind == "materials":
                have = tower.processing["materials"].get(name, 0)
            elif kind == "weapons":
                have = tower.processing["weapons"].get(name, 0)
            else:
                have = 1 if name in tower.processing["tools"] else 0
            if count == "all":
                count = have
            count = min(count, have)
            if count <= 0:
                self.toast("塔内没有该物品")
                return
            taken = tower.processing_withdraw(name, count)
            if taken <= 0:
                return
            if kind == "materials":
                inv.add_material(name, taken)
            elif kind == "weapons":
                inv.add_weapon(name, taken)
            else:
                inv.add_tool(name)
            self.toast(f"已取出 {cn} x{taken}")

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
        PICKUP_MAX_DY = 2.5           # Y 上限（R10-V2）
        nearest = None
        nearest_d2 = PICKUP_RADIUS_SQ
        for pickup in self.pickups:
            if not pickup.active:
                continue
            dx = pickup.position[0] - wx
            dy = pickup.position[1] - wy
            dz = pickup.position[2] - wz
            # R10-V2：判定区原本是无限高竖直圆柱（只比 dx/dz）——物品在崖沿
            # 下方/深坑里且 XZ≤1.8 时，点击崖顶/地面即可"隔空拾取"不可见
            # 物品。加 Y 上限：掉落物悬浮约 0.3 格、正常拾取 dy≈0.3（坡地/
            # 视差略大仍远小于 2.5），2.5 格挡住大落差穿地形拾取。同标高薄
            # 墙后的拾取仍可能（XZ-only 吸收视差属有意设计，罕见边界外溢）。
            if dy > PICKUP_MAX_DY or -dy > PICKUP_MAX_DY:
                continue
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

    def _refund_storage(self, tower) -> bool:
        """仓储塔/加工塔存物全额返还背包（玩家主动拆除；被敌人摧毁则损失）。

        加工塔另取消在制任务，已消耗的输入一并返还。返回是否有返还。
        """
        got = False
        if getattr(tower, "storage_capacity", 0):
            got = self._drain_bucket_to_inventory(tower.storage)
            if tower is self.storage_tower:
                self._close_storage_panel()
        if getattr(tower, "processing_capacity", 0):
            for mat, amt in tower.cancel_all_process_jobs().items():
                self.inventory.add_material(mat, amt)
                got = True
            if self._drain_bucket_to_inventory(tower.processing):
                got = True
            if tower is self.processing_tower:
                self._close_processing_panel()
        return got

    def _drain_bucket_to_inventory(self, bucket) -> bool:
        """把一个体积桶（storage/processing 同构）全额转入背包并清空。

        返回是否有返还。
        """
        got = False
        for mat, amt in list(bucket["materials"].items()):
            self.inventory.add_material(mat, amt)
            got = True
        for wn, amt in list(bucket["weapons"].items()):
            self.inventory.add_weapon(wn, amt)
            got = True
        for tl in list(bucket["tools"]):
            self.inventory.add_tool(tl)
            got = True
        bucket["materials"].clear()
        bucket["weapons"].clear()
        bucket["tools"].clear()
        return got

    def _try_dismantle_tower(self):
        """拆除塔并回收材料（防御塔的挂载武器全额返还背包）；先尝试拆除传送带/电线。"""
        if self._try_dismantle_belt():
            return
        if self._try_dismantle_wire():
            return
        wx, wy, wz = self.camera.screen_to_world(rl.GetMouseX(), rl.GetMouseY())
        x, z = math.floor(wx), math.floor(wz)
        tower = self._tower_at_cell(x, z, tol=1.0)
        if tower is None:
            self.toast("未找到可拆除的塔")
            return
        # 研究塔被拆：研究优先迁往其它研究塔，无处承接才取消退款（先于列表移除）
        self._remove_research_lane(tower, refund=True)
        # 先置 active=False：正在围攻此塔的敌人据 active 判断目标是否还在，
        # 不置位会让敌人继续攻击已拆除的"幽灵塔"（BUG-128）。
        tower.active = False
        self.towers.remove(tower)
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
        # 电站燃料桶返还背包（燃电站拆塔时燃料不丢失）
        fuel_refund = dict(getattr(tower, "power_fuel", {}) or {})
        for mat, amt in fuel_refund.items():
            self.inventory.add_material(mat, amt)
        # 防御塔挂载的武器全额返还（玩家主动拆除；被敌人摧毁则损失）
        got_weapons = []
        for mount in tower.weapon_mounts:
            self.inventory.add_weapon(mount["wdef"].name, 1)
            got_weapons.append(mount["wdef"].label_cn)
        # 仓储塔/加工塔：塔内存物全额返还背包
        got_storage = self._refund_storage(tower)
        # 注销塔格（旧版遗漏 → 拆塔后路径仍永久绕行）。
        # 开采塔：足迹所有格注销。
        if getattr(tower, "is_mining", False):
            for (cx, cz) in tower.footprint_cells():
                self.universe.remove_tower_cell(cx, cz)
        else:
            self.universe.remove_tower_cell(
                math.floor(tower.position[0]), math.floor(tower.position[2]))
        self.toast("已拆除，材料已回收"
                   + (f"，返还 {', '.join(got_weapons)}" if got_weapons else "")
                   + ("，塔内物品已返还背包" if got_storage else ""))

    def _try_place_weapon_tower(self):
        """放置武器塔；若点击的是已放置的防御塔，则把武器挂载上去

        防御塔自身无攻击，攻击效果来自挂载的武器（可同时挂多把，攻击叠加）。
        """
        if not self.selected_weapon:
            return
        wx, wy, wz = self.camera.screen_to_world(rl.GetMouseX(), rl.GetMouseY())
        x, z = math.floor(wx), math.floor(wz)

        # ── 命中防御塔：走挂载而非放置 ──
        wall_tower = self._tower_at_cell(x, z, tol=1.0)
        if wall_tower is not None and wall_tower.no_attack:
            if wall_tower.weapon_slots <= 0:
                self.toast("该防御塔无武器槽")
                return
            if len(wall_tower.weapon_mounts) >= wall_tower.weapon_slots:
                self.toast(f"挂载已满 ({wall_tower.weapon_slots}/{wall_tower.weapon_slots})")
                return
            if not self.inventory.remove_weapon(self.selected_weapon.name, 1):
                self.toast("背包中没有该武器")
                return
            wall_tower.mount_weapon(self.selected_weapon,
                                    damage_mult=self.tech_damage_mult())
            self.toast(f"已挂载 {self.selected_weapon.label_cn} "
                       f"({len(wall_tower.weapon_mounts)}/{wall_tower.weapon_slots})")
            self.selected_weapon = None
            self.placing_tower = False
            self.selected_tower_type = None
            self.interaction_mode = "none"
            return

        reason, _blocker = self._placement_block_reason(
            x, z, self.universe.stand_y_for_hit(x, z, wy))
        if reason:
            self.toast(f"无法放置：{reason}")
            return
        install_fee = self.selected_weapon.install_fee
        if self.money < install_fee:
            self.toast(f"安装费不足 (需要 {install_fee})")
            return
        if self.inventory.remove_weapon(self.selected_weapon.name, 1):
            # 站立面取命中方块处（拱洞内命中洞底即放洞底，与预览同源）
            y = self.universe.stand_y_for_hit(x, z, wy)
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

    def _try_unmount_weapon_tower(self):
        """收起防御塔上的挂载武器并返还背包。

        普通左键 = 取下最后挂载的那把（LIFO）；Shift+左键 = 全部取下。
        取下只是从槽位回到背包，塔体不受影响。
        """
        wx, wy, wz = self.camera.screen_to_world(rl.GetMouseX(), rl.GetMouseY())
        x, z = math.floor(wx), math.floor(wz)
        tower = self._tower_at_cell(x, z, tol=1.0)
        if tower is None:
            self.toast("未找到可收起武器的塔")
            return
        if tower.weapon_slots <= 0 or not tower.weapon_mounts:
            self.toast("该塔没有挂载武器")
            return
        if self._shift_down():
            wdefs = tower.unmount_all()
        else:
            wdefs = [tower.unmount_weapon(-1)]
        for w in wdefs:
            self.inventory.add_weapon(w.name, 1)
        self.toast("已收起 " + "、".join(w.label_cn for w in wdefs)
                   + f"（剩余 {len(tower.weapon_mounts)}/{tower.weapon_slots}）")

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

    def _toggle_unmount(self):
        """切换收起武器模式（把防御塔上的武器取回背包，Shift = 全部）"""
        if self.interaction_mode == "unmount":
            self.interaction_mode = "none"
            self.toast("收起武器模式关闭")
        else:
            self.interaction_mode = "unmount"
            self.placing_tower = False
            self.selected_tower_type = None
            self.selected_weapon = None
            self.toast("收起武器模式开启 - 点击防御塔取回武器（Shift=全部）")

    def _toggle_dig(self):
        """切换玩家挖掘模式（R 键）"""
        if self.interaction_mode == "dig":
            self.interaction_mode = "none"
            self.toast("挖掘模式关闭")
        else:
            self.interaction_mode = "dig"
            self.placing_tower = False
            self.selected_tower_type = None
            self.selected_weapon = None
            self.selected_block_id = None
            self.toast("挖掘模式开启 - 左键挖方块并收获材料")

    def _toggle_place(self):
        """切换玩家放置模式（F 键）"""
        if self.interaction_mode == "place":
            self.interaction_mode = "none"
            self.toast("放置模式关闭")
        else:
            self.interaction_mode = "place"
            self.placing_tower = False
            self.selected_tower_type = None
            self.selected_weapon = None
            if self.selected_block_id is None:
                blocks = get_placeable_blocks()
                if blocks:
                    self.selected_block_id = blocks[0].id
            self.toast("放置模式开启 - [1]泥土 [2]石头 [3]沙子 [4]原木 选择方块")

    def _select_block(self, block_id):
        """选择放置方块（1-4 数字键或方块栏点击）。"""
        bdef = get_block_by_name(BLOCK_NAME[block_id])
        if bdef is None or not bdef.placeable:
            return
        self.selected_block_id = block_id
        self.toast(f"放置方块: {bdef.name}")

    def _try_dig_block(self):
        """玩家挖掉鼠标射线命中的方块并收获材料（2D 挖通行列顶）。"""
        mx, my = rl.GetMouseX(), rl.GetMouseY()
        if self.camera.is_3d():
            cell = self.camera.screen_to_block(mx, my)
            if cell is None:
                self.toast("此处不可挖")
                return
            x, y, z = cell
        else:
            wx, _wy, wz = self.camera.screen_to_world(mx, my)
            x, z = math.floor(wx), math.floor(wz)
            y = None  # 2D 无高度信息：沿用列顶语义
        if self._tower_at_cell(x, z, tol=0.6) is not None:
            self.toast("不能挖塔脚下的方块")
            return
        if abs(x - self.universe.base_x) <= 2 and abs(z - self.universe.base_z) <= 2:
            self.toast("不能挖大本营附近的方块")
            return
        top_id = self.universe.break_block(x, z, y)
        if top_id == 0:
            self.toast("此处不可挖")
            return
        bdef = get_block_by_name(BLOCK_NAME.get(top_id, "air"))
        if bdef is not None and bdef.harvest_material:
            self.inventory.add_material(bdef.harvest_material, bdef.harvest_amount)
            self.toast(f"+{bdef.harvest_amount} "
                       f"{MATERIAL_NAMES_CN.get(bdef.harvest_material, bdef.harvest_material)}")
        else:
            self.toast("已挖开（无收获）")
        # 挖方是活动事件：唤醒附近休眠刷怪点 + 让附近侦察兵获信息回巢
        self._register_edit(x, z)
        self._notify_scouts_of_edit(x, z)
        self._terrain_edit_pending = True

    def _try_place_block(self):
        """玩家在鼠标所指列顶放置方块并消耗材料。"""
        if self.selected_block_id is None:
            self.toast("未选择方块")
            return
        bdef = get_block_by_name(BLOCK_NAME.get(self.selected_block_id, "air"))
        if bdef is None or not bdef.placeable:
            self.toast("该方块不可放置")
            return
        wx, wy, wz = self.camera.screen_to_world(rl.GetMouseX(), rl.GetMouseY())
        x, z = math.floor(wx), math.floor(wz)
        stand_y = self.universe.stand_y_for_hit(x, z, wy)
        if not self._is_valid_placement(x, z, stand_y):
            self.toast("无法在此放置")
            return
        mat = bdef.place_material or bdef.harvest_material
        if not mat or not self.inventory.remove_material(mat, 1):
            self.toast(f"缺少材料: {MATERIAL_NAMES_CN.get(mat, mat)}")
            return
        if not self.universe.place_block(x, z, self.selected_block_id):
            self.inventory.add_material(mat, 1)  # 退还（放置失败）
            self.toast("放置失败（高度上限或地形不可放置）")
            return
        self.toast(f"已放置 {bdef.name} (-1 {MATERIAL_NAMES_CN.get(mat, mat)})")
        # 放方是活动事件：唤醒附近休眠刷怪点 + 让附近侦察兵获信息回巢
        self._register_edit(x, z)
        self._notify_scouts_of_edit(x, z)
        self._terrain_edit_pending = True

    @staticmethod
    def _shift_down():
        """Shift 是否按下（左右 Shift 都算）"""
        return rl.IsKeyDown(rl.KEY_LEFT_SHIFT) or rl.IsKeyDown(rl.KEY_RIGHT_SHIFT)

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
        # 切出武器放置/拆除等互斥模式（键盘与塔栏点击两条路径共用）
        self.selected_weapon = None
        self.interaction_mode = "none"
        # 开采塔：足迹复位为最小（玩家再用 [ ] - = 调整）
        if getattr(tower_type, "is_mining", False):
            _fmin = int(tower_type.mining_attr("footprint_min", 3))
            self.selected_mining_footprint = (_fmin, _fmin)
            self.toast("开采塔：[[ ]] 调宽度、[- =] 调长度，点击放置", 2.5)

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

    # ─── 传送带（物流） ──────────────────────────────────────

    def _toggle_belt_mode(self):
        """切换布带模式：进入时默认选中传送带（若未选），退出清理。"""
        if self.interaction_mode == "belt":
            self.interaction_mode = "none"
            self.placing_belt = False
            self.selected_belt = None
            return
        if self.selected_belt is None:
            self._select_belt("belt")
            # 科技未解锁也允许进入模式（选中失败不退出）：进模式让选择栏显示，
            # 玩家可改选已解锁带型（BUG-108）。
        self.interaction_mode = "belt"
        self.placing_belt = True
        # 切出互斥模式
        self.placing_tower = False
        self.selected_tower_type = None
        self.selected_weapon = None

    def _select_belt(self, kind):
        """选择带型（科技前置检查）。"""
        bdef = gamedata.BELTS_RAW.get(kind)
        if not bdef:
            return
        missing = [t for t in bdef.get("requires_tech", []) if not self.has_tech(t)]
        if missing:
            self.toast(f"需要科技: {', '.join(self.tech_label(t) for t in missing)}  (按 T 研究)", 2.5)
            return
        self.selected_belt = kind
        self.placing_belt = True
        self.interaction_mode = "belt"
        self.placing_tower = False
        self.selected_tower_type = None
        self.selected_weapon = None

    def _belt_cost(self, kind):
        bdef = gamedata.BELTS_RAW.get(kind, {})
        return int(bdef.get("base_cost", 0) * self.build_cost_mult())

    def _try_place_belt(self):
        """尝试放置传送带。"""
        if not self.selected_belt:
            return
        wx, wy, wz = self.camera.screen_to_world(rl.GetMouseX(), rl.GetMouseY())
        x, z = math.floor(wx), math.floor(wz)
        reason = self._belt_placement_reason(x, z)
        if reason:
            self.toast(f"无法放置传送带：{reason}")
            return
        cost = self._belt_cost(self.selected_belt)
        if self.money < cost:
            self.toast(f"金币不足 (需要 {cost})")
            return
        y = float(self.universe.surface_height_at(x, z) + 1)
        belt = Belt(self.selected_belt, self.belt_direction, (x, z), y)
        self.belts.append(belt)
        self._belt_by_cell[(x, z)] = belt
        self.money -= cost
        self._register_build(x + 0.5, z + 0.5)
        self.toast(f"已放置 {gamedata.BELTS_RAW[self.selected_belt]['name_cn']}")

    def _belt_placement_reason(self, x, z):
        """传送带放置位置检查：与塔/带/基地/水域/敌人冲突。返回原因或 None。"""
        if not self.universe._is_land_pure(x, z):
            return "此处是水域"
        if abs(x - self.universe.base_x) <= 2 and abs(z - self.universe.base_z) <= 2:
            return "太靠近大本营"
        if (x, z) in self._belt_by_cell:
            return "已有传送带"
        if (x, z) in self._tower_occupied_cells():
            return "与塔重叠"
        stand_y = self.universe.surface_height_at(x, z) + 1
        for enemy in self.enemies:
            if not enemy.active:
                continue
            if (abs(enemy.position[0] - (x + 0.5)) < 1.2
                    and abs(enemy.position[2] - (z + 0.5)) < 1.2
                    and abs(enemy.position[1] - stand_y) <= 1.5):
                return "有敌人挡着"
        return None

    # ─── 电力模式（电线 / 电站 / 电池） ────────────────────────

    def _power_options(self):
        """电力模式可选类型列表（顺序固定，供 1..9 热键与选择栏）。"""
        opts = list(gamedata.WIRES_RAW.keys())
        opts += list(gamedata.STATIONS_RAW.keys())
        opts += list(gamedata.BATTERIES_RAW.keys())
        return opts

    def _toggle_power_mode(self):
        """切换电力模式：进入时默认选中第一项（若未选），退出清理。"""
        if self.interaction_mode == "power":
            self.interaction_mode = "none"
            self.placing_wire = False
            self.selected_power = None
            self.power_kind_sel = "wire"
            return
        if self.selected_power is None:
            opts = self._power_options()
            self._select_power(opts[0]) if opts else None
            # 科技未解锁也允许进入模式（选中失败不退出）：进模式让选择栏显示，
            # 玩家可改选已解锁类型（BUG-108）。
        self.interaction_mode = "power"
        self.placing_wire = True
        # 切出互斥模式
        self.placing_tower = False
        self.selected_tower_type = None
        self.selected_weapon = None
        self.placing_belt = False
        self.selected_belt = None

    def _select_power(self, name):
        """选择电力类型（电线/电站/电池；科技前置检查）。"""
        is_wire = name in gamedata.WIRES_RAW
        is_station = name in gamedata.STATIONS_RAW
        is_battery = name in gamedata.BATTERIES_RAW
        if not (is_wire or is_station or is_battery):
            return
        req = []
        if is_wire:
            req = gamedata.WIRES_RAW[name].get("requires_tech", [])
        elif is_station:
            req = gamedata.STATIONS_RAW[name].get("requires_tech", [])
        else:
            req = gamedata.BATTERIES_RAW[name].get("requires_tech", [])
        missing = [t for t in req if not self.has_tech(t)]
        if missing:
            self.toast(f"需要科技: {', '.join(self.tech_label(t) for t in missing)}  (按 T 研究)", 2.5)
            return
        self.selected_power = name
        self.power_kind_sel = "wire" if is_wire else ("station" if is_station else "battery")
        self.placing_wire = True
        self.interaction_mode = "power"
        self.placing_tower = False
        self.selected_tower_type = None
        self.selected_weapon = None
        self.placing_belt = False
        self.selected_belt = None

    def _power_cost(self, name):
        """电力类型造价（电线/电站/电池；含科技折扣）。"""
        if name in gamedata.WIRES_RAW:
            base = gamedata.WIRES_RAW[name].get("base_cost", 0)
        elif name in gamedata.STATIONS_RAW:
            base = gamedata.STATIONS_RAW[name].get("base_cost", 0)
        elif name in gamedata.BATTERIES_RAW:
            base = gamedata.BATTERIES_RAW[name].get("base_cost", 0)
        else:
            base = 0
        return int(base * self.build_cost_mult())

    def _try_place_power(self):
        """尝试放置当前选中电力类型（电线或电站/电池）。"""
        if not self.selected_power:
            return
        wx, wy, wz = self.camera.screen_to_world(rl.GetMouseX(), rl.GetMouseY())
        x, z = math.floor(wx), math.floor(wz)
        if self.selected_power in gamedata.WIRES_RAW:
            self._try_place_wire(x, z)
        else:
            self._try_place_power_building(x, z, wy)

    def _try_place_wire(self, x, z):
        """放置一格电线。"""
        kind = self.selected_power
        reason = self._wire_placement_reason(x, z)
        if reason:
            self.toast(f"无法放置电线：{reason}")
            return
        cost = self._power_cost(kind)
        if self.money < cost:
            self.toast(f"金币不足 (需要 {cost})")
            return
        y = float(self.universe.surface_height_at(x, z) + 1)
        wire = CableSegment(kind, (x, z), y)
        self.wires.append(wire)
        self._wire_by_cell[(x, z)] = wire
        self.money -= cost
        self._register_build(x + 0.5, z + 0.5)
        self.toast(f"已放置 {gamedata.WIRES_RAW[kind]['name_cn']}")

    def _wire_placement_reason(self, x, z):
        """电线放置检查：与塔/电线/基地/水域/敌人冲突。返回原因或 None。"""
        if not self.universe._is_land_pure(x, z):
            return "此处是水域"
        if abs(x - self.universe.base_x) <= 2 and abs(z - self.universe.base_z) <= 2:
            return "太靠近大本营"
        if (x, z) in self._wire_by_cell:
            return "已有电线"
        if (x, z) in self._tower_occupied_cells():
            return "与塔重叠"
        stand_y = self.universe.surface_height_at(x, z) + 1
        for enemy in self.enemies:
            if not enemy.active:
                continue
            if (abs(enemy.position[0] - (x + 0.5)) < 1.2
                    and abs(enemy.position[2] - (z + 0.5)) < 1.2
                    and abs(enemy.position[1] - stand_y) <= 1.5):
                return "有敌人挡着"
        # 耐压校验：目标格与已有电线同组件最高级不得超过本线额定级
        if self.selected_power:
            own_class = gamedata.WIRES_RAW[self.selected_power].get("voltage_class", "LV")
            adj_class = self._component_wire_max_class(x, z)
            if adj_class and self._class_order(adj_class) > self._class_order(own_class):
                return "耐压不足：邻接电网电压级高于本线"
        return None

    def _component_wire_max_class(self, x, z):
        """目标格 4 邻可达电线组件的最高电压级（None=无邻接电线/电源）。

        除电线本身外，还把组件内任一电线格 4 邻的电站/电池（self._towers_by_cell）
        的电压级一并计入——它们是电压源，邻接高压源时低压线应被判"耐压不足"
        （BUG-109）。
        """
        best = None
        seen = set()
        stack = []

        def _fold(cls):
            nonlocal best
            if cls is None:
                return
            if best is None or self._class_order(cls) > self._class_order(best):
                best = cls

        for dx, dz in power_mod.DXDZ:
            nb = (x + dx, z + dz)
            w = self._wire_by_cell.get(nb)
            if w is not None:
                stack.append(w.cell)
            # 目标格自身的 4 邻电站/电池也算电压源
            t = self._towers_by_cell.get(nb)
            if t is not None:
                _fold(getattr(t, "power_class", None) or "LV")
        while stack:
            cell = stack.pop()
            if cell in seen:
                continue
            seen.add(cell)
            w = self._wire_by_cell.get(cell)
            if w is None:
                continue
            _fold(w.voltage_class)
            for dx, dz in power_mod.DXDZ:
                nb = (cell[0] + dx, cell[1] + dz)
                if nb in self._wire_by_cell and nb not in seen:
                    stack.append(nb)
                # 组件内电线格的 4 邻电站/电池：折入其电压级
                t = self._towers_by_cell.get(nb)
                if t is not None:
                    _fold(getattr(t, "power_class", None) or "LV")
        return best

    @staticmethod
    def _class_order(vc):
        return {"LV": 0, "MV": 1, "HV": 2}.get(vc, 0)

    def _try_place_power_building(self, x, z, wy):
        """放置电站/电池塔（复用塔放置规则 + 电力特殊条件）。"""
        name = self.selected_power
        is_battery = name in gamedata.BATTERIES_RAW
        tower_type = TOWER_TYPES.get(name)
        if tower_type is None:
            return
        # 电池异型邻接拒绝：4 邻已有异型电池 → 不可放
        if is_battery:
            for dx, dz in power_mod.DXDZ:
                nb = self._towers_by_cell.get((x + dx, z + dz))
                if nb is not None and getattr(nb, "power_kind", None) == "battery" \
                        and nb.type_name != name:
                    self.toast("无法放置：异型电池不可混接（邻接为其他电池）")
                    return
        y = self.universe.stand_y_for_hit(x, z, wy)
        reason, _blocker = self._placement_block_reason(x, z, y)
        if reason:
            self.toast(f"无法放置：{reason}")
            return
        # 电站特殊建设条件
        if not is_battery:
            cond = getattr(tower_type, "condition_kind", None)
            if cond == "water_adjacent" and not self._water_adjacent(x, z):
                self.toast("无法放置：水电站须邻接水域")
                return
        cost = self._power_cost(name)
        if self.money < cost:
            self.toast(f"金币不足 (需要 {cost})")
            return
        tower = Tower(tower_type, (x + 0.5, y, z + 0.5),
                      damage_mult=self.tech_damage_mult())
        self.towers.append(tower)
        self.money -= cost
        self.universe.add_tower_cell(x, z, is_wall=tower.no_attack)
        self._register_build(tower.position[0], tower.position[2])
        self._towers_by_cell[(x, z)] = tower
        if name in gamedata.STATIONS_RAW:
            cn = gamedata.STATIONS_RAW[name]["name_cn"]
        elif name in gamedata.BATTERIES_RAW:
            cn = gamedata.BATTERIES_RAW[name]["name_cn"]
        else:
            cn = name
        self.toast(f"已放置 {cn}")

    def _water_adjacent(self, x, z):
        """目标格 4 邻是否有水域（海洋列或水面方块）。"""
        from blocks import BLOCK_ID as _bid
        for dx, dz in power_mod.DXDZ:
            nx, nz = x + dx, z + dz
            # 海洋列：非陆地即水
            if not self.universe._is_land_pure(nx, nz):
                return True
            # 内陆水方块（湖/河）：查地表及上方两层
            top = self.universe.get_surface_height(nx, nz)
            for yy in (top, top + 1, top + 2):
                if self.universe.get_block_safe(nx, yy, nz) == _bid["water"]:
                    return True
        return False

    def _try_dismantle_wire(self) -> bool:
        """拆除命中的电线（无返还）。返回是否命中。"""
        wx, wy, wz = self.camera.screen_to_world(rl.GetMouseX(), rl.GetMouseY())
        x, z = math.floor(wx), math.floor(wz)
        wire = self._wire_by_cell.get((x, z))
        if wire is None:
            return False
        self.wires.remove(wire)
        del self._wire_by_cell[(x, z)]
        self.toast("已拆除电线")
        return True

    def _try_dismantle_belt(self) -> bool:
        """拆除命中的传送带（带内存货全额返背包）。返回是否命中。"""
        wx, wy, wz = self.camera.screen_to_world(rl.GetMouseX(), rl.GetMouseY())
        x, z = math.floor(wx), math.floor(wz)
        belt = self._belt_by_cell.get((x, z))
        if belt is None:
            return False
        # 带内存货返背包（材料）
        refunded = {}
        if belt.kind == "bridge":
            streams = [l["items"] for l in belt.lanes]
        else:
            streams = [belt.items]
        for items in streams:
            for it in items:
                refunded[it["name"]] = refunded.get(it["name"], 0) + 1
        for name, amt in refunded.items():
            self.inventory.add_material(name, amt)
        self.belts.remove(belt)
        del self._belt_by_cell[(x, z)]
        parts = []
        if refunded:
            parts.append("，返还 " + ", ".join(
                f"{MATERIAL_NAMES_CN.get(m, m)}x{n}" for m, n in refunded.items()))
        self.toast("已拆除传送带" + "".join(parts))
        return True

    def _try_rotate_belt(self) -> bool:
        """默认点击链：命中已有传送带则旋转其方向/朝向。返回是否命中。"""
        wx, wy, wz = self.camera.screen_to_world(rl.GetMouseX(), rl.GetMouseY())
        x, z = math.floor(wx), math.floor(wz)
        belt = self._belt_by_cell.get((x, z))
        if belt is None:
            return False
        belt.direction = (belt.direction + 1) % 4
        if belt.kind == "bridge":
            da, db = belts_mod.BRIDGE_LANES[belt.direction]
            belt.lanes[0]["direction"] = da
            belt.lanes[1]["direction"] = db
        self.toast(f"{gamedata.BELTS_RAW[belt.kind]['name_cn']} 已旋转")
        return True

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

        # 站立面先行（world.stand_y_for_hit）：普通地表=列顶+1；拱洞/隧道内
        # 命中洞底即放洞底（堵通道）；侧脸命中爬到柱顶。敌人同层判定需要
        # 塔的落脚高度，且覆盖层感知、不触发区块生成 / LRU 淘汰。
        y = self.universe.stand_y_for_hit(x, z, wy)

        # 检查位置有效性（拒绝原因直接提示，避免"塔去哪了"的困惑）
        is_mining = getattr(self.selected_tower_type, "is_mining", False)
        footprint = tuple(self.selected_mining_footprint) if is_mining else (1, 1)
        if is_mining:
            reason, _blocker = self._footprint_placement_reason(x, z, footprint, y)
        else:
            reason, _blocker = self._placement_block_reason(x, z, y)
        if reason:
            self.toast(f"无法放置：{reason}")
            return

        # 检查金钱
        cost = self.tower_cost(self.selected_tower_type, footprint if is_mining else None)
        if self.money < cost:
            self.toast(f"金币不足 (需要 {cost})")
            return

        # 放置塔（应用科技伤害加成）
        tower = Tower(self.selected_tower_type, (x + 0.5, y, z + 0.5),
                      damage_mult=self.tech_damage_mult())
        if is_mining:
            tower.footprint = (int(footprint[0]), int(footprint[1]))
        self.towers.append(tower)
        if getattr(tower, "is_research", False):
            # 新增研究塔 → 作为空通道参与队列重排（排队项摊到新塔）
            self._rebalance_research_lanes()
        self.money -= cost
        # 注册塔格（延迟路径绕行）；防御塔额外登记为墙格（寻路近乎不可通行）。
        # 开采塔：足迹所有格都登记为墙格（竖井在塔脚下，路径天然绕行）。
        if is_mining:
            for (cx, cz) in tower.footprint_cells():
                self.universe.add_tower_cell(cx, cz, is_wall=tower.no_attack)
        else:
            self.universe.add_tower_cell(x, z, is_wall=tower.no_attack)

        # 造塔触发：检查附近休眠点
        self._register_build(tower.position[0], tower.position[2])

        # 取消放置模式
        self.placing_tower = False
        self.selected_tower_type = None

    def _tower_occupied_cells(self):
        """所有塔占据的格集合（开采塔按足迹，其余单格）。"""
        cells = set()
        for t in self.towers:
            if not t.active:
                continue
            if getattr(t, "is_mining", False):
                cells.update(t.footprint_cells())
            else:
                cells.add((math.floor(t.position[0]), math.floor(t.position[2])))
        return cells

    def _footprint_cells_for(self, ax, az, footprint):
        """锚点 (ax,az) 居中的 m×n 足迹格。"""
        m, n = footprint
        return [(ax + dx, az + dz)
                for dx in range(-(m // 2), m - (m // 2))
                for dz in range(-(n // 2), n - (n // 2))]

    def _footprint_placement_reason(self, ax, az, footprint, stand_y):
        """开采塔足迹放置检查：逐格陆地/基地/塔/带/线/敌人。返回 (原因, 阻挡坐标)。"""
        occ = self._tower_occupied_cells()
        for (x, z) in self._footprint_cells_for(ax, az, footprint):
            if not self.universe._is_land_pure(x, z):
                return "此处是水域", (x + 0.5, stand_y, z + 0.5)
            if abs(x - self.universe.base_x) <= 2 and abs(z - self.universe.base_z) <= 2:
                return "太靠近大本营", (x + 0.5, stand_y, z + 0.5)
            if (x, z) in occ:
                return "与已有塔重叠", (x + 0.5, stand_y, z + 0.5)
            if (x, z) in self._belt_by_cell:
                return "与传送带重叠", (x + 0.5, stand_y, z + 0.5)
            if (x, z) in self._wire_by_cell:
                return "与电线重叠", (x + 0.5, stand_y, z + 0.5)
            for enemy in self.enemies:
                if not enemy.active:
                    continue
                if (abs(enemy.position[0] - (x + 0.5)) < 1.2
                        and abs(enemy.position[2] - (z + 0.5)) < 1.2
                        and abs(enemy.position[1] - stand_y) <= 1.5):
                    return "有敌人挡着", enemy.position
        return None, None

    def _placement_block_reason(self, x, z, stand_y):
        """返回 (x, z) 不可放置的 (原因文本, 阻挡实体坐标)；可放置返回 (None, None)。

        stand_y 为塔的落脚高度（stand_y_for_hit），敌人判定要求同层：
        隧道/拱洞里的敌人（低数层）不应挡住地表放塔，反之亦然。
        允许放在路径上挡路（敌人会绕行/拆塔/挖地）；
        仍禁止：海洋、大本营区、与既有塔重叠、敌人正脚下（防卡死）。
        阻挡实体坐标供放置预览把"为什么红"标到具体单位上——远 zoom 下
        敌人只有几个像素，玩家看不到是谁挡的。
        """
        # 不能放在海洋/水下（纯噪声判定：与 chunk 海洋列阈值 h<0.005 一致；
        # 不用 get_biome_safe，它在区块未加载时默认返回 plains 会漏判）
        if not self.universe._is_land_pure(x, z):
            return "此处是水域", None

        # 不能放在大本营上
        if abs(x - self.universe.base_x) <= 2 and abs(z - self.universe.base_z) <= 2:
            return "太靠近大本营", None

        # 高度上限（与 world.place_block 一致，BUG-135）：stand_y 即列顶新高度
        # new_y（= surface_height_at + 1）。超过世界高度或自然地表 + 堆叠上限
        # 时必失败，提前判红，避免幽灵框显示绿色但点击总失败。
        if stand_y >= self.universe.height:
            return "已达世界高度上限", None
        natural = self.universe._surface_height_pure(x, z)
        if stand_y > natural + config.MAX_BLOCK_STACK:
            return "超过堆叠上限（自然地表+%d）" % config.MAX_BLOCK_STACK, None

        # 不能放在已有塔的位置（开采塔按足迹判定，其余单格）
        if (x, z) in self._tower_occupied_cells():
            for tower in self.towers:
                if abs(tower.position[0] - (x + 0.5)) < 1 and abs(tower.position[2] - (z + 0.5)) < 1:
                    return "与已有塔重叠", tower.position
            return "与已有塔重叠", (x + 0.5, stand_y, z + 0.5)

        # 不能放在传送带上
        if (x, z) in self._belt_by_cell:
            return "与传送带重叠", (x + 0.5, stand_y, z + 0.5)

        # 不能放在电线上（塔与电线互斥；电线与传送带可同格共存）
        if (x, z) in self._wire_by_cell:
            return "与电线重叠", (x + 0.5, stand_y, z + 0.5)

        # 不能放在敌人正脚下（敌人会被卡在塔里）——同层判定：position[1]
        # 即敌人立足地表，与塔落脚高差 >1.5 视为不同层互不影响
        for enemy in self.enemies:
            if not enemy.active:
                continue
            if (abs(enemy.position[0] - (x + 0.5)) < 1.2
                    and abs(enemy.position[2] - (z + 0.5)) < 1.2
                    and abs(enemy.position[1] - stand_y) <= 1.5):
                return "有敌人挡着", enemy.position

        return None, None

    def _is_valid_placement(self, x, z, stand_y):
        """检查放置位置是否有效（无限地形，无边界限制）。

        stand_y 为落脚高度（stand_y_for_hit），敌人同层判定需要。
        """
        return self._placement_block_reason(x, z, stand_y)[0] is None

    def render(self):
        """渲染游戏"""
        begin_drawing()

        # 渲染世界（预览/高亮在相机上下文内渲染）
        # 挖掘产生的脏区块转给渲染器（下一帧重建网格）
        if self.universe.dirty_chunks:
            self.renderer.mark_chunks_dirty(self.universe.dirty_chunks)
            self.universe.dirty_chunks.clear()
        entities = {
            "game": self,
            "towers": self.towers,
            "enemies": self.enemies,
            "projectiles": self.projectiles,
            "pickups": self.pickups,
            "belts": self.belts,
            "wires": self.wires,
            "spawners": [(rt, self.universe.spawners.get(rt.spawner_id))
                          for rt in self.spawners.values()
                          if self.universe.spawners.get(rt.spawner_id)],
        }
        # 构建相机内额外渲染回调（放置预览 + 拆除高亮 + 方块编辑预览）
        _need_preview = (self.placing_tower and self.selected_tower_type) or self.selected_weapon \
            or (self.interaction_mode == "belt") or (self.interaction_mode == "power")
        _need_dismantle = (self.interaction_mode == "dismantle")
        _need_block_edit = self.interaction_mode in ("dig", "place")
        camera_extra = None
        if _need_preview or _need_dismantle or _need_block_edit:
            def camera_extra():
                if _need_preview:
                    self.ui.render_placement_preview(self)
                if _need_dismantle:
                    self.ui.render_dismantle_highlight(self)
                if _need_block_edit:
                    self.ui.render_block_edit_preview(self)

        self.renderer.render(self.camera, self.universe, entities, camera_extra)

        # 渲染 HUD
        self.ui.render_hud(self)

        # 塔悬停识别标签（光标指向世界里的塔时显示名字+职责，不改外观）
        self.ui.render_tower_hover(self)

        # 渲染小地图（HUD 之上、面板之下；面板打开时会被自然盖住）
        if self.minimap_visible:
            self.minimap.render(self)

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
                         researching=None,
                         research_timer=0,
                         research_total=0,
                         lanes=self.research_lanes())

        # 渲染加工面板
        if self.craft_panel_open:
            self.ui.render_craft_panel(self)

        # 渲染武器库面板
        if self.weapon_panel_open:
            self.ui.render_weapon_panel(self)

        # 渲染仓储面板
        if self.storage_panel_open:
            self.ui.render_storage_panel(self)

        # 渲染加工面板
        if self.processing_panel_open:
            self.ui.render_processing_panel(self)

        # 渲染刷怪点波次面板
        if self.spawner_panel_open:
            self.ui.render_spawner_panel(self)

        # 渲染游戏结束
        if self.state == GameState.GAME_OVER:
            self.ui.render_game_over(self)

        # 渲染存档菜单
        if self.state == GameState.SAVE_MENU:
            self.ui.render_save_menu(self)

        # 渲染设置面板（最顶层，F10 打开）
        if self.settings_panel_open:
            self.ui.render_settings_panel(self)

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
        self.save_menu_scroll = 0
        self.save_menu_error = None
        self.save_menu_confirm_delete = None
        self.state = GameState.SAVE_MENU

    # ─── 设置面板（F10；持久化 settings.json） ────────────────

    def _open_settings_panel(self):
        """打开设置面板，行选择复位到首行。"""
        self.settings_panel_open = True
        self.settings_row = 0

    def _close_settings_panel(self):
        """关闭设置面板（改动已在每次调节时落盘，无需额外保存）。"""
        self.settings_panel_open = False

    def _settings_apply(self, key):
        """把某项设置的变化同步到运行时对象。

        minimap_enabled → self.minimap_visible（M 键与面板共用一份状态）；
        show_attack_routes / route_opacity 由 renderer 每帧直接读 settings，
        透明度变化通过 renderer._path_opacity_last 对比自动重烘焙网格。
        """
        if key == "minimap_enabled":
            self.minimap_visible = bool(self.settings["minimap_enabled"])

    def _update_settings_panel(self):
        """设置面板输入处理：↑↓ 选行，Enter/Space 切换，←→ 调滑块，F10/Esc 关闭。

        每次改动即时写盘（settings.save 原子写）并同步运行时对象。
        """
        rows = self._settings_rows
        n = len(rows)

        if rl.IsKeyPressed(rl.KEY_F10) or rl.IsKeyPressed(rl.KEY_ESCAPE):
            self._close_settings_panel()
            return
        if rl.IsKeyPressed(rl.KEY_UP):
            self.settings_row = (self.settings_row - 1) % n
        elif rl.IsKeyPressed(rl.KEY_DOWN):
            self.settings_row = (self.settings_row + 1) % n
        else:
            key = rows[self.settings_row]
            # 切换型（布尔）
            if key in ("show_attack_routes", "minimap_enabled"):
                if (rl.IsKeyPressed(rl.KEY_ENTER) or rl.IsKeyPressed(rl.KEY_KP_ENTER)
                        or rl.IsKeyPressed(rl.KEY_SPACE)
                        or rl.IsKeyPressed(rl.KEY_LEFT) or rl.IsKeyPressed(rl.KEY_RIGHT)):
                    self.settings[key] = not bool(self.settings[key])
                    settings.save(self.settings)
                    self._settings_apply(key)
            # 滑块型（0..1 浮点）
            elif key == "route_opacity":
                step = 0.05
                cur = float(self.settings.get(key, 0.4))
                new = None
                if rl.IsKeyPressed(rl.KEY_LEFT):
                    new = cur - step
                elif rl.IsKeyPressed(rl.KEY_RIGHT):
                    new = cur + step
                elif rl.IsKeyPressed(rl.KEY_ENTER) or rl.IsKeyPressed(rl.KEY_KP_ENTER) \
                        or rl.IsKeyPressed(rl.KEY_SPACE):
                    # 回车循环几个常用档位，便于无方向键时快速切换
                    presets = (0.2, 0.4, 0.6, 0.8, 1.0)
                    new = next((p for p in presets if p > cur + 1e-6), presets[0])
                if new is not None:
                    self.settings[key] = max(0.0, min(1.0, round(new, 2)))
                    settings.save(self.settings)
                    self._settings_apply(key)

    def _save_menu_geometry(self):
        """存档菜单几何（渲染与点击共用）：返回
        (panel_w, row_h, row_gap, base_y, panel_h, visible_rows, scroll_max)。

        panel_h 上钳到屏内（底部留 60 给页脚）；可见行数按面板内容区计算。
        """
        sh = rl.GetScreenHeight()
        panel_w, row_h, row_gap = 560, 36, 6
        base_y = sh // 2 - 140
        total = 1 + len(self.save_menu_items)
        # 底部额外空间：确认删除条 + 错误文本（避免与页脚重叠）
        bottom_extra = 0
        if self.save_menu_confirm_delete:
            bottom_extra += 30
        if self.save_menu_error:
            bottom_extra += 18
        panel_h = 70 + total * (row_h + row_gap) + 40 + bottom_extra
        if panel_h < 120:
            panel_h = 120
        max_panel_h = sh - (base_y - 40) - 60  # 面板顶部 = base_y-40，底部留 60 给页脚
        if panel_h > max_panel_h:
            panel_h = max_panel_h
        avail = panel_h - 70 - 40 - bottom_extra  # 内容区 = 标题 70 + 底部 40 + 额外
        visible_rows = max(1, avail // (row_h + row_gap))
        scroll_max = max(0, total - visible_rows)
        return (panel_w, row_h, row_gap, base_y, panel_h, visible_rows, scroll_max)

    def _clamp_save_menu_scroll(self):
        """滚动跟随选中行，并把滚动/索引夹取到合法范围（BUG-041）。"""
        _, _, _, _, _, visible_rows, scroll_max = self._save_menu_geometry()
        # 选中索引越界（删除后未夹取）先拉回，避免选中不可见行
        max_idx = len(self.save_menu_items)
        if self.save_menu_index > max_idx:
            self.save_menu_index = max_idx
        if self.save_menu_index < self.save_menu_scroll:
            self.save_menu_scroll = self.save_menu_index
        elif self.save_menu_index >= self.save_menu_scroll + visible_rows:
            self.save_menu_scroll = self.save_menu_index - visible_rows + 1
        if self.save_menu_scroll > scroll_max:
            self.save_menu_scroll = scroll_max
        if self.save_menu_scroll < 0:
            self.save_menu_scroll = 0

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
            max_idx = len(self.save_menu_items)  # 0=新建, 1..n=存档，下标上限 n
            if self.save_menu_index < max_idx:
                self.save_menu_index += 1

        # 滚动跟随选中行（面板上钳后只显示部分行，视窗外的行不可点/不可见）
        self._clamp_save_menu_scroll()

        # Enter 激活
        if rl.IsKeyPressed(rl.KEY_ENTER) or rl.IsKeyPressed(rl.KEY_KP_ENTER):
            if self.save_menu_index == 0:
                # 新建存档
                path = save_system.save_game(self)
                self.save_menu_items = save_system.list_saves()
                self.save_menu_scroll = 0
                self.save_menu_error = None
                # 新建后清掉残留的删除确认（BUG-042），否则下一个 Del 误删上一个存档
                self.save_menu_confirm_delete = None
                self.toast(f"已存档: {os.path.basename(path)}")
                self.state = GameState.PLAYING
            elif self.save_menu_index <= len(self.save_menu_items):
                # 载入存档
                item = self.save_menu_items[self.save_menu_index - 1]
                print(f"[存档] 载入: {os.path.basename(item['path'])}")
                game = save_system.load_game(item["path"])
                if game:
                    self._pending_replace = game
                    self.toast("存档已加载")
                    self.state = GameState.PLAYING
                else:
                    # 加载失败：留在存档菜单显示错误，不进 PLAYING（BUG-040），
                    # 否则刚设的 save_menu_error 立刻被丢弃、玩家看不到反馈。
                    self.save_menu_error = "加载失败"
            return

        # Del 删除（二次确认）
        if rl.IsKeyPressed(rl.KEY_DELETE):
            if self.save_menu_confirm_delete is not None:
                path = self.save_menu_confirm_delete
                if save_system.delete_save(path):
                    self.toast("已删除")
                self.save_menu_confirm_delete = None
                self.save_menu_items = save_system.list_saves()
                # 删除后条目数变少，夹取选中索引到合法范围（BUG-041），
                # 否则越界后 Enter 静默无效、选中行不可见。
                n = len(self.save_menu_items)
                if self.save_menu_index > n:
                    self.save_menu_index = n
                self._clamp_save_menu_scroll()
            elif 1 <= self.save_menu_index <= len(self.save_menu_items):
                self.save_menu_confirm_delete = self.save_menu_items[self.save_menu_index - 1]["path"]
            return

        # 鼠标点击
        self._handle_save_menu_click()

    def _handle_save_menu_click(self):
        """存档菜单鼠标处理：点击行选中并激活（只命中视窗内可见行）。"""
        mx, my = rl.GetMouseX(), rl.GetMouseY()
        sw, sh = rl.GetScreenWidth(), rl.GetScreenHeight()

        panel_w, row_h, row_gap, base_y, _, visible_rows, _ = self._save_menu_geometry()
        x = (sw - panel_w) // 2

        total = 1 + len(self.save_menu_items)
        # 只命中视窗内可见行（滚动的首行=save_menu_scroll），视窗外行不可点
        for i in range(self.save_menu_scroll,
                       min(total, self.save_menu_scroll + visible_rows)):
            y = base_y + (i - self.save_menu_scroll) * (row_h + row_gap)
            if x <= mx <= x + panel_w and y <= my <= y + row_h:
                if rl.IsMouseButtonPressed(rl.MOUSE_BUTTON_LEFT):
                    self.save_menu_index = i
                    if i == 0:
                        path = save_system.save_game(self)
                        self.save_menu_items = save_system.list_saves()
                        self.save_menu_scroll = 0
                        # 与 Enter 路径一致：清残留错误与删除确认（BUG-042/043），
                        # 否则保存成功后仍显示"加载失败"、下一个 Del 误删上一存档。
                        self.save_menu_error = None
                        self.save_menu_confirm_delete = None
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
