"""外部 IM 通道集成（微信 / 钉钉 / 企业微信 / 通用 Webhook）。

分三块，各自独立：
- :mod:`game_agent.integrations.channels`  绑定关系与凭证解析、扫码令牌、外发派发
- :mod:`game_agent.integrations.inbound`   入站回调（钉钉/企微/个微）：扫码绑定落地、回复消息并入会话
- :mod:`game_agent.api.routers.channels`   HTTP 接口（设置页、二维码、回调）

设计取舍见 docs/DESIGN-CHANNELS.md：微信个人号没有官方开放的回调通道，所以通道能力
分成「可外发」「可入站」两档，界面按档位显示，不假装都支持。
"""
