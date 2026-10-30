"""
ui_minimap.py - 小地图（倍程缩放）
以大本营为中心的俯视地图，纹理 1 像素 = step×step 格。

地形烘焙完全复刻 chunk.Chunk._generate() 的向量化管线
（natural_height_vec → 湿度 → 大本营平整 → 群系掩码链 → 表面方块），
不触发区块加载，结果与真实地形逐格一致。烘焙时在高度场上跑
marching squares 叠加等高线（海平面以上，每 N 根一条加粗主线），
每根线沿线印 4×6 点阵海拔数字（标签按服务半径散布、跨线防重叠，
白晕底衬）。地形不可变，同一倍程
只烘焙一次；敌军路径只增不改（与 renderer._render_path_2d 同以
条数为缓存键），新增时仅重做"像素拷贝 + 路径叠加 + 纹理上传"。

倍程缩放：±128 / 256 / 512 / 1024 四档（ui_config.MINIMAP_LEVELS），
按 max(相机距离, 最远非休眠刷怪点距离) 自动升降档，跨档才重烘焙。
休眠刷怪点画 1px 暗灰（数量随探索增长也不糊图），唤醒点 3px 醒目；
出界的唤醒点用"射线-矩形求交"画真方向边缘点。相机超出最大倍程
时改为沿边缘滑动的方向箭头 + 距离数字，位置信息永远真实。
"""

import math
from collections import OrderedDict

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view
import raylib as rl

import config
import gamedata
from chunk import BIOME_SURFACE_BLOCK, base_center_height, natural_height_vec
from noise import fbm_levels_2d, fbm_levels_2d_vec
from blocks import BLOCK_ID, get_block
import ui_config as uc
import rl_utils as _rl_utils
from rl_utils import (color, draw_line, draw_rect, draw_rect_lines, rect,
                      draw_text, measure_text, gen_image_color,
                      load_texture_from_image, unload_image,
                      set_texture_filter, unload_texture)

_WHITE = (255, 255, 255, 255)

# 刷怪点唤醒态颜色（与 renderer._render_spawners_2d 保持一致）
_SPAWNER_COLORS = {
    "scouting": (255, 220, 0, 220),
    "active": (255, 50, 50, 220),
}


def _step_for(world_range):
    """倍程 → 采样步长（1 像素代表 step×step 格）"""
    return max(1, (2 * world_range) // uc.MINIMAP_TEX_PX)


def pick_level(need, current):
    """带迟滞的倍程选择。need 为原始需求半径（格，无余量系数）。

    升档：need > 当前档（当前档装不下）→ 取 ≥ need 的最小档；
    降档：need ≤ 下一档 / MINIMAP_ZOOM_MARGIN（留余量防边界抖动）。
    """
    levels = uc.MINIMAP_LEVELS
    if need > current:
        for lv in levels:
            if need <= lv:
                return lv
        return levels[-1]
    try:
        idx = levels.index(current)
    except ValueError:
        idx = len(levels) - 1
    while idx > 0 and need <= levels[idx - 1] / uc.MINIMAP_ZOOM_MARGIN:
        idx -= 1
    return levels[idx]


# ─── 地形像素计算（纯 numpy，不依赖 raylib，可独立测试） ─────

def compute_surface_blocks(universe, size, world_range=None):
    """复刻 chunk._generate() 推导表面方块 ID。

    world_range 为半边长（格），默认取最小倍程；1 像素代表
    step×step 格，采样点取该像素中心的整格坐标（与 chunk 的
    整数格点采样一致）。返回 (surface_blocks, h, biome_arr)，
    行随 z（北在上）、列随 x。海洋格给出"视觉表面"水色
    （实际表层为沙，上方覆盖 2..6 层水，见 chunk._generate）。
    """
    if world_range is None:
        world_range = uc.MINIMAP_LEVELS[0]
    seed = universe.seed
    height = universe.height
    base_x = universe.base_x
    base_z = universe.base_z
    step = _step_for(world_range)
    half = (size // 2) * step
    origin_x = base_x - half
    origin_z = base_z - half

    # 采样点：像素中心所在的整格（保持与 chunk 一致的整数格点）
    offsets = np.arange(size, dtype=np.float64) * step + (step // 2)
    x_vals = origin_x + offsets
    z_vals = origin_z + offsets
    # 'xy' 索引：行随 z、列随 x —— 与图像行主序一致
    x, z = np.meshgrid(x_vals, z_vals)  # [size, size]

    # ─── 1. 高度 & 湿度（chunk._generate 第 2-3 步） ──────
    h = natural_height_vec(x, z, seed)
    m_cfg = config.NOISE_CFG["moisture"]
    moisture = fbm_levels_2d_vec(x, z, seed + m_cfg.get("base_seed", 0),
                                 m_cfg["levels"])

    # ─── 2. 大本营区域平整（第 4 步） ─────────────────────
    dist_to_base = np.sqrt((x - base_x) ** 2 + (z - base_z) ** 2)
    in_base_area = dist_to_base < config.BASE_RADIUS + 3
    h_base = base_center_height(base_x, base_z, seed)
    m_base = fbm_levels_2d(base_x, base_z, seed + m_cfg.get("base_seed", 0),
                           m_cfg["levels"])
    h = np.where(in_base_area, h_base, h)
    moisture = np.where(in_base_area, m_base, moisture)

    # ─── 3. 海拔 & 温度（第 5 步） ────────────────────────
    altitude = h * height
    temp = universe.base_temperature - altitude * config.TEMPERATURE_LAPSE_RATE

    # ─── 4. 群系掩码链（第 6 步，顺序一致） ───────────────
    biome_arr = np.full((size, size), "plains", dtype=object)
    biome_arr[h < 0.004] = "ocean"
    biome_arr[(temp < -10.0) & (h >= 0.18)] = "snowy"
    biome_arr[(temp < 3.0) & (temp >= -10.0) & (h >= 0.18)] = "tundra"
    biome_arr[(altitude > config.MOUNTAIN_ALT_RATIO * height)
              & (h >= 0.18)] = "mountain"
    biome_arr[(temp > 25.0) & (moisture < 0.3) & (h >= 0.18)] = "desert"
    biome_arr[(temp > 20.0) & (moisture >= 0.3) & (h >= 0.18)] = "savanna"
    biome_arr[(temp < 15.0) & (moisture > 0.5) & (h >= 0.18)] = "forest"

    # ─── 5. 表面方块（第 7 步：群系映射 + 雪盖 + 低草变沙） ─
    surface_y = np.floor(altitude).astype(np.int32)
    surface_blocks = np.zeros((size, size), dtype=np.uint8)
    for biome_name, block_id in BIOME_SURFACE_BLOCK.items():
        surface_blocks[biome_arr == biome_name] = block_id
    snow_cover = (surface_y > config.SNOW_SURFACE_RATIO * height) & (temp < 2.0)
    surface_blocks[snow_cover] = BLOCK_ID["snow"]
    low_grass = (h < 0.25) & (surface_blocks == BLOCK_ID["grass"])
    surface_blocks[low_grass] = BLOCK_ID["sand"]

    # ─── 满高柱修正（复刻 chunk 填柱逻辑）──────────────────
    # h 饱和到 1.0 时 surface_y == height 越界，col_pos==0 的表层方块
    # 永远不会被放置，柱顶实际是 col_pos=-1 的泥土带（chunk._generate
    # 第 9 步）。不修正的话此类种子会被雪盖误涂成全白。
    surface_blocks[surface_y >= height] = BLOCK_ID["dirt"]

    return surface_blocks, h, biome_arr


def compute_terrain_pixels(universe, size, world_range=None):
    """计算小地图地形 RGBA 像素（含高度明暗 + 等高线叠加）。

    返回 np.uint8 [size, size, 4]，行 = z（北在上）、列 = x。
    """
    surface_blocks, h, biome_arr = compute_surface_blocks(universe, size,
                                                          world_range)

    # ─── 6. 方块 → 顶面颜色 ──────────────────────────────
    rgb = np.zeros((size, size, 3), dtype=np.float64)
    for block_id in np.unique(surface_blocks):
        rgb[surface_blocks == block_id] = get_block(int(block_id)).top_color

    # 高度明暗（山体立体感）；海洋改用深度明暗（海岸线可读）
    shade = 1.0 + (h - 0.5) * 0.24
    ocean = biome_arr == "ocean"
    depth = np.clip(h / 0.004, 0.0, 1.0)
    shade[ocean] = 0.55 + 0.45 * depth[ocean]
    rgb *= np.clip(shade, 0.55, 1.3)[:, :, None]

    pixels = np.empty((size, size, 4), dtype=np.uint8)
    pixels[:, :, :3] = np.clip(rgb, 0, 255).astype(np.uint8)
    pixels[:, :, 3] = 255
    _overlay_contours(pixels, h, universe.height)
    return pixels


# ─── 等高线（marching squares + 线性插值，纯 numpy） ─────────

# 跨层单元 case（tl·8 + tr·4 + br·2 + bl·1）→ 线段的边索引对。
# 边索引：0=上 1=右 2=下 3=左；线段两侧把"高于层级"的角隔在同一侧。
# 鞍点 case（5/10）直接画两段，小地图尺度下免中心判向。
_MS_SEGMENTS = {
    1: ((3, 2),), 2: ((2, 1),), 3: ((3, 1),), 4: ((0, 1),),
    5: ((0, 1), (2, 3)),
    6: ((0, 2),), 7: ((0, 3),), 8: ((0, 3),), 9: ((0, 2),),
    10: ((0, 3), (2, 1)),
    11: ((0, 1),), 12: ((3, 1),), 13: ((2, 1),), 14: ((3, 2),),
}


def _overlay_contours(pixels, h, height, labels=True):
    """等高线叠加进地形像素。层级取 MINIMAP_CONTOUR_STEP 的等差序列，
    海平面（h < 0.004 为海洋，与 chunk 同阈值）以上才画——海底保持
    深度明暗；每 MINIMAP_CONTOUR_MAJOR_EVERY 根一条主线（加粗到 2px，
    与细线一眼可辨）。所有层级沿线烘焙海拔数字（按服务半径散布；
    height 为世界最大高度，标签值 = 层级 × height）。
    跨层单元由 numpy 圈出，逐 case 取边交点连段，按 ~1 像素步长采样
    后与 _overlay_paths 同式 alpha 混合写入 pixels。
    """
    step = uc.MINIMAP_CONTOUR_STEP
    major_every = uc.MINIMAP_CONTOUR_MAJOR_EVERY
    size = pixels.shape[0]

    # 四角高度视图：0=左上 1=右上 2=右下 3=左下（行随 z、列随 x）
    h_tl, h_tr = h[:-1, :-1], h[:-1, 1:]
    h_br, h_bl = h[1:, 1:], h[1:, :-1]

    major_entries = []  # (层级值, 线像素 rows, cols, 是否主线)，标签用
    n_levels = max(0, int(round(1.0 / step)))    # v < 1.0（饱和平台不画顶线）
    k0 = max(1, math.ceil(0.004 / step - 1e-9))  # 跳过海平面以下层级
    for k in range(k0, n_levels):
        v = k * step
        above = h >= v
        tl, tr = above[:-1, :-1], above[:-1, 1:]
        br, bl = above[1:, 1:], above[1:, :-1]
        ii, jj = np.nonzero((tl != tr) | (tr != br) | (br != bl) | (bl != tl))
        if ii.size == 0:
            continue
        c_tl, c_tr = h_tl[ii, jj], h_tr[ii, jj]
        c_br, c_bl = h_br[ii, jj], h_bl[ii, jj]
        case = tl[ii, jj] * 8 + tr[ii, jj] * 4 + br[ii, jj] * 2 + bl[ii, jj]
        # 四边交点参数：变号边分母必非 0；未变号边不入选，分母置 1 防 0/0
        d = np.where(c_tr == c_tl, 1.0, c_tr - c_tl)
        t_top = (v - c_tl) / d
        d = np.where(c_br == c_bl, 1.0, c_br - c_bl)
        t_bot = (v - c_bl) / d
        d = np.where(c_bl == c_tl, 1.0, c_bl - c_tl)
        t_left = (v - c_tl) / d
        d = np.where(c_br == c_tr, 1.0, c_br - c_tr)
        t_right = (v - c_tr) / d
        # 边 → 交点像素坐标（x=列，y=行）
        ex = (jj + t_top, jj + 1.0, jj + t_bot, jj + 0.0)
        ey = (ii + 0.0, ii + t_right, ii + 1.0, ii + t_left)
        is_major = k % major_every == 0
        rgba = (uc.MINIMAP_CONTOUR_MAJOR_COLOR if is_major
                else uc.MINIMAP_CONTOUR_COLOR)
        segs = []
        for cv, pairs in _MS_SEGMENTS.items():
            sel = case == cv
            if not sel.any():
                continue
            for a, b in pairs:
                segs.append(np.stack(
                    (ex[a][sel], ey[a][sel], ex[b][sel], ey[b][sel]), axis=1))
        if segs:
            lrows, lcols = _rasterize_segments(
                pixels, np.concatenate(segs), rgba,
                width=2 if is_major else 1)
            if labels:
                major_entries.append((v, lrows, lcols, is_major))
    if not major_entries:
        return []
    return _stamp_contour_labels(pixels, major_entries, height)


def _rasterize_segments(pixels, segs, rgba, width=1):
    """线段集 [N,4]（x0,y0,x1,y1，像素坐标）→ ~1 像素步长采样，
    与 _overlay_paths 同式 alpha 混合写入 pixels 的 RGB（alpha 不动）。
    width=2 时每采样点盖 2×2（主线加粗）。返回实际写入的 (rows, cols)。"""
    pr, pg, pb, pa = rgba
    a = pa / 255.0
    x0, y0, x1, y1 = segs[:, 0], segs[:, 1], segs[:, 2], segs[:, 3]
    counts = np.ceil(
        np.maximum(np.abs(x1 - x0), np.abs(y1 - y0))).astype(np.int64) + 1
    total = int(counts.sum())
    starts = np.repeat(np.cumsum(counts) - counts, counts)
    t = (np.arange(total, dtype=np.float64) - starts) / np.repeat(counts, counts)
    xr0, xr1 = np.repeat(x0, counts), np.repeat(x1, counts)
    yr0, yr1 = np.repeat(y0, counts), np.repeat(y1, counts)
    cols = (xr0 + (xr1 - xr0) * t).astype(np.int64)
    rows = (yr0 + (yr1 - yr0) * t).astype(np.int64)
    if width == 2:
        rows = np.concatenate((rows, rows + 1, rows, rows + 1))
        cols = np.concatenate((cols, cols, cols + 1, cols + 1))
    ok = ((rows >= 0) & (rows < pixels.shape[0])
          & (cols >= 0) & (cols < pixels.shape[1]))
    rows, cols = rows[ok], cols[ok]
    base = pixels[rows, cols, :3].astype(np.float64)
    pixels[rows, cols, 0] = np.rint(base[:, 0] * (1 - a) + pr * a)
    pixels[rows, cols, 1] = np.rint(base[:, 1] * (1 - a) + pg * a)
    pixels[rows, cols, 2] = np.rint(base[:, 2] * (1 - a) + pb * a)
    return rows, cols


# 0-9 数字 4×6 点阵（'#'=点亮），标签沿主线横排、字距 1 像素
_DIGIT_FONT = {
    "0": (".##.",
          "#..#",
          "#..#",
          "#..#",
          "#..#",
          ".##."),
    "1": ("..#.",
          ".##.",
          "..#.",
          "..#.",
          "..#.",
          ".##."),
    "2": (".##.",
          "#..#",
          "..#.",
          ".#..",
          "#...",
          "####"),
    "3": (".##.",
          "#..#",
          "..#.",
          "..#.",
          "#..#",
          ".##."),
    "4": ("#..#",
          "#..#",
          "#..#",
          "####",
          "...#",
          "...#"),
    "5": ("####",
          "#...",
          "###.",
          "...#",
          "#..#",
          ".##."),
    "6": (".##.",
          "#...",
          "###.",
          "#..#",
          "#..#",
          ".##."),
    "7": ("####",
          "...#",
          "...#",
          "..#.",
          ".#..",
          ".#.."),
    "8": (".##.",
          "#..#",
          ".##.",
          "#..#",
          "#..#",
          ".##."),
    "9": (".##.",
          "#..#",
          ".###",
          "...#",
          "...#",
          ".##."),
}
# 预展开为每个数字的点亮偏移 (dr, dc) 数组
_DIGIT_PIX = {
    ch: np.array([(r, c) for r, row in enumerate(bm) for c, s in enumerate(row)
                  if s == "#"], dtype=np.int64)
    for ch, bm in _DIGIT_FONT.items()
}


def _stamp_contour_labels(pixels, level_pixels, height):
    """等高线沿线烘焙海拔数字（4×6 点阵，白晕底衬）。

    level_pixels: [(层级值, 线像素 rows, cols, 是否主线), ...]。主线
    先印（读数锚点优先占位），组内按海拔升序。锚点按"水平度"（同行
    ±4px 线像素数 − 同列 ±4px，越大越平缓、数字横排越自然）打分，
    确定性排序（分数降序 → 坐标字典序）。放置用"服务半径"机制：
    served 掩码记录同线已覆盖区域，每盖一个章就把服务半径内的候选
    从候选数组中向量化过滤——任意两标签的 Chebyshev 距离严格大于
    SERVE，同高程的每条线段（每个山头）各自获得标签；数量硬上限
    CAP_RANGE 防极端密度。所有线共用一张占格掩码：标签矩形（含
    1px 晕边）与已印标签重叠的候选永久跳过（占格只增不减）。光晕
    先把标签矩形向白混合（顺带盖掉穿过的等高线，仿地形图留白），
    再点亮墨色数字。标签值 = 层级值 × height（格）。返回盖章锚点
    [(层级值, 行, 列, 是否主线), ...]（候选坐标，未钳制；测试断言
    间距不变量用）。
    """
    size = pixels.shape[0]
    lr, lg, lb, _ = uc.MINIMAP_CONTOUR_LABEL_COLOR
    halo_a = uc.MINIMAP_CONTOUR_LABEL_HALO
    serve = uc.MINIMAP_CONTOUR_LABEL_SERVE
    cap_lo, cap_hi = uc.MINIMAP_CONTOUR_LABEL_CAP_RANGE
    inner = slice(4, size - 4)  # 打分窗口 ±4px，边距内不做候选
    occupied = np.zeros((size, size), dtype=bool)
    anchors = []  # (层级值, 行, 列, 是否主线)，返回给调用方
    # 主线先印（读数锚点优先占位），组内按海拔升序
    ordered = sorted(level_pixels, key=lambda e: (not e[3], e[0]))
    for v, rows, cols, is_major in ordered:
        text = str(int(round(v * height)))
        w = 5 * len(text) - 1
        cap = int(min(max(len(rows) // 150, cap_lo), cap_hi))

        # ── 锚点打分 ─────────────────────────────────────
        mask = np.zeros((size, size), dtype=bool)
        mask[rows, cols] = True
        row_win = sliding_window_view(mask, 9, axis=1).sum(axis=-1)
        col_win = sliding_window_view(mask, 9, axis=0).sum(axis=-1)
        score = np.full((size, size), -(1 << 62), dtype=np.int64)
        score[inner, inner] = row_win[inner, :] - col_win[:, inner]
        cr, cc = np.nonzero(mask)
        cand = np.lexsort((cc, cr, -score[cr, cc]))

        # ── 盖章（占格检查 + 光晕 + 印字） ────────────────
        def stamp_at(r, c):
            r0 = min(max(r - 3, 1), size - 8)
            c0 = min(max(c - w // 2, 1), size - 1 - w)
            hr, hc = slice(r0 - 1, r0 + 7), slice(c0 - 1, c0 + w + 1)
            if occupied[hr, hc].any():
                return False  # 与其他线的标签重叠
            occupied[hr, hc] = True
            base = pixels[hr, hc, :3].astype(np.float64)
            pixels[hr, hc, 0] = np.rint(
                base[:, :, 0] * (1 - halo_a) + 255 * halo_a)
            pixels[hr, hc, 1] = np.rint(
                base[:, :, 1] * (1 - halo_a) + 255 * halo_a)
            pixels[hr, hc, 2] = np.rint(
                base[:, :, 2] * (1 - halo_a) + 255 * halo_a)
            for s, ch in enumerate(text):
                drs, dcs = _DIGIT_PIX[ch][:, 0], _DIGIT_PIX[ch][:, 1]
                pixels[r0 + drs, c0 + s * 5 + dcs, 0] = lr
                pixels[r0 + drs, c0 + s * 5 + dcs, 1] = lg
                pixels[r0 + drs, c0 + s * 5 + dcs, 2] = lb
            return True

        # ── 贪心：分数序扫描 + 服务半径向量化过滤 ─────────
        served = np.zeros((size, size), dtype=bool)
        i = 0
        placed = 0
        while i < cand.size and placed < cap:
            r, c = int(cr[cand[i]]), int(cc[cand[i]])
            if not stamp_at(r, c):
                i += 1  # 占格只增不减，此处碰撞则永久放不下
                continue
            placed += 1
            anchors.append((v, r, c, is_major))
            served[max(0, r - serve):r + serve + 1,
                   max(0, c - serve):c + serve + 1] = True
            rest = cand[i + 1:]
            cand = rest[~served[cr[rest], cc[rest]]] if rest.size else rest
            i = 0
    return anchors


class Minimap:
    """右上角小地图：倍程缩放烘焙地形 + 每帧实体叠加 + 点击跳转相机"""

    def __init__(self, universe):
        self._tex = None
        self._terrain_pixels = None
        self._baked_key = None      # (seed, range, path_count)
        # 地形像素缓存：地形只由 seed+倍程决定（挖掘不改小地图烘焙），
        # 四档倍程首次访问后复用，避免每次跨档重跑 50-70ms 的噪声烘焙。
        self._terrain_cache = OrderedDict()
        self._terrain_cache_max = 8
        self.range = uc.MINIMAP_LEVELS[0]
        self._step = _step_for(self.range)
        self._span = uc.MINIMAP_TEX_PX * self._step   # 覆盖的世界边长（格）
        self._origin_x = universe.base_x - self.range
        self._origin_z = universe.base_z - self.range
        self._need_raw = 0.0        # 本帧原始需求半径（箭头/调试用）
        self._compute_terrain(universe)

    # ─── 烘焙 ────────────────────────────────────────────

    def _compute_terrain(self, universe, game=None):
        """按当前倍程取地形像素；首次烘焙后按 (seed, range) 缓存复用。"""
        self._step = _step_for(self.range)
        self._span = uc.MINIMAP_TEX_PX * self._step
        half = self._span // 2
        self._origin_x = universe.base_x - half
        self._origin_z = universe.base_z - half
        key = (universe.seed, self.range)
        pixels = self._terrain_cache.get(key)
        if pixels is None:
            pixels = compute_terrain_pixels(
                universe, uc.MINIMAP_TEX_PX, self.range)
            self._terrain_cache[key] = pixels
            if len(self._terrain_cache) > self._terrain_cache_max:
                self._terrain_cache.popitem(last=False)
        else:
            self._terrain_cache.move_to_end(key)
        self._terrain_pixels = pixels
        self._baked_key = (universe.seed, self.range, universe.world_version)
        self._upload(universe, game)

    def _upload(self, universe, game=None):
        """缓存像素 + 路径叠加 → 纹理。像素拷贝 + 上传共约毫秒级。"""
        tex_px = uc.MINIMAP_TEX_PX
        pixels = self._terrain_pixels.copy()
        self._overlay_paths(pixels, universe, game)

        img = gen_image_color(tex_px, tex_px, (0, 0, 0, 255))
        buf = rl.ffi.buffer(img.data, tex_px * tex_px * 4)
        buf[:] = pixels.tobytes()
        tex = load_texture_from_image(img)
        unload_image(img)
        set_texture_filter(tex, rl.TEXTURE_FILTER_BILINEAR)
        if self._tex is not None:
            unload_texture(self._tex)
        self._tex = tex

    def unload(self):
        """释放 GPU 纹理。读档替换 Game 时须调用，否则每次读档泄漏一张纹理。"""
        if self._tex is not None:
            unload_texture(self._tex)
            self._tex = None

    def _overlay_paths(self, pixels, universe, game=None):
        """敌军路径叠加进像素数组（半透明暗黄，仿 renderer._render_path_2d）。

        有寻路管理器时只画各 (刷怪点×类型) 的主变体（idx=0）。
        """
        pr, pg, pb, pa = uc.MINIMAP_PATH_COLOR
        a = pa / 255.0
        tex_px = uc.MINIMAP_TEX_PX
        pf = getattr(game, "pathfinder", None) if game is not None else None
        if pf is not None:
            paths = [pd for (_sid, _tn, idx), pd in pf.render_paths().items()
                     if idx == 0]
        else:
            paths = list(universe.enemy_paths.values())
        for path_data in paths:
            for x, _y, z in path_data.points:
                # 路径点是世界坐标（方块中心）→ 采样像素
                px = int((x - self._origin_x) // self._step)
                pz = int((z - self._origin_z) // self._step)
                if 0 <= px < tex_px and 0 <= pz < tex_px:
                    base = pixels[pz, px]
                    pixels[pz, px] = (
                        round(base[0] * (1 - a) + pr * a),
                        round(base[1] * (1 - a) + pg * a),
                        round(base[2] * (1 - a) + pb * a),
                        255,
                    )

    # ─── 倍程管理 ────────────────────────────────────────

    def _raw_need_radius(self, game):
        """缩放需求半径（格）：相机与大本营距离、最远非休眠刷怪点距离的较大者"""
        cam = game.camera
        if cam.is_2d():
            cx, cz = cam.pos_2d[0], cam.pos_2d[2]
        else:
            cx, cz = cam.target[0], cam.target[2]
        bx = game.universe.base_x
        bz = game.universe.base_z
        need = math.hypot(cx - bx, cz - bz)
        for rt in game.spawners.values():
            if rt.mode == "dormant":
                continue  # 休眠点不触发缩放（数量随探索无界增长）
            sp = game.universe.spawners.get(rt.spawner_id)
            if sp is None:
                continue
            d = math.hypot(sp.x - bx, sp.z - bz)
            if d > need:
                need = d
        return need

    def _update_level(self, game):
        """每帧：按需求半径升降倍程，跨档重烘焙；世界版本变化只重上传路径。"""
        self._need_raw = self._raw_need_radius(game)
        level = pick_level(self._need_raw, self.range)
        range_changed = level != self.range
        if range_changed:
            self.range = level
        universe = game.universe
        version = universe.world_version
        if range_changed or universe.seed != self._baked_key[0]:
            self._compute_terrain(universe, game)
        elif version != self._baked_key[2]:
            self._baked_key = (universe.seed, self.range, version)
            self._upload(universe, game)

    # ─── 布局与坐标 ──────────────────────────────────────

    def get_rect(self):
        """屏幕矩形 (x, y, w, h)，随窗口尺寸动态计算"""
        x = rl.GetScreenWidth() - uc.MINIMAP_SIZE - uc.MINIMAP_MARGIN
        return x, uc.MINIMAP_Y, uc.MINIMAP_SIZE, uc.MINIMAP_SIZE

    def _world_to_minimap(self, wx, wz, mx, my, size):
        """世界坐标 → 小地图屏幕坐标"""
        u = (wx - self._origin_x) / self._span
        v = (wz - self._origin_z) / self._span
        return mx + u * size, my + v * size

    # ─── 渲染 ────────────────────────────────────────────

    def render(self, game):
        self._update_level(game)
        mx, my, size, _ = self.get_rect()

        # 烘焙地形纹理（缩放到显示尺寸）
        tex_px = uc.MINIMAP_TEX_PX
        rl.DrawTexturePro(self._tex, (0, 0, tex_px, tex_px),
                          (mx, my, size, size), (0, 0), 0.0, _WHITE)

        # 动态层（Scissor 裁剪到地图矩形，范围外自动裁掉）
        rl.BeginScissorMode(mx, my, size, size)
        self._render_markers(game, mx, my, size)
        rl.EndScissorMode()

        # 边框 + 底部提示条（显示当前倍程）
        draw_rect_lines(rect(mx - 1, my - 1, size + 2, size + 2), 1,
                        color(*uc.MINIMAP_BORDER_COLOR))
        draw_rect(mx, my + size + 2, size, uc.MINIMAP_BORDER_H,
                  color(*uc.MINIMAP_HINT_BG))
        draw_text(f"[M]地图 ±{self.range} | 点击跳转", mx + 4, my + size + 5,
                  uc.MINIMAP_HINT_SIZE, color(*uc.MINIMAP_HINT_COLOR))

    def _render_markers(self, game, mx, my, size):
        universe = game.universe

        def to_mm(wx, wz):
            return self._world_to_minimap(wx, wz, mx, my, size)

        def in_map(x, y):
            return 0 <= x - mx < size and 0 <= y - my < size

        # 热路径（大本营 + 塔 + 敌人，占标记数 ~97%）内联：
        # 坐标运算与 _world_to_minimap 完全同序（(wx-ox)/span 后 *size），
        # 颜色按 (原色元组, 当前全局 alpha) 缓存，等价于每次 color(*c)。
        dr = rl.DrawRectangle
        ox = self._origin_x
        oz = self._origin_z
        span = self._span
        _alpha = _rl_utils._current_alpha
        ccache = {}
        cget = ccache.get
        base_c = color(*uc.MINIMAP_BASE_COLOR)
        base_s = uc.MINIMAP_BASE_SIZE
        tower_s = uc.MINIMAP_TOWER_SIZE
        enemy_s = uc.MINIMAP_ENEMY_SIZE
        ecget = gamedata.ENEMY_COLORS.get
        enemy_fb = (255, 80, 80)

        def mm_rect(wx, wz, s, c):
            key = (c, _alpha)
            cc = cget(key)
            if cc is None:
                cc = color(*c)
                ccache[key] = cc
            u = (wx - ox) / span
            x = mx + u * size
            v = (wz - oz) / span
            y = my + v * size
            dr(int(x - s // 2), int(y - s // 2), s, s, cc)

        # 大本营（世界原点，白色块）
        mm_rect(universe.base_x + 0.5, universe.base_z + 0.5,
                base_s, uc.MINIMAP_BASE_COLOR)

        # 塔（用塔自身显示色）— 内联 mm_rect 体（坐标运算同序，省闭包调用）
        for tower in game.towers:
            c = tower.display_color
            key = (c, _alpha)
            cc = cget(key)
            if cc is None:
                cc = color(*c)
                ccache[key] = cc
            wx = tower.position[0]
            wz = tower.position[2]
            u = (wx - ox) / span
            x = mx + u * size
            v = (wz - oz) / span
            y = my + v * size
            dr(int(x - tower_s // 2), int(y - tower_s // 2), tower_s, tower_s, cc)

        # 敌人（按类型配色，未知类型红色兜底）— 同样内联
        for enemy in game.enemies:
            c = ecget(enemy.type_name, enemy_fb)
            key = (c, _alpha)
            cc = cget(key)
            if cc is None:
                cc = color(*c)
                ccache[key] = cc
            wx = enemy.position[0]
            wz = enemy.position[2]
            u = (wx - ox) / span
            x = mx + u * size
            v = (wz - oz) / span
            y = my + v * size
            dr(int(x - enemy_s // 2), int(y - enemy_s // 2), enemy_s, enemy_s, cc)

        # 刷怪点分级：休眠 1px 暗灰（图内）；唤醒 3px 醒目色，
        # 出界的唤醒点用真方向边缘点（射线-矩形求交）
        for rt in game.spawners.values():
            sp = universe.spawners.get(rt.spawner_id)
            if sp is None:
                continue
            x, y = to_mm(sp.x + 0.5, sp.z + 0.5)
            if rt.mode in _SPAWNER_COLORS:
                if in_map(x, y):
                    draw_rect(x - uc.MINIMAP_SPAWNER_SIZE // 2,
                              y - uc.MINIMAP_SPAWNER_SIZE // 2,
                              uc.MINIMAP_SPAWNER_SIZE,
                              uc.MINIMAP_SPAWNER_SIZE,
                              color(*_SPAWNER_COLORS[rt.mode]))
                else:
                    ex, ey = self._clip_to_rect(mx, my, size, x, y)
                    if ex is not None:
                        c = (uc.MINIMAP_EDGE_DOT_ACTIVE if rt.mode == "active"
                             else uc.MINIMAP_EDGE_DOT_SCOUTING)
                        draw_rect(ex - 1, ey - 1, 2, 2, color(*c))
            elif in_map(x, y):
                draw_rect(x, y, 1, 1, color(*uc.MINIMAP_SPAWNER_DORMANT))

        # 相机指示：图内画十字/视野框；图外画边缘方向箭头 + 距离
        cam = game.camera
        if cam.is_2d():
            cx, cz = cam.pos_2d[0], cam.pos_2d[2]
            half_w = rl.GetScreenWidth() / (2 * cam.zoom_2d)
            half_h = rl.GetScreenHeight() / (2 * cam.zoom_2d)
            x0, y0 = to_mm(cx - half_w, cz - half_h)
            x1, y1 = to_mm(cx + half_w, cz + half_h)
        else:
            cx, cz = cam.target[0], cam.target[2]
        cam_dist = math.hypot(cx - universe.base_x, cz - universe.base_z)
        tx, ty = to_mm(cx, cz)
        if in_map(tx, ty):
            if cam.is_2d():
                # 2D：视野矩形（世界半宽高 → 小地图），Scissor 自动裁剪
                draw_rect_lines(rect(x0, y0, x1 - x0, y1 - y0), 1,
                                color(*uc.MINIMAP_CAMERA_COLOR))
            else:
                # 3D：target 十字 + 按 distance 缩放的示意框
                r = max(4.0, min(size // 5, cam.distance * 0.3))
                c = color(*uc.MINIMAP_CAMERA_3D_COLOR)
                draw_line(tx - r, ty, tx + r, ty, c)
                draw_line(tx, ty - r, tx, ty + r, c)
                draw_rect_lines(rect(tx - r, ty - r, 2 * r, 2 * r), 1, c)
        else:
            self._draw_edge_arrow(mx, my, size, tx, ty, cam_dist)

    @staticmethod
    def _clip_to_rect(mx, my, size, x, y):
        """图外点 → 从矩形中心引射线与内缩边界求交，返回边缘点。

        保持真实方向（区别于两轴独立钳制的方向歪曲）。
        """
        inset = 3
        lo_x, hi_x = mx + inset, mx + size - inset
        lo_y, hi_y = my + inset, my + size - inset
        cx, cy = mx + size / 2.0, my + size / 2.0
        dx, dy = x - cx, y - cy
        if dx == 0 and dy == 0:
            return None
        t = float("inf")
        if dx > 0:
            t = min(t, (hi_x - cx) / dx)
        elif dx < 0:
            t = min(t, (lo_x - cx) / dx)
        if dy > 0:
            t = min(t, (hi_y - cy) / dy)
        elif dy < 0:
            t = min(t, (lo_y - cy) / dy)
        if t == float("inf") or t <= 0:
            return None
        return cx + dx * t, cy + dy * t

    def _draw_edge_arrow(self, mx, my, size, tx, ty, dist):
        """相机出界：沿边缘滑动的方向箭头 + 距离数字（格）"""
        ex, ey = self._clip_to_rect(mx, my, size, tx, ty)
        if ex is None:
            return
        dx, dy = tx - (mx + size / 2.0), ty - (my + size / 2.0)
        n = math.hypot(dx, dy)
        if n < 1e-6:
            return
        ux, uy = dx / n, dy / n
        c = color(*uc.MINIMAP_ARROW_COLOR)
        # 短杆 + 三角尖（指向图外方向）
        bx, by = ex - ux * 9, ey - uy * 9
        draw_line(bx, by, ex, ey, c)
        px_, py_ = -uy, ux
        draw_line(ex, ey, ex - ux * 5 + px_ * 4, ey - uy * 5 + py_ * 4, c)
        draw_line(ex, ey, ex - ux * 5 - px_ * 4, ey - uy * 5 - py_ * 4, c)
        # 距离文字（≥1000 显示 1.2k），贴边时翻到内侧
        label = f"{int(round(dist))}" if dist < 1000 else f"{dist / 1000:.1f}k"
        tw = measure_text(label, uc.MINIMAP_HINT_SIZE)
        lx = int(ex) + 6 if ex <= mx + size - tw - 10 else int(ex) - tw - 6
        ly = int(ey) - 14 if ey >= my + 20 else int(ey) + 6
        draw_text(label, lx, ly, uc.MINIMAP_HINT_SIZE, c)

    # ─── 交互 ────────────────────────────────────────────

    def handle_click(self, game, mouse_x, mouse_y) -> bool:
        """点击落在小地图内则跳转相机，返回是否命中（命中时调用方不再下发）"""
        mx, my, size, _ = self.get_rect()
        if not (mx <= mouse_x < mx + size and my <= mouse_y < my + size):
            return False
        wx = self._origin_x + (mouse_x - mx) / size * self._span
        wz = self._origin_z + (mouse_y - my) / size * self._span

        cam = game.camera
        if cam.is_2d():
            cam.pos_2d = (wx, 0, wz)
        else:
            wy = game.universe.get_surface_height_smooth_safe(wx, wz) + 1
            cam.target = (wx, wy, wz)
        return True
