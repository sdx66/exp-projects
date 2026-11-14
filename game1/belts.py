"""
belts.py - 传送带物流系统模拟核心

纯逻辑、可单测、不依赖 raylib。仅 import config / items / gamedata / math。

方向约定:
  N=0(z-1) E=1(x+1) S=2(z+1) W=3(x-1)
  direction = 物品流动方向（出口侧）；输入侧 = 反方向（opposite）。
  DXDZ[d] = 方向 d 的单位位移。

带类型（data/belts.json）:
  belt     传送带: 一格一段，方向 4 向；货物从入口(反方向)流向出口(direction)。
  splitter 均分器: 一入二出；direction 表示输入侧（货物从该方向进入），
            两条输出为 direction 的左右各 90°（left/right），按 counter 轮转。
  bridge   立交桥: 两条互相垂直的带在同一格交叉互不影响；direction 是
            orientation 0-3，由 BRIDGE_LANES 表给出两条独立 lane 的方向。

规则:
  - 每段带容量 = capacity（空间单位），载货数 ≤ floor(capacity / item_volume)。
  - 同一段上相邻货物保持间距（前件头部 - 前件体积/容量 ≥ 后件头部），
    后件不能越过前件；新件只能在尾件完全进入后（尾件头部 ≥ 体积/容量）放入。
  - 带上只运材料（武器/工具不落带）。
  - 塔→带 / 带→塔 均按 config.BELT_PORT_RATE 限速（每塔共享配额）。
  - 加工塔自动输出其 processed 产物；仓储塔仅输出 belt_export 白名单中的材料。
"""

import config
import gamedata
import math
from items import item_volume

# ─── 方向 ──────────────────────────────────────────────────
N, E, S, W = 0, 1, 2, 3
DXDZ = ((0, -1), (1, 0), (0, 1), (-1, 0))


def opposite(d: int) -> int:
    return (d + 2) % 4


def left(d: int) -> int:
    return (d + 3) % 4


def right(d: int) -> int:
    return (d + 1) % 4


# 立交桥 orientation → (laneA 方向, laneB 方向)
# 0: A=N→S, B=W→E | 1: A=S→N, B=E→W | 2: A=N→S, B=E→W | 3: A=S→N, B=W→E
BRIDGE_LANES = (
    (S, E),
    (N, W),
    (S, W),
    (N, E),
)

# ─── 带类型定义 ─────────────────────────────────────────────
# 直接从 gamedata.BELTS_RAW 取（唯一事实来源在 data/belts.json）


class Belt:
    """一段传送带（一格）。kind ∈ belt/splitter/bridge。"""

    __slots__ = ("kind", "direction", "cell", "y", "speed", "capacity",
                 "items", "counter", "lanes")

    def __init__(self, kind, direction, cell, y,
                 speed=None, capacity=None, counter=0):
        self.kind = kind
        self.direction = int(direction) % 4
        self.cell = (int(cell[0]), int(cell[1]))
        self.y = float(y)
        d = gamedata.BELTS_RAW.get(kind, {})
        self.speed = float(speed if speed is not None else d.get("speed", 1.5))
        self.capacity = int(capacity if capacity is not None else d.get("capacity", 16) or 16)
        self.items = []          # [{"name": str, "head": float}]  belt/splitter
        self.counter = int(counter or 0)   # splitter 轮转计数
        if kind == "bridge":
            da, db = BRIDGE_LANES[self.direction]
            self.lanes = [
                {"direction": da, "items": []},
                {"direction": db, "items": []},
            ]
        else:
            self.lanes = None

    # ── 流查询 ──────────────────────────────────────────────
    def _stream_for_input(self, from_dir):
        """接收来自 from_dir 的流 → (items_list, 该流容量)。"""
        if self.kind == "bridge":
            for l in self.lanes:
                if l["direction"] == from_dir:
                    return l["items"], self.capacity
            return None, 0
        return self.items, self.capacity

    def accepts_input_from(self, from_dir) -> bool:
        """该带能否接收从 from_dir 方向来的货物（即其入口侧）。"""
        if self.kind == "bridge":
            return any(l["direction"] == from_dir for l in self.lanes)
        return from_dir == self.direction

    @staticmethod
    def _stream_used(items) -> int:
        return sum(item_volume(it["name"]) for it in items)

    # ── 插入 ────────────────────────────────────────────────
    def can_insert(self, name, from_dir) -> bool:
        """能否从 from_dir 侧放入 1 件 name（容量足够 + 尾部间距允许）。"""
        items, cap = self._stream_for_input(from_dir)
        if items is None:
            return False
        v = item_volume(name)
        if self._stream_used(items) + v > cap:
            return False
        if items:
            tail = min(items, key=lambda it: it["head"])
            # 新件头部=0，需尾件完全进入（尾件头部 ≥ 其体积/容量）才不重叠
            if tail["head"] < item_volume(tail["name"]) / cap:
                return False
        return True

    def insert(self, name, from_dir) -> bool:
        """从 from_dir 侧放入 1 件 name（头部=0）。返回是否放入。"""
        items, _cap = self._stream_for_input(from_dir)
        if items is None:
            return False
        items.append({"name": name, "head": 0.0})
        return True

    # ── 推进 ────────────────────────────────────────────────
    def advance(self, dt):
        """所有货物按带速推进（间距 clamp；前件封顶在 1.0）。"""
        step = self.speed * dt
        if self.kind == "bridge":
            for l in self.lanes:
                _advance_items(l["items"], step, self.capacity)
        else:
            _advance_items(self.items, step, self.capacity)

    # ── 出口就绪 ────────────────────────────────────────────
    def ready_exits(self):
        """返回 [(name, out_dir, key)] 出口就绪（head ≥ 1.0）的货物。

        out_dir: belt/bridge 为固定出口方向；splitter 为 None（由调用方按
        counter 轮转解析）。key 供 remove_at 定位。
        """
        out = []
        if self.kind == "bridge":
            for li, l in enumerate(self.lanes):
                for idx, it in enumerate(l["items"]):
                    if it["head"] >= 1.0:
                        out.append((it["name"], l["direction"], ("lane", li, idx)))
        elif self.kind == "splitter":
            for idx, it in enumerate(self.items):
                if it["head"] >= 1.0:
                    out.append((it["name"], None, ("items", idx)))
        else:
            for idx, it in enumerate(self.items):
                if it["head"] >= 1.0:
                    out.append((it["name"], self.direction, ("items", idx)))
        return out

    def remove_at(self, key):
        """按 key 移除一件货物（key 来自 ready_exits）。"""
        if key[0] == "lane":
            _, li, idx = key
            items = self.lanes[li]["items"]
        else:
            _, idx = key
            items = self.items
        if 0 <= idx < len(items):
            items.pop(idx)

    # ── 拆分出口方向 ────────────────────────────────────────
    def pick_splitter_out(self):
        """splitter 当前首选出口方向（left 或 right，按 counter 奇偶）。"""
        return left(self.direction) if self.counter % 2 == 0 else right(self.direction)


def _advance_items(items, step, capacity):
    """单流货物推进（间距 clamp）。items 为 [{"name","head"}]，头部越大越靠前。"""
    if not items:
        return
    items.sort(key=lambda it: it["head"], reverse=True)
    items[0]["head"] = min(1.0, items[0]["head"] + step)
    for i in range(1, len(items)):
        front = items[i - 1]
        # 后件头部 ≤ 前件尾部（前件头部 - 前件体积/容量）
        limit = front["head"] - item_volume(front["name"]) / capacity
        cur = items[i]["head"] + step
        items[i]["head"] = min(limit, cur)


# ─── 塔↔带通用 ──────────────────────────────────────────────

def tower_is_belt_input(tower) -> bool:
    """塔能否接受传送带输入（有存储/加工容量，或为燃电站燃料桶）。"""
    return (getattr(tower, "storage_capacity", 0) > 0
            or getattr(tower, "processing_capacity", 0) > 0
            or getattr(tower, "power_kind", None) == "station")


def tower_deposit_from_belt(tower, name) -> bool:
    """带→塔：存入 1 件 name。燃电站进 power_fuel 桶；加工塔进 processing 池；
    其余进 storage 池。返回是否成功。"""
    if getattr(tower, "power_kind", None) == "station":
        # 燃电站只收其燃料类型
        fuels = getattr(tower, "fuel_types", None) or []
        if name not in fuels:
            return False
        fuel = getattr(tower, "power_fuel", None)
        if fuel is None:
            return False
        fuel[name] = fuel.get(name, 0) + 1
        return True
    pc = getattr(tower, "processing_capacity", 0)
    sc = getattr(tower, "storage_capacity", 0)
    if pc and tower.processing_can_store(name, 1):
        return tower.processing_deposit(name, 1) > 0
    if sc and tower.can_store(name, 1):
        return tower.deposit(name, 1) > 0
    return False


def tower_export_pick(tower):
    """塔→带：选一件可输出的材料名（None = 无货可出）。

    加工塔：自动输出 processed 产物（raw 原料保留用于加工）。
    仓储塔：仅输出 belt_export 白名单中的材料（默认空 = 不导出）。
    研究塔：默认不导出（belt_export 空）。
    """
    pc = getattr(tower, "processing_capacity", 0)
    if pc:
        mats = getattr(tower, "processing", {}).get("materials", {})
        # 只出加工产物（processed 类），raw 原料保留待加工
        for name in list(mats):
            if name in gamedata.PROCESSED and mats[name] > 0:
                return name
        return None
    sc = getattr(tower, "storage_capacity", 0)
    if sc:
        export = getattr(tower, "belt_export", None) or set()
        mats = getattr(tower, "storage", {}).get("materials", {})
        for name in export:
            if mats.get(name, 0) > 0:
                return name
    return None


def tower_withdraw_for_belt(tower, name) -> bool:
    """塔→带：从塔取走 1 件 name（加工池优先，其次存储池）。"""
    pc = getattr(tower, "processing_capacity", 0)
    if pc:
        got = tower.processing_withdraw(name, 1)
        if got > 0:
            return True
    sc = getattr(tower, "storage_capacity", 0)
    if sc:
        got = tower.withdraw(name, 1)
        if got > 0:
            return True
    return False


# ─── 每帧更新 ──────────────────────────────────────────────

def update_belts(dt, belts, towers):
    """推进所有带 + 段间转运 + 塔↔带。返回 None。

    belts: list[Belt]；towers: list[Tower]。塔的格位取 floor(position)。
    """
    if not belts:
        return
    # 占用者表：格 → ("belt"|"tower", obj)。带与塔互斥占格（放带时已校验）。
    occupant = {}
    for b in belts:
        occupant[b.cell] = ("belt", b)
    for t in towers:
        if getattr(t, "is_mining", False):
            for (cx, cz) in t.footprint_cells():
                occupant[(cx, cz)] = ("tower", t)
        else:
            occupant[(math.floor(t.position[0]), math.floor(t.position[2]))] = ("tower", t)

    # 带→塔限速计时器递减（R10-V3：与塔→带 _belt_out_timer 对称，见
    # _can_transfer_to/_do_transfer 的 BELT_PORT_RATE 闸门）
    for t in towers:
        tin = getattr(t, "_belt_in_timer", 0.0)
        if tin > 0:
            tin -= dt
            t._belt_in_timer = tin if tin > 0 else 0.0

    # 1. 推进
    for b in belts:
        b.advance(dt)

    # 2. 段间转运 + 带→塔（背压：前方无接收方/满/限速中则留在出口等待）
    for b in belts:
        _transfer_belt_exits(b, occupant)

    # 3. 塔→带（限速 BELT_PORT_RATE，每塔共享配额）
    _tower_to_belt(dt, towers, occupant)


def _transfer_belt_exits(b, occupant):
    """把 b 上就绪的出口货物转移到相邻带/塔。"""
    for name, out_dir, key in b.ready_exits():
        if b.kind == "splitter":
            actual = _splitter_actual_out(b, name, occupant)
            if actual is None:
                continue  # 两路都堵，留在出口等待
        else:
            actual = out_dir
            if not _can_transfer_to(name, actual, b.cell, occupant):
                continue
        if _do_transfer(name, actual, b.cell, occupant):
            b.remove_at(key)
            if b.kind == "splitter":
                b.counter += 1


def _splitter_actual_out(b, name, occupant):
    """均分器首选出口，被堵则走另一侧；全堵返回 None。"""
    pref = b.pick_splitter_out()
    if _can_transfer_to(name, pref, b.cell, occupant):
        return pref
    alt = right(b.direction) if pref == left(b.direction) else left(b.direction)
    if _can_transfer_to(name, alt, b.cell, occupant):
        return alt
    return None


def _can_transfer_to(name, out_dir, src_cell, occupant) -> bool:
    """能否把 name 从 src_cell 沿 out_dir 转移到相邻格。

    带→塔目标除容量检查外还须通过 BELT_PORT_RATE 限速（R10-V3）：塔的
    带输入配额（_belt_in_timer ≤ 0）可用才放行，否则货物留在带出口等待
    （背压），与 config.belts.py 注释"带→塔按 BELT_PORT_RATE 限速"一致。
    """
    dx, dz = DXDZ[out_dir]
    occ = occupant.get((src_cell[0] + dx, src_cell[1] + dz))
    if occ is None:
        return False
    kind, obj = occ
    if kind == "belt":
        return obj.accepts_input_from(out_dir) and obj.can_insert(name, out_dir)
    return (tower_is_belt_input(obj) and _belt_can_deposit_tower(obj, name)
            and getattr(obj, "_belt_in_timer", 0.0) <= 0)


def _belt_can_deposit_tower(tower, name) -> bool:
    """塔是否有空间接收 1 件 name（带→塔容量检查，不实际存入）。"""
    if getattr(tower, "power_kind", None) == "station":
        fuels = getattr(tower, "fuel_types", None) or []
        return name in fuels
    pc = getattr(tower, "processing_capacity", 0)
    sc = getattr(tower, "storage_capacity", 0)
    if pc and tower.processing_can_store(name, 1):
        return True
    if sc and tower.can_store(name, 1):
        return True
    return False


def _do_transfer(name, out_dir, src_cell, occupant) -> bool:
    """实际转移 1 件 name 到相邻格。成功返回 True。"""
    dx, dz = DXDZ[out_dir]
    occ = occupant.get((src_cell[0] + dx, src_cell[1] + dz))
    if occ is None:
        return False
    kind, obj = occ
    if kind == "belt":
        if obj.accepts_input_from(out_dir) and obj.can_insert(name, out_dir):
            obj.insert(name, out_dir)
            return True
        return False
    if tower_deposit_from_belt(obj, name):
        # R10-V3：带→塔成功一次后，该塔带输入配额进入冷却（1/BELT_PORT_RATE
        # 秒），全帧其余带口/其它带对它的送入都被 _can_transfer_to 闸门拦住。
        obj._belt_in_timer = 1.0 / config.BELT_PORT_RATE
        return True
    return False


def _tower_to_belt(dt, towers, occupant):
    """塔→带：各塔按 BELT_PORT_RATE 把可输出材料送到相邻接收带。"""
    rate = config.BELT_PORT_RATE
    for t in towers:
        if not tower_is_belt_input(t):
            continue
        timer = getattr(t, "_belt_out_timer", 0.0)
        timer -= dt
        if timer > 0:
            t._belt_out_timer = timer
            continue
        name = tower_export_pick(t)
        if name is None:
            t._belt_out_timer = 0.0
            continue
        # 开采塔按足迹周边找带（锚点单格不覆盖全部外沿），其余塔单格
        if getattr(t, "is_mining", False):
            cells = [(math.floor(px), math.floor(pz)) for (px, pz) in t.footprint_cells()]
        else:
            cells = [(math.floor(t.position[0]), math.floor(t.position[2]))]
        placed = False
        for (tx, tz) in cells:
            for d in (N, E, S, W):
                dx, dz = DXDZ[d]
                occ = occupant.get((tx + dx, tz + dz))
                if occ is None or occ[0] != "belt":
                    continue
                belt = occ[1]
                if belt.accepts_input_from(d) and belt.can_insert(name, d):
                    if tower_withdraw_for_belt(t, name):
                        belt.insert(name, d)
                        placed = True
                        break
            if placed:
                break
        t._belt_out_timer = (1.0 / rate) if placed else 0.0
