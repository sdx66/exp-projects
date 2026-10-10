"""
camera.py - 相机控制
支持 3D 轨道相机 和 2D 正交相机，V 键切换。
raylib 6.x: 结构体通过 tuple 传入
"""

import math
import raylib as rl
import config
from rl_utils import vec2, vec3, camera2d, camera3d


class Camera:
    """
    游戏相机，支持 2D/3D 模式切换。
    3D 模式：轨道相机（azimuth, altitude, distance）
    2D 模式：正交俯视图（position, zoom）
    """

    def __init__(self, universe=None):
        self.mode = "3d"
        self.universe = universe

        # 3D 相机参数
        # 看向大本营位置（世界原点）
        tx = 0
        tz = 0
        if universe:
            tx = universe.base_x
            tz = universe.base_z
            tx_i, tz_i = int(tx), int(tz)
            ty = universe.get_surface_height(tx_i, tz_i) + 1
        else:
            ty = 8
        self.target = vec3(tx, ty, tz)
        self.distance = 42.0
        self.altitude = 0.5   # ~29° 侧面视角，地形更立体
        self.azimuth = 0.6   # 从前方偏侧观察
        self.distance_target = self.distance
        self.altitude_target = self.altitude
        self.azimuth_target = self.azimuth

        # 2D 相机参数
        self.pos_2d = vec3(0, 0, 0)  # 大本营位置
        self.zoom_2d = 1.0
        self.zoom_2d_target = 1.0

        # 输入状态
        self._rotating = False
        self._last_mouse_x = 0
        self._last_mouse_y = 0

    def update(self, dt: float):
        """每帧更新相机"""
        self._handle_input(dt)

        smooth = 1.0 - math.exp(-8.0 * dt)

        if self.mode == "3d":
            self.distance += (self.distance_target - self.distance) * smooth
            self.altitude += (self.altitude_target - self.altitude) * smooth
            self.azimuth += (self.azimuth_target - self.azimuth) * smooth

            self.distance = max(config.CAMERA_DISTANCE_MIN,
                                min(config.CAMERA_DISTANCE_MAX, self.distance))
            self.altitude = max(config.CAMERA_ALTITUDE_MIN,
                                 min(config.CAMERA_ALTITUDE_MAX, self.altitude))
        else:
            self.zoom_2d += (self.zoom_2d_target - self.zoom_2d) * smooth
            self.zoom_2d = max(config.CAMERA_2D_ZOOM_MIN,
                                min(config.CAMERA_2D_ZOOM_MAX, self.zoom_2d))

    def get_camera3d(self):
        """获取 Camera3D tuple"""
        x = self.target[0] + self.distance * math.cos(self.altitude) * math.sin(self.azimuth)
        y = self.target[1] + self.distance * math.sin(self.altitude)
        z = self.target[2] + self.distance * math.cos(self.altitude) * math.cos(self.azimuth)

        return camera3d(
            px=x, py=y, pz=z,
            tx=self.target[0], ty=self.target[1], tz=self.target[2],
            ux=0, uy=1, uz=0,
            fovy=60.0, proj=0  # CAMERA_PERSPECTIVE
        )

    def get_camera2d(self):
        """获取 Camera2D tuple"""
        return camera2d(
            ox=rl.GetScreenWidth() / 2,
            oy=rl.GetScreenHeight() / 2,
            tx=self.pos_2d[0],
            ty=self.pos_2d[2],
            rotation=0.0,
            zoom=self.zoom_2d
        )

    def _handle_input(self, dt: float):
        """处理输入"""
        if rl.IsKeyPressed(rl.KEY_V):
            self.toggle_mode()
            return

        if self.mode == "3d":
            self._handle_input_3d(dt)
        else:
            self._handle_input_2d(dt)

    def _handle_input_3d(self, dt: float):
        """3D 模式输入"""
        pan_speed = config.CAMERA_MOVE_SPEED * dt
        # 前方向 = 从相机指向目标（取反）
        fx = -math.sin(self.azimuth)
        fz = -math.cos(self.azimuth)

        if rl.IsKeyDown(rl.KEY_W) or rl.IsKeyDown(rl.KEY_UP):
            self.target = (self.target[0] + fx * pan_speed,
                           self.target[1],
                           self.target[2] + fz * pan_speed)
        if rl.IsKeyDown(rl.KEY_S) or rl.IsKeyDown(rl.KEY_DOWN):
            self.target = (self.target[0] - fx * pan_speed,
                           self.target[1],
                           self.target[2] - fz * pan_speed)
        if rl.IsKeyDown(rl.KEY_A) or rl.IsKeyDown(rl.KEY_LEFT):
            self.target = (self.target[0] + fz * pan_speed,
                           self.target[1],
                           self.target[2] - fx * pan_speed)
        if rl.IsKeyDown(rl.KEY_D) or rl.IsKeyDown(rl.KEY_RIGHT):
            self.target = (self.target[0] - fz * pan_speed,
                           self.target[1],
                           self.target[2] + fx * pan_speed)

        # 无限地形：不再限制相机移动范围
        # （移除旧的 margin 限制）

        scroll = rl.GetMouseWheelMove()
        if scroll != 0:
            self.distance_target += scroll * config.CAMERA_ZOOM_SPEED * 100
            self.distance_target = max(config.CAMERA_DISTANCE_MIN,
                                        min(config.CAMERA_DISTANCE_MAX, self.distance_target))

        if rl.IsMouseButtonDown(rl.MOUSE_BUTTON_RIGHT):
            if not self._rotating:
                self._rotating = True
                self._last_mouse_x = rl.GetMouseX()
                self._last_mouse_y = rl.GetMouseY()

        if self._rotating:
            dx = rl.GetMouseX() - self._last_mouse_x
            dy = rl.GetMouseY() - self._last_mouse_y
            self.azimuth_target -= dx * 0.01 * config.CAMERA_ROT_SPEED
            self.altitude_target += dy * 0.005 * config.CAMERA_ROT_SPEED
            self._last_mouse_x = rl.GetMouseX()
            self._last_mouse_y = rl.GetMouseY()

        if rl.IsMouseButtonReleased(rl.MOUSE_BUTTON_RIGHT):
            self._rotating = False

        if rl.IsKeyDown(rl.KEY_Q):
            self.azimuth_target += config.CAMERA_ROT_SPEED * dt
        if rl.IsKeyDown(rl.KEY_E):
            self.azimuth_target -= config.CAMERA_ROT_SPEED * dt

    def _handle_input_2d(self, dt: float):
        """2D 模式输入"""
        pan_speed = config.CAMERA_MOVE_SPEED * dt / self.zoom_2d
        if rl.IsKeyDown(rl.KEY_W) or rl.IsKeyDown(rl.KEY_UP):
            self.pos_2d = (self.pos_2d[0], self.pos_2d[1], self.pos_2d[2] - pan_speed)
        if rl.IsKeyDown(rl.KEY_S) or rl.IsKeyDown(rl.KEY_DOWN):
            self.pos_2d = (self.pos_2d[0], self.pos_2d[1], self.pos_2d[2] + pan_speed)
        if rl.IsKeyDown(rl.KEY_A) or rl.IsKeyDown(rl.KEY_LEFT):
            self.pos_2d = (self.pos_2d[0] + pan_speed, self.pos_2d[1], self.pos_2d[2])
        if rl.IsKeyDown(rl.KEY_D) or rl.IsKeyDown(rl.KEY_RIGHT):
            self.pos_2d = (self.pos_2d[0] - pan_speed, self.pos_2d[1], self.pos_2d[2])

        # 无限地形：不再限制 2D 相机移动范围

        scroll = rl.GetMouseWheelMove()
        if scroll != 0:
            self.zoom_2d_target *= (1.0 - scroll * config.CAMERA_ZOOM_SPEED * 5)
            self.zoom_2d_target = max(config.CAMERA_2D_ZOOM_MIN,
                                        min(config.CAMERA_2D_ZOOM_MAX, self.zoom_2d_target))

    def toggle_mode(self):
        """切换 2D/3D 模式"""
        if self.mode == "3d":
            self.mode = "2d"
            self.pos_2d = (self.target[0], 0, self.target[2])
        else:
            self.mode = "3d"
            self.target = (self.pos_2d[0], self.target[1], self.pos_2d[2])

    def is_3d(self) -> bool:
        return self.mode == "3d"

    def is_2d(self) -> bool:
        return self.mode == "2d"

    def screen_to_world(self, screen_x: float, screen_y: float) -> tuple:
        """屏幕坐标转世界坐标 — 用射线步进搜索地形表面"""
        if self.is_3d():
            cam = self.get_camera3d()
            ray = rl.GetScreenToWorldRay(vec2(screen_x, screen_y), cam)
            ox, oy, oz = ray.position.x, ray.position.y, ray.position.z
            dx, dy, dz = ray.direction.x, ray.direction.y, ray.direction.z

            # 射线步进：找到射线从地形上方穿入地形的点
            # 相机在地形上方，射线向下穿过地形表面
            step_size = 0.8
            max_steps = 150
            for i in range(1, max_steps + 1):
                t = i * step_size
                rx = ox + dx * t
                ry = oy + dy * t
                rz = oz + dz * t

                xi, zi = int(rx), int(rz)
                # 无限地形：不再检查世界边界
                terrain_top = self.universe.get_surface_height_smooth_safe(rx, rz) + 1.0
                if ry < terrain_top:
                    # 射线进入地形，返回交点
                    return rx, terrain_top, rz

            # 回退：用射线与 Y=target_y 平面求交
            ground_y = self.target[1]
            if abs(dy) < 1e-6:
                return self.target[0], ground_y, self.target[2]
            t = (ground_y - oy) / dy
            x = ox + dx * t
            z = oz + dz * t
            return x, ground_y, z
        else:
            screen_w = rl.GetScreenWidth()
            screen_h = rl.GetScreenHeight()
            scale = self.zoom_2d
            world_x = self.pos_2d[0] + (screen_x - screen_w / 2) / scale
            world_z = self.pos_2d[2] + (screen_y - screen_h / 2) / scale
            return world_x, 0, world_z
