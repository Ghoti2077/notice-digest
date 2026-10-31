#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""微信群消息 -> 模型筛选 -> 飞书卡片 / 待办 / 台账。只挑真通知，闲聊不推。

用法:
  python wechat.py            正常跑（有新通知才推）
  python wechat.py --dry-run  只打印不推送
  python wechat.py --force    忽略游标，把最近 30 条全当候选（测试用）

读库靠 wechatauto-replica（解密本地 SQLCipher，窗口最小化也照读）。
微信必须登录着，否则抓不到密钥。
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
import config
import push as SZ  # 复用模型链、飞书发送、台账

STATE = os.path.join(HERE, "state.json")
GROUP = config.WECHAT["group"]
ACCOUNT = config.WECHAT["account"]
AUDIENCE = config.WECHAT.get("audience", "学生")

PROMPT = """你在给{audience}挑班级群通知。今天是 {today}。

下面是「{group}」群里最近的若干条消息，每条格式：[序号] 发送者: 内容

只挑**真的通知**：要学生本人办的事（交材料、参加活动/会议/测试、有截止日期、停课调课、缴费、领东西、请假流程），以及班干部/辅导员/老师发的正式安排。
同学之间的闲聊、接话、表情、玩笑、单纯转发新闻一律不要。

对每条选中的输出：
what：60~100 字大白话（必须写满，宁可啰嗦也别只丢一个标题），办什么事、谁要办、什么时候、怎么办/找谁、要带什么。
action：截止时间、要带的材料、去哪办；没有就写「无」。
prio 三选一（按今天 {today} 算，别乱给 high）：
  high —— 还没过期，且 3 天内截止、或必须今明两天办、或全体必须参加就在近期
  normal —— 还没过期但时间宽裕（一周以上），或只是可选报名/自愿参加
  expired —— 截止日期已经早于今天 {today}，这事已经过去了

严格输出 JSON 数组，不要解释、不要 markdown 代码块。一条真通知都没有就输出 []：
[{"id":"序号","what":"...","action":"...","prio":"high 或 normal 或 expired"}]

消息：
%s"""

KEEP_TYPES = ("文本", "文件/链接/卡片", "引用")


def body(m):
    """把 'wxid_xxx:\\n正文' 切成正文；卡片 XML 抽 title。"""
    c = (m.get("content") or "").strip()
    if "\n" in c and ("wxid_" in c.split("\n")[0] or "@chatroom" in c.split("\n")[0]):
        c = c.split("\n", 1)[1]
    c = c.strip()
    if c.startswith("<?xml") or c.startswith("<msg"):
        t = re.search(r"<title>(.*?)</title>", c, re.S)
        c = "【卡片】" + (t.group(1).strip() if t else "链接或文件")
    return re.sub(r"<[^>]+>", " ", c).strip()


def member_names(db, gid):
    for g in db.get_groups():
        if g["username"] == gid:
            return {m["username"]: (m.get("remark") or m.get("nick_name") or "?")
                    for m in g["members"]}
    return {}


def _days_left(text, base=None):
    """从一句人话里估出还剩几天（负数=已过期）。None = 看不出来。

    base = **消息发出的那天**。关键：「明天」指的是发消息那天的明天，
    不是今天的明天——拿今天去算，几天前的通知会永远显示「还有 1 天」。
    绝对日期（10月29日）比相对词可靠，优先用。"""
    import datetime
    today = datetime.date.today()
    base = base or today
    t = str(text)

    m = re.search(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]", t)
    if m:
        try:
            d = datetime.date(today.year, int(m.group(1)), int(m.group(2)))
        except ValueError:
            return None
        if (base - d).days > 60:              # 消息在 12 月说「1月5日」这种
            d = d.replace(year=d.year + 1)
        return (d - today).days

    if re.search(r"今天|今晚|今日|当天", t):
        d = base
    elif re.search(r"明天|明晚|明日", t):
        d = base + datetime.timedelta(days=1)
    elif "后天" in t:
        d = base + datetime.timedelta(days=2)
    else:
        return None
    return (d - today).days


def finalize(picked, cands=None):
    """用程序算出的天数覆盖模型给的 prio：过期 <0，紧急 <=3 天，其余 normal。"""
    import datetime
    tsmap = {str(c["id"]): c.get("ts") for c in (cands or [])}
    for g in picked:
        base = None
        ts = tsmap.get(str(g.get("id")))
        if ts:
            try:
                base = datetime.date.fromtimestamp(int(ts))
            except (ValueError, OSError, OverflowError):
                base = None
        d = _days_left(g.get("action") or "", base)
        if d is None:
            d = _days_left(g.get("what") or "", base)
        if d is None:
            continue
        g["prio"] = "expired" if d < 0 else ("high" if d <= 3 else "normal")
        g["_days"] = d
    return picked


def pick(cands):
    """模型挑真通知。cands: [{'id','who','text'}]
    模型偶尔偷懒（不给 prio / what 只丢个标题），质量不合格就重来一次。"""
    if not cands:
        return []
    import datetime
    blocks = ["[%s] %s: %s" % (c["id"], c["who"], c["text"][:400]) for c in cands]
    prompt = PROMPT.replace("{today}", datetime.date.today().isoformat()) % "\n\n".join(blocks)
    ids = {c["id"] for c in cands}
    best = []
    for attempt in range(2):
        try:
            got = [g for g in SZ.parse_items(SZ.call_llm(prompt)) if str(g.get("id")) in ids]
        except Exception as e:
            print("模型失败:", e)
            continue
        if not got:
            return []
        bad = sum(1 for g in got
                  if not str(g.get("prio", "")).strip()
                  or len(str(g.get("what", ""))) < 25)
        if bad * 3 <= len(got):          # 劣质条目不到 1/3 就算过
            return got
        print("第 %d 次结果太糊（%d/%d 条缺 prio 或过于短），重来" % (attempt + 1, bad, len(got)))
        best = best or got
    return best


def _md(text):
    return {"tag": "markdown", "content": text}


def _daytag(p):
    d = p.get("_days")
    if not isinstance(d, int):
        return ""
    if d < 0:
        return "已过期 %d 天" % -d
    return "今天截止" if d == 0 else "还有 %d 天" % d


def render(picked, cands, total_new):
    who = {c["id"]: c["who"] for c in cands}

    def key(p):
        return str(p.get("prio", "")).strip().lower()

    hot = [p for p in picked if key(p) == "high"]
    rest = [p for p in picked if key(p) not in ("high", "expired")]
    exp = [p for p in picked if key(p) == "expired"]

    def entry(p):
        """一条的正文：▎标签·发送者 / 内容 / 截止。粗体只留给分区标题，正文不抢眼。"""
        tag = _daytag(p)
        head = "%s · %s" % (tag, who.get(str(p.get("id")), "群里")) if tag else who.get(str(p.get("id")), "群里")
        out = ["▎" + head, str(p.get("what", ""))]
        act = str(p.get("action") or "").strip()
        if act and act != "无":
            out.append("　⏰ " + act)
        return "\n".join(out)

    def block(items):
        """条目之间插分隔线，别糊成一坨。"""
        out = []
        for i, p in enumerate(items):
            if i:
                out.append({"tag": "hr"})
            out.append(_md(entry(p)))
        return out

    els = []
    if hot:
        els.append(_md("**⚡ 3 天内要办**"))
        els += block(hot)
    if rest:
        if els:
            els.append({"tag": "hr"})
        els.append(_md("**⏳ 时间还宽裕**"))
        els += block(rest)
    if exp:                       # 过期的收进折叠面板，别占屏
        if els:
            els.append({"tag": "hr"})
        els.append({"tag": "collapsible_panel", "expanded": False,
                    "header": {"title": {"tag": "markdown",
                                         "content": "已过期 %d 条（点开看）" % len(exp)}},
                    "elements": block(exp)})

    card = {"schema": "2.0",
            "header": {"title": {"tag": "plain_text",
                                 "content": "%s · %d 条通知" % (GROUP, len(picked))},
                       "template": "orange"},
            "body": {"elements": els}}
    plain = []
    for e in els + [x for e in els if e.get("tag") == "collapsible_panel"
                    for x in e.get("elements", [])]:
        if e.get("tag") == "markdown":
            plain.append(e["content"].replace("**", ""))
    return card, "\n".join(plain)


def main():
    dry = "--dry-run" in sys.argv
    force = "--force" in sys.argv

    from wechatauto import WeChatDB
    db = WeChatDB(account=ACCOUNT)
    gid = db.group_name_to_id(GROUP)
    if not gid:
        msg = "微信通知群「%s」找不到了（改名或退群？），班级群监控停摆。" % GROUP
        print(msg)
        if not dry:
            SZ.feishu(msg)
        return 1

    st = json.load(open(STATE, encoding="utf-8")) if os.path.exists(STATE) else {}
    names = member_names(db, gid)

    msgs = db.get_messages(gid, limit=200 if force else 80)
    msgs = [m for m in msgs if m.get("type") in KEEP_TYPES]
    head = max([int(m["local_id"]) for m in msgs], default=0)

    def save(head, **extra):
        st["last_id"] = head
        st.update(extra)
        json.dump(st, open(STATE, "w", encoding="utf-8"), ensure_ascii=False)

    if not st.get("last_id"):
        save(head)
        note = "班级群监控已上线，记住当前最后一条（序号 %d），之后只推新通知。" % head
        print(note)
        if not dry:
            SZ.feishu(note)
        return 0

    fresh = msgs if force else [m for m in msgs if int(m["local_id"]) > int(st["last_id"])]
    cands = []
    for m in fresh:
        text = body(m)
        if len(text) < 8:
            continue
        cands.append({"id": str(m["local_id"]), "who": names.get(m.get("sender_username"), "群里"),
                      "ts": m.get("create_time"), "text": text})
    cands = cands[:60]

    if not cands:
        print("没有新消息")
        return 0

    picked = finalize(pick(cands), cands)
    if not picked:
        print("新消息 %d 条，其中没有真通知" % len(cands))
        if not dry:
            save(head)
        return 0

    card, plain = render(picked, cands, len(cands))
    print(plain)
    if dry:
        print("\n[dry-run] 未推送")
        return 0

    SZ.feishu(card, card=True)

    # 紧急且算得出日期的，顺手在飞书建个待办（需要 task:task:write 权限，没开就跳过）
    made = list(st.get("tasks", []))
    new_tasks = 0
    for p in picked:
        if str(p.get("prio", "")).strip().lower() != "high":
            continue
        d = p.get("_days")
        if not isinstance(d, int) or d < 0 or str(p.get("id")) in made:
            continue
        guid = SZ.create_task(
            str(p.get("what") or "")[:40],
            "%s\n截止：%s\n来自 %s 群" % (p.get("what", ""), p.get("action") or "未写明", GROUP),
            d)
        if guid:
            made.append(str(p.get("id")))
            new_tasks += 1

    save(head, tasks=made)

    # 台账：只把「3 天内要办」的写进去，不然表里全是垃圾
    SZ.ledger_sync([
        {"id": "wx:" + str(p.get("id")), "task": str(p.get("what") or "")[:60],
         "days": p.get("_days"), "source": "班级群"}
        for p in picked if str(p.get("prio", "")).strip().lower() == "high"
    ])
    print("\n已推送 %d 条，新建待办 %d 个" % (len(picked), new_tasks))
    return 0


def _self_test():
    """python wechat.py --self-test —— 只验日期/分档逻辑，不联网不读微信。"""
    import datetime
    today = datetime.date.today()
    old = today - datetime.timedelta(days=3)      # 三天前发的消息

    # 回归：三天前的消息说「明天」，那个明天早就过了，不能算成「还有 1 天」
    assert _days_left("明天下午两点半", old) == -2, "相对时间必须按消息发出日算"
    assert _days_left("今晚 21:00 前", old) == -3
    assert _days_left("后天", old) == -1
    # 不传 base 时退化成按今天算
    assert _days_left("明天下午两点半") == 1
    assert _days_left("今晚 21:00 前") == 0
    assert _days_left("本周五上午9:00") is None, "相对星期不该硬猜"

    # 绝对日期比相对词优先，且不受发送日影响
    fut = today + datetime.timedelta(days=5)
    assert _days_left("%d月%d日截止" % (fut.month, fut.day), old) == 5
    past = today - datetime.timedelta(days=3)
    assert _days_left("%d月%d日截止" % (past.month, past.day)) == -3

    got = finalize([
        {"id": "1", "action": "%d月%d日" % (past.month, past.day), "what": "x", "prio": "high"},
        {"id": "2", "action": "%d月%d日" % (today.month, today.day), "what": "x", "prio": "normal"},
        {"id": "3", "action": "无", "what": "没有日期", "prio": "high"},
        {"id": "4", "action": "明天集合", "what": "x", "prio": "high"},
    ], [{"id": "4", "ts": int(datetime.datetime.combine(old, datetime.time(9)).timestamp())}])
    assert got[0]["prio"] == "expired", got[0]
    assert got[1]["prio"] == "high", got[1]
    assert got[2]["prio"] == "high", "程序算不出来时该保留模型的判断"
    assert got[3]["prio"] == "expired", "三天前的「明天」应判过期，实际 %r" % got[3]

    cands = [{"id": "1", "who": "班长", "text": "t"}]
    card, plain = render(
        [{"id": "1", "what": "同学要交材料", "action": "明天前交", "prio": "high", "_days": 1},
         {"id": "2", "what": "另一条", "action": "无", "prio": "normal"}],
        cands + [{"id": "2", "who": "朋导", "text": "t"}], 2)
    els = card["body"]["elements"]
    assert card.get("schema") == "2.0", "没走卡片 2.0"
    assert els[0]["content"] == "**⚡ 3 天内要办**", "分区标题该加粗：%r" % els[0]["content"]
    body = [e for e in els if e.get("tag") == "markdown" and not e["content"].startswith("**")]
    assert body and not body[0]["content"].startswith("**"), "条目正文不该整句加粗"
    assert body[0]["content"].startswith("▎还有 1 天"), body[0]["content"]
    assert any(e.get("tag") == "hr" for e in els), "条目之间缺分隔线"
    assert "还有 1 天" in plain, plain

    # 过期的必须收进折叠面板，不能平铺占屏
    card2, _ = render(
        [{"id": "1", "what": "旧的", "action": "10月20日", "prio": "expired", "_days": -11}],
        cands, 1)
    assert any(e.get("tag") == "collapsible_panel" for e in card2["body"]["elements"]), "过期没收进折叠"
    print("self-test ok")


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        _self_test()
        sys.exit(0)
    sys.exit(main())
