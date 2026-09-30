"""
renderer.py - 2D/3D 渲染器
"""

import math
import random
import raylib as rl
import config
from blocks import get_block, is_solid
from world import BIOMES
from rl_utils import *


class Renderer:
    def __init__(self):
        self.frame_count = 0
        self._terrain_static_key = None
        self._terrain_visible_blocks = []
        self._terrain_mesh = None
        self._terrain_material = None
        self._terrain_image = None
        self._terrain_texture = None
        self._IDENTITY_MATRIX = (
            1.0, 0.0, 0.0, 0.0,
            0.0, 1.0, 0.0, 0.0,
            0.0, 0.0, 1.0, 0.0,
            0.0, 0.0, 0.0, 1.0,
        )
        self._block_color_cache = {}
        self._block_def_cache = {}
        self._entity_color_cache = {}
        self._path_cache_key = None
        self._path_cache = []

    def init_3d(self):
        self._terrain_mesh = None
        self._terrain_material = None
        self._terrain_image = None
        self._terrain_texture = None
        self._IDENTITY_MATRIX = (
            1.0, 0.0, 0.0, 0.0,
            0.0, 1.0, 0.0, 0.0,
            0.0, 0.0, 1.0, 0.0,
            0.0, 0.0, 0.0, 1.0,
        )

    def render(self, camera, universe, entities=None):
        self.frame_count += 1
        if camera.is_3d():
            self._render_3d(camera, universe, entities)
        else:
            self._render_2d(camera, universe, entities)

    def _render_3d(self, camera, universe, entities):
        biome = universe.get_biome(int(camera.target[0]), int(camera.target[2]))
        biome_def = BIOMES.get(biome, BIOMES["plains"])
        clear_background(color(*biome_def.sky_color))
        begin_camera3d(camera.get_camera3d())
        terrain_blocks = self._collect_terrain_3d(universe)
        self._draw_terrain_blocks_3d(terrain_blocks)
        if entities:
            self._render_entities_3d(entities)
        self._render_path_3d(universe)
        self._render_weather(camera, universe)
        end_camera3d()

    def _collect_terrain_3d(self, universe):
        # 现在两条路径都不依赖相机位置：静态全量返回，或按世界中心做静态裁剪。
        # 所以这里只按 static_key 重建一次，之后直接复用同一份列表。
        static_key = (universe.seed, universe.size, universe.height)
        if static_key == self._terrain_static_key:
            return self._terrain_visible_blocks

        size = universe.size
        height = universe.height
        blocks = []
        for x in range(size):
            for z in range(size):
                for y in range(height):
                    block_id = universe.get_block(x, y, z)
                    if block_id == 0:
                        continue

                    faces = {
                        "top":    (y == height - 1) or not is_solid(universe.get_block(x, y + 1, z)),
                        "bottom": (y == 0) or not is_solid(universe.get_block(x, y - 1, z)),
                        "right":  (x == size - 1) or not is_solid(universe.get_block(x + 1, y, z)),
                        "left":   (x == 0) or not is_solid(universe.get_block(x - 1, y, z)),
                        "front":  (z == size - 1) or not is_solid(universe.get_block(x, y, z + 1)),
                        "back":   (z == 0) or not is_solid(universe.get_block(x, y, z - 1)),
                    }
                    if not any(faces.values()):
                        continue

                    blocks.append((x, y, z, block_id, faces))

        if len(blocks) <= config.MAX_VISIBLE_BLOCKS:
            visible = blocks
        else:
            world_center_x = universe.size * 0.5
            world_center_y = universe.height * 0.5
            world_center_z = universe.size * 0.5
            visible = []
            for x, y, z, block_id, faces in blocks:
                dx = (x + 0.5) - world_center_x
                dy = (y + 0.5) - world_center_y
                dz = (z + 0.5) - world_center_z
                dist_sq = dx * dx + dy * dy + dz * dz
                visible.append((dist_sq, x, y, z, block_id, faces))
            visible.sort(key=lambda item: item[0])
            visible = [
                (x, y, z, block_id, faces) for _dist, x, y, z, block_id, faces in visible[:config.MAX_VISIBLE_BLOCKS]
            ]

        self._terrain_static_key = static_key
        self._terrain_visible_blocks = visible
        try:
            self._build_terrain_mesh(visible)
        except Exception:
            self._unload_terrain_mesh()
        return self._terrain_visible_blocks

    def _draw_terrain_blocks_3d(self, blocks):
        if self._terrain_mesh is not None:
            rl.DrawMesh(self._terrain_mesh[0], self._terrain_material, self._IDENTITY_MATRIX)
            return
        for x, y, z, block_id, faces in blocks:
            self._draw_block_3d(x, y, z, block_id, faces)

    def _build_terrain_mesh(self, blocks):
        self._unload_terrain_mesh()
        if not blocks:
            return

        positions = []
        colors = []
        normals = []

        def add_vertex(px, py, pz, color):
            positions.extend((px, py, pz))
            colors.extend((color[0], color[1], color[2], color[3]))
            normals.extend((0.0, 0.0, 0.0))

        for x, y, z, block_id, faces in blocks:
            top_c, side_c, bottom_c = self._get_block_colors(block_id, self._get_block_def(block_id))
            if faces.get("top"):
                add_vertex(x, y + 1, z, top_c)
                add_vertex(x, y + 1, z + 1, top_c)
                add_vertex(x + 1, y + 1, z + 1, top_c)
                add_vertex(x, y + 1, z, top_c)
                add_vertex(x + 1, y + 1, z + 1, top_c)
                add_vertex(x + 1, y + 1, z, top_c)
            if faces.get("bottom"):
                add_vertex(x, y, z, bottom_c)
                add_vertex(x + 1, y, z, bottom_c)
                add_vertex(x + 1, y, z + 1, bottom_c)
                add_vertex(x, y, z, bottom_c)
                add_vertex(x + 1, y, z + 1, bottom_c)
                add_vertex(x, y, z + 1, bottom_c)
            if faces.get("right"):
                add_vertex(x + 1, y, z, side_c)
                add_vertex(x + 1, y + 1, z, side_c)
                add_vertex(x + 1, y + 1, z + 1, side_c)
                add_vertex(x + 1, y, z, side_c)
                add_vertex(x + 1, y + 1, z + 1, side_c)
                add_vertex(x + 1, y, z + 1, side_c)
            if faces.get("left"):
                add_vertex(x, y, z + 1, side_c)
                add_vertex(x, y + 1, z + 1, side_c)
                add_vertex(x, y + 1, z, side_c)
                add_vertex(x, y, z + 1, side_c)
                add_vertex(x, y + 1, z, side_c)
                add_vertex(x, y, z, side_c)
            if faces.get("front"):
                add_vertex(x + 1, y, z + 1, side_c)
                add_vertex(x + 1, y + 1, z + 1, side_c)
                add_vertex(x, y + 1, z + 1, side_c)
                add_vertex(x + 1, y, z + 1, side_c)
                add_vertex(x, y + 1, z + 1, side_c)
                add_vertex(x, y, z + 1, side_c)
            if faces.get("back"):
                add_vertex(x, y, z, side_c)
                add_vertex(x, y + 1, z, side_c)
                add_vertex(x + 1, y + 1, z, side_c)
                add_vertex(x, y, z, side_c)
                add_vertex(x + 1, y + 1, z, side_c)
                add_vertex(x + 1, y, z, side_c)

        if not positions:
            return

        img = rl.GenImageColor(1, 1, color(255, 255, 255))
        tex = rl.LoadTextureFromImage(img)
        material = rl.LoadMaterialDefault()
        material.maps[0].texture = tex
        material.maps[0].color.r = 255
        material.maps[0].color.g = 255
        material.maps[0].color.b = 255
        material.maps[0].color.a = 255
        material.maps[0].value = 1.0

        mesh_arr = rl.ffi.new("struct Mesh[]", 1)
        mesh = mesh_arr[0]
        verts_arr = rl.ffi.new("float[]", positions)
        cols_arr = rl.ffi.new("unsigned char[]", colors)
        norms_arr = rl.ffi.new("float[]", normals)

        mesh.vertexCount = len(positions) // 3
        mesh.triangleCount = mesh.vertexCount // 3
        mesh.vertices = verts_arr
        mesh.normals = norms_arr
        mesh.colors = cols_arr
        mesh.texcoords = rl.ffi.NULL
        mesh.texcoords2 = rl.ffi.NULL
        mesh.tangents = rl.ffi.NULL
        mesh.indices = rl.ffi.NULL
        mesh.boneCount = 0
        mesh.boneIndices = rl.ffi.NULL
        mesh.boneWeights = rl.ffi.NULL
        mesh.animVertices = rl.ffi.NULL
        mesh.animNormals = rl.ffi.NULL
        mesh.vaoId = 0
        mesh.vboId = rl.ffi.NULL

        rl.UploadMesh(mesh_arr, False)

        # 上传后 GPU 已持有数据(VAO/VBO);置空 CPU 指针,避免 UnloadMesh 对
        # cffi 数组执行 RL_FREE 造成堆损坏。DrawMesh 只读 VAO,不受影响。
        mesh.vertices = rl.ffi.NULL
        mesh.normals = rl.ffi.NULL
        mesh.colors = rl.ffi.NULL

        # 必须存“数组”本身,不能存 mesh_arr[0]:本 cffi 不会在 element-ref 被存
        # 起来时保活底层数组,存 [0] 会在函数返回后悬空 → vCount 读 0 → 地形消失。
        self._terrain_mesh = mesh_arr
        self._terrain_material = material
        self._terrain_image = img
        self._terrain_texture = tex

    def _unload_terrain_mesh(self):
        if self._terrain_mesh is not None:
            # UnloadMesh 的签名是 (struct Mesh) 按值传入，而 _terrain_mesh 存的是数组
            # （为了保活，见本文件 202/233 行），所以必须取 [0]，否则 cffi 抛 TypeError
            rl.UnloadMesh(self._terrain_mesh[0])
            self._terrain_mesh = None
        if self._terrain_material is not None:
            rl.UnloadMaterial(self._terrain_material)
            self._terrain_material = None
        if self._terrain_texture is not None:
            rl.UnloadTexture(self._terrain_texture)
            self._terrain_texture = None
        if self._terrain_image is not None:
            rl.UnloadImage(self._terrain_image)
            self._terrain_image = None

    def _get_block_def(self, block_id):
        block_def = self._block_def_cache.get(block_id)
        if block_def is None:
            block_def = get_block(block_id)
            self._block_def_cache[block_id] = block_def
        return block_def

    def _get_block_colors(self, block_id, block_def):
        """缓存每个方块 ID 的颜色，避免每个可见面每帧重复创建 Color。"""
        colors = self._block_color_cache.get(block_id)
        if colors is None:
            colors = (
                (*block_def.top_color, 255),
                (*block_def.side_color, 255),
                (*block_def.bottom_color, 255),
            )
            if block_def.transparent:
                colors = (
                    (*block_def.top_color, 179),
                    (*block_def.side_color, 179),
                    (*block_def.bottom_color, 179),
                )
            self._block_color_cache[block_id] = colors
        return colors

    def _draw_block_3d(self, x, y, z, block_id, faces):
        block_def = self._get_block_def(block_id)

        # 透明方块保留原视觉效果；颜色本身已缓存为带 alpha 的 Color。
        if block_def.transparent:
            set_alpha(1.0)

        top_c, side_c, bottom_c = self._get_block_colors(block_id, block_def)

        # 顶面 — 用 top_color（最亮）
        if faces.get("top"):
            rl.DrawTriangle3D((x, y+1, z), (x, y+1, z+1), (x+1, y+1, z+1), top_c)
            rl.DrawTriangle3D((x, y+1, z), (x+1, y+1, z+1), (x+1, y+1, z), top_c)

        # 底面 — 用 bottom_color（最暗）
        if faces.get("bottom"):
            rl.DrawTriangle3D((x, y, z), (x+1, y, z), (x+1, y, z+1), bottom_c)
            rl.DrawTriangle3D((x, y, z), (x+1, y, z+1), (x, y, z+1), bottom_c)

        # 右面 (x+1)
        if faces.get("right"):
            rl.DrawTriangle3D((x+1, y, z), (x+1, y+1, z), (x+1, y+1, z+1), side_c)
            rl.DrawTriangle3D((x+1, y, z), (x+1, y+1, z+1), (x+1, y, z+1), side_c)

        # 左面 (x)
        if faces.get("left"):
            rl.DrawTriangle3D((x, y, z+1), (x, y+1, z+1), (x, y+1, z), side_c)
            rl.DrawTriangle3D((x, y, z+1), (x, y+1, z), (x, y, z), side_c)

        # 前面 (z+1)
        if faces.get("front"):
            rl.DrawTriangle3D((x+1, y, z+1), (x+1, y+1, z+1), (x, y+1, z+1), side_c)
            rl.DrawTriangle3D((x+1, y, z+1), (x, y+1, z+1), (x, y, z+1), side_c)

        # 后面 (z)
        if faces.get("back"):
            rl.DrawTriangle3D((x, y, z), (x, y+1, z), (x+1, y+1, z), side_c)
            rl.DrawTriangle3D((x, y, z), (x+1, y+1, z), (x+1, y, z), side_c)

    def _render_entities_3d(self, entities):
        white_color = (200, 200, 210, 255)
        health_bg_color = (80, 30, 30, 255)
        health_color = (220, 60, 60, 255)
        projectile_color = (255, 255, 100, 255)

        for tower in entities.get("towers", []):
            tc = self._get_entity_color(config.TOWER_COLORS, tower.type_name, (128, 128, 128))
            pos = tower.position
            draw_cube((pos[0], pos[1] + 0.5, pos[2]), 0.6, 1.0, 0.6, tc)
            draw_cube((pos[0], pos[1] + 1.2, pos[2]), 0.4, 0.2, 0.4, white_color)

        for enemy in entities.get("enemies", []):
            ec = self._get_entity_color(config.ENEMY_COLORS, enemy.type_name, (200, 50, 50))
            pos = enemy.position
            draw_cube((pos[0], pos[1] + 0.5, pos[2]), 0.7, 1.0, 0.7, ec)
            # 血条
            hp_ratio = enemy.health / enemy.max_health
            bar_w = 0.8
            bar_y = pos[1] + 1.2
            draw_cube((pos[0], bar_y, pos[2]), bar_w, 0.08, 0.15, health_bg_color)
            draw_cube((pos[0] - (bar_w * (1 - hp_ratio)) / 2, bar_y, pos[2]),
                      bar_w * hp_ratio, 0.08, 0.15, health_color)

        for proj in entities.get("projectiles", []):
            if proj.active:
                pos = proj.position
                draw_cube((pos[0], pos[1], pos[2]), 0.15, 0.15, 0.15, projectile_color)

    def _get_entity_color(self, palette, key, fallback):
        cache = self._entity_color_cache
        color = cache.get(key)
        if color is None:
            color = (*palette.get(key, fallback), 255)
            cache[key] = color
        return color

    def _render_path_3d(self, universe):
        """渲染多条敌人路径 — 主路径实线，备选路径虚线"""
        raw_paths = universe.enemy_paths if universe.enemy_paths else ([universe.enemy_path] if universe.enemy_path else [])
        path_cache_key = (universe.seed, universe.size, universe.height, len(raw_paths))
        if path_cache_key != self._path_cache_key:
            entries = []
            for p_idx, raw_path in enumerate(raw_paths):
                path = raw_path.points if hasattr(raw_path, "points") else raw_path
                # 主路径用黄色实线，备选路径用淡色虚线
                if p_idx == 0:
                    path_rgb = (255, 200, 50)
                    base_alpha = 0.3
                    size_mult = 1.0
                else:
                    # 备选路径颜色逐渐变淡
                    r, g, b = (255, 180, 80)
                    path_rgb = (max(0, r - 40 * p_idx), max(0, g - 40 * p_idx), max(0, b - 20 * p_idx))
                    base_alpha = 0.15 - 0.03 * p_idx
                    size_mult = 0.7

                for i, (x, y, z) in enumerate(path):
                    # 备选路径用虚线（每隔一个点绘制）
                    if p_idx > 0 and i % 2 == 1:
                        continue

                    a = int((base_alpha if i % 2 == 0 else base_alpha * 0.5) * 255)
                    ty = universe.get_surface_height_smooth(x + 0.5, z + 0.5)
                    s = 0.3 * size_mult
                    entries.append((x + 0.5, ty + 0.025, z + 0.5, (*path_rgb, a), s))

            if universe.enemy_path:
                sx, sy, sz = universe.enemy_path[0]
                beacon_y = universe.get_surface_height_smooth(sx + 0.5, sz + 0.5)
                beacon = (sx + 0.5, beacon_y, sz + 0.5)
            else:
                beacon = None

            self._path_cache_key = path_cache_key
            self._path_cache = (entries, beacon)

        entries, beacon = self._path_cache
        for px, ty, pz, color, s in entries:
            draw_cube((px, ty, pz), s, 0.05, s, color)

        # 出生点标记：发光信标（仅主路径）
        if beacon is not None:
            beacon_y = beacon[1] + 0.5 + math.sin(self.frame_count * 0.1) * 0.2
            draw_cube((beacon[0], beacon_y, beacon[2]), 0.4, 0.4, 0.4, (255, 80, 80, 204))
            draw_cube((beacon[0], beacon_y, beacon[2]), 0.8, 0.8, 0.8, (255, 80, 80, 102))

    def _render_2d(self, camera, universe, entities):
        biome = universe.get_biome(int(camera.pos_2d[0]), int(camera.pos_2d[2]))
        biome_def = BIOMES.get(biome, BIOMES["plains"])
        clear_background(color(*biome_def.sky_color))
        begin_camera2d(camera.get_camera2d())
        self._render_terrain_2d(camera, universe)
        self._render_path_2d(universe)
        if entities:
            self._render_entities_2d(entities)
        self._render_weather(camera, universe)
        end_camera2d()

    def _render_terrain_2d(self, camera, universe):
        size = universe.size
        screen_w = rl.GetScreenWidth()
        screen_h = rl.GetScreenHeight()
        zoom = camera.zoom_2d
        half_w = screen_w / (2 * zoom)
        half_h = screen_h / (2 * zoom)
        x_min = max(0, int(camera.pos_2d[0] - half_w) - 1)
        x_max = min(size, int(camera.pos_2d[0] + half_w) + 1)
        z_min = max(0, int(camera.pos_2d[2] - half_h) - 1)
        z_max = min(size, int(camera.pos_2d[2] + half_h) + 1)

        for x in range(x_min, x_max):
            for z in range(z_min, z_max):
                top_y = universe.get_surface_height(x, z)
                block_id = universe.get_block(x, top_y, z)
                if block_id == 0:
                    block_id = universe.get_block(x, top_y - 1, z) if top_y > 0 else 0
                if block_id == 0:
                    continue
                top_c = (*get_block(block_id).top_color, 255)
                draw_rect_v((x, z), (1.0, 1.0), top_c)

    def _render_path_2d(self, universe):
        path_color = (255, 200, 50, 102)
        for x, y, z in universe.enemy_path:
            draw_rect_v((x + 0.3, z + 0.3), (0.4, 0.4), path_color)

    def _render_entities_2d(self, entities):
        health_bg_color = (80, 30, 30, 255)
        health_color = (200, 50, 50, 255)
        projectile_color = (255, 255, 100, 255)

        for tower in entities.get("towers", []):
            tc = (*config.TOWER_COLORS.get(tower.type_name, (128, 128, 128)), 255)
            pos = (tower.position[0], tower.position[2])
            draw_circle_v(pos, 0.3, tc)
            draw_circle_v(pos, tower.range, (100, 200, 100, 51))

        for enemy in entities.get("enemies", []):
            ec = (*config.ENEMY_COLORS.get(enemy.type_name, (200, 50, 50)), 255)
            draw_circle_v((enemy.position[0], enemy.position[2]), 0.25, ec)
            bar_w = 0.6
            bar_h = 0.1
            bar_x = enemy.position[0] - bar_w / 2
            bar_y = enemy.position[2] - 0.4
            draw_rect_v((bar_x, bar_y), (bar_w, bar_h), health_bg_color)
            hp_ratio = enemy.health / enemy.max_health
            draw_rect_v((bar_x, bar_y), (bar_w * hp_ratio, bar_h), health_color)

        for proj in entities.get("projectiles", []):
            if proj.active:
                draw_circle_v((proj.position[0], proj.position[2]), 0.1, projectile_color)

    def _render_weather(self, camera, universe):
        weather = universe.weather
        if weather.is_raining and weather.intensity > 0:
            self._render_rain(camera, universe,
                              int(weather.intensity * config.RAIN_PARTICLE_COUNT))
        elif weather.is_snowing:
            self._render_snow(camera, universe, config.SNOW_PARTICLE_COUNT)

    def _render_rain(self, camera, universe, count):
        if camera.is_3d():
            rain_color = (150, 180, 220, 76)
            base_x, base_z = camera.target[0], camera.target[2]
            wind_dx = math.cos(universe.weather.wind_direction) * universe.weather.wind_strength
            wind_dz = math.sin(universe.weather.wind_direction) * universe.weather.wind_strength
            for i in range(count // 4):
                x = base_x + (i % 11 - 5) * 1.2 + wind_dx
                y = ((i * 7.3 + self.frame_count * 0.7) % 15) + 5
                z = base_z + ((i * 3 + 2) % 11 - 5) * 1.2 + wind_dz
                draw_cube((x, y, z), 0.02, 0.5, 0.02, rain_color)
        else:
            rain_color = (150, 180, 220, 76)
            for i in range(count // 2):
                sx = (i * 17 + self.frame_count * 3) % rl.GetScreenWidth()
                sy = (i * 13 + self.frame_count * 8) % rl.GetScreenHeight()
                draw_line(sx, sy, sx - 2, sy + 10, rain_color)

    def _render_snow(self, camera, universe, count):
        if camera.is_3d():
            snow_color = (240, 245, 255, 128)
            base_x, base_z = camera.target[0], camera.target[2]
            wind_dx = math.cos(universe.weather.wind_direction) * universe.weather.wind_strength
            wind_dz = math.sin(universe.weather.wind_direction) * universe.weather.wind_strength
            for i in range(count // 4):
                x = base_x + (i % 7 - 3) * 2.0 + wind_dx * 0.8
                y = ((i * 5.9 + self.frame_count * 0.4) % 15) + 5
                z = base_z + ((i * 3 + 2) % 7 - 3) * 2.0 + wind_dz * 0.8
                draw_cube((x, y, z), 0.1, 0.1, 0.1, snow_color)
        else:
            snow_color = (240, 245, 255, 128)
            for i in range(count // 2):
                sx = (i * 23 + self.frame_count * 2) % rl.GetScreenWidth()
                sy = (i * 11 + self.frame_count * 5) % rl.GetScreenHeight()
                draw_circle(sx, sy, 2, snow_color)

    def unload(self):
        self._unload_terrain_mesh()
