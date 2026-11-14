"""
ui_renderer.py - UI 渲染器
从 Game 类中提取的所有 UI 渲染逻辑（HUD、面板、公告、结束界面）
"""

import raylib as rl
import math
import config
import gamedata
import ui_config
import save_system
from entities import Tower, Enemy, Projectile, Pickup, Base, TOWER_TYPES, ENEMY_TYPES
from items import (
    MATERIAL_NAMES_CN, MATERIAL_COLORS,
    WEAPON_TYPES, TOOL_TYPES, PROCESS_RECIPES,
    can_craft_weapon, can_craft_tool, item_volume,
)
from blocks import BLOCK_NAME, get_block_by_name, get_placeable_blocks
import belts as belts_mod
from belts import Belt, DXDZ, BRIDGE_LANES
import power as power_mod
from rl_utils import *


def _fmt_power(w):
    """功率显示：<1kW→W，<1MW→kW，否则 MW。"""
    if w >= 1_000_000:
        return f"{w / 1_000_000:.2f}MW"
    if w >= 1000:
        return f"{w / 1000:.1f}kW"
    return f"{w:.0f}W"


class UIRenderer:
    """游戏 UI 渲染器，与 Game 分离以保持单一职责"""

    def render_hud(self, game):
        """渲染 HUD（顶部信息栏 + 材料栏 + 塔选择栏）"""
        screen_w = rl.GetScreenWidth()
        screen_h = rl.GetScreenHeight()
        uc = ui_config

        # 顶部信息栏
        draw_rect(0, 0, screen_w, uc.HUD_TOP_HEIGHT, color(*uc.HUD_TOP_BG))
        active_count = sum(1 for rt in game.spawners.values() if rt.mode == "active")
        scouting_count = sum(1 for rt in game.spawners.values() if rt.mode == "scouting")
        dormant_count = sum(1 for rt in game.spawners.values() if rt.mode == "dormant")
        # L0 可达性（D1a）：受阻点计数（活跃且全部类型不可达）
        blocked_count = sum(
            1 for rt in game.spawners.values()
            if rt.mode == "active" and rt.enemy_types
            and set(rt.enemy_types) <= set(rt.blocked_types))
        blocked_txt = f"  !{blocked_count} blocked" if blocked_count else ""
        draw_text(f"Spawners: {active_count}A/{scouting_count}S/{dormant_count}D{blocked_txt}  (P:详情)",
                   uc.HUD_WAVE_X, 12, uc.HUD_TEXT_SIZE, color(*uc.HUD_TEXT_WHITE))
        draw_text(f"Money: {game.money}", uc.HUD_MONEY_X, 12, uc.HUD_TEXT_SIZE, color(*uc.HUD_TEXT_MONEY))

        # 大本营血条
        hp_ratio = game.base.health / game.base.max_health
        bx, by, bw, bh = uc.HUD_BASE_BAR_X, uc.HUD_BASE_BAR_Y, uc.HUD_BASE_BAR_W, uc.HUD_BASE_BAR_H
        # 背景（深色槽）
        draw_rect(bx, by, bw, bh, color(55, 28, 28))
        # 边框
        draw_rect_lines(rect(bx, by, bw, bh), 1, color(130, 75, 75))
        # 前景（三色渐变：红 <20% 橙 <50% 绿 >=50%）
        if hp_ratio < 0.2:
            hp_color = color(230, 70, 70)
        elif hp_ratio < 0.5:
            hp_color = color(230, 190, 70)
        else:
            hp_color = color(90, 220, 90)
        draw_rect(bx + 1, by + 1,
                  max(1, int((bw - 2) * hp_ratio)), bh - 2, hp_color)
        # 数值文字（叠在血条上，白色带描边效果）
        txt = f"Base: {game.base.health}/{game.base.max_health}"
        draw_text(txt, bx + 3, by + 3, uc.HUD_TINY_TEXT_SIZE,
                  color(*uc.HUD_TEXT_WHITE))

        draw_text(f"Score: {game.score}", uc.HUD_SCORE_X, 12, uc.HUD_TEXT_SIZE, color(*uc.HUD_TEXT_SCORE))
        draw_text(f"FPS: {game.fps:.1f}", screen_w - uc.HUD_FPS_X_OFFSET, 12, uc.HUD_TEXT_SIZE, color(*uc.HUD_TEXT_FPS))

        # 顶栏倍速分段控件（右对齐，位于 FPS 左侧；点击循环升档）
        self._render_speed_bar(game, screen_w)

        # 材料栏
        draw_rect(0, uc.HUD_TOP_HEIGHT, screen_w, uc.HUD_MATERIAL_HEIGHT, color(*uc.HUD_MATERIAL_BG))
        mat_x = uc.HUD_MAT_X_START
        for mat_name in ("dirt", "wood", "stone", "sand", "iron_ore", "plank", "cobble", "iron_ingot"):
            amt = game.inventory.get_material(mat_name)
            if amt > 0:
                mc = MATERIAL_COLORS.get(mat_name, (200, 200, 200))
                cn = MATERIAL_NAMES_CN.get(mat_name, mat_name)
                label = f"{cn}:{amt}"
                draw_text(label, mat_x, uc.HUD_MAT_Y, uc.HUD_SMALL_TEXT_SIZE, color(*mc))
                mat_x += measure_text(label, uc.HUD_SMALL_TEXT_SIZE) + 12

        # 武器/工具栏（第 2 行，与材料行分离，避免重叠）
        w_mat_x = uc.HUD_MAT_X_START
        for wname, wdef in WEAPON_TYPES.items():
            count = game.inventory.get_weapon(wname)
            if count > 0:
                wc = wdef.color
                label = f"{wdef.label_cn}:{count}"
                draw_text(label, w_mat_x, uc.HUD_MAT_Y2, uc.HUD_SMALL_TEXT_SIZE, color(*wc))
                w_mat_x += measure_text(label, uc.HUD_SMALL_TEXT_SIZE) + 12
        if game.inventory.tools:
            tool_str = f"Tools:{', '.join(sorted(game.inventory.tools))}"
            draw_text(tool_str, screen_w - uc.HUD_TOOL_X_OFFSET, uc.HUD_MAT_Y2,
                      uc.HUD_TINY_TEXT_SIZE, color(*uc.HUD_TEXT_TOOL))

        # 塔选择栏（分段按钮：支持鼠标点击选中，用于无热键的防御塔）
        bar_y = screen_h - uc.HUD_BAR_Y_OFFSET
        draw_rect(0, bar_y, screen_w, uc.HUD_BAR_HEIGHT, color(*uc.HUD_BAR_BG))

        if game.interaction_mode in ("place", "belt", "power"):
            # 放置/布带/电力模式：塔栏让位给各自选择栏（各自渲染在下方单独方法里）
            self._tower_bar_rects = []
        else:
            self._tower_bar_rects = []
            seg_x = 10
            draw_text("Towers:", seg_x, bar_y + 8, uc.HUD_SMALL_TEXT_SIZE, color(*uc.HUD_TEXT_TOOL))
            seg_x += measure_text("Towers:", uc.HUD_SMALL_TEXT_SIZE) + 10
            for key, label, type_name in zip(uc.TOWER_BAR_NUMS, uc.TOWER_BAR_LABELS,
                                              uc.TOWER_BAR_KEYS):
                tt = TOWER_TYPES[type_name]
                missing = [t for t in tt.requires_tech if not game.has_tech(t)]
                seg_text = f"[{key}]{label}" + (" (lock)" if missing else "")
                seg_w = measure_text(seg_text, uc.HUD_SMALL_TEXT_SIZE) + 8
                seg_y, seg_h = bar_y + 5, 20
                selected = (game.selected_tower_type is tt)
                if missing:
                    seg_bg = uc.TOWER_BAR_LOCK_BG
                elif selected:
                    seg_bg = uc.TOWER_BAR_SELECTED_BG
                else:
                    seg_bg = uc.TOWER_BAR_SEG_BG
                draw_rect(seg_x, seg_y, seg_w, seg_h, color(*seg_bg))
                if selected:
                    draw_rect_lines(rect(seg_x, seg_y, seg_w, seg_h), 1,
                                    color(*uc.TOWER_BAR_SELECTED_BORDER))
                seg_color = uc.TOWER_BAR_LOCK_TEXT if missing else uc.HUD_TEXT_TOOL
                draw_text(seg_text, seg_x + 4, seg_y + 4, uc.HUD_SMALL_TEXT_SIZE,
                          color(*seg_color))
                self._tower_bar_rects.append((seg_x, seg_y, seg_w, seg_h, type_name))
                seg_x += seg_w + 4

        if game.interaction_mode == "dig":
            draw_text("[R] Digging: ON | Click/Hold to break top block and harvest",
                      10, bar_y + 28, uc.HUD_TINY_TEXT_SIZE, color(180, 255, 180))
        elif game.interaction_mode == "place":
            draw_text("[F] Placing: ON | [1]泥土 [2]石头 [3]沙子 [4]原木 | "
                      "Click to place (consumes material) | Esc:cancel",
                      10, bar_y + 28, uc.HUD_TINY_TEXT_SIZE, color(180, 255, 180))
        elif game.interaction_mode == "belt":
            draw_text("[K] Belts: ON | [1]传送带 [2]均分器 [3]立交桥 | "
                      "[R]旋转方向 | Click to place | Esc:cancel",
                      10, bar_y + 28, uc.HUD_TINY_TEXT_SIZE, color(180, 255, 180))
        elif game.interaction_mode == "power":
            draw_text("[E] Power: ON | [1..9]选择电线/电站/电池 | "
                      "Click to place | Esc:cancel",
                      10, bar_y + 28, uc.HUD_TINY_TEXT_SIZE, color(180, 255, 180))
        elif game.interaction_mode == "dismantle":
            draw_text("[X] Dismantle: ON | Click tower to remove", 10, bar_y + 28,
                      uc.HUD_TINY_TEXT_SIZE, color(255, 120, 120))
        elif game.interaction_mode == "unmount":
            draw_text("[U] Unmount: ON | Click wall tower to take weapons back "
                      "(Shift = all)", 10, bar_y + 28,
                      uc.HUD_TINY_TEXT_SIZE, color(255, 190, 120))
        elif game.craft_panel_open:
            draw_text("[B] Craft Panel Open", 10, bar_y + 28,
                      uc.HUD_TINY_TEXT_SIZE, color(255, 200, 100))
        elif game.selected_tower_type and game.selected_tower_type.no_attack:
            if getattr(game.selected_tower_type, "is_mining", False):
                draw_text(f"Placing {game.selected_tower_type.name_cn} | "
                          f"footprint {game.selected_mining_footprint[0]}x"
                          f"{game.selected_mining_footprint[1]} | "
                          f"[[]] 宽 [-] 长 | needs mining tool inside | "
                          f"click to place | Esc:cancel",
                          10, bar_y + 28, uc.HUD_TINY_TEXT_SIZE, color(200, 225, 190))
            else:
                draw_text(f"Placing {game.selected_tower_type.name_cn} (no attack) | "
                          f"slots {game.selected_tower_type.weapon_slots} | "
                          f"after placing, click it with a weapon to mount | "
                          f"[U] take weapons back",
                          10, bar_y + 28, uc.HUD_TINY_TEXT_SIZE, color(200, 205, 225))
        elif game.selected_weapon:
            draw_text(f"Placing: {game.selected_weapon.label_cn} (${game.selected_weapon.install_fee}) | Esc:cancel",
                      10, bar_y + 28, uc.HUD_TINY_TEXT_SIZE, color(180, 255, 180))
        else:
            draw_text("V:2D/3D | T:Tech | B:Craft | H:Weapons | X:Dismantle | "
                      "U:Unmount | R:Dig | F:Place | L:Lab | F8:存档 | M:小地图 | "
                      "';'/','倍速 | Click:Harvest/Upgrade | "
                      "Shift+Click:强制升级(加工/仓储塔)",
                      10, bar_y + 28, uc.HUD_TINY_TEXT_SIZE, color(*uc.HUD_TEXT_DIM))

        # 放置模式：方块选择栏（复用塔栏分段样式）
        if game.interaction_mode == "place":
            self._render_block_bar(game, bar_y)

        # 布带模式：传送带选择栏
        if game.interaction_mode == "belt":
            self._render_belt_bar(game, bar_y)

        # 电力模式：电线/电站/电池选择栏 + 电网摘要
        if game.interaction_mode == "power":
            self._render_power_bar(game, bar_y)
            self._render_power_summary(game, bar_y)

        # 当前选择
        if game.selected_tower_type:
            tc = color(*game.selected_tower_type.color)
            draw_circle_v(vec2(screen_w - 100, bar_y + 15), 8, tc)
            draw_text(f"${game.tower_cost(game.selected_tower_type)}",
                      screen_w - 85, bar_y + 10, uc.HUD_SMALL_TEXT_SIZE, color(*uc.HUD_TEXT_MONEY))

        # 正在研究科技（每条研究通道逐行完整显示：塔号/手研 + 科技名 + 进度）
        try:
            lanes = game.research_lanes()
        except AttributeError:
            lanes = []
        busy_lines = []
        for i, lane in enumerate(lanes):
            cur = lane.get("current")
            who = "手研" if lane.get("hand") else f"T{i + 1}"
            if cur:
                frac = 1.0 - lane.get("timer", 0.0) / max(1e-6, lane.get("total", 1.0))
                import tech_tree as tt
                name = tt.tech_tree().node_name(cur)
                line = f"研究 {who}: {name} {frac*100:.0f}%"
                q = lane.get("queue") or ()
                if q:
                    line += f" (+{len(q)}排队)"
                busy_lines.append(line)
            elif lane.get("blocked"):
                # 无在研但排队中被阻塞的通道也显示（⛔ + 折叠原因）
                brief = str(lane.get("blocked"))
                busy_lines.append(f"研究 {who}: ⛔{brief[:10]}…" if len(brief) > 10
                                  else f"研究 {who}: ⛔{brief}")
        max_lines = uc.HUD_RESEARCH_MAX_LINES
        shown = busy_lines[:max_lines]
        for i, line in enumerate(shown):
            draw_text(line, 10, bar_y - 22 - i * 15,
                      uc.HUD_SMALL_TEXT_SIZE, color(255, 200, 100))
        if len(busy_lines) > max_lines:
            draw_text(f"… 共{len(busy_lines)}条研究通道",
                      10, bar_y - 22 - max_lines * 15,
                      uc.HUD_SMALL_TEXT_SIZE, color(255, 200, 100))

        # 屏幕提示（如有研究通道行，toast 上移避开，保证信息完整可读）
        toast_y = bar_y - 22
        if busy_lines:
            block_lines = len(shown) + (1 if len(busy_lines) > max_lines else 0)
            toast_y = bar_y - 22 - block_lines * 15 - 6
        if game.toast_timer > 0 and game.toast_message:
            draw_text_centered(game.toast_message, toast_y, 14, color(255, 220, 140))

    def _tower_hover_text(self, tower):
        """塔悬停标签文本：(名称行, 细节行)。

        塔多后光看方块认不清是哪座（投石/水炮/城墙/仓储/加工/研究/开采/电力
        全是方块+颜色），沿鼠标给一行"塔名 + 关键参数/职责"说明，不改塔外观。
        """
        td = tower.type_def
        name = td.name_cn
        lv = f" Lv{tower.level}" if tower.level > 1 else ""
        # 职责/参数摘要（按塔类特化，复用现有中文文案与字号）
        if tower.is_power:
            if tower.power_kind == "station":
                det = f"电站 · 发电 {_fmt_power(getattr(tower, 'generation_w', 0.0))}"
            else:
                cap = max(1e-6, float(getattr(tower, "cell_energy_j", 1.0)))
                frac = min(1.0, float(getattr(tower, "charge_j", 0.0)) / cap)
                det = f"电池 · 储能 {frac * 100:.0f}%"
        elif getattr(tower, "is_mining", False):
            fp = getattr(tower, "footprint", None)
            m, n = (int(fp[0]), int(fp[1])) if fp else (3, 3)
            det = f"开采塔 · 占地{m}×{n}格 · 竖井开采"
        elif getattr(tower, "is_research", False):
            det = f"研究塔 · 学识{getattr(tower.type_def, 'research_attr', 1)}"
        elif tower.processing_capacity:
            det = f"加工塔 · 容量{tower.processing_capacity}"
        elif tower.storage_capacity:
            det = f"仓储塔 · 容量{tower.storage_capacity}"
        elif not getattr(tower, "no_attack", False):
            det = (f"攻击塔 · 伤害{td.base_damage} 射程{td.base_range}"
                   + (f" 减速{td.slow_factor}" if td.slow_factor else "")
                   + (f" 溅射{td.splash_radius}" if td.splash_radius else ""))
        else:
            det = f"防御塔 · 可挂武器{getattr(tower.type_def, 'weapon_slots', 0)}"
        mounts = getattr(tower, "weapon_mounts", ())
        if mounts:
            det += f" · 挂载{len(mounts)}件武器"
        return f"{name}{lv}", det

    def render_tower_hover(self, game):
        """鼠标指向世界里的塔时，光标旁显示塔名+职责说明（不改外观）。

        仅在正常游玩态绘制：任意面板/模式/小地图/菜单打开时不显示。
        """
        uc = ui_config
        if getattr(game, "state", None) != "playing":
            return
        if (game.processing_panel_open or game.storage_panel_open
                or game.weapon_panel_open or game.craft_panel_open
                or game.spawner_panel_open or game.settings_panel_open
                or game.placing_tower or game.selected_weapon):
            return
        # 布带/电力/放置/挖掘模式下有各自的摆放预览，悬停标签会让画面叠加过
        # 于拥挤；拆除/取下武器时正是需要认塔的场景，保留显示。
        if game.interaction_mode in ("place", "belt", "power", "dig", "place_classic"):
            return
        if game._cursor_on_minimap():
            return
        mx, my = rl.GetMouseX(), rl.GetMouseY()
        if not (0 <= mx < rl.GetScreenWidth() and 0 <= my < rl.GetScreenHeight()):
            return
        # 悬停世界格缓存：3D 射线每帧全量拾取偏贵，鼠标停在同格时复用
        cell_key = (mx // 2, my // 2)
        cached = getattr(self, "_tower_hover_cache", None)
        if cached is not None and cached[0] == cell_key:
            tower = cached[1]
        else:
            wx, _wy, wz = game.camera.screen_to_world(mx, my)
            tower = game._tower_at_cell(math.floor(wx), math.floor(wz), tol=0.6)
            self._tower_hover_cache = (cell_key, tower)
        if tower is None:
            return
        line1, line2 = self._tower_hover_text(tower)
        pad = uc.TOWER_HOVER_PAD
        fs1 = uc.TOWER_HOVER_NAME_SIZE
        fs2 = uc.TOWER_HOVER_DETAIL_SIZE
        w1 = measure_text(line1, fs1)
        w2 = measure_text(line2, fs2)
        w = max(w1, w2) + pad * 2
        h = fs1 + fs2 + pad * 2 + 4
        ox, oy = uc.TOWER_HOVER_OFFSET
        x = min(mx + ox, rl.GetScreenWidth() - w - 4)
        y = min(my + oy, rl.GetScreenHeight() - h - 4)
        x = max(4, x)
        y = max(4, y)
        draw_rect(x, y, w, h, color(*uc.TOWER_HOVER_BG))
        draw_rect_lines(rect(x, y, w, h), 1, color(*uc.TOWER_HOVER_BORDER))
        draw_text(line1, x + pad, y + pad, fs1, color(*uc.TOWER_HOVER_NAME))
        draw_text(line2, x + pad, y + pad + fs1 + 2, fs2, color(*uc.TOWER_HOVER_DETAIL))

    def render_wave_announcement(self, game):
        """渲染波次公告（显示全局阶段 + 刷怪点状态摘要）"""
        screen_h = rl.GetScreenHeight()
        uc = ui_config

        alpha = min(1.0, game.wave_announcement_timer)
        set_alpha(alpha)
        draw_text_centered(f"WAVE {game.wave}",
                           screen_h // 2 - 20, uc.WAVE_ANNOUNCE_TITLE_SIZE, color(255, 255, 255))
        # 刷怪点状态摘要
        active = sum(1 for rt in game.spawners.values() if rt.mode == "active")
        scouting = sum(1 for rt in game.spawners.values() if rt.mode == "scouting")
        dormant = sum(1 for rt in game.spawners.values() if rt.mode == "dormant")
        detail = f"Active: {active} | Scouting: {scouting} | Dormant: {dormant}"
        draw_text_centered(detail, screen_h // 2 + 20,
                           uc.WAVE_ANNOUNCE_DETAIL_SIZE, color(200, 200, 220))
        set_alpha(1.0)

    def tower_hit_type(self, mx, my):
        """塔选择栏鼠标命中：返回被点中的塔类型名（可放置时）或 None。

        布局与 render_hud 的塔栏分段完全同构。锁定塔(科技未解锁)返回 None，
        由调用方 toast 提示。
        """
        for rx, ry, rw, rh, type_name in getattr(self, "_tower_bar_rects", []):
            if rx <= mx <= rx + rw and ry <= my <= ry + rh:
                return type_name
        return None

    def _render_speed_bar(self, game, screen_w):
        """顶栏倍速分段控件：全部挡位并排，当前档高亮，点击循环升档。

        写入 self._speed_bar_rects（与 _tower_bar_rects 同模式）供 speed_bar_hit
        命中测试。右对齐于 FPS 左侧，顶栏内垂直居中。
        """
        uc = ui_config
        self._speed_bar_rects = []
        speeds = config.GAME_SPEEDS
        y = uc.HUD_SPEED_Y
        h = uc.HUD_SPEED_H
        font = uc.HUD_SPEED_FONT

        # 标题 + 各段（自右向左排布，整条右缘对齐 FPS 左侧）
        title = "速度"
        title_w = measure_text(title, uc.HUD_SPEED_TITLE_FONT) + 8
        segs = []
        for s in speeds:
            label = f"{s:g}x"
            segs.append((label, measure_text(label, font) + 2 * uc.HUD_SPEED_PAD))
        total = title_w + uc.HUD_SPEED_GAP
        for _l, w in segs:
            total += w + uc.HUD_SPEED_GAP
        right = screen_w - uc.HUD_SPEED_RIGHT_OFFSET
        x = right - total

        draw_text(title, x, y + (h - uc.HUD_SPEED_TITLE_FONT) // 2,
                  uc.HUD_SPEED_TITLE_FONT, color(*uc.HUD_SPEED_TITLE_COLOR))
        x += title_w + uc.HUD_SPEED_GAP

        for i, (label, w) in enumerate(segs):
            selected = (i == game._speed_index)
            bg = uc.HUD_SPEED_SEL_BG if selected else uc.HUD_SPEED_BG
            draw_rect(x, y, w, h, color(*bg))
            if selected:
                draw_rect_lines(rect(x, y, w, h), 1,
                                color(*uc.HUD_SPEED_SEL_BORDER))
            tcol = uc.HUD_SPEED_SEL_TEXT if selected else uc.HUD_SPEED_TEXT
            draw_text(label, x + uc.HUD_SPEED_PAD,
                      y + (h - font) // 2, font, color(*tcol))
            self._speed_bar_rects.append((x, y, w, h))
            x += w + uc.HUD_SPEED_GAP

    def speed_bar_hit(self, mx, my):
        """顶栏倍速分段命中：落在任一挡位上返回 True（调用方升档）。"""
        for rx, ry, rw, rh in getattr(self, "_speed_bar_rects", []):
            if rx <= mx <= rx + rw and ry <= my <= ry + rh:
                return True
        return False

    def craft_hit_action(self, game, mx, my):
        """合成面板鼠标命中：返回 (action_type, target) 或 None。

        布局与 render_craft_panel 完全同构；行数超过数字键(1-9,0)后靠点击补齐。
        """
        uc = ui_config
        sw, sh = rl.GetScreenWidth(), rl.GetScreenHeight()
        pw, ph = uc.CRAFT_PANEL_W, uc.CRAFT_PANEL_H
        px = (sw - pw) // 2
        py = (sh - ph) // 2
        top = py + 50
        bot = py + ph - 32
        mid = pw // 2
        lx = px + 12
        rx = px + mid + 12
        lw = mid - 24           # 左列内容宽度
        rw = pw - mid - 24      # 右列内容宽度
        row_h = uc.CRAFT_PANEL_ITEM_SIZE + 8
        head_h = uc.CRAFT_PANEL_HEADER_SIZE + 8

        def in_rect(x, y, w, h):
            return x <= mx <= x + w and y <= my <= y + h

        # 左列：Processing 配方（每条占 2 行，命中第 1 行）
        y = top
        y += head_h  # Materials 标题
        for _mat in MATERIAL_NAMES_CN:
            if y > bot:
                break
            if game.inventory.get_material(_mat) > 0:
                y += row_h
        y += 8
        y += head_h  # Tools: 行
        if y + head_h < bot:
            y += head_h  # Processing: 标题
            for key, _recipe in PROCESS_RECIPES.items():
                if y + row_h * 2 > bot:
                    break
                if in_rect(lx, y, lw, row_h):
                    return ("process", key)
                y += row_h * 2

        # 右列：Weapons / Tools（各占 1 行）
        y = top
        y += head_h  # Craft Weapons: 标题
        for _name, _wdef in WEAPON_TYPES.items():
            if y > bot:
                break
            if in_rect(rx, y, rw, row_h):
                return ("weapon", _name)
            y += row_h
        y += 10
        if y + head_h < bot:
            y += head_h  # Craft Tools: 标题
            for tname, _tdef in TOOL_TYPES.items():
                if y > bot:
                    break
                if in_rect(rx, y, rw, row_h):
                    return ("tool", tname)
                y += row_h
        return None

    def render_weapon_panel(self, game):
        """武器库面板：点击选择一把武器用于放置（H 键打开）。"""
        uc = ui_config
        sw, sh = rl.GetScreenWidth(), rl.GetScreenHeight()
        set_alpha(uc.PANEL_BG_ALPHA)
        draw_rect(0, 0, sw, sh, color(*uc.PANEL_OVERLAY_COLOR))
        set_alpha(1.0)
        pw, ph = 400, min(620, sh - 60)
        px = (sw - pw) // 2
        py = (sh - ph) // 2
        draw_rect(px, py, pw, ph, color(35, 40, 55))
        draw_rect_lines(rect(px, py, pw, ph), 2, color(100, 180, 200))
        draw_text_centered("武器库 (ARMORY)", py + 22, 24, color(220, 220, 240))

        y = py + 55
        row_h = 30
        for wname, wdef in WEAPON_TYPES.items():
            if y + row_h > py + ph - 10:
                break
            owned = game.inventory.get_weapon(wname)
            missing = [t for t in (wdef.requires_tech or ())
                       if not game.has_tech(t)]
            if missing:
                txt = f"  [{wdef.label_cn}]  D{wdef.damage:.0f} R{wdef.range:.1f}  [需科技: {','.join(missing)}]"
                draw_text(txt, px + 12, y + 6, 14, color(90, 90, 110))
            elif owned >= 1:
                txt = (f"  [{wdef.label_cn}] x{owned}  D{wdef.damage:.0f} "
                       f"R{wdef.range:.1f} C{wdef.cooldown:.1f}  ${wdef.install_fee}")
                draw_text(txt, px + 12, y + 6, 14, color(180, 255, 180))
            else:
                txt = (f"  [{wdef.label_cn}] x0  D{wdef.damage:.0f} "
                       f"R{wdef.range:.1f} C{wdef.cooldown:.1f}  ${wdef.install_fee}")
                draw_text(txt, px + 12, y + 6, 14, color(150, 150, 175))
            y += row_h
        draw_text("H / Esc: close   点击选择武器放置", px + 10, py + ph - 26, 13,
                  color(150, 150, 175))

    def weapon_hit_action(self, game, mx, my):
        """武器库面板鼠标命中：返回武器名或 None（布局与 render_weapon_panel 一致）。"""
        uc = ui_config
        sw, sh = rl.GetScreenWidth(), rl.GetScreenHeight()
        pw, ph = 400, min(620, sh - 60)
        px = (sw - pw) // 2
        py = (sh - ph) // 2
        y = py + 55
        row_h = 30
        for wname, _wdef in WEAPON_TYPES.items():
            if y + row_h > py + ph - 10:
                break
            if px + 4 <= mx <= px + pw - 4 and y <= my <= y + row_h:
                return wname
            y += row_h
        return None

    def render_craft_panel(self, game):
        """渲染加工/合成面板"""
        screen_w = rl.GetScreenWidth()
        screen_h = rl.GetScreenHeight()
        uc = ui_config

        set_alpha(uc.PANEL_BG_ALPHA)
        draw_rect(0, 0, screen_w, screen_h, color(*uc.PANEL_OVERLAY_COLOR))
        set_alpha(1.0)

        pw, ph = uc.CRAFT_PANEL_W, uc.CRAFT_PANEL_H
        px = (screen_w - pw) // 2
        py = (screen_h - ph) // 2

        draw_rect(px, py, pw, ph, color(*uc.CRAFT_PANEL_BG))
        draw_rect_lines(rect(px, py, pw, ph), 2, color(*uc.CRAFT_PANEL_BORDER))

        draw_text_centered("CRAFT & PROCESS", py + 20, uc.CRAFT_PANEL_TITLE_SIZE, color(*uc.CRAFT_PANEL_TITLE_COLOR))
        draw_text("B/Esc: close", px + 10, py + ph - 24, uc.CRAFT_PANEL_FOOTER_SIZE, color(*uc.CRAFT_PANEL_FOOTER_COLOR))

        # 双列布局：左列 材料/工具/加工，右列 武器/工具合成
        top = py + 50
        bot = py + ph - 32              # 内容下限（避开底部 footer）
        mid = pw // 2
        lx = px + 12                    # 左列 x
        rx = px + mid + 12              # 右列 x
        row_h = uc.CRAFT_PANEL_ITEM_SIZE + 8      # 行高（随字号缩放）
        head_h = uc.CRAFT_PANEL_HEADER_SIZE + 8   # 标题行高

        # 列分隔线
        draw_rect(px + mid, top - 6, 2, bot - top + 6, color(*uc.CRAFT_PANEL_DIVIDER_COLOR))

        # ── 左列：材料 / 工具 / 加工 ──
        y = top
        draw_text("Materials:", lx, y, uc.CRAFT_PANEL_HEADER_SIZE, color(*uc.CRAFT_PANEL_HEADER_COLOR))
        y += head_h
        for mat in MATERIAL_NAMES_CN:  # 顺序即 data/recipes.json 中材料的顺序
            if y > bot:
                break
            amt = game.inventory.get_material(mat)
            if amt > 0:
                mc = MATERIAL_COLORS.get(mat, (200, 200, 200))
                cn = MATERIAL_NAMES_CN.get(mat, mat)
                draw_text(f"  {cn}: {amt}", lx, y, uc.CRAFT_PANEL_ITEM_SIZE, color(*mc))
                y += row_h

        y += 8
        if y < bot:
            tool_str = ', '.join(sorted(game.inventory.tools)) if game.inventory.tools else 'None'
            draw_text(f"Tools: {tool_str}", lx, y, uc.CRAFT_PANEL_ITEM_SIZE, color(180, 180, 200))
        y += head_h

        if y + head_h < bot:
            draw_text("Processing:", lx, y, uc.CRAFT_PANEL_HEADER_SIZE, color(*uc.CRAFT_PANEL_HEADER_COLOR))
            y += head_h
            for recipe in PROCESS_RECIPES.values():
                if y + row_h * 2 > bot:          # 每条配方占 2 行，放不下就停
                    break
                in_mat = recipe["in"]
                out_mat = recipe["out"]
                in_cn = MATERIAL_NAMES_CN.get(in_mat, in_mat)
                out_cn = MATERIAL_NAMES_CN.get(out_mat, out_mat)
                in_amt = game.inventory.get_material(in_mat)
                # 科技前置（data/recipes.json 的 requires_tech）
                missing_tech = [t for t in (recipe.get("requires_tech") or [])
                                if not game.has_tech(t)]
                tech_locked = bool(missing_tech)
                hand_ok = recipe["hand"] > 0 and in_amt > 0
                tool_needed = recipe.get("requires_tool")
                tool_ok = tool_needed and game.inventory.has_tool(tool_needed) and in_amt > 0

                # 第 1 行：配方名（有原料则高亮；科技锁定则置灰并标注）
                if tech_locked:
                    name_color = uc.CRAFT_PANEL_DISABLED_COLOR
                    draw_text(f"  {in_cn} -> {out_cn}  [需科技: {', '.join(missing_tech)}]",
                              lx, y, uc.CRAFT_PANEL_ITEM_SIZE, color(*name_color))
                    y += row_h
                    y += row_h  # 锁定配方不再展示产量详情，占 2 行保持对齐
                    continue
                name_color = uc.CRAFT_PANEL_HAND_OK_COLOR if in_amt > 0 else uc.CRAFT_PANEL_HAND_NA_COLOR
                draw_text(f"  {in_cn} -> {out_cn}", lx, y, uc.CRAFT_PANEL_ITEM_SIZE, color(*name_color))
                y += row_h
                # 第 2 行：徒手信息
                if hand_ok:
                    draw_text(f"    Hand: x{recipe['hand']} ({recipe['time_hand']}s)",
                              lx, y, uc.CRAFT_PANEL_ITEM_SIZE, color(*uc.CRAFT_PANEL_HAND_OK_COLOR))
                else:
                    draw_text("    Hand: -",
                              lx, y, uc.CRAFT_PANEL_ITEM_SIZE, color(*uc.CRAFT_PANEL_HAND_NA_COLOR))
                # 工具信息（仅 requires_tool 的配方，如炼铁需熔炉）接在同一行右侧
                if tool_needed:
                    tdef = TOOL_TYPES.get(tool_needed)
                    tlabel = tdef.label_cn if tdef else tool_needed
                    if tool_ok:
                        tool_part = f"Tool: x{recipe['tool']} ({recipe['time_tool']}s)"
                        tool_c = uc.CRAFT_PANEL_TOOL_OK_COLOR
                    else:
                        tool_part = f"Tool: need {tlabel}"
                        tool_c = uc.CRAFT_PANEL_TOOL_NEED_COLOR
                    draw_text(tool_part, lx + 170, y, uc.CRAFT_PANEL_ITEM_SIZE, color(*tool_c))
                y += row_h

        # ── 右列：武器 / 工具合成 ──
        y = top
        draw_text("Craft Weapons:", rx, y, uc.CRAFT_PANEL_HEADER_SIZE, color(*uc.CRAFT_PANEL_HEADER_COLOR))
        y += head_h
        for wname, wdef in WEAPON_TYPES.items():
            if y > bot:
                break
            owned = game.inventory.get_weapon(wname)
            missing_tech = [t for t in (wdef.requires_tech or ())
                            if not game.has_tech(t)]
            recipe_str = " + ".join(f"{MATERIAL_NAMES_CN.get(m, m)}x{a}" for m, a in wdef.recipe.items())
            if missing_tech:
                draw_text(f"  [{wdef.label_cn}] x{owned}  [需科技: {', '.join(missing_tech)}]",
                          rx, y, uc.CRAFT_PANEL_ITEM_SIZE, color(*uc.CRAFT_PANEL_DISABLED_COLOR))
            elif can_craft_weapon(wname, game.inventory):
                draw_text(f"  [{wdef.label_cn}] x{owned}  {recipe_str}  [Craft]",
                          rx, y, uc.CRAFT_PANEL_ITEM_SIZE, color(*uc.CRAFT_PANEL_TOOL_OK_COLOR))
            else:
                draw_text(f"  [{wdef.label_cn}] x{owned}  {recipe_str}",
                          rx, y, uc.CRAFT_PANEL_ITEM_SIZE, color(*uc.CRAFT_PANEL_DISABLED_COLOR))
            y += row_h

        y += 10
        if y + head_h < bot:
            draw_text("Craft Tools:", rx, y, uc.CRAFT_PANEL_HEADER_SIZE, color(*uc.CRAFT_PANEL_HEADER_COLOR))
            y += head_h
            for tname, tdef in TOOL_TYPES.items():
                if y > bot:
                    break
                owned = "yes" if tname in game.inventory.tools else "no"
                missing_tech = [t for t in (tdef.requires_tech or ())
                                if not game.has_tech(t)]
                recipe_str = " + ".join(f"{MATERIAL_NAMES_CN.get(m, m)}x{a}" for m, a in tdef.recipe.items())
                if missing_tech:
                    draw_text(f"  [{tdef.label_cn}] {owned}  [需科技: {', '.join(missing_tech)}]",
                              rx, y, uc.CRAFT_PANEL_ITEM_SIZE, color(*uc.CRAFT_PANEL_DISABLED_COLOR))
                elif can_craft_tool(tname, game.inventory):
                    draw_text(f"  [{tdef.label_cn}] {owned}  {recipe_str}  [Craft]",
                              rx, y, uc.CRAFT_PANEL_ITEM_SIZE, color(*uc.CRAFT_PANEL_TOOL_OK_COLOR))
                else:
                    draw_text(f"  [{tdef.label_cn}] {owned}  {recipe_str}",
                              rx, y, uc.CRAFT_PANEL_ITEM_SIZE, color(*uc.CRAFT_PANEL_DISABLED_COLOR))
                y += row_h

    def _storage_rows(self, game, uc):
        """仓储面板行布局：render 与 hit-test 共用，保证两处布局一致。

        返回 (px, py, pw, ph, top, bot, mid, row_h, head_h, rows)；
        rows 每项: (side, kind, name, count, label, label_color, x, y, w, h)，
        kind/name 为 None 的行是段标题或空占位，不可点击。
        """
        screen_w = rl.GetScreenWidth()
        screen_h = rl.GetScreenHeight()
        pw, ph = uc.STORAGE_PANEL_W, uc.STORAGE_PANEL_H
        px = (screen_w - pw) // 2
        py = (screen_h - ph) // 2
        row_h = uc.STORAGE_PANEL_ITEM_SIZE + 8
        head_h = uc.STORAGE_PANEL_HEADER_SIZE + 8
        top = py + 94
        bot = py + ph - 100   # 底部预留传送带输出勾选带
        mid = pw // 2

        def material_order(d):
            # 材料按 data/recipes.json 定义顺序展示，未定义名兜底排后
            known = [m for m in MATERIAL_NAMES_CN if m in d]
            extra = sorted(m for m in d if m not in MATERIAL_NAMES_CN)
            return [(m, d[m]) for m in known + extra]

        def item_label_color(kind, name):
            if kind == "materials":
                return MATERIAL_COLORS.get(name, (200, 200, 200))
            if kind == "weapons":
                return WEAPON_TYPES[name].color
            return (180, 180, 200)

        def item_cn(kind, name):
            if kind == "materials":
                return MATERIAL_NAMES_CN.get(name, name)
            if kind == "weapons":
                return WEAPON_TYPES[name].label_cn
            return TOOL_TYPES[name].label_cn

        def build_rows(side, source, empty_hint):
            """source: kind → [(name, count)]，按材料/武器/工具三段展开"""
            x = px + 14 if side == "left" else px + mid + 14
            w = mid - 28
            rows = []
            y = top
            any_item = False
            for kind, header in (("materials", "材料"), ("weapons", "武器"),
                                 ("tools", "工具")):
                if y + row_h > bot:
                    break
                rows.append((side, None, None, 0, header,
                             uc.STORAGE_PANEL_HEADER_COLOR, x, y, w, head_h))
                y += head_h
                for name, count in source(kind):
                    if count <= 0 or y + row_h > bot:
                        continue
                    any_item = True
                    vol = item_volume(name) * count
                    label = f"{item_cn(kind, name)} x{count}  ·{vol}空间"
                    rows.append((side, kind, name, count, label,
                                 item_label_color(kind, name), x, y, w, row_h))
                    y += row_h
            if not any_item:
                rows.append((side, None, None, 0, empty_hint,
                             uc.STORAGE_PANEL_NA_COLOR, x, y, w, row_h))
            return rows

        def tower_source(kind):
            st = game.storage_tower.storage if game.storage_tower else None
            if not st:
                return []
            if kind == "tools":
                return [(t, 1) for t in sorted(st["tools"])]
            return material_order(st[kind]) if kind == "materials" \
                else list(st[kind].items())

        def inv_source(kind):
            inv = game.inventory
            if kind == "tools":
                return [(t, 1) for t in sorted(inv.tools)]
            return material_order(inv.materials) if kind == "materials" \
                else list(inv.weapons.items())

        rows = build_rows("left", tower_source, "（塔内空空如也）")
        rows += build_rows("right", inv_source, "（背包没有可存物品）")
        return px, py, pw, ph, top, bot, mid, row_h, head_h, rows

    def render_storage_panel(self, game):
        """渲染仓储塔操作窗口（左列塔内取出 / 右列背包存入）"""
        uc = ui_config
        screen_w = rl.GetScreenWidth()
        screen_h = rl.GetScreenHeight()

        set_alpha(uc.PANEL_BG_ALPHA)
        draw_rect(0, 0, screen_w, screen_h, color(*uc.PANEL_OVERLAY_COLOR))
        set_alpha(1.0)

        px, py, pw, ph, top, bot, mid, row_h, head_h, rows = \
            self._storage_rows(game, uc)

        draw_rect(px, py, pw, ph, color(*uc.STORAGE_PANEL_BG))
        draw_rect_lines(rect(px, py, pw, ph), 2, color(*uc.STORAGE_PANEL_BORDER))

        # 容量条（进度条样式：绿 → 黄 → 红）
        tower = game.storage_tower
        # 面板标题：用塔类型显示名（仓储塔/研究塔等共用本窗口）
        title_cn = "仓储塔"
        if tower is not None:
            title_cn = getattr(tower.type_def, "name_cn", "仓储塔")
        draw_text_centered(f"STORAGE  {title_cn}", py + 18,
                           uc.STORAGE_PANEL_TITLE_SIZE,
                           color(*uc.STORAGE_PANEL_TITLE_COLOR))

        # 容量条（进度条样式：绿 → 黄 → 红）
        used = tower.storage_used() if tower else 0
        cap = tower.storage_capacity if tower else 0
        bar_x, bar_y, bar_w, bar_h = px + 14, py + 48, pw - 28, 16
        draw_rect(bar_x, bar_y, bar_w, bar_h, color(*uc.STORAGE_CAP_BG))
        ratio = (used / cap) if cap > 0 else 0.0
        if ratio >= 1.0:
            fg = uc.STORAGE_CAP_FG_FULL
        elif ratio >= 0.75:
            fg = uc.STORAGE_CAP_FG_WARN
        else:
            fg = uc.STORAGE_CAP_FG_OK
        draw_rect(bar_x + 1, bar_y + 1,
                  max(1, int((bar_w - 2) * ratio)), bar_h - 2, color(*fg))
        draw_rect_lines(rect(bar_x, bar_y, bar_w, bar_h), 1, color(140, 120, 90))
        cap_txt = f"{used}/{cap} 空间"
        draw_text(cap_txt, bar_x + bar_w - measure_text(cap_txt, 12) - 4,
                  bar_y + 2, 12, color(230, 230, 220))
        # 研究塔：在容量条右侧标注当前学识属性
        if tower is not None and getattr(tower, "is_research", False):
            attr_txt = f"学识 Lv{tower.research_attribute()}"
            draw_text(attr_txt, bar_x + bar_w + 12, bar_y + 2, 12,
                      color(200, 180, 255))
        # 开采塔：容量条右侧标注足迹 / 工具 / 状态
        if tower is not None and getattr(tower, "is_mining", False):
            _fp = getattr(tower, "footprint", (3, 3))
            _tier, _spd = tower.mining_tool_stats()
            _tool = f"工具档{_tier}" if _tier else "无工具"
            _reason = getattr(tower, "mining_reason", None) or "运行中"
            _mines = len(tower.mine_shaft)
            _st_txt = f"{_fp[0]}×{_fp[1]} {_tool} {_mines}井 {_reason}"
            draw_text(_st_txt, bar_x + bar_w + 12, bar_y + 2, 12,
                      color(180, 230, 170))

        # 列标题与分隔线
        draw_text("塔内仓储（点击取出）", px + 14, py + 72,
                  uc.STORAGE_PANEL_HEADER_SIZE,
                  color(*uc.STORAGE_PANEL_HEADER_COLOR))
        draw_text("背包（点击存入）", px + mid + 14, py + 72,
                  uc.STORAGE_PANEL_HEADER_SIZE,
                  color(*uc.STORAGE_PANEL_HEADER_COLOR))
        draw_rect(px + mid, top - 8, 2, bot - top + 14,
                  color(*uc.STORAGE_PANEL_DIVIDER_COLOR))

        # 物品行（可点击行加深底色，便于识别点击区域）
        for side, kind, _name, _count, label, lc, x, y, w, h in rows:
            if kind is not None:
                draw_rect(x - 4, y - 1, w + 8, h - 4, color(*uc.STORAGE_ROW_BG))
            draw_text(label, x, y, uc.STORAGE_PANEL_ITEM_SIZE, color(*lc))

        # 传送带输出勾选带（仓储塔手工配置：勾选的材料才会输出到传送带）
        ex = self._storage_export_band(game, uc)
        bx, by, bw, bh, chips = ex
        draw_rect(bx, by, bw, bh, color(48, 42, 34))
        draw_rect_lines(rect(bx, by, bw, bh), 1, color(120, 100, 70))
        draw_text("传送带输出（点选材料勾选/取消；默认不输出）",
                  bx + 2, by + 2, uc.STORAGE_PANEL_HEADER_SIZE,
                  color(*uc.STORAGE_PANEL_HEADER_COLOR))
        for name, cx, cy, cw, ch in chips:
            on = name in (tower.belt_export if tower else set())
            bg = uc.TOWER_BAR_SELECTED_BG if on else uc.TOWER_BAR_SEG_BG
            draw_rect(cx, cy, cw, ch, color(*bg))
            cn = MATERIAL_NAMES_CN.get(name, name)
            draw_text(f"[{'✓' if on else ' '}]{cn}", cx + 4, cy + 3,
                      uc.STORAGE_PANEL_ITEM_SIZE,
                      color(*uc.STORAGE_PANEL_OK_COLOR if on else uc.HUD_TEXT_TOOL))

        draw_text("左键 存/取 1 个 · 右键 全部存/取 · E/Esc 关闭",
                  px + 10, py + ph - 24, uc.STORAGE_PANEL_FOOTER_SIZE,
                  color(*uc.STORAGE_PANEL_FOOTER_COLOR))

    def storage_panel_rect(self, game):
        """仓储面板屏幕矩形 (px, py, pw, ph)，供点击面板外关窗判定。"""
        pw = ui_config.STORAGE_PANEL_W
        ph = ui_config.STORAGE_PANEL_H
        return ((rl.GetScreenWidth() - pw) // 2,
                (rl.GetScreenHeight() - ph) // 2, pw, ph)

    def _storage_export_band(self, game, uc):
        """仓储面板底部"传送带输出"勾选带几何（render 与 hit-test 共用）。

        返回 (x, y, w, h, chips)；chips = [(name, cx, cy, cw, ch), ...]。
        """
        pw, ph = uc.STORAGE_PANEL_W, uc.STORAGE_PANEL_H
        px = (rl.GetScreenWidth() - pw) // 2
        py = (rl.GetScreenHeight() - ph) // 2
        x, y = px + 14, py + ph - 90
        w, h = pw - 28, 58
        tower = game.storage_tower
        mats = dict(tower.storage["materials"]) if tower else {}
        export = set(tower.belt_export) if tower else set()
        chips = []
        cx = x
        for name in MATERIAL_NAMES_CN:
            if name not in mats:
                continue
            cn = MATERIAL_NAMES_CN.get(name, name)
            on = name in export
            label = f"[{'✓' if on else ' '}]{cn}"
            cw = measure_text(label, uc.STORAGE_PANEL_ITEM_SIZE) + 10
            if cx + cw > x + w:
                break
            chips.append((name, cx, y + 20, cw, 18))
            cx += cw + 6
        return (x, y, w, h, chips)

    def storage_hit_action(self, game, mx, my):
        """仓储面板鼠标命中：返回 ("withdraw"/"deposit", kind, name) 或
        ("belt_export", "materials", name) 或 None。

        布局与 render_storage_panel 共用 _storage_rows，天然一致。
        存/取数量由调用方按鼠标键位决定（左键 1 个、右键全部）。
        """
        uc = ui_config
        # 传送带输出勾选带优先（点在 chips 上即勾选/取消）
        _, _, _, _, chips = self._storage_export_band(game, uc)
        for name, cx, cy, cw, ch in chips:
            if cx <= mx <= cx + cw and cy <= my <= cy + ch:
                return ("belt_export", "materials", name)
        _, _, _, _, _, _, _, _, _, rows = self._storage_rows(game, uc)
        for side, kind, name, _count, _label, _lc, x, y, w, h in rows:
            if kind is None:
                continue
            if x - 4 <= mx <= x + w + 4 and y - 1 <= my <= y + h - 5:
                action = "withdraw" if side == "left" else "deposit"
                return (action, kind, name)
        return None

    # ─── 加工塔面板 ──────────────────────────────────────────

    def _processing_rows(self, game, uc):
        """加工面板行布局：render 与 hit-test 共用，保证两处布局一致。

        返回 (px, py, pw, ph, top, bot, mid, row_h, head_h, rows)。
        rows 每项: (side, kind, name, count, label, label_color, x, y, w, h, rkey)
          side="left"/"right"        双栏物品行（塔内取出 / 背包存入）
          side="mid", kind="tools"   工具架行（点击取下工具；name=工具名）
          side="mid", kind="recipe"  手动配方行（点选/再点取消；name=配方key）
        kind/name 为 None 的行是段标题或空占位，不可点击；rkey 供渲染进度条。
        """
        screen_w = rl.GetScreenWidth()
        screen_h = rl.GetScreenHeight()
        pw, ph = uc.PROCESSING_PANEL_W, uc.PROCESSING_PANEL_H
        px = (screen_w - pw) // 2
        py = (screen_h - ph) // 2
        row_h = uc.PROCESSING_PANEL_ITEM_SIZE + 8
        head_h = uc.PROCESSING_PANEL_HEADER_SIZE + 8
        bot = py + ph - 34
        mid = pw // 2

        tower = game.processing_tower
        recipes = PROCESS_RECIPES

        def material_order(d):
            # 材料按 data/recipes.json 定义顺序展示，未定义名兜底排后
            known = [m for m in MATERIAL_NAMES_CN if m in d]
            extra = sorted(m for m in d if m not in MATERIAL_NAMES_CN)
            return [(m, d[m]) for m in known + extra]

        def item_label_color(kind, name):
            if kind == "materials":
                return MATERIAL_COLORS.get(name, (200, 200, 200))
            if kind == "weapons":
                return WEAPON_TYPES[name].color
            return (180, 180, 200)

        def item_cn(kind, name):
            if kind == "materials":
                return MATERIAL_NAMES_CN.get(name, name)
            if kind == "weapons":
                return WEAPON_TYPES[name].label_cn
            return TOOL_TYPES[name].label_cn

        def recipe_cn(rec):
            in_cn = MATERIAL_NAMES_CN.get(rec["in"], rec["in"])
            out_cn = MATERIAL_NAMES_CN.get(rec["out"], rec["out"])
            return f"{in_cn}→{out_cn}"

        # 工具 → 配方 key：先按 applies 反查（增益与必需工具都算，与加工塔
        # 自动线口径一致——石锯→木材→木板、块炼炉→铁矿→铁块、核精炼→铀矿→
        # 核燃料 等），采掘工具（石镐/铁镐/凿岩机）applies=None 自然无配方。
        tool_recipe = {}
        for _tn, _td in TOOL_TYPES.items():
            if _td.applies and _td.applies in recipes:
                tool_recipe[_tn] = _td.applies

        rows = []
        x_mid = px + 14
        w_mid = pw - 28
        y = py + 72

        # ── 工具架 · 自动加工 ──
        rows.append((None, None, None, 0, "工具架 · 自动加工（点击行取下工具）",
                     uc.PROCESSING_PANEL_HEADER_COLOR, x_mid, y, w_mid, head_h, None))
        y += head_h
        tools = sorted(tower.processing["tools"]) if tower else []
        if not tools:
            rows.append((None, None, None, 0,
                         "（未装配工具 — 放入工具即启用其配方自动加工）",
                         uc.PROCESSING_PANEL_NA_COLOR, x_mid, y, w_mid, row_h, None))
            y += row_h
        for t in tools:
            tdef = TOOL_TYPES.get(t)
            tcn = tdef.label_cn if tdef else t
            rkey = tool_recipe.get(t)
            if rkey is None:
                label = f"[{tcn}]  （无对应配方）"
                lc = uc.PROCESSING_PANEL_NA_COLOR
            else:
                rec = recipes[rkey]
                job = tower.process_jobs.get(rkey)
                dur = tower.process_duration(rkey)
                if job is None:
                    reason = tower.process_block_reason(rkey)
                    if reason == "input":
                        lc = uc.PROCESSING_STATUS_BLOCK_COLOR
                        label = f"[{tcn}] ▶ {recipe_cn(rec)}  — 缺原料"
                    elif reason == "space":
                        lc = uc.PROCESSING_STATUS_BLOCK_COLOR
                        label = f"[{tcn}] ▶ {recipe_cn(rec)}  — 产物空间不足"
                    else:
                        lc = uc.PROCESSING_STATUS_IDLE_COLOR
                        label = f"[{tcn}] ▶ {recipe_cn(rec)}  — 待开工"
                elif job >= dur:
                    lc = uc.PROCESSING_STATUS_BLOCK_COLOR
                    label = f"[{tcn}] ▶ {recipe_cn(rec)}  — 完成，等待产出空间"
                else:
                    lc = uc.PROCESSING_STATUS_RUN_COLOR
                    label = f"[{tcn}] ▶ {recipe_cn(rec)}  {job:.1f}/{dur:.1f}s"
            rows.append(("mid", "tools", t, 1, label, lc, x_mid, y, w_mid, row_h, rkey))
            y += row_h

        # ── 手动配方 ──
        rows.append((None, None, None, 0, "手动配方（点击选择开工 · 再点一次停机）",
                     uc.PROCESSING_PANEL_HEADER_COLOR, x_mid, y, w_mid, head_h, None))
        y += head_h
        free_recipes = [(rk, rec) for rk, rec in recipes.items()
                        if not rec.get("requires_tool")]
        if not free_recipes:
            rows.append((None, None, None, 0, "（没有可手动选择的配方）",
                         uc.PROCESSING_PANEL_NA_COLOR, x_mid, y, w_mid, row_h, None))
            y += row_h
        for rkey, rec in free_recipes:
            locked = not all(game.has_tech(t) for t in rec.get("requires_tech", []))
            selected = tower.manual_recipe == rkey
            job = tower.process_jobs.get(rkey)
            dur = tower.process_duration(rkey)
            out_n = tower.process_yield(rkey)
            if locked:
                lc = uc.PROCESSING_PANEL_NA_COLOR
                label = f"　{recipe_cn(rec)}  （科技未解锁）"
            elif job is not None:
                lc = uc.PROCESSING_STATUS_RUN_COLOR
                if job >= dur:
                    lc = uc.PROCESSING_STATUS_BLOCK_COLOR
                    label = f"▶ {recipe_cn(rec)} x{out_n}  — 完成，等待产出空间"
                else:
                    label = f"▶ {recipe_cn(rec)} x{out_n}  {job:.1f}/{dur:.1f}s"
            elif selected:
                reason = tower.process_block_reason(rkey)
                if reason == "input":
                    lc = uc.PROCESSING_STATUS_BLOCK_COLOR
                    label = f"▶ {recipe_cn(rec)} x{out_n}  — 缺原料"
                elif reason == "space":
                    lc = uc.PROCESSING_STATUS_BLOCK_COLOR
                    label = f"▶ {recipe_cn(rec)} x{out_n}  — 产物空间不足"
                else:
                    lc = uc.PROCESSING_STATUS_RUN_COLOR
                    label = f"▶ {recipe_cn(rec)} x{out_n}  — 待开工"
            else:
                lc = uc.PROCESSING_PANEL_OK_COLOR
                label = f"　{recipe_cn(rec)} x{out_n} ({dur:.1f}s)"
            rows.append(("mid", "recipe", rkey, 1, label, lc,
                         x_mid, y, w_mid, row_h, rkey))
            y += row_h

        # ── 双栏物品（列标题并入 rows，不可点击） ──
        col_hdr_y = y
        rows.append((None, None, None, 0, "塔内物品（点击取出）",
                     uc.PROCESSING_PANEL_HEADER_COLOR,
                     px + 14, col_hdr_y, mid - 28, head_h, None))
        rows.append((None, None, None, 0, "背包（点击存入）",
                     uc.PROCESSING_PANEL_HEADER_COLOR,
                     px + mid + 14, col_hdr_y, mid - 28, head_h, None))
        top = col_hdr_y + head_h

        def build_rows(side, source, empty_hint):
            """source: kind → [(name, count)]，按材料/武器/工具三段展开"""
            x = px + 14 if side == "left" else px + mid + 14
            w = mid - 28
            out_rows = []
            cy = top
            any_item = False
            for kind, header in (("materials", "材料"), ("weapons", "武器"),
                                 ("tools", "工具")):
                if cy + row_h > bot:
                    break
                out_rows.append((side, None, None, 0, header,
                                 uc.PROCESSING_PANEL_HEADER_COLOR,
                                 x, cy, w, head_h, None))
                cy += head_h
                for name, count in source(kind):
                    if count <= 0 or cy + row_h > bot:
                        continue
                    any_item = True
                    vol = item_volume(name) * count
                    label = f"{item_cn(kind, name)} x{count}  ·{vol}空间"
                    out_rows.append((side, kind, name, count, label,
                                     item_label_color(kind, name),
                                     x, cy, w, row_h, None))
                    cy += row_h
            if not any_item:
                out_rows.append((side, None, None, 0, empty_hint,
                                 uc.PROCESSING_PANEL_NA_COLOR,
                                 x, cy, w, row_h, None))
            return out_rows

        def tower_source(kind):
            pt = tower.processing if tower else None
            if not pt:
                return []
            if kind == "tools":
                return [(t, 1) for t in sorted(pt["tools"])]
            return material_order(pt[kind]) if kind == "materials" \
                else list(pt[kind].items())

        def inv_source(kind):
            inv = game.inventory
            if kind == "tools":
                return [(t, 1) for t in sorted(inv.tools)]
            return material_order(inv.materials) if kind == "materials" \
                else list(inv.weapons.items())

        rows += build_rows("left", tower_source, "（塔内空空如也）")
        rows += build_rows("right", inv_source, "（背包没有可存物品）")
        return px, py, pw, ph, top, bot, mid, row_h, head_h, rows

    def render_processing_panel(self, game):
        """渲染加工塔操作窗口（工具架/手动配方 + 左列塔内取出 / 右列背包存入）"""
        uc = ui_config
        screen_w = rl.GetScreenWidth()
        screen_h = rl.GetScreenHeight()

        set_alpha(uc.PANEL_BG_ALPHA)
        draw_rect(0, 0, screen_w, screen_h, color(*uc.PANEL_OVERLAY_COLOR))
        set_alpha(1.0)

        px, py, pw, ph, top, bot, mid, row_h, head_h, rows = \
            self._processing_rows(game, uc)

        draw_rect(px, py, pw, ph, color(*uc.PROCESSING_PANEL_BG))
        draw_rect_lines(rect(px, py, pw, ph), 2,
                        color(*uc.PROCESSING_PANEL_BORDER))

        draw_text_centered("PROCESSING  加工塔", py + 18,
                           uc.PROCESSING_PANEL_TITLE_SIZE,
                           color(*uc.PROCESSING_PANEL_TITLE_COLOR))

        # 容量条（进度条样式：绿 → 黄 → 红）
        tower = game.processing_tower
        used = tower.processing_used() if tower else 0
        cap = tower.processing_capacity if tower else 0
        bar_x, bar_y, bar_w, bar_h = px + 14, py + 48, pw - 28, 16
        draw_rect(bar_x, bar_y, bar_w, bar_h, color(*uc.PROCESSING_CAP_BG))
        ratio = (used / cap) if cap > 0 else 0.0
        if ratio >= 1.0:
            fg = uc.PROCESSING_CAP_FG_FULL
        elif ratio >= 0.75:
            fg = uc.PROCESSING_CAP_FG_WARN
        else:
            fg = uc.PROCESSING_CAP_FG_OK
        draw_rect(bar_x + 1, bar_y + 1,
                  max(1, int((bar_w - 2) * ratio)), bar_h - 2, color(*fg))
        draw_rect_lines(rect(bar_x, bar_y, bar_w, bar_h), 1, color(140, 110, 90))
        cap_txt = f"{used}/{cap} 空间"
        draw_text(cap_txt, bar_x + bar_w - measure_text(cap_txt, 12) - 4,
                  bar_y + 2, 12, color(230, 230, 220))

        # 双栏分隔线
        draw_rect(px + mid, top - 8, 2, bot - top + 14,
                  color(*uc.PROCESSING_PANEL_DIVIDER_COLOR))

        # 行渲染：可点击行加深底色；在制任务行高亮并画进度条
        for side, kind, name, _count, label, lc, x, y, w, h, rkey in rows:
            clickable = kind is not None
            active = (rkey is not None and tower is not None
                      and rkey in tower.process_jobs)
            if active:
                draw_rect(x - 4, y - 1, w + 8, h - 4,
                          color(*uc.PROCESSING_ROW_BG_ACTIVE))
            elif clickable:
                draw_rect(x - 4, y - 1, w + 8, h - 4,
                          color(*uc.PROCESSING_ROW_BG))
            draw_text(label, x, y, uc.PROCESSING_PANEL_ITEM_SIZE, color(*lc))
            if active and rkey is not None and tower is not None:
                job = tower.process_jobs[rkey]
                dur = tower.process_duration(rkey)
                ratio = (job / dur) if dur > 0 else 1.0
                bw, bh = 120, 8
                bx, by = x + w - bw - 4, y + 6
                draw_rect(bx, by, bw, bh, color(*uc.PROCESSING_PROGRESS_BG))
                draw_rect(bx, by, max(1, int(bw * min(1.0, ratio))), bh,
                          color(*uc.PROCESSING_PROGRESS_FG))

        draw_text("左键 存/取/选 1 个 · 右键 全部存/取 · 点配方选择 · E/Esc 关闭",
                  px + 10, py + ph - 24, uc.PROCESSING_PANEL_FOOTER_SIZE,
                  color(*uc.PROCESSING_PANEL_FOOTER_COLOR))

    def processing_panel_rect(self, game):
        """加工面板屏幕矩形 (px, py, pw, ph)，供点击面板外关窗判定。"""
        pw = ui_config.PROCESSING_PANEL_W
        ph = ui_config.PROCESSING_PANEL_H
        return ((rl.GetScreenWidth() - pw) // 2,
                (rl.GetScreenHeight() - ph) // 2, pw, ph)

    def processing_hit_action(self, game, mx, my):
        """加工面板鼠标命中：返回 (action, kind, name) 或 None。

        action ∈ "withdraw"/"deposit"/"select_recipe"；布局与
        render_processing_panel 共用 _processing_rows，天然一致。
        存/取数量由调用方按鼠标键位决定（左键 1 个、右键全部）。
        """
        uc = ui_config
        _, _, _, _, _, _, _, _, _, rows = self._processing_rows(game, uc)
        for side, kind, name, _count, _label, _lc, x, y, w, h, _rkey in rows:
            if kind is None:
                continue
            if x - 4 <= mx <= x + w + 4 and y - 1 <= my <= y + h - 5:
                if side == "left":
                    return ("withdraw", kind, name)
                if side == "right":
                    return ("deposit", kind, name)
                if kind == "tools":
                    return ("withdraw", "tools", name)
                return ("select_recipe", "recipe", name)
        return None

    def render_spawner_panel(self, game):
        """渲染刷怪点波次面板（各点独立波次状态，分页）"""
        import math as _math
        screen_w = rl.GetScreenWidth()
        screen_h = rl.GetScreenHeight()
        uc = ui_config

        # 半透明遮罩
        set_alpha(uc.PANEL_BG_ALPHA)
        draw_rect(0, 0, screen_w, screen_h, color(*uc.PANEL_OVERLAY_COLOR))
        set_alpha(1.0)

        # 面板
        PAGE_SIZE = 8
        pw, ph = 520, min(580, screen_h - 60)
        px = (screen_w - pw) // 2
        py = (screen_h - ph) // 2

        draw_rect(px, py, pw, ph, color(20, 20, 30, 235))
        draw_rect_lines(rect(px, py, pw, ph), 2, color(120, 140, 180))

        # 标题
        draw_text_centered("SPAWNER WAVES", py + 22, 22, color(220, 220, 240))

        # 摘要
        active = sum(1 for rt in game.spawners.values() if rt.mode == "active")
        scouting = sum(1 for rt in game.spawners.values() if rt.mode == "scouting")
        dormant = sum(1 for rt in game.spawners.values() if rt.mode == "dormant")
        draw_text(f"Active: {active}  |  Scouting: {scouting}  |  Dormant: {dormant}",
                   px + 10, py + 50, 16, color(180, 200, 220))

        # 按距离排序（防御：追踪器引用了已不存在的 spawner 时跳过，避免 KeyError）
        valid_spawners = [
            rt for rt in game.spawners.values()
            if rt.spawner_id in game.universe.spawners]
        sorted_spawners = sorted(
            valid_spawners,
            key=lambda rt: _math.hypot(
                game.universe.spawners[rt.spawner_id].x - game.universe.base_x,
                game.universe.spawners[rt.spawner_id].z - game.universe.base_z))

        # 分页
        total = len(sorted_spawners)
        total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        # 仅在本地 clamp 会让计数器越界后「←」黏滞；把钳制值写回计数器，
        # 使 game.spawner_panel_page 与显示页保持一致（BUG-085）
        page = min(game.spawner_panel_page, total_pages - 1)
        game.spawner_panel_page = page
        page_spawners = sorted_spawners[page * PAGE_SIZE: (page + 1) * PAGE_SIZE]

        # 页码指示
        draw_text(f"Page {page + 1}/{total_pages}  ({total} spawners)",
                   px + 10, py + ph - 26, 14, color(150, 150, 175))
        draw_text("P / Esc: close   ← →: pages",
                   px + pw - 200, py + ph - 26, 14, color(150, 150, 175))

        # 各刷怪点状态
        mode_colors = {
            "dormant": (150, 150, 160),
            "scouting": (255, 220, 0),
            "active": (255, 80, 80),
        }
        mode_labels = {"dormant": "DORMANT", "scouting": "SCOUTING", "active": "ACTIVE"}

        y = py + 80
        for rt in page_spawners:
            if y > py + ph - 40:
                break
            sp = game.universe.spawners.get(rt.spawner_id)
            if sp is None:
                continue  # 追踪器引用了已不存在的 spawner：跳过该行
            dist = _math.hypot(sp.x - game.universe.base_x, sp.z - game.universe.base_z)
            mc = mode_colors.get(rt.mode, (150, 150, 160))

            # 第1行：状态 + 距离 + 坐标
            label = mode_labels.get(rt.mode, rt.mode.upper())
            draw_text(f"[{label}]  dist={dist:.0f}  ({sp.x},{sp.z})",
                       px + 10, y, 15, color(*mc))
            y += 20

            # 第2行：波次详情
            if rt.mode == "active":
                if rt.between_wave:
                    detail = f"  Wave {rt.wave_num} -> {rt.wave_num + 1}  (between: {rt.between_timer:.1f}s)  enemies: {rt.enemy_count}/{rt.max_enemies}"
                else:
                    detail = f"  Wave {rt.wave_num}  spawning: {rt.enemy_count}/{rt.max_enemies}  interval: {rt.spawn_interval:.1f}s  types: {','.join(rt.enemy_types)}"
            elif rt.mode == "scouting":
                detail = f"  scouts pending: {rt.scouts_pending}  message: {'YES' if rt.message_received else 'no'}"
            else:
                detail = f"  (waiting for trigger: enemy/build/combat nearby)"
            draw_text(detail, px + 10, y, 13, color(160, 170, 185))
            y += 22

            # L0 可达性（D1a）：受阻类型提示——全部受阻时该点不出兵
            if rt.blocked_types:
                all_blocked = set(rt.enemy_types) <= set(rt.blocked_types) \
                    if rt.enemy_types else False
                if all_blocked:
                    note = f"  PATH BLOCKED: no reachable route ({','.join(sorted(rt.blocked_types))})"
                    nc = (255, 120, 60)
                else:
                    note = f"  path blocked for: {','.join(sorted(rt.blocked_types))}"
                    nc = (230, 200, 90)
                draw_text(note, px + 10, y, 12, color(*nc))
                y += 16

            # 波次进度条（仅 active）
            if rt.mode == "active" and rt.max_enemies > 0:
                frac = min(1.0, rt.enemy_count / rt.max_enemies)
                bar_w = pw - 30
                draw_rect(px + 15, y + 2, bar_w, 6, color(40, 40, 50))
                draw_rect(px + 15, y + 2, int(bar_w * frac), 6, color(*mc))
                y += 10

            y += 4

    def render_settings_panel(self, game):
        """渲染设置面板（F10 打开；持久化 settings.json）。

        三行：显示进攻路线 / 显示小地图 / 路线透明度。
        ↑↓ 选行；Enter/Space 切换开关；←→ 调滑块；F10/Esc 关闭。
        仅渲染，输入在 game._update_settings_panel 处理（几何函数本处复刻）。
        """
        screen_w = rl.GetScreenWidth()
        screen_h = rl.GetScreenHeight()
        uc = ui_config

        set_alpha(uc.PANEL_BG_ALPHA)
        draw_rect(0, 0, screen_w, screen_h, color(*uc.PANEL_OVERLAY_COLOR))
        set_alpha(1.0)

        pw = uc.SETTINGS_PANEL_W
        rows = game._settings_rows
        n = len(rows)
        content_h = n * uc.SETTINGS_PANEL_ROW_H + (n - 1) * uc.SETTINGS_PANEL_ROW_GAP
        header = 64
        footer = 34
        ph = header + content_h + footer
        px = (screen_w - pw) // 2
        py = (screen_h - ph) // 2

        draw_rect(px, py, pw, ph, color(*uc.SETTINGS_PANEL_BG, 240))
        draw_rect_lines(rect(px, py, pw, ph), 2, color(*uc.SETTINGS_PANEL_BORDER))

        draw_text_centered("设置", py + 18, uc.SETTINGS_PANEL_TITLE_SIZE,
                           color(*uc.SETTINGS_PANEL_TITLE_COLOR))

        base_y = py + header
        sel = game.settings_row
        s = game.settings
        for i, key in enumerate(rows):
            ry = base_y + i * (uc.SETTINGS_PANEL_ROW_H + uc.SETTINGS_PANEL_ROW_GAP)
            selected = (i == sel)
            bg = (uc.SETTINGS_PANEL_ROW_SELECTED_BG if selected
                  else uc.SETTINGS_PANEL_ROW_BG)
            draw_rect(px + 12, ry, pw - 24, uc.SETTINGS_PANEL_ROW_H,
                      color(*bg, 230))
            if selected:
                draw_rect_lines(rect(px + 12, ry, pw - 24, uc.SETTINGS_PANEL_ROW_H),
                                2, color(*uc.SETTINGS_PANEL_ROW_BORDER))

            label_y = ry + (uc.SETTINGS_PANEL_ROW_H - uc.SETTINGS_PANEL_ITEM_SIZE) // 2
            if key == "show_attack_routes":
                label = "显示进攻路线"
                on = bool(s.get(key, True))
                val_color = (uc.SETTINGS_PANEL_VALUE_ON if on
                             else uc.SETTINGS_PANEL_VALUE_OFF)
                val = "开" if on else "关"
                draw_text(label, px + 28, label_y, uc.SETTINGS_PANEL_ITEM_SIZE,
                          color(*uc.SETTINGS_PANEL_TITLE_COLOR))
                vt = f"◀ {val} ▶" if selected else f"  {val}  "
                vw = measure_text(vt, uc.SETTINGS_PANEL_ITEM_SIZE)
                draw_text(vt, px + pw - 40 - vw, label_y,
                          uc.SETTINGS_PANEL_ITEM_SIZE, color(*val_color))
            elif key == "minimap_enabled":
                label = "显示小地图"
                on = bool(s.get(key, True))
                val_color = (uc.SETTINGS_PANEL_VALUE_ON if on
                             else uc.SETTINGS_PANEL_VALUE_OFF)
                val = "开" if on else "关"
                draw_text(label, px + 28, label_y, uc.SETTINGS_PANEL_ITEM_SIZE,
                          color(*uc.SETTINGS_PANEL_TITLE_COLOR))
                vt = f"◀ {val} ▶" if selected else f"  {val}  "
                vw = measure_text(vt, uc.SETTINGS_PANEL_ITEM_SIZE)
                draw_text(vt, px + pw - 40 - vw, label_y,
                          uc.SETTINGS_PANEL_ITEM_SIZE, color(*val_color))
            elif key == "route_opacity":
                label = "路线透明度"
                draw_text(label, px + 28, label_y, uc.SETTINGS_PANEL_ITEM_SIZE,
                          color(*uc.SETTINGS_PANEL_TITLE_COLOR))
                # 滑块（右侧 180px 区域）
                track_x = px + pw - 220
                track_w = 170
                track_y = ry + uc.SETTINGS_PANEL_ROW_H // 2 - 5
                frac = max(0.0, min(1.0, float(s.get(key, 0.4))))
                draw_rect(track_x, track_y, track_w, 10,
                          color(*uc.SETTINGS_PANEL_SLIDER_TRACK))
                fill_w = max(2, int(track_w * frac))
                draw_rect(track_x, track_y, fill_w, 10,
                          color(*uc.SETTINGS_PANEL_SLIDER_FILL))
                knob_x = track_x + fill_w - 5
                draw_rect(knob_x, track_y - 4, 10, 18,
                          color(*uc.SETTINGS_PANEL_SLIDER_KNOB))
                pct = f"{int(round(frac * 100))}%"
                pw_text = measure_text(pct, uc.SETTINGS_PANEL_ITEM_SIZE)
                draw_text(pct, track_x + track_w + 8, label_y,
                          uc.SETTINGS_PANEL_ITEM_SIZE,
                          color(*uc.SETTINGS_PANEL_TITLE_COLOR))

        footer_y = py + ph - footer + 6
        draw_text("↑↓ 选择   Enter/Space 切换   ←→ 调节   F10/Esc 关闭",
                  px + 16, footer_y, uc.SETTINGS_PANEL_FOOTER_SIZE,
                  color(*uc.SETTINGS_PANEL_FOOTER_COLOR))
        draw_text("改动即时保存到 settings.json",
                  px + pw - 16 - measure_text("改动即时保存到 settings.json",
                                              uc.SETTINGS_PANEL_FOOTER_SIZE),
                  footer_y, uc.SETTINGS_PANEL_FOOTER_SIZE,
                  color(*uc.SETTINGS_PANEL_HINT_COLOR))

    def render_game_over(self, game):
        """渲染游戏结束界面"""
        screen_w = rl.GetScreenWidth()
        screen_h = rl.GetScreenHeight()
        uc = ui_config

        set_alpha(uc.GAMEOVER_ALPHA)
        draw_rect(0, 0, screen_w, screen_h, color(0, 0, 0))
        set_alpha(1.0)

        draw_text_centered("GAME OVER", screen_h // 2 - 40, uc.GAMEOVER_TITLE_SIZE, color(*uc.GAMEOVER_TITLE_COLOR))
        draw_text_centered("大本营已被摧毁!", screen_h // 2 - 5, uc.GAMEOVER_SUBTITLE_SIZE, color(*uc.GAMEOVER_SUBTITLE_COLOR))
        draw_text_centered(f"Final Score: {game.score}", screen_h // 2 + 25, uc.GAMEOVER_SCORE_SIZE, color(*uc.GAMEOVER_SCORE_COLOR))
        draw_text_centered(f"Reached Wave: {game.wave}", screen_h // 2 + 55, uc.GAMEOVER_WAVE_SIZE, color(*uc.GAMEOVER_WAVE_COLOR))
        draw_text_centered("Press Enter to restart", screen_h // 2 + 85, uc.GAMEOVER_HINT_SIZE, color(*uc.GAMEOVER_HINT_COLOR))

    def render_save_menu(self, game):
        """渲染存档管理菜单（F8 打开）"""
        screen_w = rl.GetScreenWidth()
        screen_h = rl.GetScreenHeight()

        # 半透明遮罩
        set_alpha(0.7)
        draw_rect(0, 0, screen_w, screen_h, color(0, 0, 0))
        set_alpha(1.0)

        # 面板
        (panel_w, row_h, row_gap, base_y, panel_h,
         visible_rows, _scroll_max) = game._save_menu_geometry()
        px = (screen_w - panel_w) // 2

        draw_rect(px, base_y - 40, panel_w, panel_h, color(30, 35, 50))
        draw_rect_lines(rect(px, base_y - 40, panel_w, panel_h), 2, color(80, 100, 140))

        # 标题
        draw_text_centered("存档管理", base_y - 20, 22, color(220, 220, 240))

        # 菜单行（只画视窗内可见行，滚动跟随 save_menu_scroll）
        total = 1 + len(game.save_menu_items)
        for i in range(game.save_menu_scroll,
                       min(total, game.save_menu_scroll + visible_rows)):
            y = base_y + (i - game.save_menu_scroll) * (row_h + row_gap)
            selected = (i == game.save_menu_index)

            if i == 0:
                label = "+ 新建存档"
                desc = "(Enter 保存当前进度)"
            else:
                item = game.save_menu_items[i - 1]
                time_str = save_system.format_time(item["created_at"])
                size_kb = item["size"] / 1024
                label = f"W{item['wave']}  |  种子 {item['seed']}  |  {time_str}  |  {size_kb:.1f}KB"
                desc = ""

            # 行背景
            row_x = px + 16
            row_w = panel_w - 32
            bg = color(55, 65, 90) if selected else color(40, 45, 60)
            draw_rect(row_x, y, row_w, row_h, bg)
            if selected:
                draw_rect_lines(rect(row_x, y, row_w, row_h), 1, color(100, 150, 200))

            # 文本
            text_color = color(240, 240, 255) if selected else color(180, 185, 200)
            draw_text(label, row_x + 12, y + 7, 15, text_color)
            if desc:
                draw_text(desc, row_x + 12 + measure_text(label, 15) + 8, y + 9, 12,
                          color(130, 135, 150))

        # 删除确认提示
        if game.save_menu_confirm_delete:
            confirm_y = base_y + visible_rows * (row_h + row_gap) + 8
            draw_rect(px + 16, confirm_y, panel_w - 32, 28, color(60, 30, 30))
            draw_text_centered("按 Del 确认删除该存档", confirm_y + 4, 14, color(255, 180, 180))

        # 错误提示
        if game.save_menu_error:
            err_y = base_y + visible_rows * (row_h + row_gap) + (40 if game.save_menu_confirm_delete else 12)
            draw_text_centered(f"! {game.save_menu_error}", err_y, 14, color(255, 150, 150))

        # 底部提示
        footer_y = base_y - 40 + panel_h - 28
        draw_text_centered("↑↓ 选择   Enter 新建/载入   Del 删除   Esc/T 返回",
                           footer_y, 13, color(150, 155, 175))

    def render_placement_preview(self, game):
        """渲染塔放置预览（经典/武器/仓储/加工/城墙）。

        幽灵立方体几何取自 TowerType.body_dims，与实塔渲染完全同锚同尺寸，
        避免斜视角下"塔出现在光标旁边"。2D 视图下 camera_extra 处于
        begin_camera2d 内（renderer._render_2d 末段、地形之后），改画
        世界坐标 2D 图元（格子矩形 + 射程圆线）。
        """
        uc = ui_config
        wx, wy, wz = game.camera.screen_to_world(rl.GetMouseX(), rl.GetMouseY())
        x, z = math.floor(wx), math.floor(wz)
        is_3d = game.camera.is_3d()

        # 电力放置预览（电线 / 电站 / 电池）
        if game.interaction_mode == "power" and game.selected_power:
            self._render_power_preview(game, x, z, wy, is_3d, uc)
            return

        # 传送带放置预览
        if game.interaction_mode == "belt" and game.selected_belt:
            reason = game._belt_placement_reason(x, z)
            valid = reason is None
            ok = valid and game.money >= game._belt_cost(game.selected_belt)
            y = float(game.universe.surface_height_at(x, z) + 1)
            c = color(*uc.PLACE_PREVIEW_VALID_COLOR) if ok else color(*uc.PLACE_PREVIEW_INVALID_COLOR)
            if is_3d:
                set_alpha(uc.PLACE_PREVIEW_ALPHA_VALID if ok else uc.PLACE_PREVIEW_ALPHA_INVALID)
                self._draw_belt_shape(game, game.selected_belt, game.belt_direction,
                                      x + 0.5, y, z + 0.5, c)
                set_alpha(1.0)
                if not valid:
                    self._draw_blocker_marker(game, (x + 0.5, y, z + 0.5))
            else:
                self._draw_2d_preview(x, z, 0.0, ok)
            return

        if game.selected_weapon:
            gy = float(game.universe.stand_y_for_hit(x, z, wy))
            reason, blocker = game._placement_block_reason(x, z, gy)
            valid = reason is None
            ok = valid and game.money >= game.selected_weapon.install_fee
            rng = game.selected_weapon.range if valid else 0.0
            if is_3d:
                c = color(*uc.PLACE_PREVIEW_VALID_COLOR) if ok else color(*uc.PLACE_PREVIEW_INVALID_COLOR)
                set_alpha(uc.PLACE_PREVIEW_ALPHA_VALID if ok else uc.PLACE_PREVIEW_ALPHA_INVALID)
                # 底格高亮：实际占格是光标命中的基座格（塔身有高度，斜视角下
                # 身体在光标上方，底板标出真正要填的格子）
                draw_cube(vec3(x + 0.5, gy + 0.03, z + 0.5), 0.96, 0.05, 0.96, c)
                sz = uc.PLACE_PREVIEW_CUBE_SIZE
                draw_cube(vec3(x + 0.5, gy, z + 0.5), sz[0], sz[1], sz[2], c)
                set_alpha(1.0)
                if not valid:
                    self._draw_blocker_marker(game, blocker)
                if rng > 0:
                    set_alpha(uc.PLACE_PREVIEW_RANGE_ALPHA)
                    draw_ring_3d(vec3(x + 0.5, gy + 0.05, z + 0.5), rng,
                                 color(*uc.PLACE_PREVIEW_RANGE_COLOR))
                    set_alpha(1.0)
            else:
                self._draw_2d_preview(x, z, rng, ok)
            return

        if not game.placing_tower or not game.selected_tower_type:
            return

        tt = game.selected_tower_type
        gy = float(game.universe.stand_y_for_hit(x, z, wy))
        is_mining = getattr(tt, "is_mining", False)
        if is_mining:
            footprint = tuple(game.selected_mining_footprint)
            reason, blocker = game._footprint_placement_reason(x, z, footprint, gy)
            valid = reason is None
            ok = valid and game.money >= game.tower_cost(tt, footprint)
            bw, bh, bd, by = tt.body_dims(footprint)
            rng = 0.0
        else:
            footprint = None
            reason, blocker = game._placement_block_reason(x, z, gy)
            valid = reason is None
            ok = valid and game.money >= game.tower_cost(tt)
            bw, bh, bd, by = tt.body_dims()
            rng = tt.base_range if valid else 0.0
        if is_3d:
            c = color(*uc.PLACE_PREVIEW_VALID_COLOR) if ok else color(*uc.PLACE_PREVIEW_INVALID_COLOR)
            set_alpha(uc.PLACE_PREVIEW_ALPHA_VALID if ok else uc.PLACE_PREVIEW_ALPHA_INVALID)
            if is_mining:
                # 开采塔：足迹逐格底格高亮 + 内部竖井格稍亮 + 幽灵平台
                for (cx, cz) in game._footprint_cells_for(x, z, footprint):
                    draw_cube(vec3(cx + 0.5, gy + 0.03, cz + 0.5), 0.96, 0.05, 0.96, c)
                draw_cube(vec3(x + 0.5, gy + by, z + 0.5), bw, bh, bd, c)
                set_alpha(1.0)
                # 竖井格：略高的绿色小柱，示意可向下开采
                ring = int(tt.mining_attr("ring", 1) or 1)
                m, n = footprint
                for (cx, cz) in game._footprint_cells_for(x, z, footprint):
                    if (ring <= cx - (x - (m // 2)) < m - ring
                            and ring <= cz - (z - (n // 2)) < n - ring):
                        set_alpha(0.55)
                        draw_cube(vec3(cx + 0.5, gy + 0.12, cz + 0.5), 0.5, 0.18, 0.5, c)
                        set_alpha(1.0)
            else:
                # 底格高亮（同武器分支）：底板 = 实际占格，幽灵身体只是造型
                draw_cube(vec3(x + 0.5, gy + 0.03, z + 0.5), 0.96, 0.05, 0.96, c)
                draw_cube(vec3(x + 0.5, gy + by, z + 0.5), bw, bh, bd, c)
                set_alpha(1.0)
            if not valid:
                self._draw_blocker_marker(game, blocker)
            # 射程环：防御塔无射程（base_range=0）不画，避免出现退化/异常圆环
            if rng > 0:
                set_alpha(uc.PLACE_PREVIEW_RANGE_ALPHA)
                draw_ring_3d(vec3(x + 0.5, gy + 0.05, z + 0.5), rng,
                             color(*uc.PLACE_PREVIEW_RANGE_COLOR))
                set_alpha(1.0)
        else:
            self._draw_2d_preview(x, z, rng, ok)

    def _render_power_preview(self, game, x, z, wy, is_3d, uc):
        """电力放置预览：电线幽灵（有效绿/无效红）；电站/电池沿用塔幽灵 + 条件。"""
        name = game.selected_power
        if name in gamedata.WIRES_RAW:
            reason = game._wire_placement_reason(x, z)
            valid = reason is None
            ok = valid and game.money >= game._power_cost(name)
            y = float(game.universe.surface_height_at(x, z) + 1)
            c = color(*uc.PLACE_PREVIEW_VALID_COLOR) if ok else color(*uc.PLACE_PREVIEW_INVALID_COLOR)
            if is_3d:
                set_alpha(uc.PLACE_PREVIEW_ALPHA_VALID if ok else uc.PLACE_PREVIEW_ALPHA_INVALID)
                # 电线：细扁长条（横向），示意输电介质
                draw_cube(vec3(x + 0.5, y + 0.05, z + 0.5), 0.9, 0.06, 0.18, c)
                set_alpha(1.0)
                if not valid:
                    self._draw_blocker_marker(game, (x + 0.5, y, z + 0.5))
            else:
                self._draw_2d_preview(x, z, 0.0, ok)
            return
        # 电站/电池
        tower_type = TOWER_TYPES.get(name)
        if tower_type is None:
            return
        gy = float(game.universe.stand_y_for_hit(x, z, wy))
        reason, blocker = game._placement_block_reason(x, z, gy)
        # 电站特殊条件（水电站贴水）预览也标红
        cond_reason = None
        if name in gamedata.STATIONS_RAW:
            cond = getattr(tower_type, "condition_kind", None)
            if cond == "water_adjacent" and not game._water_adjacent(x, z):
                cond_reason = "水电站须邻接水域"
        # 电池异型邻接拒绝预览
        if name in gamedata.BATTERIES_RAW:
            for dx, dz in power_mod.DXDZ:
                nb = game._towers_by_cell.get((x + dx, z + dz))
                if nb is not None and getattr(nb, "power_kind", None) == "battery" \
                        and nb.type_name != name:
                    cond_reason = "异型电池不可混接"
                    break
        valid = reason is None and cond_reason is None
        ok = valid and game.money >= game._power_cost(name)
        bw, bh, bd, by = tower_type.body_dims()
        if is_3d:
            c = color(*uc.PLACE_PREVIEW_VALID_COLOR) if ok else color(*uc.PLACE_PREVIEW_INVALID_COLOR)
            set_alpha(uc.PLACE_PREVIEW_ALPHA_VALID if ok else uc.PLACE_PREVIEW_ALPHA_INVALID)
            draw_cube(vec3(x + 0.5, gy + 0.03, z + 0.5), 0.96, 0.05, 0.96, c)
            draw_cube(vec3(x + 0.5, gy + by, z + 0.5), bw, bh, bd, c)
            set_alpha(1.0)
            if not valid:
                self._draw_blocker_marker(game, blocker or (x + 0.5, gy, z + 0.5))
        else:
            self._draw_2d_preview(x, z, 0.0, ok)

    def _draw_blocker_marker(self, game, blocker):
        """把"为什么红"标到阻挡单位所在格子（敌人/已有塔）。

        远 zoom 下敌人只有几个像素；敌人还可能在隧道/拱洞内（地表看不到），
        所以除红色底板外加一根短光柱，穿出地形便于定位。
        """
        if blocker is None:
            return
        fx, fz = math.floor(blocker[0]), math.floor(blocker[2])
        gy = game.universe.surface_height_at(fx, fz) + 1.0
        c = color(*ui_config.PLACE_PREVIEW_INVALID_COLOR)
        set_alpha(ui_config.PLACE_PREVIEW_ALPHA_INVALID)
        draw_cube(vec3(fx + 0.5, gy + 0.06, fz + 0.5), 0.96, 0.06, 0.96, c)
        set_alpha(1.0)
        set_alpha(0.45)
        draw_cube(vec3(fx + 0.5, gy + 1.3, fz + 0.5), 0.14, 2.6, 0.14, c)
        set_alpha(1.0)

    def _draw_belt_shape(self, game, kind, direction, cx, y, cz, c):
        """按带型在 (cx,y,cz) 处画传送带示意（预览与实塔共用几何）。

        仅画造型，颜色/透明度由调用方设置。kind ∈ belt/splitter/bridge。
        """
        if kind == "bridge":
            da, db = BRIDGE_LANES[direction % 4]
            # 两条正交 lane：沿其轴向画长条；第二条抬高以示立交
            self._draw_lane_strip(da, cx, y, cz, c)
            self._draw_lane_strip(db, cx, y + 0.22, cz, c)
            return
        # belt / splitter：底部主体扁盒
        draw_cube(vec3(cx, y, cz), 0.9, 0.1, 0.9, c)
        if kind == "splitter":
            d = direction % 4
            # 输入箭头（朝输入侧，即反方向）+ 两路输出箭头（左右各 90°）
            _ix, _iz = -DXDZ[d][0], -DXDZ[d][1]
            draw_cube(vec3(cx + _ix * 0.3, y + 0.09, cz + _iz * 0.3), 0.16, 0.06, 0.16, c)
            for od in (belts_mod.left(d), belts_mod.right(d)):
                draw_cube(vec3(cx + DXDZ[od][0] * 0.3, y + 0.09, cz + DXDZ[od][1] * 0.3),
                          0.16, 0.06, 0.16, c)
        else:
            d = direction % 4
            draw_cube(vec3(cx + DXDZ[d][0] * 0.3, y + 0.09, cz + DXDZ[d][1] * 0.3),
                      0.16, 0.06, 0.16, c)

    def _draw_lane_strip(self, lane_dir, cx, y, cz, c):
        """桥单条 lane 的扁长条（沿 lane_dir 轴向）。"""
        if lane_dir in (belts_mod.E, belts_mod.W):
            draw_cube(vec3(cx, y, cz), 0.9, 0.08, 0.22, c)
        else:
            draw_cube(vec3(cx, y, cz), 0.22, 0.08, 0.9, c)

    def _draw_2d_preview(self, x, z, rng, ok):
        """2D 视图放置预览：格子矩形 + 射程圆线。

        camera_extra 在 begin_camera2d 世界变换内被调用，与
        _render_entities_2d 同约定（世界坐标，z 当屏幕 y）。
        """
        uc = ui_config
        set_alpha(uc.PLACE_PREVIEW_ALPHA_VALID if ok else uc.PLACE_PREVIEW_ALPHA_INVALID)
        c = color(*uc.PLACE_PREVIEW_VALID_COLOR) if ok else color(*uc.PLACE_PREVIEW_INVALID_COLOR)
        draw_rect_v(vec2(x + 0.05, z + 0.05), vec2(0.9, 0.9), c)
        set_alpha(1.0)
        if rng > 0:
            set_alpha(uc.PLACE_PREVIEW_RANGE_ALPHA)
            draw_circle_lines_v(vec2(x + 0.5, z + 0.5), rng, color(*uc.PLACE_PREVIEW_RANGE_COLOR))
            set_alpha(1.0)

    def render_dismantle_highlight(self, game):
        """渲染拆除模式高亮（按各塔自身箱体几何包住塔身）"""
        # 仅 3D 相机绘制：2D 正交投影下 DrawCube 是 3D 图元，画不出正确高亮。
        # 与 render_block_edit_preview 一致，2D 交由地形渲染覆盖（BUG-086）。
        if not game.camera.is_3d():
            return
        uc = ui_config
        color_val = uc.DISMANTLE_HIGHLIGHT_COLOR
        for tower in game.towers:
            pos = tower.position
            # 按塔实际足迹取箱体（开采塔默认用 footprint_min 会画小、包不住真实平台，BUG-087）
            bw, bh, bd, by = tower.type_def.body_dims(getattr(tower, "footprint", None))
            draw_cube((pos[0], pos[1] + by, pos[2]), bw + 0.1, bh + 0.1, bd + 0.1, color_val)

    # ─── 玩家挖/放方块 UI ───────────────────────────────────

    def _render_block_bar(self, game, bar_y):
        """放置模式的方块选择栏（复用塔栏分段样式，布局与 block_bar_hit 同构）。"""
        uc = ui_config
        self._block_bar_rects = []
        seg_x = 10
        draw_text("Blocks:", seg_x, bar_y + 8, uc.HUD_SMALL_TEXT_SIZE,
                  color(*uc.HUD_TEXT_TOOL))
        seg_x += measure_text("Blocks:", uc.HUD_SMALL_TEXT_SIZE) + 10
        for i, bdef in enumerate(get_placeable_blocks()):
            label = bdef.name
            seg_text = f"[{i + 1}]{label}"
            seg_w = measure_text(seg_text, uc.HUD_SMALL_TEXT_SIZE) + 8
            seg_y, seg_h = bar_y + 5, 20
            selected = (game.selected_block_id == bdef.id)
            if selected:
                seg_bg = uc.TOWER_BAR_SELECTED_BG
            else:
                seg_bg = uc.TOWER_BAR_SEG_BG
            draw_rect(seg_x, seg_y, seg_w, seg_h, color(*seg_bg))
            if selected:
                draw_rect_lines(rect(seg_x, seg_y, seg_w, seg_h), 1,
                                color(*uc.TOWER_BAR_SELECTED_BORDER))
            draw_text(seg_text, seg_x + 4, seg_y + 4, uc.HUD_SMALL_TEXT_SIZE,
                      color(*uc.HUD_TEXT_TOOL))
            self._block_bar_rects.append((seg_x, seg_y, seg_w, seg_h, bdef.id))
            seg_x += seg_w + 4

    def block_bar_hit(self, mx, my):
        """方块选择栏鼠标命中：返回方块 ID 或 None。"""
        for rx, ry, rw, rh, block_id in getattr(self, "_block_bar_rects", []):
            if rx <= mx <= rx + rw and ry <= my <= ry + rh:
                return block_id
        return None

    def _render_belt_bar(self, game, bar_y):
        """布带模式的传送带选择栏（复用塔栏分段样式，布局与 belt_bar_hit 同构）。"""
        uc = ui_config
        self._belt_bar_rects = []
        seg_x = 10
        draw_text("Belts:", seg_x, bar_y + 8, uc.HUD_SMALL_TEXT_SIZE,
                  color(*uc.HUD_TEXT_TOOL))
        seg_x += measure_text("Belts:", uc.HUD_SMALL_TEXT_SIZE) + 10
        for i, kind in enumerate(("belt", "splitter", "bridge")):
            bdef = gamedata.BELTS_RAW.get(kind, {})
            label = bdef.get("name_cn", kind)
            missing = [t for t in bdef.get("requires_tech", []) if not game.has_tech(t)]
            seg_text = f"[{i + 1}]{label}" + (" (lock)" if missing else "")
            seg_w = measure_text(seg_text, uc.HUD_SMALL_TEXT_SIZE) + 8
            seg_y, seg_h = bar_y + 5, 20
            selected = (game.selected_belt == kind)
            if missing:
                seg_bg = uc.TOWER_BAR_LOCK_BG
            elif selected:
                seg_bg = uc.TOWER_BAR_SELECTED_BG
            else:
                seg_bg = uc.TOWER_BAR_SEG_BG
            draw_rect(seg_x, seg_y, seg_w, seg_h, color(*seg_bg))
            if selected:
                draw_rect_lines(rect(seg_x, seg_y, seg_w, seg_h), 1,
                                color(*uc.TOWER_BAR_SELECTED_BORDER))
            seg_color = uc.TOWER_BAR_LOCK_TEXT if missing else uc.HUD_TEXT_TOOL
            draw_text(seg_text, seg_x + 4, seg_y + 4, uc.HUD_SMALL_TEXT_SIZE,
                      color(*seg_color))
            self._belt_bar_rects.append((seg_x, seg_y, seg_w, seg_h, kind))
            seg_x += seg_w + 4

    def belt_bar_hit(self, mx, my):
        """传送带选择栏鼠标命中：返回带型（belt/splitter/bridge）或 None。"""
        for rx, ry, rw, rh, kind in getattr(self, "_belt_bar_rects", []):
            if rx <= mx <= rx + rw and ry <= my <= ry + rh:
                return kind
        return None

    def _power_options(self, game):
        """电力模式可选类型（与 game._power_options 一致，避免重复）。"""
        opts = list(gamedata.WIRES_RAW.keys())
        opts += list(gamedata.STATIONS_RAW.keys())
        opts += list(gamedata.BATTERIES_RAW.keys())
        return opts

    def _power_label(self, name):
        """电力类型显示名。"""
        if name in gamedata.WIRES_RAW:
            return gamedata.WIRES_RAW[name].get("name_cn", name)
        if name in gamedata.STATIONS_RAW:
            return gamedata.STATIONS_RAW[name].get("name_cn", name)
        if name in gamedata.BATTERIES_RAW:
            return gamedata.BATTERIES_RAW[name].get("name_cn", name)
        return name

    def _power_requires(self, name):
        """电力类型科技前置。"""
        if name in gamedata.WIRES_RAW:
            return gamedata.WIRES_RAW[name].get("requires_tech", [])
        if name in gamedata.STATIONS_RAW:
            return gamedata.STATIONS_RAW[name].get("requires_tech", [])
        if name in gamedata.BATTERIES_RAW:
            return gamedata.BATTERIES_RAW[name].get("requires_tech", [])
        return []

    def _render_power_bar(self, game, bar_y):
        """电力模式的电线/电站/电池选择栏（分段样式，布局与 power_bar_hit 同构）。"""
        uc = ui_config
        self._power_bar_rects = []
        seg_x = 10
        draw_text("Power:", seg_x, bar_y + 8, uc.HUD_SMALL_TEXT_SIZE,
                  color(*uc.HUD_TEXT_TOOL))
        seg_x += measure_text("Power:", uc.HUD_SMALL_TEXT_SIZE) + 10
        for i, name in enumerate(self._power_options(game)):
            missing = [t for t in self._power_requires(name) if not game.has_tech(t)]
            # 仅前 9 项标数字热键（game.py 电力模式键处理只支持 1..9）；
            # 电池等后续项仅鼠标点选，不显示不存在的热键（BUG-088）。
            seg_label = (f"[{i + 1}]" if i < 9 else "") + self._power_label(name)
            seg_text = seg_label + (" (lock)" if missing else "")
            seg_w = measure_text(seg_text, uc.HUD_SMALL_TEXT_SIZE) + 8
            seg_y, seg_h = bar_y + 5, 20
            selected = (game.selected_power == name)
            if missing:
                seg_bg = uc.TOWER_BAR_LOCK_BG
            elif selected:
                seg_bg = uc.TOWER_BAR_SELECTED_BG
            else:
                seg_bg = uc.TOWER_BAR_SEG_BG
            draw_rect(seg_x, seg_y, seg_w, seg_h, color(*seg_bg))
            if selected:
                draw_rect_lines(rect(seg_x, seg_y, seg_w, seg_h), 1,
                                color(*uc.TOWER_BAR_SELECTED_BORDER))
            seg_color = uc.TOWER_BAR_LOCK_TEXT if missing else uc.HUD_TEXT_TOOL
            draw_text(seg_text, seg_x + 4, seg_y + 4, uc.HUD_SMALL_TEXT_SIZE,
                      color(*seg_color))
            self._power_bar_rects.append((seg_x, seg_y, seg_w, seg_h, name))
            seg_x += seg_w + 4

    def power_bar_hit(self, mx, my):
        """电力选择栏鼠标命中：返回电力类型名（电线/电站/电池）或 None。"""
        for rx, ry, rw, rh, name in getattr(self, "_power_bar_rects", []):
            if rx <= mx <= rx + rw and ry <= my <= ry + rh:
                return name
        return None

    def _render_power_summary(self, game, bar_y):
        """电网摘要行（电力模式下显示活动组件/电压/发电/损耗/弃电/跳闸）。"""
        uc = ui_config
        st = getattr(game, "power_state", None)
        if st is None:
            return
        s = st.summarize()
        txt = (f"电网: 发电 {_fmt_power(s['generation_w'])} | "
               f"充电 {_fmt_power(s['charge_w'])} | "
               f"损耗 {_fmt_power(s['loss_w'])} | "
               f"弃电 {_fmt_power(s['waste_w'])} | "
               f"跳闸 {s['tripped']} 段")
        # 抬到研究摘要行（bar_y-22）之上，避免两行 2px 间距内的字形重叠（BUG-136）
        draw_text(txt, 10, bar_y - 36, uc.HUD_TINY_TEXT_SIZE, color(200, 220, 255))

    def render_block_edit_preview(self, game):
        """挖掘/放置模式的方块编辑预览（仅 3D 相机绘制，2D 由地形渲染覆盖）。"""
        uc = ui_config
        if not game.camera.is_3d():
            return
        if game.interaction_mode == "dig":
            # 与 _try_dig_block 同源：预览射线命中的那一格，而不是通行列顶
            # （敌人隧道列顶覆盖层会低于悬垂，避免高亮/挖掘落进洞里）。
            cell = game.camera.screen_to_block(rl.GetMouseX(), rl.GetMouseY())
            if cell is None:
                return
            x, y, z = cell
            top_id = game.universe.get_block(x, y, z)
            bdef = get_block_by_name(BLOCK_NAME.get(top_id, "air"))
            harvestable = (bdef is not None and bdef.harvest_material is not None)
            c = color(*uc.PLACE_PREVIEW_VALID_COLOR) if harvestable \
                else color(*uc.PLACE_PREVIEW_INVALID_COLOR)
            set_alpha(uc.PLACE_PREVIEW_ALPHA_VALID if harvestable
                      else uc.PLACE_PREVIEW_ALPHA_INVALID)
            sz = uc.BLOCK_EDIT_PREVIEW_CUBE_SIZE
            # 方块 y 占 [y, y+1]，中心 y+0.5；略放大让高亮面探出地形，
            # 否则整格嵌在实心体里会被深度遮挡（侧脸命中尤其明显）。
            draw_cube(vec3(x + 0.5, y + 0.5, z + 0.5), sz[0], sz[1], sz[2], c)
            set_alpha(1.0)
        else:  # place
            if game.selected_block_id is None:
                return
            wx, _wy, wz = game.camera.screen_to_world(rl.GetMouseX(), rl.GetMouseY())
            x, z = math.floor(wx), math.floor(wz)
            bdef = get_block_by_name(BLOCK_NAME.get(game.selected_block_id, "air"))
            if bdef is None or not bdef.placeable:
                return
            # _is_valid_placement 需要落脚高度 stand_y（三参必填），
            # 原两参调用每帧抛 TypeError 崩溃（BUG-005），与 game.py 放置路径
            # 一致地用 stand_y_for_hit 取列顶+1 / 洞底。
            stand_y = game.universe.stand_y_for_hit(x, z, _wy)
            valid = game._is_valid_placement(x, z, stand_y)
            mat = bdef.place_material or bdef.harvest_material
            has_mat = bool(mat) and game.inventory.get_material(mat) >= 1
            ok = valid and has_mat
            c = color(*uc.PLACE_PREVIEW_VALID_COLOR) if ok \
                else color(*uc.PLACE_PREVIEW_INVALID_COLOR)
            set_alpha(uc.PLACE_PREVIEW_ALPHA_VALID if ok
                      else uc.PLACE_PREVIEW_ALPHA_INVALID)
            # 放置方块占 [surface+1, surface+2]，中心 surface+1.5；
            # 原用 +1.0 使幽灵半格陷入地面，误导落点。
            y = game.universe.surface_height_at(x, z) + 1.5
            sz = uc.PLACE_PREVIEW_CUBE_SIZE
            draw_cube(vec3(x + 0.5, y, z + 0.5), sz[0], sz[1], sz[2], c)
            set_alpha(1.0)