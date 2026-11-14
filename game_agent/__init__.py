"""循迹（Trace） · 个人版游戏开发智能体

设计文档：docs/DESIGN.md (v0.3.1)
实现范围：lite 模式核心引擎 + M0-M6 单人可用主链路。
全部跨模式差异收敛在 5 个抽象接口后（Repository / EventBus / Queue / LockManager / ObjectStore）。
"""
__version__ = "0.4.0"
