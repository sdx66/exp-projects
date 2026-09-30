"""
game.py - 主游戏循环与状态机
管理：游戏状态、波次系统、塔放置、敌人管理、输入处理
"""

import raylib as rl
import config
from entities import Tower, Enemy, Projectile, TOWER_TYPES, ENEMY_TYPES
from physics import distance_2d, clamp
from rl_utils import *


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

        # 游戏状态
        self.state = GameState.PLAYING
        self.money = config.STARTING_MONEY
        self.lives = config.STARTING_LIVES
        self.wave = 0
        self.score = 0

        # 实体
        self.towers = []
        self.enemies = []
        self.projectiles = []

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

        # 输入
        self.keys = {}
        self.mouse_x = 0
        self.mouse_y = 0

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
        self.lives = config.STARTING_LIVES
        self.wave = 0
        self.score = 0
        self.towers = []
        self.enemies = []
        self.projectiles = []
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
        all_paths = self.universe.enemy_paths if self.universe.enemy_paths else [self.universe.enemy_path]
        chosen_path = Enemy.pick_path(enemy_type, all_paths, existing_enemies=self.enemies, rng=self.universe.rng)

        # 位置：所选路径的起点
        if chosen_path is not None:
            start = chosen_path.points[0]
        elif self.universe.enemy_path:
            start = self.universe.enemy_path[0]
        else:
            start = (0, 0, 0)

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
        """击杀结算：金钱 + 分数"""
        self.money += enemy.reward
        self.score += enemy.reward * 2

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
        self.universe.update_weather()

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
            if result == "reached":
                self.lives -= 1
                if self.lives <= 0:
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
            if rl.IsKeyPressed(rl.KEY_ONE):
                self._select_tower_type("stone_thrower")
            elif rl.IsKeyPressed(rl.KEY_TWO):
                self._select_tower_type("water_cannon")
            elif rl.IsKeyPressed(rl.KEY_THREE):
                self._select_tower_type("sand_trap")
            elif rl.IsKeyPressed(rl.KEY_FOUR):
                self._select_tower_type("metal_tower")
            elif rl.IsKeyPressed(rl.KEY_ESCAPE):
                self.placing_tower = False
                self.selected_tower_type = None

            # 鼠标点击: 放置塔，或点击已有塔升级
            if rl.IsMouseButtonPressed(rl.MOUSE_BUTTON_LEFT):
                if self.placing_tower and self.selected_tower_type:
                    self._try_place_tower()
                else:
                    self._try_upgrade_tower()

    def _try_upgrade_tower(self):
        """点击已有塔进行升级"""
        wx, wy, wz = self.camera.screen_to_world(rl.GetMouseX(), rl.GetMouseY())
        x, z = int(wx), int(wz)
        for tower in self.towers:
            if abs(tower.position[0] - x) <= 1 and abs(tower.position[2] - z) <= 1:
                if tower.level >= config.MAX_TOWER_LEVEL:
                    return
                cost = tower.upgrade_cost
                if self.money < cost:
                    return
                tower.upgrade()
                self.money -= cost
                return

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
        x, z = int(wx), int(wz)

        # 检查位置有效性
        if not self._is_valid_placement(x, z):
            return

        # 检查金钱
        cost = self.tower_cost(self.selected_tower_type)
        if self.money < cost:
            return

        # 放置塔（应用科技伤害加成）
        y = self.universe.get_surface_height(x, z) + 1
        tower = Tower(self.selected_tower_type, (x, y, z),
                      damage_mult=self.tech_damage_mult())
        self.towers.append(tower)
        self.money -= cost

        # 取消放置模式
        self.placing_tower = False
        self.selected_tower_type = None

    def _is_valid_placement(self, x, z):
        """检查放置位置是否有效"""
        size = self.universe.size
        if x < 0 or x >= size or z < 0 or z >= size:
            return False

        # 不能放在路径上
        for px, py, pz in self.universe.enemy_path:
            if abs(px - x) < 1 and abs(pz - z) < 1:
                return False

        # 不能放在已有塔的位置
        for tower in self.towers:
            if abs(tower.position[0] - x) < 1 and abs(tower.position[2] - z) < 1:
                return False

        return True

    def render(self):
        """渲染游戏"""
        begin_drawing()

        # 渲染世界
        entities = {
            "towers": self.towers,
            "enemies": self.enemies,
            "projectiles": self.projectiles,
        }
        self.renderer.render(self.camera, self.universe, entities)

        # 渲染放置预览
        if self.placing_tower and self.selected_tower_type:
            self._render_placement_preview()

        # 渲染 HUD
        self._render_hud()

        # 渲染波次公告
        if self.wave_announcement_timer > 0:
            self._render_wave_announcement()

        # 渲染科技面板
        if self.state == GameState.TECH_PANEL:
            self._render_tech_panel()

        # 渲染游戏结束
        if self.state == GameState.GAME_OVER:
            self._render_game_over()

        end_drawing()

    def _render_placement_preview(self):
        """渲染塔放置预览"""
        wx, wy, wz = self.camera.screen_to_world(rl.GetMouseX(), rl.GetMouseY())
        x, z = int(wx), int(wz)
        valid = self._is_valid_placement(x, z)
        cost = self.selected_tower_type.base_cost
        can_afford = self.money >= cost

        c = color(100, 255, 100) if valid and can_afford else color(255, 100, 100)
        alpha = 0.5 if valid and can_afford else 0.3
        set_alpha(alpha)
        draw_cube(vec3(x + 0.5, self.universe.get_surface_height(x, z) + 1.0, z + 0.5),
                  0.6, 1.0, 0.6, c)
        set_alpha(1.0)

        # 绘制射程圈
        if valid:
            set_alpha(0.2)
            draw_circle_v(vec2(x + 0.5, z + 0.5),
                          self.selected_tower_type.base_range, color(100, 200, 100))
            set_alpha(1.0)

    def _render_hud(self):
        """渲染 HUD"""
        # 顶部信息栏
        draw_rect(0, 0, rl.GetScreenWidth(), 40, color(20, 20, 30, 200))

        draw_text(f"Wave: {self.wave}", 10, 12, 14, color(220, 220, 240))
        draw_text(f"Money: {self.money}", 120, 12, 14, color(255, 215, 0))
        draw_text(f"Lives: {self.lives}", 250, 12, 14, color(255, 80, 80))
        draw_text(f"Score: {self.score}", 360, 12, 14, color(180, 255, 180))
        draw_text(f"FPS: {self.fps}", rl.GetScreenWidth() - 100, 12, 14, color(150, 150, 175))

        # 塔选择栏 — 未解锁的塔标记为锁
        bar_y = rl.GetScreenHeight() - 60
        draw_rect(0, bar_y, rl.GetScreenWidth(), 50, color(30, 30, 40, 200))

        labels = [("1", "Stone"), ("2", "Water"), ("3", "Sand"), ("4", "Metal")]
        keys = ["stone_thrower", "water_cannon", "sand_trap", "metal_tower"]
        parts = ["Towers:"]
        for (key, label), type_name in zip(labels, keys):
            tt = TOWER_TYPES[type_name]
            if tt.requires_tech:
                missing = [t for t in tt.requires_tech if t not in self.unlocked_tech]
                if missing:
                    parts.append(f"[{key}]{label}🔒")
                    continue
            parts.append(f"[{key}]{label}")
        draw_text(" ".join(parts), 10, bar_y + 8, 12, color(180, 180, 200))
        draw_text("V:2D/3D | T:Tech | Esc:Cancel | Click:Place/Upgrade", 10, bar_y + 28, 11, color(150, 150, 175))

        # 当前选择
        if self.selected_tower_type:
            tc = color(*self.selected_tower_type.color)
            draw_circle_v(vec2(rl.GetScreenWidth() - 100, bar_y + 15), 8, tc)
            draw_text(f"${self.tower_cost(self.selected_tower_type)}",
                      rl.GetScreenWidth() - 85, bar_y + 10, 12, color(255, 215, 0))

        # 正在研究科技
        if self.tech_researching:
            frac = 1.0 - self.tech_research_timer / max(1e-6, self.tech_research_total)
            draw_text(f"Researching: {self.tech_researching} ({frac*100:.0f}%)",
                      10, bar_y - 22, 12, color(255, 200, 100))

        # 屏幕提示
        if self.toast_timer > 0 and self.toast_message:
            draw_text_centered(self.toast_message, bar_y - 22, 14, color(255, 220, 140))

    def _render_wave_announcement(self):
        """渲染波次公告"""
        alpha = min(1.0, self.wave_announcement_timer)
        set_alpha(alpha)
        draw_text_centered(f"WAVE {self.wave}",
                           rl.GetScreenHeight() // 2 - 20, 36, color(255, 255, 255))
        if self.between_waves:
            draw_text_centered(f"Next wave in {self.wave_timer:.1f}s",
                               rl.GetScreenHeight() // 2 + 20, 16, color(200, 200, 220))
        else:
            draw_text_centered(f"Enemies: {self.wave_enemy_count}/{self.wave_max_enemies}",
                               rl.GetScreenHeight() // 2 + 20, 16, color(200, 200, 220))
        set_alpha(1.0)

    # 科技说明（用于面板展示）
    TECH_DESC = {
        "handcraft": "塔伤害 +10%",
        "stone_mining": "建造费用 -15%",
        "biomass_energy": "前置: 水机械学",
        "water_mechanics": "解锁 [2] Water Cannon",
        "metallurgy": "解锁 [4] Metal Tower",
    }

    def _render_tech_panel(self):
        """渲染科技面板"""
        # 半透明背景
        set_alpha(0.7)
        draw_rect(0, 0, rl.GetScreenWidth(), rl.GetScreenHeight(), color(0, 0, 0))
        set_alpha(1.0)

        # 面板
        pw = 680
        ph = 420
        px = (rl.GetScreenWidth() - pw) // 2
        py = (rl.GetScreenHeight() - ph) // 2

        draw_rect(px, py, pw, ph, color(40, 45, 60))
        draw_rect_lines(rect(px, py, pw, ph), 2, color(100, 150, 200))

        draw_text_centered("TECHNOLOGY TREE", py + 20, 24, color(220, 220, 240))
        draw_text(f"Money: {self.money}   |   Press 1-5 to research   |   T/Esc: close",
                  px + 10, py + ph - 30, 12, color(150, 150, 175))

        # 已解锁科技
        tech_y = py + 62
        draw_text(f"Unlocked: {', '.join(self.unlocked_tech) if self.unlocked_tech else 'None'}",
                  px + 10, tech_y, 14, color(180, 255, 180))
        tech_y += 28

        draw_text("Available Research:", px + 10, tech_y, 14, color(200, 200, 220))
        tech_y += 22

        for i, (tech, defn) in enumerate(config.TECH_TREE.items()):
            if tech in self.unlocked_tech:
                mark, tc = "[OK]", color(180, 255, 180)
                detail = self.TECH_DESC.get(tech, "")
            elif self.tech_researching == tech:
                frac = 1.0 - self.tech_research_timer / max(1e-6, self.tech_research_total)
                mark = f"[{frac*100:3.0f}%]"
                tc = color(255, 200, 100)
                detail = f"研究中 {self.tech_research_timer:.1f}s / {defn['time']}s"
            else:
                missing = [r for r in defn["requires"] if r not in self.unlocked_tech]
                if missing:
                    mark, tc = "[ - ]", color(90, 90, 110)
                    detail = f"需要: {', '.join(missing)}"
                elif self.tech_researching:
                    mark, tc = "[ - ]", color(90, 90, 110)
                    detail = "已有研究进行中"
                elif self.money < defn["cost"]:
                    mark, tc = "[ $ ]", color(255, 120, 120)
                    detail = f"钱不够 (需 {defn['cost']})"
                else:
                    mark, tc = "[  ]", color(255, 240, 160)
                    detail = f"费用 {defn['cost']} / {defn['time']}s"
                detail += "  " + self.TECH_DESC.get(tech, "")

            draw_text(f"  [{i+1}] {mark} {tech:<18} {detail}", px + 10, tech_y, 13, tc)
            tech_y += 30

        # 正在研究时画进度条
        if self.tech_researching:
            frac = 1.0 - self.tech_research_timer / max(1e-6, self.tech_research_total)
            bar_w = pw - 40
            draw_rect(px + 20, tech_y + 8, bar_w, 10, color(60, 60, 75))
            draw_rect(px + 20, tech_y + 8, int(bar_w * frac), 10, color(255, 200, 100))

    def _render_game_over(self):
        """渲染游戏结束界面"""
        set_alpha(0.8)
        draw_rect(0, 0, rl.GetScreenWidth(), rl.GetScreenHeight(), color(0, 0, 0))
        set_alpha(1.0)

        draw_text_centered("GAME OVER", rl.GetScreenHeight() // 2 - 40, 40, color(255, 80, 80))
        draw_text_centered(f"Final Score: {self.score}", rl.GetScreenHeight() // 2 + 10, 24, color(220, 220, 240))
        draw_text_centered(f"Reached Wave: {self.wave}", rl.GetScreenHeight() // 2 + 45, 16, color(180, 180, 200))
        draw_text_centered("Press Enter to restart", rl.GetScreenHeight() // 2 + 80, 14, color(150, 150, 175))

    def is_running(self):
        """游戏是否继续运行 — 始终返回 True，退出靠关闭窗口"""
        return True
