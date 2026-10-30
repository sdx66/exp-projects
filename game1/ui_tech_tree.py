"""
ui_tech_tree.py - 科技树 UI 模块

处理：
  - 两页切换：成果树 / 技术树
  - 时代切换（石器/青铜/铁器/蒸汽/电气）
  - 树形渲染（节点 + 边 + 时代分隔 + 分类标识）
  - 节点选择与信息面板
  - 研究按钮（仅技术页）
  - 缩放/平移/拖动
  - LOD（zoom < 阈值隐藏描述）
"""

import math
import raylib as rl
import gamedata
import ui_config as ui
import tech_tree as tt
from rl_utils import *


# ─── 面板状态 ───────────────────────────────────────────────

class TechTreePanel:
    """科技树面板状态和交互。"""

    def __init__(self, game=None):
        self.open = False
        self.game = game  # Game 引用，用于调用研究逻辑
        self.page = tt.PAGE_ACHIEVEMENTS  # 'ach' or 'tech'
        self.era_idx = 0
        self.view = tt.Viewport(0, 0, 1.0)
        self.selected_id = None
        self.info_visible = False
        self._dragging = False
        self._drag_start = (0, 0)
        self._drag_start_pan = (0, 0)
        self._hover_id = None
        self._hover_btn = False
        self._fit_done = False

    @property
    def era_id(self):
        return gamedata.TECH_ERAS[self.era_idx]["id"]

    def open_panel(self):
        """打开面板。"""
        self.open = True
        self._fit_done = False
        self._dragging = False

    def close_panel(self):
        """关闭面板。"""
        self.open = False
        self.selected_id = None
        self.info_visible = False
        self._dragging = False

    def toggle(self):
        """切换面板。"""
        if self.open:
            self.close_panel()
        else:
            self.open_panel()

    def switch_page(self, page):
        """切换页面。"""
        self.page = page
        self.selected_id = None
        self.info_visible = False
        self._fit_done = False
        self._dragging = False

    def switch_era(self, direction):
        """切换时代（direction: -1 上一个，+1 下一个）。"""
        self.era_idx = (self.era_idx + direction) % len(gamedata.TECH_ERAS)
        self.selected_id = None
        self.info_visible = False
        self._fit_done = False
        self._dragging = False

    def get_layout(self, viewport_w, viewport_h):
        """获取当前布局。"""
        tree = tt.tech_tree()
        positions, edges = tt.compute_layout(tree, self.era_id, self.page, viewport_w, viewport_h)
        return tree, positions, edges

    def content_bbox(self, positions):
        """计算内容包围盒。"""
        if not positions:
            return (0, 0, 1, 1)
        min_x = min(p[0] for p in positions.values())
        min_y = min(p[1] for p in positions.values())
        max_x = max(p[0] + p[2] for p in positions.values())
        max_y = max(p[1] + p[3] for p in positions.values())
        return (min_x, min_y, max_x - min_x, max_y - min_y)

    def fit_view(self, positions, viewport_w, viewport_h):
        """适配视口（扣除表头/底栏预留高度后居中）。"""
        bbox = self.content_bbox(positions)
        tt.fit_view(self.view, bbox, viewport_w, viewport_h,
                    inset_top=ui.TECHTREE_CONTENT_TOP,
                    inset_bottom=ui.TECHTREE_CONTENT_BOTTOM)
        self._fit_done = True

    def process_input(self, viewport_w, viewport_h):
        """处理输入。

        顺序：鼠标释放清理 → 滚轮缩放 → 页签/按钮命中 → 节点命中 →
        空白处按下开始拖动 → 拖动中按 IsMouseButtonDown 持续更新 → 末尾钳制平移。
        """
        if not self.open:
            return

        tree, positions, _ = self.get_layout(viewport_w, viewport_h)

        # 首次打开或切换页面/时代时适配视口
        if not self._fit_done:
            self.fit_view(positions, viewport_w, viewport_h)

        # Esc 关闭面板（main.py 已 SetExitKey(0)，Esc 不再顺带关窗）
        if rl.IsKeyPressed(rl.KEY_ESCAPE):
            self.close_panel()
            return

        # Shift+Tab 上一个时代，Tab 下一个时代
        if rl.IsKeyPressed(rl.KEY_TAB):
            shift = rl.IsKeyDown(rl.KEY_LEFT_SHIFT) or rl.IsKeyDown(rl.KEY_RIGHT_SHIFT)
            if shift:
                self.switch_era(-1)
            else:
                self.switch_era(1)
            return

        # 1/2 切换页面
        if rl.IsKeyPressed(rl.KEY_ONE):
            self.switch_page(tt.PAGE_ACHIEVEMENTS)
            return
        if rl.IsKeyPressed(rl.KEY_TWO):
            self.switch_page(tt.PAGE_BREAKTHROUGHS)
            return

        # F 重新适配
        if rl.IsKeyPressed(rl.KEY_F):
            self._fit_done = False
            self.fit_view(positions, viewport_w, viewport_h)
            return

        # 获取鼠标状态
        mouse_x = rl.GetMouseX()
        mouse_y = rl.GetMouseY()
        wheel = rl.GetMouseWheelMoveV()

        # 释放按钮时立即停止拖动（必须在命中测试之前，否则会被早退路径跳过）
        if rl.IsMouseButtonReleased(rl.MOUSE_BUTTON_LEFT):
            self._dragging = False

        # 滚轮缩放（放在所有命中测试之前，悬停节点/标签上也可缩放）
        wheel_val = wheel.y if hasattr(wheel, 'y') else float(wheel)
        if wheel_val != 0:
            factor = 1.1 if wheel_val > 0 else 0.9
            tt.zoom_at(self.view, mouse_x, mouse_y, factor)

        # 页面标签按钮
        page_rect = self.page_tab_rects(viewport_w)
        for page_id, r in page_rect.items():
            if tt.is_in_rect(mouse_x, mouse_y, r):
                if rl.IsMouseButtonPressed(rl.MOUSE_BUTTON_LEFT):
                    self.switch_page(page_id)
                    return

        # 关闭按钮
        close_rect = self.close_button_rect(viewport_w)
        if tt.is_in_rect(mouse_x, mouse_y, close_rect):
            if rl.IsMouseButtonPressed(rl.MOUSE_BUTTON_LEFT):
                self.close_panel()
                return

        # 时代标签按钮
        era_rects = self.era_tab_rects(viewport_w)
        for idx, r in enumerate(era_rects):
            if tt.is_in_rect(mouse_x, mouse_y, r):
                if rl.IsMouseButtonPressed(rl.MOUSE_BUTTON_LEFT):
                    self.era_idx = idx
                    self.selected_id = None
                    self.info_visible = False
                    self._fit_done = False
                    return

        # 研究按钮（仅技术页）
        if self.page == tt.PAGE_BREAKTHROUGHS and self.selected_id:
            btn_rect = self.research_button_rect(viewport_w, viewport_h)
            self._hover_btn = tt.is_in_rect(mouse_x, mouse_y, btn_rect)
            if self._hover_btn and rl.IsMouseButtonPressed(rl.MOUSE_BUTTON_LEFT):
                self._on_research_click()
                return
        else:
            self._hover_btn = False

        # 节点命中
        node_id = tt.node_at(tree, positions, self.view, mouse_x, mouse_y, self.page)
        self._hover_id = node_id

        if node_id:
            if rl.IsMouseButtonPressed(rl.MOUSE_BUTTON_LEFT):
                self.selected_id = node_id
                self.info_visible = True
            # 悬停在节点上时不开始拖动，但仍允许上面的滚轮缩放和末尾的钳制
        else:
            # 空白区域：按下开始拖动
            if rl.IsMouseButtonPressed(rl.MOUSE_BUTTON_LEFT):
                self._dragging = True
                self._drag_start = (mouse_x, mouse_y)
                self._drag_start_pan = (self.view.pan_x, self.view.pan_y)

            # 拖动中（鼠标持续按住）按位移差更新 pan
            if self._dragging and rl.IsMouseButtonDown(rl.MOUSE_BUTTON_LEFT):
                new_pan = tt.drag_pan(
                    self._drag_start_pan, self._drag_start, (mouse_x, mouse_y))
                self.view.pan_x = new_pan[0]
                self.view.pan_y = new_pan[1]

        # 限制平移（扣除表头/底栏预留高度）
        bbox = self.content_bbox(positions)
        tt.clamp_pan(self.view, bbox, viewport_w, viewport_h,
                     inset_top=ui.TECHTREE_CONTENT_TOP,
                     inset_bottom=ui.TECHTREE_CONTENT_BOTTOM)

    def _on_research_click(self):
        """研究按钮点击回调。"""
        if self.game and self.selected_id:
            self.game._research_tech(self.selected_id)

    def page_tab_rects(self, viewport_w):
        """页面标签矩形。"""
        tab_w = 100
        tab_h = 30
        y = 4
        x = viewport_w - tab_w * 2 - 20
        return {
            tt.PAGE_ACHIEVEMENTS: (x, y, tab_w, tab_h),
            tt.PAGE_BREAKTHROUGHS: (x + tab_w + 10, y, tab_w, tab_h),
        }

    def close_button_rect(self, viewport_w):
        """左上角关闭按钮矩形（Esc 会直接退出游戏，故用 T 或点击此按钮关闭）。"""
        return (12, 4, 108, 30)

    def era_tab_rects(self, viewport_w):
        """时代标签矩形。"""
        era_w = 60
        era_h = 24
        y = 38
        x = 12
        return [(x + i * (era_w + 6), y, era_w, era_h) for i in range(len(gamedata.TECH_ERAS))]

    def research_button_rect(self, viewport_w, viewport_h):
        """研究按钮矩形（让开底栏）。"""
        return tt.research_button_rect(self.view, viewport_w, viewport_h,
                                       ui.TECHTREE_FOOTER_H)

    def render(self, font, viewport_w, viewport_h, game_money=0,
               unlocked_tech=None, researching=None,
               research_timer=0, research_total=0):
        """渲染面板。"""
        if not self.open:
            return

        tree, positions, edges = self.get_layout(viewport_w, viewport_h)
        bbox = self.content_bbox(positions)

        # 背景
        draw_rect(0, 0, viewport_w, viewport_h, ui.TECHTREE_PANEL_BG)

        # 绘制时代标签
        self._render_era_tabs(font, viewport_w)

        # 绘制页面标签
        self._render_page_tabs(font, viewport_w)

        # 绘制关闭按钮
        self._render_close_button(font, viewport_w)

        # 绘制分隔线
        draw_rect(0, ui.TECHTREE_HEADER_H - 1, viewport_w, 1, color(60, 65, 80))

        # 绘制边
        self._render_edges(positions, edges, viewport_w, viewport_h)

        # 绘制节点
        self._render_nodes(tree, positions, edges, font, viewport_w, viewport_h,
                           game_money, unlocked_tech, researching,
                           research_timer, research_total)

        # 绘制信息面板
        if self.info_visible and self.selected_id:
            self._render_info_panel(tree, font, viewport_w, viewport_h,
                                    game_money, unlocked_tech, researching,
                                    research_timer, research_total)

        # 绘制研究按钮
        if self.page == tt.PAGE_BREAKTHROUGHS and self.selected_id:
            self._render_research_button(tree, font, viewport_w, viewport_h,
                                         game_money, unlocked_tech, researching,
                                         research_timer, research_total)

        # 底部提示
        self._render_footer(font, viewport_w, viewport_h)

    def _render_era_tabs(self, font, viewport_w):
        """渲染时代标签。"""
        rects = self.era_tab_rects(viewport_w)
        for i, (r, era) in enumerate(zip(rects, gamedata.TECH_ERAS)):
            x, y, w, h = r
            is_active = (i == self.era_idx)
            is_hover = tt.is_in_rect(rl.GetMouseX(), rl.GetMouseY(), r)

            bg = color(60, 65, 80) if is_active else color(45, 50, 65)
            if is_hover and not is_active:
                bg = color(70, 75, 90)
            draw_rect(x, y, w, h, bg)
            if is_active:
                draw_rect_lines(rect(x, y, w, h), 1, color(100, 150, 200))

            text = era["name_cn"]
            text_color = color(220, 220, 240) if is_active else color(150, 155, 175)
            tx = x + (w - measure_text(text, ui.TECHTREE_CAT_LABEL_SIZE)) / 2
            ty = y + (h - ui.TECHTREE_CAT_LABEL_SIZE) / 2
            draw_text(text, tx, ty, ui.TECHTREE_CAT_LABEL_SIZE, text_color)

    def _render_page_tabs(self, font, viewport_w):
        """渲染页面标签。"""
        rects = self.page_tab_rects(viewport_w)
        labels = {tt.PAGE_ACHIEVEMENTS: "成果", tt.PAGE_BREAKTHROUGHS: "技术"}
        for page_id, r in rects.items():
            x, y, w, h = r
            is_active = (page_id == self.page)
            is_hover = tt.is_in_rect(rl.GetMouseX(), rl.GetMouseY(), r)

            bg = color(60, 65, 80) if is_active else color(45, 50, 65)
            if is_hover and not is_active:
                bg = color(70, 75, 90)
            draw_rect(x, y, w, h, bg)
            if is_active:
                draw_rect_lines(rect(x, y, w, h), 1, color(100, 150, 200))

            text = labels[page_id]
            text_color = color(220, 220, 240) if is_active else color(150, 155, 175)
            tx = x + (w - measure_text(text, ui.TECHTREE_BUTTON_SIZE)) / 2
            ty = y + (h - ui.TECHTREE_BUTTON_SIZE) / 2
            draw_text(text, tx, ty, ui.TECHTREE_BUTTON_SIZE, text_color)

    def _render_close_button(self, font, viewport_w):
        """渲染左上角关闭按钮（Esc 会被 raylib 当作窗口退出键，故用 T 或点击此按钮关闭）。"""
        x, y, w, h = self.close_button_rect(viewport_w)
        is_hover = tt.is_in_rect(rl.GetMouseX(), rl.GetMouseY(), (x, y, w, h))

        bg = color(95, 62, 68) if is_hover else color(52, 46, 62)
        draw_rect(x, y, w, h, bg)
        draw_rect_lines(rect(x, y, w, h), 1, color(150, 100, 105))

        text = "× 关闭 [T]"
        tw = measure_text(text, ui.TECHTREE_BUTTON_SIZE)
        draw_text(text, x + (w - tw) / 2, y + (h - ui.TECHTREE_BUTTON_SIZE) / 2,
                  ui.TECHTREE_BUTTON_SIZE, color(240, 215, 215))

    def _render_edges(self, positions, edges, viewport_w, viewport_h):
        """渲染连接边。

        tree = 同轨 ├/└ 父子(实线);cross = ⊕ 跨轨硬依赖;
        soft = 概念级软依赖(淡线);ach = 成果间依赖。
        每条边带跨层级数 span，跨层越多透明度越低（不会淡到看不见）。
        """
        if not edges:
            return
        bases = {
            "tree":  color(*ui.TECHTREE_NODE_EDGES_COLOR),
            "cross": color(120, 110, 150, 255),   # ⊕ 跨轨硬依赖
            "soft":  color(120, 120, 140, 150),   # 概念级软依赖(半透明)
            "ach":   color(100, 140, 180, 255),   # 成果间依赖
        }
        # 逐帧常量：graph_to_screen 仅依赖 view 的 zoom/pan（一次渲染内不变），
        # 内联成同序的 乘加；edge 颜色只依赖 (kind, span)（_current_alpha 本次
        # 渲染内不变），按 (kind, span) 缓存，与逐条重算逐位一致。
        zoom = self.view.zoom
        pan_x = self.view.pan_x
        pan_y = self.view.pan_y
        edge_fade = tt.edge_fade
        color_cache = {}
        cget = color_cache.get
        for edge in edges:
            x1, y1, x2, y2 = edge[:4]
            kind = edge[4] if len(edge) > 4 else "tree"
            span = edge[5] if len(edge) > 5 else 1
            ck = (kind, span)
            c = cget(ck)
            if c is None:
                r, g, b, a = bases.get(kind, bases["tree"])
                c = color(r, g, b, int(a * edge_fade(span)))
                color_cache[ck] = c
            sx1 = x1 * zoom + pan_x
            sy1 = y1 * zoom + pan_y
            sx2 = x2 * zoom + pan_x
            sy2 = y2 * zoom + pan_y
            draw_line(sx1, sy1, sx2, sy2, c)

    def _render_nodes(self, tree, positions, edges, font, viewport_w, viewport_h,
                      game_money, unlocked_tech, researching,
                      research_timer, research_total):
        """渲染节点。"""
        visible = tt.visible_nodes(positions, self.view, viewport_w, viewport_h)
        zoom = self.view.zoom
        show_desc = zoom >= tt.LOD_THRESHOLD
        label_size = max(9, min(16, round(13 * zoom)))
        inset = 8 * zoom
        info_size = max(8, min(14, round(11 * zoom)))

        for node_id, x, y, w, h in visible:
            sx, sy = tt.graph_to_screen(self.view, x, y)
            sw = w * zoom
            sh = h * zoom

            # 确定节点类型和状态
            if self.page == tt.PAGE_BREAKTHROUGHS:
                node = tree.nodes.get(node_id, {})
                state = tree.node_state(node_id, unlocked_tech or set(), researching)
            else:
                ach = tree.achievements.get(node_id, {})
                state = tree.achievement_state(node_id, unlocked_tech or set())

            # 节点颜色
            bg = self._node_color(state, is_ach=(self.page == tt.PAGE_ACHIEVEMENTS))

            # 绘制背景
            draw_rect(int(sx), int(sy), int(sw), int(sh), bg)

            # 绘制边框
            border_color = self._node_border_color(state, node_id)
            draw_rect_lines(rect(sx, sy, sw, sh), 1, border_color)

            # 绘制文本
            if self.page == tt.PAGE_BREAKTHROUGHS:
                node = tree.nodes.get(node_id, {})
                name = node.get("name_cn", node_id)
                cost = node.get("cost", 0)
                display_text = name if show_desc else name[:12]
                if len(display_text) > 14:
                    display_text = display_text[:13] + "…"

                text_color = self._node_text_color(state)
                draw_text(display_text,
                             sx + inset, sy + (sh - label_size) / 2,
                             label_size, text_color)

                if show_desc and zoom >= 1.0:
                    cost_text = f"💰{cost}"
                    draw_text(cost_text,
                                 sx + sw - measure_text(cost_text, info_size) - 4,
                                 sy + (sh - info_size) / 2,
                                 info_size, color(255, 215, 0))
            else:
                ach = tree.achievements.get(node_id, {})
                name = ach.get("name_cn", node_id)
                display_text = name if show_desc else name[:12]
                if len(display_text) > 14:
                    display_text = display_text[:13] + "…"

                text_color = self._node_text_color(state)
                draw_text(display_text,
                             sx + inset, sy + (sh - label_size) / 2,
                             label_size, text_color)

                if state != tt.ACH_LOCKED:
                    done, total, frac = tree.achievement_progress(node_id, unlocked_tech or set())
                    prog_text = f"{done}/{total}"
                    draw_text(prog_text,
                                 sx + sw - measure_text(prog_text, info_size) - 4,
                                 sy + (sh - info_size) / 2,
                                 info_size, color(200, 200, 200))

    def _node_color(self, state, is_ach=False):
        """节点背景色。

        is_ach 为成就页时用成就配色；否则用科技配色。
        注意 STATE_* 与 ACH_* 共用同一字符串（unlocked/locked），
        必须按页面分流，否则成就页解锁/锁定节点永远命中科技配色分支。
        """
        if is_ach:
            if state == tt.ACH_UNLOCKED:
                return color(*ui.TECHTREE_ACH_UNLOCKED)
            elif state == tt.ACH_PARTIAL:
                return color(*ui.TECHTREE_ACH_PARTIAL)
            else:
                return color(*ui.TECHTREE_ACH_LOCKED)
        if state == tt.STATE_UNLOCKED:
            return color(*ui.TECHTREE_NODE_UNLOCKED)
        elif state == tt.STATE_RESEARCHING:
            return color(*ui.TECHTREE_NODE_RESEARCHING)
        elif state == tt.STATE_AVAILABLE:
            return color(*ui.TECHTREE_NODE_AVAILABLE)
        else:
            return color(*ui.TECHTREE_NODE_LOCKED)

    def _node_border_color(self, state, node_id):
        """节点边框色。"""
        if node_id == self.selected_id:
            return color(*ui.TECHTREE_NODE_SELECTED_BORDER)
        elif state in (tt.STATE_UNLOCKED, tt.ACH_UNLOCKED):
            return color(80, 160, 100)
        elif state == tt.STATE_RESEARCHING:
            return color(200, 150, 50)
        elif state in (tt.STATE_AVAILABLE, tt.ACH_PARTIAL):
            return color(100, 130, 160)
        else:
            return color(70, 75, 90)

    def _node_text_color(self, state):
        """节点文本色。"""
        if state in (tt.STATE_UNLOCKED, tt.ACH_UNLOCKED):
            return color(220, 255, 220)
        elif state == tt.STATE_RESEARCHING:
            return color(255, 220, 150)
        elif state in (tt.STATE_AVAILABLE, tt.ACH_PARTIAL):
            return color(220, 220, 240)
        else:
            return color(120, 120, 140)

    def _render_info_panel(self, tree, font, viewport_w, viewport_h,
                           game_money, unlocked_tech, researching,
                           research_timer, research_total):
        """渲染信息面板。"""
        panel_w = ui.TECHTREE_INFO_W
        panel_h = ui.TECHTREE_INFO_H
        panel_x = ui.TECHTREE_INFO_X
        panel_y = ui.TECHTREE_INFO_Y

        # 背景
        draw_rect(panel_x, panel_y, panel_w, panel_h, ui.TECHTREE_INFO_BG)
        draw_rect_lines(rect(panel_x, panel_y, panel_w, panel_h), 1, color(*ui.TECHTREE_INFO_BORDER))

        node_id = self.selected_id
        if not node_id:
            return

        if self.page == tt.PAGE_BREAKTHROUGHS:
            node = tree.nodes.get(node_id, {})
            name = node.get("name_cn", node_id)
            era_id = node.get("era", "")
            track = node.get("track", "")
            era_info = next((e for e in gamedata.TECH_ERAS if e["id"] == era_id), {})
            era_name = era_info.get("name_cn", era_id)
            cat_info = gamedata.TECH_CATEGORIES.get(era_id, {}).get(track, {})
            track_name = cat_info.get("name_cn", track)

            state = tree.node_state(node_id, unlocked_tech or set(), researching)
            cost = node.get("cost", 0)
            time = node.get("time", 0)
            requires = node.get("requires", [])
            outputs = node.get("outputs", [])
            tags = node.get("tags", [])
            group = node.get("group", "")
            ach_id = node.get("achievement", "")
            ach = tree.achievements.get(ach_id, {}) if ach_id else {}

            y = panel_y + 12
            # 名称
            draw_text(name, panel_x + 10, y, ui.TECHTREE_INFO_SIZE, color(220, 220, 240))
            y += 20
            # 时代·分类·组
            head = f"{era_name}·{track_name}" + (f"·{group}" if group else "")
            draw_text(head, panel_x + 10, y, ui.TECHTREE_DESC_SIZE, color(150, 155, 175))
            y += 18
            # 标签（闭合 / 晚 / soft / 非火 / prototype…）
            if tags:
                draw_text("标签: " + " ".join(tags), panel_x + 10, y,
                          ui.TECHTREE_DESC_SIZE, color(195, 165, 225))
                y += 18
            # 状态
            state_text = {"unlocked": "✓ 已解锁", "available": "◉ 可研究",
                          "researching": "⟳ 研究中", "locked": "🔒 未解锁"}.get(state, state)
            draw_text(state_text, panel_x + 10, y, ui.TECHTREE_DESC_SIZE, color(200, 200, 220))
            y += 18
            # 研究中进度（用真实计时器，不再只画一个转圈）
            if state == "researching" and research_total > 0:
                done, total, frac = tree.research_progress(
                    researching, research_timer, research_total)
                draw_text(f"进度 {frac:.0%}（{done:.0f}/{total:.0f}s）", panel_x + 10, y,
                          ui.TECHTREE_DESC_SIZE, color(120, 200, 255))
                y += 18
            # 费用/时间
            cost_text = f"💰{cost}  ⏱{time:.1f}s"
            draw_text(cost_text, panel_x + 10, y, ui.TECHTREE_DESC_SIZE, color(255, 215, 0))
            y += 18
            # 前置（中文名；有未解锁的前置则标红）
            if requires:
                req_text = "需: " + "、".join(tree.node_name(r) for r in requires[:3])
                if len(requires) > 3:
                    req_text += "…"
                missing = any(r not in (unlocked_tech or set()) for r in requires)
                draw_text(req_text, panel_x + 10, y, ui.TECHTREE_DESC_SIZE,
                          color(255, 140, 140) if missing else color(150, 155, 175))
                y += 18
            # 产出（→ 流向的下游节点）
            if outputs:
                out_text = "产出: " + "、".join(tree.node_name(o) for o in outputs[:3])
                if len(outputs) > 3:
                    out_text += "…"
                draw_text(out_text, panel_x + 10, y, ui.TECHTREE_DESC_SIZE, color(150, 200, 220))
                y += 18
            # 所属成果
            if ach:
                ach_text = f"成果: {ach.get('name_cn', '')}"
                draw_text(ach_text, panel_x + 10, y, ui.TECHTREE_DESC_SIZE, color(180, 200, 180))
                y += 18
        else:
            ach = tree.achievements.get(node_id, {})
            name = ach.get("name_cn", node_id)
            era_id = ach.get("era", "")
            track = ach.get("track", "")
            era_info = next((e for e in gamedata.TECH_ERAS if e["id"] == era_id), {})
            era_name = era_info.get("name_cn", era_id)
            cat_info = gamedata.TECH_CATEGORIES.get(era_id, {}).get(track, {})
            track_name = cat_info.get("name_cn", track)

            state = tree.achievement_state(node_id, unlocked_tech or set())
            done, total, frac = tree.achievement_progress(node_id, unlocked_tech or set())
            eta = tree.eta_seconds(node_id, unlocked_tech or set())

            y = panel_y + 12
            # 名称
            draw_text(name, panel_x + 10, y, ui.TECHTREE_INFO_SIZE, color(220, 220, 240))
            y += 20
            # 时代·分类
            info_text = f"{era_name}·{track_name}"
            draw_text(info_text, panel_x + 10, y, ui.TECHTREE_DESC_SIZE, color(150, 155, 175))
            y += 18
            # 进度
            state_text = {"locked": "🔒 未开始", "partial": "◐ 进行中",
                          "unlocked": "✓ 已完成"}.get(state, state)
            draw_text(state_text, panel_x + 10, y, ui.TECHTREE_DESC_SIZE, color(200, 200, 220))
            y += 18
            # 突破点进度
            prog_text = f"突破: {done}/{total} ({frac:.0%})"
            draw_text(prog_text, panel_x + 10, y, ui.TECHTREE_DESC_SIZE, color(200, 200, 200))
            y += 18
            # 预计时间
            if eta > 0:
                eta_text = f"预计: {eta:.0f}s"
                draw_text(eta_text, panel_x + 10, y, ui.TECHTREE_DESC_SIZE, color(255, 200, 100))
                y += 18

    def _render_research_button(self, tree, font, viewport_w, viewport_h,
                                game_money, unlocked_tech, researching,
                                research_timer=0, research_total=0):
        """渲染研究按钮。

        可研究=高亮；不可研究=显示 can_research 的原因（以前被丢弃）；
        正在研究本节点=按钮上方画进度条。
        """
        bx, by, bw, bh = self.research_button_rect(viewport_w, viewport_h)

        node_id = self.selected_id
        if not node_id:
            return

        node = tree.nodes.get(node_id, {})
        cost = node.get("cost", 0)
        ok, reason = tree.can_research(node_id, unlocked_tech or set(), game_money, researching)

        if self._hover_btn and ok:
            bg = color(*ui.TECHTREE_BTN_HOVER)
        elif ok:
            bg = color(*ui.TECHTREE_BTN_BG)
        else:
            bg = color(*ui.TECHTREE_BTN_DISABLED)

        draw_rect(bx, by, bw, bh, bg)
        draw_rect_lines(rect(bx, by, bw, bh), 1, color(80, 120, 80) if ok else color(80, 80, 90))

        # 正在研究本节点：按钮上方画进度条
        if researching == node_id and research_total > 0:
            done, total, frac = tree.research_progress(
                researching, research_timer, research_total)
            draw_rect(bx, by - 10, bw, 6, color(60, 65, 80))
            draw_rect(bx, by - 10, max(1, int(bw * frac)), 6, color(120, 200, 255))
            btn_text = f"研究中 {done:.0f}/{total:.0f}s"
        elif ok:
            btn_text = f"研究 ({cost})"
        else:
            btn_text = reason

        # 按钮宽度有限，超长截断
        while btn_text and measure_text(btn_text + "…", ui.TECHTREE_BUTTON_SIZE) > bw - 6:
            btn_text = btn_text[:-1]
        if not btn_text:
            btn_text = "…"

        draw_text(btn_text,
                     bx + (bw - measure_text(btn_text, ui.TECHTREE_BUTTON_SIZE)) / 2,
                     by + (bh - ui.TECHTREE_BUTTON_SIZE) / 2,
                     ui.TECHTREE_BUTTON_SIZE,
                     color(*ui.TECHTREE_BTN_TEXT) if ok else color(200, 160, 160))

    def _render_footer(self, font, viewport_w, viewport_h):
        """渲染底部提示。"""
        fh = ui.TECHTREE_FOOTER_H
        footer_y = viewport_h - fh
        draw_rect(0, footer_y, viewport_w, fh, color(30, 35, 50, 200))

        hint = "拖动:平移 | 滚轮:缩放 | F:适配 | 1:成果 2:技术 | Tab:换时代 | Esc/T:关闭"
        draw_text(hint,
                     viewport_w / 2 - measure_text(hint, ui.TECHTREE_FOOTER_SIZE) / 2,
                     footer_y + 8,
                     ui.TECHTREE_FOOTER_SIZE, color(150, 155, 175))


# ─── 单例 ─────────────────────────────────────────────────────

_panel_instance = None


def tech_tree_panel(game=None):
    """获取面板单例（可传入 game 引用）。"""
    global _panel_instance
    if _panel_instance is None:
        _panel_instance = TechTreePanel(game)
    elif game is not None:
        # 读档（_pending_replace）会整体替换 Game 对象，此处必须重绑，
        # 否则面板继续持有已被丢弃的旧 Game，研究点击会失效。
        _panel_instance.game = game
    return _panel_instance
