"""通道模块自测（不碰真库、不发真网络请求）。

覆盖：
1. 绑定 upsert：同标识换人绑定 → 旧绑定被 revoke
2. 群机器人直填绑定：不需要 token（扫码入口已移除），external_id 用 group- 占位
3. 通知内容拼装（成功/失败/待回答三种）+ 文案不再谎称「可以直接回复」
4. 钉钉群机器人「加签」：_dingtalk_sign 生成 timestamp/sign 且能被验签还原
5. 钉钉回调验签：正确签名通过、篡改失败、无 secret 时如实标 unverified
6. 企微签名

跑法：python tests/test_channels_selfcheck.py
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import sys
import uuid
from urllib.parse import parse_qs, urlencode
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


async def main() -> int:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from game_agent.db.models import Base, Message, Project, Run, Session, Team, User
    from game_agent.integrations import channels as ch
    from game_agent.integrations import inbound

    fails: list[str] = []

    def check(name: str, cond: bool, extra: str = "") -> None:
        print(("  ok   " if cond else "  FAIL ") + name + (f"  {extra}" if not cond and extra else ""))
        if not cond:
            fails.append(name)

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    team_id = uuid.uuid4()
    user_id = uuid.uuid4()
    async with sf() as s:
        s.add(Team(id=team_id, name="t", slug="t" + uuid.uuid4().hex[:6]))
        await s.flush()
        s.add(User(id=user_id, email=f"{uuid.uuid4().hex[:6]}@local", display_name="u"))
        await s.flush()
        p = Project(team_id=team_id, name="p", slug="p", repo_path="p")
        s.add(p)
        await s.flush()
        await s.commit()

    # ---------------- 1. 群机器人直填绑定（无 token） ----------------
    link = await ch.upsert_link(sf, team_id=team_id, user_id=user_id, channel="dingtalk",
                               external_id="group-abc12345", display_name="群提醒",
                               credentials={"webhook": "https://oapi.dingtalk.com/robot/send?access_token=x"},
                               inbound_enabled=False)
    check("群机器人绑定成功", link is not None and str(link.id))
    check("群绑定默认不做入站回复", link.inbound_enabled is False, str(link.inbound_enabled))
    check("提醒开关默认全开",
          bool(link.notify_done) and bool(link.notify_question) and bool(link.notify_failed))
    got = await ch.find_by_external(sf, "dingtalk", "group-abc12345")
    check("能按 external_id 找回绑定", got is not None and str(got.id) == str(link.id))

    # ---------------- 2. 同标识换人绑定 → revoke 旧的 ----------------
    other_user = uuid.uuid4()
    async with sf() as s:
        s.add(User(id=other_user, email=f"{uuid.uuid4().hex[:6]}@local", display_name="u2"))
        await s.commit()
    await ch.upsert_link(sf, team_id=team_id, user_id=other_user, channel="dingtalk",
                         external_id="group-abc12345", credentials={"webhook": "https://x/y"},
                         inbound_enabled=False)
    links = await ch.active_links(sf, team_id, "dingtalk")
    check("同标识只保留一条 active 绑定", len(links) == 1, f"got {len(links)}")
    check("active 绑定归新用户", links and str(links[0].user_id) == str(other_user))

    # ---------------- 3. 通知拼装 ----------------
    kind, title, body = await ch.compose_run_notice(sf, str(uuid.uuid4()), "succeeded", None)
    check("成功通知 kind=done", kind == "done", kind)
    check("成功通知标题含完成任务", "任务完成" in title, title)
    check("通知正文不再谎称可以直接回复", "可以直接回复" not in body, body)
    k2, _t2, _b2 = await ch.compose_run_notice(sf, str(uuid.uuid4()), "failed", None)
    check("失败通知 kind=failed", k2 == "failed", k2)
    k3, _t3, _b3 = await ch.compose_run_notice(sf, str(uuid.uuid4()), "blocked", None)
    check("待回答通知 kind=question", k3 == "question", k3)

    # 通知必须带「来源」（项目/会话/发起人）和**完整汇报正文**：
    # 群里可能同时挂多个项目，标题只写「任务完成」用户分不清是哪件事；
    # 汇报被截断就失去「不在电脑前也能读完结论」的意义（用户 2026-11-14 明确要求）。
    proj = Project(team_id=team_id, name="循迹主站", slug=f"p-{uuid.uuid4().hex[:6]}",
                   repo_path="web")
    async with sf() as s:
        s.add(proj)
        await s.commit()          # 先落库：sessions.project_id 是 NOT NULL，proj.id 需已生成
        sess = Session(team_id=team_id, project_id=proj.id, title="增加微信钉钉关联")
        asker = User(email=f"{uuid.uuid4().hex[:6]}@local", display_name="老王")
        s.add_all([sess, asker])
        await s.commit()
        ask_msg = Message(session_id=sess.id, author_type="user", author_id=asker.id,
                          content="增加微信、钉钉关联功能")
        s.add(ask_msg)
        await s.commit()
        long_report = "## 目标\n" + ("这一段是详细汇报内容。" * 200) + "\n## 结论\n已修好"
        run = Run(team_id=team_id, project_id=proj.id, session_id=sess.id,
                  triggered_by=asker.id, trigger_msg_id=ask_msg.id, status="succeeded",
                  final_summary=long_report)
        s.add(run)
        await s.commit()
        run_id2 = str(run.id)
    k4, t4, b4 = await ch.compose_run_notice(sf, run_id2, "succeeded", None)
    check("通知标题带项目名", "循迹主站" in t4, t4)
    check("通知标题带会话名", "增加微信钉钉关联" in t4, t4)
    check("正文带项目来源", "**项目**：循迹主站" in b4, b4[:200])
    check("正文带会话来源", "**会话**：增加微信钉钉关联" in b4, b4[:200])
    check("正文带发起人", "老王" in b4, b4[:200])
    check("正文带用户原问题", "增加微信、钉钉关联功能" in b4, b4[:400])
    check("汇报正文完整未被截断", "已修好" in b4 and len(b4) > 2300, f"len={len(b4)}")
    check("正文带会话ID便于对号", str(sess.id) in b4, b4[-200:])

    # 历史库里有 6000 字量级的汇报（真实样本 6098）。旧代码 _short(...,3000) 会静默腰斩，
    # 用户看到的就是「钉钉里汇报短了一截、没有任何提示」。这里锁死两件事：
    #   ① 6000+ 字的汇报必须整段发出；
    #   ② 就算超过兜底上限，正文里必须显式写明被省略，不能靠一个「…」了事。
    huge_report = "## 汇报\n" + ("这是一段很长的正文内容。" * 700) + "\n## 结尾标记XYZ"
    async with sf() as s:
        run2 = Run(team_id=team_id, project_id=proj.id, session_id=sess.id,
                   triggered_by=asker.id, trigger_msg_id=ask_msg.id, status="succeeded",
                   final_summary=huge_report)
        s.add(run2)
        await s.commit()
        run_id3 = str(run2.id)
    _k5, _t5, b5 = await ch.compose_run_notice(sf, run_id3, "succeeded", None)
    check("6000+字汇报整段发出（不再被3000腰斩）",
          "结尾标记XYZ" in b5 and len(huge_report) > 6000, f"report={len(huge_report)} body={len(b5)}")

    over = "## 汇报\n" + ("超长内容。" * 6000)   # 约 30000 字，超过 NOTICE_BODY_LIMIT
    async with sf() as s:
        run3 = Run(team_id=team_id, project_id=proj.id, session_id=sess.id,
                   triggered_by=asker.id, trigger_msg_id=ask_msg.id, status="succeeded",
                   final_summary=over)
        s.add(run3)
        await s.commit()
        run_id4 = str(run3.id)
    _k6, _t6, b6 = await ch.compose_run_notice(sf, run_id4, "succeeded", None)
    check("超兜底上限时正文写明省略字数", "省略约" in b6, b6[-400:])
    check("超兜底上限时仍保留结尾", "超长内容。" in b6, b6[-120:])

    # 只有真正能收消息的绑定才追加「可以回复」提示（发不出去的绑定不追加）
    notices = await ch.notify_run_finished(sf, run_id=str(uuid.uuid4()), team_id=team_id,
                                           status="succeeded", session_id=None)
    check("收尾通知发给 active 绑定", len(notices) == 1, f"got {len(notices)}")
    if notices:
        # 这个绑定 inbound_enabled=False，正文不该带「可以回复继续对话」
        check("纯提醒绑定不带回复提示", "继续对话" not in (notices[0].body or ""), notices[0].body or "")
        check("发送失败原因被记录（假 webhook）",
              notices[0].status == "failed" and bool(notices[0].error), str(notices[0].error))
        # 反向：明确允许 IM 回复的绑定，才追加「可以回复」提示
        async with sf() as s:
            row = await s.get(ch.ChannelLink, notices[0].link_id)
            row.inbound_enabled = True
            await s.commit()
        notices2 = await ch.notify_run_finished(sf, run_id=str(uuid.uuid4()), team_id=team_id,
                                               status="succeeded", session_id=None)
        check("允许回复的绑定才带回复提示",
              bool(notices2) and "继续对话" in (notices2[0].body or ""),
              notices2[0].body if notices2 else "no notice")

    # ---------------- 4. 钉钉加签 ----------------
    secret = "SEC-test-secret"
    sig = ch._dingtalk_sign(secret)
    check("加签含 timestamp 与 sign", bool(sig.get("timestamp")) and bool(sig.get("sign")), str(sig))
    # 用同一个 secret 按钉钉口径复算：sign 必须是**裸 base64**，不能是被手工 URL 编码过的串。
    # 历史 bug：_dingtalk_sign 里先 quote_plus 编一次、调用处 urlencode 再编一次，
    # 钉钉只解码一次拿到带 %2B 的串 → 验签失败 errcode 310000（表现为「测试能收到、
    # 任务完成收不到」）。旧断言用 unquote(sign) 比较，正好把这个 bug 掩盖掉了。
    ts = sig["timestamp"]
    want = base64.b64encode(hmac.new(secret.encode(), f"{ts}\n{secret}".encode(),
                                     hashlib.sha256).digest()).decode()
    check("加签 sign 是裸 base64（未被提前 URL 编码）", sig["sign"] == want,
          f"{sig['sign']} != {want}")
    check("加签 sign 不含 % 转义", "%" not in sig["sign"], sig["sign"])
    # 最终拼进 URL 时必须只被编码一次：解码回来要等于裸 sign
    url_qs = urlencode(sig)
    check("加签参数进 URL 只编码一次",
          parse_qs(url_qs).get("sign") == [want] and "%25" not in url_qs,
          url_qs)
    qs = parse_qs(f"timestamp={ts}&sign={sig['sign']}")
    check("加签参数能拼进 URL 查询串", qs.get("timestamp") == [ts] and "sign" in qs, str(qs))

    # ---------------- 5. 钉钉回调验签 ----------------
    ts2 = "1700000000000"
    want2 = base64.b64encode(hmac.new(secret.encode(), f"{ts2}\n{secret}".encode(),
                                      hashlib.sha256).digest()).decode()
    ok, why = inbound.verify_dingtalk({"timestamp": ts2, "sign": want2}, secret)
    check("正确签名通过", ok is True, why)
    bad_ok, bad_why = inbound.verify_dingtalk({"timestamp": ts2, "sign": "xxxx"}, secret)
    check("篡改签名被拒", bad_ok is False, bad_why)
    un_ok, un_why = inbound.verify_dingtalk({}, None)
    check("无 secret 时标记 unverified", un_ok is True and un_why == "unverified", un_why)

    # ---------------- 6. 企微签名 ----------------
    s1 = inbound.wecom_signature("tok", "123", "nonce", "enc")
    check("企微签名是 40 位 sha1",
          len(s1) == 40 and s1 == inbound.wecom_signature("tok", "123", "nonce", "enc"))

    await engine.dispose()
    print()
    if fails:
        print(f"FAILED {len(fails)}: {fails}")
        return 1
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
