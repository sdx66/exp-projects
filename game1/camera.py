"""
camera.py - 相机控制
支持 3D 视角相机（右键以相机自身为轴旋转）和 2D 正交相机，V 键切换。
raylib 6.x: 结构体通过 tuple 传入
"""

import math
import raylib as rl
import config
from rl_utils import vec2, vec3, camera2d, camera3d


class Camera:
    """
    游戏相机，支持 2D/3D 模式切换。
    3D 模式：视角相机（azimuth, altitude, distance）。右键拖动 / Q·E
             以相机自身为轴旋转——眼位保持不动、视线方向绕相机旋转，
             target 随视线方向被重新放置；WASD 前后左右平移、Z/C 上下
             平移、滚轮沿视线方向推拉距离。
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
        self.distance = config.CAMERA_DISTANCE_DEFAULT
        self.altitude = config.CAMERA_ALTITUDE_DEFAULT   # ~29° 侧面视角，地形更立体
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

        # 3D 相机地形避让缓存（Minecraft 式实体推挤后的眼位点）。
        # 缓存键含 terrain 版本（见 get_camera3d）：地形变更后即使相机静止
        # 也会强制重算，防止陈旧钳制把眼位封进新放置的实心格（穿模）。
        self._eye_clamp_key = None
        self._eye_clamp_pos = None
        # 上一帧"公式眼位"（未求解的 target+offset 点）。_slide_stop 只在
        # 连续平移帧可用上一帧眼位作起点；读档首帧/测试直设姿态/切模式等
        # 状态跳变时公式眼位骤变，距离超过 2 格或缓存链被重置即不信任，
        # 退回"最近空气格"回收（见 get_camera3d 的 trust 判定）。
        self._prev_formula_eye = None

        # 下潜状态：按住 C 键时为真（下一帧 get_camera3d 进入实体推挤分支）。
        self._diving_input = False

        # 开局眼位下限：一次性的视线走廊扫描（防开局相机埋进山体/被山脊挡视线）。
        # 玩家一动（眼位偏离初始位置）即失效，回到交互式足迹 clamp；
        # 出生点地形被挖掘/放置修改（world_version bump）后同样失效（见
        # get_camera3d 的版本门控），避免在出生点挖坑后眼位被抬在原地面之上。
        self._startup_eye_floor = None
        self._startup_eye_xz = None
        self._startup_world_version = None
        if universe is not None:
            self._compute_startup_eye_floor()

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

    def _move_target_y(self, dy: float):
        """垂直平移相机焦点（Z=上浮 / C=下潜 共用，3D 模式）。

        与 WASD 移 XZ 同一套"平移"语义：焦点 Y 自由升降，仅受绝对下限
        CAMERA_TARGET_Y_MIN 与上限 CAMERA_TARGET_Y_MAX 约束——不按所在列
        地表钳制（否则在高地上按 C 会把焦点顶到地表之上，下潜被高度卡死）。
        眼位随焦点自动跟随；"不穿山体"由 _solve_eye 负责（俯视保留地形
        避让、下潜放开但保海平面下限）。焦点钻入地表只会让视线朝下，眼位
        本身不会嵌进实心地形。
        """
        self.target = (self.target[0],
                       max(config.CAMERA_TARGET_Y_MIN,
                           min(config.CAMERA_TARGET_Y_MAX,
                               self.target[1] + dy)),
                       self.target[2])

    def _invalidate_startup_floor(self):
        """显式上下平移（Z/C）后，开局防埋山下限不再适用。

        开局眼位下限只在"玩家还没动相机"时生效；按 Z/C 主动上浮/下潜是
        明确的相机操作，继续保留它会把出生点附近的眼位抬回平台之上（按 C
        无反应）。玩家一动即永久失效（与 WASD 平移同理）。
        """
        self._startup_eye_floor = None
        self._startup_eye_xz = None

    def _compute_startup_eye_floor(self):
        """开局眼位下限：足迹 clamp + 眼→基地视线走廊扫描（一次性）。

        在 __init__ 时调用（distance/altitude/azimuth 均等于 *_target，
        无平滑偏移），用与 get_camera3d 相同的表达式求初始眼位，再沿
        眼→target 连线每 0.5 格采样地形顶：任何采样点要求连线高度高于
        地形（含 clearance），把需要的眼位抬升按比例折算回起点。
        仅对"未移动"的相机生效（get_camera3d 位置门控），玩家一动即失效。
        """
        if self.universe is None:
            return
        self._startup_world_version = self.universe.world_version
        ty = self.target[1]
        ex = self.target[0] + self.distance * math.cos(self.altitude) * math.sin(self.azimuth)
        ez = self.target[2] + self.distance * math.cos(self.altitude) * math.cos(self.azimuth)
        ey0 = self.target[1] + self.distance * math.sin(self.altitude)

        # 1) 眼位足迹 clamp（与 _solve_eye 的地形避让口径一致）
        fp_top = self.universe.get_surface_top_over_footprint_pure(ex, ez, 1.0)
        floor = max(ey0, fp_top + 1.0 + config.CAMERA_TERRAIN_CLEARANCE)

        # 2) 视线走廊扫描（t ∈ (0, 0.9)；基地半径 6 内已填平，跳过近端避免 (1-t)→0）
        tx, tz = self.target[0], self.target[2]
        step = 0.5
        span = math.hypot(tx - ex, tz - ez)
        if span > 1e-6:
            steps = max(2, int(span / step))
            for i in range(1, steps):
                t = i / steps
                if t >= 0.9:
                    break
                px = ex + (tx - ex) * t
                pz = ez + (tz - ez) * t
                th = self.universe.get_surface_top_over_footprint_pure(px, pz, 0.5)
                req = ty + (th + 1.0 + config.CAMERA_TERRAIN_CLEARANCE - ty) / (1.0 - t)
                if req > floor:
                    floor = req

        # 3) 封顶：相对基地地表不超上限（防御极端地形）
        cap = ty + config.CAMERA_SPAWN_RAISE_MAX
        if floor > cap:
            floor = cap
        self._startup_eye_floor = floor
        self._startup_eye_xz = (ex, ez)

    def get_camera3d(self):
        """获取 Camera3D tuple。

        眼位由 _solve_eye 约束（Minecraft 式）：
        俯视/平视（非下潜）：不低于目标到眼位连线下的地形（防平移插进山体）；
        下潜（按住 C 或仰角<0）：沿视线做实体推挤 —— 穿过水/空气、实心块
        沿面滑行、从不出现在实心方块内部，最低到 CAMERA_EYE_MIN_Y（海平面上方）。
        """
        x = self.target[0] + self.distance * math.cos(self.altitude) * math.sin(self.azimuth)
        y = self.target[1] + self.distance * math.sin(self.altitude)
        z = self.target[2] + self.distance * math.cos(self.altitude) * math.cos(self.azimuth)

        diving = self._diving_input or y < self.target[1]
        # 缓存键附加地形版本（world_version）：挖掘/放置会话获批后统一 bump，
        # 即使眼位/目标/下潜状态全都不变也会失效重算——否则静止相机沿用陈旧
        # 钳制：脚下被挖低只是悬空不跟地，但放置方块封住眼位格时会把眼位留在
        # 实心块内（穿模）。universe 为 None（纯构造调试）时用 None 常量键。
        terrain = self.universe.world_version if self.universe is not None else None
        # 上一帧的已求解眼位（顶盖压制用）：必须在本帧赋值 _eye_clamp_pos 之前取，
        # 否则被新解覆盖后就拿不到"上帧被顶盖压住"的证据了。
        prev_clamp = self._eye_clamp_pos
        key = (round(x, 2), round(y, 2), round(z, 2),
               round(self.target[0], 2), round(self.target[2], 2), diving, terrain)
        if key != self._eye_clamp_key:
            had_prev_key = self._eye_clamp_key is not None
            prev_formula = self._prev_formula_eye
            self._prev_formula_eye = (x, y, z)
            self._eye_clamp_key = key
            # _slide_stop 需要"上一帧眼位"作连续平移起点——只有连续帧才可信：
            # 上一帧公式眼位存在、且与当前公式眼位距离 ≤ 2 格、且缓存链未被
            # 重置（读档首帧 _eye_clamp_key=None / 测试直设姿态 / 切模式瞬移
            # 均不满足）。不信任时 prev=None，由 _solve_eye 落回最近空气回收。
            trust_prev = (had_prev_key and prev_formula is not None
                          and prev_clamp is not None
                          and math.hypot(prev_formula[0] - x,
                                         prev_formula[1] - y,
                                         prev_formula[2] - z) <= 2.0)
            self._eye_clamp_pos = self._solve_eye(
                x, y, z, diving, prev_clamp if trust_prev else None)
        px, py, pz = self._eye_clamp_pos

        # 顶盖压制 & 洞内锁（下潜/非下潜统一）
        # ① 顶盖压制：上一帧眼位被实心顶盖压住（洞顶/檐下），本帧不许越过
        #    顶盖竖直升起（否则 target 升过洞顶/山顶时公式眼位跳进顶盖上方
        #    的空气，整帧穿出到山外面）——钉在上一帧高度。
        # ② 洞内锁：上一帧相机在实心顶盖之下（洞里/檐下），本帧求解位置
        #    若沿直线穿墙/穿山离开（滚轮拉远、公式眼位越出洞壁），被洞壁
        #    挡住并保持原位；路径畅通（洞内移动、走向洞口、开阔地拉远/飞高）
        #    则自由放行——不会"弹射出山"，也不挡正常移动。
        if (prev_clamp is not None
                and not self._eye_cell_solid(prev_clamp[0], prev_clamp[1],
                                             prev_clamp[2])
                and self._eye_cell_solid(prev_clamp[0], prev_clamp[1] + 1.0,
                                         prev_clamp[2])):
            if py > prev_clamp[1]:
                py = prev_clamp[1]
                self._eye_clamp_pos = (px, py, pz)
            if (math.hypot(px - prev_clamp[0], py - prev_clamp[1],
                           pz - prev_clamp[2]) > 2.0
                    and self._path_crosses_solid(prev_clamp, (px, py, pz))):
                px, py, pz = prev_clamp
                self._eye_clamp_pos = prev_clamp

        # 开局眼位下限：眼位尚未偏离开局位置时，把眼位平滑抬到开局扫描
        # 高度（保证开局视野在山体之上且看得到基地）。随眼位偏离线性淡出
        # （CAMERA_SPAWN_KEEP→CAMERA_SPAWN_RELEASE），避免切换时竖直跳变。
        # 玩家按 Z/C 显式上浮/下潜后永久失效（见 _invalidate_startup_floor）；
        # 出生点地形被挖掘/放置修改后同样失效——扫描是一次性的出生地形
        # 结果，地形变了仍生效会把眼位抬在原地面之上（出生点挖坑后坑里
        # 移动眼位被瞬间拉出坑）。
        if self._startup_eye_floor is not None:
            if (self.universe is not None
                    and self.universe.world_version != self._startup_world_version):
                self._invalidate_startup_floor()
            else:
                d = math.hypot(px - self._startup_eye_xz[0],
                               pz - self._startup_eye_xz[1])
                keep = config.CAMERA_SPAWN_KEEP
                release = config.CAMERA_SPAWN_RELEASE
                if d < keep:
                    factor = 1.0
                elif d < release:
                    factor = (release - d) / (release - keep)
                else:
                    factor = 0.0
                if factor > 0.0:
                    blended = py + (self._startup_eye_floor - py) * factor
                    py = max(py, blended)

        return camera3d(
            px=px, py=py, pz=pz,
            tx=self.target[0], ty=self.target[1], tz=self.target[2],
            ux=0, uy=1, uz=0,
            fovy=60.0, proj=0  # CAMERA_PERSPECTIVE
        )

    def _nearest_air(self, x, y, z, radius=5):
        """以眼位为中心的 3D 邻域内找最近的空气格中心（欧氏距离优先）。

        眼位嵌入实心时（撞洞壁/洞底/洞顶）就近退回空气——而不是沿某条
        射线或抬到列顶（后者会从洞里穿出山顶）：撞侧壁→横向回洞、
        撞洞底→向上回洞、撞洞顶→向下回洞。返回空气格中心
        (fx+0.5, fy+0.5, fz+0.5)，radius 内全实心返回 None，由调用方
        走"抬升列顶"兜底（防卡死）。
        """
        fx, fy, fz = math.floor(x), math.floor(y), math.floor(z)
        best = None
        best_d2 = None
        for dy in range(-radius, radius + 1):
            for dx in range(-radius, radius + 1):
                for dz in range(-radius, radius + 1):
                    if dx == 0 and dy == 0 and dz == 0:
                        continue
                    if self._eye_cell_solid(fx + dx, fy + dy, fz + dz):
                        continue
                    d2 = dx * dx + dy * dy + dz * dz
                    if best_d2 is None or d2 < best_d2:
                        best_d2 = d2
                        best = (fx + dx + 0.5, fy + dy + 0.5, fz + dz + 0.5)
        return best

    def _nearest_air_column(self, x, y, z, cap=256):
        """沿眼位所在列向上/向下找最近的空气格中心（兜底回退）。

        洞内按住 C 下潜时，公式眼位会沉到洞底之下；一旦离洞超过
        `_nearest_air` 的半径（5 格），水平气泡找不到空气——此时不能抬到
        列顶（洞穴列的列顶=山顶，等于穿出洞顶），而是沿所在列找最近的
        空气：洞就在正上/正下方时回到洞里，真被埋死才轮到抬列顶兜底。
        返回空气格中心或 None（cap 内全实心）。
        """
        fx, fy, fz = math.floor(x), math.floor(y), math.floor(z)
        for d in range(1, cap + 1):
            if not self._eye_cell_solid(fx, fy + d, fz):
                return fx + 0.5, fy + d + 0.5, fz + 0.5
            if not self._eye_cell_solid(fx, fy - d, fz):
                return fx + 0.5, fy - d + 0.5, fz + 0.5
        return None

    def _path_crosses_solid(self, a, b, max_len=32.0, step=0.6):
        """a→b 直线路径上是否有实心格（用于"洞内锁"判定穿墙/穿山）。

        只采样 a→b 之间的点（不含终点 b）；b 距 a 超过 max_len 时按
        max_len 截断（超长跃迁只看近端路径——近端就被洞壁挡住即可判穿墙）。
        """
        dx = b[0] - a[0]
        dy = b[1] - a[1]
        dz = b[2] - a[2]
        length = math.hypot(dx, dy, dz)
        if length <= 1e-9:
            return False
        n = int(min(length, max_len) / step)
        for i in range(1, n + 1):
            t = i * step / length   # 归一化（截断后 t<1，但只判近端实心即可）
            px = a[0] + dx * t
            py = a[1] + dy * t
            pz = a[2] + dz * t
            if self._eye_cell_solid(px, py, pz):
                return True
        return False

    def _floor_below_eye(self, x, y, z):
        """眼位所在列、位于眼位下方的最近实心支撑格 y（`< floor(y)`）。

        区分"洞穴/隧道"与"地表"：洞穴里眼位下方最近实心是洞底，地表上
        是地面列顶——两者对"眼位不低于支撑+1.2"都成立，不会按整列最高点
        （洞顶上方山顶）把洞里的眼位抬出洞顶。已加载区块逐格查询；
        未加载或 16 格内无实心退回 surface_height_at（纯噪声+覆盖层列顶，
        与旧行为一致）。
        """
        u = self.universe
        fx, fy, fz = math.floor(x), math.floor(y), math.floor(z)
        cs = config.CHUNK_SIZE
        if u.chunks.get((fx // cs, fz // cs)) is not None:
            lo = max(fy - 16, 2)
            for yy in range(fy - 1, lo - 1, -1):
                if u.ray_block_solid(fx, yy, fz) is True:
                    return yy
        return u.surface_height_at(fx, fz)

    def _solve_eye(self, x, y, z, diving, prev=None):
        """眼位求解（Minecraft 式）。

        非下潜：原地形避让——眼位不低于"眼位下方最近实心支撑" + 1.0 +
        clearance（眼位所在列/洞穴洞底/贴坡同一语义），防止平移把相机插进
        山体，同时洞穴里不会按整列最高点（洞顶上方山顶）被抬出洞顶。
        眼位嵌入实心时（撞洞壁/洞底/洞顶）：连续平移撞墙（上一帧眼位 prev
        非实心）先走 _slide_stop 沿移动方向停在实心面边界——连续、无格心
        量化，按住方向键贴墙时眼位稳定钉在墙面（不振荡、不反向）；prev
        缺失/不可用（读档/切模式/测试直设姿态等状态跳变）才就近退回最近
        空气格（撞侧壁→横向回洞、撞底→向上回洞、撞顶→向下回洞），退无可
        退（周围全是实心）才抬升到列顶之上（防卡死）。

        下潜（按住 C 或仰角<0）：实体推挤——眼位穿过水/空气自由下沉，
        落到实心方块时沿"眼→目标"射线被推到最近的非实心位置（贴面滑行），
        硬下限 CAMERA_EYE_MIN_Y（海平面上方），避免穿进海底/基岩。
        用纯噪声查询（get_surface_top_over_footprint_pure）而非默认 5 的
        安全查询：开局时眼位所处区块常未生成，默认 5 会把避让高度算成
        平地 → 相机埋进山体。纯查询不加载区块、始终精确。
        """
        if self.universe is None:
            return x, y, z
        if not diving:
            if self._eye_cell_solid(x, y, z):
                # 连续平移撞墙优先：从上一帧眼位沿本帧位移方向滑停到实心面
                # 边界（_slide_stop 连续二分，无格心量化）。prev 非空且非实心
                # 时总有效——贴着墙面时返回 prev 本身，眼位钉在面上不振荡；
                # prev 缺失/实心（状态跳变）才落回"最近空气格"回收。
                if prev is not None:
                    r = self._slide_stop(prev, (x, y, z))
                    if r is not None:
                        return r
                # 眼位已嵌入实心且无可靠上一帧：就近退回空气（洞壁→回洞）；
                # 气泡搜不到再沿所在列找（洞底之下→回洞），都失败才抬升列顶
                # 兜底（防卡死）。"顶盖压制/洞内锁"由 get_camera3d 统一处理。
                r = self._nearest_air(x, y, z)
                if r is not None:
                    return r
                r = self._nearest_air_column(x, y, z)
                if r is not None:
                    return r
                fp = self.universe.get_surface_top_over_footprint_pure(x, z, 0.0)
                return x, max(y, fp + 1.0 + config.CAMERA_TERRAIN_CLEARANCE), z
            floor = self._floor_below_eye(x, y, z)
            return x, max(y, floor + 1.0 + config.CAMERA_TERRAIN_CLEARANCE), z
        px, py, pz = self._clip_eye(x, y, z)
        # 顶盖压制已统一到 get_camera3d（下潜/非下潜一致），这里不再重复。
        return px, max(py, config.CAMERA_EYE_MIN_Y), pz

    def _slide_stop(self, a, b):
        """从上一帧非实心眼位 a 沿直线滑向新公式眼位 b，停在首个实心面。

        连续平移撞墙的标准解：与 _clip_eye 同构的二分逼近，取最后一个非实心
        点（浮点连续，不落格心）。贴着墙面时（lo_t→0）返回 a 本身——眼位钉
        在面上，按住方向键既不穿透也不倒退/振荡。b 非实心 → 原样返回 b
        （正常平移恒等）；a 实心、ab 无空隙、或 a→b 是长距离跳变（读档/切
        模式/直设姿态，> 2 格）→ 返回 None，由调用方落回"最近空气格"回收。
        """
        if (self._eye_cell_solid(a[0], a[1], a[2])
                or math.hypot(b[0] - a[0], b[1] - a[1], b[2] - a[2]) > 2.0):
            return None
        if not self._eye_cell_solid(b[0], b[1], b[2]):
            return b
        lo_t, hi_t = 0.0, 1.0
        for _ in range(12):
            mid = (lo_t + hi_t) * 0.5
            px = a[0] + (b[0] - a[0]) * mid
            py = a[1] + (b[1] - a[1]) * mid
            pz = a[2] + (b[2] - a[2]) * mid
            if self._eye_cell_solid(px, py, pz):
                hi_t = mid
            else:
                lo_t = mid
        return (a[0] + (b[0] - a[0]) * lo_t,
                a[1] + (b[1] - a[1]) * lo_t,
                a[2] + (b[2] - a[2]) * lo_t)

    def _clip_eye(self, x, y, z):
        """Minecraft 式实体推挤：把眼位沿"眼→目标"射线推到最近的非实心格。

        眼位本就在非实心空间（空气/水/岩浆）→ 原样返回（自由穿越及下沉）；
        眼位落在实心块内 → 二分逼近射线与实心的入口面，取最后一个非实心点
        作为眼位（相机贴地表/墙沿滑行，从不出现在方块内部）。
        退化：射线全程实心且极近处仍实心（眼位被完全困住）→ 就近退回最近
        空气格（撞侧壁→横向回洞、撞底→向上回洞、撞顶→向下回洞），
        退无可退才抬升到列顶之上 + clearance（防卡死）。
        """
        if self.universe is None:
            return x, y, z
        if not self._eye_cell_solid(x, y, z):
            return x, y, z
        tx, tyy, tz = self.target
        lo_t, hi_t = 0.0, 1.0
        for _ in range(16):
            mid = (lo_t + hi_t) * 0.5
            px = x + (tx - x) * mid
            py = y + (tyy - y) * mid
            pz = z + (tz - z) * mid
            if self._eye_cell_solid(px, py, pz):
                hi_t = mid
            else:
                lo_t = mid
        if lo_t <= 0.02:
            # 眼位与 target 之间全实心（例如撞进洞壁且 target 也扎进墙里，
            # 或顶着洞顶按 Z 上移）：就近退回空气格（撞侧壁→横向回洞、
            # 撞底→向上回洞、撞顶→向下回洞），气泡搜不到再沿所在列找
            # （洞里下潜压过洞底→回到洞内），都失败才抬升列顶（防卡死）。
            r = self._nearest_air(x, y, z)
            if r is not None:
                return r
            r = self._nearest_air_column(x, y, z)
            if r is not None:
                return r
            # 不在此处用上一帧眼位兜底（缓存可能因状态跳变而过期，见
            # _solve_eye 的注释）；"顶盖压制/洞内锁"由 get_camera3d 统一处理。
            fp = self.universe.get_surface_top_over_footprint_pure(x, z, 0.0)
            return x, max(y, fp + 1.0 + config.CAMERA_TERRAIN_CLEARANCE), z
        return (x + (tx - x) * lo_t,
                y + (tyy - y) * lo_t,
                z + (tz - z) * lo_t)

    def _eye_cell_solid(self, x, y, z):
        """眼位所在格是否实心（只查眼位自己所在的格）。

        用 ray_block_solid（已加载区块查真实方块，未加载返回 None → 非实心），
        与射线拾取同一套实心语义（空气/水/岩浆不实心）——水可穿，海底/山体
        不可穿。眼位是点：自己所在格非实心即不穿模；四邻格实心（例如坑里
        贴着坑壁、隧道里贴着洞壁）是合法姿态——若把邻格也算实心，下潜时
        靠近任何一面墙眼位都会被沿视线推走（坑里移动被瞬间推出坑外）。
        """
        u = self.universe
        if u is None:
            return False
        return u.ray_block_solid(math.floor(x), math.floor(y), math.floor(z)) is True

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

    def _apply_zoom_step(self, scroll: float):
        """滚轮缩放一步（指数比例），只改距离、不改仰角/方位角。

        供 _handle_input_3d 与诊断脚本（check_camera_depth._dolly）共用，
        避免测试复制公式而与实现漂移。
        """
        self.distance_target *= math.exp(scroll * config.CAMERA_ZOOM_SPEED * 100)
        self.distance_target = max(config.CAMERA_DISTANCE_MIN,
                                   min(config.CAMERA_DISTANCE_MAX, self.distance_target))

    def _handle_input_3d(self, dt: float):
        """3D 模式输入"""
        pan_speed = config.CAMERA_MOVE_SPEED * dt
        # 前方向 = 从相机指向目标（取反）
        fx = -math.sin(self.azimuth)
        fz = -math.cos(self.azimuth)
        t_before = self.target

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

        # Z/C：相机焦点垂直平移（3D 模式，2D 俯视图无高度概念不参与）。
        # 与 WASD 移 XZ 同一套"平移"语义，构成 Minecraft 式三维飞行——
        # 水平走到海面上方后按 C 沉到海面/海底，按 Z 上浮。
        # 焦点 Y 自由升降（只受绝对下限约束）；开局防埋山下限在显式
        # 上浮/下潜后失效，避免出生点附近按 C 被抬回平台之上。
        # 按住 C 同时进入"下潜"眼位分支（实体推挤，见 get_camera3d/_solve_eye）。
        self._diving_input = rl.IsKeyDown(rl.KEY_C)
        if rl.IsKeyDown(rl.KEY_Z) or rl.IsKeyDown(rl.KEY_C):
            self._invalidate_startup_floor()
        if rl.IsKeyDown(rl.KEY_Z):
            self._move_target_y(pan_speed)
        if rl.IsKeyDown(rl.KEY_C):
            self._move_target_y(-pan_speed)

        # 无限地形：不再限制相机移动范围
        # （移除旧的 margin 限制）

        scroll = rl.GetMouseWheelMove()
        if scroll != 0:
            # 指数（比例）缩放：距离越大单格增量越大，可覆盖 5→2100 的 400 倍范围
            # （原线性 0.2/格需上万格才能拉到顶，范围拉大后不可用）。
            # 满范围约 ln(2100/5)/(CAMERA_ZOOM_SPEED*100) ≈ 30 格滚轮。
            # 纯缩放：只改距离、不改仰角/方位角——下潜与不下潜共用同一套
            # 行为，缩放绝不使视线方向旋转；进入/退出下潜用右键拖仰角。
            self._apply_zoom_step(scroll)

        if rl.IsMouseButtonDown(rl.MOUSE_BUTTON_RIGHT):
            if not self._rotating:
                self._rotating = True

        if self._rotating:
            # 用未钳制的鼠标增量（GetMouseDelta）而非绝对坐标差：光标顶到
            # 窗口边缘时旋转不中断、不从陈旧位置回弹；否则"拖到边上一帧停、
            # 回来一帧猛甩"会被误认为角度限制/跳变。
            m = rl.GetMouseDelta()
            self._rotate_in_place(m.x * 0.01 * config.CAMERA_ROT_SPEED,
                                  m.y * 0.005 * config.CAMERA_ROT_SPEED)

        if rl.IsMouseButtonReleased(rl.MOUSE_BUTTON_RIGHT):
            self._rotating = False

        # Q/E：与右键同一套"以相机为轴"旋转（Q 视线右转 / E 视线左转）
        if rl.IsKeyDown(rl.KEY_Q):
            self._rotate_in_place(config.CAMERA_ROT_SPEED * dt, 0.0)
        if rl.IsKeyDown(rl.KEY_E):
            self._rotate_in_place(-config.CAMERA_ROT_SPEED * dt, 0.0)

        # 平移后视口校正：眼位被地形钳制时 target 会被拉回，保证镜头只平动
        # 不转动（见 _reconcile_target_to_eye 的说明，BUG-105）。
        # 只在"本帧 target 确实被按键移动"时校正——空闲帧（无平移输入）
        # 不跑，避免下潜推挤/地形编辑等微小眼位偏差被反复写进 target 造
        # 成相机缓慢漂移（check_camera_depth 下潜拉远到底 600 帧回归）。
        if self.target != t_before:
            self._reconcile_target_to_eye()

    def _reconcile_target_to_eye(self):
        """平移后把 target 拉回，使"公式眼位 == 渲染眼位"（XZ 水平分量）。

        平移语义是"移动 target（焦点）"，眼位 = target + 视线偏移随后由
        get_camera3d 求解，并受地形钳制：眼位嵌入实心时被 _solve_eye 回弹到
        最近空气格、洞穴里被"洞内锁"钉住上一帧位置、顶盖压制时不许抬升。
        这些钳制只约束**眼位**、不约束 target——若 target 照常前进而眼位
        钉在原地，视线方向（target−眼位）会被拧转，画面上世界就朝按键的
        反方向滑动（按上往下、按左往右，即"方向键偶尔反了"；在隧道/洞穴/
        贴墙时出现，开阔地正常）。

        本方法沿用 _rotate_in_place 的同一个恒等式：眼位 = target + offset
        ⇔ target = 眼位 − offset。平移后重解一次眼位，并在 renderer 调用
        get_camera3d 之前把 target 的水平分量对齐到"渲染眼位 − offset"，使
        公式眼位与渲染眼位重合：此后每次 get_camera3d 的求解都退化为恒等
        （缓存键命中/求解无位移），视线方向保持恒定——相机会被地形"挡住"
        停在墙边/洞壁，而不再拧转视口造成反向平移观感。
        - 只校正 XZ：target.y 不受影响（Y 由 Z/C 显式控制；地形避让/开局
          下限只抬升眼位 Y，不动 XZ，属既有行为，不在此处对齐）。
        - 开阔地渲染眼位 XZ == 公式眼位 XZ，本方法为恒等空操作，不改变
          任何正常平移手感；2D 模式无眼位钳制、不参与。
        """
        cos_a = math.cos(self.altitude)
        ox = self.distance * cos_a * math.sin(self.azimuth)
        oz = self.distance * cos_a * math.cos(self.azimuth)
        fx = self.target[0] + ox
        fz = self.target[2] + oz
        ex, _ey, ez = self.get_camera3d()[0]
        if abs(fx - ex) > 1e-4 or abs(fz - ez) > 1e-4:
            self.target = (ex - ox, self.target[1], ez - oz)

    def _rotate_in_place(self, d_azimuth: float, d_altitude: float):
        """以相机自身为轴旋转（右键拖动 / Q·E）：眼位固定，仅改变视线方向。

        与轨道旋转（眼位绕 target 公转）不同：旋转基准取"实际渲染眼位"
        （get_camera3d 的最终结果，含地形避让/下潜推挤/开局下限），再把
        target 沿新视线方向重新放置，使公式眼位 == 渲染眼位，因此地形/下潜
        求解器在旋转期间恒为恒等——无论下潜、贴山、开局抬升，渲染眼位都
        分毫不动，视角严格跟随拖动量（转角 == 拖动量，无范围回弹/跳变）。
        altitude 钳制在 [CAMERA_ALTITUDE_MIN, MAX]。
        """
        az = self.azimuth + d_azimuth
        alt = max(config.CAMERA_ALTITUDE_MIN,
                  min(config.CAMERA_ALTITUDE_MAX, self.altitude + d_altitude))
        d = self.distance

        # 实际渲染眼位（旋转期间保持不动）
        px, py, pz = self.get_camera3d()[0]

        # 新 target：眼位 + 新视线方向（-offset）的 distance 点。
        # 眼位 = target + d*offset，要让它不动 ⇒ target = 眼位 - d*offset_new。
        cos_a = math.cos(alt)
        self.target = vec3(
            px - d * cos_a * math.sin(az),
            py - d * math.sin(alt),
            pz - d * cos_a * math.cos(az),
        )
        self.azimuth = az
        self.altitude = alt
        self.azimuth_target = az  # 直接对齐，平滑在旋转时为恒等
        self.altitude_target = alt

    def _handle_input_2d(self, dt: float):
        """2D 模式输入"""
        pan_speed = config.CAMERA_MOVE_SPEED * dt / self.zoom_2d
        if rl.IsKeyDown(rl.KEY_W) or rl.IsKeyDown(rl.KEY_UP):
            self.pos_2d = (self.pos_2d[0], self.pos_2d[1], self.pos_2d[2] - pan_speed)
        if rl.IsKeyDown(rl.KEY_S) or rl.IsKeyDown(rl.KEY_DOWN):
            self.pos_2d = (self.pos_2d[0], self.pos_2d[1], self.pos_2d[2] + pan_speed)
        if rl.IsKeyDown(rl.KEY_A) or rl.IsKeyDown(rl.KEY_LEFT):
            self.pos_2d = (self.pos_2d[0] - pan_speed, self.pos_2d[1], self.pos_2d[2])
        if rl.IsKeyDown(rl.KEY_D) or rl.IsKeyDown(rl.KEY_RIGHT):
            self.pos_2d = (self.pos_2d[0] + pan_speed, self.pos_2d[1], self.pos_2d[2])

        # 无限地形：不再限制 2D 相机移动范围

        scroll = rl.GetMouseWheelMove()
        if scroll != 0:
            # 2D 缩放用与 3D 一致的指数形式（原线性 1-scroll*0.01 每格仅 ±1%，
            # 走完 0.3~3.0 需约 230 格，缩放形同失效，BUG-018）。
            self.zoom_2d_target *= math.exp(scroll * config.CAMERA_ZOOM_SPEED * 100)
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
        # 切换模式时复位输入闩锁：3D 按住右键 → V 切 2D → 松开右键 → V 切回 3D，
        # 若 _rotating 残留 True，相机将随裸鼠标位移持续旋转；_diving_input 同理，
        # 避免切回 3D 首帧误走潜水分支造成眼位跳变。
        self._rotating = False
        self._diving_input = False

    def is_3d(self) -> bool:
        return self.mode == "3d"

    def is_2d(self) -> bool:
        return self.mode == "2d"

    def _ray_terrain_crossing(self, ox, oy, oz, dx, dy, dz):
        """射线与地形的首个实体方块命中点 — 粗步进定位后二分求精。

        拾取必须跟随"指针射线最先遇到的地面"：世界存在拱洞/隧道——敌人
        开洞后实体方块悬在通道上方（dig_front_block"内部掏洞不改列顶"，
        mark_passage 只把通行列顶降到洞底），同一列有上下两个地面。只用
        列顶高度场判定会把射向洞内的射线在洞口沿提前判为入地，塔位偏前
        （屏幕上偏上）到洞口上方的崖顶，因此逐采样查真实方块实心。

        未加载区块（可见区块按帧预算补建期间）退化为列顶判定：
        surface_height_at 不依赖区块生成，既不会引入假平面，也不会飞过
        未建区命中更远地形。

        二分 10 次残差 < 0.001；取首个穿透侧 t_hi（而非中值）：洞口/断崖
        边界处落在先遇到的实体格，而不是回退到空气侧末格。

        返回 (t, x, y, z, cell)，y 为命中点坐标（非列顶）；cell 为首个实心
        体素 (bx, by, bz)，供挖掘/预览精确取格（不能用 floor(y) 反推：顶面
        或负方向侧面恰落在整数平面时 floor 会落到相邻空气格）。区块未加载
        走列顶兜底时 cell 为 None；射线未命中返回 None。
        """
        step = 0.8
        # 距离上限提到 2100 后，默认 150 步（120 格）不够；按当前距离缩放，
        # 默认距离 42 下仍为 150 步（保持原行为）。
        max_steps = max(150, int(self.distance / step) + 60)
        height_at = self.universe.surface_height_at
        solid_at = self.universe.ray_block_solid

        def probe(fx, fy, fz, ry):
            """返回 (是否命中, 实心体素)；未加载区块兜底命中时体素为 None。"""
            s = solid_at(fx, fy, fz)
            if s is None:
                # 区块未加载：列顶判定兜底（int 键，传 float 会 miss）
                return ry < height_at(fx, fz) + 1.0, None
            if s:
                return True, (fx, fy, fz)
            return False, None

        # 无限地形：不再检查世界边界
        ok, cell = probe(math.floor(ox), math.floor(oy), math.floor(oz), oy)
        if ok:
            # 起点已在实体内（异常视角）：直接返回起点
            return 0.0, ox, oy, oz, cell
        hit_cell = None
        t_lo, t_hi = 0.0, 0.0
        for i in range(1, max_steps + 1):
            t_hi = i * step
            rx = ox + dx * t_hi
            ry = oy + dy * t_hi
            rz = oz + dz * t_hi
            ok, cell = probe(math.floor(rx), math.floor(ry), math.floor(rz), ry)
            if ok:
                hit_cell = cell
                break
            t_lo = t_hi
        else:
            return None
        # 二分求精：t_lo 在空气侧、t_hi 在实体侧
        for _ in range(10):
            t_mid = (t_lo + t_hi) * 0.5
            ry = oy + dy * t_mid
            rx = ox + dx * t_mid
            rz = oz + dz * t_mid
            ok, cell = probe(math.floor(rx), math.floor(ry), math.floor(rz), ry)
            if ok:
                t_hi = t_mid
                hit_cell = cell
            else:
                t_lo = t_mid
        t = t_hi
        return t, ox + dx * t, oy + dy * t, oz + dz * t, hit_cell

    def screen_to_world(self, screen_x: float, screen_y: float) -> tuple:
        """屏幕坐标转世界坐标 — 用射线步进搜索地形表面"""
        if self.is_3d():
            cam = self.get_camera3d()
            ray = rl.GetScreenToWorldRay(vec2(screen_x, screen_y), cam)
            ox, oy, oz = ray.position.x, ray.position.y, ray.position.z
            dx, dy, dz = ray.direction.x, ray.direction.y, ray.direction.z

            hit = self._ray_terrain_crossing(ox, oy, oz, dx, dy, dz)
            if hit is not None:
                _, rx, ry, rz, _cell = hit
                return rx, ry, rz

            # 回退：射线在预算内未命中任何方块（朝天 / 雾外地平线附近的
            # 近水平射线）。原实现 dy≈0 时直接返回 target——轨道相机静止时
            # target 即大本营格，幽灵跳回基地误报"太靠近大本营"；dy>0 时
            # t 为负落到相机身后。改为：下视射线用前方平面交点，其余沿
            # 射线向前外推 2 倍相机距离，幽灵停在瞄准方向上。
            ground_y = self.target[1]
            if dy < -1e-6:
                t = (ground_y - oy) / dy
                if t > 0:
                    return ox + dx * t, ground_y, oz + dz * t
            t = 2.0 * self.distance
            return ox + dx * t, oy + dy * t, oz + dz * t
        else:
            screen_w = rl.GetScreenWidth()
            screen_h = rl.GetScreenHeight()
            scale = self.zoom_2d
            world_x = self.pos_2d[0] + (screen_x - screen_w / 2) / scale
            world_z = self.pos_2d[2] + (screen_y - screen_h / 2) / scale
            return world_x, 0, world_z

    def screen_to_block(self, screen_x: float, screen_y: float):
        """屏幕坐标 → 射线命中的首个实心体素 (bx, by, bz)。

        与 screen_to_world 共用同一条射线与首个命中语义，但保留实际采样到
        的方块坐标；玩家挖掘/挖掘预览用它做到"所见即所挖"。2D、射线未
        命中或仅靠未加载区块列顶兜底时返回 None。
        """
        if not self.is_3d():
            return None
        cam = self.get_camera3d()
        ray = rl.GetScreenToWorldRay(vec2(screen_x, screen_y), cam)
        hit = self._ray_terrain_crossing(
            ray.position.x, ray.position.y, ray.position.z,
            ray.direction.x, ray.direction.y, ray.direction.z)
        if hit is None:
            return None
        return hit[4]
