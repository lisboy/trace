"""进程关闭标志：区分「用户取消」与「服务关闭」。

为什么需要：worker 任务收到 CancelledError 时，orchestrator 默认按「用户点了取消」处理
（落 cancelled + 「已按你的要求取消本次执行」）。但服务优雅停机时也会 cancel 在跑的
worker——若沿用默认措辞，重启后用户会看到一堆「已按你的要求取消」，像是自己误点，
与事实不符（真相是服务重启中断）。

这里用一个进程级的布尔标志：main 在停机前先置位，orchestrator 的取消分支据此改判为
「服务重启，运行中断」（和启动清理对僵尸 run 的措辞一致），避免谎报。单事件循环，普通
bool 足够，不需要加锁。
"""
from __future__ import annotations

_shutting_down = False


def begin_shutdown() -> None:
    """置位停机标志（main 在 cancel worker 之前调用）。"""
    global _shutting_down
    _shutting_down = True


def is_shutting_down() -> bool:
    return _shutting_down


def reset() -> None:
    """测试用：把标志复位。"""
    global _shutting_down
    _shutting_down = False
