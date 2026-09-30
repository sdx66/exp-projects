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


# ─── 文本绘制 ──────────────────────────────────────────────

def draw_text(text: str, x: int, y: int, font_size: int = 10, c=None):
    if c is None:
        c = (255, 255, 255, 255)
    rl.DrawText(text.encode("utf-8"), x, y, font_size, c)


def draw_text_centered(text: str, y: int, font_size: int = 10, c=None):
    if c is None:
        c = (255, 255, 255, 255)
    w = rl.MeasureText(text.encode("utf-8"), font_size)
    x = (rl.GetScreenWidth() - w) // 2
    draw_text(text, x, y, font_size, c)


def measure_text(text: str, font_size: int = 10) -> int:
    return rl.MeasureText(text.encode("utf-8"), font_size)


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
