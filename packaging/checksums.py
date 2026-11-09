"""为分发产物生成 .sha256 校验文件（sha256sum 兼容格式）。

用法：
  python packaging/checksums.py [目录]      # 默认 dist_release

只处理 .zip / .tar.gz 文件，已存在同名 .sha256 会被覆盖重算。
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    out = Path(argv[1]) if len(argv) > 1 else Path("dist_release")
    if not out.is_dir():
        print(f"[checksum] 目录不存在: {out}")
        return 1
    n = 0
    for p in sorted(out.iterdir()):
        if not p.is_file() or p.name.endswith(".sha256"):
            continue
        if not (p.name.endswith(".zip") or p.name.endswith(".tar.gz")):
            continue
        h = hashlib.sha256(p.read_bytes()).hexdigest()
        # newline="\n": sha256sum(1) 在 Linux/macOS 上无法解析 CRLF 行尾，必须写纯 LF。
        with open(p.with_name(p.name + ".sha256"), "w", encoding="utf-8", newline="\n") as f:
            f.write(f"{h} *{p.name}\n")
        print(f"  {p.name}  {h[:16]}...")
        n += 1
    print(f"[checksum] {n} 个文件 -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
