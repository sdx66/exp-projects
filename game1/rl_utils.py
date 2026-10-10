"""
rl_utils.py - raylib 6.x 辅助工具

raylib 6.x 使用 cffi 绑定，结构体通过 tuple 直接传入（不解包）。
结构体格式:
  Color:     (r, g, b, a)     - 0-255
  Vector2:   (x, y)           - float
  Vector3:   (x, y, z)        - float
  Rectangle: (x, y, w, h)     - float
  Camera2D:  (ox, oy, tx, ty, rot, zoom)
  Camera3D:  (px, py, pz, tx, ty, tz, ux, uy, uz, fovy, proj)

关键：传入时不解包，直接传 tuple！
  ✓ rl.DrawRectangle(x, y, w, h, color(255,0,0))
  ✗ rl.DrawRectangle(x, y, w, h, *color(255,0,0))
"""

import os
import platform

import raylib as rl


# ─── 颜色 ──────────────────────────────────────────────────

_current_alpha = 1.0


def color(r, g, b, a=255):
    if isinstance(r, tuple):
        r, g, b = r
        a = 255
    # 应用全局透明度
    a = int(a * _current_alpha)
    return (r, g, b, a)


def color_raw(r, g, b, a=255):
    """创建颜色不应用全局透明度"""
    if isinstance(r, tuple):
        r, g, b = r
        a = 255
    return (r, g, b, a)


def color_hex(hex_val: int, a: int = 255):
    r = (hex_val >> 16) & 0xFF
    g = (hex_val >> 8) & 0xFF
    b = hex_val & 0xFF
    return (r, g, b, a)


# ─── 向量 ──────────────────────────────────────────────────

def vec2(x=0.0, y=0.0):
    return (x, y)


def vec3(x=0.0, y=0.0, z=0.0):
    return (x, y, z)


# ─── 矩形 ──────────────────────────────────────────────────

def rect(x=0.0, y=0.0, w=0.0, h=0.0):
    return (x, y, w, h)


# ─── 相机 ──────────────────────────────────────────────────

def camera2d(ox=0.0, oy=0.0, tx=0.0, ty=0.0, rotation=0.0, zoom=1.0):
    return ((ox, oy), (tx, ty), rotation, zoom)


def camera3d(px=0.0, py=0.0, pz=0.0,
             tx=0.0, ty=0.0, tz=0.0,
             ux=0.0, uy=1.0, uz=0.0,
             fovy=45.0, proj=0):
    return ((px, py, pz), (tx, ty, tz), (ux, uy, uz), fovy, proj)


# ─── CJK 字体 ─────────────────────────────────────────────
# raylib 默认字体只含 ASCII 字形,中文会渲染成 '?'。这里加载一个含 CJK 的
# TTF,并把实际用到的码点显式传给 LoadFontEx(传 NULL 只会加载 ASCII 字形)。
# 优先用项目内捆绑的 assets/cn_font.ttf(Noto Sans SC 子集,OFL);找不到时
# 回退到各系统 CJK 字体;都没有则用默认字体(中文仍会变 '?' 但不崩)。

_FONT = None
_FONT_BASE_SIZE = 64  # 字形栅格基准;运行时按 font_size 缩放,10~40px 都清晰
_FILTER_BILINEAR = getattr(rl, "TEXTURE_FILTER_BILINEAR", 1)  # raylib 6.x

try:
    from _cn_codepoints import CODEPOINTS as _CODEPOINTS
except Exception:  # 子集码点表缺失时退化为仅 ASCII
    _CODEPOINTS = list(range(32, 127))


def _candidate_font_paths():
    here = os.path.dirname(os.path.abspath(__file__))
    yield os.path.join(here, "assets", "cn_font.ttf")  # 捆绑字体(优先)
    sys_name = platform.system()
    if sys_name == "Windows":
        # raylib 6.x 的 LoadFontEx 不支持 .ttc(msyh.ttc/simsun.ttc 会静默退回
        # 默认字体),故优先单 .ttf 的中文字体;.ttc 留作兜底,下方 _font_has_cjk
        # 会校验并跳过无效结果。
        for n in ("simhei.ttf", "Deng.ttf", "msyh.ttc", "simsun.ttc"):
            yield os.path.join(r"C:\Windows\Fonts", n)
    elif sys_name == "Darwin":
        yield "/Library/Fonts/Arial Unicode.ttf"  # 单 TTF,装了 Office 才有
        for n in ("PingFang.ttc", "STHeiti Medium.ttc", "Hiragino Sans GB.ttc"):
            yield "/System/Library/Fonts/" + n
    else:  # Linux:多为 .ttc/.otf,raylib 多半不支持,仅作尝试
        for n in (
            "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/wqy-microhei/wqy-microhei.ttc",
            "/usr/share/fonts/wenquanyi/wqy-microhei/wqy-microhei.ttc",
        ):
            yield n


# 用两个不同的常用汉字探测字体是否真含 CJK 字形。raylib 6.x 的 LoadFontEx
# 不支持 .ttc,会把 .ttc 静默退回默认字体(只含 ASCII),此时 GetGlyphIndex
# 对任意中文都返回同一个 notdef 索引 —— 据此判定无效并跳到下一个候选。
_CJK_PROBE = (0x4E2D, 0x4E00)  # '中', '一'


def _font_has_cjk(font) -> bool:
    try:
        a = rl.GetGlyphIndex(font, _CJK_PROBE[0])
        b = rl.GetGlyphIndex(font, _CJK_PROBE[1])
        return a >= 0 and b >= 0 and a != b
    except Exception:
        return False


def _get_font():
    global _FONT
    if _FONT is not None:
        return _FONT
    # cffi int 数组,供 LoadFontEx 的 int *codepoints 参数使用
    cps = rl.ffi.new("int[]", _CODEPOINTS) if _CODEPOINTS else None
    for path in _candidate_font_paths():
        if not path or not os.path.isfile(path):
            continue
        try:
            font = rl.LoadFontEx(path.encode("utf-8"), _FONT_BASE_SIZE,
                                 cps, len(_CODEPOINTS))
        except Exception:
            continue
        if not _font_has_cjk(font):
            # .ttc 退回默认字体 / 不含中文 -> 跳过(不 UnloadFont,以免误释放
            # 共享的默认字体),继续尝试下一个候选
            continue
        try:
            rl.SetTextureFilter(font.texture, _FILTER_BILINEAR)
        except Exception:
            pass
        _FONT = font
        return _FONT
    _FONT = rl.GetFontDefault()
    return _FONT


# ─── 文本绘制 ──────────────────────────────────────────────

def draw_text(text: str, x: int, y: int, font_size: int = 10, c=None):
    if c is None:
        c = (255, 255, 255, 255)
    spacing = font_size / 10.0  # 与 raylib DrawText/MeasureText 默认间距一致
    rl.DrawTextEx(_get_font(), text.encode("utf-8"),
                  (float(x), float(y)), float(font_size), spacing, c)


def draw_text_centered(text: str, y: int, font_size: int = 10, c=None):
    if c is None:
        c = (255, 255, 255, 255)
    w = measure_text(text, font_size)
    x = (rl.GetScreenWidth() - w) // 2
    draw_text(text, x, y, font_size, c)


def measure_text(text: str, font_size: int = 10) -> int:
    spacing = font_size / 10.0
    res = rl.MeasureTextEx(_get_font(), text.encode("utf-8"),
                           float(font_size), spacing)
    try:
        w = res.x          # cffi 返回 struct Vector2
    except AttributeError:
        w = res[0]         # 兼容绑定以元组返回的情况
    return int(w)


def set_text_color(c):
    pass  # 6.x 不需要 SetTextColor，颜色直接传给 DrawText


def set_font_size(size: int):
    pass  # 6.x 不需要 SetFontSize，大小直接传给 DrawText


# ─── 绘制 ──────────────────────────────────────────────────

def clear_background(c):
    rl.ClearBackground(c)


def draw_rect(x, y, w, h, c):
    rl.DrawRectangle(int(x), int(y), int(w), int(h), c)


def draw_rect_lines(rec, thickness=1, c=None):
    if c is None:
        c = (255, 255, 255, 255)
    rl.DrawRectangleLinesEx(rec, thickness, c)


def draw_rect_v(pos, size, c):
    rl.DrawRectangleV(pos, size, c)


def draw_circle_v(center, radius, c):
    rl.DrawCircleV(center, radius, c)


def draw_circle_3d(center, radius, c):
    """地面圆环（3D，XZ 平面）- 绕 X 轴转 90°"""
    rl.DrawCircle3D(center, radius, vec3(1, 0, 0), 90.0, c)


def draw_circle(x, y, radius, c):
    rl.DrawCircle(int(x), int(y), radius, c)


def draw_line(x1, y1, x2, y2, c):
    rl.DrawLine(int(x1), int(y1), int(x2), int(y2), c)


def draw_cube(pos, w, h, d, c):
    rl.DrawCube(pos, w, h, d, c)


def set_alpha(a):
    """设置全局透明度，影响后续所有 color() 调用"""
    global _current_alpha
    _current_alpha = max(0.0, min(1.0, a))


def set_material_mode(mode):
    pass  # 6.x 无此函数


# ─── 图像 ──────────────────────────────────────────────────

def gen_image_color(w, h, c):
    return rl.GenImageColor(w, h, c)


def image_draw_pixel(img, x, y, c):
    rl.ImageDrawPixel(img, x, y, c)


def get_image_color(img, x, y):
    return rl.GetImageColor(img, x, y)


def load_texture_from_image(img):
    return rl.LoadTextureFromImage(img)


def unload_image(img):
    rl.UnloadImage(img)


def set_texture_filter(tex, filter):
    rl.SetTextureFilter(tex, filter)


def unload_texture(tex):
    rl.UnloadTexture(tex)


# ─── 模型/网格 ────────────────────────────────────────────

def gen_mesh_cube(w, h, d):
    return rl.GenMeshCube(w, h, d)


def load_model_from_mesh(mesh):
    return rl.LoadModelFromMesh(mesh)


def unload_mesh(mesh):
    rl.UnloadMesh(mesh)


def unload_model(model):
    rl.UnloadModel(model)


# ─── 绘制循环 ──────────────────────────────────────────────

def begin_drawing():
    global _current_alpha
    _current_alpha = 1.0
    rl.BeginDrawing()


def end_drawing():
    rl.EndDrawing()


def begin_camera3d(cam):
    rl.BeginMode3D(cam)


def end_camera3d():
    rl.EndMode3D()


def begin_camera2d(cam):
    rl.BeginMode2D(cam)


def end_camera2d():
    rl.EndMode2D()
