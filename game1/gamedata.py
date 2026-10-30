"""
gamedata.py - 数据加载器：游戏启动时从 data/*.json 读取全部配置数据。

设计原则：
  - 只依赖标准库（json/os），不 import 任何游戏模块 → 无循环依赖。
  - 所有数值的“唯一事实来源”在 data/*.json；本模块做结构校验与默认值归一。
  - 上层模块（config/items/entities/game/world）从这里取数据并构建运行时对象。

数据文件：
  tech.json      科技树 + 科技效果 + 说明          → TECH_TREE / TECH_EFFECTS / TECH_DESC
  recipes.json   材料/加工配方/工具/武器/拆除比例   → PROCESS_RECIPES / TOOLS_RAW / WEAPONS_RAW /
                                                     MATERIAL_* / RAW / PROCESSED / CLASSIC_TOWER_MATERIALS / DISMANTLE_REFUND
  enemies.json   敌人类型 + 掉落 + 全局 + 地形速度  → ENEMIES_RAW / DROP_TABLES / ENEMY_GLOBAL / TERRAIN_SPEED_MULTS / ENEMY_COLORS / ENEMY_PERSONALITIES
  spawners.json  刷怪点分布 + 种类 + 侦察兵属性     → SPAWNERS_CFG / SPAWNER_GRID / spawner_kind() / spawner_kind_for() / STANDARD_SCOUT
  waves.json     波次构成表                        → WAVE_TABLE / WAVE_FALLBACK
  towers.json    塔类型                            → TOWERS_RAW / TOWER_COLORS

改数值/加内容：编辑对应 JSON 即可，无需改 Python。
"""

import json
import os

# 数据目录 = 本文件同级的 data/
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def _load(name):
    path = os.path.join(DATA_DIR, name)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _color(c):
    """JSON 颜色 [r,g,b] 或 [r,g,b,a] → tuple。"""
    return tuple(c)


# ─── 原始数据文件 ─────────────────────────────────────────────
# 注：tech.json 在下方"科技"段按 v1/v2 双格式单独加载，此处不重复解析。
RECIPES_DATA = _load("recipes.json")
ENEMIES_DATA = _load("enemies.json")
SPAWNERS_DATA = _load("spawners.json")
WAVES_DATA = _load("waves.json")
TOWERS_DATA = _load("towers.json")


# ─── 科技 ─────────────────────────────────────────────────────
# v2 格式：tech.json 包含 eras/categories/achievements/breakthroughs/legacy_map
# 向后兼容：TECH_TREE 仍为扁平 dict，legacy ID → 新突破点数据
TECH_DATA = _load("tech.json")
if TECH_DATA.get("version", 1) >= 2:
    # v2 格式
    TECH_ERAS = TECH_DATA["eras"]
    TECH_CATEGORIES = TECH_DATA["categories"]
    TECH_ACHIEVEMENTS = TECH_DATA["achievements"]
    TECH_BREAKTHROUGHS = TECH_DATA["breakthroughs"]
    TECH_LEGACY_MAP = TECH_DATA.get("legacy_map", {})
    TECH_EFFECTS = TECH_DATA.get("effects", {})

    # 构建向后兼容的 TECH_TREE：legacy ID → breakthrough data
    TECH_TREE = {}
    for legacy_id, new_id in TECH_LEGACY_MAP.items():
        if new_id in TECH_BREAKTHROUGHS:
            bt = TECH_BREAKTHROUGHS[new_id]
            TECH_TREE[legacy_id] = {
                "cost": bt["cost"],
                "time": bt["time"],
                "requires": [],  # legacy techs have no cross-requires in the flat view
                "effect": bt.get("effect"),
                "desc": bt["name_cn"],
                "name_cn": bt["name_cn"],
                "era": bt["era"],
                "track": bt["track"],
                "achievement": bt.get("achievement"),
            }
else:
    # v1 格式（旧版）
    TECH_TREE = TECH_DATA["tech_tree"]
    TECH_EFFECTS = TECH_DATA.get("effects", {})
    TECH_ERAS = []
    TECH_CATEGORIES = {}
    TECH_ACHIEVEMENTS = {}
    TECH_BREAKTHROUGHS = {}
    TECH_LEGACY_MAP = {}

TECH_DESC = {name: td.get("desc", td.get("name_cn", "")) for name, td in TECH_TREE.items()}

# 制作门控的唯一真源：突破点 ID 集合。requires_tech 只能引用这里面的 ID，
# 否则该门控永不可达（历史上 legacy ID 与新树并存时静默失效过）。
TECH_IDS = set(TECH_BREAKTHROUGHS)

# effect 键 → 带该效果的突破点 ID 列表（供 _has_effect_tech 泛化查找）。
TECH_EFFECT_NODES = {}
for _tid, _td in TECH_BREAKTHROUGHS.items():
    _eff = _td.get("effect")
    if _eff:
        TECH_EFFECT_NODES.setdefault(_eff, []).append(_tid)


# ─── 配方 / 物品 ──────────────────────────────────────────────
PROCESS_RECIPES = RECIPES_DATA["processing"]      # key → {in,out,hand,tool,time_hand,time_tool,requires_tool,requires_tech}
TOOLS_RAW = RECIPES_DATA.get("tools", {})         # name → {label_cn,recipe,applies,color,requires_tech}
WEAPONS_RAW = RECIPES_DATA.get("weapons", {})     # name → {label_cn,recipe,damage,range,cooldown,install_fee,special,color,requires_tech}
DISMANTLE_REFUND = RECIPES_DATA.get("dismantle_refund", 0.5)

MATERIALS_DATA = RECIPES_DATA.get("materials", {})  # name → {class,color,name_cn}
MATERIAL_COLORS = {name: _color(m["color"]) for name, m in MATERIALS_DATA.items()}
MATERIAL_NAMES_CN = {name: m.get("name_cn", name) for name, m in MATERIALS_DATA.items()}
RAW = {name for name, m in MATERIALS_DATA.items() if m.get("class") == "raw"}
PROCESSED = {name for name, m in MATERIALS_DATA.items() if m.get("class") == "processed"}


# ─── 敌人 ─────────────────────────────────────────────────────
ENEMIES_RAW = ENEMIES_DATA["types"]   # name → {base_health,base_speed,base_reward,color,personality,drops}
ENEMY_GLOBAL = ENEMIES_DATA.get("global", {})
TERRAIN_SPEED_MULTS = ENEMIES_DATA.get("terrain_speed", {})
ENEMY_COLORS = {name: _color(ed["color"]) for name, ed in ENEMIES_RAW.items()}
ENEMY_PERSONALITIES = {
    name: dict(ed.get("personality", {})) for name, ed in ENEMIES_RAW.items()}
ENEMY_ENVIRONMENTS = {
    name: dict(ed.get("environment", {})) for name, ed in ENEMIES_RAW.items()}
ENEMY_ATTACKS = {
    name: dict(ed.get("attack", {})) for name, ed in ENEMIES_RAW.items()}
# 掉落表：{敌人类型: [(材料, 数量), ...]}
DROP_TABLES = {
    name: [tuple(d) for d in ed.get("drops", [["wood", 1]])]
    for name, ed in ENEMIES_RAW.items()}


# ─── 塔 ───────────────────────────────────────────────────────
TOWERS_RAW = TOWERS_DATA["towers"]     # name → 全部塔属性
TOWER_COLORS = {name: _color(td["color"]) for name, td in TOWERS_RAW.items()}
TOWER_HP = {name: td.get("base_hp", 100) for name, td in TOWERS_RAW.items()}
# 经典塔拆除回收材料（从 towers.json 的 materials 字段派生）
CLASSIC_TOWER_MATERIALS = {
    name: dict(td.get("materials", {})) for name, td in TOWERS_RAW.items()}


# ─── 刷怪点 / 侦察兵 ──────────────────────────────────────────
SPAWNERS_CFG = SPAWNERS_DATA
DISTRIBUTION_CFG = SPAWNERS_DATA["distribution"]
SPAWNER_GRID = DISTRIBUTION_CFG["grid"]   # 兼容别名（save_system 等使用）


def spawner_kind(kind: str) -> dict:
    """按名字取刷怪点种类配置；未知种类回落 standard。"""
    kinds = SPAWNERS_CFG.get("kinds", {})
    return kinds.get(kind, kinds.get("standard", {"trigger_radii": {}, "scout": {}}))


def spawner_kind_for(biome: str) -> str:
    """生物群系 → 刷怪点种类名（第一个覆盖该群系的种类，无匹配回落 standard）。"""
    for kname, kdef in SPAWNERS_CFG.get("kinds", {}).items():
        if biome in kdef.get("biomes", []):
            return kname
    return "standard"


# 标准种类侦察兵配置（无种类信息时的兜底，如读取存档中的侦察怪）
STANDARD_SCOUT = spawner_kind("standard")["scout"]


def scout_cfg_of(scout) -> dict:
    """返回敌人身上的侦察兵配置（读取存档的侦察怪用标准配置兜底）。"""
    cfg = getattr(scout, "scout_cfg", None)
    return cfg or STANDARD_SCOUT


# ─── 波次 ─────────────────────────────────────────────────────
WAVE_FALLBACK = WAVES_DATA["fallback"]
WAVE_TABLE = {
    int(row["wave"]): {
        "count": row.get("count"),
        "enemy_types": list(row.get("enemy_types") or []),
    }
    for row in WAVES_DATA.get("waves", [])}


def wave_def(wave_num: int) -> dict:
    """第 wave_num 波的构成：显式表优先，缺字段回落到 fallback。"""
    row = WAVE_TABLE.get(int(wave_num), {})
    f = WAVE_FALLBACK
    count = row.get("count")
    if count is None:
        count = f["base_count"] + wave_num * f["count_per_wave"]
    types = row.get("enemy_types") or f["enemy_types_default"]
    return {"count": count, "enemy_types": list(types)}


# ─── 数据合理性校验 ─────────────────────────────────────────
# 导入时全量校验：前置引用存在性、数值范围、表间一致性。
# 任何错误都会在启动时抛 ValueError 并列出全部问题（fail fast，
# 避免"写错科技名/材料名导致门控或配方静默失效"这类难排查的问题）。

def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _valid_color(c):
    if not isinstance(c, (list, tuple)) or len(c) not in (3, 4):
        return False
    return all(isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 255
               for v in c)


def _collect_data_issues():
    """返回 (errors, warnings)。errors 会阻止启动；warnings 仅提示。"""
    errs, warns = [], []
    bad = errs.append
    warn = warns.append

    # ── 科技 ──
    tech_names = set(TECH_TREE)
    for name, td in TECH_TREE.items():
        if not isinstance(td.get("cost"), (int, float)) or td["cost"] < 0:
            bad(f"tech[{name}].cost 非法: {td.get('cost')!r}")
        if not isinstance(td.get("time"), (int, float)) or td["time"] <= 0:
            bad(f"tech[{name}].time 非法: {td.get('time')!r}")
        for req in td.get("requires", []):
            if req not in tech_names:
                bad(f"tech[{name}].requires 引用了不存在的科技: {req!r}")
        eff = td.get("effect")
        if eff is not None and eff not in TECH_EFFECTS:
            bad(f"tech[{name}].effect 引用了未定义的效果键: {eff!r}")
    # 前置环检测（DFS）
    state = {n: 0 for n in TECH_TREE}  # 0=未访问 1=访问中 2=完成

    def dfs(n, path):
        state[n] = 1
        for r in TECH_TREE[n].get("requires", []):
            if r not in TECH_TREE:
                continue  # 已在上方报错
            if state[r] == 1:
                i = path.index(r) if r in path else 0
                bad("科技前置存在环: " + " -> ".join(path[i:] + [r]))
            elif state[r] == 0:
                dfs(r, path + [r])
        state[n] = 2
    for n in TECH_TREE:
        if state[n] == 0:
            dfs(n, [n])

    # 突破点前置环检测（迭代式，避免深链触发递归上限）：
    # 环上的节点永远研究不出来，依赖它们的 requires_tech 门控也就永不可达。
    bstate = {tid: 0 for tid in TECH_BREAKTHROUGHS}  # 0=未访问 1=访问中 2=完成
    for start in TECH_BREAKTHROUGHS:
        if bstate[start] != 0:
            continue
        bstate[start] = 1
        path = [start]
        stack = [(start, iter(TECH_BREAKTHROUGHS[start].get("requires", [])))]
        while stack:
            node, it = stack[-1]
            nxt = next(it, None)
            if nxt is None:
                bstate[node] = 2
                stack.pop()
                path.pop()
                continue
            if nxt not in TECH_BREAKTHROUGHS:
                continue  # 未知引用由下方可达性检查报错
            s = bstate[nxt]
            if s == 1:
                i = path.index(nxt) if nxt in path else 0
                bad("突破点前置存在环: " + " -> ".join(path[i:] + [nxt]))
            elif s == 0:
                bstate[nxt] = 1
                path.append(nxt)
                stack.append((nxt, iter(TECH_BREAKTHROUGHS[nxt].get("requires", []))))

    # ── 材料 / 配方 / 工具 / 武器 ──
    mat_names = set(MATERIALS_DATA)
    for mname, md in MATERIALS_DATA.items():
        if md.get("class") not in ("raw", "processed"):
            bad(f"material[{mname}].class 非法: {md.get('class')!r}")
        if not _valid_color(md.get("color")):
            bad(f"material[{mname}].color 非法: {md.get('color')!r}")
    for key, rp in PROCESS_RECIPES.items():
        if rp.get("in") not in mat_names:
            bad(f"recipe[{key}].in 引用了不存在的材料: {rp.get('in')!r}")
        if rp.get("out") not in mat_names:
            bad(f"recipe[{key}].out 引用了不存在的材料: {rp.get('out')!r}")
        if not _is_int(rp.get("hand")) or rp["hand"] < 0:
            bad(f"recipe[{key}].hand 非法: {rp.get('hand')!r}")
        if not _is_int(rp.get("tool")) or rp["tool"] < 1:
            bad(f"recipe[{key}].tool 非法: {rp.get('tool')!r}")
        if rp.get("hand") > 0 and not isinstance(rp.get("time_hand"), (int, float)):
            bad(f"recipe[{key}].time_hand 非法（hand>0 时必须有）: {rp.get('time_hand')!r}")
        if not isinstance(rp.get("time_tool"), (int, float)):
            bad(f"recipe[{key}].time_tool 非法: {rp.get('time_tool')!r}")
        req_tool = rp.get("requires_tool")
        if req_tool is not None and req_tool not in TOOLS_RAW:
            bad(f"recipe[{key}].requires_tool 引用了不存在的工具: {req_tool!r}")
        for t in rp.get("requires_tech", []) or []:
            if t in TECH_IDS:
                continue
            if t in tech_names:
                warn(f"recipe[{key}].requires_tech 仍用 legacy ID {t!r}，应迁移为对应的新突破点 ID")
            else:
                bad(f"recipe[{key}].requires_tech 引用了不存在的科技: {t!r}")
    for tname, tdef in TOOLS_RAW.items():
        if tdef.get("applies") not in PROCESS_RECIPES:
            bad(f"tool[{tname}].applies 引用了不存在的加工配方: {tdef.get('applies')!r}")
        for m, a in tdef.get("recipe", {}).items():
            if m not in mat_names:
                bad(f"tool[{tname}].recipe 引用了不存在的材料: {m!r}")
            if not _is_int(a) or a < 1:
                bad(f"tool[{tname}].recipe[{m}] 数量非法: {a!r}")
        for t in tdef.get("requires_tech", []) or []:
            if t in TECH_IDS:
                continue
            if t in tech_names:
                warn(f"tool[{tname}].requires_tech 仍用 legacy ID {t!r}，应迁移为对应的新突破点 ID")
            else:
                bad(f"tool[{tname}].requires_tech 引用了不存在的科技: {t!r}")
    for wname, wdef in WEAPONS_RAW.items():
        for m, a in wdef.get("recipe", {}).items():
            if m not in mat_names:
                bad(f"weapon[{wname}].recipe 引用了不存在的材料: {m!r}")
            if not _is_int(a) or a < 1:
                bad(f"weapon[{wname}].recipe[{m}] 数量非法: {a!r}")
        for f in ("damage", "range", "cooldown"):
            if not isinstance(wdef.get(f), (int, float)) or wdef[f] <= 0:
                bad(f"weapon[{wname}].{f} 非法: {wdef.get(f)!r}")
        if not isinstance(wdef.get("install_fee"), (int, float)) or wdef["install_fee"] < 0:
            bad(f"weapon[{wname}].install_fee 非法: {wdef.get('install_fee')!r}")
        if not _valid_color(wdef.get("color")):
            bad(f"weapon[{wname}].color 非法: {wdef.get('color')!r}")
        for t in wdef.get("requires_tech", []) or []:
            if t in TECH_IDS:
                continue
            if t in tech_names:
                warn(f"weapon[{wname}].requires_tech 仍用 legacy ID {t!r}，应迁移为对应的新突破点 ID")
            else:
                bad(f"weapon[{wname}].requires_tech 引用了不存在的科技: {t!r}")
        bv = wdef.get("special", {}).get("bonus_vs")
        if bv is not None and bv not in ENEMIES_RAW:
            bad(f"weapon[{wname}].special.bonus_vs 引用了不存在的敌人类型: {bv!r}")

    # ── 敌人 ──
    enemy_names = set(ENEMIES_RAW)
    # 群系/方块名注册表（blocks/biomes 为纯数据模块，无循环依赖）
    from blocks import BLOCK_NAME
    from biomes import BIOMES as _BIOME_NAMES
    block_names = set(BLOCK_NAME.values())
    for ename, ed in ENEMIES_RAW.items():
        for f in ("base_health", "base_speed", "base_reward"):
            if not isinstance(ed.get(f), (int, float)) or ed[f] <= 0:
                bad(f"enemy[{ename}].{f} 非法: {ed.get(f)!r}")
        if not _valid_color(ed.get("color")):
            bad(f"enemy[{ename}].color 非法: {ed.get('color')!r}")
        pers = ed.get("personality") or {}
        for f in ("direct", "terrain_aware", "aggressive", "wobble"):
            if f not in pers:
                bad(f"enemy[{ename}].personality 缺少键 {f}")
        # 寻路偏好（有默认值，缺失不报错，值非法才报错）
        if "climb_limit" in pers and (not isinstance(pers["climb_limit"], (int, float)) or pers["climb_limit"] <= 0):
            bad(f"enemy[{ename}].personality.climb_limit 非法: {pers['climb_limit']!r}")
        if "dig_speed" in pers and (not isinstance(pers["dig_speed"], (int, float)) or pers["dig_speed"] < 0):
            bad(f"enemy[{ename}].personality.dig_speed 非法: {pers['dig_speed']!r}")
        for f in ("crowd_avoidance", "tower_hostility"):
            if f in pers and (not isinstance(pers[f], (int, float)) or not 0.0 <= pers[f] <= 1.0):
                bad(f"enemy[{ename}].personality.{f} 应在 [0,1]: {pers[f]!r}")
        if "detour_tolerance" in pers and (not isinstance(pers["detour_tolerance"], (int, float)) or pers["detour_tolerance"] < 1.0):
            bad(f"enemy[{ename}].personality.detour_tolerance 应 ≥1.0: {pers['detour_tolerance']!r}")
        env = ed.get("environment") or {}
        ws = env.get("weather_speed") or {}
        for f in ("rain", "snow"):
            if f in ws and (not isinstance(ws[f], (int, float)) or not 0.0 < ws[f] <= 2.0):
                bad(f"enemy[{ename}].environment.weather_speed.{f} 应在 (0,2]: {ws[f]!r}")
        for b, mult in (env.get("terrain_speed") or {}).items():
            if b not in _BIOME_NAMES:
                bad(f"enemy[{ename}].environment.terrain_speed 引用了不存在的群系: {b!r}")
            if not isinstance(mult, (int, float)) or mult <= 0:
                bad(f"enemy[{ename}].environment.terrain_speed[{b}] 非法: {mult!r}")
        for blk, mult in (env.get("surface_affinity") or {}).items():
            if blk not in block_names:
                bad(f"enemy[{ename}].environment.surface_affinity 引用了不存在的方块: {blk!r}")
            if not isinstance(mult, (int, float)) or mult <= 0:
                bad(f"enemy[{ename}].environment.surface_affinity[{blk}] 非法: {mult!r}")
        atk = ed.get("attack") or {}
        for f in ("damage", "cooldown", "range"):
            if f in atk and (not isinstance(atk[f], (int, float)) or atk[f] <= 0):
                bad(f"enemy[{ename}].attack.{f} 非法: {atk.get(f)!r}")
        drops = ed.get("drops") or []
        if not drops:
            bad(f"enemy[{ename}].drops 为空")
        for d in drops:
            if not isinstance(d, (list, tuple)) or len(d) != 2:
                bad(f"enemy[{ename}].drops 项格式非法: {d!r}")
                continue
            if d[0] not in mat_names:
                bad(f"enemy[{ename}].drops 引用了不存在的材料: {d[0]!r}")
            if not _is_int(d[1]) or d[1] < 1:
                bad(f"enemy[{ename}].drops 数量非法: {d[1]!r}")
    eg = ENEMY_GLOBAL
    for f in ("base_damage", "health_growth", "speed_growth", "reward_growth"):
        if not isinstance(eg.get(f), (int, float)):
            bad(f"enemies.global.{f} 缺失或非法: {eg.get(f)!r}")
    for biome, mult in TERRAIN_SPEED_MULTS.items():
        if not isinstance(mult, (int, float)) or mult <= 0:
            bad(f"enemies.terrain_speed[{biome}] 非法: {mult!r}")

    # ── 塔 ──
    for tn, tdef in TOWERS_RAW.items():
        no_atk = bool(tdef.get("no_attack", False))
        if not isinstance(tdef.get("no_attack", False), bool):
            bad(f"tower[{tn}].no_attack 应为布尔: {tdef.get('no_attack')!r}")
        # 攻击属性：防御塔(no_attack)允许 0(永不索敌开火)；攻击塔仍须 >0
        for f in ("base_damage", "base_range", "base_cooldown", "base_cost"):
            v = tdef.get(f)
            if not isinstance(v, (int, float)) or (v < 0 if no_atk else v <= 0):
                bad(f"tower[{tn}].{f} 非法: {tdef.get(f)!r}")
        ws = tdef.get("weapon_slots", 0)
        if not isinstance(ws, int) or isinstance(ws, bool) or ws < 0:
            bad(f"tower[{tn}].weapon_slots 非法: {ws!r}")
        if not isinstance(tdef.get("base_hp", 100), (int, float)) or tdef.get("base_hp", 100) <= 0:
            bad(f"tower[{tn}].base_hp 非法: {tdef.get('base_hp')!r}")
        if not _valid_color(tdef.get("color")):
            bad(f"tower[{tn}].color 非法: {tdef.get('color')!r}")
        for t in tdef.get("requires_tech", []) or []:
            if t in TECH_IDS:
                continue
            if t in tech_names:
                warn(f"tower[{tn}].requires_tech 仍用 legacy ID {t!r}，应迁移为对应的新突破点 ID")
            else:
                bad(f"tower[{tn}].requires_tech 引用了不存在的科技: {t!r}")
        for m in tdef.get("materials", {}):
            if m not in mat_names:
                bad(f"tower[{tn}].materials 引用了不存在的材料: {m!r}")
        bv = tdef.get("bonus_vs")
        if bv is not None and bv not in enemy_names:
            bad(f"tower[{tn}].bonus_vs 引用了不存在的敌人类型: {bv!r}")
        sr = tdef.get("splash_radius", 0.0)
        if not isinstance(sr, (int, float)) or sr < 0:
            bad(f"tower[{tn}].splash_radius 非法: {sr!r}")

    # ── 刷怪点 / 侦察兵 ──
    dist = DISTRIBUTION_CFG
    for f in ("grid", "density", "base_materialize_radius", "materialize_radius",
              "path_max_steps", "awake_count"):
        if not isinstance(dist.get(f), (int, float)) or dist[f] <= 0:
            bad(f"spawners.distribution.{f} 缺失或非法: {dist.get(f)!r}")
    kinds = SPAWNERS_CFG.get("kinds", {})
    if "standard" not in kinds:
        bad("spawners.kinds 缺少 standard（兜底种类）")
    covered_biomes = {}
    for kname, kd in kinds.items():
        biomes = kd.get("biomes") or []
        if not biomes:
            bad(f"spawners.kinds[{kname}].biomes 为空")
        for b in biomes:
            if b in covered_biomes:
                warn(f"spawners.kinds 生物群系 {b} 被 {covered_biomes[b]} 与 {kname} 同时覆盖（按 kinds 顺序取第一个）")
            else:
                covered_biomes[b] = kname
        tr = kd.get("trigger_radii") or {}
        for f in ("presence", "build", "combat", "edit"):
            if not isinstance(tr.get(f), (int, float)) or tr[f] <= 0:
                bad(f"spawners.kinds[{kname}].trigger_radii.{f} 非法: {tr.get(f)!r}")
        sc = kd.get("scout") or {}
        if sc.get("enemy_type") not in enemy_names:
            bad(f"spawners.kinds[{kname}].scout.enemy_type 引用了不存在的敌人类型: {sc.get('enemy_type')!r}")
        if not _is_int(sc.get("batch")) or sc["batch"] < 1:
            bad(f"spawners.kinds[{kname}].scout.batch 非法: {sc.get('batch')!r}")
        for f in ("perception_radius", "wander_duration", "wander_speed_mult",
                  "turn_interval", "wander_turn", "noise_bias", "noise_jitter",
                  "probe_dist", "meet_radius", "edit_radius"):
            if not isinstance(sc.get(f), (int, float)) or sc[f] <= 0:
                bad(f"spawners.kinds[{kname}].scout.{f} 缺失或非法: {sc.get(f)!r}")

    # ── 波次 ──
    for wrow in WAVES_DATA.get("waves", []):
        wn = wrow.get("wave")
        if not _is_int(wn) or wn < 1:
            bad(f"waves[].wave 非法: {wn!r}")
        cnt = wrow.get("count")
        if cnt is not None and (not _is_int(cnt) or cnt < 1):
            bad(f"waves[wave={wn}].count 非法: {cnt!r}")
        types = wrow.get("enemy_types")
        if types is not None:
            for et in types:
                if et not in enemy_names:
                    bad(f"waves[wave={wn}].enemy_types 引用了不存在的敌人类型: {et!r}")
    fb = WAVE_FALLBACK
    for f in ("base_count", "count_per_wave", "interval_base", "interval_min",
              "interval_decay", "bonus_base", "bonus_per_wave", "score_per_wave"):
        if not isinstance(fb.get(f), (int, float)):
            bad(f"waves.fallback.{f} 缺失或非法: {fb.get(f)!r}")
    for et in fb.get("enemy_types_default", []):
        if et not in enemy_names:
            bad(f"waves.fallback.enemy_types_default 引用了不存在的敌人类型: {et!r}")
    if (isinstance(fb.get("interval_min"), (int, float))
            and isinstance(fb.get("interval_base"), (int, float))
            and fb["interval_min"] > fb["interval_base"]):
        bad(f"waves.fallback.interval_min({fb['interval_min']}) 大于 interval_base({fb['interval_base']})")

    return errs, warns


def validate_data():
    """启动校验：发现问题抛 ValueError（列出全部），警告打印到 stderr。"""
    errs, warns = _collect_data_issues()
    for w in warns:
        print(f"[gamedata] 警告: {w}")
    if errs:
        raise ValueError(
            "data/*.json 数据校验失败，共 %d 项问题:\n  - %s"
            % (len(errs), "\n  - ".join(errs)))


# 导入即校验（fail fast）
validate_data()