# 字体与中文渲染

## ⚠️ 先说关键限制

在 raylib 6.x（底层 stb_truetype）的 Python 绑定中，如果加载 `.ttc` 时未正确指定 `fontIndex`，会静默退回默认字体（只含 ASCII）→ 中文照样显示为 `?`。

不带字体、依赖系统回退，**能不能用完全取决于机器上有没有“单 .ttf 的中文字体”**：
- 中文版 Windows（有黑体/等线）→ 一般可用 ✅
- 西文 Windows / 精简 Linux / 多数 macOS（只有 .ttc/.otf）→ 回退失败，中文变 `?` ❌

当前加载器已加入 `_font_has_cjk` 校验：不含中文的候选会被跳过，不会卡在坏的 `.ttc` 上。

---

## 怎么用

### A. 不带字体，用系统回退
**什么都不用做**。不放 `game1/assets/cn_font.ttf`，游戏按顺序找系统字体：
- Windows: `simhei.ttf` → `Deng.ttf` → `msyh.ttc`/`simsun.ttc`（后两者会被校验跳过）
- macOS: `Arial Unicode.ttf`（若有）→ PingFang 等（多为 .ttc，多半跳过）
- Linux: Noto CJK / 文泉驿（多为 .ttc/.otf，多半跳过）

### B. 带字体
把一个支持简体中文的 **TrueType** 字体重命名为 `cn_font.ttf`，放到 `game1/assets/cn_font.ttf`。

游戏启动时会自动优先用它，保证 Windows/macOS/Linux 三平台渲染一致。

## 对任何字体文件的要求（硬约束）

- **必须是 TrueType（`.ttf` / `glyf` 轮廓）**。OTF/CFF（`.otf`）在 raylib 下不可靠（缺字 raylib [#2550](https://github.com/raysan5/raylib/issues/2550)、崩溃 [#5435](https://github.com/raysan5/raylib/issues/5435)）。`.ttc` 大概率不被支持。
- 静态实例优先；变量字体只读默认实例（可能偏细）。
- 需覆盖游戏用到的码点（见 `_cn_codepoints.py`，约 890 个：ASCII 32–126 + 简体常用字）。缺的字显示成方框。

---

## 排查

- **中文显示 `?`**：没找到可用 CJK 字体。依次查：
  1. `game1/assets/cn_font.ttf` 在不在；
  2. 系统有没有**单 .ttf** 中文字体（`.ttc` 不算）；
  3. 字体格式是否 TrueType（非 .otf/.ttc）。
- **部分字变方框**：字体没覆盖那个字（对照 `_cn_codepoints.py` 与字体 `cmap`）。
