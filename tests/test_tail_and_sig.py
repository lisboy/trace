"""思考截断与循环签名归一化的纯函数单测。

对应 docs/DESIGN-OPTIMIZATION.md 里两个 P0 快赢项：
- 「思考尾部优先截断」：_tail_keep_head 不再取头丢尾，而是保头部锚点 + 尾部主体；
- 「死循环签名归一化」：_sig_arg 让路径/时间戳/随机段不参与签名，不同文件仍可区分。
这两个函数此前只有 _tail_with_sources 的测试，核心行为没有直接单测钉死。
"""
from __future__ import annotations

from game_agent.agents.runtime import _call_signature, _sig_arg, _tail_keep_head


# ---------------------------------------------------------------- _tail_keep_head
def test_tail_short_text_passthrough() -> None:
    """不超限原样返回，不添油加醋。"""
    text = "短文本"
    assert _tail_keep_head(text, 100) == text


def test_tail_keeps_tail_when_over_limit() -> None:
    """超限时末尾结论必须保留——旧逻辑 [:limit] 会把最值钱的尾部丢掉。"""
    text = "开头" + "中" * 500 + "末尾结论"
    cut = _tail_keep_head(text, 100, head=10)
    assert "末尾结论" in cut          # 尾部保住
    assert len(cut) <= 130            # head + 中略标记 + tail，不会无界膨胀
    assert "…[中略]…" in cut          # 省略标记存在


def test_tail_head_anchor_kept() -> None:
    """前 head 字锚点保留，便于续跑时知道上下文从哪开始。"""
    text = "ABCDEFGHIJ" + "x" * 500 + "END"
    cut = _tail_keep_head(text, 100, head=10)
    assert cut.startswith("ABCDEFGHIJ")
    assert cut.endswith("END")


def test_tail_plain_result_uses_tail() -> None:
    """无来源块时，超长文本末尾优先，行为与 _tail_with_sources 对正文的处理一致。"""
    text = "x" * 5000
    cut = _tail_keep_head(text, 2000)
    assert cut.endswith("x" * (2000 - 80))
    assert cut.startswith("x" * 80)


# ---------------------------------------------------------------- _sig_arg
def test_sig_numbers_and_bool() -> None:
    """数字/时间戳归一为 #num，bool 原样保留（False 不能被 str() 吃掉）。"""
    assert _sig_arg(123) == "#num"
    assert _sig_arg(3.14) == "#num"
    assert _sig_arg(True) is True
    assert _sig_arg(False) is False
    assert _sig_arg("run-20261109") == "run-#num"
    assert _sig_arg("第 12 轮") == "第 #num 轮"


def test_sig_long_random_token_normalized() -> None:
    """20+ 位连续字母/下划线/横杠段归一为 #rand，不参与循环签名。"""
    assert _sig_arg("abcdefghijklmnopqrstuvwx") == "#rand"
    assert _sig_arg("another_random_token_segment") == "#rand"


def test_sig_timestamp_shot_names_normalize_same() -> None:
    """截图名里的时间戳不参与签名：同基名、不同时间戳应归一成同一签名。"""
    a = _sig_arg("shot-1794241065424-02b38b")
    b = _sig_arg("shot-1794241065425-02b38b")
    assert a == b
    assert "1794241065424" not in a


def test_sig_abs_path_keeps_basename() -> None:
    """绝对路径只保留最后一段；不同文件名仍可区分。"""
    assert _sig_arg("/data/projects/foo/bar.txt") == "bar.txt"
    assert _sig_arg("D:/repo/src/main.py") == "main.py"


def test_sig_relative_path_preserved() -> None:
    """相对路径原样保留（不同文件/目录仍能区分，不会被误归一）。"""
    assert _sig_arg("src/main.py") == "src/main.py"
    assert _sig_arg("docs/DESIGN.md") == "docs/DESIGN.md"


def test_sig_nested_dict_list_recursive() -> None:
    """dict/list 递归归一，键顺序不影响签名。"""
    a = {"path": "D:/repo/a.txt", "ts": 20261109, "items": [1, "x" * 30]}
    b = {"items": [2, "y" * 25], "ts": 20251101, "path": "D:/repo/a.txt"}
    assert _sig_arg(a) == _sig_arg(b)


# ---------------------------------------------------------------- _call_signature 分页不误判
def test_call_signature_pagination_offsets_distinct() -> None:
    """连续翻页读同一文件（offset 不同）必须是不同签名，不能被死循环检测误杀。

    真实事故（2026-11-11）：读一个 685 行文件，offset=300 → 420 → 599 连翻几页，
    offset/limit 全被归一成 #num → 三次签名完全相同 → 「重复调用」被掐停收尾、
    还落成 failed，用户看到「上一次执行失败：只做了定位/勘察」的弹窗。
    """
    a = _call_signature("read_file", {"path": "index.html", "offset": 300, "limit": 400})
    b = _call_signature("read_file", {"path": "index.html", "offset": 420, "limit": 300})
    c = _call_signature("read_file", {"path": "index.html", "offset": 599, "limit": 120})
    assert a != b != c and a != c


def test_call_signature_same_pagination_still_identical() -> None:
    """同一页（offset/limit 完全一样）连发，仍应判成同一签名——真正的重复调用照样拦。"""
    a = _call_signature("read_file", {"path": "index.html", "offset": 300, "limit": 400})
    b = _call_signature("read_file", {"path": "index.html", "offset": 300, "limit": 400})
    assert a == b


def test_call_signature_non_pagination_numbers_still_normalized() -> None:
    """非分页参数里的数字（时间戳/长 id）仍按原规则归一，不影响既有行为。"""
    a = _call_signature("run_command", {"cmd": "pytest -k test_run 20261109"})
    b = _call_signature("run_command", {"cmd": "pytest -k test_run 20251101"})
    assert a == b
