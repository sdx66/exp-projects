"""
game.py - 主游戏循环与状态机
管理：游戏状态、波次系统、塔放置、敌人管理、输入处理
"""

import raylib as rl
import math
import config
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


class GameState:
    """游戏状态机"""
    MENU = "menu"
    PLAYING = "playing"
    TECH_PANEL = "tech_panel"
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

        # 波次管理
        self.wave_timer = 0.0
        self.wave_delay = 3.0
        self.wave_spawn_timer = 0.0
        self.wave_spawn_interval = 0.5
        self.wave_enemy_count = 0
        self.wave_max_enemies = 0
        self.wave_enemy_types = []
        self.between_waves = True
        self.wave_announcement_timer = 0.0

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

        # 材料/背包/交互
        self.inventory = Inventory()
        self.interaction_mode = "none"  # "none"|"place_classic"|"place_weapon"|"dismantle"
        self.selected_weapon = None
        self.craft_panel_open = False
        self.craft_timer = 0.0
        self.craft_action = None  # 当前加工/合成动作

        # 初始化
        self._init_wave(1)

    def _init_wave(self, wave_num):
        """初始化波次"""
        self.wave = wave_num
        self.wave_max_enemies = config.WAVE_BASE_COUNT + wave_num * config.WAVE_COUNT_PER_WAVE
        self.wave_enemy_count = 0
        self.wave_spawn_timer = 0.0
        self.wave_spawn_interval = max(config.WAVE_SPAWN_INTERVAL_MIN, config.WAVE_SPAWN_INTERVAL_BASE - wave_num * config.WAVE_SPAWN_INTERVAL_DECAY)

        # 选择敌人类型
        types = []
        if wave_num >= 1:
            types.append("regular")
        if wave_num >= 3:
            types.append("fast")
        if wave_num >= 5:
            types.append("tank")
        self.wave_enemy_types = types

        self.between_waves = False
        self.wave_announcement_timer = 3.0

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
        self.wave_timer = 0.0
        self.wave_delay = 3.0
        self.wave_spawn_timer = 0.0
        self.wave_spawn_interval = 0.5
        self.wave_enemy_count = 0
        self.wave_max_enemies = 0
        self.wave_enemy_types = []
        self.between_waves = True
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
        self._init_wave(1)
        self.inventory.reset()
        self.interaction_mode = "none"
        self.selected_weapon = None
        self.craft_panel_open = False
        self.craft_timer = 0.0
        self.craft_action = None

    def _spawn_enemy(self):
        """生成敌人 — 根据性格和拥挤度选择路径"""
        if self.wave_enemy_count >= self.wave_max_enemies:
            return

        # 选择敌人类型
        type_name = self.wave_enemy_types[
            self.wave_enemy_count % len(self.wave_enemy_types)]
        enemy_type = ENEMY_TYPES.get(type_name)
        if not enemy_type:
            return

        # 选择路径：根据敌人性格、地形偏好、拥挤程度
        all_paths = self.universe.enemy_paths
        if not all_paths:
            return

        # 选择路径：根据敌人性格、地形偏好、拥挤程度
        chosen_path = Enemy.pick_path(enemy_type, all_paths, existing_enemies=self.enemies, rng=self.universe.rng)

        # 位置：所选路径的起点
        start = chosen_path.points[0] if chosen_path else all_paths[0].points[0]

        # 波次增强（指数增长；原来的加法 1+G*(w-1) 使波10 就达 11 倍，曲线失真）
        health_mult = config.ENEMY_HEALTH_GROWTH ** (self.wave - 1)
        speed_mult = 1.0 + (self.wave - 1) * config.ENEMY_SPEED_GROWTH
        reward = int(enemy_type.base_reward * (1 + self.wave * config.ENEMY_REWARD_GROWTH))

        # 创建敌人
        enemy = Enemy(enemy_type, start, chosen_path, all_paths=all_paths, rng=self.universe.rng)
        enemy.health = int(enemy.health * health_mult)
        enemy.max_health = enemy.health
        enemy.speed = enemy_type.base_speed * speed_mult
        enemy.reward = reward
        self.enemies.append(enemy)
        self.wave_enemy_count += 1

    def _check_wave_complete(self):
        """检查波次是否完成"""
        if self.wave_enemy_count >= self.wave_max_enemies:
            still_active = False
            for enemy in self.enemies:
                if enemy.active:
                    still_active = True
                    break
            if not still_active:
                self.between_waves = True
                self.wave_timer = self.wave_delay
                # 波次奖励
                self.money += config.WAVE_BONUS_BASE + self.wave * config.WAVE_BONUS_PER_WAVE
                self.score += config.SCORE_PER_WAVE * self.wave

    # ─── 科技 ────────────────────────────────────────────────
    def tech_damage_mult(self):
        """科技带来的塔伤害倍率（handcraft +10%）"""
        return config.TECH_EFFECTS.get("tower_damage", 1.0) if "handcraft" in self.unlocked_tech else 1.0

    def build_cost_mult(self):
        """科技带来的建造费用倍率（stone_mining -15%）"""
        return config.TECH_EFFECTS.get("build_cost", 1.0) if "stone_mining" in self.unlocked_tech else 1.0

    def tower_cost(self, tower_type):
        return int(tower_type.base_cost * self.build_cost_mult())

    def _tech_available(self, tech_name):
        """科技是否可以开始研究（已解锁 / 正在研究 / 前置未满足 / 钱不够）"""
        tech = config.TECH_TREE.get(tech_name)
        if not tech or tech_name in self.unlocked_tech:
            return False
        if self.tech_researching:
            return False
        for req in tech["requires"]:
            if req not in self.unlocked_tech:
                return False
        return self.money >= tech["cost"]

    def _research_tech(self, tech_name):
        """开始研究科技"""
        tech = config.TECH_TREE.get(tech_name)
        if not tech or not self._tech_available(tech_name):
            return False
        self.money -= tech["cost"]
        self.tech_researching = tech_name
        self.tech_research_total = tech["time"]
        self.tech_research_timer = tech["time"]
        return True

    def _award_kill(self, enemy):
        """击杀结算：金钱 + 分数 + 掉落物"""
        self.money += enemy.reward
        self.score += enemy.reward * 2

        # 掉落物：根据敌人类型决定
        drop_table = {
            "regular": [("wood", 2), ("stone", 2)],
            "fast": [("sand", 1), ("wood", 1)],
            "tank": [("iron_ore", 1), ("stone", 3)],
        }
        drops = drop_table.get(enemy.type_name, [("wood", 1)])
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

        # 更新相机
        self.camera.update(dt)

        # 更新天气
        self.universe.update_weather(dt)

    def _update_playing(self, dt):
        """更新游戏进行中状态"""
        # 波次管理
        if self.between_waves:
            self.wave_timer -= dt
            if self.wave_timer <= 0:
                self._init_wave(self.wave + 1)
        else:
            # 生成敌人
            self.wave_spawn_timer -= dt
            if self.wave_spawn_timer <= 0 and self.wave_enemy_count < self.wave_max_enemies:
                self._spawn_enemy()
                self.wave_spawn_timer = self.wave_spawn_interval

            # 检查波次完成
            self._check_wave_complete()

        # 波次公告计时
        if self.wave_announcement_timer > 0:
            self.wave_announcement_timer -= dt

        # 屏幕提示计时
        if self.toast_timer > 0:
            self.toast_timer -= dt

        # 更新塔（收集新抛射体）
        new_projectiles = []
        for tower in self.towers:
            proj = tower.update(dt, self.enemies)
            if proj:
                new_projectiles.append(proj)
        self.projectiles.extend(new_projectiles)

        # 更新敌人
        for enemy in self.enemies:
            result = enemy.update(dt, self.universe)
            if result == "reached_base":
                killed = self.base.take_damage(config.ENEMY_BASE_DAMAGE)
                if killed:
                    self.state = GameState.GAME_OVER
                    return

        # 更新抛射体（仅在有弹道时重建列表，避免空列表分配）
        if self.projectiles:
            active_projectiles = []
            for proj in self.projectiles:
                if not proj.active:
                    continue
                result = proj.update(dt, self.universe, self.enemies)
                if result and result.get("hit"):
                    # 主目标击杀
                    if result.get("killed"):
                        self._award_kill(result["enemy"])
                    # 溅射额外击杀
                    for extra in result.get("extra_kills", []):
                        self._award_kill(extra)
                if proj.active:
                    active_projectiles.append(proj)
            self.projectiles = active_projectiles

        # 清理非活跃敌人（只在确实有击杀/漏怪时重建列表）
        if not all(enemy.active for enemy in self.enemies):
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
                self.unlocked_tech.add(self.tech_researching)
                self.tech_researching = None

        # 输入处理
        self._handle_input(dt)

    def _update_tech_panel(self, dt):
        """更新科技面板状态"""
        # 科技面板打开时游戏暂停
        if rl.IsKeyPressed(rl.KEY_T) or rl.IsKeyPressed(rl.KEY_ESCAPE):
            self.state = GameState.PLAYING
            return
        self._handle_input(dt)

    def _handle_input(self, dt):
        """处理输入"""
        # 加工面板打开时
        if self.craft_panel_open:
            if rl.IsKeyPressed(rl.KEY_B) or rl.IsKeyPressed(rl.KEY_ESCAPE):
                self.craft_panel_open = False
                return
            # 1-3: 加工（自动选徒手/工具）
            if rl.IsKeyPressed(rl.KEY_ONE):
                self._craft_panel_action("process", "wood->plank")
            elif rl.IsKeyPressed(rl.KEY_TWO):
                self._craft_panel_action("process", "stone->cobble")
            elif rl.IsKeyPressed(rl.KEY_THREE):
                self._craft_panel_action("process", "iron_ore->ingot")
            # 4-7: 合成武器
            elif rl.IsKeyPressed(rl.KEY_FOUR):
                self._craft_panel_action("weapon", "wood_club")
            elif rl.IsKeyPressed(rl.KEY_FIVE):
                self._craft_panel_action("weapon", "stone_axe")
            elif rl.IsKeyPressed(rl.KEY_SIX):
                self._craft_panel_action("weapon", "iron_sword")
            elif rl.IsKeyPressed(rl.KEY_SEVEN):
                self._craft_panel_action("weapon", "sand_sling")
            # 8-0: 合成工具
            elif rl.IsKeyPressed(rl.KEY_EIGHT):
                self._craft_panel_action("tool", "saw")
            elif rl.IsKeyPressed(rl.KEY_NINE):
                self._craft_panel_action("tool", "hammer")
            elif rl.IsKeyPressed(rl.KEY_ZERO):
                self._craft_panel_action("tool", "furnace")
            return

        # T 键打开科技面板
        if rl.IsKeyPressed(rl.KEY_T) and self.state == GameState.PLAYING:
            self.state = GameState.TECH_PANEL

        # Esc 退出科技面板
        if rl.IsKeyPressed(rl.KEY_ESCAPE) and self.state == GameState.TECH_PANEL:
            self.state = GameState.PLAYING

        # 数字键: 科技面板内研究科技，游戏内选择塔类型
        if self.state == GameState.TECH_PANEL:
            techs = list(config.TECH_TREE.keys())
            for i, tech_name in enumerate(techs):
                if rl.IsKeyPressed(rl.KEY_ONE + i) and i < 5:
                    self._research_tech(tech_name)
        elif self.state == GameState.PLAYING:
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
        """加工面板内的操作"""
        if action_type == "process":
            recipe = PROCESS_RECIPES.get(target)
            if not recipe:
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
            if can_craft_weapon(target, self.inventory):
                craft_weapon(target, self.inventory)
                wdef = WEAPON_TYPES.get(target)
                self.toast(f"合成 {wdef.label_cn} 成功")
            else:
                wdef = WEAPON_TYPES.get(target)
                self.toast(f"无法合成 {wdef.label_cn}: 材料不足")

        elif action_type == "tool":
            if can_craft_tool(target, self.inventory):
                craft_tool(target, self.inventory)
                tdef = TOOL_TYPES.get(target)
                self.toast(f"合成 {tdef.label_cn} 成功")
            else:
                tdef = TOOL_TYPES.get(target)
                self.toast(f"无法合成 {tdef.label_cn}: 材料不足")

    def _select_tower_type(self, type_name):
        """选择塔类型"""
        tower_type = TOWER_TYPES.get(type_name)
        if not tower_type:
            return

        # 检查科技前置
        if tower_type.requires_tech:
            missing = [t for t in tower_type.requires_tech if t not in self.unlocked_tech]
            if missing:
                self.toast(f"需要科技: {', '.join(missing)}  (按 T 研究)", 2.5)
                return  # 未解锁

        self.selected_tower_type = tower_type
        self.placing_tower = True

    def _select_weapon(self, weapon_name):
        """选择武器装备放置"""
        wdef = WEAPON_TYPES.get(weapon_name)
        if not wdef:
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
        y = self.universe.get_surface_height(x, z) + 1
        tower = Tower(self.selected_tower_type, (x + 0.5, y, z + 0.5),
                      damage_mult=self.tech_damage_mult())
        self.towers.append(tower)
        self.money -= cost

        # 取消放置模式
        self.placing_tower = False
        self.selected_tower_type = None

    def _is_valid_placement(self, x, z):
        """检查放置位置是否有效（无限地形，无边界限制）"""
        # 不能放在海洋/水下
        biome = self.universe.get_biome_safe(x, z)
        if biome == "ocean":
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

        # 渲染科技面板
        if self.state == GameState.TECH_PANEL:
            self.ui.render_tech_panel(self)

        # 渲染加工面板
        if self.craft_panel_open:
            self.ui.render_craft_panel(self)

        # 渲染游戏结束
        if self.state == GameState.GAME_OVER:
            self.ui.render_game_over(self)

        end_drawing()

    def is_running(self):
        """游戏是否继续运行 — 始终返回 True，退出靠关闭窗口"""
        return True
