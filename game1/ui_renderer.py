"""
ui_renderer.py - UI 渲染器
从 Game 类中提取的所有 UI 渲染逻辑（HUD、面板、公告、结束界面）
"""

import raylib as rl
import math
import gamedata
import ui_config
import save_system
from entities import Tower, Enemy, Projectile, Pickup, Base, TOWER_TYPES, ENEMY_TYPES
from items import (
    MATERIAL_NAMES_CN, MATERIAL_COLORS,
    WEAPON_TYPES, TOOL_TYPES, PROCESS_RECIPES,
    can_craft_weapon, can_craft_tool,
)
from rl_utils import *


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
        draw_text(f"Spawners: {active_count}A/{scouting_count}S/{dormant_count}D  (P:详情)",
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
        draw_text(f"FPS: {game.fps}", screen_w - uc.HUD_FPS_X_OFFSET, 12, uc.HUD_TEXT_SIZE, color(*uc.HUD_TEXT_FPS))

        # 材料栏
        draw_rect(0, uc.HUD_TOP_HEIGHT, screen_w, uc.HUD_MATERIAL_HEIGHT, color(*uc.HUD_MATERIAL_BG))
        mat_x = uc.HUD_MAT_X_START
        for mat_name in ("wood", "stone", "sand", "iron_ore", "plank", "cobble", "iron_ingot"):
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

        # 塔选择栏
        bar_y = screen_h - uc.HUD_BAR_Y_OFFSET
        draw_rect(0, bar_y, screen_w, uc.HUD_BAR_HEIGHT, color(*uc.HUD_BAR_BG))

        parts = ["Towers:"]
        for key, label, type_name in zip(uc.TOWER_BAR_NUMS, uc.TOWER_BAR_LABELS, uc.TOWER_BAR_KEYS):
            tt = TOWER_TYPES[type_name]
            if tt.requires_tech:
                missing = [t for t in tt.requires_tech if not game.has_tech(t)]
                if missing:
                    parts.append(f"[{key}]{label} (lock)")
                    continue
            parts.append(f"[{key}]{label}")
        draw_text(" ".join(parts), 10, bar_y + 8, uc.HUD_SMALL_TEXT_SIZE, color(*uc.HUD_TEXT_TOOL))

        if game.interaction_mode == "dismantle":
            draw_text("[X] Dismantle: ON | Click tower to remove", 10, bar_y + 28,
                      uc.HUD_TINY_TEXT_SIZE, color(255, 120, 120))
        elif game.craft_panel_open:
            draw_text("[B] Craft Panel Open", 10, bar_y + 28,
                      uc.HUD_TINY_TEXT_SIZE, color(255, 200, 100))
        elif game.selected_weapon:
            draw_text(f"Placing: {game.selected_weapon.label_cn} (${game.selected_weapon.install_fee}) | Esc:cancel",
                      10, bar_y + 28, uc.HUD_TINY_TEXT_SIZE, color(180, 255, 180))
        else:
            draw_text("V:2D/3D | T:Tech | B:Craft | X:Dismantle | F8:存档 | Click:Harvest/Upgrade",
                      10, bar_y + 28, uc.HUD_TINY_TEXT_SIZE, color(*uc.HUD_TEXT_DIM))

        # 当前选择
        if game.selected_tower_type:
            tc = color(*game.selected_tower_type.color)
            draw_circle_v(vec2(screen_w - 100, bar_y + 15), 8, tc)
            draw_text(f"${game.tower_cost(game.selected_tower_type)}",
                      screen_w - 85, bar_y + 10, uc.HUD_SMALL_TEXT_SIZE, color(*uc.HUD_TEXT_MONEY))

        # 正在研究科技
        if game.tech_researching:
            frac = 1.0 - game.tech_research_timer / max(1e-6, game.tech_research_total)
            draw_text(f"Researching: {game.tech_researching} ({frac*100:.0f}%)",
                      10, bar_y - 22, uc.HUD_SMALL_TEXT_SIZE, color(255, 200, 100))

        # 屏幕提示
        if game.toast_timer > 0 and game.toast_message:
            draw_text_centered(game.toast_message, bar_y - 22, 14, color(255, 220, 140))

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
        """武器库面板：点击选择一把武器用于放置（C 键打开）。"""
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
        draw_text("C / Esc: close   点击选择武器放置", px + 10, py + ph - 26, 13,
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

        # 按距离排序
        sorted_spawners = sorted(
            game.spawners.values(),
            key=lambda rt: _math.hypot(
                game.universe.spawners[rt.spawner_id].x - game.universe.base_x,
                game.universe.spawners[rt.spawner_id].z - game.universe.base_z))

        # 分页
        total = len(sorted_spawners)
        total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        page = min(game.spawner_panel_page, total_pages - 1)
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
            sp = game.universe.spawners[rt.spawner_id]
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

            # 波次进度条（仅 active）
            if rt.mode == "active" and rt.max_enemies > 0:
                frac = min(1.0, rt.enemy_count / rt.max_enemies)
                bar_w = pw - 30
                draw_rect(px + 15, y + 2, bar_w, 6, color(40, 40, 50))
                draw_rect(px + 15, y + 2, int(bar_w * frac), 6, color(*mc))
                y += 10

            y += 4

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
        panel_w = 560
        row_h = 36
        row_gap = 6
        px = (screen_w - panel_w) // 2
        base_y = screen_h // 2 - 140
        rows = 1 + len(game.save_menu_items)  # 1=新建 + n=存档
        panel_h = 70 + rows * (row_h + row_gap) + 40  # 标题 + 行 + 底部
        if panel_h < 120:
            panel_h = 120

        draw_rect(px, base_y - 40, panel_w, panel_h, color(30, 35, 50))
        draw_rect_lines(rect(px, base_y - 40, panel_w, panel_h), 2, color(80, 100, 140))

        # 标题
        draw_text_centered("存档管理", base_y - 20, 22, color(220, 220, 240))

        # 菜单行
        for i in range(rows):
            y = base_y + i * (row_h + row_gap)
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
            confirm_y = base_y + rows * (row_h + row_gap) + 8
            draw_rect(px + 16, confirm_y, panel_w - 32, 28, color(60, 30, 30))
            draw_text_centered("按 Del 确认删除该存档", confirm_y + 4, 14, color(255, 180, 180))

        # 错误提示
        if game.save_menu_error:
            err_y = base_y + rows * (row_h + row_gap) + (40 if game.save_menu_confirm_delete else 12)
            draw_text_centered(f"! {game.save_menu_error}", err_y, 14, color(255, 150, 150))

        # 底部提示
        footer_y = base_y - 40 + panel_h - 28
        draw_text_centered("↑↓ 选择   Enter 新建/载入   Del 删除   Esc/T 返回",
                           footer_y, 13, color(150, 155, 175))

    def render_placement_preview(self, game):
        """渲染塔放置预览（经典塔 + 武器塔）"""
        uc = ui_config
        wx, wy, wz = game.camera.screen_to_world(rl.GetMouseX(), rl.GetMouseY())
        x, z = math.floor(wx), math.floor(wz)

        if game.selected_weapon:
            valid = game._is_valid_placement(x, z)
            fee = game.selected_weapon.install_fee
            can_afford = game.money >= fee
            ok = valid and can_afford
            c = color(*uc.PLACE_PREVIEW_VALID_COLOR) if ok else color(*uc.PLACE_PREVIEW_INVALID_COLOR)
            set_alpha(uc.PLACE_PREVIEW_ALPHA_VALID if ok else uc.PLACE_PREVIEW_ALPHA_INVALID)
            sz = uc.PLACE_PREVIEW_CUBE_SIZE
            draw_cube(vec3(x + 0.5, game.universe._surface_height_pure(x, z) + 1.0, z + 0.5),
                      sz[0], sz[1], sz[2], c)
            set_alpha(1.0)
            if valid:
                set_alpha(uc.PLACE_PREVIEW_RANGE_ALPHA)
                draw_ring_3d(vec3(x + 0.5, game.universe._surface_height_pure(x, z) + 0.1, z + 0.5),
                             game.selected_weapon.range, color(*uc.PLACE_PREVIEW_RANGE_COLOR))
                set_alpha(1.0)
            return

        if not game.placing_tower or not game.selected_tower_type:
            return

        valid = game._is_valid_placement(x, z)
        cost = game.tower_cost(game.selected_tower_type)
        can_afford = game.money >= cost
        ok = valid and can_afford
        c = color(*uc.PLACE_PREVIEW_VALID_COLOR) if ok else color(*uc.PLACE_PREVIEW_INVALID_COLOR)
        set_alpha(uc.PLACE_PREVIEW_ALPHA_VALID if ok else uc.PLACE_PREVIEW_ALPHA_INVALID)
        sz = uc.PLACE_PREVIEW_CUBE_SIZE
        draw_cube(vec3(x + 0.5, game.universe._surface_height_pure(x, z) + 1.0, z + 0.5),
                  sz[0], sz[1], sz[2], c)
        set_alpha(1.0)

        if valid:
            set_alpha(uc.PLACE_PREVIEW_RANGE_ALPHA)
            draw_ring_3d(vec3(x + 0.5, game.universe._surface_height_pure(x, z) + 0.1, z + 0.5),
                         game.selected_tower_type.base_range, color(*uc.PLACE_PREVIEW_RANGE_COLOR))
            set_alpha(1.0)

    def render_dismantle_highlight(self, game):
        """渲染拆除模式高亮"""
        uc = ui_config
        color_val = uc.DISMANTLE_HIGHLIGHT_COLOR
        sz = uc.DISMANTLE_HIGHLIGHT_SIZE
        for tower in game.towers:
            pos = tower.position
            draw_cube((pos[0], pos[1] + 0.5, pos[2]), sz[0], sz[1], sz[2], color_val)