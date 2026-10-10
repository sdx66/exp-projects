""" """

import math
import random
import array
import raylib as rl
import config
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
        self._chunk_meshes: dict = {}
        self._shared_material = None
        self._shared_image = None
        self._shared_texture = None
        self._terrain_chunk_key = None
        self._built_key = None
        self._last_biome = None
        self._path_mesh = None
        self._path_key = None
        self._last_visible = None
        self._prop_meshes: dict = {}
        self._prop_material = None
        self._prop_image = None
        self._prop_texture = None
        self._2d_terrain_cache = {}     # 2D           (x,z) -> color
        self._2d_terrain_key = None     # 2D        key
        self._2d_path_cache = []        # 2D      
        self._2d_path_key = None        # 2D          key
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
        self._fog_color = (130, 180, 230)
        self._fog_shader = None
        self._fog_loc_color = self._fog_loc_near = self._fog_loc_far = -1

    def init_3d(self):
        self._unload_terrain_mesh()
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

    def _render_3d(self, camera, universe, entities, camera_extra=None):
        biome = universe.get_biome(math.floor(camera.target[0]), math.floor(camera.target[2]))
        biome_def = BIOMES.get(biome, BIOMES["plains"])
        self._fog_color = biome_def.sky_color
        if biome != self._last_biome:
            self._last_biome = biome
            fr, fg, fb = self._fog_color
            rl.SetShaderValue(self._fog_shader, self._fog_loc_color, rl.ffi.new("float[]", [fr/255.0, fg/255.0, fb/255.0]), rl.SHADER_UNIFORM_VEC3)
            rl.SetShaderValue(self._fog_shader, self._fog_loc_near, rl.ffi.new("float[]", [config.FOG_NEAR]), rl.SHADER_UNIFORM_FLOAT)
            rl.SetShaderValue(self._fog_shader, self._fog_loc_far, rl.ffi.new("float[]", [config.FOG_FAR]), rl.SHADER_UNIFORM_FLOAT)
        eye_x = camera.target[0] + camera.distance * math.cos(camera.altitude) * math.sin(camera.azimuth)
        eye_y = camera.target[1] + camera.distance * math.sin(camera.altitude)
        eye_z = camera.target[2] + camera.distance * math.cos(camera.altitude) * math.cos(camera.azimuth)
        rl.SetShaderValue(self._fog_shader, self._fog_loc_cam, rl.ffi.new("float[]", [eye_x, eye_y, eye_z]), rl.SHADER_UNIFORM_VEC3)
        clear_background(color(*biome_def.sky_color))
        begin_camera3d(camera.get_camera3d())
        terrain_key = self._collect_terrain_3d(universe, camera)
        self._draw_terrain_blocks_3d(terrain_key)
        self._render_props_3d(universe, camera)
        self._render_base_3d(universe, camera)
        if entities:
            self._render_entities_3d(entities)
            self._render_pickups_3d(entities)
        self._render_path_3d(universe)
        self._render_weather(camera, universe)
        if camera_extra:
            camera_extra()
        end_camera3d()

    def _render_props_3d(self, universe, camera):
        """绘制道具（区块级烘焙网格，取代逐道具即时 DrawCube）"""
        chunks = self._last_visible or universe.get_visible_chunks(camera.target[0], camera.target[2])
        material = self._get_prop_material()
        live = set()
        for cx, cz in chunks:
            chunk = universe.chunks.get((cx, cz))
            if not chunk:
                continue
            key = (cx, cz)
            live.add(key)
            props = chunk.props
            n = len(props)
            entry = self._prop_meshes.get(key)
            if entry is not None and entry["props"] is props and entry["n"] == n:
                rl.DrawMesh(entry["mesh"][0], material, self._IDENTITY_MATRIX)
                continue
            self._build_prop_mesh(key, props, material)
        for key in list(self._prop_meshes.keys()):
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
        for f in faces:
            for px, py, pz in (f[0], f[1], f[2], f[0], f[2], f[3]):
                positions.extend((px, py, pz))
                colors.extend(c)

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

    def _build_prop_mesh(self, key, props, material):
        """把整个区块的道具烘成单个网格（仅在道具数量变化时重建）"""
        old = self._prop_meshes.pop(key, None)
        if old is not None:
            rl.UnloadMesh(old["mesh"][0])

        positions = array.array('f')
        colors = array.array('B')
        for prop in props:
            if prop.harvested:
                continue
            self._emit_prop_3d(prop, positions, colors)

        if not positions:
            return

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

        self._prop_meshes[key] = {"mesh": mesh_arr, "props": props, "n": len(props)}

    def _unload_prop_mesh(self, key):
        entry = self._prop_meshes.pop(key, None)
        if entry:
            rl.UnloadMesh(entry["mesh"][0])

    def _collect_terrain_3d(self, universe, camera):
        """Per-chunk mesh management with per-frame build budget"""
        chunks = universe.get_visible_chunks(camera.target[0], camera.target[2])
        chunk_key = frozenset(chunks)
        self._last_visible = chunks

        if chunk_key == self._built_key:
            # 静态相机：仍 touch LRU 防可见区块被淘汰
            for cx, cz in chunks:
                if (cx, cz) in universe.chunks:
                    universe._chunk_order.move_to_end((cx, cz))
            return chunk_key

        cam_x = camera.target[0]
        cam_z = camera.target[2]
        prev_keys = set(self._chunk_meshes.keys())
        cur_keys = set(chunk_key)

        # Unload chunks that left the visible set
        for key in prev_keys - cur_keys:
            self._unload_chunk_mesh(key)

        # Build meshes for chunks entering the visible set
        # numpy 向量化后单区块生成+收集约 3-4ms，预算 5 ≈ 20ms/帧
        worked = 0
        for cx, cz in chunks:
            if worked >= 5:
                break
            key = (cx, cz)
            if key in self._chunk_meshes:
                continue  # Already has mesh, reuse

            # get_chunk generates the chunk if not present, touches LRU
            chunk = universe.get_chunk(cx, cz)
            worked += 1

            blocks = self._collect_chunk_blocks(chunk, universe, cx, cz)
            if blocks:
                try:
                    self._build_chunk_mesh(key, blocks, cam_x, cam_z)
                except Exception as e:
                    print(f"[chunk mesh] {key}: {e}")
                    self._unload_chunk_mesh(key)

        # Only update _built_key when all visible chunks have meshes
        if all(k in self._chunk_meshes for k in cur_keys):
            self._built_key = chunk_key

        return chunk_key

    def _collect_chunk_blocks(self, chunk, universe, cx, cz):
        """Collect visible blocks for a single chunk"""
        cs = chunk.size
        ch = chunk.height
        sh = cs * ch
        tf = chunk.terrain_flat
        gx0 = chunk.gx
        gz0 = chunk.gz
        TOP, BOTTOM, RIGHT, LEFT, FRONT, BACK = 1, 2, 4, 8, 16, 32
        blocks = []

        for lx in range(cs):
            for lz in range(cs):
                gx = gx0 + lx
                gz = gz0 + lz
                base_idx = lx * sh + lz * ch
                for y in range(ch):
                    block_id = tf[base_idx + y]
                    if block_id == 0:
                        continue
                    face_mask = 0
                    if y == ch - 1:
                        face_mask |= TOP
                    elif not is_solid(tf[base_idx + y + 1]):
                        face_mask |= TOP
                    if y == 0:
                        face_mask |= BOTTOM
                    elif not is_solid(tf[base_idx + y - 1]):
                        face_mask |= BOTTOM
                    if lx == cs - 1:
                        nb = self._get_neighbor_block_safe(universe, cx + 1, 0, y, cz, lz)
                    else:
                        nb = tf[base_idx + sh + y]
                    if not is_solid(nb):
                        face_mask |= RIGHT
                    if lx == 0:
                        nb = self._get_neighbor_block_safe(universe, cx - 1, cs - 1, y, cz, lz)
                    else:
                        nb = tf[base_idx - sh + y]
                    if not is_solid(nb):
                        face_mask |= LEFT
                    if lz == cs - 1:
                        nb = self._get_neighbor_block_safe(universe, cx, lx, y, cz + 1, 0)
                    else:
                        nb = tf[base_idx + ch + y]
                    if not is_solid(nb):
                        face_mask |= FRONT
                    if lz == 0:
                        nb = self._get_neighbor_block_safe(universe, cx, lx, y, cz - 1, cs - 1)
                    else:
                        nb = tf[base_idx - ch + y]
                    if not is_solid(nb):
                        face_mask |= BACK
                    if face_mask:
                        blocks.append((gx, y, gz, block_id, face_mask))
        return blocks

    @staticmethod
    def _get_neighbor_block_safe(universe, ncx, nlx, y, ncz, nlz) -> int:
        """ """
        chunk = universe.get_chunk_safe(ncx, ncz)
        if chunk is None:
            return BLOCK_ID["air"]
        return chunk.get_block(nlx, y, nlz)

    def _draw_terrain_blocks_3d(self, chunk_key):
        """ """
        for key, entry in self._chunk_meshes.items():
            if key in chunk_key:
                rl.DrawMesh(entry['mesh'][0], entry['material'], self._IDENTITY_MATRIX)

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

    def _build_chunk_mesh(self, key, blocks, cam_x, cam_z):
        """ """
        material = self._get_shared_material()
        TOP, BOTTOM, RIGHT, LEFT, FRONT, BACK = 1, 2, 4, 8, 16, 32

        positions = array.array('f')
        colors = array.array('B')

        def add_face(v0, v1, v2, v3, c):
            """Emit 6 vertices (2 triangles) per face - no EBO, no fog"""
            for px, py, pz in (v0, v1, v2):
                positions.extend((px, py, pz))
                colors.extend(c)
            for px, py, pz in (v0, v2, v3):
                positions.extend((px, py, pz))
                colors.extend(c)

        for x, y, z, block_id, fm in blocks:
            top_c, side_c, bottom_c = self._get_block_colors(block_id, self._get_block_def(block_id))
            if fm & TOP:
                add_face((x, y+1, z), (x, y+1, z+1), (x+1, y+1, z+1), (x+1, y+1, z), top_c)
            if fm & BOTTOM:
                add_face((x, y, z), (x+1, y, z), (x+1, y, z+1), (x, y, z+1), bottom_c)
            if fm & RIGHT:
                add_face((x+1, y, z), (x+1, y+1, z), (x+1, y+1, z+1), (x+1, y, z+1), side_c)
            if fm & LEFT:
                add_face((x, y, z+1), (x, y+1, z+1), (x, y+1, z), (x, y, z), side_c)
            if fm & FRONT:
                add_face((x+1, y, z+1), (x+1, y+1, z+1), (x, y+1, z+1), (x, y, z+1), side_c)
            if fm & BACK:
                add_face((x, y, z), (x, y+1, z), (x+1, y+1, z), (x+1, y, z), side_c)

        if not positions:
            return

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

        self._chunk_meshes[key] = {'mesh': mesh_arr, 'material': material}

    def _unload_chunk_mesh(self, key):
        entry = self._chunk_meshes.pop(key, None)
        if entry:
            rl.UnloadMesh(entry['mesh'][0])

    def _unload_terrain_mesh(self):
        """ """
        for key in list(self._chunk_meshes.keys()):
            self._unload_chunk_mesh(key)
        for key in list(self._prop_meshes.keys()):
            self._unload_prop_mesh(key)
        if self._shared_material is not None:
            rl.UnloadMaterial(self._shared_material)
            self._shared_material = None
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
        health_bg_color = (80, 30, 30, 255)
        health_color = (200, 50, 50, 255)
        projectile_color = (255, 255, 100, 255)

        for tower in entities.get("towers", []):
            tc = tower.display_color
            pos = tower.position
            draw_cube((pos[0], pos[1], pos[2]), 0.5, 1.0, 0.5, tc)
            # Range indicator (faint circle on ground)
            draw_cube((pos[0], pos[1] - 0.05, pos[2]), tower.range * 2, 0.05, tower.range * 2, (100, 200, 100, 30))

        for enemy in entities.get("enemies", []):
            ec = (*config.ENEMY_COLORS.get(enemy.type_name, (200, 50, 50)), 255)
            pos = enemy.position
            draw_cube((pos[0], pos[1], pos[2]), 0.4, 0.6, 0.4, ec)
            # Health bar above enemy
            bar_w = 0.6
            bar_h = 0.08
            bar_x = pos[0] - bar_w / 2
            bar_y = pos[1] + 0.5
            bar_z = pos[2]
            draw_cube((bar_x, bar_y, bar_z), bar_w, bar_h, 0.05, health_bg_color)
            hp_ratio = enemy.health / enemy.max_health
            draw_cube((bar_x, bar_y, bar_z), bar_w * hp_ratio, bar_h, 0.05, health_color)

        for proj in entities.get("projectiles", []):
            if proj.active:
                pos = proj.position
                draw_cube((pos[0], pos[1], pos[2]), 0.15, 0.15, 0.15, projectile_color)

    def _render_pickups_3d(self, entities):
        """ """
        for pickup in entities.get("pickups", []):
            if not pickup.active:
                continue
            px, py, pz = pickup.position
            if pickup.item_type == "material":
                c = MATERIAL_COLORS.get(pickup.item_name, (255, 255, 255))
            elif pickup.item_type == "weapon":
                wdef = WEAPON_TYPES.get(pickup.item_name)
                c = wdef.color if wdef else (255, 255, 255)
            else:
                c = (255, 255, 255)
            # Floating pickup with glow
            float_y = py + 0.3 + math.sin(self.frame_count * 0.1) * 0.1
            draw_cube((px, float_y, pz), 0.2, 0.2, 0.2, (*c, 255))
            draw_cube((px, float_y, pz), 0.35, 0.35, 0.35, (*c, 60))

    def _build_path_mesh(self, universe):
        """Bake all enemy paths into a single static mesh"""
        if self._path_mesh:
            rl.UnloadMesh(self._path_mesh)
            self._path_mesh = None

        material = self._get_shared_material()
        positions = array.array('f')
        colors = array.array('B')

        for path in universe.enemy_paths:
            points = path.points
            for i in range(len(points) - 1):
                p1 = points[i]
                p2 = points[i + 1]
                x1, y1, z1 = p1
                x2, y2, z2 = p2
                dx = x2 - x1
                dz = z2 - z1
                seg_len = math.sqrt(dx * dx + dz * dz)
                steps = max(1, int(seg_len))
                for s in range(steps):
                    t = s / steps
                    # 路径点已是世界坐标（方块中心），不再额外 +0.5
                    x = x1 + dx * t
                    z = z1 + dz * t
                    # surface_y 是地表方块的 y 索引，方块顶面在 +1；再抬 0.05 避免贴地 z-fighting
                    y = universe.get_surface_height(math.floor(x - 0.5), math.floor(z - 0.5)) + 1.05
                    # 完整四边形（两个三角形覆盖整块）
                    for dx_c, dy_c, dz_c in [
                        (-0.45, 0, -0.45), (0.45, 0, 0.45), (0.45, 0, -0.45),
                        (-0.45, 0, -0.45), (-0.45, 0, 0.45), (0.45, 0, 0.45),
                    ]:
                        positions.extend((x + dx_c, y + dy_c + 0.1, z + dz_c))
                        colors.extend((180, 160, 120, 255))

        if not positions:
            return

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
        self._path_mesh = mesh_arr[0]
        self._path_key = len(universe.enemy_paths)

    def _render_path_3d(self, universe):
        """Draw baked path mesh"""
        if len(universe.enemy_paths) != self._path_key:
            self._build_path_mesh(universe)
        if self._path_mesh:
            rl.DrawMesh(self._path_mesh, self._get_shared_material(), self._IDENTITY_MATRIX)

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
        clear_background(color(*biome_def.sky_color))
        begin_camera2d(camera.get_camera2d())
        self._render_terrain_2d(camera, universe)
        self._render_props_2d(universe, camera)
        self._render_base_2d(universe, camera)
        self._render_path_2d(universe)
        if entities:
            self._render_entities_2d(entities)
        self._render_weather(camera, universe)
        if camera_extra:
            camera_extra()
        end_camera2d()

    def _render_props_2d(self, universe, camera=None):
        """ """
        if camera:
            chunks = universe.get_visible_chunks(camera.pos_2d[0], camera.pos_2d[2])
            for cx, cz in chunks:
                chunk = universe.chunks.get((cx, cz))
                if chunk:
                    for prop in chunk.props:
                        if not prop.harvested:
                            self._draw_prop_2d(prop)
        else:
            for prop in universe.props_by_cell.values():
                if not prop.harvested:
                    self._draw_prop_2d(prop)

    def _draw_prop_2d(self, prop):
        """ """
        x, z = prop.x + 0.5, prop.z + 0.5
        s = prop.size
        shape = prop.prop_type.shape
        c = prop.prop_type.color

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

        #
        cache_key = (cx, cz, zoom)
        if cache_key != self._2d_terrain_key:
            self._2d_terrain_cache = {}
            self._2d_terrain_key = cache_key

        for x in range(x_min, x_max):
            for z in range(z_min, z_max):
                key = (x, z)
                color = self._2d_terrain_cache.get(key)
                if color is None:
                    top_y = universe.get_surface_height(x, z)
                    block_id = universe.get_block(x, top_y, z)
                    if block_id == 0:
                        block_id = universe.get_block(x, max(0, top_y - 1), z)
                    if block_id == 0:
                        continue
                    color = (*get_block(block_id).top_color, 255)
                    self._2d_terrain_cache[key] = color
                draw_rect_v((x, z), (1.0, 1.0), color)

    def _render_path_2d(self, universe):
        """ """
        cache_key = len(universe.enemy_paths)
        if cache_key != self._2d_path_key:
            path_color = (255, 200, 50, 102)
            cached = []
            for path_data in universe.enemy_paths:
                for x, y, z in path_data.points:
                    # 0.4 宽格子，x/z 已是世界坐标（方块中心），故 -0.2 对齐
                    cached.append(((x - 0.2, z - 0.2), path_color))
            self._2d_path_cache = cached
            self._2d_path_key = cache_key

        for pos, color in self._2d_path_cache:
            draw_rect_v(pos, (0.4, 0.4), color)

    def _render_entities_2d(self, entities):
        health_bg_color = (80, 30, 30, 255)
        health_color = (200, 50, 50, 255)
        projectile_color = (255, 255, 100, 255)

        for tower in entities.get("towers", []):
            tc = tower.display_color
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
