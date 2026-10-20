"""
main.py - 游戏入口

入口页（种子界面）为可见菜单：新游戏 / 载入存档 / 退出。
无参数时走种子输入；--load <path> 直接载入存档；传种子数字跳过种子界面。

命令行:
    python main.py            显示种子界面
    python main.py 12345      用种子 12345 直接开局
    python main.py --load f   直接载入存档 f
"""

import sys
import os

LIBS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "libs")
if os.path.isdir(LIBS_DIR):
    sys.path.insert(0, LIBS_DIR)

import raylib as rl
from rl_utils import *

import config
import save_system
from world import Universe, print_terrain_overview


# ─── 入口页（种子输入 + 可见菜单） ─────────────────────────

# raylib 6.x 绑定没有 IsMouseButtonDoublePressed，存档浏览器的双击自己判定。
DOUBLE_CLICK_INTERVAL = 0.35   # 秒：两次点击间隔小于该值视为双击
MAX_SEED_LEN = 64              # 种子文本长度上限
SEED_BOX_W = 320               # 种子输入框宽
SEED_BOX_H = 52                # 种子输入框高
SEED_FONT_SIZE = 22            # 种子文本字号
SEED_TEXT_PAD = 16             # 种子文本左右内边距
SAVE_ROW_H = 36                # 存档行高
SAVE_ROW_STEP = 42             # 存档行步距
SAVE_BASE_Y = 140              # 存档列表首行基准 Y
SAVE_ROW_W = 560               # 存档行宽


class SeedScreen:
    """入口页：种子输入框 + 三个可见菜单行（新游戏/载入存档/退出）。

    键盘: ↑↓ 选行, Enter 激活选中行, Esc 退出。
    种子框直接接受可打印字符（ASCII 32-126）；纯数字按整数种子，
    其余文本按 crc32 散列为整数种子。
    鼠标: 点击菜单行选中并激活; 存档列表单击选中、双击载入。
    """

    MENU_ITEMS = [
        ("新游戏", "Enter"),
        ("载入存档", "Enter"),
        ("退出", "Esc"),
    ]

    def __init__(self):
        self.input_text = ""
        self.cursor_visible = True
        self.cursor_timer = 0.0
        self.menu_index = 0
        self.browse_saves = False
        self.saved_items = []
        self.save_sel = 0
        self.save_scroll = 0             # 存档列表滚动偏移（行）
        self.confirm_delete_path = None  # 待二次确认删除的存档路径
        self._last_click_time = -1.0     # 上次点击时间（双击判定用）
        self._last_click_row = -1        # 上次点击所在行（双击判定用）
        self._last_visible = None        # 上次存档列表可见行数（窗口尺寸变化时重新夹取）
        self.loaded_game = None    # 从存档浏览器载入的 Game
        self.starting = False
        self.exit_requested = False

    # ── 布局 ──

    def _menu_row_rect(self, i):
        sw = rl.GetScreenWidth()
        row_w = 420
        row_h = 44
        row_x = (sw - row_w) // 2
        row_y = 430 + i * 54
        return (row_x, row_y, row_w, row_h)

    def _save_browser_metrics(self):
        """存档列表几何参数（按窗口高度自适应，保证不越界、不与提示行重叠）。

        返回 (base_y, row_h, row_step, hint_y, visible)。
        """
        sh = rl.GetScreenHeight()
        row_h = SAVE_ROW_H
        row_step = SAVE_ROW_STEP
        hint_y = max(SAVE_BASE_Y + row_h + 10, sh - 80)
        visible = max(1, (hint_y - SAVE_BASE_Y - row_h) // row_step)
        return SAVE_BASE_Y, row_h, row_step, hint_y, visible

    def _save_row_rect(self, i):
        """存档行矩形（按绝对行号 i 定位，已扣除滚动偏移）。"""
        sw = rl.GetScreenWidth()
        base_y, row_h, row_step, _hint_y, _visible = self._save_browser_metrics()
        row_x = (sw - SAVE_ROW_W) // 2
        row_y = base_y + (i - self.save_scroll) * row_step
        return (row_x, row_y, SAVE_ROW_W, row_h)

    # ── 输入 ──

    def update(self, dt):
        self.cursor_timer += dt
        if self.cursor_timer > 0.5:
            self.cursor_timer = 0.0
            self.cursor_visible = not self.cursor_visible

        if self.browse_saves:
            self._update_save_browser()
            return

        # 键盘
        while (key := rl.GetKeyPressed()) > 0:
            # 可打印字符优先入框：否则 W/S/L 会被菜单快捷键吃掉，
            # 含 w/s/l 的文字种子就打不出来。
            if 32 <= key <= 126:
                if len(self.input_text) < MAX_SEED_LEN:
                    self.input_text += chr(key).lower()
                continue
            if key == rl.KEY_ENTER or key == rl.KEY_KP_ENTER:
                self._activate_menu(self.menu_index)
                return
            elif key == rl.KEY_UP:
                self.menu_index = (self.menu_index - 1) % len(self.MENU_ITEMS)
                continue
            elif key == rl.KEY_DOWN:
                self.menu_index = (self.menu_index + 1) % len(self.MENU_ITEMS)
                continue
            elif key == rl.KEY_BACKSPACE:
                if self.input_text:
                    self.input_text = self.input_text[:-1]
                continue
            elif key == rl.KEY_ESCAPE:
                self.exit_requested = True
                return

        # 鼠标点击菜单行
        self._handle_menu_click()

    def _update_save_browser(self):
        """存档浏览器输入处理。↑↓/W/S 选择, Enter 载入, Del 删除(二次确认), Esc 返回。"""
        self._sync_visible_clamp()

        # 滚轮滚动
        wheel = rl.GetMouseWheelMove()
        if wheel and self.saved_items:
            max_scroll = max(0, len(self.saved_items)
                             - self._save_browser_metrics()[4])
            self.save_scroll = max(0, min(max_scroll,
                                          self.save_scroll - int(wheel)))

        while (key := rl.GetKeyPressed()) > 0:
            if key == rl.KEY_ESCAPE:
                if self.confirm_delete_path is not None:
                    self.confirm_delete_path = None   # 先取消删除确认
                    continue
                self.browse_saves = False
                return
            elif key in (rl.KEY_UP, rl.KEY_W):
                if self.saved_items:
                    self.save_sel = (self.save_sel - 1) % len(self.saved_items)
                    self.confirm_delete_path = None
                    self._clamp_scroll()
                continue
            elif key in (rl.KEY_DOWN, rl.KEY_S):
                if self.saved_items:
                    self.save_sel = (self.save_sel + 1) % len(self.saved_items)
                    self.confirm_delete_path = None
                    self._clamp_scroll()
                continue
            elif key == rl.KEY_ENTER or key == rl.KEY_KP_ENTER:
                self.confirm_delete_path = None
                self._load_selected_save()
                return
            elif key == rl.KEY_DELETE or key == rl.KEY_BACK:
                self._confirm_delete()
                continue

        # 鼠标：单击选中，双击载入
        self._handle_save_browser_click()

    def _activate_menu(self, index):
        """激活选中的菜单行。"""
        if index == 0:
            self.starting = True
        elif index == 1:
            self._open_save_browser()
        elif index == 2:
            self.exit_requested = True

    def _open_save_browser(self):
        self.browse_saves = True
        self.saved_items = save_system.list_saves()
        self.save_sel = 0
        self.save_scroll = 0              # 重开总是从顶部开始（不残留上次的滚动）
        self.confirm_delete_path = None   # 清掉跨次残留的删除确认
        self._last_click_row = -1         # 清掉跨次残留的双击配对
        self._last_click_time = -1.0
        self._last_visible = None
        self._after_list_refresh()

    def _load_selected_save(self):
        if not self.saved_items or self.save_sel >= len(self.saved_items):
            return
        path = self.saved_items[self.save_sel]["path"]
        print(f"[入口] 加载存档: {os.path.basename(path)}")
        game = save_system.load_game(path)
        if game:
            self.loaded_game = game
            self.starting = True

    def _confirm_delete(self):
        """删除选中存档（需二次确认，与游戏内 F8 存档菜单一致）。"""
        if not self.saved_items or self.save_sel >= len(self.saved_items):
            return
        if self.confirm_delete_path is not None:
            path = self.confirm_delete_path
            self.confirm_delete_path = None
            if save_system.delete_save(path):
                print(f"[入口] 已删除: {os.path.basename(path)}")
                self.saved_items = save_system.list_saves()
                self._after_list_refresh()
        else:
            self.confirm_delete_path = self.saved_items[self.save_sel]["path"]

    def _sync_visible_clamp(self):
        """窗口尺寸变化导致可见行数变化时，重新夹取选中与滚动。

        visible 由当前窗口高度动态算出，只在键盘/滚轮/刷新时夹取滚动会漏掉
        "浏览中拖动窗口"这条路径，导致底部留白或选中行跑出可见区。
        """
        visible = self._save_browser_metrics()[4]
        if visible != self._last_visible:
            self._last_visible = visible
            self._after_list_refresh()

    def _after_list_refresh(self):
        """存档列表刷新后，夹取选中与滚动到合法范围。"""
        n = len(self.saved_items)
        if n == 0:
            self.save_sel = 0
            self.save_scroll = 0
            return
        self.save_sel = max(0, min(self.save_sel, n - 1))
        self._clamp_scroll()

    def _clamp_scroll(self):
        """滚动偏移夹取到合法范围，并保证选中行可见。"""
        visible = self._save_browser_metrics()[4]
        n = len(self.saved_items)
        max_scroll = max(0, n - visible)
        self.save_scroll = max(0, min(max_scroll, self.save_scroll))
        if self.save_sel < self.save_scroll:
            self.save_scroll = self.save_sel
        elif self.save_sel >= self.save_scroll + visible:
            self.save_scroll = self.save_sel - visible + 1

    def _handle_menu_click(self):
        mx, my = rl.GetMouseX(), rl.GetMouseY()
        for i in range(len(self.MENU_ITEMS)):
            rx, ry, rw, rh = self._menu_row_rect(i)
            if rx <= mx <= rx + rw and ry <= my <= ry + rh:
                if rl.IsMouseButtonPressed(rl.MOUSE_BUTTON_LEFT):
                    self.menu_index = i
                    self._activate_menu(i)
                    return

    def _handle_save_browser_click(self):
        """鼠标：单击选中，双击载入。

        raylib 绑定无 IsMouseButtonDoublePressed，用时间戳手动判定。
        """
        mx, my = rl.GetMouseX(), rl.GetMouseY()
        now = rl.GetTime()
        for i in range(len(self.saved_items)):
            rx, ry, rw, rh = self._save_row_rect(i)
            if rx <= mx <= rx + rw and ry <= my <= ry + rh:
                if rl.IsMouseButtonPressed(rl.MOUSE_BUTTON_LEFT):
                    self.save_sel = i
                    self._clamp_scroll()      # 选中行随后一定在可见区内
                    if (i == self._last_click_row
                            and now - self._last_click_time < DOUBLE_CLICK_INTERVAL):
                        # 同行间隔内第二次点击 = 双击，载入
                        self.confirm_delete_path = None
                        self._load_selected_save()
                        self._last_click_row = -1
                    else:
                        self.confirm_delete_path = None
                        self._last_click_time = now
                        self._last_click_row = i
                return

    # ── 绘制 ──

    def draw(self):
        begin_drawing()
        clear_background(color(20, 25, 40))

        if self.browse_saves:
            self._draw_save_browser()
            end_drawing()
            return

        # 标题
        draw_text_centered("SEED TOWER DEFENSE", 80, 40, color(220, 220, 240))
        draw_text_centered("Enter a seed to generate your universe", 130, 16, color(180, 185, 200))

        # 种子输入框
        box_x = rl.GetScreenWidth() // 2 - SEED_BOX_W // 2
        box_y = 190

        draw_rect(box_x, box_y, SEED_BOX_W, SEED_BOX_H, color(40, 45, 60))
        draw_rect_lines(rect(box_x, box_y, SEED_BOX_W, SEED_BOX_H), 2, color(100, 150, 200))

        draw_text(self._seed_display_text(), box_x + SEED_TEXT_PAD, box_y + 12,
                  SEED_FONT_SIZE, color(240, 240, 255))

        if not self.input_text:
            draw_text_centered(f"Leave empty for default seed: {config.DEFAULT_SEED}",
                               box_y + SEED_BOX_H + 8, 14, color(120, 125, 140))

        # 菜单行
        for i, (label, hotkey) in enumerate(self.MENU_ITEMS):
            rx, ry, rw, rh = self._menu_row_rect(i)
            selected = (i == self.menu_index)
            bg = color(60, 70, 100) if selected else color(35, 40, 55)
            draw_rect(rx, ry, rw, rh, bg)
            if selected:
                draw_rect_lines(rect(rx, ry, rw, rh), 2, color(100, 150, 200))

            prefix = "▶ " if selected else "  "
            text = f"{prefix}{label}"
            tw = measure_text(text, 20)
            draw_text(text, rx + (rw - tw) // 2, ry + 9, 20,
                      color(240, 240, 255) if selected else color(175, 180, 195))
            draw_text(f"[{hotkey}]", rx + rw - 56, ry + 9, 14, color(110, 115, 130))

        end_drawing()

    def _draw_save_browser(self):
        _base_y, _row_h, _row_step, hint_y, visible = self._save_browser_metrics()

        draw_text_centered("存档管理", 60, 36, color(220, 220, 240))
        draw_text_centered("Select a save to load", 105, 16, color(180, 185, 200))

        if not self.saved_items:
            draw_text_centered("暂无存档", SAVE_BASE_Y + 60, 22, color(160, 160, 180))
            draw_text_centered("游戏内按 F5 创建存档", SAVE_BASE_Y + 105, 16,
                               color(120, 125, 140))
        else:
            first = self.save_scroll
            last = min(first + visible, len(self.saved_items))
            for i in range(first, last):
                item = self.saved_items[i]
                rx, ry, rw, rh = self._save_row_rect(i)
                selected = (i == self.save_sel)
                bg = color(60, 70, 100) if selected else color(35, 40, 55)
                draw_rect(rx, ry, rw, rh, bg)
                if selected:
                    draw_rect_lines(rect(rx, ry, rw, rh), 1, color(100, 150, 200))

                time_str = save_system.format_time(item["created_at"])
                size_kb = item["size"] / 1024
                label = f"W{item['wave']}  |  种子 {item['seed']}  |  {time_str}  |  {size_kb:.1f}KB"
                draw_text(label, rx + 16, ry + 7, 16,
                          color(240, 240, 255) if selected else color(175, 180, 195))

            if len(self.saved_items) > visible:
                draw_text_centered(f"{self.save_scroll + 1}-{last} / {len(self.saved_items)}",
                                   hint_y - 24, 13, color(120, 125, 140))

        # 底部提示
        if self.confirm_delete_path is not None:
            draw_text_centered("再次按 Del 确认删除   Esc 取消", hint_y, 14,
                               color(255, 170, 120))
        else:
            draw_text_centered("↑↓ 选择   Enter 载入   Del 删除   Esc 返回",
                               hint_y, 14, color(150, 155, 175))

    # ── 输出 ──

    def _seed_display_text(self):
        """输入框内可见文本：超宽时只保留尾部窗口，保证光标始终可见。

        MAX_SEED_LEN=64 允许远超框宽的文本（64 字符 ≈ 798px > 框宽 320px），
        直接绘出会画出框外。这里只显示最右端能放下的部分（含光标），
        所以打字时光标永远可见；退格时更早的字符会滑回来。
        """
        caret = "|" if self.cursor_visible else ""
        max_w = SEED_BOX_W - 2 * SEED_TEXT_PAD
        s = self.input_text
        while s and measure_text(s + caret, SEED_FONT_SIZE) > max_w:
            s = s[1:]
        return s + caret

    def get_seed(self):
        if not self.input_text:
            return config.DEFAULT_SEED
        try:
            return int(self.input_text)
        except ValueError:
            import zlib
            return zlib.crc32(self.input_text.encode("utf-8")) & 0xFFFFFFFF

    def is_ready(self):
        return self.starting

    def get_loaded_game(self):
        return self.loaded_game


# ─── 新建游戏辅助 ────────────────────────────────────────────

def _create_new_game(seed):
    """根据种子创建新的 Game 对象（Universe + Camera + Renderer + Game）。"""
    print(f"\n[初始化] 种子: {seed}")
    print("[生成中] 正在生成宇宙...")
    universe = Universe(seed)
    print("[完成] 宇宙生成完毕")
    print_terrain_overview(universe)

    from camera import Camera
    from renderer import Renderer
    from game import Game

    print("[初始化] 初始化相机...")
    camera = Camera(universe)

    print("[初始化] 初始化渲染器...")
    renderer = Renderer()
    renderer.init_3d()

    print("[初始化] 初始化游戏...")
    return Game(universe, camera, renderer)


# ─── 命令行参数 ─────────────────────────────────────────────

def parse_cli_args(argv):
    """解析命令行参数，返回 ("load", path) | ("seed", int) | ("seed_screen", None)。

    非法参数抛 ValueError（带原因），由调用方打印用法并退出，避免 traceback。
    """
    if not argv:
        return ("seed_screen", None)
    if argv[0] == "--load":
        if len(argv) < 2:
            raise ValueError("--load 需要指定存档路径")
        if len(argv) > 2:
            raise ValueError(f"--load 只接受一个存档路径，多余: {' '.join(argv[2:])}")
        return ("load", argv[1])
    try:
        seed = int(argv[0])
    except ValueError as e:
        raise ValueError(f"种子必须是整数: {argv[0]!r}") from e
    if len(argv) > 1:
        raise ValueError(f"多余的参数: {' '.join(argv[1:])}"
                         "（载入存档请用 --load <路径>）")
    return ("seed", seed)


# ─── 主函数 ──────────────────────────────────────────────────

def _run_game_loop(game):
    """游戏主循环。无论正常退出还是中途抛异常，都保证卸载渲染器并关窗。"""
    try:
        while rl.WindowShouldClose() is False:
            dt = min(rl.GetFrameTime(), 1 / 30)
            game.update(dt)
            game.render()

            # 存档加载替换（F8 菜单载入新存档时）
            pending = getattr(game, "_pending_replace", None)
            if pending:
                game.renderer.unload()
                game = pending
    finally:
        try:
            game.renderer.unload()
        except Exception as e:
            print(f"[退出] 渲染器卸载失败: {type(e).__name__}: {e}")
        rl.CloseWindow()
        print("\n[退出] 游戏已关闭。")


def main():
    rl.SetConfigFlags(rl.FLAG_VSYNC_HINT | rl.FLAG_WINDOW_RESIZABLE)
    rl.InitWindow(config.WINDOW_WIDTH, config.WINDOW_HEIGHT,
                  config.WINDOW_TITLE.encode("utf-8"))
    # 窗口可缩放，但入口页菜单块是固定布局（行 y=430/484/538，高 44，底边 582），
    # 缩到 ~640 以下菜单会被裁掉，所以给一个下限。
    rl.SetWindowMinSize(720, 640)
    # 关掉 raylib 的「Esc 自动关窗」：否则按 Esc 想关科技面板时窗口会跟着退掉。
    # 关闭游戏仍用窗口右上角的 X；各状态自己处理 Esc（关面板/取消操作/返回菜单）。
    rl.SetExitKey(0)
    _LOG_LEVELS = {"warning": rl.LOG_WARNING, "error": rl.LOG_ERROR, "none": rl.LOG_NONE}
    rl.SetTraceLogLevel(_LOG_LEVELS.get(config.LOG_LEVEL, rl.LOG_WARNING))
    rl.SetTargetFPS(config.TARGET_FPS)

    game = None

    try:
        mode, value = parse_cli_args(sys.argv[1:])
    except ValueError as e:
        print(f"[错误] {e}")
        print("用法: python main.py [种子数字 | --load <存档路径>]")
        rl.CloseWindow()
        return

    if mode == "load":
        # --load <path> 直接载入存档
        print(f"[初始化] 载入存档: {value}")
        game = save_system.load_game(value)
        if game is None:
            print("[错误] 无法加载存档")
            rl.CloseWindow()
            return
    elif mode == "seed":
        # 命令行传入种子，跳过种子界面
        game = _create_new_game(value)
    else:
        # 无参数：显示种子界面（可见菜单）
        seed_screen = SeedScreen()
        while rl.WindowShouldClose() is False and not seed_screen.exit_requested:
            seed_screen.update(rl.GetFrameTime())
            seed_screen.draw()
            if seed_screen.is_ready():
                break
        # 种子界面期间点了窗口 X 或按了 Esc：直接退出，不再生成宇宙
        if rl.WindowShouldClose() or seed_screen.exit_requested:
            rl.CloseWindow()
            return

        loaded = seed_screen.get_loaded_game()
        if loaded:
            game = loaded
            print("[初始化] 已从存档载入")
        else:
            seed = seed_screen.get_seed()
            game = _create_new_game(seed)

    print("\n[提示] 游戏运行中。按 Esc 取消操作/关闭面板；关闭窗口请点右上角 X。")
    print("[提示] V: 2D/3D | T: 科技面板(再按T关闭) | B: 加工 | X: 拆除")
    print("[提示] C: 武器库 | P: 刷怪点波次面板 | F5: 快速存档 | F8: 存档管理")

    _run_game_loop(game)


if __name__ == "__main__":
    main()
