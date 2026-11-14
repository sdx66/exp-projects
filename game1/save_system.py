"""
save_system.py - 纯 Python 存档系统（零外部依赖，仅 json/os/time/random）

格式: JSON，自包含路径数据（不依赖 Universe 重生成顺序）。
兼容: 反序列化一律 .get(key, default) —— 存档多出的字段忽略，缺少的字段兜底。
版本: config.SAVE_VERSION（v10 起按塔研究：研究状态存入各研究塔 research 段，
      旧全局 tech researching 经 migrate_v9_to_v10 迁入首座研究塔）。

用法:
    import save_system
    save_system.save_game(game)            # 快存（F5），自动命名
    save_system.save_game(game, "my_run")  # 命名存档
    game2 = save_system.load_game(path)    # 返回 Game 或 None（失败）
    saves = save_system.list_saves()       # [{name,path,wave,seed,created_at,size}]
    save_system.delete_save(path)
"""

import json
import os
import random
import time
import math

import config
import gamedata

# 存档目录 = <项目根>/saves/（与 tower_defense/ 同级）
_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_HERE)
SAVE_DIR = os.path.join(_PROJECT_ROOT, "saves")


# ─── 目录管理 ────────────────────────────────────────────────

def ensure_save_dir() -> str:
    """确保存档目录存在，返回绝对路径。"""
    os.makedirs(SAVE_DIR, exist_ok=True)
    return SAVE_DIR


# ─── 序列化 ──────────────────────────────────────────────────

def _serialize_tower(t) -> dict:
    return {
        "type_name": t.type_name,
        "position": list(t.position),
        "level": t.level,
        "damage_mult": t.damage_mult,
        "damage": t.damage,
        "range": t.range,
        "cooldown": t.cooldown,
        "cooldown_timer": t.cooldown_timer,
        "active": t.active,
        "hp": t.hp,
        "max_hp": t.max_hp,
        "source": list(t.source) if t.source else ["classic", t.type_name],
        "material_recipe": dict(t.material_recipe) if t.material_recipe else {},
        # 防御塔挂载的武器（v6+；旧档缺省为空）。timer 为逐帧递减的冷却倒计时
        # （BUG-074）：不存会让读档后武器 timer 归零、立即开火白赚一次 DPS。
        "mounted_weapons": [
            {"weapon": m["wdef"].name, "damage_mult": m["damage_mult"],
             "timer": float(m.get("timer", 0.0) or 0.0)}
            for m in getattr(t, "weapon_mounts", [])
        ],
        # 仓储塔存物（v7+；旧档缺省为空）
        "storage": (
            {
                "materials": dict(t.storage["materials"]),
                "weapons": dict(t.storage["weapons"]),
                "tools": sorted(t.storage["tools"]),
            }
            if getattr(t, "storage_capacity", 0) else None
        ),
        # 传送带导出白名单（v7+；旧档缺省为空集）
        "belt_export": sorted(getattr(t, "belt_export", ()) or ()),
        # 加工塔存物与在制任务（v6+；旧档缺省为空）
        "processing": (
            {
                "materials": dict(t.processing["materials"]),
                "weapons": dict(t.processing["weapons"]),
                "tools": sorted(t.processing["tools"]),
                "manual_recipe": t.manual_recipe,
                "jobs": {k: float(v) for k, v in t.process_jobs.items()},
                "meta": {k: dict(v) for k, v in getattr(t, "process_meta", {}).items()},
            }
            if getattr(t, "processing_capacity", 0) else None
        ),
        # 电力塔（v8+）：电站燃料桶 / 电池储能（旧档缺省为空）
        "power_fuel": dict(getattr(t, "power_fuel", {}) or {}),
        "charge_j": float(getattr(t, "charge_j", 0.0) or 0.0),
        # 电站逐帧小数燃料累加器（BUG-129）：不存会让每次读档把已累计的
        # 不足一件燃料的消耗义务清零，等于白饶一件燃料。旧档缺省 0.0（兼容）。
        "fuel_need_acc": float(getattr(t, "_fuel_need_acc", 0.0) or 0.0),
        # 开采塔（v9+）：足迹 / 竖井前沿与进度（旧档缺省为空）
        "mining": (
            {
                "footprint": [int(t.footprint[0]), int(t.footprint[1])],
                "start_surface": {
                    f"{cx},{cz}": int(sy)
                    for (cx, cz), sy in getattr(t, "mine_start_surface", {}).items()
                },
                "shaft": {
                    f"{cx},{cz}": [int(col["front"]) if col["front"] is not None else -1,
                                   float(col["timer"])]
                    for (cx, cz), col in getattr(t, "mine_shaft", {}).items()
                },
            }
            if getattr(t, "is_mining", False) else None
        ),
        # 研究塔（v10+）：current/timer/total + 排队列表（旧档缺省为空）
        "research": (
            {
                "current": getattr(t, "research_current", None),
                "timer": float(getattr(t, "research_timer", 0.0) or 0.0),
                "total": float(getattr(t, "research_total", 0.0) or 0.0),
                "queue": list(getattr(t, "research_queue", ()) or ()),
            }
            if getattr(t, "is_research", False) else None
        ),
    }

def _serialize_base_research(br) -> dict:
    """基地手研通道序列化（station=false 科技，无需研究塔；v11+）。"""
    if br is None:
        return None
    return {
        "current": getattr(br, "research_current", None),
        "timer": float(getattr(br, "research_timer", 0.0) or 0.0),
        "total": float(getattr(br, "research_total", 0.0) or 0.0),
        "queue": list(getattr(br, "research_queue", ()) or ()),
    }


def _serialize_belt(b) -> dict:
    """传送带序列化：cell / kind / direction / 带内货物 / 均分器计数 / 桥两条 lane。"""
    d = {
        "cell": [b.cell[0], b.cell[1]],
        "kind": b.kind,
        "direction": b.direction,
        "counter": getattr(b, "counter", 0),
    }
    if b.kind == "bridge":
        d["lanes"] = [
            {"direction": lane["direction"],
             "items": [[it["name"], float(it["head"])] for it in lane["items"]]}
            for lane in b.lanes
        ]
    else:
        d["items"] = [[it["name"], float(it["head"])] for it in b.items]
    return d


def _serialize_wire(w) -> dict:
    """电线序列化：cell / kind / 跳闸计时（电流/损耗为求解结果，不持久化）。"""
    return {
        "cell": [w.cell[0], w.cell[1]],
        "kind": w.kind,
        "trip_timer": float(getattr(w, "trip_timer", 0.0) or 0.0),
        "tripped": bool(getattr(w, "tripped", False)),
    }


def _serialize_enemy(e, tower_ids=None, pathfinder=None) -> dict:
    """序列化敌人。tower_ids: {id(Tower): 索引}，用于把攻击目标还原成引用
    （BUG-070：behavior='attack_tower' 但 attack_target 不存，读档后弃攻回 wave）。
    pathfinder: 用于判定敌人是否真走在变体路径上（BUG-076）。"""
    at = getattr(e, "attack_target", None)
    if at is not None and tower_ids:
        attack_target = tower_ids.get(id(at))
    else:
        attack_target = None
    # 是否真用变体路径（BUG-076）：存档格式里 path_variant=0 既可能是变体 0，
    # 也可能是出生时寻路失败退回主线的敌人。按对象同一性比对当前变体集，
    # 只在确实命中变体时置 True，读档才挂变体，否则保持主线。
    on_variant = False
    sid = getattr(e, "home_spawner_id", -1)
    if pathfinder is not None and isinstance(sid, int) and sid >= 0:
        entry = pathfinder.variants.get((sid, e.type_name))
        if entry:
            cur = getattr(e, "path", None)
            on_variant = any(cur is vp for vp in (entry.get("paths") or []))
    return {
        "type_name": e.type_name,
        "position": list(e.position),
        "path_ref": getattr(e, "home_spawner_id", -1),
        "path_index": e.path_index,
        "health": e.health,
        "max_health": e.max_health,
        "speed": e.speed,
        "reward": e.reward,
        # #2 掉落波次口径：记录生成时的刷怪点 wave，读档后击杀掉落加成
        # 仍与本敌人 reward/血量/速度同口径（不由全局 self.wave 代替）。
        "spawn_wave": getattr(e, "spawn_wave", 0),
        "active": e.active,
        "slow_timer": e.slow_timer,
        "slow_factor": e.slow_factor,
        "wobble_amp": e.wobble_amp,
        "wobble_freq": e.wobble_freq,
        "wobble_phase": e.wobble_phase,
        "time": e.time,
        "path_noise": e.path_noise,
        "personality": dict(e.personality) if e.personality else None,
        # 行为状态机字段（v2+）
        "behavior": getattr(e, "behavior", "wave"),
        # 攻击目标（v10+）：game.towers 中的索引，None = 无目标（旧档缺省）
        "attack_target": attack_target,
        "home_spawner_id": getattr(e, "home_spawner_id", -1),
        "has_message": getattr(e, "has_message", False),
        "scout_returned_with_message": getattr(e, "scout_returned_with_message", False),
        "wander_heading": getattr(e, "wander_heading", 0.0),
        "wander_turn_timer": getattr(e, "wander_turn_timer", 0.0),
        "wander_lifetime": getattr(e, "wander_lifetime", 0.0),
        "return_path": [list(pt) for pt in getattr(e, "return_path", ())],
        "return_path_index": getattr(e, "return_path_index", 0),
        # 寻路状态（v5+）
        "path_variant": getattr(e, "path_variant", 0),
        # 是否真用变体路径（v10+，BUG-076）：读档据此决定挂变体还是保持主线
        "on_variant": on_variant,
        "path_version": getattr(e, "path_version", -1),
        "attack_timer": getattr(e, "attack_timer", 0.0),
        "dig_target": list(getattr(e, "dig_target", None) or []),
        "dig_progress": getattr(e, "dig_progress", 0.0),
        "dig_y": getattr(e, "dig_y", None),
        "dig_base_y": getattr(e, "dig_base_y", None),
        "dig_passed": bool(getattr(e, "dig_passed", False)),
        "dug_count": getattr(e, "_dug_count", 0),
        # BUG-103：会话是否改低了目标列通行高度（读档续挖时需保留，否则
        # 地形变化漏 bump world_version）
        "dig_marked_passage": bool(getattr(e, "_dig_marked_passage", False)),
        "repath_timer": getattr(e, "repath_timer", 0.0),
        # R3-H1：BUG-105"原地等待"标记必须进存档——否则读档后等待态敌人变
        # 主动攻基地（恢复主线末段 + waiting=False → 走完末段即结算 reached_base）。
        "waiting_for_path": bool(getattr(e, "waiting_for_path", False)),
        # 非标准侦察兵配置（v6+）：丢失会让高地带/森林哨兵读档后退回标准侦察兵
        "scout_cfg": (dict(getattr(e, "scout_cfg", None))
                      if getattr(e, "scout_cfg", None) else None),
    }


def _path_index_for(e) -> int:
    """在 universe.enemy_paths 中查找敌人当前使用的路径索引。"""
    try:
        for i, p in enumerate(e.all_paths):
            if p is e.path:
                return i
    except Exception:
        pass
    return 0


def _serialize_projectile(p) -> dict:
    return {
        "position": list(p.position),
        "velocity": list(p.velocity),
        "damage": p.damage,
        "lifetime": p.lifetime,
        "active": p.active,
        "splash_radius": p.splash_radius,
        "splash_falloff": p.splash_falloff,
        "slow_factor": p.slow_factor,
        "slow_duration": p.slow_duration,
        "bonus_vs": p.bonus_vs,
        "bonus_mult": p.bonus_mult,
        # source_tower 在 serialize_game 中填充（需查 towers 列表）
    }


def _serialize_pickup(p) -> dict:
    return {
        "position": list(p.position),
        "item_type": p.item_type,
        "item_name": p.item_name,
        "amount": p.amount,
        "active": p.active,
        "bob_phase": p.bob_phase,
        "bob_speed": p.bob_speed,
        "time": p.time,
    }


def _serialize_camera(cam) -> dict:
    return {
        "mode": cam.mode,
        "target": list(cam.target),
        "distance": cam.distance,
        "altitude": cam.altitude,
        "azimuth": cam.azimuth,
        "pos_2d": list(cam.pos_2d),
        "zoom_2d": cam.zoom_2d,
    }


def _serialize_rng(rng) -> list:
    """random.Random.getstate() → JSON 可序列化列表。"""
    state = rng.getstate()
    return [state[0], list(state[1]), state[2]]


def serialize_game(game) -> dict:
    """遍历 game.* 提取全部可保存状态。"""
    universe = game.universe

    # 路径（自包含存储，dict by spawner_id）
    paths_data = []
    for sid, pd in universe.enemy_paths.items():
        paths_data.append({
            "spawner_id": sid,
            "points": [list(pt) for pt in pd.points],
            "length": pd.length,
            "avg_slope": pd.avg_slope,
        })

    # 寻路变体路径（v5+，{(sid,type): [PathData]} → 扁平列表）
    variants_data = []
    pf = getattr(game, "pathfinder", None)
    if pf is not None:
        for (sid, type_name), entry in pf.variants.items():
            for idx, pd in enumerate(entry.get("paths", [])):
                variants_data.append({
                    "spawner_id": sid,
                    "type_name": type_name,
                    "index": idx,
                    "points": [list(pt) for pt in pd.points],
                    "length": pd.length,
                    "avg_slope": pd.avg_slope,
                    "dig_cells": [list(c) for c in pd.dig_cells],
                    # 变体版本号（BUG-072）：-1 = 存档时已失效（陈旧路径）。
                    # 不存会让读档把陈旧变体当成当前版本复活。
                    "version": entry.get("version", -1),
                })

    # 塔（用于 projectile 的 source_tower 索引）
    tower_ids = {id(t): i for i, t in enumerate(game.towers)}

    # 抛射体
    projectiles = []
    for p in game.projectiles:
        pd = _serialize_projectile(p)
        pd["source_tower"] = tower_ids.get(id(p.source)) if p.source else None
        projectiles.append(pd)

    return {
        "version": config.SAVE_VERSION,
        "created_at": time.time(),
        "seed": universe.seed,
        "paths": paths_data,
        "variants": variants_data,
        "game": {
            "money": game.money,
            "score": game.score,
            "wave": game.wave,
            "game_time": game.game_time,
            "wave_delay": game.wave_delay,
            "wave_announcement_timer": game.wave_announcement_timer,
        },
        "base": {
            "health": game.base.health,
            "max_health": game.base.max_health,
        },
        "spawners": [
            {
                "spawner_id": rt.spawner_id,
                "mode": rt.mode,
                "wave_num": rt.wave_num,
                "spawn_timer": rt.spawn_timer,
                "spawn_interval": rt.spawn_interval,
                "enemy_count": rt.enemy_count,
                "max_enemies": rt.max_enemies,
                "enemy_types": list(rt.enemy_types),
                "between_wave": rt.between_wave,
                "between_timer": rt.between_timer,
                "scout_batch_spawned": rt.scout_batch_spawned,
                "scouts_pending": rt.scouts_pending,
                "message_received": rt.message_received,
                "announcement_timer": rt.announcement_timer,
                "x": universe.spawners[rt.spawner_id].x if rt.spawner_id in universe.spawners else 0,
                "z": universe.spawners[rt.spawner_id].z if rt.spawner_id in universe.spawners else 0,
            }
            for rt in game.spawners.values()
        ],
        "inventory": {
            "materials": dict(game.inventory.materials),
            "weapons": dict(game.inventory.weapons),
            "tools": sorted(game.inventory.tools),
        },
        "towers": [_serialize_tower(t) for t in game.towers],
        "belts": [_serialize_belt(b) for b in getattr(game, "belts", ())],
        "wires": [_serialize_wire(w) for w in getattr(game, "wires", ())],
        "enemies": [_serialize_enemy(e, tower_ids, pf) for e in game.enemies],
        "projectiles": projectiles,
        "pickups": [_serialize_pickup(p) for p in game.pickups],
        "tech": {
            "unlocked": sorted(game.unlocked_tech),
            # v10+：研究状态按塔保存（见塔 research 段），不再写全局槽
            # v11+：基地手研通道（station=false 科技，无需研究塔）
            "base_research": _serialize_base_research(getattr(game, "base_research", None)),
        },
        "world": {
            "rng_state": _serialize_rng(universe.rng),
            "harvested_cells": [list(c) for c in universe._harvested_cells],
            "world_version": universe.world_version,
            "block_edits": [[x, y, z, b]
                            for (x, y, z), b in universe.block_edits.items()],
            "surface_override": [[x, z, y]
                                 for (x, z), y in universe.surface_override.items()],
            "weather": {
                "is_raining": universe.weather.is_raining,
                "is_snowing": universe.weather.is_snowing,
                "intensity": universe.weather.intensity,
                "wind_direction": universe.weather.wind_direction,
                "wind_strength": universe.weather.wind_strength,
            },
            "weather_timer": universe.weather_timer,
        },
        "camera": _serialize_camera(game.camera),
    }


# ─── 反序列化 ────────────────────────────────────────────────

def _restore_paths(universe, paths_data):
    """用存档路径覆盖 universe.enemy_paths，重建 _path_cells。"""
    if not paths_data:
        return  # 旧存档无 paths 段，沿用 Universe 重生成路径
    from world import PathData
    restored = {}
    for pd in paths_data:
        try:
            points = tuple(tuple(pt) for pt in pd.get("points", []))
        except Exception:
            continue
        if not points:
            continue
        sid = pd.get("spawner_id", 0)
        restored[sid] = PathData(
            points=points,
            length=pd.get("length", len(points)),
            avg_slope=pd.get("avg_slope", 0.0),
        )
    if not restored:
        return
    universe.enemy_paths = restored
    universe._path_cells = {
        (math.floor(x), math.floor(z))
        for path in restored.values() for x, y, z in path.points
    }


def _restore_spawner_positions(universe, spawners_data):
    """重新材料化存档记录过的刷怪点（保存时已材料化的，加载时补回）。

    加载时 Universe 只按基地半径重新材料化（_materialize_base_spawners），
    存档里更远的 dormant 刷怪点不会自动出现，但其 id/坐标已随存档保存。
    这里按粗格坐标直接补材料化，保证 game.spawners 的每个 id 都存在于
    universe.spawners（否则 _rebuild_spawner_hash 会 KeyError）。
    落地校验：存档 id 与坐标推导出的粗格 id 必须一致，缺坐标/不匹配即跳过。
    """
    grid = gamedata.SPAWNER_GRID
    for sd in spawners_data:
        sid = sd.get("spawner_id", 0)
        if not isinstance(sid, int) or sid < 0:
            continue
        if sid in universe.spawners:
            continue
        x, z = sd.get("x"), sd.get("z")
        if not isinstance(x, int) or not isinstance(z, int):
            continue
        gx, gz = x // grid, z // grid
        if universe._spawner_id_for(gx, gz) != sid:
            # 存档 id 与坐标推导不符（旧格式/损坏），跳过
            continue
        if sid in universe.spawners:
            continue
        universe._materialized_coarse.add((gx, gz))
        universe._materialize_spawner_at_coarse(gx, gz)


def _spawner_ids_need_reencode(data: dict) -> bool:
    """检测存档刷怪点 id 是否仍是 BUG-098 修复前的 base-65536 编码。

    新编码 (gx&0xFFFF)<<16|(gz&0xFFFF) 对可达网格与旧编码取值不同。
    任一存档刷怪点：用其坐标重算新编码，若与存储 id 不符 → 需要重编码。
    全部相符（已是新档）则返回 False，避免无谓重映射。
    """
    grid = gamedata.SPAWNER_GRID
    for sd in data.get("spawners", []):
        if not isinstance(sd, dict):
            continue
        sid = sd.get("spawner_id")
        x, z = sd.get("x"), sd.get("z")
        if not isinstance(sid, int) or not isinstance(x, int) or not isinstance(z, int):
            continue
        if sid != ((x // grid) & 0xFFFF) << 16 | ((z // grid) & 0xFFFF):
            return True
    return False


def _reencode_spawner_ids(data: dict):
    """BUG-098 存档兼容：把旧 base-65536 刷怪点 id 重映射为当前定宽编码。

    刷怪点坐标随存档保存且稳定，故以坐标重算新 id，建立 old→new 映射后
    同步更新 spawners / paths / variants / enemies 里的所有 spawner_id 引用，
    保证读档后 id 与当前 _spawner_id_for 一致（否则 _restore_spawner_positions
    会因 id 不符丢弃刷怪点，敌人/路径随之失联）。
    """
    grid = gamedata.SPAWNER_GRID
    old_to_new = {}
    for sd in data.get("spawners", []):
        if not isinstance(sd, dict):
            continue
        x, z = sd.get("x"), sd.get("z")
        if not isinstance(x, int) or not isinstance(z, int):
            continue
        old = sd.get("spawner_id")
        new = ((x // grid) & 0xFFFF) << 16 | ((z // grid) & 0xFFFF)
        if isinstance(old, int):
            old_to_new[old] = new
        sd["spawner_id"] = new
    if not old_to_new:
        return
    # 路径（dict by spawner_id，序列化为 list 项）
    for pd in data.get("paths", []):
        if isinstance(pd, dict) and pd.get("spawner_id") in old_to_new:
            pd["spawner_id"] = old_to_new[pd["spawner_id"]]
    # 变体（扁平 list，每项带 spawner_id/type_name）
    for vd in data.get("variants", []):
        if isinstance(vd, dict) and vd.get("spawner_id") in old_to_new:
            vd["spawner_id"] = old_to_new[vd["spawner_id"]]
    # 敌人（home_spawner_id / path_ref）
    for ed in data.get("enemies", []):
        if not isinstance(ed, dict):
            continue
        for field in ("home_spawner_id", "path_ref"):
            old = ed.get(field)
            if isinstance(old, int) and old in old_to_new:
                ed[field] = old_to_new[old]


def _restore_rng(universe, rng_state_data):
    """恢复 random.Random 内部状态。失败则保持重生成的 RNG。"""
    if not rng_state_data or len(rng_state_data) < 3:
        return
    try:
        state = (rng_state_data[0], tuple(rng_state_data[1]), rng_state_data[2])
        universe.rng.setstate(state)
    except (ValueError, TypeError):
        pass  # 状态不合法，保持重生成的 RNG


def _restore_weather(universe, weather_data):
    w = universe.weather
    if not weather_data:
        return
    w.is_raining = weather_data.get("is_raining", w.is_raining)
    w.is_snowing = weather_data.get("is_snowing", w.is_snowing)
    w.intensity = weather_data.get("intensity", w.intensity)
    w.wind_direction = weather_data.get("wind_direction", w.wind_direction)
    w.wind_strength = weather_data.get("wind_strength", w.wind_strength)


def _restore_terrain_edits(universe, world_data):
    """恢复地形编辑覆盖层（挖掘痕迹）。v5+，旧存档无此段则跳过。"""
    if not world_data:
        return
    universe.block_edits = {}
    for item in world_data.get("block_edits", []):
        try:
            x, y, z, b = int(item[0]), int(item[1]), int(item[2]), int(item[3])
        except (TypeError, ValueError, IndexError):
            continue
        universe.block_edits[(x, y, z)] = b
    universe.surface_override = {}
    for item in world_data.get("surface_override", []):
        try:
            x, z, y = int(item[0]), int(item[1]), int(item[2])
        except (TypeError, ValueError, IndexError):
            continue
        universe.surface_override[(x, z)] = y
    universe.world_version = world_data.get("world_version", 0)
    # 重建区块索引
    cs = config.CHUNK_SIZE
    universe._edits_by_chunk = {}
    for (x, y, z) in universe.block_edits:
        universe._edits_by_chunk.setdefault(
            (x // cs, z // cs), set()).add((x, y, z))
    # 已加载的区块（基地周边）立即重放，避免挖掘痕迹缺失
    for key in list(universe.chunks):
        chunk = universe.chunks[key]
        if key in universe._edits_by_chunk:
            universe._replay_chunk_edits(chunk)


def _restore_variants(game, variants_data):
    """恢复寻路变体缓存。v5+，缺省时各变体惰性重算。"""
    pf = getattr(game, "pathfinder", None)
    if pf is None or not variants_data:
        return
    from world import PathData
    grouped = {}
    for vd in variants_data:
        try:
            sid = int(vd.get("spawner_id", 0))
            type_name = str(vd.get("type_name", ""))
            idx = int(vd.get("index", 0))
            # 每个点必须恰好 3 元素 (x,y,z)：下面 `for x, _y, z in pd.points`
            # 的解包在 try/except 之外，一个 2 元素的坏点会抛 ValueError 并
            # 让整个存档被拒（BUG-115）。这里过滤掉非 3 元素的点，若有脏数据
            # （过滤后数量与原始数量不一致）则整条变体跳过，沿用防御性 continue。
            raw_points = list(vd.get("points", []))
            points = tuple(
                tuple(pt) for pt in raw_points
                if isinstance(pt, (list, tuple)) and len(pt) == 3)
        except (TypeError, ValueError):
            continue
        if len(points) != len(raw_points):
            continue
        if not points or not type_name:
            continue
        dig_cells = frozenset(
            (int(c[0]), int(c[1]))
            for c in vd.get("dig_cells", [])
            if isinstance(c, (list, tuple)) and len(c) >= 2)
        version = vd.get("version", -1)
        if not isinstance(version, int):
            try:
                version = int(version)
            except (TypeError, ValueError):
                version = -1
        # 陈旧变体（存档时已被 invalidate 标脏，version=-1）不复活：
        # 否则读档会把旧世界算出的路径当成当前版本，敌人沿它穿过新塔/新挖地形
        # （BUG-072）。缺省让它惰性重算。
        if version < 0 or version != game.universe.world_version:
            continue
        pd = PathData(
            points=points,
            length=vd.get("length", len(points)),
            avg_slope=vd.get("avg_slope", 0.0),
            dig_cells=dig_cells,
        )
        grouped.setdefault((sid, type_name), {})[idx] = pd
    for (sid, type_name), by_idx in grouped.items():
        paths = [by_idx[i] for i in sorted(by_idx)]
        pf.variants[(sid, type_name)] = {
            "version": game.universe.world_version,
            "paths": paths,
        }
    # 路径格并入全局集合（道具避让语义沿用）
    for entry in pf.variants.values():
        for pd in entry["paths"]:
            for x, _y, z in pd.points:
                game.universe._path_cells.add((math.floor(x), math.floor(z)))


def _restore_camera(cam, cam_data):
    """恢复相机状态。缺失字段保持默认。*_target 设为当前值避免平滑过渡。"""
    if not cam_data:
        return
    cam.mode = cam_data.get("mode", cam.mode)
    try:
        cam.target = tuple(cam_data.get("target", cam.target))
    except (TypeError, ValueError):
        pass
    cam.distance = cam_data.get("distance", cam.distance)
    cam.altitude = cam_data.get("altitude", cam.altitude)
    cam.azimuth = cam_data.get("azimuth", cam.azimuth)
    cam.distance_target = cam.distance
    cam.altitude_target = cam.altitude
    cam.azimuth_target = cam.azimuth
    try:
        cam.pos_2d = tuple(cam_data.get("pos_2d", cam.pos_2d))
    except (TypeError, ValueError):
        pass
    cam.zoom_2d = cam_data.get("zoom_2d", cam.zoom_2d)
    cam.zoom_2d_target = cam.zoom_2d


def _restore_towers(data, towers_data):
    """从存档重建塔列表。未知类型跳过+警告。"""
    from entities import TOWER_TYPES, Tower
    from items import CLASSIC_TOWER_MATERIALS, WEAPON_TYPES, PROCESS_RECIPES
    towers = []
    for td in towers_data:
        type_name = td.get("type_name", "")
        tt = TOWER_TYPES.get(type_name)
        try:
            pos = tuple(td.get("position", [0, 0, 0]))
        except (TypeError, ValueError):
            continue
        level = td.get("level", 1)
        damage_mult = td.get("damage_mult", 1.0)
        if tt is not None:
            tower = Tower(tt, pos, level, damage_mult)
        else:
            # 武器派生塔：type_name 是武器名（如 wood_club），不在 TOWER_TYPES 中，
            # 需按 WEAPON_TYPES 重建，否则武器塔会在读档时被静默丢弃。
            wdef = WEAPON_TYPES.get(type_name)
            if wdef is None:
                print(f"[存档] 跳过未知塔类型: {type_name}")
                continue
            tower = Tower.from_weapon(wdef, pos, damage_mult)
            tower.level = level
        # 覆盖存档中的实际数值（可能与重算不同，如升级/科技加成后）
        tower.damage = td.get("damage", tower.damage)
        tower.range = td.get("range", tower.range)
        tower.cooldown = td.get("cooldown", tower.cooldown)
        tower.cooldown_timer = td.get("cooldown_timer", 0.0)
        tower.active = td.get("active", True)
        # 塔体 HP（v5+；旧存档满血）
        # 武器派生塔的 max_hp 无法从等级推导（TOWER_HP 里没有武器名），
        # 必须按存档恢复，否则 3 级武器塔会退回 100。
        saved_max = td.get("max_hp")
        if isinstance(saved_max, (int, float)) and saved_max > 0:
            tower.max_hp = float(saved_max)
        hp = td.get("hp", tower.max_hp)
        if isinstance(hp, (int, float)):
            tower.hp = max(0.0, min(float(hp), tower.max_hp))
        else:
            tower.hp = tower.max_hp
        src = td.get("source")
        if src and len(src) >= 2:
            tower.source = (src[0], src[1])
        mat_recipe = td.get("material_recipe")
        if mat_recipe is not None:
            tower.material_recipe = dict(mat_recipe)
        elif not tower.material_recipe:
            # 旧档无 material_recipe 字段时才兜底（BUG-073）：武器派生塔的
            # 配方已由 from_weapon 按武器定义填好，不能被空 dict 覆盖。
            # 经典塔此处本为空，按经典配方兜底。
            tower.material_recipe = dict(CLASSIC_TOWER_MATERIALS.get(type_name, {}))
        # 防御塔挂载武器（v6+；旧档无此字段 → 空列表，兼容）
        for mw in td.get("mounted_weapons") or []:
            wdef = WEAPON_TYPES.get(mw.get("weapon"))
            if wdef is None:
                print(f"[存档] 跳过未知挂载武器: {mw.get('weapon')!r}")
                continue
            if not tower.mount_weapon(wdef,
                                      damage_mult=float(mw.get("damage_mult", 1.0))):
                break  # 槽位已满：不再挂更多
            # 冷却倒计时（BUG-074）：mount_weapon 把 timer 归零，会让读档后
            # 武器立刻开火。按存档恢复 mid-cooldown 状态。clamp 到 [0, cooldown]
            # 防御旧档/异常值出现负数或超长冷却。
            mt = mw.get("timer")
            if isinstance(mt, (int, float)) and mt > 0:
                tower.weapon_mounts[-1]["timer"] = min(float(mt),
                                                       float(wdef.cooldown))
        # 仓储塔存物（v7+；旧档无此字段 → 空，兼容）
        storage = td.get("storage")
        if storage and getattr(tower, "storage_capacity", 0):
            st = tower.storage
            st["materials"] = {str(k): int(v)
                               for k, v in (storage.get("materials") or {}).items()}
            st["weapons"] = {str(k): int(v)
                             for k, v in (storage.get("weapons") or {}).items()}
            st["tools"] = set(storage.get("tools") or [])
        # 加工塔存物与在制任务（v6+；旧档无此字段 → 空，兼容）
        processing = td.get("processing")
        if processing and getattr(tower, "processing_capacity", 0):
            pt = tower.processing
            pt["materials"] = {str(k): int(v)
                               for k, v in (processing.get("materials") or {}).items()}
            pt["weapons"] = {str(k): int(v)
                             for k, v in (processing.get("weapons") or {}).items()}
            pt["tools"] = set(processing.get("tools") or [])
            mr = processing.get("manual_recipe")
            # 只恢复无工具要求的配方（requires_tool 配方须工具在塔，自动线自理）
            if (mr and mr in PROCESS_RECIPES
                    and not PROCESS_RECIPES[mr].get("requires_tool")):
                tower.manual_recipe = mr
            for jk, jv in (processing.get("jobs") or {}).items():
                if isinstance(jk, str) and jk in PROCESS_RECIPES \
                        and isinstance(jv, (int, float)) and jv >= 0:
                    tower.process_jobs[str(jk)] = float(jv)
            # 恢复开工时冻结的产量/耗时（旧档无此字段 → 缺省，按当前工具重算）
            for mk, mv in (processing.get("meta") or {}).items():
                if isinstance(mk, str) and mk in PROCESS_RECIPES \
                        and isinstance(mv, dict):
                    y = mv.get("yield")
                    d = mv.get("duration")
                    if isinstance(y, (int, float)) and isinstance(d, (int, float)):
                        tower.process_meta[str(mk)] = {"yield": int(y), "duration": float(d)}
        # 传送带导出白名单（v7+；旧档无此字段 → 空集，兼容）
        for mat in td.get("belt_export") or ():
            if isinstance(mat, str) and mat:
                tower.belt_export.add(mat)
        # 电力塔（v8+）：电站燃料桶 / 电池储能（旧档缺省为空）
        pf = td.get("power_fuel")
        if isinstance(pf, dict):
            for k, v in pf.items():
                if isinstance(k, str) and isinstance(v, (int, float)) and v > 0:
                    tower.power_fuel[k] = int(v)
        cj = td.get("charge_j")
        if isinstance(cj, (int, float)) and cj >= 0:
            tower.charge_j = float(cj)
        # 电站逐帧小数燃料累加器（BUG-129）：与 power_fuel 一同恢复，避免读档
        # 把累计的不足一件燃料的消耗义务清零。旧档无此字段 → 保持默认 0.0。
        fa = td.get("fuel_need_acc")
        if isinstance(fa, (int, float)):
            tower._fuel_need_acc = float(fa)
        # 开采塔（v9+）：足迹 / 竖井前沿与进度（旧档缺省 → 默认与空）
        mining = td.get("mining")
        if mining and getattr(tower, "is_mining", False):
            fp = mining.get("footprint")
            if isinstance(fp, (list, tuple)) and len(fp) == 2:
                try:
                    m, n = int(fp[0]), int(fp[1])
                except (TypeError, ValueError):
                    m = n = 0
                fmin = int(tower.type_def.mining_attr("footprint_min", 3))
                fmax = int(tower.type_def.mining_attr("footprint_max", 7))
                if fmin <= m <= fmax and fmin <= n <= fmax:
                    tower.footprint = (m, n)
            for k, v in (mining.get("start_surface") or {}).items():
                if isinstance(k, str) and "," in k:
                    try:
                        cx, cz = (int(p) for p in k.split(",", 1))
                        tower.mine_start_surface[(cx, cz)] = int(v)
                    except (TypeError, ValueError):
                        continue
            for k, v in (mining.get("shaft") or {}).items():
                if isinstance(k, str) and "," in k and isinstance(v, (list, tuple)) and len(v) == 2:
                    try:
                        cx, cz = (int(p) for p in k.split(",", 1))
                        front = int(v[0])
                        tower.mine_shaft[(cx, cz)] = {
                            "front": front if front >= 0 else None,
                            "timer": float(v[1]),
                        }
                    except (TypeError, ValueError):
                        continue
        # 研究塔（v10+）：current/timer/total + 排队列表（旧档缺省 → 空）
        if getattr(tower, "is_research", False):
            import tech_tree as tt
            tree = tt.tech_tree()
            rd = td.get("research")
            if isinstance(rd, dict):
                cur = rd.get("current")
                if isinstance(cur, str) and cur in tree.nodes:
                    tower.research_current = cur
                    tmr = rd.get("timer", 0.0)
                    ttl = rd.get("total", 0.0)
                    if isinstance(tmr, (int, float)) and tmr >= 0:
                        tower.research_timer = float(tmr)
                    if isinstance(ttl, (int, float)) and ttl > 0:
                        tower.research_total = float(ttl)
                # 排队列表：仅保留已知节点、去重、剔除已解锁
                unlocked = set((data.get("tech") or {}).get("unlocked", []))
                seen = set()
                for qid in (rd.get("queue") or []):
                    if not isinstance(qid, str) or qid not in tree.nodes:
                        print(f"[存档] 跳过未知排队科技: {qid!r}")
                        continue
                    if qid in seen or qid in unlocked:
                        continue
                    seen.add(qid)
                    tower.research_queue.append(qid)
        towers.append(tower)
    return towers


def _restore_base_research(game, tech_data):
    """恢复基地手研通道（station=false 科技，无需研究塔；v11+，旧档缺省为空）。

    只接受已知节点；排队去重、剔除已解锁，与塔 research 段同规则。
    """
    br = getattr(game, "base_research", None)
    if br is None:
        return
    import tech_tree as tt
    tree = tt.tech_tree()
    rd = tech_data.get("base_research") or {}
    if not isinstance(rd, dict):
        return
    cur = rd.get("current")
    if isinstance(cur, str) and cur in tree.nodes:
        br.research_current = cur
        tmr = rd.get("timer", 0.0)
        ttl = rd.get("total", 0.0)
        if isinstance(tmr, (int, float)) and tmr >= 0:
            br.research_timer = float(tmr)
        if isinstance(ttl, (int, float)) and ttl > 0:
            br.research_total = float(ttl)
    # 排队列表：仅保留已知节点、去重、剔除已解锁
    unlocked = set((tech_data.get("unlocked") or []))
    seen = set()
    for qid in (rd.get("queue") or []):
        if not isinstance(qid, str) or qid not in tree.nodes:
            print(f"[存档] 跳过未知排队科技: {qid!r}")
            continue
        if qid in seen or qid in unlocked:
            continue
        seen.add(qid)
        br.research_queue.append(qid)


def _restore_belts(data, belts_data, universe):
    """从存档重建传送带列表。未知类型跳过+警告；y 由地表重算。"""
    from belts import Belt
    belts = []
    for bd in belts_data:
        kind = bd.get("kind", "")
        if kind not in ("belt", "splitter", "bridge"):
            print(f"[存档] 跳过未知传送带类型: {kind!r}")
            continue
        try:
            cell = (int(bd["cell"][0]), int(bd["cell"][1]))
        except (KeyError, TypeError, ValueError, IndexError):
            continue
        direction = int(bd.get("direction", 1)) % 4
        y = float(universe.surface_height_at(cell[0], cell[1]) + 1)
        belt = Belt(kind, direction, cell, y)
        belt.counter = int(bd.get("counter", 0) or 0)
        if kind == "bridge":
            lanes_data = bd.get("lanes") or []
            for li, lane_data in enumerate(lanes_data):
                if li >= len(belt.lanes):
                    break
                items = []
                for it in lane_data.get("items") or ():
                    try:
                        items.append({"name": str(it[0]), "head": float(it[1])})
                    except (TypeError, ValueError, IndexError):
                        continue
                belt.lanes[li]["items"] = items
        else:
            items = []
            for it in bd.get("items") or ():
                try:
                    items.append({"name": str(it[0]), "head": float(it[1])})
                except (TypeError, ValueError, IndexError):
                    continue
            belt.items = items
        belts.append(belt)
    return belts


def _restore_wires(data, wires_data, universe):
    """从存档重建电线列表。未知类型跳过+警告；y 由地表重算。"""
    from power import CableSegment
    wires = []
    for wd in wires_data:
        kind = wd.get("kind", "")
        if kind not in gamedata.WIRES_RAW:
            print(f"[存档] 跳过未知电线类型: {kind!r}")
            continue
        try:
            cell = (int(wd["cell"][0]), int(wd["cell"][1]))
        except (KeyError, TypeError, ValueError, IndexError):
            continue
        y = float(universe.surface_height_at(cell[0], cell[1]) + 1)
        wire = CableSegment(kind, cell, y)
        wire.trip_timer = float(wd.get("trip_timer", 0.0) or 0.0)
        wire.tripped = bool(wd.get("tripped", False))
        wires.append(wire)
    return wires


def _restore_enemies(data, universe, enemies_data, pathfinder=None, towers=None):
    """从存档重建敌人列表。未知类型/路径越界兜底。

    towers: 已还原的塔列表，用于把 attack_target 索引还原成引用（BUG-070）。
    """
    from entities import ENEMY_TYPES, Enemy
    enemies = []
    paths = universe.enemy_paths
    for ed in enemies_data:
        type_name = ed.get("type_name", "")
        et = ENEMY_TYPES.get(type_name)
        if et is None:
            print(f"[存档] 跳过未知敌人类型: {type_name}")
            continue
        try:
            pos = tuple(ed.get("position", [0, 0, 0]))
        except (TypeError, ValueError):
            continue
        path_ref = ed.get("path_ref", 0)
        if not isinstance(path_ref, (int, float)):
            path_ref = -1
        # 变体集优先：读档敌人应挂在它实际使用的变体路径上，而非主线。
        # 主线与变体点集不同，把保存的 path_index 直接套到主线上会跳点/跟错路
        # （原实现只存 path_ref=home_spawner_id，读档总是挂主线）。
        sid = ed.get("home_spawner_id", -1)
        if not isinstance(sid, (int, float)):
            sid = -1
        sid = int(sid)
        behavior = ed.get("behavior", "wave")
        path = None
        all_paths = []
        # 侦察怪（BUG-075）：活体侦察怪由 Enemy.as_scout 用 path=None、
        # all_paths=[None] 构造（entities.py），路径字段对它无意义——移动只看
        # wander_heading/return_path。这里同样跳过变体/主线解析，保持 path=None。
        # 围攻/挖掘（attack_tower/dig）虽然不靠 path 移动，但它们交战前是沿
        # 本刷怪点路线行走的，且回到 wave 的状态迁移不会重寻路（BUG-127）：
        # 不给它们恢复路径会让读档后 path_points=()，下一帧被误判"抵达大本营"
        # 而直接移除。因此除侦察怪外一律恢复变体/主线（含 attack_tower/dig）。
        is_scout = behavior in ("scout_wander", "scout_return")
        needs_path = not is_scout
        if needs_path and pathfinder is not None:
            variant_entry = pathfinder.variants.get((sid, type_name))
            if variant_entry:
                variants = variant_entry.get("paths") or []
                # 仅当敌人存档时真的走在变体路径上才挂变体（BUG-076）。
                # 旧档无 on_variant 字段（缺省 False）一律走主线兜底。
                if variants and ed.get("on_variant", False):
                    pv = ed.get("path_variant", 0)
                    if not isinstance(pv, int) or not (0 <= pv < len(variants)):
                        pv = 0
                    path = variants[pv]
                    all_paths = variants
        if needs_path and path is None:
            # 主线敌人：只挂自己刷怪点的主线（BUG-077），不再展开全部刷怪点路线。
            own = paths.get(int(path_ref))
            if own is not None:
                path = own
                all_paths = [own]
            else:
                # 自己刷怪点主线缺失（旧/残缺档）→ 丢弃该敌人（BUG-078）：
                # 绝不路由到别的刷怪点路线（原 next(iter(paths.values())) 会走错路）；
                # 也不给 None 路径的 wave 怪（会因 path_index>=0 立刻判"抵达大本营"）。
                continue
        path_index = 0 if path is None else ed.get("path_index", 0)
        if path is not None and path.points:
            # BUG-103：存档里的 path_index 可能来自更长的历史/变体路径，
            # 恢复出的路径较短时越界 → 敌人直接进"到达大本营"分支、位置
            # 又低于基地层无法结算，读档后永久站立不走。夹到合法范围。
            path_index = min(path_index, len(path.points) - 1)
        # R3-H1：读档保持"原地等待"（BUG-105，无解重寻路清空路径的标记）。
        # 此时不恢复任何路径（path_points=()，同活体等待态语义），否则敌人
        # 恢复出主线末段、walking 态被动走向大本营，走完末段即被结算
        # reached_base。地形编辑（world_version bump）会触发重估自愈。
        waiting_for_path = bool(ed.get("waiting_for_path", False))
        if waiting_for_path and needs_path:
            path = None
            all_paths = []
            path_index = 0
        # 注入 universe 的种子随机源（BUG-131）：不传 rng 会让 Enemy 内部回退到
        # 模块级 random，破坏活体敌人（game 传入 universe.rng）的种子可复现性。
        # 个体 wobble 参数另行从存档覆盖（下方），此处只保证运行时随机流一致。
        # #4 读档 RNG 脱同步：不能直接把 universe.rng 传给构造器——Enemy.__init__
        # 会消费 8 个随机数（wobble_amp/freq/phase/time/path_noise/repath_timer/
        # wander_heading），而这些值随后被存档覆盖；若用 universe.rng 构造，
        # 读档后随机流被额外推进 N×8 步，与"存档点继续在线播放"脱同步。
        # 因此构造用一次性独立 rng（只生成会被覆盖的占位值），之后再把手动
        # 选择的种子源挂回 enemy.rng，运行时决策由此继续从 universe.rng 取数。
        enemy = Enemy(et, pos, path,
                      all_paths=(all_paths if path is not None else None),
                      path_index=path_index,
                      rng=random.Random())
        enemy.rng = universe.rng
        # 覆盖所有个体属性
        enemy.health = ed.get("health", enemy.health)
        enemy.max_health = ed.get("max_health", enemy.max_health)
        enemy.speed = ed.get("speed", enemy.speed)
        enemy.reward = ed.get("reward", enemy.reward)
        # #2 掉落波次口径：旧档无该字段时回落默认 0（击杀掉落回落全局 wave）。
        enemy.spawn_wave = ed.get("spawn_wave", 0)
        enemy.active = ed.get("active", True)
        enemy.slow_timer = ed.get("slow_timer", 0.0)
        enemy.slow_factor = ed.get("slow_factor", 1.0)
        enemy.wobble_amp = ed.get("wobble_amp", enemy.wobble_amp)
        enemy.wobble_freq = ed.get("wobble_freq", enemy.wobble_freq)
        enemy.wobble_phase = ed.get("wobble_phase", enemy.wobble_phase)
        enemy.time = ed.get("time", enemy.time)
        enemy.path_noise = ed.get("path_noise", enemy.path_noise)
        personality = ed.get("personality")
        if personality and isinstance(personality, dict):
            enemy.personality = dict(personality)
        # 非标准侦察兵配置（BUG-071）：还原个体 kind 参数，避免退回标准侦察兵
        sc = ed.get("scout_cfg")
        if sc and isinstance(sc, dict):
            enemy.scout_cfg = dict(sc)
        # 行为状态机字段（v2+，旧存档用 getattr 兜底默认值）
        enemy.behavior = ed.get("behavior", "wave")
        # 攻击目标（BUG-070）：按 game.towers 索引还原引用。不还原会让
        # 攻塔中的敌人读档后 attack_target=None，下一帧弃攻回 wave。
        at_idx = ed.get("attack_target")
        if (enemy.behavior == "attack_tower" and isinstance(at_idx, int)
                and towers is not None and 0 <= at_idx < len(towers)):
            enemy.attack_target = towers[at_idx]
        enemy.home_spawner_id = ed.get("home_spawner_id", -1)
        enemy.has_message = ed.get("has_message", False)
        enemy.scout_returned_with_message = ed.get("scout_returned_with_message", False)
        enemy.wander_heading = ed.get("wander_heading", enemy.wander_heading)
        enemy.wander_turn_timer = ed.get("wander_turn_timer", enemy.wander_turn_timer)
        enemy.wander_lifetime = ed.get("wander_lifetime", 0.0)
        rp = ed.get("return_path", [])
        if rp:
            enemy.return_path = tuple(tuple(pt) for pt in rp)
        enemy.return_path_index = ed.get("return_path_index", 0)
        # 寻路状态（v5+，旧存档用默认值）
        enemy.path_variant = ed.get("path_variant", 0)
        enemy.path_version = ed.get("path_version", -1)
        enemy.attack_timer = ed.get("attack_timer", 0.0)
        dig_t = ed.get("dig_target")
        enemy.dig_target = (int(dig_t[0]), int(dig_t[1])) \
            if isinstance(dig_t, (list, tuple)) and len(dig_t) >= 2 else None
        enemy.dig_progress = ed.get("dig_progress", 0.0)
        enemy.dig_y = ed.get("dig_y")
        enemy.dig_base_y = ed.get("dig_base_y")
        enemy.dig_passed = bool(ed.get("dig_passed", False))
        enemy._dug_count = int(ed.get("dug_count", 0) or 0)
        enemy._dig_marked_passage = bool(ed.get("dig_marked_passage", False))
        enemy.repath_timer = ed.get("repath_timer", 0.0)
        # R3-H1：恢复"原地等待"标记（旧档无此字段默认 False）。标记置位时
        # 上方已把路径清空，保持活体等待态语义。
        enemy.waiting_for_path = waiting_for_path
        enemies.append(enemy)
    return enemies


def _restore_projectiles(data, towers, projectiles_data):
    """从存档重建抛射体列表。"""
    from entities import Projectile
    projectiles = []
    for pd in projectiles_data:
        try:
            pos = tuple(pd.get("position", [0, 0, 0]))
            vel = tuple(pd.get("velocity", [0, 0, 0]))
        except (TypeError, ValueError):
            continue
        damage = pd.get("damage", 0)
        proj = Projectile(pos, vel, damage)
        proj.lifetime = pd.get("lifetime", proj.lifetime)
        proj.active = pd.get("active", True)
        proj.splash_radius = pd.get("splash_radius", 0.0)
        proj.splash_falloff = pd.get("splash_falloff", 0.0)
        proj.slow_factor = pd.get("slow_factor", 1.0)
        proj.slow_duration = pd.get("slow_duration", 0.0)
        proj.bonus_vs = pd.get("bonus_vs")
        proj.bonus_mult = pd.get("bonus_mult", 1.0)
        src_tower = pd.get("source_tower")
        if isinstance(src_tower, int) and 0 <= src_tower < len(towers):
            proj.source = towers[src_tower]
        else:
            proj.source = None
        projectiles.append(proj)
    return projectiles


def _restore_pickups(pickups_data):
    """从存档重建掉落物列表。"""
    from entities import Pickup
    pickups = []
    for pd in pickups_data:
        try:
            pos = tuple(pd.get("position", [0, 0, 0]))
        except (TypeError, ValueError):
            continue
        item_type = pd.get("item_type", "material")
        item_name = pd.get("item_name", "wood")
        amount = pd.get("amount", 1)
        pickup = Pickup(pos, item_type, item_name, amount)
        pickup.active = pd.get("active", True)
        pickup.bob_phase = pd.get("bob_phase", pickup.bob_phase)
        pickup.bob_speed = pd.get("bob_speed", pickup.bob_speed)
        pickup.time = pd.get("time", pickup.time)
        pickups.append(pickup)
    return pickups


def deserialize_game(data: dict):
    """从存档 dict 重建完整 Game 对象。全程 .get() 兜底。失败抛异常由 load_game 捕获。"""
    from world import Universe
    from camera import Camera
    from renderer import Renderer
    from game import Game, GameState

    # 1) 种子 → 重生成 Universe（确定性）
    seed = data.get("seed", config.DEFAULT_SEED)
    universe = Universe(seed)

    # 1.5) v2→v3 迁移：按距离排名重映射刷怪点 id
    if data.get("_v2_to_v3", False):
        _remap_spawner_ids_v2_to_v3(data, universe)

    # 2) 路径覆盖（自包含；旧存档无 paths 段则沿用重生成路径）
    _restore_paths(universe, data.get("paths"))

    # 3) 世界状态恢复
    world_data = data.get("world") or {}
    harvested = world_data.get("harvested_cells", [])
    universe._harvested_cells = set(tuple(c) for c in harvested)
    _restore_terrain_edits(universe, world_data)
    _restore_weather(universe, world_data.get("weather"))
    universe.weather_timer = world_data.get("weather_timer", 0.0)
    _restore_rng(universe, world_data.get("rng_state"))

    # 4) 相机
    camera = Camera(universe)
    _restore_camera(camera, data.get("camera"))

    # 5) 渲染器
    renderer = Renderer()
    renderer.init_3d()

    # 6) 游戏对象（默认状态构造）
    game = Game(universe, camera, renderer)

    # 6.5) 补材料化存档记录过的刷怪点，并恢复其路径。
    #      Game.__init__ → _init_spawners → _materialize_base_spawners 会清空
    #      universe.spawners / enemy_paths，因此必须在 Game 构造之后执行，
    #      否则远处 dormant 刷怪点的 id 不在 universe.spawners 中，
    #      _rebuild_spawner_hash 会 KeyError。
    spawners_data = data.get("spawners") or []
    _restore_spawner_positions(universe, spawners_data)
    _restore_paths(universe, data.get("paths"))
    _restore_variants(game, data.get("variants"))

    # 7) 核心字段
    g = data.get("game") or {}
    game.money = g.get("money", config.STARTING_MONEY)
    game.score = g.get("score", 0)
    game.wave = g.get("wave", 1)
    game.game_time = g.get("game_time", 0.0)

    game.wave_delay = g.get("wave_delay", 3.0)
    game.wave_announcement_timer = g.get("wave_announcement_timer", 0.0)

    # 刷怪点运行时（v2+，dict by spawner_id）
    from game import SpawnerRuntime
    game.spawners = {}
    for sd in spawners_data:
        sid = sd.get("spawner_id", 0)
        rt = SpawnerRuntime(
            spawner_id=sid,
            mode=sd.get("mode", "dormant"),
            wave_num=sd.get("wave_num", 0),
            spawn_timer=sd.get("spawn_timer", 0.0),
            spawn_interval=sd.get("spawn_interval", 0.5),
            enemy_count=sd.get("enemy_count", 0),
            max_enemies=sd.get("max_enemies", 0),
            enemy_types=list(sd.get("enemy_types", [])),
            between_wave=sd.get("between_wave", True),
            between_timer=sd.get("between_timer", 3.0),
            scout_batch_spawned=sd.get("scout_batch_spawned", False),
            scouts_pending=sd.get("scouts_pending", 0),
            message_received=sd.get("message_received", False),
            announcement_timer=sd.get("announcement_timer", 0.0),
        )
        game.spawners[sid] = rt
    game._spawner_hash_dirty = True

    # 8) 大本营
    base_data = data.get("base") or {}
    game.base.max_health = base_data.get("max_health", game.base.max_health)
    game.base.health = base_data.get("health", game.base.health)

    # 9) 背包
    inv = data.get("inventory") or {}
    game.inventory.materials = dict(inv.get("materials", {}))
    game.inventory.weapons = dict(inv.get("weapons", {}))
    game.inventory.tools = set(inv.get("tools", []))

    # 10) 科技（v10+：研究状态按塔恢复，见 _restore_towers 的 research 段；
    #     v11+：基地手研通道 base_research）
    tech = data.get("tech") or {}
    game.unlocked_tech = set(tech.get("unlocked", []))
    _restore_base_research(game, tech)

    # 11) 实体
    game.towers = _restore_towers(data, data.get("towers") or [])
    # 读档后重新登记塔格（BUG-017）：Universe(seed) 的 _tower_cells/_wall_cells
    # 为空，寻路读这两个集合，不重登记会让敌人穿塔、城墙失效。
    for _t in game.towers:
        if not getattr(_t, "active", True):
            continue
        if getattr(_t, "is_mining", False):
            for _cx, _cz in _t.footprint_cells():
                universe.add_tower_cell(_cx, _cz, is_wall=_t.no_attack)
        else:
            universe.add_tower_cell(math.floor(_t.position[0]),
                                    math.floor(_t.position[2]),
                                    is_wall=_t.no_attack)
    # 重戳变体缓存版本号（BUG-134）：上面的 add_tower_cell 会推进
    # universe.world_version，而 pathfinder._seen_version 仍是构造时的 -1。
    # 读档后首次 begin_frame() 会因 -1 != world_version 触发 invalidate()，
    # 把 _restore_variants 刚按旧 version 存入的变体全部标脏、每帧重算一遍，
    # 使存档里的 variants 永远不生效。塔/地形与变体计算时完全一致，重戳合法。
    _pf = getattr(game, "pathfinder", None)
    if _pf is not None:
        _pf._seen_version = game.universe.world_version
        for _entry in _pf.variants.values():
            _entry["version"] = game.universe.world_version
    game.belts = _restore_belts(data, data.get("belts") or [], universe)
    game._belt_by_cell = {b.cell: b for b in game.belts}
    game.wires = _restore_wires(data, data.get("wires") or [], universe)
    game._wire_by_cell = {w.cell: w for w in game.wires}
    game.enemies = _restore_enemies(data, universe, data.get("enemies") or [],
                                    pathfinder=game.pathfinder,
                                    towers=game.towers)
    game.projectiles = _restore_projectiles(data, game.towers, data.get("projectiles") or [])
    game.pickups = _restore_pickups(data.get("pickups") or [])

    # 12) 重置交互态
    game.state = GameState.PLAYING
    game.placing_tower = False
    game.selected_tower_type = None
    game.placement_position = None
    game.interaction_mode = "none"
    game.selected_weapon = None
    game.placing_belt = False
    game.selected_belt = None
    game.belt_direction = 1
    game.placing_wire = False
    game.selected_power = None
    game.power_kind_sel = "wire"
    game.power_state = None
    game.craft_panel_open = False
    game.craft_timer = 0.0
    game.craft_action = None
    game.toast_message = None
    game.toast_timer = 0.0
    game.fps = 0.0
    game.frame_count = 0
    game._fps_window_start = time.perf_counter()

    return game


# ─── 版本迁移 ────────────────────────────────────────────────

def migrate_v2_to_v3(data: dict) -> dict:
    """将 v2 存档（list-index 刷怪点 id）标记为 v3（粗格编码 id）。

    v2 的 spawners 是 list，id=0..n（距离序），enemy_paths 是 list（按序对齐）。
    v3 的 spawners 用粗格编码 id，enemy_paths 是 dict by spawner_id。
    迁移：给 paths 加 spawner_id（按 list 序），标记 _v2_to_v3，
    实际 id 重映射在 deserialize_game 中按距离排名完成。
    """
    # 给 paths 加 spawner_id（按 list 序对齐）
    paths = data.get("paths", [])
    for i, pd in enumerate(paths):
        pd.setdefault("spawner_id", i)
    # 标记迁移，供 deserialize_game 做 id 重映射
    data["_v2_to_v3"] = True
    data["version"] = 3
    return data


def _remap_spawner_ids_v2_to_v3(data: dict, universe):
    """v2→v3 id 重映射：按距离排名将 list-order id (0..n) 映射到粗格编码 id。"""
    # 按距离排序新的材料化刷怪点
    new_spawners = sorted(
        universe.spawners.items(),
        key=lambda kv: (kv[1].x - universe.base_x) ** 2
                       + (kv[1].z - universe.base_z) ** 2)
    new_ids_by_rank = [sid for sid, _ in new_spawners]

    # 构建 old_id → new_id 映射（按距离排名）
    old_to_new = {}
    spawners_data = data.get("spawners", [])
    for i, sd in enumerate(spawners_data):
        old_id = sd.get("spawner_id", i)
        if i < len(new_ids_by_rank):
            old_to_new[old_id] = new_ids_by_rank[i]

    # 重映射 spawners
    for sd in spawners_data:
        old_id = sd.get("spawner_id", 0)
        sd["spawner_id"] = old_to_new.get(old_id, old_id)

    # 重映射 paths
    for pd in data.get("paths", []):
        old_id = pd.get("spawner_id", 0)
        pd["spawner_id"] = old_to_new.get(old_id, old_id)

    # 重映射 enemies
    for ed in data.get("enemies", []):
        old_sid = ed.get("home_spawner_id", -1)
        if old_sid in old_to_new:
            ed["home_spawner_id"] = old_to_new[old_sid]
        old_pr = ed.get("path_ref", 0)
        if old_pr in old_to_new:
            ed["path_ref"] = old_to_new[old_pr]


def migrate_v3_to_v4(data: dict) -> dict:
    """将 v3 存档迁移到 v4（科技树 v2 数据，旧科技 ID 别名映射）。

    v3 的 tech.unlocked 是旧科技 ID 列表（如 "flint_knapping"）。
    v4 使用新突破点 ID（如 "stone:B1"），通过 gamedata.TECH_LEGACY_MAP 映射。
    未在映射中的旧 ID 丢弃并打印警告。
    """
    tech = data.get("tech", {})
    if not tech:
        data["version"] = 4
        return data

    # 导入 legacy map
    import gamedata
    legacy_map = gamedata.TECH_LEGACY_MAP

    # 映射 unlocked 列表
    old_unlocked = tech.get("unlocked", [])
    new_unlocked = []
    for old_id in old_unlocked:
        new_id = legacy_map.get(old_id)
        if new_id:
            new_unlocked.append(new_id)
        else:
            print(f"[存档] v3→v4: 未知科技 ID '{old_id}'，已丢弃")
    tech["unlocked"] = sorted(set(new_unlocked))

    # 映射 researching
    old_researching = tech.get("researching")
    if old_researching:
        new_researching = legacy_map.get(old_researching)
        if new_researching:
            tech["researching"] = new_researching
        else:
            print(f"[存档] v3→v4: 未知研究中科技 '{old_researching}'，已清空")
            tech["researching"] = None

    data["version"] = 4
    return data


def migrate_v4_to_v5(data: dict) -> dict:
    """将 v4 存档迁移到 v5（寻路系统）。

    v5 新增：地形编辑覆盖层 / 变体路径 / 敌人攻击挖掘状态 / 塔体 HP。
    结构兼容：缺失段全部 .get() 兜底——
      - 地形编辑缺省 = 无挖掘痕迹（v4 世界本来就没有）；
      - 变体路径缺省 = 各 (刷怪点×类型) 首次请求时重新计算；
      - 敌人 path_version 缺省 -1 → 加载后首帧就近重估；
      - 塔 hp 缺省满血。
    """
    data["version"] = 5
    return data


def migrate_v5_to_v6(data: dict) -> dict:
    """将 v5 存档迁移到 v6（加工塔）。

    v6 新增：processing 存物/在制任务/手动配方字段（旧档缺字段宽松兜底为空）。
    结构兼容：缺失字段由 _restore_towers .get() 兜底，此处仅推进版本号。
    """
    data["version"] = 6
    return data


def migrate_v6_to_v7(data: dict) -> dict:
    """将 v6 存档迁移到 v7（传送带物流）。

    v7 新增：game.belts 列表 + 塔 belt_export 导出白名单（旧档缺省为空）。
    结构兼容：缺失段由 _restore_belts / _restore_towers .get() 兜底。
    """
    data["belts"] = data.get("belts") or []
    data["version"] = 7
    return data


def migrate_v7_to_v8(data: dict) -> dict:
    """将 v7 存档迁移到 v8（电力系统）。

    v8 新增：game.wires 列表（电线）+ 电力塔（电站/电池）燃料桶/电池储能字段。
    结构兼容：缺失段由 _restore_wires / _restore_towers .get() 兜底。
    """
    data["wires"] = data.get("wires") or []
    data["version"] = 8
    return data


def migrate_v8_to_v9(data: dict) -> dict:
    """将 v8 存档迁移到 v9（开采塔）。

    v9 新增：塔 mining 段（足迹/竖井前沿与进度）。旧档开采塔不存在，
    此段缺省即可，无需改动（_restore_towers .get() 兜底为空）。
    """
    data["version"] = 9
    return data


def migrate_v9_to_v10(data: dict) -> dict:
    """将 v9 存档迁移到 v10（按塔研究）。

    v10 结构性变化：研究状态从全局 tech 段迁到各研究塔的 research 段
    （current/timer/total/queue 队列）。旧档只有一个全局在研槽：
      - 有研究塔 → 写入第一座研究塔的 research.current（保留剩余计时）；
      - 无研究塔 → 丢弃并打印警告（旧语义下该研究本来也靠研究塔承载）。
    旧 tech 段 researching/research_timer/research_total 字段删除。
    """
    tech = data.get("tech") or {}
    old_researching = tech.get("researching")
    if old_researching:
        target = None
        for td in data.get("towers") or []:
            tname = td.get("type_name", "")
            from entities import TOWER_TYPES
            ttdef = TOWER_TYPES.get(tname)
            if ttdef is not None and int(getattr(ttdef, "research_attr", 0) or 0) > 0:
                target = td
                break
        if target is not None:
            target["research"] = {
                "current": old_researching,
                "timer": float(tech.get("research_timer", 0.0) or 0.0),
                "total": float(tech.get("research_total", 0.0) or 0.0),
                "queue": [],
            }
            print(f"[存档] v9→v10: 全局研究 {old_researching!r} 迁入研究塔")
        else:
            print(f"[存档] v9→v10: 无研究塔，丢弃在研科技 {old_researching!r}")
    # 删除旧全局研究字段
    for k in ("researching", "research_timer", "research_total"):
        tech.pop(k, None)
    data["version"] = 10
    return data


def migrate_v10_to_v11(data: dict) -> dict:
    """将 v10 存档迁移到 v11（基地手研通道）。

    v11 新增：tech.base_research（station=false 科技，无需研究塔）。
    纯增量字段：旧档缺省为空（_restore_base_research .get() 兜底），无破坏。
    """
    data["version"] = 11
    return data


def migrate_v1_to_v2(data: dict) -> dict:
    """将 v1 存档（全局波次）迁移到 v2（刷怪点独立波次）。

    v1 的 wave_state 映射到 spawners[0]（最近基地的刷怪点）：
    - between_waves / wave_timer → spawners[0].between_wave / between_timer
    - wave_enemy_count / max_enemies → spawners[0].enemy_count / max_enemies
    - wave_enemy_types → spawners[0].enemy_types
    - wave (全局) → spawners[0].wave_num
    其余刷怪点保持 dormant 初始状态。
    """
    g = data.get("game", {})
    ws = g.get("wave_state")
    if ws is None:
        return data  # 已是 v2 或无 wave_state，无需迁移

    wave = g.get("wave", 1)
    g.pop("wave_state", None)
    g["wave_delay"] = ws.get("wave_delay", 3.0)
    g["wave_announcement_timer"] = ws.get("wave_announcement_timer", 0.0)

    # 构造 spawners[0]（最近基地的刷怪点，活跃态）
    between_wave = ws.get("between_waves", True)
    rt0 = {
        "spawner_id": 0,
        "mode": "active",
        "wave_num": wave,
        "spawn_timer": ws.get("wave_spawn_timer", 0.0),
        "spawn_interval": ws.get("wave_spawn_interval", 0.5),
        "enemy_count": ws.get("wave_enemy_count", 0),
        "max_enemies": ws.get("wave_max_enemies", 0),
        "enemy_types": list(ws.get("wave_enemy_types", [])),
        "between_wave": between_wave,
        "between_timer": ws.get("wave_timer", 0.0),
        "scout_batch_spawned": False,
        "scouts_pending": 0,
        "message_received": False,
        "announcement_timer": ws.get("wave_announcement_timer", 0.0),
    }
    data["spawners"] = [rt0]
    data["version"] = 2
    return data


# ─── 文件操作 ────────────────────────────────────────────────

def _auto_name(seed) -> str:
    ts = time.strftime("%Y%m%d_%H%M%S")
    return f"save_{ts}_{seed}.json"


def save_game(game, name=None) -> str:
    """保存游戏到 JSON。name=None 自动命名。返回文件绝对路径。"""
    ensure_save_dir()
    data = serialize_game(game)
    if name is None:
        name = _auto_name(game.universe.seed)
    elif not name.endswith(".json"):
        name += ".json"
    path = os.path.join(SAVE_DIR, name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    size = os.path.getsize(path)
    print(f"[存档] 已保存: {name} ({size / 1024:.1f} KB)")
    return path


def load_game(path) -> "Game | None":
    """从 JSON 加载游戏。失败返回 None。"""
    if not os.path.isfile(path):
        print(f"[存档] 文件不存在: {path}")
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        print(f"[存档] JSON 损坏: {path} ({e})")
        return None
    except OSError as e:
        print(f"[存档] 读取失败: {path} ({e})")
        return None

    # 合法 JSON 但不是对象（数组/字符串等）：按契约失败返回 None，
    # 避免 data.get() 抛未捕获 AttributeError
    if not isinstance(data, dict):
        print(f"[存档] 存档内容不是对象: {path}")
        return None

    # 版本检查与迁移
    ver = data.get("version", 0)
    if ver > config.SAVE_VERSION:
        print(f"[存档] 警告: 存档版本 v{ver} > 当前 v{config.SAVE_VERSION}，尽力加载")
    elif ver < config.SAVE_VERSION:
        if ver == 1:
            data = migrate_v1_to_v2(data)
        if data.get("version", 1) == 2:
            data = migrate_v2_to_v3(data)
        if data.get("version", 1) == 3:
            data = migrate_v3_to_v4(data)
        if data.get("version", 1) == 4:
            data = migrate_v4_to_v5(data)
        if data.get("version", 1) == 5:
            data = migrate_v5_to_v6(data)
        if data.get("version", 1) == 6:
            data = migrate_v6_to_v7(data)
        if data.get("version", 1) == 7:
            data = migrate_v7_to_v8(data)
        if data.get("version", 1) == 8:
            data = migrate_v8_to_v9(data)
        if data.get("version", 1) == 9:
            data = migrate_v9_to_v10(data)
        if data.get("version", 1) == 10:
            data = migrate_v10_to_v11(data)

    # BUG-098 存档兼容：刷怪点 id 编码变更后，旧档 id 需重映射为新编码，
    # 否则 _restore_spawner_positions 会因 id 不符丢弃刷怪点。已是新编码则跳过。
    if _spawner_ids_need_reencode(data):
        _reencode_spawner_ids(data)

    try:
        game = deserialize_game(data)
    except Exception as e:
        print(f"[存档] 反序列化失败: {path} ({type(e).__name__}: {e})")
        return None
    return game


def list_saves() -> list:
    """列出所有存档，按修改时间倒序。每项含 name/path/wave/seed/created_at/size。"""
    ensure_save_dir()
    results = []
    for fname in os.listdir(SAVE_DIR):
        if not fname.endswith(".json"):
            continue
        fpath = os.path.join(SAVE_DIR, fname)
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("存档内容不是对象")
            # 用 `or {}` 而非默认 {}：'game' 键存在但为 null/非字典时，
            # .get 返回该值，链式 .get('wave') 会抛 AttributeError（BUG-069）
            wave = (data.get("game") or {}).get("wave", 0) if isinstance(
                data.get("game"), dict) else 0
            seed = data.get("seed", 0)
            # R1-A3：created_at 非数值（如字符串）时不得入库——与浮点并存时
            # 下方 sort 混合比较 TypeError；单存档时 format_time→time.localtime(str)
            # 也 TypeError → 入口浏览器/F8 菜单崩溃。非数值回落文件 mtime。
            created_at = data.get("created_at", None)
            if not isinstance(created_at, (int, float)) \
                    or isinstance(created_at, bool):
                created_at = None
        except (json.JSONDecodeError, OSError, KeyError, ValueError, TypeError):
            wave = 0
            seed = 0
            created_at = None
        # R2-F1：stat 阶段（getsize/getmtime）与坏 JSON 同容错——open 读到
        # 内容与下方 stat 之间文件被外部删除/锁（外部清理、杀毒扫描）即抛
        # 未捕获 OSError，沿 _open_save_menu/SeedScreen 冒泡崩整个存档浏览器。
        # 同函数对坏 JSON/非 dict 存档都有"跳过坏文件"容错，唯独 stat 漏保护。
        try:
            if created_at is None:
                created_at = os.path.getmtime(fpath)
            size = os.path.getsize(fpath)
        except OSError:
            continue  # 文件刚被外部删除/锁住：跳过该项，不崩浏览器
        results.append({
            "name": fname,
            "path": fpath,
            "wave": wave,
            "seed": seed,
            "created_at": created_at,
            "size": size,
        })
    results.sort(key=lambda x: x["created_at"], reverse=True)
    return results


def delete_save(path) -> bool:
    """删除存档文件。返回是否成功。"""
    try:
        os.remove(path)
        return True
    except OSError as e:
        print(f"[存档] 删除失败: {path} ({e})")
        return False


def format_time(ts: float) -> str:
    """时间戳 → 人类可读（存档列表显示用）。"""
    if not ts:
        return "?"
    return time.strftime("%m-%d %H:%M", time.localtime(ts))
