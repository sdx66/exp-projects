"""
save_system.py - 纯 Python 存档系统（零外部依赖，仅 json/os/time/random）

格式: JSON，自包含路径数据（不依赖 Universe 重生成顺序）。
兼容: 反序列化一律 .get(key, default) —— 存档多出的字段忽略，缺少的字段兜底。
版本: config.SAVE_VERSION，未来加 migrate(data) 做版本升级。

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
        # 防御塔挂载的武器（v6+；旧档缺省为空）
        "mounted_weapons": [
            {"weapon": m["wdef"].name, "damage_mult": m["damage_mult"]}
            for m in getattr(t, "weapon_mounts", [])
        ],
    }


def _serialize_enemy(e) -> dict:
    return {
        "type_name": e.type_name,
        "position": list(e.position),
        "path_ref": getattr(e, "home_spawner_id", -1),
        "path_index": e.path_index,
        "health": e.health,
        "max_health": e.max_health,
        "speed": e.speed,
        "reward": e.reward,
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
        "path_version": getattr(e, "path_version", -1),
        "attack_timer": getattr(e, "attack_timer", 0.0),
        "dig_target": list(getattr(e, "dig_target", None) or []),
        "dig_progress": getattr(e, "dig_progress", 0.0),
        "dig_y": getattr(e, "dig_y", None),
        "dig_base_y": getattr(e, "dig_base_y", None),
        "dig_passed": bool(getattr(e, "dig_passed", False)),
        "repath_timer": getattr(e, "repath_timer", 0.0),
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
        "enemies": [_serialize_enemy(e) for e in game.enemies],
        "projectiles": projectiles,
        "pickups": [_serialize_pickup(p) for p in game.pickups],
        "tech": {
            "unlocked": sorted(game.unlocked_tech),
            "researching": game.tech_researching,
            "research_timer": game.tech_research_timer,
            "research_total": game.tech_research_total,
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
            # 存档 id 与坐标不符（旧格式/损坏），跳过
            continue
        universe._materialized_coarse.add((gx, gz))
        universe._materialize_spawner_at_coarse(gx, gz)


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
            points = tuple(tuple(pt) for pt in vd.get("points", []))
        except (TypeError, ValueError):
            continue
        if not points or not type_name:
            continue
        dig_cells = frozenset(
            (int(c[0]), int(c[1]))
            for c in vd.get("dig_cells", [])
            if isinstance(c, (list, tuple)) and len(c) >= 2)
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
    from items import CLASSIC_TOWER_MATERIALS, WEAPON_TYPES
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
        if mat_recipe is None:
            tower.material_recipe = dict(CLASSIC_TOWER_MATERIALS.get(type_name, {}))
        else:
            tower.material_recipe = dict(mat_recipe)
        # 防御塔挂载武器（v6+；旧档无此字段 → 空列表，兼容）
        for mw in td.get("mounted_weapons") or []:
            wdef = WEAPON_TYPES.get(mw.get("weapon"))
            if wdef is None:
                print(f"[存档] 跳过未知挂载武器: {mw.get('weapon')!r}")
                continue
            if not tower.mount_weapon(wdef,
                                      damage_mult=float(mw.get("damage_mult", 1.0))):
                break  # 槽位已满：不再挂更多
        towers.append(tower)
    return towers


def _restore_enemies(data, universe, enemies_data):
    """从存档重建敌人列表。未知类型/路径越界兜底。"""
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
        if isinstance(path_ref, (int, float)):
            path = paths.get(int(path_ref))
        else:
            path = None
        if path is None and paths:
            path = next(iter(paths.values()))
        path_index = ed.get("path_index", 0)
        if path is None:
            path_index = 0
        all_paths = list(paths.values()) if paths else []
        enemy = Enemy(et, pos, path, all_paths=all_paths, path_index=path_index)
        # 覆盖所有个体属性
        enemy.health = ed.get("health", enemy.health)
        enemy.max_health = ed.get("max_health", enemy.max_health)
        enemy.speed = ed.get("speed", enemy.speed)
        enemy.reward = ed.get("reward", enemy.reward)
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
        # 行为状态机字段（v2+，旧存档用 getattr 兜底默认值）
        enemy.behavior = ed.get("behavior", "wave")
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
        enemy.repath_timer = ed.get("repath_timer", 0.0)
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

    # 10) 科技
    tech = data.get("tech") or {}
    game.unlocked_tech = set(tech.get("unlocked", []))
    game.tech_researching = tech.get("researching")
    game.tech_research_timer = tech.get("research_timer", 0.0)
    game.tech_research_total = tech.get("research_total", 0.0)

    # 11) 实体
    game.towers = _restore_towers(data, data.get("towers") or [])
    game.enemies = _restore_enemies(data, universe, data.get("enemies") or [])
    game.projectiles = _restore_projectiles(data, game.towers, data.get("projectiles") or [])
    game.pickups = _restore_pickups(data.get("pickups") or [])

    # 12) 重置交互态
    game.state = GameState.PLAYING
    game.placing_tower = False
    game.selected_tower_type = None
    game.placement_position = None
    game.interaction_mode = "none"
    game.selected_weapon = None
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
            wave = data.get("game", {}).get("wave", 0)
            seed = data.get("seed", 0)
            created_at = data.get("created_at", os.path.getmtime(fpath))
        except (json.JSONDecodeError, OSError, KeyError):
            wave = 0
            seed = 0
            created_at = os.path.getmtime(fpath)
        size = os.path.getsize(fpath)
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
