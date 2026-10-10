"""
main.py - 游戏入口
"""

import sys
import os

LIBS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "libs")
if os.path.isdir(LIBS_DIR):
    sys.path.insert(0, LIBS_DIR)

import raylib as rl
from rl_utils import *

import config
from world import Universe, print_terrain_overview


class SeedScreen:
    def __init__(self):
        self.input_text = ""
        self.cursor_visible = True
        self.cursor_timer = 0.0
        self.starting = False
        self.focus = True
        self.exit_requested = False

    def update(self, dt):
        if not self.focus:
            return
        self.cursor_timer += dt
        if self.cursor_timer > 0.5:
            self.cursor_timer = 0.0
            self.cursor_visible = not self.cursor_visible

        while (key := rl.GetKeyPressed()) > 0:
            if key == rl.KEY_ENTER or key == rl.KEY_KP_ENTER:
                self.starting = True
                return
            elif key == rl.KEY_BACKSPACE:
                if self.input_text:
                    self.input_text = self.input_text[:-1]
                continue
            elif key == rl.KEY_ESCAPE:
                self.exit_requested = True
                return
            elif key >= 48 and key <= 57:
                self.input_text += chr(key)
            elif key >= 65 and key <= 90:
                self.input_text += chr(key).lower()
            elif key == 32:
                self.input_text += " "


    def draw(self):
        begin_drawing()
        clear_background(color(20, 25, 40))

        draw_text_centered("SEED TOWER DEFENSE", 120, 40, color(220, 220, 240))
        draw_text_centered("Enter a seed to generate your universe", 180, 16, color(180, 185, 200))

        box_x = rl.GetScreenWidth() // 2 - 150
        box_y = 280
        box_w = 300
        box_h = 50

        draw_rect(box_x, box_y, box_w, box_h, color(40, 45, 60))
        draw_rect_lines(rect(box_x, box_y, box_w, box_h), 2, color(100, 150, 200))

        text = self.input_text
        if self.cursor_visible:
            text += "|"
        draw_text(text, box_x + 15, box_y + 10, 22, color(240, 240, 255))

        draw_text_centered("Press Enter to start  |  Press Esc to quit",
                           box_y + box_h + 30, 14, color(150, 155, 175))

        if not self.input_text:
            draw_text_centered(f"Leave empty for default seed: {config.DEFAULT_SEED}",
                               box_y + box_h + 55, 14, color(120, 125, 140))

        end_drawing()

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


def main():
    rl.SetConfigFlags(rl.FLAG_VSYNC_HINT | rl.FLAG_WINDOW_RESIZABLE)
    rl.InitWindow(config.WINDOW_WIDTH, config.WINDOW_HEIGHT,
                  config.WINDOW_TITLE.encode("utf-8"))
    _LOG_LEVELS = {"warning": rl.LOG_WARNING, "error": rl.LOG_ERROR, "none": rl.LOG_NONE}
    rl.SetTraceLogLevel(_LOG_LEVELS.get(config.LOG_LEVEL, rl.LOG_WARNING))
    rl.SetTargetFPS(config.TARGET_FPS)

    # 支持命令行传入种子跳过种子界面
    import sys
    if len(sys.argv) > 1:
        seed = int(sys.argv[1])
    else:
        seed_screen = SeedScreen()
        while rl.WindowShouldClose() is False and not seed_screen.exit_requested:
            seed_screen.update(rl.GetFrameTime())
            seed_screen.draw()
            if seed_screen.is_ready():
                break
        if seed_screen.exit_requested:
            rl.CloseWindow()
            return
        seed = seed_screen.get_seed()
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
    game = Game(universe, camera, renderer)

    print("\n[提示] 游戏运行中。按 Esc 退出。")
    print("[提示] V: 2D/3D | T: 科技面板 | 1-4: 选塔 | 点击: 放置")

    while rl.WindowShouldClose() is False:
        dt = min(rl.GetFrameTime(), 1/30)
        game.update(dt)
        game.render()

        if not game.is_running():
            break

    renderer.unload()
    rl.CloseWindow()
    print("\n[退出] 游戏已关闭。")


if __name__ == "__main__":
    main()
