"""把 packaging/exe/icon-src/trace-icon.svg 栅格化，生成所有需要的图标产物。

为什么要这个脚本（而不是手工存几张图）：
  同一份母版要出 6 类产物（浏览器 favicon 三件套 + 打包 exe 的 .ico + PWA touch icon），
  手工会漏尺寸、且改一次母版就要重做一遍。这里一次生成、尺寸固定、可重复。

栅格化不依赖 cairosvg（本机没装）：用无头 Chrome 渲染 SVG → 高清 PNG → Pillow 缩放，
各尺寸单独从矢量缩放（不是从 256 缩），16px 也不会糊。

用法（仓库根目录）：
  .venv\\Scripts\\python.exe packaging\\exe\\icon-src\\build_icons.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[3]
SRC_SVG = Path(__file__).resolve().parent / "trace-icon.svg"

# 产物落点
PUBLIC = ROOT / "frontend" / "public"
WEB_DIST = ROOT / "web" / "dist"
EXE_ICO = ROOT / "packaging" / "exe" / "Trace.ico"

# .ico 内含尺寸（Windows 资源管理器 16/20/24/32/40/48/64/128/256 都会取用）
ICO_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)

_BROWSERS = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)


def _browser_exe() -> str:
    env = os.environ.get("GA_BROWSER_PATH", "").strip()
    if env and Path(env).is_file():
        return env
    for name in ("chrome", "msedge", "chromium", "google-chrome"):
        found = shutil.which(name)
        if found:
            return found
    for p in _BROWSERS:
        if Path(p).is_file():
            return p
    raise SystemExit("找不到无头浏览器（Chrome/Edge）：可设 GA_BROWSER_PATH 指向 chrome.exe")


def render_master(size: int = 1024) -> Image.Image:
    """无头浏览器渲染 SVG 到透明背景 PNG，再读成 PIL 图。"""
    browser = _browser_exe()
    with tempfile.TemporaryDirectory() as td:
        td_p = Path(td)
        html = td_p / "icon.html"
        png = td_p / "icon.png"
        svg_text = SRC_SVG.read_text(encoding="utf-8")
        html.write_text(
            "<!doctype html><meta charset='utf-8'>"
            "<style>html,body{margin:0;padding:0;background:transparent}"
            f"svg{{display:block;width:{size}px;height:{size}px}}</style>"
            + svg_text,
            encoding="utf-8",
        )
        cmd = [
            browser, "--headless=new", "--disable-gpu", "--hide-scrollbars",
            "--default-background-color=00000000",
            f"--window-size={size},{size}",
            f"--screenshot={png}",
            html.as_uri(),
        ]
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=120)
        if not png.is_file():
            raise SystemExit(f"浏览器没有产出截图：{png}")
        return Image.open(png).convert("RGBA")


def resize(master: Image.Image, size: int) -> Image.Image:
    return master.resize((size, size), Image.LANCZOS)


def save_png(img: Image.Image, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, format="PNG", optimize=True)


def main() -> None:
    if not SRC_SVG.is_file():
        raise SystemExit(f"缺少母版：{SRC_SVG}")
    print(f"[icons] 渲染母版 {SRC_SVG.name} …")
    master = render_master(1024)
    if master.getbbox() is None:
        raise SystemExit("母版渲染结果是全透明，检查 SVG 是否被浏览器正确加载")

    # 1) 浏览器 favicon：SVG 母版 + 32px PNG + 180px touch icon
    PUBLIC.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(SRC_SVG, PUBLIC / "favicon.svg")
    save_png(resize(master, 32), PUBLIC / "favicon-32.png")
    save_png(resize(master, 180), PUBLIC / "apple-touch-icon.png")
    print("[icons] favicon.svg / favicon-32.png / apple-touch-icon.png")

    # 2) favicon.ico（多尺寸）
    frames = [resize(master, s) for s in ICO_SIZES]
    ico_tmp = PUBLIC / "favicon.ico"
    frames[-1].save(ico_tmp, format="ICO", sizes=[(s, s) for s in ICO_SIZES])
    print(f"[icons] favicon.ico ({', '.join(str(s) for s in ICO_SIZES)})")

    # 3) exe 图标
    EXE_ICO.parent.mkdir(parents=True, exist_ok=True)
    frames[-1].save(EXE_ICO, format="ICO", sizes=[(s, s) for s in ICO_SIZES])
    print(f"[icons] {EXE_ICO.relative_to(ROOT)}")

    # 4) 同步到已构建的 web/dist（不动 default-avatar 等其它静态资源）
    if WEB_DIST.is_dir():
        shutil.copyfile(PUBLIC / "favicon.svg", WEB_DIST / "favicon.svg")
        shutil.copyfile(PUBLIC / "favicon-32.png", WEB_DIST / "favicon-32.png")
        shutil.copyfile(PUBLIC / "apple-touch-icon.png", WEB_DIST / "apple-touch-icon.png")
        shutil.copyfile(PUBLIC / "favicon.ico", WEB_DIST / "favicon.ico")
        print("[icons] 已同步到 web/dist")

    # 5) 顺带出几张预览图，方便肉眼验收（放 .trace/tmp，随时可删）
    prev = ROOT / ".trace" / "tmp" / "icon-preview"
    prev.mkdir(parents=True, exist_ok=True)
    strip_labels = [16, 24, 32, 48, 64, 128, 256]
    sheet = Image.new("RGBA", (sum(strip_labels) + 12 * (len(strip_labels) + 1), 272),
                      (24, 24, 28, 255))
    x = 12
    for s in strip_labels:
        im = resize(master, s)
        sheet.alpha_composite(im, (x, 12))
        x += s + 12
    sheet.save(prev / "strips.png")
    save_png(resize(master, 512), prev / "icon-512.png")
    print(f"[icons] 预览：{prev}（strips.png / icon-512.png）")


if __name__ == "__main__":
    sys.exit(main())
