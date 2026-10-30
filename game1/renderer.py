""" """

import math
import random
import array
import time
from collections import OrderedDict
import numpy as np
import raylib as rl
import config
import gamedata
from blocks import get_block, is_solid, BLOCK_ID
from biomes import BIOMES
from items import MATERIAL_COLORS, WEAPON_TYPES
from rl_utils import *

FOG_VS = b"""
#version 330
in vec3 vertexPosition;
in vec4 vertexColor;
out vec4 vColor;
out vec3 vWorld;
uniform mat4 mvp;
uniform mat4 matModel;
void main() {
    vec4 w = matModel * vec4(vertexPosition, 1.0);
    vWorld = w.xyz;
    vColor = vertexColor;
    gl_Position = mvp * vec4(vertexPosition, 1.0);
}
"""

FOG_FS = b"""
#version 330
in vec4 vColor;
in vec3 vWorld;
out vec4 fragColor;
uniform vec3 cameraPos;
uniform vec3 fogColor;
uniform float fogNear;
uniform float fogFar;
void main() {
    float dist = distance(vWorld, cameraPos);
    float t = clamp((dist - fogNear) / (fogFar - fogNear), 0.0, 1.0);
    float f = t * t * (3.0 - 2.0 * t);
    fragColor = vec4(mix(vColor.rgb, fogColor, f), vColor.a);
}
"""


class Renderer:
    def __init__(self):
        self.frame_count = 0
        self._terrain_static_key = None
        self._terrain_visible_blocks = []
        self._chunk_meshes: OrderedDict = OrderedDict()
        self._index_sentinel = None
        self._shared_material = None
        self._shared_image = None
        self._shared_texture = None
        self._terrain_chunk_key = None
        self._built_key = None
        self._last_biome = None
        self._path_meshes: dict = {}   # (sid, type_name, idx) -> (mesh_arr, bounds, PathData)
        self._path_key = None
        self._dirty_chunks: set = set()  # 挖掘后需重建的区块网格
        # 已确认需重建、排队等预算的区块（旧网格保留渲染，见 _collect_terrain_3d）
        self._stale_pending: set = set()
        # 地形编辑（挖掘/放置）待重建的区块（旧网格保留渲染）
        self._dirty_pending: set = set()
        self._last_visible = None
        self._force_sync_builds = False  # 调试/像素校验：忽略流式预算
        self._prop_meshes: OrderedDict = OrderedDict()
        self._cull_view = None
        self._cull_m = None
        self._cull_tan_x = 0.0
        self._cull_tan_y = 0.0
        self._frustum_culling = True
        self._visible_cache_key = None
        self._visible_cache = ()
        self._prop_material = None
        self._prop_image = None
        self._prop_texture = None
        # 2D 地形顶面色：按区块缓存（(cx,cz) -> 256 个颜色/None），
        # 相机跨格时不再整屏重算；LRU 上限防止长时间漫游无限增长。
        self._2d_chunk_colors = OrderedDict()
        self._2d_chunk_color_cache_max = 512
        self._2d_terrain_rev = 0
        # 2D 道具绘制数据：按区块缓存，避免每帧重复读属性/拼颜色。
        self._2d_prop_cache = OrderedDict()
        self._2d_prop_cache_max = 512
        self._2d_path_cache = []        # 2D      
        self._2d_path_key = None        # 2D          key
        self._2d_layer_tex = None
        self._2d_layer_key = None
        self._2d_last_key = None
        self._2d_settle = 0
        self._IDENTITY_MATRIX = (
            1.0, 0.0, 0.0, 0.0,
            0.0, 1.0, 0.0, 0.0,
            0.0, 0.0, 1.0, 0.0,
            0.0, 0.0, 0.0, 1.0,
        )
        self._block_color_cache = {}
        self._block_def_cache = {}
        self._entity_color_cache = {}
        self._pickup_color_cache = {}
        self._solid_lut = np.zeros(256, dtype=bool)
        self._face_color_lut = np.zeros((256, 3, 4), dtype=np.uint8)
        self._top_color_lut = np.zeros((256, 4), dtype=np.uint8)
        for block_id in range(256):
            block_def = get_block(block_id)
            self._solid_lut[block_id] = block_def.solid
            alpha = 179 if block_def.transparent else 255
            self._face_color_lut[block_id, 0] = (*block_def.top_color, alpha)
            self._face_color_lut[block_id, 1] = (*block_def.bottom_color, alpha)
            self._face_color_lut[block_id, 2] = (*block_def.side_color, alpha)
            # 2D 地形层始终不透明（与原 (*top_color, 255) 一致）
            self._top_color_lut[block_id, :3] = block_def.top_color
            self._top_color_lut[block_id, 3] = 255
        self._top_color_tuples = [
            tuple(int(v) for v in self._top_color_lut[i]) for i in range(256)]
        self._path_cache_key = None
        self._path_cache = []
        self._fog_color = (130, 180, 230)
        self._fog_shader = None
        self._fog_loc_color = self._fog_loc_near = self._fog_loc_far = -1
        # 当前帧实际生效的雾距（随相机距离缩放），_update_frustum 也用它封顶剔除半径
        self._fog_near_cur = config.FOG_NEAR
        self._fog_far_cur = config.FOG_FAR

    def init_3d(self):
        self._unload_terrain_mesh()
        if self._fog_shader is not None:
            rl.UnloadShader(self._fog_shader)
            self._fog_shader = None
        self._IDENTITY_MATRIX = (
            1.0, 0.0, 0.0, 0.0,
            0.0, 1.0, 0.0, 0.0,
            0.0, 0.0, 1.0, 0.0,
            0.0, 0.0, 0.0, 1.0,
        )
        self._fog_shader = rl.LoadShaderFromMemory(FOG_VS, FOG_FS)
        self._fog_loc_color = rl.GetShaderLocation(self._fog_shader, b"fogColor")
        self._fog_loc_near = rl.GetShaderLocation(self._fog_shader, b"fogNear")
        self._fog_loc_far = rl.GetShaderLocation(self._fog_shader, b"fogFar")
        self._fog_loc_cam = rl.GetShaderLocation(self._fog_shader, b"cameraPos")

    def render(self, camera, universe, entities=None, camera_extra=None):
        self.frame_count += 1
        if camera.is_3d():
            self._render_3d(camera, universe, entities, camera_extra)
        else:
            self._render_2d(camera, universe, entities, camera_extra)

    def _fog_range(self, camera):
        """按相机距离计算当前雾近/远。

        基准：默认相机距离（CAMERA_DISTANCE_DEFAULT）下雾 = FOG_NEAR/FOG_FAR
        （保持原有观感）；拉远时随距离等比放大，封顶 CAMERA_VIEW_DISTANCE_MAX，
        保证雾距始终不超过地形加载半径（无露边）。
        """
        d = camera.distance
        far = config.FOG_FAR * max(1.0, d / config.CAMERA_DISTANCE_DEFAULT)
        far = min(far, config.CAMERA_VIEW_DISTANCE_MAX)
        near = config.FOG_NEAR * (far / config.FOG_FAR)
        return near, far

    def _render_3d(self, camera, universe, entities, camera_extra=None):
        biome = universe.get_biome(math.floor(camera.target[0]), math.floor(camera.target[2]))
        biome_def = BIOMES.get(biome, BIOMES["plains"])
        self._fog_color = biome_def.sky_color
        # 雾距随相机距离每帧更新（拉远时可见范围随之扩大）
        fog_near, fog_far = self._fog_range(camera)
        self._fog_near_cur = fog_near
        self._fog_far_cur = fog_far
        if biome != self._last_biome:
            self._last_biome = biome
            fr, fg, fb = self._fog_color
            rl.SetShaderValue(self._fog_shader, self._fog_loc_color, rl.ffi.new("float[]", [fr/255.0, fg/255.0, fb/255.0]), rl.SHADER_UNIFORM_VEC3)
        rl.SetShaderValue(self._fog_shader, self._fog_loc_near, rl.ffi.new("float[]", [fog_near]), rl.SHADER_UNIFORM_FLOAT)
        rl.SetShaderValue(self._fog_shader, self._fog_loc_far, rl.ffi.new("float[]", [fog_far]), rl.SHADER_UNIFORM_FLOAT)
        cam3d = camera.get_camera3d()
        eye_x, eye_y, eye_z = cam3d[0]
        rl.SetShaderValue(self._fog_shader, self._fog_loc_cam, rl.ffi.new("float[]", [eye_x, eye_y, eye_z]), rl.SHADER_UNIFORM_VEC3)
        self._update_frustum(camera)
        clear_background(color(*biome_def.sky_color))
        begin_camera3d(cam3d)
        terrain_key = self._collect_terrain_3d(universe, camera)
        self._draw_terrain_blocks_3d(terrain_key)
        self._render_props_3d(universe, camera)
        self._render_base_3d(universe, camera)
        if entities:
            self._render_entities_3d(entities)
            self._render_spawners_3d(universe, entities)
            self._render_pickups_3d(entities)
        self._render_path_3d(universe, entities)
        self._render_weather(camera, universe)
        if camera_extra:
            camera_extra()
        end_camera3d()

    def _update_frustum(self, camera):
        cam3d = camera.get_camera3d()
        self._cull_near = 0.1
        # 加载/剔除半径：覆盖到目标的地形，但封顶 CAMERA_VIEW_DISTANCE_MAX，
        # 避免拉远到 2000+ 时一次性生成上万区块。雾远 ≤ 此半径，边缘被满雾盖住。
        view_dist = min(float(camera.distance) + config.CHUNK_SIZE * 2,
                        config.CAMERA_VIEW_DISTANCE_MAX)
        self._cull_far = max(float(self._fog_far_cur), view_dist)
        self._cull_chunk_radius = int(
            math.ceil(self._cull_far / config.CHUNK_SIZE)) + 2
        if not self._frustum_culling:
            self._cull_view = None
            self._cull_m = None
            return

        self._cull_view = rl.GetCameraMatrix(cam3d)
        m = self._cull_view
        self._cull_m = (
            float(m.m0), float(m.m1), float(m.m2),
            float(m.m4), float(m.m5), float(m.m6),
            float(m.m8), float(m.m9), float(m.m10),
            float(m.m12), float(m.m13), float(m.m14),
        )
        tan_y = math.tan(math.radians(cam3d[3]) * 0.5)
        aspect = rl.GetScreenWidth() / max(1, rl.GetScreenHeight())
        self._cull_tan_y = tan_y
        self._cull_tan_x = tan_y * aspect

    @staticmethod
    def _mesh_bounds(positions):
        if positions is None or len(positions) == 0:
            return None
        pts = np.asarray(positions, dtype=np.float32).reshape(-1, 3)
        return (float(pts[:, 0].min()), float(pts[:, 1].min()),
                float(pts[:, 2].min()), float(pts[:, 0].max()),
                float(pts[:, 1].max()), float(pts[:, 2].max()))

    def _aabb_visible(self, bounds):
        if bounds is None or self._cull_m is None:
            return True
        min_x, min_y, min_z, max_x, max_y, max_z = bounds
        cx = (min_x + max_x) * 0.5
        cy = (min_y + max_y) * 0.5
        cz = (min_z + max_z) * 0.5
        ex = (max_x - min_x) * 0.5 + 2.0
        ey = (max_y - min_y) * 0.5 + 2.0
        ez = (max_z - min_z) * 0.5 + 2.0

        m0, m1, m2, m4, m5, m6, m8, m9, m10, m12, m13, m14 = self._cull_m
        vcx = cx * m0 + cy * m4 + cz * m8 + m12
        vcy = cx * m1 + cy * m5 + cz * m9 + m13
        vcz = cx * m2 + cy * m6 + cz * m10 + m14
        vex = abs(m0) * ex + abs(m4) * ey + abs(m8) * ez
        vey = abs(m1) * ex + abs(m5) * ey + abs(m9) * ez
        vez = abs(m2) * ex + abs(m6) * ey + abs(m10) * ez

        tan_x = self._cull_tan_x
        tan_y = self._cull_tan_y
        near = self._cull_near

        if vcx + vcz * tan_x - vex - vez * tan_x > 0.0:
            return False
        if -vcx + vcz * tan_x - vex - vez * tan_x > 0.0:
            return False
        if vcy + vcz * tan_y - vey - vez * tan_y > 0.0:
            return False
        if -vcy + vcz * tan_y - vey - vez * tan_y > 0.0:
            return False
        if vcz - vez > -near:
            return False
        return True

    def _visible_chunks_3d(self, universe, camera):
        cam3d = camera.get_camera3d()
        cache_key = (cam3d, rl.GetScreenWidth(), rl.GetScreenHeight(),
                     self._cull_far, self._cull_near, self._frustum_culling)
        if cache_key == self._visible_cache_key:
            return self._visible_cache

        eye_x = float(cam3d[0][0])
        eye_z = float(cam3d[0][2])
        cs = config.CHUNK_SIZE
        max_dist = self._cull_far
        ecx = math.floor(eye_x / cs)
        ecz = math.floor(eye_z / cs)
        radius = self._cull_chunk_radius
        max_dist_sq = max_dist * max_dist
        world_top = float(config.WORLD_HEIGHT)
        # 视锥剔除内联：区块 bounds 在 x/z 方向恒为 16 宽、y 为 [0, world_top]，
        # 中心/半 extents 均为整数×0.5 的精确运算（≤2^53），预计算后逐格
        # 与 _aabb_visible 完全相同的 5 个比较、相同浮点操作顺序 → 可见区块
        # 集合逐格一致，仅省掉每格的元组构造与方法调用。
        m = self._cull_m
        frustum = m is not None
        if frustum:
            m0, m1, m2, m4, m5, m6, m8, m9, m10, m12, m13, m14 = m
            tan_x = self._cull_tan_x
            tan_y = self._cull_tan_y
            near = self._cull_near
            cy = world_top * 0.5            # (0 + world_top) * 0.5 精确
            ey = cy + 2.0                    # (world_top - 0) * 0.5 + 2 精确
            half = cs * 0.5                  # 8.0
            exz = half + 2.0                 # ex = ez = (cs - 0) * 0.5 + 2 = 10.0
            vex = abs(m0) * exz + abs(m4) * ey + abs(m8) * exz
            vey = abs(m1) * exz + abs(m5) * ey + abs(m9) * exz
            vez = abs(m2) * exz + abs(m6) * ey + abs(m10) * exz
        keys = []
        for dcx in range(-radius, radius + 1):
            for dcz in range(-radius, radius + 1):
                cx = ecx + dcx
                cz = ecz + dcz
                x0 = cx * cs
                z0 = cz * cs
                x1 = x0 + cs
                z1 = z0 + cs
                dx = max(x0 - eye_x, 0.0, eye_x - x1)
                dz = max(z0 - eye_z, 0.0, eye_z - z1)
                if dx * dx + dz * dz > max_dist_sq:
                    continue
                if frustum:
                    # cx_center = (x0 + x1) * 0.5 = x0 + half（精确）
                    vcx = float(x0) + half
                    vcz = float(z0) + half
                    cx_v = vcx * m0 + cy * m4 + vcz * m8 + m12
                    cy_v = vcx * m1 + cy * m5 + vcz * m9 + m13
                    cz_v = vcx * m2 + cy * m6 + vcz * m10 + m14
                    if cx_v + cz_v * tan_x - vex - vez * tan_x > 0.0:
                        continue
                    if -cx_v + cz_v * tan_x - vex - vez * tan_x > 0.0:
                        continue
                    if cy_v + cz_v * tan_y - vey - vez * tan_y > 0.0:
                        continue
                    if -cy_v + cz_v * tan_y - vey - vez * tan_y > 0.0:
                        continue
                    if cz_v - vez > -near:
                        continue
                keys.append((cx, cz))
        self._visible_cache_key = cache_key
        self._visible_cache = keys
        return keys

    def _render_props_3d(self, universe, camera):
        """绘制道具（区块级烘焙网格，取代逐道具即时 DrawCube）"""
        chunks = self._last_visible or self._visible_chunks_3d(universe, camera)
        material = self._get_prop_material()
        live = set()
        for cx, cz in chunks:
            chunk = universe.chunks.get((cx, cz))
            if not chunk:
                continue
            key = (cx, cz)
            if key not in self._chunk_meshes:
                continue
            live.add(key)
            props = chunk.props
            n = len(props)
            entry = self._prop_meshes.get(key)
            if entry is not None and entry["props"] is props and entry["n"] == n:
                rl.DrawMesh(entry["mesh"][0], material,
                            self._IDENTITY_MATRIX)
                self._prop_meshes.move_to_end(key)
                continue
            self._build_prop_mesh(key, props, material)

        limit = max(config.MAX_PROP_MESHES,
                    len(chunks) * config.CACHE_VISIBLE_FACTOR)
        if len(self._prop_meshes) > limit:
            for key in list(self._prop_meshes):
                if len(self._prop_meshes) <= limit:
                    break
                if key not in live:
                    self._unload_prop_mesh(key)

    @staticmethod
    def _to_color4(c):
        """道具颜色统一为 4 元组（与 rl_utils.color 对 3 元组的约定一致：alpha=255）"""
        if len(c) == 3:
            return (*c, 255)
        return c

    @staticmethod
    def _emit_cube(positions, colors, x, y, z, w, h, d, c):
        """输出居中方块的 6 个面顶点；面片顺序/缠绕与 _build_chunk_mesh 的 add_face 一致（外法线朝外）"""
        x0, x1 = x - w * 0.5, x + w * 0.5
        y0, y1 = y - h * 0.5, y + h * 0.5
        z0, z1 = z - d * 0.5, z + d * 0.5
        faces = (
            ((x0, y1, z0), (x0, y1, z1), (x1, y1, z1), (x1, y1, z0)),   # TOP    +y
            ((x0, y0, z0), (x1, y0, z0), (x1, y0, z1), (x0, y0, z1)),   # BOTTOM -y
            ((x1, y0, z0), (x1, y1, z0), (x1, y1, z1), (x1, y0, z1)),   # RIGHT  +x
            ((x0, y0, z1), (x0, y1, z1), (x0, y1, z0), (x0, y0, z0)),   # LEFT   -x
            ((x1, y0, z1), (x1, y1, z1), (x0, y1, z1), (x0, y0, z1)),   # FRONT  +z
            ((x0, y0, z0), (x0, y1, z0), (x1, y1, z0), (x1, y0, z0)),   # BACK   -z
        )
        for v0, v1, v2, v3 in faces:
            positions.extend((*v0, *v1, *v2, *v0, *v2, *v3))
        colors.extend(c * 36)

    def _emit_prop_3d(self, prop, positions, colors):
        """把单个道具的几何写入顶点缓冲（几何/颜色与原 _draw_prop_3d 完全一致）"""
        shape = prop.prop_type.shape
        x, y, z = prop.x + 0.5, prop.y, prop.z + 0.5
        s = prop.size

        if shape == "tree":
            self._emit_cube(positions, colors, x, y + 0.5, z,
                            0.3 * s, 1.0 * s, 0.3 * s, (100, 75, 40, 255))
            canopy_h = 1.0 + s * 0.5
            self._emit_cube(positions, colors, x, y + 1.5 * s, z,
                            1.0 * s, canopy_h, 1.0 * s, (60, 140, 50, 255))
            self._emit_cube(positions, colors, x, y + 2.2 * s, z,
                            0.6 * s, 0.5 * s, 0.6 * s, (80, 160, 60, 255))
        elif shape == "dead_tree":
            self._emit_cube(positions, colors, x, y + 0.5 * s, z,
                            0.2 * s, 1.2 * s, 0.2 * s, (80, 70, 55, 255))
            self._emit_cube(positions, colors, x - 0.2, y + 1.0 * s, z,
                            0.15 * s, 0.3 * s, 0.15 * s, (80, 70, 55, 255))
        elif shape == "cactus":
            self._emit_cube(positions, colors, x, y + 0.5 * s, z,
                            0.35 * s, 1.0 * s, 0.35 * s, (60, 130, 60, 255))
            self._emit_cube(positions, colors, x + 0.2 * s, y + 0.7 * s, z,
                            0.2 * s, 0.4 * s, 0.2 * s, (60, 130, 60, 255))
            self._emit_cube(positions, colors, x - 0.2 * s, y + 0.9 * s, z,
                            0.2 * s, 0.3 * s, 0.2 * s, (60, 130, 60, 255))
        elif shape == "rock":
            self._emit_cube(positions, colors, x, y + 0.25 * s, z,
                            s, 0.5 * s, s, (145, 145, 145, 255))
            self._emit_cube(positions, colors, x + 0.15 * s, y + 0.5 * s, z - 0.1 * s,
                            0.6 * s, 0.35 * s, 0.6 * s, (120, 120, 120, 255))
        elif shape == "ore_vein":
            self._emit_cube(positions, colors, x, y + 0.3 * s, z,
                            s, 0.6 * s, s, (160, 140, 120, 255))
            self._emit_cube(positions, colors, x, y + 0.5 * s, z,
                            0.4 * s, 0.3 * s, 0.4 * s, (200, 180, 140, 255))
        elif shape == "sand_mound":
            self._emit_cube(positions, colors, x, y + 0.15 * s, z,
                            s, 0.3 * s, s, (219, 207, 163, 255))
        elif shape == "bush":
            self._emit_cube(positions, colors, x, y + 0.25 * s, z,
                            0.6 * s, 0.5 * s, 0.6 * s, (50, 120, 40, 255))
            self._emit_cube(positions, colors, x + 0.15 * s, y + 0.45 * s, z,
                            0.3 * s, 0.25 * s, 0.3 * s, (70, 140, 50, 255))
        elif shape == "grass_tuft":
            self._emit_cube(positions, colors, x, y + 0.1 * s, z,
                            0.15 * s, 0.2 * s, 0.15 * s, (76, 175, 80, 255))
            self._emit_cube(positions, colors, x + 0.1 * s, y + 0.08 * s, z + 0.08 * s,
                            0.1 * s, 0.15 * s, 0.1 * s, (100, 190, 90, 255))
        elif shape == "flower":
            c = self._to_color4(prop.prop_type.color)
            self._emit_cube(positions, colors, x, y + 0.05 * s, z,
                            0.08 * s, 0.1 * s, 0.08 * s, c)
            self._emit_cube(positions, colors, x - 0.08 * s, y + 0.02 * s, z,
                            0.04 * s, 0.06 * s, 0.04 * s, (76, 175, 80, 255))
        elif shape == "snow_patch":
            self._emit_cube(positions, colors, x, y + 0.02 * s, z,
                            s, 0.04 * s, s, (240, 245, 255, 255))

    def _get_prop_material(self):
        """道具共享材质：顶点色着色、默认着色器（无雾），保持与即时模式一致的观感"""
        if self._prop_material is None:
            img = rl.GenImageColor(1, 1, color(255, 255, 255))
            tex = rl.LoadTextureFromImage(img)
            material = rl.LoadMaterialDefault()
            material.maps[0].texture = tex
            material.maps[0].color.r = 255
            material.maps[0].color.g = 255
            material.maps[0].color.b = 255
            material.maps[0].color.a = 255
            material.maps[0].value = 1.0
            self._prop_material = material
            self._prop_image = img
            self._prop_texture = tex
        return self._prop_material

    def _prop_cubes(self, prop, out):
        """把单个道具拆成若干立方体参数 (x, y, z, w, h, d, rgba)。

        与 _emit_prop_3d 的几何表达式逐项一致，供向量化烘焙使用。
        """
        shape = prop.prop_type.shape
        x, y, z = prop.x + 0.5, prop.y, prop.z + 0.5
        s = prop.size

        if shape == "tree":
            out.append((x, y + 0.5, z,
                        0.3 * s, 1.0 * s, 0.3 * s, (100, 75, 40, 255)))
            canopy_h = 1.0 + s * 0.5
            out.append((x, y + 1.5 * s, z,
                        1.0 * s, canopy_h, 1.0 * s, (60, 140, 50, 255)))
            out.append((x, y + 2.2 * s, z,
                        0.6 * s, 0.5 * s, 0.6 * s, (80, 160, 60, 255)))
        elif shape == "dead_tree":
            out.append((x, y + 0.5 * s, z,
                        0.2 * s, 1.2 * s, 0.2 * s, (80, 70, 55, 255)))
            out.append((x - 0.2, y + 1.0 * s, z,
                        0.15 * s, 0.3 * s, 0.15 * s, (80, 70, 55, 255)))
        elif shape == "cactus":
            out.append((x, y + 0.5 * s, z,
                        0.35 * s, 1.0 * s, 0.35 * s, (60, 130, 60, 255)))
            out.append((x + 0.2 * s, y + 0.7 * s, z,
                        0.2 * s, 0.4 * s, 0.2 * s, (60, 130, 60, 255)))
            out.append((x - 0.2 * s, y + 0.9 * s, z,
                        0.2 * s, 0.3 * s, 0.2 * s, (60, 130, 60, 255)))
        elif shape == "rock":
            out.append((x, y + 0.25 * s, z,
                        s, 0.5 * s, s, (145, 145, 145, 255)))
            out.append((x + 0.15 * s, y + 0.5 * s, z - 0.1 * s,
                        0.6 * s, 0.35 * s, 0.6 * s, (120, 120, 120, 255)))
        elif shape == "ore_vein":
            out.append((x, y + 0.3 * s, z,
                        s, 0.6 * s, s, (160, 140, 120, 255)))
            out.append((x, y + 0.5 * s, z,
                        0.4 * s, 0.3 * s, 0.4 * s, (200, 180, 140, 255)))
        elif shape == "sand_mound":
            out.append((x, y + 0.15 * s, z,
                        s, 0.3 * s, s, (219, 207, 163, 255)))
        elif shape == "bush":
            out.append((x, y + 0.25 * s, z,
                        0.6 * s, 0.5 * s, 0.6 * s, (50, 120, 40, 255)))
            out.append((x + 0.15 * s, y + 0.45 * s, z,
                        0.3 * s, 0.25 * s, 0.3 * s, (70, 140, 50, 255)))
        elif shape == "grass_tuft":
            out.append((x, y + 0.1 * s, z,
                        0.15 * s, 0.2 * s, 0.15 * s, (76, 175, 80, 255)))
            out.append((x + 0.1 * s, y + 0.08 * s, z + 0.08 * s,
                        0.1 * s, 0.15 * s, 0.1 * s, (100, 190, 90, 255)))
        elif shape == "flower":
            c = self._to_color4(prop.prop_type.color)
            out.append((x, y + 0.05 * s, z,
                        0.08 * s, 0.1 * s, 0.08 * s, c))
            out.append((x - 0.08 * s, y + 0.02 * s, z,
                        0.04 * s, 0.06 * s, 0.04 * s, (76, 175, 80, 255)))
        elif shape == "snow_patch":
            out.append((x, y + 0.02 * s, z,
                        s, 0.04 * s, s, (240, 245, 255, 255)))

    # 立方体 8 角点顺序：bit0=x1, bit1=y1, bit2=z1
    _CUBE_FACE_CORNERS = np.array([
        [2, 6, 7, 3],   # TOP    +y
        [0, 1, 5, 4],   # BOTTOM -y
        [1, 3, 7, 5],   # RIGHT  +x
        [4, 6, 2, 0],   # LEFT   -x
        [5, 7, 6, 4],   # FRONT  +z
        [0, 2, 3, 1],   # BACK   -z
    ], dtype=np.intp)

    @classmethod
    def _prop_geometry(cls, cubes):
        """向量化生成道具顶点/颜色缓冲（与逐顶点 _emit_cube 字节一致）。"""
        geom = np.array([c[:6] for c in cubes], dtype=np.float64)
        cols = np.array([c[6] for c in cubes], dtype=np.uint8)
        x0 = geom[:, 0] - geom[:, 3] * 0.5
        x1 = geom[:, 0] + geom[:, 3] * 0.5
        y0 = geom[:, 1] - geom[:, 4] * 0.5
        y1 = geom[:, 1] + geom[:, 4] * 0.5
        z0 = geom[:, 2] - geom[:, 5] * 0.5
        z1 = geom[:, 2] + geom[:, 5] * 0.5
        corners = np.stack([
            x0, y0, z0, x1, y0, z0, x0, y1, z0, x1, y1, z0,
            x0, y0, z1, x1, y0, z1, x0, y1, z1, x1, y1, z1,
        ], axis=1).reshape(-1, 8, 3)
        quads = corners[:, cls._CUBE_FACE_CORNERS, :]
        tris = quads[:, :, [0, 1, 2, 0, 2, 3], :]
        positions = tris.reshape(-1).astype(np.float32)
        colors = np.repeat(cols, 36, axis=0).reshape(-1)
        return positions, colors

    def _build_prop_mesh(self, key, props, material):
        """把整个区块的道具烘成单个网格（仅在道具数量变化时重建）"""
        old = self._prop_meshes.pop(key, None)
        if old is not None:
            rl.UnloadMesh(old["mesh"][0])

        cubes = []
        for prop in props:
            if prop.harvested:
                continue
            self._prop_cubes(prop, cubes)

        if not cubes:
            return

        positions, colors = self._prop_geometry(cubes)
        mesh_arr = rl.ffi.new("struct Mesh[]", 1)
        mesh = mesh_arr[0]
        verts_view = rl.ffi.from_buffer("float[]", positions)
        cols_view = rl.ffi.from_buffer("unsigned char[]", colors)

        mesh.vertexCount = positions.size // 3
        mesh.triangleCount = positions.size // 9
        mesh.vertices = verts_view
        mesh.normals = rl.ffi.NULL
        mesh.colors = cols_view
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
        mesh.vertices = rl.ffi.NULL
        mesh.colors = rl.ffi.NULL
        mesh.indices = rl.ffi.NULL

        self._prop_meshes[key] = {"mesh": mesh_arr, "props": props,
                                  "n": len(props),
                                  "bounds": self._mesh_bounds(positions)}

    def _unload_prop_mesh(self, key):
        entry = self._prop_meshes.pop(key, None)
        if entry:
            rl.UnloadMesh(entry["mesh"][0])

    def mark_chunks_dirty(self, keys):
        """挖掘等地形编辑后标脏区块网格（下一帧重建）。"""
        self._dirty_chunks.update(keys)
        # 2D 顶面色缓存失效：推进地形版本并丢弃受影响区块的颜色。
        self._2d_terrain_rev += 1
        for key in keys:
            self._2d_chunk_colors.pop(key, None)

    def _consume_dirty_chunks(self):
        """地形编辑过的区块转入重建队列；旧网格保留渲染到重建完成。

        不再先卸载：卸载后按预算排队会造成"已卸载未重建"的可见区块
        整帧无网格（挖掘高频标脏时的蓝天闪烁）。延迟 1-3 帧才显示
        挖掘结果，远比真空闪烁不显眼。
        """
        if not self._dirty_chunks:
            return
        for key in self._dirty_chunks:
            if key in self._chunk_meshes:
                self._dirty_pending.add(key)
        self._dirty_chunks.clear()

    def _collect_terrain_3d(self, universe, camera):
        """Per-chunk mesh management with per-frame build budget"""
        self._consume_dirty_chunks()
        chunks = self._visible_chunks_3d(universe, camera)
        chunk_key = frozenset(chunks)
        self._last_visible = chunks
        universe.set_chunk_limit(max(
            config.MAX_LOADED_CHUNKS,
            len(chunks) * config.CACHE_VISIBLE_FACTOR,
        ))

        if (chunk_key == self._built_key
                and all(k in self._chunk_meshes for k in chunk_key)
                and not self._stale_pending.intersection(chunk_key)
                and not self._dirty_pending.intersection(chunk_key)):
            # 静态相机：touch LRU 防可见区块被淘汰
            for cx, cz in chunks:
                if (cx, cz) in universe.chunks:
                    universe._chunk_order.move_to_end((cx, cz))
                if (cx, cz) in self._chunk_meshes:
                    self._chunk_meshes.move_to_end((cx, cz))
            return chunk_key

        cam3d = camera.get_camera3d()
        cam_x = float(cam3d[0][0])
        cam_z = float(cam3d[0][2])
        cur_keys = set(chunk_key)
        cs = config.CHUNK_SIZE
        half = cs * 0.5

        def dist2(k):
            dx = k[0] * cs + half - cam_x
            dz = k[1] * cs + half - cam_z
            return dx * dx + dz * dz

        # 邻居加载状态变化只需重建受影响区块（顶点/颜色缓存仍是原算法，
        # 单块构建已降到 ~1ms），确认后与缺口一起按预算逐帧重建。
        # 重建完成前旧网格继续渲染：先卸载再排队会让"已卸载未重建"的
        # 可见区块整帧无网格可画，表现为缩放时成片的蓝天闪烁。
        stale = []
        for key in chunks:
            entry = self._chunk_meshes.get(key)
            if entry is None:
                continue
            sig = self._neighbor_sig(universe, key)
            old_sig = entry.get('neighbors')
            if old_sig == sig:
                # 签名已一致（邻块又卸载/重生成回原状），旧的重建判定作废。
                self._stale_pending.discard(key)
                continue
            if key in self._stale_pending:
                stale.append(key)
                continue
            if self._neighbor_needs_rebuild(universe, key, old_sig):
                self._stale_pending.add(key)
                stale.append(key)
            else:
                # 邻块载入/卸载未改变本块可见面：沿用旧网格，只更新签名。
                entry['neighbors'] = sig

        dirty_queued = [k for k in self._dirty_pending if k in cur_keys]
        missing = [k for k in chunks if k not in self._chunk_meshes]
        if not stale and not missing and not dirty_queued:
            self._built_key = chunk_key
            for cx, cz in chunks:
                if (cx, cz) in universe.chunks:
                    universe._chunk_order.move_to_end((cx, cz))
                if (cx, cz) in self._chunk_meshes:
                    self._chunk_meshes.move_to_end((cx, cz))
            return chunk_key

        # 近处优先。首帧（无任何网格）一次性建完，保持原有开局体验；
        # 其余情况按每帧时间预算逐帧补齐，避免长卡顿。
        # 优先级：地形编辑（挖掘/放置要及时显示）> 邻接变化 > 新区块。
        entries = [(dist2(k), 0, k) for k in dirty_queued]
        entries += [(dist2(k), 1, k) for k in stale]
        entries += [(dist2(k), 2, k) for k in missing]
        entries.sort()
        sync_all = self._force_sync_builds or not self._chunk_meshes
        budget = config.CHUNK_BUILD_BUDGET_MS / 1000.0
        started = time.perf_counter()
        built = 0
        # 无雾近景（雾近距以内）的缺块不受预算限制：玩家视野内的地形
        # 缺席会直接泛蓝（快速平移时的闪烁）。单帧近景同步构建上限
        # 防止传送时一次性卡顿过长。
        near_d2 = float(self._fog_near_cur) ** 2
        near_built = 0

        for dist2_key, _prio, key in entries:
            if not sync_all and built > 0:
                over = (time.perf_counter() - started) >= budget
                if over and (dist2_key > near_d2 or near_built >= 16):
                    break
                if dist2_key <= near_d2:
                    near_built += 1
            cx, cz = key
            # 先备齐四邻数据再收集几何：面剔除结果与邻块加载顺序彻底解耦，
            # 新建/重建的网格永不存在"缺边界墙"，等待重建期间渲染的旧网格
            # 也就没有缺口（消除缩放/传送时的瞬时蓝斑）。
            universe.get_chunk(cx + 1, cz)
            universe.get_chunk(cx - 1, cz)
            universe.get_chunk(cx, cz + 1)
            universe.get_chunk(cx, cz - 1)
            # get_chunk generates the chunk if not present, touches LRU
            chunk = universe.get_chunk(cx, cz)
            geometry = self._collect_chunk_blocks(chunk, universe, cx, cz)
            try:
                self._build_chunk_mesh(key, geometry, cam_x, cam_z,
                                       self._neighbor_sig(universe, key))
            except Exception as e:
                print(f"[chunk mesh] {key}: {e}")
                # 构建失败时旧网格（若有）原样保留继续渲染，下帧重试；
                # 不再卸载，避免真空帧。
            self._stale_pending.discard(key)
            self._dirty_pending.discard(key)
            built += 1

        for key in chunk_key:
            if key in self._chunk_meshes:
                self._chunk_meshes.move_to_end(key)

        for key, entry in list(self._chunk_meshes.items()):
            if key not in cur_keys and not entry.get('cacheable'):
                self._unload_chunk_mesh(key)

        limit = max(len(chunk_key), config.MAX_CHUNK_MESHES,
                    len(chunk_key) * config.CACHE_VISIBLE_FACTOR)
        if len(self._chunk_meshes) > limit:
            for key in list(self._chunk_meshes):
                if len(self._chunk_meshes) <= limit:
                    break
                if key not in cur_keys:
                    self._unload_chunk_mesh(key)

        # Only update _built_key when all visible chunks have meshes
        if all(k in self._chunk_meshes for k in cur_keys):
            self._built_key = chunk_key
        return chunk_key

    @staticmethod
    def _neighbor_sig(universe, key):
        cx, cz = key
        return (
            (cx + 1, cz) in universe.chunks,
            (cx - 1, cz) in universe.chunks,
            (cx, cz + 1) in universe.chunks,
            (cx, cz - 1) in universe.chunks,
        )

    def _neighbor_needs_rebuild(self, universe, key, old_sig):
        """邻块载入后，本块边界是否真的出现新暴露面。

        缺失邻块在面剔除时按“实心”处理，所以：
        - 邻块卸载（True→False）：本块原边界面的可见性不变，无需重建；
        - 邻块载入（False→True）：本块边界实心、而邻块相邻格非实心处，
          本块的墙面原本被剔除，需要补出朝向邻块的面；邻块边界实心处
          旧网格仍然正确。
        返回 False 时可安全沿用旧网格（并更新邻居签名）。
        """
        if old_sig is None:
            return True
        chunk = universe.get_chunk_safe(*key)
        if chunk is None:
            return False
        cs = chunk.size
        height = chunk.height
        tf = np.frombuffer(chunk.terrain_flat, dtype=np.uint8).reshape(
            cs, cs, height)
        dirs = (
            ((key[0] + 1, key[1]), 0, 1),
            ((key[0] - 1, key[1]), 0, -1),
            ((key[0], key[1] + 1), 1, 1),
            ((key[0], key[1] - 1), 1, -1),
        )
        for i, ((ncx, ncz), axis, sign) in enumerate(dirs):
            if old_sig[i]:
                continue
            neighbor = universe.get_chunk_safe(ncx, ncz)
            if neighbor is None:
                continue
            y1 = min(height, max(chunk.max_y, neighbor.max_y) + 7)
            ntf = np.frombuffer(neighbor.terrain_flat, dtype=np.uint8).reshape(
                cs, cs, neighbor.height)
            if axis == 0:
                ti = cs - 1 if sign > 0 else 0
                ni = 0 if sign > 0 else cs - 1
                tcol = tf[ti, :, :y1]
                ncol = ntf[ni, :, :y1]
            else:
                ti = cs - 1 if sign > 0 else 0
                ni = 0 if sign > 0 else cs - 1
                tcol = tf[:, ti, :y1]
                ncol = ntf[:, ni, :y1]
            if np.any(self._solid_lut[tcol] & ~self._solid_lut[ncol]):
                return True
        return False

    def _neighbor_solid_x(self, universe, ncx, ncz, nlx, y0, y1, cs):
        chunk = universe.get_chunk_safe(ncx, ncz)
        if chunk is None:
            return np.ones((cs, y1 - y0), dtype=bool)
        tf = np.frombuffer(chunk.terrain_flat, dtype=np.uint8).reshape(
            cs, cs, chunk.height)
        return self._solid_lut[tf[nlx, :, y0:y1]]

    def _neighbor_solid_z(self, universe, ncx, ncz, nlz, y0, y1, cs):
        chunk = universe.get_chunk_safe(ncx, ncz)
        if chunk is None:
            return np.ones((cs, y1 - y0), dtype=bool)
        tf = np.frombuffer(chunk.terrain_flat, dtype=np.uint8).reshape(
            cs, cs, chunk.height)
        return self._solid_lut[tf[:, nlz, y0:y1]]

    def _chunk_band_bottom(self, chunk, universe, cx, cz, ch):
        """面可能出现的 y 下界（含下侧邻接检查余量）。

        天然地形是高度图：每列从基岩连续实心到列顶。本块侧面在边界处
        可能一直露到邻块最低列顶（邻块是深谷时），故取本块与已加载四邻
        的最低列顶。挖掘/放置编辑会破坏该结构（竖井、悬垂），编辑区块
        再取自身及四邻最低编辑高度，两者取更小值。
        """
        min_top = min(min(row) for row in chunk.surface_height)
        for ncx, ncz in ((cx + 1, cz), (cx - 1, cz),
                         (cx, cz + 1), (cx, cz - 1)):
            neighbor = universe.get_chunk_safe(ncx, ncz)
            if neighbor is not None:
                m = min(min(row) for row in neighbor.surface_height)
                if m < min_top:
                    min_top = m
        y0 = max(1, min_top - 1)
        edits_by_chunk = getattr(universe, "_edits_by_chunk", None)
        if edits_by_chunk:
            y_edit = ch
            found = False
            for key in ((cx, cz), (cx + 1, cz), (cx - 1, cz),
                        (cx, cz + 1), (cx, cz - 1)):
                edits = edits_by_chunk.get(key)
                if edits:
                    found = True
                    m = min(e[1] for e in edits)
                    if m < y_edit:
                        y_edit = m
            if found:
                y0 = min(y0, max(1, y_edit - 1))
        return y0

    def _collect_chunk_blocks(self, chunk, universe, cx, cz):
        """Collect visible faces for a single chunk as vertex/color buffers."""
        cs = chunk.size
        ch = chunk.height
        tf_full = np.frombuffer(chunk.terrain_flat, dtype=np.uint8).reshape(
            cs, cs, ch)
        # 只处理可能出现暴露面的 y 区间。天然地形为高度图（实心到列顶），
        # 大部分体积深埋地下不可能有面；编辑区块按最低编辑高度下探。
        # 面剔除与顶点顺序仍完全沿用原算法，只是裁掉必然无面的深层。
        y0 = self._chunk_band_bottom(chunk, universe, cx, cz, ch)
        # 所有非空气方块都低于 chunk.max_y（放置限高 = 自然地表 + 6）。
        y1 = min(ch, chunk.max_y + 7)
        if y1 <= y0:
            return None
        tf = tf_full[:, :, y0:y1]
        solid = self._solid_lut[tf]
        yy = np.arange(y0, y1, dtype=np.int32)[None, None, :]
        # 实际柱顶：每列最高实体方块 y。不要用 chunk.surface_height——敌人掘进
        # mark_passage 会把它降到通道高度，而上方的悬垂（拱/悬崖顶）仍是实体；
        # 若按通道高度裁切（within/max_top）会把悬垂面误删，表现为挖开后
        # 新暴露面/悬崖上部不被渲染。实际柱顶从实体方块求得，天然悬崖、
        # 玩家方块墙、拱门都正确呈现。
        actual_top = np.where(solid, yy, np.int32(0)).max(axis=2)
        max_top = min(ch, int(actual_top.max()) + 7)
        max_top = max(1, max_top)
        keep = max_top - y0
        if keep <= 0:
            return None
        ts = tf[:, :, :keep]
        solid = solid[:, :, :keep]

        yy = yy[:, :, :keep]
        within = yy < (actual_top[:, :, None] + 7)

        air = np.zeros((cs, cs, 1), dtype=bool)
        above = np.concatenate((solid[:, :, 1:], air), axis=2)
        below = np.empty_like(solid)
        below[:, :, 1:] = solid[:, :, :-1]
        if y0 > 0:
            below[:, :, 0] = self._solid_lut[tf_full[:, :, y0 - 1]]
        else:
            below[:, :, 0] = False

        x_plus = np.empty_like(solid)
        x_plus[:-1] = solid[1:]
        x_plus[-1] = self._neighbor_solid_x(universe, cx + 1, cz, 0,
                                           y0, max_top, cs)
        x_minus = np.empty_like(solid)
        x_minus[1:] = solid[:-1]
        x_minus[0] = self._neighbor_solid_x(universe, cx - 1, cz, cs - 1,
                                           y0, max_top, cs)
        z_plus = np.empty_like(solid)
        z_plus[:, :-1] = solid[:, 1:]
        z_plus[:, -1] = self._neighbor_solid_z(universe, cx, cz + 1, 0,
                                              y0, max_top, cs)
        z_minus = np.empty_like(solid)
        z_minus[:, 1:] = solid[:, :-1]
        z_minus[:, 0] = self._neighbor_solid_z(universe, cx, cz - 1, cs - 1,
                                              y0, max_top, cs)

        fm = np.zeros(solid.shape, dtype=np.uint8)
        fm |= (~above).astype(np.uint8) * 1
        fm |= (~below).astype(np.uint8) * 2
        fm |= (~x_plus).astype(np.uint8) * 4
        fm |= (~x_minus).astype(np.uint8) * 8
        fm |= (~z_plus).astype(np.uint8) * 16
        fm |= (~z_minus).astype(np.uint8) * 32

        exposed = (ts != 0) & within & (fm != 0)
        if not exposed.any():
            return None

        lx, lz, yk = np.nonzero(exposed)
        y = yk + y0
        ids = ts[lx, lz, yk]
        fm_e = fm[lx, lz, yk]
        gx = lx + chunk.gx
        gz = lz + chunk.gz
        lin = (lx * cs + lz) * max_top + y

        face_defs = (
            (1, 0, ((0, 1, 0), (0, 1, 1), (1, 1, 1), (1, 1, 0))),
            (2, 1, ((0, 0, 0), (1, 0, 0), (1, 0, 1), (0, 0, 1))),
            (4, 2, ((1, 0, 0), (1, 1, 0), (1, 1, 1), (1, 0, 1))),
            (8, 2, ((0, 0, 1), (0, 1, 1), (0, 1, 0), (0, 0, 0))),
            (16, 2, ((1, 0, 1), (1, 1, 1), (0, 1, 1), (0, 0, 1))),
            (32, 2, ((0, 0, 0), (0, 1, 0), (1, 1, 0), (1, 0, 0))),
        )
        pos_parts = []
        col_parts = []
        lin_parts = []
        prio_parts = []
        for prio, (bit, color_idx, corners) in enumerate(face_defs):
            sel = (fm_e & bit) != 0
            if bit == 2:
                sel &= y > 0
            n = int(sel.sum())
            if not n:
                continue
            bx = gx[sel]
            by = y[sel]
            bz = gz[sel]
            c = np.empty((n, 4, 3), dtype=np.float32)
            for vi, (ox, oy, oz) in enumerate(corners):
                c[:, vi, 0] = bx + ox
                c[:, vi, 1] = by + oy
                c[:, vi, 2] = bz + oz
            pos_parts.append(c)
            face_colors = self._face_color_lut[ids[sel], color_idx]
            col_parts.append(np.repeat(face_colors[:, None, :], 4, axis=1))
            lin_parts.append(lin[sel])
            prio_parts.append(np.full(n, prio, dtype=np.uint8))

        if not pos_parts:
            return None

        pos = np.concatenate(pos_parts, axis=0)
        col = np.concatenate(col_parts, axis=0)
        lin_all = np.concatenate(lin_parts)
        prio_all = np.concatenate(prio_parts)
        order = np.lexsort((prio_all, lin_all))
        pos = pos[order]
        col = col[order]
        bounds = self._mesh_bounds(pos.reshape(-1))
        quad = np.array((0, 1, 2, 0, 2, 3), dtype=np.uint16)
        segments = []
        max_quads = 16383
        for start in range(0, pos.shape[0], max_quads):
            end = min(start + max_quads, pos.shape[0])
            base = (np.arange(end - start, dtype=np.uint16) * 4)[:, None]
            idx = quad[None, :] + base
            segments.append((pos[start:end].reshape(-1),
                             col[start:end].reshape(-1),
                             idx.reshape(-1)))
        return segments, bounds

    def _draw_terrain_blocks_3d(self, chunk_key):
        """ """
        for key in sorted(self._last_visible or ()):
            entry = self._chunk_meshes.get(key)
            if entry is not None and self._aabb_visible(entry.get('bounds')):
                for mesh_arr in entry['meshes']:
                    rl.DrawMesh(mesh_arr[0], entry['material'],
                                self._IDENTITY_MATRIX)

    def _get_shared_material(self):
        """ """
        if self._shared_material is None:
            img = rl.GenImageColor(1, 1, color(255, 255, 255))
            tex = rl.LoadTextureFromImage(img)
            material = rl.LoadMaterialDefault()
            material.maps[0].texture = tex
            material.maps[0].color.r = 255
            material.maps[0].color.g = 255
            material.maps[0].color.b = 255
            material.maps[0].color.a = 255
            material.maps[0].value = 1.0
            material.shader = self._fog_shader
            self._shared_material = material
            self._shared_image = img
            self._shared_texture = tex
        return self._shared_material

    def _build_chunk_mesh(self, key, geometry, cam_x, cam_z, neighbors):
        """构建（或整体替换）区块网格。

        已有旧网格时先安装新条目、再释放旧 GPU 资源：替换过程原子，
        该区块任何一帧都有网格可画。
        """
        material = self._get_shared_material()
        if geometry is not None and not geometry[0]:
            geometry = None
        if geometry is None:
            # 全无暴露面的区块也记录空网格，避免每帧重复尝试构建。
            old = self._chunk_meshes.get(key)
            self._chunk_meshes[key] = {'meshes': [], 'material': material,
                                       'bounds': None, 'neighbors': neighbors,
                                       'cacheable': True}
            if old is not None:
                self._free_mesh_buffers(old)
            return
        segments, bounds = geometry

        sentinel = self._index_sentinel
        if sentinel is None:
            sentinel = rl.ffi.new("unsigned short[1]")
            self._index_sentinel = sentinel

        meshes = []
        for positions, colors, indices in segments:
            mesh_arr = rl.ffi.new("struct Mesh[]", 1)
            mesh = mesh_arr[0]
            verts_view = rl.ffi.from_buffer("float[]", positions)
            cols_view = rl.ffi.from_buffer("unsigned char[]", colors)
            idx_view = rl.ffi.from_buffer("unsigned short[]", indices)

            mesh.vertexCount = positions.size // 3
            mesh.triangleCount = indices.size // 3
            mesh.vertices = verts_view
            mesh.normals = rl.ffi.NULL
            mesh.colors = cols_view
            mesh.texcoords = rl.ffi.NULL
            mesh.texcoords2 = rl.ffi.NULL
            mesh.tangents = rl.ffi.NULL
            mesh.indices = idx_view
            mesh.boneCount = 0
            mesh.boneIndices = rl.ffi.NULL
            mesh.boneWeights = rl.ffi.NULL
            mesh.animVertices = rl.ffi.NULL
            mesh.animNormals = rl.ffi.NULL
            mesh.vaoId = 0
            mesh.vboId = rl.ffi.NULL

            rl.UploadMesh(mesh_arr, False)
            mesh.vertices = rl.ffi.NULL
            mesh.colors = rl.ffi.NULL
            mesh.indices = sentinel
            meshes.append(mesh_arr)

        old = self._chunk_meshes.get(key)
        self._chunk_meshes[key] = {'meshes': meshes, 'material': material,
                                   'bounds': bounds, 'neighbors': neighbors,
                                   'cacheable': True}
        if old is not None:
            self._free_mesh_buffers(old)

    def _free_mesh_buffers(self, entry):
        """释放一个网格条目的 GPU 资源（不动 _chunk_meshes 字典）。"""
        for mesh_arr in entry['meshes']:
            mesh_arr[0].indices = rl.ffi.NULL
            rl.UnloadMesh(mesh_arr[0])

    def _unload_chunk_mesh(self, key):
        entry = self._chunk_meshes.pop(key, None)
        if self._built_key is not None and key in self._built_key:
            self._built_key = None
        self._stale_pending.discard(key)
        self._dirty_pending.discard(key)
        if entry:
            self._free_mesh_buffers(entry)

    def _unload_terrain_mesh(self):
        """ """
        self._built_key = None
        self._stale_pending.clear()
        self._dirty_pending.clear()
        self._unload_path_meshes()
        for key in list(self._chunk_meshes.keys()):
            self._unload_chunk_mesh(key)
        for key in list(self._prop_meshes.keys()):
            self._unload_prop_mesh(key)
        if self._shared_material is not None:
            rl.UnloadMaterial(self._shared_material)
            self._shared_material = None
            # raylib 6 的 UnloadMaterial 会一并释放材质引用的 shader；
            # 这里清空引用，避免 unload() 再次 UnloadShader 造成 double free。
            self._fog_shader = None
        if self._shared_texture is not None:
            rl.UnloadTexture(self._shared_texture)
            self._shared_texture = None
        if self._shared_image is not None:
            rl.UnloadImage(self._shared_image)
            self._shared_image = None
        if self._prop_material is not None:
            rl.UnloadMaterial(self._prop_material)
            self._prop_material = None
        if self._prop_texture is not None:
            rl.UnloadTexture(self._prop_texture)
            self._prop_texture = None
        if self._prop_image is not None:
            rl.UnloadImage(self._prop_image)
            self._prop_image = None

    def _get_block_def(self, block_id):
        block_def = self._block_def_cache.get(block_id)
        if block_def is None:
            block_def = get_block(block_id)
            self._block_def_cache[block_id] = block_def
        return block_def

    def _get_block_colors(self, block_id, block_def):
        """Cache block colors per ID"""
        colors = self._block_color_cache.get(block_id)
        if colors is None:
            if block_def.transparent:
                colors = (
                    (*block_def.top_color, 179),
                    (*block_def.side_color, 179),
                    (*block_def.bottom_color, 179),
                )
            else:
                colors = (
                    (*block_def.top_color, 255),
                    (*block_def.side_color, 255),
                    (*block_def.bottom_color, 255),
                )
            self._block_color_cache[block_id] = colors
        return colors

    def _get_entity_color(self, palette, key, fallback):
        cache = self._entity_color_cache
        color = cache.get(key)
        if color is None:
            color = (*palette.get(key, fallback), 255)
            cache[key] = color
        return color

    def _render_base_3d(self, universe, camera):
        """ """
        bx = universe.base_x + 0.5
        bz = universe.base_z + 0.5
        # get_surface_height 返回地表方块的 y 索引，+1 才是方块顶面（世界坐标基准）
        base_y = universe.get_surface_height(universe.base_x, universe.base_z) + 1

        # 主塔身（2.2 那次注释改写时连代码一起被删，导致塔身"看不到了"，在此恢复）
        draw_cube((bx, base_y + 2.0, bz), 2.0, 4.0, 2.0, (180, 160, 140, 255))
        # Base platform
        draw_cube((bx, base_y + 4.5, bz), 2.4, 0.4, 2.4, (140, 120, 100, 255))
        # Flag pole
        flag_offset = math.sin(self.frame_count * 0.15) * 0.1
        draw_cube((bx + 0.5, base_y + 5.5, bz + flag_offset), 0.3, 1.2, 0.05, (220, 50, 50, 255))
        # Corner pillars
        for dx in (-1.5, 1.5):
            for dz in (-1.5, 1.5):
                draw_cube((bx + dx, base_y + 0.5, bz + dz), 0.5, 1.0, 0.5, (120, 100, 80, 255))
        # Gate
        draw_cube((bx, base_y + 0.75, bz - 1.5), 1.0, 1.5, 0.1, (80, 60, 40, 255))

        # Glow effect
        glow_y = base_y + 0.1 + math.sin(self.frame_count * 0.08) * 0.05
        draw_cube((bx, glow_y, bz), 3.0, 0.1, 3.0, (100, 200, 100, 60))

    def _render_entities_3d(self, entities):
        """ """
        draw = rl.DrawCube
        enemy_colors = gamedata.ENEMY_COLORS
        ecget = enemy_colors.get
        enemy_fb = (200, 50, 50)
        scout_color = (100, 150, 255, 255)
        # 敌人本色 4 元组按类型缓存（原每敌每帧重建 (*c, 255)）
        ecol_cache = {}
        eget = ecol_cache.get
        health_bg_color = (80, 30, 30, 255)
        health_color = (200, 50, 50, 255)
        health_fg = (60, 180, 80, 255)
        projectile_color = (255, 255, 100, 255)

        for tower in entities.get("towers", ()):
            tc = tower.display_color
            pos = tower.position
            # 防御塔（城墙）更高更粗，与攻击塔在视觉上明显区分
            if getattr(tower, "no_attack", False):
                draw((pos[0], pos[1] + 1.1, pos[2]), 0.6, 2.2, 0.6, tc)
                # 顶端垛口示意（垛口色按塔缓存：颜色只依赖塔本色）
                bc = getattr(tower, "_battlement_color", None)
                if bc is None or bc[0] is not tc:
                    bc = ((min(255, tc[0] + 30), min(255, tc[1] + 30),
                           min(255, tc[2] + 30), 255), tc)
                    tower._battlement_color = bc
                draw((pos[0], pos[1] + 2.3, pos[2]), 0.6, 0.2, 0.6, bc[0])
                # 挂载武器标记：每把武器在其本色小方块上凸出，便于数槽位
                for _mi, _m in enumerate(getattr(tower, "weapon_mounts", ())):
                    _wc = _m["wdef"].color
                    draw((pos[0] - 0.36, pos[1] + 1.0 + _mi * 0.26, pos[2]),
                         0.12, 0.14, 0.30, (_wc[0], _wc[1], _wc[2], 255))
            else:
                draw((pos[0], pos[1], pos[2]), 0.5, 1.0, 0.5, tc)
            # 塔体血条（受损时显示）
            if tower.hp < tower.max_hp:
                bar_w = 0.7
                bar_h = 0.08
                bar_y = pos[1] + (2.5 if getattr(tower, "no_attack", False) else 0.8)
                draw((pos[0], bar_y, pos[2]), bar_w, bar_h, 0.05, health_bg_color)
                hp_ratio = max(0.0, tower.hp / tower.max_hp)
                draw((pos[0] - bar_w * (1.0 - hp_ratio) * 0.5, bar_y, pos[2]),
                     bar_w * hp_ratio, bar_h, 0.05, health_fg)

        for enemy in entities.get("enemies", ()):
            if enemy.behavior in ("scout_wander", "scout_return"):
                ec = scout_color
            else:
                tn = enemy.type_name
                ec = eget(tn)
                if ec is None:
                    ec = (*ecget(tn, enemy_fb), 255)
                    ecol_cache[tn] = ec
            pos = enemy.position
            draw((pos[0], pos[1], pos[2]), 0.4, 0.6, 0.4, ec)
            # Health bar above enemy
            bar_w = 0.6
            bar_h = 0.08
            bar_x = pos[0] - bar_w / 2
            bar_y = pos[1] + 0.5
            bar_z = pos[2]
            draw((bar_x, bar_y, bar_z), bar_w, bar_h, 0.05, health_bg_color)
            hp_ratio = enemy.health / enemy.max_health
            draw((bar_x, bar_y, bar_z), bar_w * hp_ratio, bar_h, 0.05, health_color)

        for proj in entities.get("projectiles", ()):
            if proj.active:
                pos = proj.position
                draw((pos[0], pos[1], pos[2]), 0.15, 0.15, 0.15, projectile_color)

    def _render_spawners_3d(self, universe, entities):
        """刷怪点 3D 标记：颜色区分休眠/侦察/激活状态。"""
        spawners = entities.get("spawners", [])
        if not spawners:
            return
        mode_colors = {
            "dormant": (120, 120, 120, 200),
            "scouting": (255, 220, 0, 200),
            "active": (255, 50, 50, 200),
        }
        for rt, sp in spawners:
            cx, cz = sp.x + 0.5, sp.z + 0.5
            cy = sp.surface_y + 1
            c = mode_colors.get(rt.mode, (120, 120, 120, 200))
            # 底座方块
            draw_cube((cx, cy, cz), 0.8, 0.1, 0.8, c)
            # 状态光柱
            if rt.mode == "active":
                draw_cube((cx, cy + 0.5, cz), 0.2, 1.0, 0.2, (*c[:3], 80))
            elif rt.mode == "scouting":
                draw_cube((cx, cy + 0.3, cz), 0.15, 0.6, 0.15, (*c[:3], 60))

    def _render_spawners_2d(self, universe, entities):
        """刷怪点 2D 标记。"""
        spawners = entities.get("spawners", [])
        if not spawners:
            return
        mode_colors = {
            "dormant": (120, 120, 120, 200),
            "scouting": (255, 220, 0, 200),
            "active": (255, 50, 50, 200),
        }
        for rt, sp in spawners:
            cx, cz = sp.x + 0.5, sp.z + 0.5
            c = mode_colors.get(rt.mode, (120, 120, 120, 200))
            draw_circle_v((cx, cz), 0.4, c)
            if rt.mode == "active":
                draw_circle_v((cx, cz), 0.6, (*c[:3], 60))

    def _render_pickups_3d(self, entities):
        """ """
        draw = rl.DrawCube
        cache = self._pickup_color_cache
        bob = 0.3 + math.sin(self.frame_count * 0.1) * 0.1
        for pickup in entities.get("pickups", ()):
            if not pickup.active:
                continue
            key = (pickup.item_type, pickup.item_name)
            cols = cache.get(key)
            if cols is None:
                if pickup.item_type == "material":
                    c = MATERIAL_COLORS.get(pickup.item_name, (255, 255, 255))
                elif pickup.item_type == "weapon":
                    wdef = WEAPON_TYPES.get(pickup.item_name)
                    c = wdef.color if wdef else (255, 255, 255)
                else:
                    c = (255, 255, 255)
                cols = ((*c, 255), (*c, 60))
                cache[key] = cols
            px, py, pz = pickup.position
            float_y = py + bob
            draw((px, float_y, pz), 0.2, 0.2, 0.2, cols[0])
            draw((px, float_y, pz), 0.35, 0.35, 0.35, cols[1])

    def _build_single_path_mesh(self, pd, color=(180, 160, 120, 255)):
        """把单个路径烘成静态网格。

        路径点已带表面锚定 y（= 地表方块顶面 +1），段内位置/高度线性插值，
        不再逐点重算 fbm 地形（原 _build_path_mesh 每个路径点重算一次地形，
        全量重建时随总路径点线性恶化，是游戏后期单帧 1-2 秒卡死的根因）。
        color: 路径带颜色（按敌人类型淡染）。
        """
        points = pd.points
        if len(points) < 2:
            return None
        positions = array.array('f')
        colors = array.array('B')
        cr, cg, cb, ca = color

        for i in range(len(points) - 1):
            x1, y1, z1 = points[i]
            x2, y2, z2 = points[i + 1]
            dx = x2 - x1
            dz = z2 - z1
            seg_len = math.sqrt(dx * dx + dz * dz)
            steps = max(1, int(seg_len))
            for s in range(steps):
                t = s / steps
                # 路径点已是世界坐标（方块中心），x/z 不再 +0.5；
                # y 线性插值保持连续，再抬 0.05 避免贴地 z-fighting
                x = x1 + dx * t
                z = z1 + dz * t
                y = y1 + (y2 - y1) * t + 0.05
                # 完整四边形（两个三角形覆盖整块）
                for dx_c, dy_c, dz_c in [
                    (-0.45, 0, -0.45), (0.45, 0, 0.45), (0.45, 0, -0.45),
                    (-0.45, 0, -0.45), (-0.45, 0, 0.45), (0.45, 0, 0.45),
                ]:
                    positions.extend((x + dx_c, y + dy_c + 0.1, z + dz_c))
                    colors.extend((cr, cg, cb, ca))

        mesh_arr = rl.ffi.new("struct Mesh[]", 1)
        mesh = mesh_arr[0]
        verts_view = rl.ffi.from_buffer("float[]", positions)
        cols_view = rl.ffi.from_buffer("unsigned char[]", colors)

        mesh.vertexCount = len(positions) // 3
        mesh.triangleCount = len(positions) // 9
        mesh.vertices = verts_view
        mesh.normals = rl.ffi.NULL
        mesh.colors = cols_view
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
        mesh.vertices = rl.ffi.NULL
        mesh.colors = rl.ffi.NULL
        mesh.indices = rl.ffi.NULL
        return mesh_arr, self._mesh_bounds(positions)

    @staticmethod
    def _path_color_for(key):
        """路径带颜色：主变体 = 类型色与路径色 50% 混合；备用变体更灰。"""
        _, type_name, idx = key
        base = gamedata.ENEMY_COLORS.get(type_name, (200, 50, 50))
        mix = 0.5 if idx == 0 else 0.22
        tan = (180, 160, 120)
        blend = tuple(int(t * (1 - mix) + b * mix) for t, b in zip(tan, base))
        return (*blend, 255)

    def _desired_paths(self, universe, entities):
        """当前应渲染的路径集 {(sid, type_name, idx): PathData}。"""
        game = entities.get("game") if entities else None
        pf = getattr(game, "pathfinder", None)
        if pf is not None:
            return pf.render_paths()
        # 兜底：legacy 单路径
        return {("legacy", sid, 0): pd
                for sid, pd in universe.enemy_paths.items()}

    def _sync_path_meshes(self, universe, entities=None):
        """增量同步路径网格：只烘焙新增/失效路径，逐路径缓存。

        失效判定用 PathData 对象同一性（重路由会产生新 PathData 对象）。
        """
        desired = self._desired_paths(universe, entities)
        meshes = self._path_meshes
        for key in list(meshes):
            if key not in desired or desired[key] is not meshes[key][2]:
                rl.UnloadMesh(meshes.pop(key)[0][0])
        baked = 0
        for key, pd in desired.items():
            entry = meshes.get(key)
            if entry is not None and entry[2] is pd:
                continue
            m = self._build_single_path_mesh(pd, self._path_color_for(key))
            if m is not None:
                meshes[key] = (m[0], m[1], pd)
            baked += 1
            if baked >= 24:
                break

    def _render_path_3d(self, universe, entities=None):
        """逐路径网格绘制（增量缓存，路径数变化不卡帧）"""
        self._sync_path_meshes(universe, entities)
        material = self._get_shared_material()
        for entry in self._path_meshes.values():
            rl.DrawMesh(entry[0][0], material, self._IDENTITY_MATRIX)

    def _unload_path_meshes(self):
        """卸载全部路径网格。"""
        for entry in list(self._path_meshes.values()):
            rl.UnloadMesh(entry[0][0])
        self._path_meshes.clear()
        self._path_key = None

    def _render_base_2d(self, universe, camera):
        """ """
        bx = universe.base_x + 0.5
        bz = universe.base_z + 0.5
        # 主基地外圈（2.2 注释改写时被误删，恢复）
        draw_circle_v((bx, bz), 1.2, (180, 160, 140, 255))
        # 内圈
        draw_circle_v((bx, bz), 0.8, (140, 120, 100, 255))
        # 发光标记
        glow_r = 1.5 + math.sin(self.frame_count * 0.08) * 0.2
        draw_circle_v((bx, bz), glow_r, (100, 200, 100, 60))

    def _render_2d(self, camera, universe, entities, camera_extra=None):
        biome = universe.get_biome(math.floor(camera.pos_2d[0]), math.floor(camera.pos_2d[2]))
        biome_def = BIOMES.get(biome, BIOMES["plains"])
        sky = color(*biome_def.sky_color)
        screen_w = rl.GetScreenWidth()
        screen_h = rl.GetScreenHeight()
        layer_key = (camera.pos_2d[0], camera.pos_2d[2], camera.zoom_2d,
                     screen_w, screen_h, len(universe.props),
                     self._2d_terrain_rev)
        # 相机静止若干帧后才烘焙整屏图层；移动期间直接画地形/道具，
        # 避免每帧 LoadImageFromScreen + 上传纹理（原先约 5ms/帧）。
        if layer_key == self._2d_last_key:
            self._2d_settle += 1
        else:
            self._2d_last_key = layer_key
            self._2d_settle = 0
        cache_hit = (self._2d_layer_tex is not None
                     and layer_key == self._2d_layer_key)
        clear_background(sky)
        if cache_hit:
            rl.SetTextureFilter(self._2d_layer_tex,
                                rl.TEXTURE_FILTER_POINT)
            rl.DrawTexture(self._2d_layer_tex, 0, 0,
                           (255, 255, 255, 255))
        else:
            begin_camera2d(camera.get_camera2d())
            self._render_terrain_2d(camera, universe)
            self._render_props_2d(universe, camera)
            end_camera2d()
            if self._2d_settle >= 2:
                rl.rlDrawRenderBatchActive()
                image = rl.LoadImageFromScreen()
                pixels = np.frombuffer(
                    rl.ffi.buffer(image.data, image.width * image.height * 4),
                    dtype=np.uint8)
                pixels[3::4] = 255
                if self._2d_layer_tex is not None:
                    rl.UnloadTexture(self._2d_layer_tex)
                self._2d_layer_tex = rl.LoadTextureFromImage(image)
                rl.UnloadImage(image)
                rl.SetTextureFilter(self._2d_layer_tex,
                                    rl.TEXTURE_FILTER_POINT)
                self._2d_layer_key = layer_key
        begin_camera2d(camera.get_camera2d())
        self._render_base_2d(universe, camera)
        self._render_path_2d(universe, entities)
        if entities:
            self._render_entities_2d(entities)
            self._render_spawners_2d(universe, entities)
        if camera_extra:
            camera_extra()
        end_camera2d()
        # 雨雪粒子用屏幕坐标，必须画在 2D 相机变换之外（否则被当作世界坐标，
        # 只覆盖画面一角且不随镜头）。
        self._render_weather(camera, universe)

    def _render_props_2d(self, universe, camera=None):
        """绘制可见区块道具（按区块缓存绘制数据，移动时直接重画）"""
        if camera:
            chunks = universe.get_visible_chunks(camera.pos_2d[0], camera.pos_2d[2])
            draw_circle = rl.DrawCircleV
            draw_rect = rl.DrawRectangleV
            cache = self._2d_prop_cache
            # 屏幕变换参数（Camera2D: screen = (world - target) * zoom + offset）
            zoom = camera.zoom_2d
            cam_x = camera.pos_2d[0]
            cam_z = camera.pos_2d[2]
            half_w = rl.GetScreenWidth() * 0.5
            half_h = rl.GetScreenHeight() * 0.5
            screen_w = half_w * 2.0
            screen_h = half_h * 2.0
            cs = config.CHUNK_SIZE
            # 道具几何最大偏离格心 0.6 世界单位（max_size=2.0：圆 0.3*2 / cactus 0.6*2），
            # 2.0 边距保守覆盖；zoom 缩放后边距按比例 → 始终 ≥ 实际像素 extent。
            # 整块在屏幕外时跳过其全部图元（像素不变：raylib 本就会裁剪屏外绘制）。
            margin = 2.0 * zoom
            floor = math.floor
            eps = 1e-3
            # 快速路径：zoom=1 时所有圆形道具都在格心 (x+0.5, z+0.5)，
            # 到最近像素中心的偏移只取决于相机小数部分，可整帧只算一次。
            fast_zoom = (zoom == 1.0)
            if fast_zoom:
                bfx = half_w - cam_x
                bfy = half_h - cam_z
                gdx = bfx - floor(bfx + 0.5)
                gdy = bfy - floor(bfy + 0.5)
                gdist2 = gdx * gdx + gdy * gdy
                aligned = (gdx == 0.0 and gdy == 0.0)
            for cx, cz in chunks:
                chunk = universe.chunks.get((cx, cz))
                if not chunk or not chunk.props:
                    continue
                # 区块级屏外剔除（边距已含最大道具 extent）
                wx0 = cx * cs
                wz0 = cz * cs
                if ((wx0 + cs - cam_x) * zoom + half_w + margin < 0.0
                        or (wx0 - cam_x) * zoom + half_w - margin > screen_w
                        or (wz0 + cs - cam_z) * zoom + half_h + margin < 0.0
                        or (wz0 - cam_z) * zoom + half_h - margin > screen_h):
                    continue
                key = (cx, cz)
                props = chunk.props
                sig = (id(chunk), id(props), len(props))
                entry = cache.get(key)
                if entry is not None and entry[0] == sig:
                    prims = entry[1]
                    cache.move_to_end(key)
                else:
                    prims = self._build_2d_prop_prims(props)
                    cache[key] = (sig, prims)
                    cache.move_to_end(key)
                    if len(cache) > self._2d_prop_cache_max:
                        cache.popitem(last=False)
                for kind, p1, p2, c in prims:
                    if kind == 0:
                        if fast_zoom:
                            r = p2
                            if gdist2 > (r + eps) * (r + eps):
                                continue
                            if aligned and r < 0.5 - eps:
                                # 圆心恰在像素中心且半径 < 半个像素：扇形只覆盖
                                # 该像素，用 1x1 方片等价替换（像素完全一致）。
                                draw_rect((p1[0] - 0.5, p1[1] - 0.5),
                                          (1.0, 1.0), c)
                            else:
                                draw_circle(p1, p2, c)
                            continue
                        # 亚像素圆：raylib 用 36 边内接扇形光栅化，只有像素中心
                        # 落在圆内才可能着色。若最近像素中心距离 > 半径，跳过必然
                        # 不改变任何像素（保守判定，半径用外接圆）。
                        sx = (p1[0] - cam_x) * zoom + half_w
                        sy = (p1[1] - cam_z) * zoom + half_h
                        r = p2 * zoom
                        fx = sx - 0.5
                        fy = sy - 0.5
                        dx = fx - floor(fx + 0.5)
                        dy = fy - floor(fy + 0.5)
                        if dx * dx + dy * dy > (r + eps) * (r + eps):
                            continue
                        if dx == 0.0 and dy == 0.0 and r < 0.5 - eps:
                            draw_rect((p1[0] - 0.5 / zoom,
                                       p1[1] - 0.5 / zoom),
                                      (1.0 / zoom, 1.0 / zoom), c)
                        else:
                            draw_circle(p1, p2, c)
                    else:
                        sx = (p1[0] - cam_x) * zoom + half_w
                        sy = (p1[1] - cam_z) * zoom + half_h
                        ex = sx + p2[0] * zoom
                        ey = sy + p2[1] * zoom
                        i0 = math.ceil(sx - 0.5 - eps)
                        i1 = math.floor(ex - 0.5 + eps)
                        j0 = math.ceil(sy - 0.5 - eps)
                        j1 = math.floor(ey - 0.5 + eps)
                        if i0 > i1 or j0 > j1:
                            continue
                        draw_rect(p1, p2, c)
        else:
            for prop in universe.props_by_cell.values():
                if not prop.harvested:
                    self._draw_prop_2d(prop)

    def _build_2d_prop_prims(self, props):
        """把区块道具展开为绘制图元（形状/颜色与 _draw_prop_2d 完全一致）"""
        prims = []
        for prop in props:
            if prop.harvested:
                continue
            x, z = prop.x + 0.5, prop.z + 0.5
            s = prop.size
            shape = prop.prop_type.shape
            c = prop.prop_type.color
            # raylib 的 Color 需要 4 通道：3 元组会被 cffi 置 alpha=0。
            if len(c) == 3:
                c = (*c, 255)
            if shape in ("tree", "dead_tree"):
                prims.append((0, (x, z), 0.25 * s, c))
            elif shape == "cactus":
                prims.append((1, (x - 0.08, z - 0.3 * s),
                              (0.16, 0.6 * s), c))
            elif shape in ("rock", "ore_vein"):
                prims.append((0, (x, z), 0.3 * s, c))
            elif shape == "sand_mound":
                prims.append((0, (x, z), 0.2 * s, c))
            elif shape == "bush":
                prims.append((0, (x, z), 0.2 * s, c))
            elif shape == "grass_tuft":
                prims.append((0, (x, z), 0.08 * s, c))
            elif shape == "flower":
                prims.append((0, (x, z), 0.06 * s, c))
            elif shape == "snow_patch":
                prims.append((0, (x, z), 0.15 * s, (*c[:3], 128)))
        return prims

    def _draw_prop_2d(self, prop):
        """ """
        x, z = prop.x + 0.5, prop.z + 0.5
        s = prop.size
        shape = prop.prop_type.shape
        c = prop.prop_type.color
        # raylib 的 Color 需要 4 通道：3 元组会被 cffi 置 alpha=0 → 完全透明，
        # 2D 视图下所有植被/道具不可见。补默认 alpha=255。
        if len(c) == 3:
            c = (*c, 255)

        if shape in ("tree", "dead_tree"):
            draw_circle_v((x, z), 0.25 * s, c)
        elif shape == "cactus":
            draw_rect_v((x - 0.08, z - 0.3 * s), (0.16, 0.6 * s), c)
        elif shape in ("rock", "ore_vein"):
            draw_circle_v((x, z), 0.3 * s, c)
        elif shape == "sand_mound":
            draw_circle_v((x, z), 0.2 * s, c)
        elif shape == "bush":
            draw_circle_v((x, z), 0.2 * s, c)
        elif shape == "grass_tuft":
            draw_circle_v((x, z), 0.08 * s, c)
        elif shape == "flower":
            draw_circle_v((x, z), 0.06 * s, c)
        elif shape == "snow_patch":
            draw_circle_v((x, z), 0.15 * s, (*c[:3], 128))

    def _get_2d_chunk_colors(self, universe, cx, cz):
        """获取区块的 2D 地表绘制缓存（16 行 run-length 段 + 垂直合并矩形）。"""
        key = (cx, cz)
        entry = self._2d_chunk_colors.get(key)
        if entry is not None:
            self._2d_chunk_colors.move_to_end(key)
            return entry[1]
        rows = self._build_2d_chunk_colors(universe, cx, cz)
        merged = self._merge_rows_vertical(rows)
        self._2d_chunk_colors[key] = (rows, merged)
        if len(self._2d_chunk_colors) > self._2d_chunk_color_cache_max:
            self._2d_chunk_colors.popitem(last=False)
        return merged

    @staticmethod
    def _merge_rows_vertical(rows):
        """把逐行 run 合并成垂直矩形 (lx, lz, w, h, color)。

        2D 地形层颜色恒为 alpha=255（_top_color_lut 第 4 通道恒 255），
        且矩形互不重叠——合并后任一像素仍被同色矩形恰好覆盖一次，
        逐像素结果与逐行绘制一致（不透明矩形与绘制顺序无关）。
        """
        merged = []
        consumed = [set() for _ in rows]  # consumed[lz]: 该行已并入上方矩形的 run 下标
        for lz, runs in enumerate(rows):
            for ri, (start, length, rcolor) in enumerate(runs):
                if ri in consumed[lz]:
                    continue
                h = 1
                if rcolor[3] == 255:
                    while lz + h < len(rows):
                        nxt = rows[lz + h]
                        # 行内 run 起点互不相同，同 (start, length, color) 至多一个
                        for rj, (s2, l2, c2) in enumerate(nxt):
                            if (s2 == start and l2 == length and c2 is rcolor
                                    and rj not in consumed[lz + h]):
                                consumed[lz + h].add(rj)
                                h += 1
                                break
                        else:
                            break
                merged.append((start, lz, length, h, rcolor))
        return merged

    def _build_2d_chunk_colors(self, universe, cx, cz):
        """烘焙一个区块 16x16 的地表顶面色（与原逐格 get_block 逻辑一致）。

        返回 16 行、每行若干 (起始 lx, 长度, 颜色) 段；同色连续格已合并。
        """
        cs = config.CHUNK_SIZE
        chunk = universe.get_chunk(cx, cz)
        size = chunk.size
        height = chunk.height
        blocks = np.frombuffer(chunk.terrain_flat, dtype=np.uint8).reshape(
            size, size, height)
        surface = np.asarray(chunk.surface_height, dtype=np.int64)
        ix = np.arange(size, dtype=np.int64)[:, None]
        iz = np.arange(size, dtype=np.int64)[None, :]

        # 原逻辑：top = get_block(surface)；若为 air 再取 surface-1。
        # surface 可能等于 height（越界），此时 get_block 返回 air。
        top = np.zeros((size, size), dtype=np.uint8)
        ok_top = (surface >= 0) & (surface < height)
        if ok_top.any():
            y_top = np.clip(surface, 0, height - 1)
            top[ok_top] = blocks[ix, iz, y_top][ok_top]
        second = np.zeros((size, size), dtype=np.uint8)
        y2 = surface - 1
        ok_second = (y2 >= 0) & (y2 < height)
        if ok_second.any():
            y2c = np.clip(y2, 0, height - 1)
            second[ok_second] = blocks[ix, iz, y2c][ok_second]
        ids = np.where(top != 0, top, second)

        lut = self._top_color_tuples
        # ids 以 [lx, lz] 排列；转成 [lz, lx] 后逐行做 run-length。
        flat_ids = ids.T.ravel().tolist()
        rows = []
        for lz in range(size):
            runs = []
            base = lz * size
            run_color = None
            run_start = 0
            run_len = 0
            for lx in range(size):
                bid = flat_ids[base + lx]
                color = lut[bid] if bid else None
                if color is None:
                    if run_len:
                        runs.append((run_start, run_len, run_color))
                    run_color = None
                    run_len = 0
                elif run_len and color == run_color:
                    run_len += 1
                else:
                    if run_len:
                        runs.append((run_start, run_len, run_color))
                    run_color = color
                    run_start = lx
                    run_len = 1
            if run_len:
                runs.append((run_start, run_len, run_color))
            rows.append(runs)
        return rows

    def _render_terrain_2d(self, camera, universe):
        screen_w = rl.GetScreenWidth()
        screen_h = rl.GetScreenHeight()
        zoom = camera.zoom_2d
        half_w = screen_w / (2 * zoom)
        half_h = screen_h / (2 * zoom)
        # 可见裁剪：限制到 RENDER_DISTANCE 区块范围，防止低 zoom 下遍历数亿格
        max_half = config.RENDER_DISTANCE * config.CHUNK_SIZE
        half_w = min(half_w, max_half)
        half_h = min(half_h, max_half)
        cx = math.floor(camera.pos_2d[0])
        cz = math.floor(camera.pos_2d[2])
        x_min = math.floor(cx - half_w) - 1
        x_max = math.floor(cx + half_w) + 1
        z_min = math.floor(cz - half_h) - 1
        z_max = math.floor(cz + half_h) + 1

        cs = config.CHUNK_SIZE
        draw_rect = rl.DrawRectangleV
        get_merged = self._get_2d_chunk_colors
        c0x = x_min // cs
        c1x = (x_max - 1) // cs
        c0z = z_min // cs
        c1z = (z_max - 1) // cs
        for czc in range(c0z, c1z + 1):
            chunk_z0 = czc * cs
            for cxc in range(c0x, c1x + 1):
                chunk_x0 = cxc * cs
                for lx, lz, w, h, run_color in get_merged(universe, cxc, czc):
                    wx0 = chunk_x0 + lx
                    wz0 = chunk_z0 + lz
                    wx1 = wx0 + w
                    wz1 = wz0 + h
                    # 裁剪到可见窗口（与原先逐行 [x, end) 裁剪覆盖完全一致）
                    if wx1 <= x_min or wx0 >= x_max \
                            or wz1 <= z_min or wz0 >= z_max:
                        continue
                    if wx0 < x_min:
                        wx0 = x_min
                    if wz0 < z_min:
                        wz0 = z_min
                    if wx1 > x_max:
                        wx1 = x_max
                    if wz1 > z_max:
                        wz1 = z_max
                    draw_rect((wx0, wz0),
                              (float(wx1 - wx0), float(wz1 - wz0)), run_color)

    def _render_path_2d(self, universe, entities=None):
        """ """
        desired = self._desired_paths(universe, entities)
        cache_key = frozenset((k, id(pd)) for k, pd in desired.items())
        if cache_key != self._2d_path_key:
            cached = []
            for key, pd in desired.items():
                _, type_name, idx = key
                base = gamedata.ENEMY_COLORS.get(type_name, (200, 50, 50))
                mix = 0.55 if idx == 0 else 0.25
                c = (int(255 * (1 - mix) + base[0] * mix),
                     int(200 * (1 - mix) + base[1] * mix),
                     int(50 * (1 - mix) + base[2] * mix), 102)
                for x, y, z in pd.points:
                    # 0.4 宽格子，x/z 已是世界坐标（方块中心），故 -0.2 对齐
                    cached.append(((x - 0.2, z - 0.2), c))
            self._2d_path_cache = cached
            self._2d_path_key = cache_key

        for pos, color in self._2d_path_cache:
            draw_rect_v(pos, (0.4, 0.4), color)

    def _render_entities_2d(self, entities):
        health_bg_color = (80, 30, 30, 255)
        health_color = (200, 50, 50, 255)
        health_fg = (60, 180, 80, 255)
        projectile_color = (255, 255, 100, 255)
        scout_color = (100, 150, 255, 255)
        ecget = gamedata.ENEMY_COLORS.get
        enemy_fb = (200, 50, 50)
        ecol_cache = {}
        eget = ecol_cache.get

        for tower in entities.get("towers", []):
            tc = tower.display_color
            pos = (tower.position[0], tower.position[2])
            # 防御塔（城墙）画成方块；攻击塔画成圆点
            if getattr(tower, "no_attack", False):
                w = 0.45
                draw_rect_v((pos[0] - w, pos[1] - w), (w * 2, w * 2), tc)
                # 挂载武器标记：方块上方一条暗色底 + 武器本色小点，可数槽位
                _mounts = getattr(tower, "weapon_mounts", ())
                if _mounts:
                    draw_rect_v((pos[0] - 0.30, pos[1] - 0.70), (0.60, 0.16),
                                (20, 22, 30, 210))
                    for _mi, _m in enumerate(_mounts[:3]):
                        _wc = _m["wdef"].color
                        draw_circle_v((pos[0] - 0.20 + _mi * 0.20,
                                        pos[1] - 0.62), 0.06,
                                       (_wc[0], _wc[1], _wc[2], 255))
            else:
                draw_circle_v(pos, 0.3, tc)
            # 塔体血条（受损时显示）
            if tower.hp < tower.max_hp:
                hp_ratio = max(0.0, tower.hp / tower.max_hp)
                bw = 0.6
                by = pos[1] - 0.5
                draw_rect_v((pos[0] - bw / 2, by), (bw, 0.08),
                            health_bg_color)
                draw_rect_v((pos[0] - bw / 2, by), (bw * hp_ratio, 0.08),
                            health_fg)

        for enemy in entities.get("enemies", []):
            if enemy.behavior in ("scout_wander", "scout_return"):
                ec = scout_color
            else:
                tn = enemy.type_name
                ec = eget(tn)
                if ec is None:
                    ec = (*ecget(tn, enemy_fb), 255)
                    ecol_cache[tn] = ec
            epos = enemy.position
            draw_circle_v((epos[0], epos[2]), 0.25, ec)
            bar_w = 0.6
            bar_h = 0.1
            bar_x = epos[0] - bar_w / 2
            bar_y = epos[2] - 0.4
            draw_rect_v((bar_x, bar_y), (bar_w, bar_h), health_bg_color)
            hp_ratio = enemy.health / enemy.max_health
            draw_rect_v((bar_x, bar_y), (bar_w * hp_ratio, bar_h), health_color)

        for proj in entities.get("projectiles", []):
            if proj.active:
                draw_circle_v((proj.position[0], proj.position[2]), 0.1, projectile_color)

        for pickup in entities.get("pickups", []):
            if not pickup.active:
                continue
            px, pz = pickup.position[0], pickup.position[2]
            if pickup.item_type == "material":
                c = MATERIAL_COLORS.get(pickup.item_name, (255, 255, 255))
            elif pickup.item_type == "weapon":
                wdef = WEAPON_TYPES.get(pickup.item_name)
                c = wdef.color if wdef else (255, 255, 255)
            else:
                c = (255, 255, 255)
            draw_circle_v((px, pz), 0.15, (*c, 255))
            draw_circle_v((px, pz), 0.25, (*c, 80))

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
        if self._2d_layer_tex is not None:
            rl.UnloadTexture(self._2d_layer_tex)
            self._2d_layer_tex = None
            self._2d_layer_key = None
        self._2d_chunk_colors.clear()
        self._2d_prop_cache.clear()
        self._2d_last_key = None
        self._2d_settle = 0
        self._2d_terrain_rev = 0
        # 雾着色器：LoadShaderFromMemory 创建后从不释放，每次读档泄漏一个 GPU shader
        if self._fog_shader is not None:
            rl.UnloadShader(self._fog_shader)
            self._fog_shader = None
