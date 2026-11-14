"""
settings.py - 玩家设置持久化（零外部依赖，仅 json/os）

与存档系统（save_system.py）分离：存档存"对局状态"，本模块存"玩家偏好"，
跨对局生效，与具体世界/种子无关。

- 文件位置：<项目根>/settings.json（与 tower_defense/ 同级）
- 读取：load() → dict（缺失/损坏/未知字段一律 .get() 兜底为默认值）
- 写入：save(settings) 原子写（先写临时文件再替换，避免中途崩溃损坏）

设计原则：
  - 键集合由 DEFAULTS 唯一确定；未知键读取时丢弃、写入时也不会产生。
  - 布尔键强制 bool、数值键夹到定义区间，防止手改 JSON 填入非法值
    导致渲染异常（如透明度 >1 或负数）。
"""

import json
import os

# ui_config 是 UI 层常量模块（无重依赖）；minimap 默认值以其为单一事实来源，
# 避免两处各写一份 True。ui_config 不可用时（极少）兜底 True。
try:
    import ui_config as _ui_config
    _MINIMAP_DEFAULT = bool(getattr(_ui_config, "MINIMAP_ENABLED_DEFAULT", True))
except Exception:
    _MINIMAP_DEFAULT = True

# ─── 默认值（唯一事实来源） ──────────────────────────────────
# 显示：进攻路线主视图开关（独立于小地图，见 ui_minimap 自身开关）
DEFAULT_SHOW_ATTACK_ROUTES = True
# 显示：小地图开关（原 ui_config.MINIMAP_ENABLED_DEFAULT 语义，迁入设置持久化）
DEFAULT_MINIMAP_ENABLED = _MINIMAP_DEFAULT
# 视觉：进攻路线透明度 0..1（仅影响主视图路径带；0 等效隐藏但仍受总开关约束）
DEFAULT_ROUTE_OPACITY = 0.40

DEFAULTS = {
    "show_attack_routes": DEFAULT_SHOW_ATTACK_ROUTES,
    "minimap_enabled": DEFAULT_MINIMAP_ENABLED,
    "route_opacity": DEFAULT_ROUTE_OPACITY,
}

# 数值键的合法区间（夹取用）
_RANGE = {
    "route_opacity": (0.0, 1.0),
}

# 文件位置：<项目根>/settings.json
_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_HERE)
SETTINGS_PATH = os.path.join(_PROJECT_ROOT, "settings.json")


def _coerce(key, value):
    """按 DEFAULTS 的类型把 value 归一：布尔键强制 bool，数值键夹到区间。"""
    default = DEFAULTS[key]
    if isinstance(default, bool):
        return bool(value)
    if isinstance(default, (int, float)):
        try:
            num = float(value)
        except (TypeError, ValueError):
            return default
        if key in _RANGE:
            lo, hi = _RANGE[key]
            num = max(lo, min(hi, num))
        # 整数默认值保持整数
        return int(num) if isinstance(default, int) else num
    return value if value is not None else default


def load():
    """读取设置；文件缺失/损坏/字段非法时逐键兜底为默认值。永不抛异常。"""
    result = dict(DEFAULTS)
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return result
    if not isinstance(data, dict):
        return result
    for key in DEFAULTS:
        if key in data:
            result[key] = _coerce(key, data[key])
    return result


def save(settings):
    """原子写入设置（先写 .tmp 再替换）。失败打印告警但不抛异常。"""
    # 只写已知键，且按 _coerce 归一，保证落盘内容永远合法
    clean = {key: _coerce(key, settings.get(key, DEFAULTS[key]))
             for key in DEFAULTS}
    tmp = SETTINGS_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(clean, f, ensure_ascii=False, indent=2)
        os.replace(tmp, SETTINGS_PATH)
    except OSError as e:
        print(f"[设置] 写入失败: {type(e).__name__}: {e}")
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
