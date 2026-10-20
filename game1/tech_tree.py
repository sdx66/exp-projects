"""
tech_tree.py - 科技树运行时模型

纯逻辑、可单测、不依赖 raylib：
  - 状态计算：locked / available / researching / unlocked
  - 进度计算：成果完成度、ETA
  - 布局计算：树形布局（技术页自下而上：y=tier 行，x=行内叶序；成果页按轨分列）
  - 命中测试：node_at(screen_pos, view)
  - 视口数学：graph_to_screen / screen_to_graph / zoom_at / clamp_pan
"""

import math
from collections import defaultdict
import gamedata


# ─── 状态常量 ─────────────────────────────────────────────────

STATE_LOCKED = "locked"
STATE_AVAILABLE = "available"
STATE_RESEARCHING = "researching"
STATE_UNLOCKED = "unlocked"

ACH_LOCKED = "locked"
ACH_PARTIAL = "partial"
ACH_UNLOCKED = "unlocked"

PAGE_ACHIEVEMENTS = "ach"
PAGE_BREAKTHROUGHS = "tech"

# ─── 布局常量 ─────────────────────────────────────────────────

COL_WIDTH = 220
ROW_HEIGHT = 52
TIER_GAP = 150      # 技术页纵轴行距：相邻 tier 行之间的间距（根在底部）
NODE_WIDTH = 180
NODE_HEIGHT = 36
ACHIEVEMENT_NODE_HEIGHT = 44
LABEL_FONT_SIZE = 13
LOD_THRESHOLD = 0.7
MIN_ZOOM = 0.35
MAX_ZOOM = 2.5
DRAG_THRESHOLD_PX = 4

# 依赖线淡化：跨 span 层 → alpha *= EDGE_FADE_DECAY^(span-1)，下限 EDGE_FADE_MIN
EDGE_FADE_DECAY = 0.68
EDGE_FADE_MIN = 0.2


# ─── 布局缓存 ─────────────────────────────────────────────────

_layout_cache = {}


def clear_layout_cache():
    """清除布局缓存（数据变更时调用）。"""
    _layout_cache.clear()


# ─── 树索引 ───────────────────────────────────────────────────

class TechTree:
    """科技树索引：从 gamedata 构建节点/成果索引和父子关系。"""

    def __init__(self):
        self.nodes = {}          # id → node data (dict)
        self.achievements = {}   # id → achievement data (dict)
        self.by_era = defaultdict(dict)       # era → {id → node}
        self.by_category = defaultdict(dict)  # (era, track) → {id → node}
        self.children = defaultdict(list)     # parent_id → [child_id, ...]
        self.parents = {}                      # child_id → parent_id
        self.era_tracks = defaultdict(list)    # era → [track, ...]
        self._build()

    def _build(self):
        """从 gamedata 构建索引。"""
        bt_data = gamedata.TECH_BREAKTHROUGHS
        ach_data = gamedata.TECH_ACHIEVEMENTS

        for bt_id, bt in bt_data.items():
            era = bt["era"]
            track = bt["track"]
            self.nodes[bt_id] = bt
            self.by_era[era][bt_id] = bt
            self.by_category[(era, track)][bt_id] = bt

        for ach_id, ach in ach_data.items():
            self.achievements[ach_id] = ach

        # bt → 所属成果（成果页画 ach↔ach 边用）
        self.bt_to_ach = {}
        for ach_id, ach in ach_data.items():
            for bt_id in ach.get("breakthroughs", []):
                self.bt_to_ach[bt_id] = ach_id

        # 树形父子关系：只用 parent_id（同轨 ├/└ 父），不用 ⊕ 跨轨软依赖。
        # 否则 C8↔J11 / N8↔A14 这类互 ⊕ 会被拉成环，污染布局的 children 图。
        for bt_id, bt in bt_data.items():
            pid = bt.get("parent_id")
            if pid and pid in bt_data:
                self.children[pid].append(bt_id)
                self.parents[bt_id] = pid

        # 构建 era→tracks 映射
        for era, tracks in gamedata.TECH_CATEGORIES.items():
            self.era_tracks[era] = list(tracks.keys())

    def node_state(self, node_id, unlocked_set, researching_id=None):
        """计算节点状态。"""
        node = self.nodes.get(node_id)
        if not node:
            return STATE_LOCKED
        if node_id in unlocked_set:
            return STATE_UNLOCKED
        if node_id == researching_id:
            return STATE_RESEARCHING
        # 检查前置是否全部解锁
        requires = node.get("requires", [])
        if requires:
            for req in requires:
                if req not in unlocked_set:
                    return STATE_LOCKED
        return STATE_AVAILABLE

    def achievement_state(self, ach_id, unlocked_set):
        """计算成果状态。"""
        ach = self.achievements.get(ach_id)
        if not ach:
            return ACH_LOCKED
        bts = ach.get("breakthroughs", [])
        if not bts:
            return ACH_LOCKED
        done = sum(1 for bt in bts if bt in unlocked_set)
        if done >= len(bts):
            return ACH_UNLOCKED
        if done > 0:
            return ACH_PARTIAL
        return ACH_LOCKED

    def achievement_progress(self, ach_id, unlocked_set):
        """返回 (done, total, fraction)。"""
        ach = self.achievements.get(ach_id)
        if not ach:
            return 0, 0, 0.0
        bts = ach.get("breakthroughs", [])
        total = len(bts)
        if total == 0:
            return 0, 0, 0.0
        done = sum(1 for bt in bts if bt in unlocked_set)
        return done, total, done / total

    def node_name(self, node_id):
        """突破点中文名（缺失时退回 ID）。跨时代前置也照此解析。"""
        node = self.nodes.get(node_id)
        return (node or {}).get("name_cn") or node_id

    def can_research(self, node_id, unlocked_set, money, researching_id=None):
        """检查是否可以研究，返回 (ok, reason)。reason 用中文名，直接给玩家看。"""
        node = self.nodes.get(node_id)
        if not node:
            return False, "节点不存在"
        if node_id in unlocked_set:
            return False, "已解锁"
        if researching_id and researching_id != node_id:
            return False, "正在研究其他科技"
        requires = node.get("requires", [])
        for req in requires:
            if req not in unlocked_set:
                return False, f"需要前置: {self.node_name(req)}"
        if money < node.get("cost", 0):
            return False, f"资金不足 (需要 {node.get('cost', 0)})"
        return True, "可以研究"

    def eta_seconds(self, ach_id, unlocked_set):
        """估算成果完成剩余时间（单线研究，下界）。"""
        ach = self.achievements.get(ach_id)
        if not ach:
            return 0.0
        bts = ach.get("breakthroughs", [])
        total_time = 0.0
        for bt_id in bts:
            if bt_id not in unlocked_set:
                node = self.nodes.get(bt_id, {})
                total_time += node.get("time", 0)
        return total_time

    def remaining_seconds(self, researching_id, research_timer):
        """返回研究中节点的剩余时间。"""
        if not researching_id:
            return 0.0
        return max(0.0, research_timer)

    def research_progress(self, researching_id, research_timer, research_total):
        """返回 (done_time, total_time, fraction)。"""
        if not researching_id or research_total <= 0:
            return 0, 0, 0.0
        done = max(0.0, research_total - research_timer)
        return done, research_total, done / research_total


# ─── 视口数学 ─────────────────────────────────────────────────

class Viewport:
    """视口状态：pan + zoom。"""

    def __init__(self, pan_x=0.0, pan_y=0.0, zoom=1.0):
        self.pan_x = pan_x
        self.pan_y = pan_y
        self.zoom = zoom

    def clone(self):
        return Viewport(self.pan_x, self.pan_y, self.zoom)


def graph_to_screen(view, gx, gy):
    """图坐标 → 屏幕坐标。"""
    return (gx * view.zoom + view.pan_x, gy * view.zoom + view.pan_y)


def screen_to_graph(view, sx, sy):
    """屏幕坐标 → 图坐标。"""
    return ((sx - view.pan_x) / view.zoom, (sy - view.pan_y) / view.zoom)


def zoom_at(view, screen_x, screen_y, factor):
    """以屏幕点为锚缩放，保持该点下的图坐标不动。"""
    old_gx, old_gy = screen_to_graph(view, screen_x, screen_y)
    new_zoom = max(MIN_ZOOM, min(MAX_ZOOM, view.zoom * factor))
    if new_zoom == view.zoom:
        return
    view.zoom = new_zoom
    # 重新计算 pan 使得锚点不动
    view.pan_x = screen_x - old_gx * new_zoom
    view.pan_y = screen_y - old_gy * new_zoom


def _clamp_axis(pan, size, start, end):
    """单轴平移钳制：内容小于视口则完全留在内部；大于视口则可自由平移到边缘。

    pan 是内容原点偏移量。内容屏幕区间为 [pan, pan+size]，视口区间为 [start, end]。
    - 内容 <= 视口：pan ∈ [start, end-size]，内容整个留在视口内、居中不动。
    - 内容 > 视口：pan ∈ [end-size, start]，可自由拖到内容的左/上边缘与右/下边缘，
      避免出现「放大后拖不动、卡住」的假限制（此前用固定 keep 比例保留可见内容，
      内容比视口大很多时这个比例不可达，反而把平移死锁在视口附近）。
    返回被钳制后的 pan 值。
    """
    span = end - start
    if size <= span:
        lo, hi = start, end - size
    else:
        lo, hi = end - size, start
    return max(lo, min(hi, pan))


def clamp_pan(view, content_bbox, viewport_w, viewport_h,
              inset_top=0.0, inset_bottom=0.0):
    """限制平移范围，保证内容不跑出视口。

    内容小于视口时居中锁定；大于视口时可自由平移到内容边缘（放大后能把整棵树
    拖到左/上/右/下边缘，不再被固定 keep 比例限制住）。
    inset_top/inset_bottom 是顶/底的预留高度（表头/底栏），y 轴平移范围限制在
    [inset_top, viewport_h - inset_bottom] 内。
    """
    cx, cy, cw, ch = content_bbox
    if cw <= 0 or ch <= 0:
        return
    # compute_layout 里 x/y 都从 0 起排，故 cx/cy 恒为 0，这里只需用宽高算范围。
    zoom = view.zoom
    content_sw = cw * zoom
    content_sh = ch * zoom

    view.pan_x = _clamp_axis(view.pan_x, content_sw, 0.0, float(viewport_w))
    view.pan_y = _clamp_axis(view.pan_y, content_sh,
                             float(inset_top), float(viewport_h) - float(inset_bottom))


def fit_view(view, content_bbox, viewport_w, viewport_h,
             inset_top=0.0, inset_bottom=0.0):
    """适配视口，使内容完全可见。

    inset_top/inset_bottom 是顶/底预留高度，内容在扣除预留后的可用区域内居中。
    """
    cx, cy, cw, ch = content_bbox
    if cw <= 0 or ch <= 0:
        return
    # 留边距
    margin = 40
    avail_w = viewport_w - margin * 2
    top = inset_top
    bottom = viewport_h - inset_bottom
    avail_h = (bottom - top) - margin * 2
    if avail_h <= 0:
        avail_h = margin
    zoom = min(avail_w / cw, avail_h / ch, MAX_ZOOM)
    zoom = max(MIN_ZOOM, zoom)
    view.zoom = zoom
    view.pan_x = (viewport_w - cw * zoom) / 2 - cx * zoom
    # 纵向按「内容区」[top, bottom] 居中（不是按 [0, bottom]）：
    view.pan_y = (top + bottom - ch * zoom) / 2 - cy * zoom


def drag_pan(start_pan, start_mouse, mouse, threshold=DRAG_THRESHOLD_PX):
    """把拖动位移换算为新的 pan；位移小于阈值时保持原 pan 不变。

    start_pan: (pan_x, pan_y) 按下时的 pan
    start_mouse: (x, y) 按下时的鼠标位置
    mouse: (x, y) 当前鼠标位置
    threshold: 拖动的最小位移（像素）
    """
    dx = mouse[0] - start_mouse[0]
    dy = mouse[1] - start_mouse[1]
    if abs(dx) < threshold and abs(dy) < threshold:
        return start_pan
    return (start_pan[0] + dx, start_pan[1] + dy)


# ─── 树形布局 ─────────────────────────────────────────────────

def dependency_layers(nodes):
    """按依赖关系自动分层（时代内 requires 的最长路径层数）。

    第 0 层 = 无前置（本时代内 requires 为空）的根；第 k 层只依赖低于 k 的层，
    即 layer(n) = 1 + max(layer(req))。这保证「第 0 层全是根、第 k 层最多依赖
    第 k-1 层」的整齐不变式 —— 而数据里手写的 tier 只是各轨 ASCII 树深度，
    会被跨轨硬依赖（如石器 J1 tier=0 依赖 B 轨深处的 B21）破坏。

    nodes: {id: node dict}，node 至少含 requires 字段；只统计 nodes 内的前置，
    跨时代前置天然不算入层（它们的节点不在本视图里）。
    带递归防环保护：环中的节点按 0 占位（数据当前无环，仅为防御）。
    返回 {id: layer}。
    """
    layers = {}
    visiting = set()

    def _layer(nid):
        if nid in layers:
            return layers[nid]
        if nid in visiting:
            # 环中节点：按 0 占位，避免无限递归
            return 0
        visiting.add(nid)
        reqs = [r for r in nodes[nid].get("requires", []) if r in nodes]
        layers[nid] = 0 if not reqs else 1 + max(_layer(r) for r in reqs)
        visiting.discard(nid)
        return layers[nid]

    for nid in nodes:
        _layer(nid)
    return layers


def edge_fade(span):
    """依赖线淡化系数：跨 span 层 → EDGE_FADE_DECAY^(span-1)，下限 EDGE_FADE_MIN。

    span=1（相邻层）返回 1.0；跨层越多越淡，但不会淡到看不见。
    调用方把返回值乘到边的 alpha 上。
    """
    span = max(1, int(span))
    return max(EDGE_FADE_MIN, EDGE_FADE_DECAY ** (span - 1))


def compute_layout(tree, era_id, page, viewport_w, viewport_h):
    """计算布局，返回 {node_id: (x, y, w, h)} 和边列表 [(x1,y1,x2,y2,kind,span), ...]。

    技术页（自下而上的树）：
      y = (max_layer - layer) * TIER_GAP  —— layer 由 dependency_layers() 按依赖
                                              自动分层（根在最底部，依赖级越高越靠上）
      x = 行内序号 * COL_WIDTH            —— 同层节点按叶序（叶序分配 + 父居中）打包
    成果页：按 track 分列，列内按 (tier, order, id) 排序垂直堆叠。
    边的第 6 个分量 span 是两端点的层数差，渲染层据此淡化长跨线。
    结果按 (era, page) 缓存。
    """
    cache_key = (era_id, page)
    if cache_key in _layout_cache:
        return _layout_cache[cache_key]

    if page == PAGE_BREAKTHROUGHS:
        nodes = tree.by_era.get(era_id, {})
    else:
        # 成果页：只显示该时代的成果
        nodes = {aid: ach for aid, ach in tree.achievements.items() if ach["era"] == era_id}

    if not nodes:
        _layout_cache[cache_key] = ({}, [])
        return _layout_cache[cache_key]

    if page == PAGE_BREAKTHROUGHS:
        # 突破页：树形父子关系只用 parent_id（同轨 ├/└ 父），保证无环；
        # ⊕ 跨轨软依赖只画边、不进树，避免 C8↔J11 / N8↔A14 这类互 ⊕
        # 把 children 图拉成环、把两个叶节点挤到同一 y。
        # 方向：自下而上的树 —— 根（layer 最低）在底部，依赖级越高越靠上。
        children = defaultdict(list)
        roots = []
        for nid, node in nodes.items():
            pid = node.get("parent_id")
            if pid and pid in nodes:
                children[pid].append(nid)
            else:
                roots.append(nid)

        # 按 tier 排序（直接读取，不写回数据 dict）
        roots.sort(key=lambda nid: nodes[nid].get("tier", 0))

        # 叶序分配「槽位」索引（带环保护）：叶节点得整数槽位、父节点得子槽位中点。
        # 槽位不是像素坐标，只作为同 layer 行内的排序依据（叶序分配 + 父居中）。
        y_positions = {}
        current_y = 0

        def assign_y(nid, visiting=None):
            nonlocal current_y
            if visiting is None:
                visiting = set()
            if nid in visiting:
                # 环中节点：跳过（避免无限递归）
                return y_positions.get(nid, 0)
            visiting.add(nid)
            kids = children.get(nid, [])
            if not kids:
                y_positions[nid] = current_y
                current_y += 1
                return y_positions[nid]
            child_ys = []
            for kid in kids:
                child_ys.append(assign_y(kid, visiting))
            y = (min(child_ys) + max(child_ys)) / 2
            y_positions[nid] = y
            visiting.discard(nid)
            return y

        for root in roots:
            assign_y(root)

        # 计算位置：纵轴按依赖层分排（根在最底部），横轴按 layer 行内叶序打包。
        # 用 dependency_layers() 而不是手写 tier：手写 tier 是各轨树深度，跨轨硬依赖
        # 会让它违反层级不变式（第 0 层并非全是根）；这里按 requires 最长路径分层，
        # 保证第 0 层全是根、第 k 层只依赖低于 k 的层。
        # COL_WIDTH > NODE_WIDTH 且 TIER_GAP > NODE_HEIGHT，保证任意两点不重叠。
        layers = dependency_layers(nodes)
        max_layer = max(layers.values(), default=0)
        by_layer = defaultdict(list)         # layer → [(slot, nid), ...]
        for nid in nodes:
            by_layer[layers[nid]].append((y_positions.get(nid, 0), nid))

        positions = {}
        for layer, items in by_layer.items():
            items.sort(key=lambda t: t[0])
            y = (max_layer - layer) * TIER_GAP
            for rank, (_, nid) in enumerate(items):
                positions[nid] = (rank * COL_WIDTH, y, NODE_WIDTH, NODE_HEIGHT)

        # 计算边: tree(同轨 parent_id 父子) + cross(⊕ 跨轨硬依赖 & 概念级 soft)
        # 端点：前置节点顶部中心 → 依赖节点底部中心（父在下、子在上）。
        edges = []
        for nid, node in nodes.items():
            pid = node.get("parent_id")
            for req in node.get("requires", []):
                if req in positions:
                    x1, y1, _, _ = positions[req]
                    x2, y2, _, _ = positions[nid]
                    kind = "tree" if req == pid else "cross"
                    # span = 两端点的层数差，渲染层按它淡化长跨线
                    span = abs(layers[req] - layers[nid])
                    edges.append((x1 + NODE_WIDTH / 2, y1,
                                  x2 + NODE_WIDTH / 2, y2 + NODE_HEIGHT, kind, span))
            for sreq in node.get("soft_requires", []):
                if sreq in positions:
                    x1, y1, _, _ = positions[sreq]
                    x2, y2, _, _ = positions[nid]
                    span = abs(layers[sreq] - layers[nid])
                    edges.append((x1 + NODE_WIDTH / 2, y1,
                                  x2 + NODE_WIDTH / 2, y2 + NODE_HEIGHT, "soft", span))

    else:
        # 成果页：按 track 分列，每 track 一列，列内按 (tier, order, id) 排序垂直堆叠
        track_groups = defaultdict(list)
        for aid, ach in nodes.items():
            track_groups[ach.get("track", "other")].append((aid, ach))

        # track 顺序：优先 gamedata.TECH_CATEGORIES 中的定义顺序，其余按字母序
        cat_tracks = list(gamedata.TECH_CATEGORIES.get(era_id, {}).keys())
        if cat_tracks:
            ordered = [t for t in cat_tracks if t in track_groups]
            ordered += sorted(t for t in track_groups if t not in ordered)
        else:
            ordered = sorted(track_groups.keys())

        positions = {}
        for ti, track in enumerate(ordered):
            track_nodes = track_groups[track]
            track_nodes.sort(key=lambda kv: (kv[1].get("tier", 0), kv[1].get("order", 0), kv[0]))
            for ri, (aid, _) in enumerate(track_nodes):
                positions[aid] = (ti * COL_WIDTH, ri * ROW_HEIGHT,
                                  NODE_WIDTH, ACHIEVEMENT_NODE_HEIGHT)

        # 成果页边：ach.requires 是该成果 bt 的"外部前置 bt"，经 bt→ach
        # 映射画 ach→ach 边（同期）。成果页不建树（按轨分列），互 ⊕ 在此
        # 只表现为双向边，不影响布局，故无需破环。
        edges = []
        bt_to_ach = getattr(tree, "bt_to_ach", {})
        for aid, ach in nodes.items():
            for req_bt in ach.get("requires", []):
                src = bt_to_ach.get(req_bt)
                if src and src in positions and src != aid:
                    x1, y1, _, _ = positions[src]
                    x2, y2, _, _ = positions[aid]
                    # 成果页纵轴不是依赖层，跨层级差取两端成果 tier 的差
                    ach_src = tree.achievements.get(src, {})
                    span = abs(int(ach_src.get("tier", 0)) - int(ach.get("tier", 0)))
                    edges.append((x1 + NODE_WIDTH, y1 + ACHIEVEMENT_NODE_HEIGHT / 2,
                                  x2, y2 + ACHIEVEMENT_NODE_HEIGHT / 2, "ach", span))

    _layout_cache[cache_key] = (positions, edges)
    return _layout_cache[cache_key]


# ─── 命中测试 ─────────────────────────────────────────────────

def node_at(tree, positions, view, screen_x, screen_y, page):
    """屏幕坐标 → 节点 ID。"""
    gx, gy = screen_to_graph(view, screen_x, screen_y)
    # 从后往前遍历（后面的节点在视觉上在上层）
    for nid, (x, y, w, h) in reversed(list(positions.items())):
        if x <= gx <= x + w and y <= gy <= y + h:
            return nid
    return None


def research_button_rect(view, viewport_w, viewport_h, footer_h=30, margin=8):
    """研究按钮矩形（屏幕坐标）。

    按钮必须落在底栏（footer_h）之上：以前硬编码 y=vh-60 会让 40px 高的按钮
    压住 30px 底栏 10px。
    """
    bw, bh = 100, 40
    bx = viewport_w - 120
    by = viewport_h - footer_h - margin - bh
    return (bx, by, bw, bh)


def is_in_rect(x, y, rect):
    """点是否在矩形内。"""
    rx, ry, rw, rh = rect
    return rx <= x <= rx + rw and ry <= y <= ry + rh


def visible_nodes(positions, view, viewport_w, viewport_h):
    """裁剪：只返回视口内的节点。"""
    visible = []
    for nid, (x, y, w, h) in positions.items():
        sx, sy = graph_to_screen(view, x, y)
        sw = w * view.zoom
        sh = h * view.zoom
        if sx + sw > 0 and sx < viewport_w and sy + sh > 0 and sy < viewport_h:
            visible.append((nid, x, y, w, h))
    return visible


# ─── 单例 ─────────────────────────────────────────────────────

_tree_instance = None


def tech_tree():
    """获取 TechTree 单例（懒加载）。"""
    global _tree_instance
    if _tree_instance is None:
        _tree_instance = TechTree()
    return _tree_instance
