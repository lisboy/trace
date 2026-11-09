"""生成 GA_SECRET_KEY（用于会话/令牌签名）。

用法：
    python scripts/gen_secret.py
"""
from __future__ import annotations

import secrets
import base64


def main() -> None:
    raw = secrets.token_bytes(32)
    print(base64.urlsafe_b64encode(raw).decode("ascii"))


if __name__ == "__main__":
    main()
