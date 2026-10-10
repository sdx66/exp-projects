"""
ui_renderer.py - UI 渲染器
从 Game 类中提取的所有 UI 渲染逻辑（HUD、面板、公告、结束界面）
"""

import raylib as rl
import math
import config
import ui_config
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
        draw_text(f"Wave: {game.wave}", uc.HUD_WAVE_X, 12, uc.HUD_TEXT_SIZE, color(*uc.HUD_TEXT_WHITE))
        draw_text(f"Money: {game.money}", uc.HUD_MONEY_X, 12, uc.HUD_TEXT_SIZE, color(*uc.HUD_TEXT_MONEY))

        # 大本营血条
        hp_ratio = game.base.health / game.base.max_health
        draw_rect(uc.HUD_BASE_BAR_X, uc.HUD_BASE_BAR_Y, uc.HUD_BASE_BAR_W, uc.HUD_BASE_BAR_H, color(80, 30, 30))
        hp_color = color(220, 80, 80) if hp_ratio < 0.3 else color(220, 180, 80)
        draw_rect(uc.HUD_BASE_BAR_X, uc.HUD_BASE_BAR_Y,
                  int(uc.HUD_BASE_BAR_W * hp_ratio), uc.HUD_BASE_BAR_H, hp_color)
        draw_text(f"Base: {game.base.health}/{game.base.max_health}",
                  uc.HUD_BASE_BAR_X + 4, uc.HUD_BASE_BAR_Y + 2, uc.HUD_TINY_TEXT_SIZE,
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

        # 武器/工具栏
        w_mat_x = uc.HUD_MAT_X_START
        for wname, wdef in WEAPON_TYPES.items():
            count = game.inventory.get_weapon(wname)
            if count > 0:
                wc = wdef.color
                label = f"{wdef.label_cn}:{count}"
                draw_text(label, w_mat_x, uc.HUD_MAT_Y, uc.HUD_SMALL_TEXT_SIZE, color(*wc))
                w_mat_x += measure_text(label, uc.HUD_SMALL_TEXT_SIZE) + 12
        if game.inventory.tools:
            tool_str = f"Tools:{', '.join(sorted(game.inventory.tools))}"
            draw_text(tool_str, screen_w - uc.HUD_TOOL_X_OFFSET, uc.HUD_MAT_Y,
                      uc.HUD_TINY_TEXT_SIZE, color(*uc.HUD_TEXT_TOOL))

        # 塔选择栏
        bar_y = screen_h - uc.HUD_BAR_Y_OFFSET
        draw_rect(0, bar_y, screen_w, uc.HUD_BAR_HEIGHT, color(*uc.HUD_BAR_BG))

        parts = ["Towers:"]
        for key, label, type_name in zip(uc.TOWER_BAR_NUMS, uc.TOWER_BAR_LABELS, uc.TOWER_BAR_KEYS):
            tt = TOWER_TYPES[type_name]
            if tt.requires_tech:
                missing = [t for t in tt.requires_tech if t not in game.unlocked_tech]
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
            draw_text("V:2D/3D | T:Tech | B:Craft | X:Dismantle | Click:Harvest/Upgrade",
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
        """渲染波次公告"""
        screen_h = rl.GetScreenHeight()
        uc = ui_config

        alpha = min(1.0, game.wave_announcement_timer)
        set_alpha(alpha)
        draw_text_centered(f"WAVE {game.wave}",
                           screen_h // 2 - 20, uc.WAVE_ANNOUNCE_TITLE_SIZE, color(255, 255, 255))
        if game.between_waves:
            draw_text_centered(f"Next wave in {game.wave_timer:.1f}s",
                               screen_h // 2 + 20, uc.WAVE_ANNOUNCE_DETAIL_SIZE, color(200, 200, 220))
        else:
            draw_text_centered(f"Enemies: {game.wave_enemy_count}/{game.wave_max_enemies}",
                               screen_h // 2 + 20, uc.WAVE_ANNOUNCE_DETAIL_SIZE, color(200, 200, 220))
        set_alpha(1.0)

    def render_tech_panel(self, game):
        """渲染科技面板"""
        screen_w = rl.GetScreenWidth()
        screen_h = rl.GetScreenHeight()
        uc = ui_config

        set_alpha(uc.PANEL_BG_ALPHA)
        draw_rect(0, 0, screen_w, screen_h, color(*uc.PANEL_OVERLAY_COLOR))
        set_alpha(1.0)

        pw, ph = uc.TECH_PANEL_W, uc.TECH_PANEL_H
        px = (screen_w - pw) // 2
        py = (screen_h - ph) // 2

        draw_rect(px, py, pw, ph, color(*uc.TECH_PANEL_BG))
        draw_rect_lines(rect(px, py, pw, ph), 2, color(*uc.TECH_PANEL_BORDER))

        draw_text_centered("TECHNOLOGY TREE", py + 20, uc.TECH_PANEL_TITLE_SIZE, color(*uc.TECH_PANEL_TITLE_COLOR))
        draw_text(f"Money: {game.money}   |   Press 1-5 to research   |   T/Esc: close",
                  px + 10, py + ph - 30, uc.TECH_PANEL_FOOTER_SIZE, color(*uc.TECH_PANEL_FOOTER_COLOR))

        tech_y = py + 62
        draw_text(f"Unlocked: {', '.join(game.unlocked_tech) if game.unlocked_tech else 'None'}",
                  px + 10, tech_y, uc.TECH_PANEL_HEADER_SIZE, color(*uc.TECH_PANEL_UNLOCKED_COLOR))
        tech_y += 28

        draw_text("Available Research:", px + 10, tech_y, uc.TECH_PANEL_HEADER_SIZE, color(*uc.TECH_PANEL_HEADER_COLOR))
        tech_y += 22

        for i, (tech, defn) in enumerate(config.TECH_TREE.items()):
            if tech in game.unlocked_tech:
                mark, tc = "[OK]", uc.TECH_PANEL_UNLOCKED_COLOR
                detail = uc.TECH_DESC.get(tech, "")
            elif game.tech_researching == tech:
                frac = 1.0 - game.tech_research_timer / max(1e-6, game.tech_research_total)
                mark = f"[{frac*100:3.0f}%]"
                tc = uc.TECH_PANEL_RESEARCHING_COLOR
                detail = f"研究中 {game.tech_research_timer:.1f}s / {defn['time']}s"
            else:
                missing = [r for r in defn["requires"] if r not in game.unlocked_tech]
                if missing:
                    mark, tc = "[ - ]", uc.TECH_PANEL_DISABLED_COLOR
                    detail = f"需要: {', '.join(missing)}"
                elif game.tech_researching:
                    mark, tc = "[ - ]", uc.TECH_PANEL_DISABLED_COLOR
                    detail = "已有研究进行中"
                elif game.money < defn["cost"]:
                    mark, tc = "[ $ ]", uc.TECH_PANEL_POOR_COLOR
                    detail = f"钱不够 (需 {defn['cost']})"
                else:
                    mark, tc = "[  ]", uc.TECH_PANEL_AVAILABLE_COLOR
                    detail = f"费用 {defn['cost']} / {defn['time']}s"
                detail += "  " + uc.TECH_DESC.get(tech, "")

            draw_text(f"  [{i+1}] {mark} {tech:<18} {detail}", px + 10, tech_y, uc.TECH_PANEL_ITEM_SIZE, color(*tc))
            tech_y += 30

        if game.tech_researching:
            frac = 1.0 - game.tech_research_timer / max(1e-6, game.tech_research_total)
            bar_w = pw - 40
            draw_rect(px + 20, tech_y + 8, bar_w, 10, color(*uc.TECH_PANEL_PROGRESS_BG))
            draw_rect(px + 20, tech_y + 8, int(bar_w * frac), 10, color(*uc.TECH_PANEL_PROGRESS_FG))

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
        for mat in ("wood", "stone", "sand", "iron_ore", "plank", "cobble", "iron_ingot"):
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
                hand_ok = recipe["hand"] > 0 and in_amt > 0
                tool_needed = recipe.get("requires_tool")
                tool_ok = tool_needed and game.inventory.has_tool(tool_needed) and in_amt > 0

                # 第 1 行：配方名（有原料则高亮）
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
            can_craft = can_craft_weapon(wname, game.inventory)
            recipe_str = " + ".join(f"{MATERIAL_NAMES_CN.get(m, m)}x{a}" for m, a in wdef.recipe.items())
            if can_craft:
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
                can_craft = can_craft_tool(tname, game.inventory)
                recipe_str = " + ".join(f"{MATERIAL_NAMES_CN.get(m, m)}x{a}" for m, a in tdef.recipe.items())
                if can_craft:
                    draw_text(f"  [{tdef.label_cn}] {owned}  {recipe_str}  [Craft]",
                              rx, y, uc.CRAFT_PANEL_ITEM_SIZE, color(*uc.CRAFT_PANEL_TOOL_OK_COLOR))
                else:
                    draw_text(f"  [{tdef.label_cn}] {owned}  {recipe_str}",
                              rx, y, uc.CRAFT_PANEL_ITEM_SIZE, color(*uc.CRAFT_PANEL_DISABLED_COLOR))
                y += row_h

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
            draw_cube(vec3(x + 0.5, game.universe.get_surface_height_safe(x, z) + 1.0, z + 0.5),
                      sz[0], sz[1], sz[2], c)
            set_alpha(1.0)
            if valid:
                set_alpha(uc.PLACE_PREVIEW_RANGE_ALPHA)
                draw_circle_3d(vec3(x + 0.5, game.universe.get_surface_height_safe(x, z) + 0.1, z + 0.5),
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
        draw_cube(vec3(x + 0.5, game.universe.get_surface_height_safe(x, z) + 1.0, z + 0.5),
                  sz[0], sz[1], sz[2], c)
        set_alpha(1.0)

        if valid:
            set_alpha(uc.PLACE_PREVIEW_RANGE_ALPHA)
            draw_circle_3d(vec3(x + 0.5, game.universe.get_surface_height_safe(x, z) + 0.1, z + 0.5),
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