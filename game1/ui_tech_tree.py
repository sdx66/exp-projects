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
import config
from items import TOOL_TYPES, MATERIAL_NAMES_CN
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
        self._drag_engaged = False
        self._hover_id = None
        self._hover_btn = False
        self._fit_done = False
        # 研究目标塔：None=自动(学识最高且满足条件)；否则为研究塔下标（点通道行切换）
        self.target_lane_index = None

    # ─── 研究通道（多研究塔）辅助 ────────────────────────────

    def _lanes(self):
        """当前研究通道列表（来自 game；无 game 或场上无研究塔 → 空/仅基地手研）。"""
        if self.game is None:
            return []
        lanes = self.game.research_lanes()
        return lanes or []

    def lanes_h(self):
        """研究通道条占用的垂直高度（无研究塔为 0）。"""
        n = len(self._lanes())
        if n <= 0:
            return 0
        return min(n, ui.TECHTREE_LANE_MAX_ROWS) * ui.TECHTREE_LANE_H

    def lane_rows(self, viewport_w, viewport_h):
        """各通道行矩形（自底栏向上排列）：[(x, y, w, h, lane_dict), ...]。"""
        lanes = self._lanes()
        if not lanes:
            return []
        h = ui.TECHTREE_LANE_H
        n = min(len(lanes), ui.TECHTREE_LANE_MAX_ROWS)
        rows = []
        for i in range(n):
            y = viewport_h - ui.TECHTREE_FOOTER_H - (i + 1) * h
            rows.append((0, y, viewport_w, h, lanes[i], i))
        return rows

    def lane_buttons(self, viewport_w, viewport_h):
        """通道行内交互按钮：[(rect, action), ...]。
        action = ("lane", i) 切换目标塔 | ("cancel_current", i)
               | ("cancel_queued", i, queue_index)。
        取消按钮排在行点击之前（按钮优先命中），行点击矩形最后。
        """
        out = []
        for x, y, w, h, lane, i in self.lane_rows(viewport_w, viewport_h):
            # 取消按钮矩形须与 _render_lanes 绘制矩形完全一致（否则 1px 错位
            # 导致边缘点击失灵）：bx=w-15, rect=(y+4, 12, h-8)（BUG-137）。
            bx = w - 15
            # 队列项取消按钮（从右往左排）
            qlen = len(lane.get("queue") or ())
            for qi in range(min(qlen, 2)):
                out.append(((bx, y + 4, 12, h - 8), ("cancel_queued", i, qi)))
                bx -= 22
            # 当前项取消按钮
            if lane.get("current"):
                out.append(((bx, y + 4, 12, h - 8), ("cancel_current", i)))
            # 整行点击 = 切换目标塔
            out.append(((x, y, w, h), ("lane", i)))
        return out

    def _target_tower(self):
        """返回研究按钮的目标塔（None=自动选最优）。"""
        lanes = self._lanes()
        if not lanes:
            return None
        if self.target_lane_index is not None and 0 <= self.target_lane_index < len(lanes):
            return lanes[self.target_lane_index]["tower"]
        return None

    def _research_click_lane(self, node_id):
        """复现 game._research_tech 的通道选择，返回点击将占用的通道对象。

        station=false → 基地手研通道（不占研究塔）；station=true 且手动指派
        目标塔 → 该塔；否则自动选最优塔（game._pick_research_tower），
        选不到（无研究塔）时返回 None（上游已用"需要研究塔"拦截）。
        仅用于队列容量预判（BUG-117），不做 station 校验。
        """
        game = self.game
        if game is None:
            return None
        if tt.node_station(node_id):
            tower = self._target_tower()
            if tower is None:
                tower = game._pick_research_tower(node_id)
            return tower
        return getattr(game, "base_research", None)

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
            return tt.EMPTY_CONTENT_BBOX
        min_x = min(p[0] for p in positions.values())
        min_y = min(p[1] for p in positions.values())
        max_x = max(p[0] + p[2] for p in positions.values())
        max_y = max(p[1] + p[3] for p in positions.values())
        return (min_x, min_y, max_x - min_x, max_y - min_y)

    def fit_view(self, positions, viewport_w, viewport_h):
        """适配视口（扣除表头/底栏/研究通道条预留高度后居中）。"""
        bbox = self.content_bbox(positions)
        tt.fit_view(self.view, bbox, viewport_w, viewport_h,
                    inset_top=ui.TECHTREE_CONTENT_TOP,
                    inset_bottom=ui.TECHTREE_CONTENT_BOTTOM + self.lanes_h())
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

        # 研究通道条：点击行切换目标塔；取消按钮（当前项/队列项）
        for rect, action in self.lane_buttons(viewport_w, viewport_h):
            if tt.is_in_rect(mouse_x, mouse_y, rect):
                if rl.IsMouseButtonPressed(rl.MOUSE_BUTTON_LEFT):
                    if action[0] == "lane":
                        _, i = action
                        # 再次点击已选中行 → 回到自动
                        self.target_lane_index = None if self.target_lane_index == i else i
                        return
                    if action[0] == "cancel_current":
                        _, i = action
                        lanes = self._lanes()
                        if self.game is not None and 0 <= i < len(lanes):
                            lane = lanes[i]
                            if lane.get("hand"):
                                self.game._cancel_research(None, tower=self.game.base_research)
                            else:
                                self.game._cancel_research(None, tower=lane["tower"])
                        return
                    if action[0] == "cancel_queued":
                        _, i, qi = action
                        lanes = self._lanes()
                        if self.game is not None and 0 <= i < len(lanes):
                            q = lanes[i].get("queue") or ()
                            if qi < len(q):
                                self.game._cancel_research(q[qi])
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
                self._drag_engaged = False

        # 拖动中（鼠标持续按住）按位移差更新 pan。
        # 提到 if/else 之外：拖动一旦开始，光标划过节点/边也不能冻结平移（BUG-091）。
        if self._dragging and rl.IsMouseButtonDown(rl.MOUSE_BUTTON_LEFT):
            dx = mouse_x - self._drag_start[0]
            dy = mouse_y - self._drag_start[1]
            # 阈值是「启动拖动」的一次性门槛：越过后即视为已启动，
            # 已启动的拖动不再重复做死区判定（否则指针回到按下点会回弹，BUG-080）。
            if (not self._drag_engaged
                    and (abs(dx) >= tt.DRAG_THRESHOLD_PX
                         or abs(dy) >= tt.DRAG_THRESHOLD_PX)):
                self._drag_engaged = True
            new_pan = tt.drag_pan(
                self._drag_start_pan, self._drag_start, (mouse_x, mouse_y),
                engaged=self._drag_engaged)
            self.view.pan_x = new_pan[0]
            self.view.pan_y = new_pan[1]

        # 限制平移（扣除表头/底栏/研究通道条预留高度）
        bbox = self.content_bbox(positions)
        tt.clamp_pan(self.view, bbox, viewport_w, viewport_h,
                     inset_top=ui.TECHTREE_CONTENT_TOP,
                     inset_bottom=ui.TECHTREE_CONTENT_BOTTOM + self.lanes_h())

    def _on_research_click(self):
        """研究按钮点击回调。

        已排队 → 取消并退款；否则把选中科技加入目标塔队列（自动选最优塔）。
        """
        if not (self.game and self.selected_id):
            return
        if self.selected_id in self.game._queued_research():
            self.game._cancel_research(self.selected_id)
            return
        self.game._research_tech(self.selected_id, tower=self._target_tower())

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
        """研究按钮矩形（让开底栏与研究通道条）。"""
        return tt.research_button_rect(self.view, viewport_w,
                                       viewport_h - self.lanes_h(),
                                       ui.TECHTREE_FOOTER_H)

    def render(self, font, viewport_w, viewport_h, game_money=0,
               unlocked_tech=None, researching=None,
               research_timer=0, research_total=0, lanes=None):
        """渲染面板。

        lanes（可选）：[lane_dict, ...] 每座研究塔一条通道（多塔并行/队列）；
        None 时回退旧单槽参数（researching/timer/total）兼容测试与 profile。
        """
        if not self.open:
            return

        if lanes is None:
            # 旧单槽回退：在研注入单通道
            if researching:
                lanes = [{"tower": None, "current": researching,
                          "timer": research_timer, "total": research_total,
                          "queue": [], "blocked": None}]
            else:
                lanes = []
        elif hasattr(self, "game") and self.game is not None:
            # 数据始终以 game 为准（读档后 Game 被整体替换，lanes 缓存会过期）
            lanes = self._lanes()

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

        # 绘制树内容（边+节点），裁剪到内容区，避免 pan 上移后树内容绘制在
        # 头部页签之上、遮挡页签；clamp_pan 为查看大树底部允许内容越过
        # CONTENT_TOP，故此处必须裁剪到 [CONTENT_TOP, viewport_h - CONTENT_BOTTOM
        # - 通道条高度]。
        clip_bottom = ui.TECHTREE_CONTENT_BOTTOM + self.lanes_h()
        rl.BeginScissorMode(
            0, ui.TECHTREE_CONTENT_TOP, viewport_w,
            max(1, viewport_h - ui.TECHTREE_CONTENT_TOP - clip_bottom))
        # 绘制边
        self._render_edges(positions, edges, viewport_w, viewport_h)

        # 绘制节点
        self._render_nodes(tree, positions, edges, font, viewport_w, viewport_h,
                           game_money, unlocked_tech, lanes)
        rl.EndScissorMode()

        # 绘制信息面板
        if self.info_visible and self.selected_id:
            self._render_info_panel(tree, font, viewport_w, viewport_h,
                                    game_money, unlocked_tech, lanes)

        # 绘制研究按钮
        if self.page == tt.PAGE_BREAKTHROUGHS and self.selected_id:
            self._render_research_button(tree, font, viewport_w, viewport_h,
                                         game_money, unlocked_tech, lanes)

        # 绘制研究通道条（研究塔队列/并行状态）
        if lanes:
            self._render_lanes(font, viewport_w, viewport_h, lanes)

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
                      game_money, unlocked_tech, lanes):
        """渲染节点。lanes 为研究通道列表（含在研/排队集合推导）。"""
        visible = tt.visible_nodes(positions, self.view, viewport_w, viewport_h)
        zoom = self.view.zoom
        show_desc = zoom >= tt.LOD_THRESHOLD
        label_size = max(9, min(16, round(13 * zoom)))
        inset = 8 * zoom
        info_size = max(8, min(14, round(11 * zoom)))
        active_ids = {l["current"] for l in lanes if l.get("current")}
        queued_ids = {q for l in lanes for q in (l.get("queue") or ())}

        for node_id, x, y, w, h in visible:
            sx, sy = tt.graph_to_screen(self.view, x, y)
            sw = w * zoom
            sh = h * zoom

            # 确定节点类型和状态
            if self.page == tt.PAGE_BREAKTHROUGHS:
                node = tree.nodes.get(node_id, {})
                state = tree.node_state(node_id, unlocked_tech or set(),
                                        active_ids, queued_ids)
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
                    cost_text = f"${cost}"
                    draw_text(cost_text,
                                 sx + sw - measure_text(cost_text, info_size) - 4,
                                 sy + (sh - info_size) / 2,
                                 info_size, color(255, 215, 0))

                # 站点标记：station=true 需研究塔；station=false 手研即可（右下角小角标）
                if sw >= 26 and sh >= 16:
                    bs = max(10, min(16, int(12 * zoom)))
                    bx = int(sx + sw - bs - 2)
                    byy = int(sy + sh - bs - 2)
                    if tree.node_station(node_id):
                        draw_rect(bx, byy, bs, bs, color(90, 80, 140, 220))
                        draw_text("塔", bx + 1, byy + 1,
                                  max(8, min(12, int(9 * zoom))), color(220, 210, 255))
                    else:
                        draw_rect(bx, byy, bs, bs, color(60, 130, 70, 220))
                        draw_text("手", bx + 1, byy + 1,
                                  max(8, min(12, int(9 * zoom))), color(210, 255, 220))
                # 排队标记：已在某塔队列等待（左下角小角标）
                if state == tt.STATE_QUEUED and sw >= 26 and sh >= 16:
                    bs = max(10, min(16, int(12 * zoom)))
                    draw_rect(int(sx + 2), int(sy + sh - bs - 2), bs, bs,
                              color(140, 110, 40, 230))
                    draw_text("队", int(sx + 3), int(sy + sh - bs - 1),
                              max(8, min(12, int(9 * zoom))), color(255, 230, 160))
                # 材料标记：研发需要材料（左上角小角标，如 引火法需薪柴）
                _mat = tree.node_requirements(node_id)[2]
                if _mat and sw >= 26 and sh >= 16:
                    bs = max(10, min(16, int(12 * zoom)))
                    draw_rect(int(sx + 2), int(sy + 2), bs, bs,
                              color(120, 90, 30, 220))
                    draw_text("材", int(sx + 3), int(sy + 3),
                              max(8, min(12, int(9 * zoom))), color(255, 235, 180))
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
        elif state == tt.STATE_QUEUED:
            return color(110, 95, 40)
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
        elif state == tt.STATE_QUEUED:
            return color(190, 160, 60)
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
        elif state == tt.STATE_QUEUED:
            return color(240, 215, 130)
        elif state in (tt.STATE_AVAILABLE, tt.ACH_PARTIAL):
            return color(220, 220, 240)
        else:
            return color(120, 120, 140)

    def _render_info_panel(self, tree, font, viewport_w, viewport_h,
                           game_money, unlocked_tech, lanes):
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

            state = tree.node_state(node_id, unlocked_tech or set(),
                                {l["current"] for l in lanes if l.get("current")},
                                {q for l in lanes for q in (l.get("queue") or ())})
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
                          "queued": "⏳ 已排队", "researching": "⇄ 研究中",
                          "locked": "× 未解锁"}.get(state, state)
            draw_text(state_text, panel_x + 10, y, ui.TECHTREE_DESC_SIZE, color(200, 200, 220))
            y += 18
            # 在研进度 / 排队位次（定位到具体通道）
            for li, lane in enumerate(lanes):
                if lane.get("current") == node_id and lane.get("total", 0) > 0:
                    done, total, frac = tree.research_progress(
                        node_id, lane.get("timer", 0.0), lane.get("total", 0.0))
                    draw_text(f"T{li + 1} 进度 {frac:.0%}（{done:.0f}/{total:.0f}s）",
                              panel_x + 10, y, ui.TECHTREE_DESC_SIZE, color(120, 200, 255))
                    y += 18
                q = lane.get("queue") or ()
                if node_id in q:
                    pos = q.index(node_id) + 1
                    draw_text(f"T{li + 1} 队列第 {pos} 位", panel_x + 10, y,
                              ui.TECHTREE_DESC_SIZE, color(240, 215, 130))
                    y += 18
            # 费用/时间
            cost_text = f"${cost}  {time:.1f}s"
            draw_text(cost_text, panel_x + 10, y, ui.TECHTREE_DESC_SIZE, color(255, 215, 0))
            y += 18
            # 站点条件（研究塔）：station=true 需研究塔学识/工具/材料；
            # station=false 无需研究塔，基地手研即可（有 mat_req 则需背包材料）
            if tree.node_station(node_id):
                attr_req, tool_req, mat_req = tree.node_requirements(node_id)
                req_bits = []
                if attr_req > 0:
                    req_bits.append(f"学识≥{attr_req}")
                if tool_req:
                    req_bits.append("工具:" + ",".join(
                        TOOL_TYPES.get(t, None).label_cn if t in TOOL_TYPES else t
                        for t in tool_req))
                if mat_req:
                    req_bits.append("材料:" + ",".join(
                        f"{MATERIAL_NAMES_CN.get(m, m)}x{n}" for m, n in mat_req.items()))
                cond_text = "塔: " + ("、".join(req_bits) if req_bits else "研究塔")
                draw_text(cond_text, panel_x + 10, y, ui.TECHTREE_DESC_SIZE, color(180, 170, 255))
                y += 18
            else:
                attr_req, tool_req, mat_req = tree.node_requirements(node_id)
                bits = []
                if mat_req:
                    bits.append("材料:" + ",".join(
                        f"{MATERIAL_NAMES_CN.get(m, m)}x{n}" for m, n in mat_req.items()))
                if bits:
                    draw_text("手研: 无需研究塔，需" + "、".join(bits),
                              panel_x + 10, y, ui.TECHTREE_DESC_SIZE, color(170, 220, 180))
                else:
                    draw_text("手研: 无需研究塔", panel_x + 10, y,
                              ui.TECHTREE_DESC_SIZE, color(170, 220, 180))
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
            eta = tree.eta_seconds(node_id, unlocked_tech or set(),
                                   lanes=lanes)

            y = panel_y + 12
            # 名称
            draw_text(name, panel_x + 10, y, ui.TECHTREE_INFO_SIZE, color(220, 220, 240))
            y += 20
            # 时代·分类
            info_text = f"{era_name}·{track_name}"
            draw_text(info_text, panel_x + 10, y, ui.TECHTREE_DESC_SIZE, color(150, 155, 175))
            y += 18
            # 进度
            state_text = {"locked": "× 未开始", "partial": "◐ 进行中",
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
                                game_money, unlocked_tech, lanes):
        """渲染研究按钮（多状态）。

        - 该节点在研 → 进度条 + "研究中 x/y s"（不可点）
        - 该节点已排队 → "取消排队"（点击退款取消）
        - 场上无研究塔 → 禁用"需要研究塔"
        - 否则 → "研究 ($cost)"，被前置/资金/站点/队列满拦截时显示原因
        """
        bx, by, bw, bh = self.research_button_rect(viewport_w, viewport_h)

        node_id = self.selected_id
        if not node_id:
            return

        node = tree.nodes.get(node_id, {})
        cost = node.get("cost", 0)
        active_ids = {l["current"] for l in lanes if l.get("current")}
        queued_ids = {q for l in lanes for q in (l.get("queue") or ())}

        # 在研本节点 → 进度展示
        busy_lane = next((l for l in lanes if l.get("current") == node_id), None)
        if busy_lane is not None and busy_lane.get("total", 0) > 0:
            done, total, frac = tree.research_progress(
                node_id, busy_lane.get("timer", 0.0), busy_lane.get("total", 0.0))
            draw_rect(bx, by - 10, bw, 6, color(*ui.TECHTREE_LANE_PROGRESS_BG))
            draw_rect(bx, by - 10, max(1, int(bw * frac)), 6,
                      color(*ui.TECHTREE_LANE_PROGRESS_FG))
            draw_rect(bx, by, bw, bh, color(*ui.TECHTREE_BTN_DISABLED))
            draw_rect_lines(rect(bx, by, bw, bh), 1, color(80, 80, 90))
            btn_text = f"研究中 {done:.0f}/{total:.0f}s"
            self._draw_btn_text(btn_text, bx, by, bw, bh, ok=False)
            return

        # 已排队 → 取消排队按钮（可点）
        if node_id in queued_ids:
            btn_text = "取消排队"
            enabled = True
            reason = ""
        else:
            # 无研究塔：仅 station=true 的科技需要研究塔；station=false 手研即可
            if not lanes and tree.node_station(node_id):
                btn_text, enabled, reason, can_ok = "需要研究塔", False, "", False
            else:
                ok, reason = tree.can_research(node_id, unlocked_tech or set(),
                                               game_money, active_ids, queued_ids)
                can_ok = ok
                # 站点/材料条件：station=true 对目标塔（或自动最优塔）评估学识/工具/材料；
                # station=false 手研检查背包研发材料（mat_req）
                if ok and self.game is not None:
                    target = self._target_tower()
                    if target is not None:
                        st_ok, st_reason = self.game._station_satisfied(node_id, target)
                    else:
                        st_ok, st_reason = self.game._station_satisfied(node_id)
                    if not st_ok:
                        ok = False
                        reason = st_reason
                # 队列容量校验（BUG-117）：与 game._research_tech 的队列上限
                # 判定同源，否则按钮显示可点、点击却只弹"队列已满"。
                if ok and self.game is not None:
                    lane_obj = self._research_click_lane(node_id)
                    if lane_obj is not None:
                        total = (len(getattr(lane_obj, "research_queue", ()) or ())
                                 + (1 if getattr(lane_obj, "research_current", None) else 0))
                        if total >= config.RESEARCH_QUEUE_MAX:
                            ok = False
                            reason = "该研究通道队列已满"
                btn_text = "研究" + (f" ({cost})" if ok else "")
                enabled = ok

        if self._hover_btn and enabled:
            bg = color(*ui.TECHTREE_BTN_HOVER)
        elif enabled:
            bg = color(*ui.TECHTREE_BTN_BG)
        else:
            bg = color(*ui.TECHTREE_BTN_DISABLED)

        draw_rect(bx, by, bw, bh, bg)
        draw_rect_lines(rect(bx, by, bw, bh), 1,
                        color(80, 120, 80) if enabled else color(80, 80, 90))

        if enabled:
            btn_text = f"研究 ({cost})" if btn_text == "研究" else btn_text
        elif reason:
            btn_text = reason
        self._draw_btn_text(btn_text, bx, by, bw, bh, ok=enabled)

    def _draw_btn_text(self, btn_text, bx, by, bw, bh, ok=True):
        """按钮文字（超长截断 + 居中绘制，含配色）。"""
        while btn_text and measure_text(btn_text + "…", ui.TECHTREE_BUTTON_SIZE) > bw - 6:
            btn_text = btn_text[:-1]
        if not btn_text:
            btn_text = "…"
        draw_text(btn_text,
                  bx + (bw - measure_text(btn_text, ui.TECHTREE_BUTTON_SIZE)) / 2,
                  by + (bh - ui.TECHTREE_BUTTON_SIZE) / 2,
                  ui.TECHTREE_BUTTON_SIZE,
                  color(*ui.TECHTREE_BTN_TEXT) if ok else color(200, 160, 160))

    def _render_lanes(self, font, viewport_w, viewport_h, lanes):
        """渲染研究通道条（每座研究塔一行：在研/暂停/空闲 + 队列 + 取消按钮）。

        取消按钮坐标须与 lane_buttons() 完全一致（队列 ✕ 自右往左，当前项 ✕ 在其左）。
        """
        rows = self.lane_rows(viewport_w, viewport_h)
        if not rows:
            return
        tree = tt.tech_tree()
        fs = ui.TECHTREE_FOOTER_SIZE
        for x, y, w, h, lane, i in rows:
            tower = lane.get("tower")
            hand = lane.get("hand", False)
            lv = getattr(tower, "level", 1)
            attr = getattr(tower, "research_attribute", lambda: 0)() if tower is not None else 0
            sel = self.target_lane_index == i
            draw_rect(int(x), int(y), int(w), int(h),
                      color(*ui.TECHTREE_LANE_BG_SEL) if sel else color(*ui.TECHTREE_LANE_BG))
            if sel:
                draw_rect_lines(rect(x, y, w, h), 1, color(*ui.TECHTREE_LANE_BORDER))

            tx = x + 6
            if hand:
                # 基地手研通道（station=false 科技，无需研究塔）
                head = f"T{i + 1} 手研"
            else:
                head = f"T{i + 1} Lv{lv}(学识{attr})"
            if sel:
                head = "▶" + head
            draw_text(head, int(tx), int(y + 5), fs, color(*ui.TECHTREE_LANE_TEXT))
            tx += measure_text(head, fs) + 12

            cur = lane.get("current")
            q = lane.get("queue") or ()
            if cur:
                name = tree.node_name(cur)
                total = float(lane.get("total", 0) or 0)
                timer = float(lane.get("timer", 0) or 0)
                frac = max(0.0, min(1.0, 1.0 - timer / total)) if total > 0 else 0.0
                draw_text(f"⇄ {name}", int(tx), int(y + 5), fs, color(255, 220, 150))
                tx += measure_text(f"⇄ {name}", fs) + 8
                pb_w = 110
                draw_rect(int(tx), int(y + h / 2 - 2), pb_w, 5,
                          color(*ui.TECHTREE_LANE_PROGRESS_BG))
                if frac > 0:
                    draw_rect(int(tx), int(y + h / 2 - 2), max(1, int(pb_w * frac)), 5,
                              color(*ui.TECHTREE_LANE_PROGRESS_FG))
                tx += pb_w + 8
                draw_text(f"{max(0.0, timer):.0f}s", int(tx), int(y + 5), fs,
                          color(160, 200, 235))
            elif lane.get("blocked"):
                draw_text(f"⛔ {lane['blocked']}", int(tx), int(y + 5), fs,
                          color(*ui.TECHTREE_LANE_BLOCKED))
            else:
                draw_text("空闲", int(tx), int(y + 5), fs, color(120, 150, 130))

            # 队列文本（右对齐，让开取消按钮区）
            if q:
                names = "、".join(tree.node_name(qq) for qq in q[:3])
                if len(q) > 3:
                    names += "…"
                qtext = f"队列{len(q)}: {names}"
                cancel_area = 14 + min(len(q), 2) * 22 + 10
                qx = w - cancel_area - measure_text(qtext, fs)
                qx = max(tx + 10, qx)
                draw_text(qtext, int(qx), int(y + 5), fs, color(*ui.TECHTREE_LANE_QLEN))

            # 取消按钮（与 lane_buttons 命中矩形完全一致，避免 1px 错位（BUG-137）：
            # 队列 ✕ 自右往左，当前项 ✕ 在其左）
            bx = w - 15
            for qi in range(min(len(q), 2)):
                draw_rect(bx, int(y + 4), 12, h - 8, color(110, 70, 70))
                draw_text("✕", bx + 2, int(y + 6), fs, color(235, 190, 190))
                bx -= 22
            if cur:
                draw_rect(bx, int(y + 4), 12, h - 8, color(*ui.TECHTREE_LANE_CANCEL))
                draw_text("✕", bx + 2, int(y + 6), fs, color(255, 210, 210))

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
