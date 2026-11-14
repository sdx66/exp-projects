"""
power.py - 电力系统模拟核心（发电站 · 电网 · 电池储能）

纯逻辑、可单测、不依赖 raylib。仅 import config / gamedata / math。

设计（对应 plan §3）：
  - 电线（CableSegment）一格一段、无方向、4 邻接成网。电流 = 功率 / 电压，
    损耗 = Σ I²·R（每格电阻）。电流超限 → 该段跳闸（暂态断路、自动恢复）。
  - 发电站（Tower.power_kind=="station"）按额定功率 × 环境系数出力，
    燃电站消耗燃料：消耗 = 产出_w·dt / (效率·热值)。
  - 电池（Tower.power_kind=="battery"）同型 4 邻接成组：链式（每格度≤2 无环）=
    串联串（V=N·V₀，E=N·E₀，I_max=I₀）；多个等长支路 → 并联组（V=L·V₀，
    E=总·E₀，I_max=串数·I₀）；接法非法（环接/支路不等长）→ 组 idle + 警告。
  - 电网组件 = 电线连通块。组件电压级 = 组件内装置/电线的等级；多级混接 →
    "电压冲突"（警告、不传输）。装置电压级与组件级不匹配 → 不挂接。
  - 功率平衡：源（电站发电 + 电池放电）与 汇（电池充电 + 用电端 demand）按
    比例缩放取小，生成树上求各段功率流 → 电流 → 损耗 → 扣损交付/弃电/削峰。
    电池充放电按往返效率 config.POWER_BATTERY_EFF 结算储能。

电压等级标称（gamedata.VOLTAGE_CLASS_NOMINAL）: LV=220V MV=10000V HV=110000V。
"""

import config
import gamedata
import math

# ─── 方向（与 belts 同构） ───────────────────────────────────
N, E, S, W = 0, 1, 2, 3
DXDZ = ((0, -1), (1, 0), (0, 1), (-1, 0))


def wire_def(kind: str) -> dict:
    """电线类型定义（唯一事实来源在 data/wires.json）。"""
    return gamedata.WIRES_RAW.get(kind, {})


def voltage_nominal(vclass: str) -> float:
    """电压等级 → 标称电压（伏）。未知等级回落 LV。"""
    return gamedata.VOLTAGE_CLASS_NOMINAL.get(vclass, 220.0)


def class_of_voltage(v: float) -> str:
    """电压（伏）→ 等级。LV ≤240V，MV ≤20kV，其余 HV。"""
    if v <= 240.0:
        return "LV"
    if v <= 20000.0:
        return "MV"
    return "HV"


# ─── 电线 ────────────────────────────────────────────────────

class CableSegment:
    """一段电线（一格）。kind ∈ wires.json 的键。无方向、4 邻接。"""

    __slots__ = ("kind", "cell", "y", "voltage_class", "rated_voltage_v",
                 "max_current_a", "resistance", "current", "trip_timer",
                 "tripped", "loss_w")

    def __init__(self, kind, cell, y):
        self.kind = kind
        self.cell = (int(cell[0]), int(cell[1]))
        self.y = float(y)
        d = gamedata.WIRES_RAW.get(kind, {})
        self.voltage_class = str(d.get("voltage_class", "LV"))
        self.rated_voltage_v = float(d.get("rated_voltage_v", 220.0))
        self.max_current_a = float(d.get("max_current_a", 30.0))
        self.resistance = float(d.get("resistance_per_tile", 0.05))
        self.current = 0.0
        self.trip_timer = 0.0
        self.tripped = False
        self.loss_w = 0.0

    def is_overloaded(self, current_a: float) -> bool:
        """电流是否超额定（跳闸判定）。"""
        return current_a > self.max_current_a


# ─── 塔判定（duck-typing，不 import entities 避免循环） ──────

def is_station(t) -> bool:
    return getattr(t, "power_kind", None) == "station"


def is_battery(t) -> bool:
    return getattr(t, "power_kind", None) == "battery"


def is_power_building(t) -> bool:
    return is_station(t) or is_battery(t)


# ─── 电池成组 ────────────────────────────────────────────────

def _battery_adj(cells):
    """cells: list[Tower]。返回 {tower: [邻居tower]}（4 邻接）。"""
    pos = {}
    for t in cells:
        pos[(math.floor(t.position[0]), math.floor(t.position[2]))] = t
    adj = {t: [] for t in cells}
    for t in cells:
        tx, tz = math.floor(t.position[0]), math.floor(t.position[2])
        for dx, dz in DXDZ:
            nb = pos.get((tx + dx, tz + dz))
            if nb is not None and nb is not t:
                adj[t].append(nb)
    return adj


def _vertex_disjoint_paths(a, b, adj, max_paths=16):
    """a→b 的内部点不交路径组（贪心：找到一条后把其内部点从图中摘除，
    再在剩余图上继续找）。返回 list[list[Tower]]，结果两两无共享内部点。

    R5-M2：原实现 DFS 回溯时 `used.discard(nb)` 释放节点，收集到的多条
    "路径"可共享内部节点——含闭环短接的拓扑（全度≤2 不成立、两端点、
    多条等长路径共享内部节点，如环接+双尾）被误判为并联，非法环接组以
    错误电压/电流参数充放电。现改为"找到一条路径即占用其内部点"，保证
    结果真正点不交：真并联（两端点+多条互不相交支路）仍判 parallel，
    环接/网格拓扑摘掉一条路径后无第二条 a→b 路 → 判 invalid。
    """
    results = []
    remaining = {t: [nb for nb in adj[t]] for t in adj}

    def _find(cur, path):
        if len(results) >= max_paths:
            return None
        if cur is b:
            return list(path)
        for nb in remaining.get(cur, []):
            if nb in path:
                continue
            found = _find(nb, path + [nb])
            if found is not None:
                return found
        return None

    while len(results) < max_paths:
        path = _find(a, [a])
        if path is None:
            break
        results.append(path)
        # 占用这条路径的内部点：从剩余邻接图中移除
        interior = path[1:-1]
        for node in interior:
            remaining.pop(node, None)
        for t in list(remaining):
            remaining[t] = [nb for nb in remaining[t]
                            if nb not in interior]
    return results


def analyze_battery_group(towers):
    """分析一组同型电池的串并联拓扑。

    towers: 同型电池 Tower 列表（≥1）。返回 dict：
      {"kind": "series"|"parallel"|"single"|"invalid",
       "v": 组电压(伏), "energy_j": 总容量(焦), "i_max": 最大电流(安),
       "n": 节数, "warning": 原因文本或 None}
    """
    if not towers:
        return {"kind": "invalid", "v": 0, "energy_j": 0, "i_max": 0,
                "n": 0, "warning": "空组"}
    v0 = float(getattr(towers[0], "cell_voltage_v", 1.0))
    e0 = float(getattr(towers[0], "cell_energy_j", 1.0))
    i0 = float(getattr(towers[0], "cell_max_current_a", 1.0))
    n = len(towers)
    if n == 1:
        return {"kind": "single", "v": v0, "energy_j": e0, "i_max": i0,
                "n": n, "warning": None}
    adj = _battery_adj(towers)
    deg = {t: len(adj[t]) for t in towers}
    endpoints = [t for t in towers if deg[t] == 1]

    if all(d <= 2 for d in deg.values()):
        if len(endpoints) == 2:
            return {"kind": "series", "v": n * v0, "energy_j": n * e0,
                    "i_max": i0, "n": n, "warning": None}
        return {"kind": "invalid", "v": 0, "energy_j": n * e0, "i_max": 0,
                "n": n, "warning": "环接无端子（不可作为串联串）"}

    if len(endpoints) == 2:
        a, b = endpoints
        paths = _vertex_disjoint_paths(a, b, adj)
        if len(paths) >= 2:
            L = len(paths[0])
            if all(len(p) == L for p in paths):
                k = len(paths)
                return {"kind": "parallel", "v": L * v0, "energy_j": n * e0,
                        "i_max": k * i0, "n": n, "warning": None}
            return {"kind": "invalid", "v": 0, "energy_j": n * e0, "i_max": 0,
                    "n": n, "warning": "并联支路不等长"}
        return {"kind": "invalid", "v": 0, "energy_j": n * e0, "i_max": 0,
                "n": n, "warning": "两端点间无法形成并联支路"}
    return {"kind": "invalid", "v": 0, "energy_j": n * e0, "i_max": 0,
            "n": n, "warning": "电池组拓扑无法归类"}


def _battery_group_class(v: float) -> str:
    """组电压 → 等级（决定能否接入电网电压级）。"""
    return class_of_voltage(v)


# ─── 环境系数 ────────────────────────────────────────────────

def _hash01(x: int, z: int) -> float:
    """确定性 [0,1) 哈希（FNV-1a 精简版）。"""
    h = 2166136261
    for v in (x, z):
        h ^= (v & 0xFFFFFFFF)
        h = (h * 16777619) & 0xFFFFFFFF
    return (h % 100000) / 100000.0


def wind_factor(x: int, z: int) -> float:
    """风电站出力系数（0.35–1.25）。"""
    return 0.35 + _hash01(x, z) * 0.90


def solar_factor(universe) -> float:
    """光伏出力系数 = 1 - 0.6×雨强（无天气=1.0）。"""
    rain = 0.0
    if universe is not None:
        rain = float(getattr(universe, "rain_level", 0.0) or 0.0)
    return max(0.0, min(1.0, 1.0 - 0.6 * rain))


def env_factor(station, x, z, universe) -> float:
    """电站环境系数。cond 为 water_adjacent(1.0)/wind/solar。"""
    cond = getattr(station, "condition_kind", None)
    if cond == "wind":
        return wind_factor(x, z)
    if cond == "solar":
        return solar_factor(universe)
    return 1.0


# ─── 电站发电 ────────────────────────────────────────────────

def station_generation_w(station, dt, universe) -> tuple:
    """电站本帧可出力（瓦）→ (gen_w, message)。燃电站受燃料存量限制。"""
    rated = float(getattr(station, "rated_kw", 0.0)) * 1000.0
    x, z = math.floor(station.position[0]), math.floor(station.position[2])
    factor = env_factor(station, x, z, universe)
    gen = rated * factor
    fuels = getattr(station, "fuel_types", None)
    if not fuels:
        return gen, None
    eff = float(getattr(station, "efficiency", 1.0))
    max_by_fuel = 0.0
    fuel = getattr(station, "power_fuel", {})
    for fname in fuels:
        count = fuel.get(fname, 0)
        if count <= 0:
            continue
        hv = float(gamedata.FUELS_DATA.get(fname, 0.0) or 0.0)
        if hv <= 0:
            continue
        avail_w = count * hv * eff / max(dt, 1e-6)
        if avail_w > max_by_fuel:
            max_by_fuel = avail_w
    if max_by_fuel <= 0:
        return 0.0, "缺燃料"
    return min(gen, max_by_fuel), None


def consume_station_fuel(station, gen_w, dt) -> dict:
    """按产出扣燃料（按热值降序消耗）。返回实际扣减 dict {fuel:件}。"""
    if gen_w <= 0 or dt <= 0:
        return {}
    fuels = getattr(station, "fuel_types", None) or []
    eff = float(getattr(station, "efficiency", 1.0))
    if eff <= 0:
        return {}
    # 累积燃料需求（焦耳）：按公式 消耗 = 产出_w·dt/(效率·热值)。
    # 用分数累积器跨帧摊销，避免每帧 ceil 抹平到 ≥1 件（BUG-002）。
    acc = float(getattr(station, "_fuel_need_acc", 0.0) or 0.0)
    acc += gen_w * dt / eff
    fuel = getattr(station, "power_fuel", {})
    spent = {}
    for fname in sorted(fuels, key=lambda f: -float(gamedata.FUELS_DATA.get(f, 0.0) or 0.0)):
        if acc <= 0:
            break
        hv = float(gamedata.FUELS_DATA.get(fname, 0.0) or 0.0)
        if hv <= 0:
            continue
        count = fuel.get(fname, 0)
        if count <= 0:
            continue
        per_item = hv  # 每件提供的输入（化学）能量即热值（BUG-011）
        n_use = int(acc // per_item) if per_item > 0 else 0
        n_use = max(0, min(count, n_use))
        if n_use > 0:
            fuel[fname] = count - n_use
            spent[fname] = spent.get(fname, 0) + n_use
            acc -= n_use * per_item
    # 保留不足一件的余量到下一帧（仅当仍有燃料可用时累积）
    if any(c > 0 for c in fuel.values()):
        station._fuel_need_acc = max(0.0, acc)
    else:
        station._fuel_need_acc = 0.0
    for fname in list(fuel):
        if fuel[fname] <= 0:
            del fuel[fname]
    return spent


# ─── 电网求解 ────────────────────────────────────────────────

class PowerState:
    """一次求解的快照结果（每组件统计 + 全局统计）。"""

    def __init__(self):
        self.components = []
        self.total_generation_w = 0.0
        self.total_charge_w = 0.0
        self.total_discharge_w = 0.0
        self.total_loss_w = 0.0
        self.total_waste_w = 0.0
        self.total_deficit_w = 0.0
        self.tripped_count = 0
        self.warnings = []

    def summarize(self) -> dict:
        return {
            "generation_w": self.total_generation_w,
            "charge_w": self.total_charge_w,
            "discharge_w": self.total_discharge_w,
            "loss_w": self.total_loss_w,
            "waste_w": self.total_waste_w,
            "deficit_w": self.total_deficit_w,
            "tripped": self.tripped_count,
        }


def _build_wire_graph(wires):
    """电线连通块（跳过跳闸中的）。返回 (cell→idx, {idx: [(nb_idx, wire)]})。"""
    active_idx = [i for i, w in enumerate(wires) if not w.tripped]
    cell_to_idx = {wires[i].cell: i for i in active_idx}
    adj = {i: [] for i in active_idx}
    for i in active_idx:
        w = wires[i]
        for dx, dz in DXDZ:
            nb_cell = (w.cell[0] + dx, w.cell[1] + dz)
            j = cell_to_idx.get(nb_cell)
            if j is not None:
                adj[i].append((j, w))
    return cell_to_idx, adj


def _connected_components(node_indices, adj):
    seen = set()
    comps = []
    for start in node_indices:
        if start in seen:
            continue
        comp = []
        stack = [start]
        seen.add(start)
        while stack:
            cur = stack.pop()
            comp.append(cur)
            for nb, _w in adj.get(cur, ()):
                if nb not in seen:
                    seen.add(nb)
                    stack.append(nb)
        comps.append(comp)
    return comps


def _spanning_tree(comp, adj):
    parent = {}
    children = {i: [] for i in comp}
    root = comp[0]
    parent[root] = (None, None)
    seen = {root}
    queue = [root]
    while queue:
        cur = queue.pop(0)
        for nb, wire in adj.get(cur, ()):
            if nb not in seen:
                seen.add(nb)
                parent[nb] = (cur, wire)
                children[cur].append(nb)
                queue.append(nb)
    return parent, children


def _component_voltage_class(wire_idxs, wires):
    """组件电压级 = 组件内电线的等级（BUG-132：不再折叠装置等级）。

    只由电线决定组件级。若组件内电线本身多级混接（LV+MV 电线在同一连通块）
    才算真正的电压冲突（警告、不传输）。装置与组件级不匹配由 _solve_component
    逐装置过滤（不挂接），不再因单个错级装置黑掉整个组件。
    """
    classes = set()
    for i in wire_idxs:
        classes.add(wires[i].voltage_class)
    classes.discard(None)
    if len(classes) > 1:
        return None, True
    return (classes.pop() if classes else "LV"), False


class _BatteryGroup:
    """电池组运行时（组内同型、4 邻接）。"""

    __slots__ = ("id", "type", "towers", "analysis", "charge_j",
                 "in_grid", "message", "charge_rate", "discharge_rate",
                 "charge_power_w", "discharge_power_w", "claimed")

    def __init__(self, gid, towers, analysis):
        self.id = gid
        self.type = towers[0].type_name
        self.towers = towers
        self.analysis = analysis
        self.charge_j = sum(float(getattr(t, "charge_j", 0.0)) for t in towers)
        self.in_grid = False
        self.message = None
        self.charge_rate = 0.0
        self.discharge_rate = 0.0
        # 本帧是否已被某个连通块领走结算（BUG-057）。组可能 4 邻接两个不相连
        # 组件，无标记会被两个组件各结算一次，导致 charge_j 同帧双重充/放电。
        # 组对象每帧由 _group_batteries 重建，故无需额外跨帧复位。
        self.claimed = False
        # 预置回写字段：_settle_batteries 会跳过 rate<=0 的组，
        # 不预置会在回写循环读到未赋值槽位而崩溃（BUG-003）。
        self.charge_power_w = 0.0
        self.discharge_power_w = 0.0


def _group_batteries(towers):
    """同型电池 4 邻接成组（BFS）。返回 list[_BatteryGroup]。"""
    bats = [t for t in towers if is_battery(t)]
    if not bats:
        return []
    pos = {}
    for t in bats:
        pos[(math.floor(t.position[0]), math.floor(t.position[2]))] = t
    seen = set()
    groups = []
    for t in bats:
        if t in seen:
            continue
        comp = []
        stack = [t]
        seen.add(t)
        while stack:
            cur = stack.pop()
            comp.append(cur)
            cx, cz = math.floor(cur.position[0]), math.floor(cur.position[2])
            for dx, dz in DXDZ:
                nb = pos.get((cx + dx, cz + dz))
                if nb is not None and nb not in seen and nb.type_name == cur.type_name:
                    seen.add(nb)
                    stack.append(nb)
        analysis = analyze_battery_group(comp)
        groups.append(_BatteryGroup(len(groups), comp, analysis))
    return groups


def solve_power(dt, wires, towers, demand_w=0.0, universe=None):
    """求解电网一帧。返回 PowerState，并回写各塔/各电线字段。"""
    state = PowerState()
    state.demand_w = float(demand_w or 0.0)

    # 复位本帧电力塔状态
    for t in towers:
        if is_station(t):
            t.generation_w = 0.0
            t.in_grid = False
            t.grid_message = None
            t._universe = universe
        elif is_battery(t):
            t.charge_power_w = 0.0
            t.discharge_power_w = 0.0
            t.in_grid = False
            t.grid_message = None
            t.group_id = None
            t.group_kind = None

    if not wires:
        return state

    # 复位本帧电线状态（BUG-056）：非传输组件（电压冲突/无流动早退）的电线
    # 不应残留上一帧的电流与损耗，否则渲染的负载比与损耗会显示陈旧值。
    # 真正参与传输的组件会在 _balance_and_flow 里覆写这些字段。
    for w in wires:
        w.current = 0.0
        w.loss_w = 0.0

    cell_to_idx, adj = _build_wire_graph(wires)
    active_idx = [i for i, w in enumerate(wires) if not w.tripped]
    comps = _connected_components(active_idx, adj)

    tower_by_cell = {}
    for t in towers:
        if is_power_building(t):
            tower_by_cell[(math.floor(t.position[0]), math.floor(t.position[2]))] = t

    battery_groups = _group_batteries(towers)
    for grp in battery_groups:
        for bt in grp.towers:
            bt.group_id = grp.id
            bt.group_kind = grp.analysis["kind"]

    # 电站/装置挂接去重（BUG-012）：一座电站可能 4 邻接两个互不相连的组件，
    # 若每个组件都把它挂进 devices，会发电+烧燃料两次。用 claimed 集合保证
    # 每座电力塔本帧只被一个组件结算（与电池组的 claimed 同理）。
    claimed_devices: set = set()

    # 需求分摊（BUG-058 + BUG-111）：单一全局 demand_w 若原样传给每个组件，
    # N 个组件会各自吃一整份需求，合计 N×demand_w，故必须拆分。但按 1/N 均摊
    # 会让无发电组件静默吞掉份额（不报缺口）、发电组件被 1/N 截断（余量变弃电），
    # 导致总交付 < min(总发电, demand)。改为按各组件实际发电比例分摊：
    #   per_comp_i = demand × (gen_i / total_gen)  （total_gen > 0 时）
    # 使总交付 = min(总发电, demand)；无发电组件份额为 0（其本地需求不被满足）；
    # total_gen == 0 时回落均摊（此时全部需求都转为 deficit，见 _balance_and_flow）。
    n_comp = len(comps)
    demand_total = float(demand_w or 0.0)
    comp_demands = [0.0] * n_comp
    if demand_total > 0 and n_comp:
        # 预扫：按与主循环相同的 BUG-012 去重顺序（tower_by_cell 迭代序，先到先得）
        # 统计每个组件的电站发电，仅用于分摊比例。station_generation_w 无副作用，
        # 主循环会再次调用它做真实结算，故此处重复调用不影响结果。
        # R5-M3：预扫必须与主循环一致地按电压级过滤——否则错级电站被计入
        # comp_gen，组件按虚增发电分得需求份额却无法满足（记 deficit），真正
        # 发电组件份额被压缩（余量变 waste）→ 总交付 < min(总发电, demand)。
        pre_claimed: set = set()
        comp_gen = []
        total_gen = 0.0
        for comp in comps:
            comp_cells = {wires[i].cell for i in comp}
            clevel, cconflict = _component_voltage_class(comp, wires)
            g = 0.0
            if not cconflict:
                for (tx, tz), t in tower_by_cell.items():
                    if id(t) in pre_claimed or not is_station(t):
                        continue
                    dclass = getattr(t, "power_class", None) or "LV"
                    if dclass != clevel:
                        continue
                    if any((tx + dx, tz + dz) in comp_cells for dx, dz in DXDZ):
                        pre_claimed.add(id(t))
                        gw, _msg = station_generation_w(t, dt, universe)
                        g += max(0.0, gw)
            comp_gen.append(g)
            total_gen += g
        for i, g in enumerate(comp_gen):
            if total_gen > 0:
                comp_demands[i] = demand_total * (g / total_gen)
            else:
                comp_demands[i] = demand_total / n_comp

    for ci, comp in enumerate(comps):
        comp_stats = _solve_component(dt, comp, wires, adj, tower_by_cell,
                                      battery_groups, comp_demands[ci], universe, state,
                                      claimed_devices)
        if comp_stats:
            state.components.append(comp_stats)
            state.total_generation_w += comp_stats["generation_w"]
            state.total_charge_w += comp_stats["charge_w"]
            state.total_discharge_w += comp_stats["discharge_w"]
            state.total_loss_w += comp_stats["loss_w"]
            state.total_waste_w += comp_stats["waste_w"]
            state.total_deficit_w += comp_stats["deficit_w"]
            state.tripped_count += comp_stats["tripped"]

    # 跳闸计时与恢复
    for w in wires:
        if w.tripped:
            w.trip_timer -= dt
            if w.trip_timer <= 0:
                w.tripped = False
                w.trip_timer = 0.0

    # 未接入电网标记（回写 UI）
    for t in towers:
        if is_power_building(t) and not getattr(t, "in_grid", False):
            t.grid_message = t.grid_message or "未接入电网"

    return state


def _solve_component(dt, comp, wires, adj, tower_by_cell, battery_groups,
                     demand_w, universe, state, claimed_devices=()):
    comp_wires = [wires[i] for i in comp]
    comp_cells = {wires[i].cell for i in comp}

    level, conflict = _component_voltage_class(comp, wires)
    v = voltage_nominal(level)
    comp_stats = {
        "wires": comp_wires, "level": level, "voltage_v": v,
        "generation_w": 0.0, "charge_w": 0.0, "discharge_w": 0.0,
        "loss_w": 0.0, "waste_w": 0.0, "deficit_w": 0.0, "tripped": 0,
    }
    if conflict:
        # 真正的多级电线混接（LV+MV 电线在同一连通块）→ 整组件冲突、不传输。
        # 邻接装置仍占用本轮 claim（与原实现一致），整组标记冲突，防止双发电。
        for (tx, tz), t in tower_by_cell.items():
            if id(t) in claimed_devices:
                continue
            for dx, dz in DXDZ:
                if (tx + dx, tz + dz) in comp_cells:
                    claimed_devices.add(id(t))
                    t.in_grid = False
                    t.grid_message = "电压冲突"
                    break
        comp_stats["conflict"] = True
        state.warnings.append("电压级混接：组件含多级电线，未传输")
        return comp_stats

    # 挂接装置：电力塔 4 邻接本组件任意电线格。claimed_devices 去重
    # （BUG-012）：跨两个不相连组件的电站只被首个组件结算，避免双发电+双烧
    # 燃料。R1-C1：claim 推迟到电压级匹配确认之后——原实现挂接阶段即 claim，
    # 电压过滤（BUG-132）在其后且不释放：电站 4 邻接两个互不相连、电压级不同
    # 的组件且先处理的组件与其不匹配时，先处理组件 claim 它并判"电压级不匹
    # 配"，真正匹配的组件因 id(t) in claimed_devices 永久跳过 → 电站整帧不
    # 发电（搁浅）。与电池组 BUG-133"claim 推迟到确认结算之后"对齐。
    devices = []
    for (tx, tz), t in tower_by_cell.items():
        if id(t) in claimed_devices:
            continue
        for dx, dz in DXDZ:
            if (tx + dx, tz + dz) in comp_cells:
                devices.append(t)
                break

    # BUG-132：装置电压级与组件级（由电线决定）不匹配 → 仅该装置不挂接
    # （与下方电池组挂接处的逐组跳过同理），组件继续用匹配的装置运行，
    # 不再因单个错级装置黑掉整个组件（设计文档 L14-15）。
    matched_devices = []
    for t in devices:
        dclass = getattr(t, "power_class", None) or "LV"
        if dclass != level:
            t.in_grid = False
            t.grid_message = "电压级不匹配"
        else:
            claimed_devices.add(id(t))
            matched_devices.append(t)
    devices = matched_devices

    # 电站出力
    gen_list = []  # [(tower, gen_w)]
    for t in devices:
        if is_station(t):
            g, msg = station_generation_w(t, dt, universe)
            if g > 0:
                gen_list.append((t, g))
            else:
                t.in_grid = False
                t.grid_message = msg or "无出力"

    # 电池组挂接（组压与组件级匹配）
    bat_groups = []  # [_BatteryGroup]
    for grp in battery_groups:
        # 已被另一连通块领走结算的组不再处理（BUG-057）：
        # 组可能 4 邻接两个不相连组件，若都挂接会同帧结算两次。
        if grp.claimed:
            continue
        ana = grp.analysis
        if ana["kind"] == "invalid":
            for bt in grp.towers:
                bt.in_grid = False
                bt.grid_message = ana["warning"] or "接法非法"
            continue
        if _battery_group_class(ana["v"]) != level:
            for bt in grp.towers:
                bt.in_grid = False
                bt.grid_message = "组压与电网电压级不匹配"
            continue
        # 组需与组件邻接（组内任一塔 4 邻接本组件电线格）
        attached = False
        for bt in grp.towers:
            bx, bz = math.floor(bt.position[0]), math.floor(bt.position[2])
            for dx, dz in DXDZ:
                if (bx + dx, bz + dz) in comp_cells:
                    attached = True
                    break
            if attached:
                break
        if not attached:
            continue
        # 组当前储能
        grp.charge_j = sum(float(getattr(bt, "charge_j", 0.0)) for bt in grp.towers)
        ana = grp.analysis
        vg, imax, cap = ana["v"], ana["i_max"], ana["energy_j"]
        max_power = vg * imax
        grp.charge_rate = max(0.0, min(max_power, (cap - grp.charge_j) / max(dt, 1e-6))) \
            if grp.charge_j < cap else 0.0
        # 放电率上限含往返效率因子（R5-M1）：要交付 P 需从储能扣 P/eff，故
        # 能量受限时最多只能交付 charge_j·eff（而非 charge_j）。否则每个放空
        # 末帧按 charge_j 全额入账交付、实际只耗出 charge_j 储能——按效率模型
        # 只应交付 charge_j×0.92，凭空多交付约 8%。
        grp.discharge_rate = max(
            0.0, min(max_power,
                     grp.charge_j * config.POWER_BATTERY_EFF / max(dt, 1e-6))) \
            if grp.charge_j > 0 else 0.0
        bat_groups.append(grp)
        # 注意：此处不再立即 grp.claimed = True（BUG-133）。claim 推迟到确认本
        # 组件会真正结算（通过下方 gen/demand 早退）之后，避免无站无需求的死支路
        # 先领走电池组又遗弃，导致活组件因 grp.claimed 永久跳过、电池永不充放。

    gen_total = sum(g for _t, g in gen_list)
    demand = float(demand_w or 0.0)

    if gen_total <= 0 and demand <= 0:
        for t in devices:
            t.in_grid = False
            # 已有具体原因（如燃料耗尽的“缺燃料”）的装置不被通用消息覆盖（BUG-059）
            t.grid_message = t.grid_message or "无功率流动"
        return comp_stats

    # BUG-133：只有真正会结算的组件才 claim 电池组（放在早退之后）。
    # 仍保证 BUG-057：组可能 4 邻接两个不相连组件，仅首个会结算的组件领走，
    # 另一组件见 grp.claimed 跳过，不会同帧双重充/放电。死支路在上方早退、不 claim。
    for grp in bat_groups:
        grp.claimed = True

    # 电池角色决策：源≥需求 → 充电；否则放电补缺
    if gen_total >= demand:
        for grp in bat_groups:
            grp.in_grid = True
            grp.message = "充电中"
        _balance_and_flow(dt, comp, wires, adj, gen_list, bat_groups,
                          demand, charge=True, v=v, comp_stats=comp_stats,
                          state=state)
    else:
        for grp in bat_groups:
            grp.in_grid = True
            grp.message = "放电中"
        _balance_and_flow(dt, comp, wires, adj, gen_list, bat_groups,
                          demand, charge=False, v=v, comp_stats=comp_stats,
                          state=state)

    return comp_stats


def _balance_and_flow(dt, comp, wires, adj, gen_list, bat_groups, demand,
                      charge, v, comp_stats, state):
    """功率平衡 + 生成树流 + 损耗 + 跳闸 + 电池结算。

    源/汇分开累计（inj_src / inj_sink），再用源使用系数 scale 与汇满足系数
    sink_scale 各自缩放：
      - scale      = min(1, 总汇/总源)   → 源侧(电站/放电电池) 节流系数
      - sink_scale = min(1, 总源/总汇)   → 汇侧(充电电池/demand) 满足系数
    这样电站与电池共挂同一节点时，节点净注入可抵消（就地传递不经电线），
    但 gross 值仍决定总缩放，树枝流动由净注入决定。
    """
    cell_node = {wires[i].cell: i for i in comp}
    inj_src = {i: 0.0 for i in comp}
    inj_sink = {i: 0.0 for i in comp}
    gross_src = 0.0
    gross_sink = 0.0

    def put(t, power, is_src):
        """把装置功率注入其邻接电线节点（src/sink 分开）。返回是否注入成功。

        设计约定：每个装置在其所属组件内只接入单一节点（BUG-014）。
        装置可能 4 邻接多个本组件电线格，若逐格累加会把同一份功率计入 k 次，
        而 gross_src/gross_sink 只计一次，导致电流/损耗虚高、误跳闸。
        故只选一个确定性的主接入格（节点序号最小）注入。
        """
        tx, tz = math.floor(t.position[0]), math.floor(t.position[2])
        dst = inj_src if is_src else inj_sink
        hit = [i for dx, dz in DXDZ
               if (i := cell_node.get((tx + dx, tz + dz))) is not None]
        if hit:
            dst[min(hit)] += power
            return True
        return False

    def inject_group_share(members, rate, is_src):
        """把一份组功率注入组件，保证总量守恒（BUG-013 副作用修正）。

        组挂接只要求"组内任一塔邻接本组件"，串链电池的中段成员四面都是电池、
        不邻接任何电线格。若对不邻接成员直接跳过注入（put 返回 False），
        它的份额 rate/n 就既不注入也不报错，而 gross 仍按整组 rate 计 → 功率凭空
        消失（能量不守恒）。故：先注入各邻接成员的自身份额，再把不邻接成员的
        份额按比例摊给同组内"可注入"的成员，使实际注入总量恰等于 gross 计的 rate。
        """
        n = max(1, len(members))
        share = rate / n
        orphan = 0.0
        donors = []
        for t in members:
            if put(t, share, is_src):
                donors.append(t)
            else:
                orphan += share
        if orphan > 0 and donors:
            per = orphan / len(donors)
            for t in donors:
                # 摊给已注入的成员（它们在本组件内确有接入节点）
                dst = inj_src if is_src else inj_sink
                tx, tz = math.floor(t.position[0]), math.floor(t.position[2])
                hit = [i for dx, dz in DXDZ
                       if (i := cell_node.get((tx + dx, tz + dz))) is not None]
                if hit:
                    dst[min(hit)] += per

    # 源：电站（电站恒邻接其组件，直接注入）
    for t, g in gen_list:
        put(t, g, True)
        gross_src += g
    # 汇/源：电池（charge=汇，discharge=源）
    for grp in bat_groups:
        rate = grp.charge_rate if charge else grp.discharge_rate
        inject_group_share(grp.towers, rate, not charge)
        if charge:
            gross_sink += rate
        else:
            gross_src += rate
    # demand：汇（平均摊到各节点）
    if demand > 0 and comp:
        per = demand / len(comp)
        for i in comp:
            inj_sink[i] += per
        gross_sink += demand

    if gross_src <= 0 or gross_sink <= 0:
        for t, _g in gen_list:
            t.in_grid = False
            t.grid_message = "无功率流动"
        for grp in bat_groups:
            grp.in_grid = False
            grp.message = "无功率流动"
        # 仍要回写电池塔显示字段（BUG-060），否则已接入但闲置的电池
        # 保持每帧复位值 in_grid=False，被 solve_power 标为“未接入电网”。
        for grp in bat_groups:
            for bt in grp.towers:
                bt.in_grid = grp.in_grid
                bt.grid_message = grp.message
                bt.charge_power_w = grp.charge_power_w
                bt.discharge_power_w = grp.discharge_power_w
        # BUG-111：源为 0 而汇（demand/充电电池）> 0 时，本组件的需求无法满足，
        # 必须记入 deficit_w，避免静默丢失（交付 < 需求却不报缺口）。
        # 典型触发：total_gen == 0 时按均摊分到需求的组件（无任何发电）。
        if gross_src <= 0 and gross_sink > 0:
            comp_stats["deficit_w"] += gross_sink
        return
    scale = min(1.0, gross_sink / gross_src)      # 源使用系数
    sink_scale = min(1.0, gross_src / gross_sink)  # 汇满足系数
    if gross_src >= gross_sink:
        comp_stats["waste_w"] = gross_src - gross_sink
    else:
        comp_stats["deficit_w"] = gross_sink - gross_src

    inj = {i: inj_src[i] * scale - inj_sink[i] * sink_scale for i in comp}
    parent, children = _spanning_tree(comp, adj)
    subtree = {i: inj[i] for i in comp}
    for node in _postorder(comp, children):
        for child in children[node]:
            subtree[node] += subtree[child]

    loss_total = 0.0
    edge_flow = {}  # node -> 流入该节点的边流量（BUG-015）
    for node in comp:
        p, _wire = parent[node]
        if p is None:
            continue
        edge_flow[node] = abs(subtree[node])

    # 每格电线承载“流入本格”的电流（含经本格流向其所有子树的总流量）。
    # 旧实现写到父格电线，导致多子节点的分支线被最后一个子边覆写（BUG-015）。
    for node in comp:
        if node in edge_flow:
            flow = edge_flow[node]
        elif parent[node][0] is None:
            # 根：无流入边。真实净流 = |Σ 子边净流|（R5-M4）：守恒使子树净流
            # 在根交汇时符号相反（源支 +、汇支 −），绝对值之和会高估（极端
            # 2×），损耗 4× 并可误跳闸。old 实现用 Σ|subtree[child]|。
            flow = abs(sum(subtree[c] for c in children[node]))
        else:
            continue
        wire = wires[node]
        i_a = flow / max(v, 1e-6)
        wire.current = i_a
        wire.loss_w = i_a * i_a * wire.resistance
        loss_total += wire.loss_w
        if wire.is_overloaded(i_a):
            wire.tripped = True
            wire.trip_timer = config.POWER_TRIP_TIME
            comp_stats["tripped"] += 1

    delivered = gross_src * scale
    delivered = max(0.0, delivered - loss_total)
    # 发电统计只计电站出力，电池放电单独由 discharge_w 报告（BUG-061）
    station_gen = sum(g for _t, g in gen_list)
    comp_stats["generation_w"] = station_gen * scale
    comp_stats["loss_w"] = loss_total

    # 分配：先满足 demand（按汇侧满足系数），余量给电池
    # （R8-Q3：gross_src<gross_sink 时物理流已按 sink_scale 同比例压缩全部
    # 汇——demand 与充电电池；结算若"需求优先"会让电池吃剩余并报满供，
    # 与物理流口径不一致。按 demand×sink_scale 分配使两者一致。游戏内
    # demand_w 恒为 0 走不到，属潜伏路径，check_power.py 覆盖。）
    if demand > 0:
        given_demand = min(delivered, demand * sink_scale)
        comp_stats["demand_delivered"] = given_demand
        charge_pool = max(0.0, delivered - given_demand)
    else:
        charge_pool = delivered

    # 结算电池（按组容量比例分配 charge_pool 或放电补缺）
    _settle_batteries(dt, bat_groups, charge_pool, charge, scale,
                      sink_scale, comp_stats, state)

    # 发电结算（扣燃料 + 回写）
    for t, g in gen_list:
        actual = g * scale
        t.generation_w = actual
        consume_station_fuel(t, actual, dt)
        t.in_grid = True
        t.grid_message = "发电中"

    # 回写电池塔显示字段
    for grp in bat_groups:
        for bt in grp.towers:
            bt.in_grid = grp.in_grid
            bt.grid_message = grp.message
            bt.charge_power_w = grp.charge_power_w
            bt.discharge_power_w = grp.discharge_power_w


def _postorder(comp, children):
    order = []
    seen = set()

    def dfs(n):
        seen.add(n)
        for c in children[n]:
            if c not in seen:
                dfs(c)
        order.append(n)

    for n in comp:
        if n not in seen:
            dfs(n)
    return order


def _settle_batteries(dt, bat_groups, charge_pool, charge, scale, sink_scale,
                      comp_stats, state):
    """结算电池储能。

    charge=True：把 charge_pool（满足 demand 后的余量）按组充电上限比例分配给
    各组（汇侧；sink_scale 已体现在 charge_pool 中）。
    charge=False：各组放电（源侧；按 scale 节流）。
    """
    if charge:
        total_rate = sum(g.charge_rate for g in bat_groups)
        if total_rate <= 0:
            return
        # 组按容量比例分 charge_pool（≤各组 charge_rate）
        for grp in bat_groups:
            if grp.charge_rate <= 0:
                continue
            share = charge_pool * (grp.charge_rate / total_rate)
            share = min(share, grp.charge_rate)
            eff = config.POWER_BATTERY_EFF
            gain = share * dt * eff
            _add_group_energy(grp, gain)
            grp.charge_power_w = share
            grp.discharge_power_w = 0.0
        comp_stats["charge_w"] = charge_pool
    else:
        for grp in bat_groups:
            if grp.discharge_rate <= 0:
                continue
            share = grp.discharge_rate * scale
            # 从储能取（交付能量，往返效率在储能侧按 1/eff 反向扣除）
            draw = share * dt / max(config.POWER_BATTERY_EFF, 1e-6)
            _draw_group_energy(grp, draw)
            grp.discharge_power_w = share
            grp.charge_power_w = 0.0
        comp_stats["discharge_w"] = sum(g.discharge_rate * scale for g in bat_groups)


def _add_group_energy(grp, gain_j):
    """给组加储能（按各塔 capacity 比例分摊）。"""
    ana = grp.analysis
    cap = ana["energy_j"]
    grp.charge_j = min(cap, grp.charge_j + gain_j)
    n = len(grp.towers)
    # 均分到各塔（单体同型同容）
    for bt in grp.towers:
        bt.charge_j = grp.charge_j / max(1, n)


def _draw_group_energy(grp, draw_j):
    """从组扣除储能（交付能量，按往返效率反向扣除）。"""
    grp.charge_j = max(0.0, grp.charge_j - draw_j)
    n = len(grp.towers)
    for bt in grp.towers:
        bt.charge_j = grp.charge_j / max(1, n)
