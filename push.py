#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""网页通知源（VSB 站群）-> 模型摘要 -> 飞书卡片 / 多维表格台账。

用法:
  python push.py            正常跑（有新才推）
  python push.py --dry-run  只打印不推送
  python push.py --force    忽略去重，把当前列表前 8 条当新推（测试用）
  python push.py --self-test  只验渲染/日期逻辑，不联网

配置全在 config.py（从 config.example.py 复制后填）。
"""
import json
import os
import re
import sys
import urllib.error
import urllib.request

import config

DIR = os.path.dirname(os.path.abspath(__file__))
SEEN = os.path.join(DIR, "seen.json")
LIST_URL = config.WEB["list_url"]
BASE = config.WEB["base"]
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
COL = config.WEB["columns"]
DETAIL_COLS = tuple(config.WEB.get("detail_columns") or ())
AUDIENCE = config.WEB.get("audience", "学生")


def get(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


def parse_list(html):
    """列表页 -> [{'aid','col','title','date','url'}]，按出现顺序（新在前）。"""
    items = []
    for chunk in re.split(r'(?=<a href="info/)', html)[1:]:
        m = re.match(r'<a href="info/(\d+)/(\d+)\.htm"[^>]*title="([^"]*)"', chunk)
        if not m:
            continue
        col, aid, title = m.group(1), m.group(2), m.group(3).strip()
        d = re.search(r"(\d{4}-\d{2}-\d{2})", chunk[:900])
        items.append({
            "aid": aid, "col": col, "title": title,
            "date": d.group(1) if d else "",
            "url": "%sinfo/%s/%s.htm" % (BASE, col, aid),
        })
    return items


def clean(s):
    s = re.sub(r"<[^>]+>", " ", s)
    s = s.replace("&nbsp;", " ").replace("&amp;", "&").replace("&ldquo;", "“")
    s = s.replace("&rdquo;", "”").replace("&mdash;", "—").replace("&quot;", '"')
    return " ".join(s.split())


def fetch_article(it, limit=1400):
    """取正文前 limit 字（通知的关键信息都在开头）。"""
    try:
        html = get(it["url"])
    except Exception as e:
        return "", ""
    dept = re.search(r"作者：\s*([^<\s]+)", html)
    body = ""
    m = re.search(r'class="[^"]*v_news_content[^"]*"[^>]*>', html)
    if m:
        body = clean(html[m.end():m.end() + limit * 6])[:limit]
    else:
        body = clean(html)[-limit:]
    return (dept.group(1) if dept else ""), body


def call_glm(prompt, model="glm-4-flash"):
    key = config.LLM["glm"]["api_key"]
    if not key:
        raise RuntimeError("GLM_API_KEY missing")
    body = {"model": model, "temperature": 0.3, "max_tokens": 3000,
            "messages": [{"role": "user", "content": prompt}]}
    req = urllib.request.Request(
        config.LLM["glm"]["base_url"].rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        d = json.loads(r.read().decode())
    return d["choices"][0]["message"]["content"] or ""


def call_ark(prompt):
    key = config.LLM["ark"]["api_key"]
    if not key:
        raise RuntimeError("ARK_API_KEY missing")
    data = json.dumps({"model": config.LLM["ark"]["model"], "input": prompt,
                       "reasoning": {"effort": "low"}}).encode()
    req = urllib.request.Request(
        config.LLM["ark"]["base_url"].rstrip("/") + "/responses", data=data,
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        resp = json.loads(r.read().decode())
    parts = []
    for o in resp.get("output", []):
        if o.get("type") == "message":
            for c in o.get("content", []):
                if c.get("type") in ("output_text", "text"):
                    parts.append(c.get("text", ""))
    return "".join(parts)


def call_llm(prompt):
    """glm-4.6 主力（大白话质量最好，一次几分钱），免费档 glm-4-flash 兜底，方舟最后。
    方舟不续费也不影响这套。"""
    tries = [(config.LLM["glm"]["models"][0], lambda p: call_glm(p, config.LLM["glm"]["models"][0])),
             (config.LLM["glm"]["models"][-1], lambda p: call_glm(p, config.LLM["glm"]["models"][-1])),
             ("ark", call_ark)]
    errs = []
    for name, fn in tries:
        try:
            out = fn(prompt)
            if out.strip():
                return out
            errs.append(name + ": empty")
        except Exception as e:
            errs.append("%s: %s" % (name, str(e)[:120]))
    print("模型全挂:", errs)
    raise RuntimeError("no model available")


PROMPT = """你在给一位{audience}转告校园通知。下面是本校的几则通知，逐条输出：

what：这件事到底是什么，**80~100 字**，大白话，像同学转告那样把要点讲清楚 —— 办什么事、谁要办、什么时间、在哪办/找谁。别复述公文腔，别只把标题换个说法。
action：有没有时间节点或要他本人办的事（截止日期、必须提交、必须参加、报名）。有就写成「截止 10月31日 前提交材料」这种；对象是研究生/教师/科研申报、跟这个大一生无关的，一律写「与你无关」。
prio：四个字段里这个最容易漏，必须给。他该不该立刻留意 —— 竞赛/比赛/科创项目/奖学金评优/实习就业/研学交换这类**他本人能报名参加的机会**，或他必须完成的硬任务（必修网课、必须到的会、有截止要本人交的材料），写 high；其余一律 normal。老师科研申报、行政采购、教职工培训这种不是给他的一律 normal，哪怕标题里有「科研」「竞赛」字样。

注意：what 和 action 两个字段都必须有值，不许省略、不许留空。action 尤其不能因为「找不到日期」就整段删掉。

严格输出 JSON 数组，不要解释、不要 markdown 代码块：
[{"id":"编号","what":"...","action":"...","prio":"high 或 normal"}]

通知：
%s"""


def parse_items(raw):
    """模型有时给一个大数组，有时每行一个数组/对象 —— 都吃下。"""
    try:
        v = json.loads(raw.strip())
        return v if isinstance(v, list) else [v]
    except Exception:
        pass
    out = []
    for m in re.finditer(r"\{[^{}]*\}", raw, re.S):
        try:
            o = json.loads(m.group(0))
            if "id" in o:
                out.append(o)
        except Exception:
            continue
    return out


def summarize(new):
    blocks = []
    for it in new:
        dept, body = fetch_article(it)
        it["dept"], it["body"] = dept, body
        blocks.append("[%s] 栏目:%s 日期:%s 标题:%s\n单位:%s\n正文:%s" % (
            it["aid"], COL.get(it["col"], "其他"), it["date"], it["title"], dept, body))
    try:
        got = parse_items(call_llm(PROMPT % "\n\n".join(blocks)))
        return {str(g.get("id")): g for g in got}
    except Exception as e:
        print("总结失败，退化为只发标题:", e)
        return {}


def _md(text):
    return {"tag": "markdown", "content": text}


def _entry(it, sums):
    """一条：▎标题 / 摘要 / 截止 / 链接。不加粗整句，粗体留给分区标题。"""
    s = sums.get(it["aid"], {})
    out = ["▎" + it["title"]]
    if it["col"] in DETAIL_COLS and s.get("what"):
        out.append(s["what"])
        act = (s.get("action") or "").strip()
        if act and act not in ("与你无关", "无"):
            out.append("　⏰ " + act)
    out.append("　[原文](%s)" % it["url"])
    return "\n".join(out)


def render(new, sums, total_new):
    """返回 (飞书卡片 dict, 纯文本预览)。重点置顶，粗体只给分区标题。
    用卡片 JSON 2.0：body.elements + markdown 组件。"""
    hot = [it for it in new if str(sums.get(it["aid"], {}).get("prio", "")).lower() == "high"]
    rest = [it for it in new if it not in hot]

    def block(items):
        out = []
        for i, it in enumerate(items):
            if i:
                out.append({"tag": "hr"})
            out.append(_md(_entry(it, sums)))
        return out

    els = []
    if hot:
        els.append(_md("**⚡ 重点**"))
        els += block(hot)
    for col in ("1018", "1021", "1020", "1019"):
        group = [it for it in rest if it["col"] == col]
        if not group:
            continue
        if els:
            els.append({"tag": "hr"})
        els.append(_md("**%s · %d 条**" % (COL.get(col, "其他"), len(group))))
        els += block(group)

    card = {"schema": "2.0",
            "header": {"title": {"tag": "plain_text",
                                 "content": "深技大通知 · 新增 %d 条" % total_new},
                       "template": "blue"},
            "body": {"elements": els}}
    plain = []
    for e in els:
        if e.get("tag") == "markdown":
            plain.append(re.sub(r"\[原文\]\((\S+)\)", r"\1", e["content"]).replace("**", ""))
    return card, "\n".join(plain)


def _days_left(text):
    """从『截止 10月31日』这类文本算还剩几天，None=看不出。学校通知都有绝对日期。"""
    import datetime
    today = datetime.date.today()
    t = str(text)
    m = re.search(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]", t)
    if m:
        try:
            d = datetime.date(today.year, int(m.group(1)), int(m.group(2)))
        except ValueError:
            return None
        if (today - d).days > 60:      # 跨年
            d = d.replace(year=d.year + 1)
        return (d - today).days
    # 「本周五」「周五」「星期五」→ 本周的那天（周一为一周起点）
    w = re.search(r"(?:本周|周|星期)([一二三四五六日天])", t)
    if w:
        wd = "一二三四五六日".find(w.group(1).replace("天", "日"))
        if wd >= 0:
            monday = today - datetime.timedelta(days=today.weekday())
            return (monday + datetime.timedelta(days=wd) - today).days
    if re.search(r"今天|今日|当天", t):
        return 0
    if re.search(r"明天|明日", t):
        return 1
    return None


BITABLE = config.BITABLE


def _bt(method, path, body=None):
    import urllib.error
    req = urllib.request.Request(
        "https://open.feishu.cn/open-apis" + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": "Bearer " + _token(), "Content-Type": "application/json"},
        method=method)
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            return 0, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:250]
    except Exception as e:
        return "ERR", str(e)[:150]


def _plain(v):
    """飞书文本字段可能回 str，也可能回 [{'text':...}]。"""
    if isinstance(v, list):
        return "".join(x.get("text", "") for x in v if isinstance(x, dict))
    return v or ""


def ledger_sync(rows, prune_days=7):
    """把通知写进多维表格台账，顺手收拾过期的。
    rows: [{'id': 唯一键, 'task': 文本, 'days': 剩余天数(None=没日期), 'source': '班级群'/'学校'}]
    规则：已存在（按来源ID）不重复插；截止过了 → 标「已过期」；过期超 prune_days 天直接删。
    """
    import datetime
    today = datetime.date.today()
    base = "/bitable/v1/apps/%s/tables/%s" % (BITABLE["app"], BITABLE["table"])
    st, d = _bt("GET", base + "/records?page_size=500")
    if st != 0:
        print("台账读取失败:", d)
        return None
    items = d.get("data", {}).get("items", [])
    seen, seen_task, upd, dele = set(), set(), [], []
    for it in items:
        f, rid = it.get("fields", {}), it["record_id"]
        key = _plain(f.get("来源ID"))
        if key:
            seen.add(key)
        # 同一件事可能被两个人发两遍（消息 id 不同）→ 按正文前 30 字再挡一道
        seen_task.add(_plain(f.get("任务"))[:30])
        due = f.get("截止")
        if not isinstance(due, (int, float)) or f.get("状态") == "已完成":
            continue
        left = (datetime.date.fromtimestamp(due / 1000) - today).days
        if left < -prune_days:
            dele.append(rid)
        elif left < 0 and f.get("截止日期") != "已过期":
            upd.append({"record_id": rid, "fields": {"截止日期": "已过期"}})

    new = []
    for r in rows:
        if not r.get("id") or r["id"] in seen:
            continue
        task = str(r.get("task") or "")
        if task[:30] in seen_task:
            continue
        seen_task.add(task[:30])
        days = r.get("days")
        flds = {"任务": "%s〔%s〕" % (task, r.get("source", "")),
                "状态": "待处理",
                "重要紧急程度": "重要且紧急" if isinstance(days, int) and days <= 3 else "重要不紧急",
                "来源ID": r["id"]}
        if isinstance(days, int):
            d0 = today + datetime.timedelta(days=days)
            flds["截止"] = int(datetime.datetime.combine(d0, datetime.time(23, 59)).timestamp() * 1000)
            if days < 0:
                flds["截止日期"] = "已过期"
            else:
                flds["截止日期"] = "🔥今日" if days == 0 else ("本周内" if days <= 7 else "以后")
        else:
            flds["截止日期"] = "时间未定"     # 原文没写日期，不猜，单独标出来
        new.append({"fields": flds})

    if new:
        _bt("POST", base + "/records/batch_create", {"records": new})
    if upd:
        _bt("POST", base + "/records/batch_update", {"records": upd})
    if dele:
        _bt("POST", base + "/records/batch_delete", {"records": dele})
    res = {"new": len(new), "expired": len(upd), "pruned": len(dele)}
    print("台账：新增 %(new)d 行，标过期 %(expired)d 行，清掉 %(pruned)d 行" % res)
    return res


def _token():
    def post(url, data, hdr=None):
        req = urllib.request.Request(url, data=json.dumps(data).encode(),
                                     headers={"Content-Type": "application/json", **(hdr or {})})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    tok = post("https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
               {"app_id": config.FEISHU["app_id"], "app_secret": config.FEISHU["app_secret"]})
    return tok["tenant_access_token"]


def create_task(summary, desc, due_days):
    """在飞书建一个待办任务，due_days = 还剩几天（定在当天 23:59）。
    需要应用开通 task:task:write 权限，没开就返回 None 不炸主流程。"""
    import datetime
    import urllib.error
    ou = (config.FEISHU.get("user_open_id") or "").split(",")[0].strip()
    if not ou:
        return None
    d = datetime.date.today() + datetime.timedelta(days=max(int(due_days), 0))
    ts = str(int(datetime.datetime.combine(d, datetime.time(23, 59)).timestamp() * 1000))
    body = {"summary": summary[:200], "description": desc[:1200],
            "due": {"timestamp": ts, "is_all_day": False},
            "members": [{"id": ou, "role": "assignee"}]}
    req = urllib.request.Request(
        "https://open.feishu.cn/open-apis/task/v2/tasks", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + _token()})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            res = json.loads(r.read().decode())
        return res.get("data", {}).get("task", {}).get("guid")
    except urllib.error.HTTPError as e:
        print("建任务失败(权限?):", e.read().decode()[:200])
        return None
    except Exception as e:
        print("建任务失败:", e)
        return None


def feishu(content, chat_id=None, card=False):
    """card=True 时 content 是卡片 dict（能加粗/带链接），否则是纯文本。"""
    chat_id = chat_id or config.FEISHU["chat_id"]
    def post(url, data, hdr=None):
        req = urllib.request.Request(url, data=json.dumps(data).encode(),
                                     headers={"Content-Type": "application/json", **(hdr or {})})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    tok = post("https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
               {"app_id": config.FEISHU["app_id"], "app_secret": config.FEISHU["app_secret"]})
    at = tok["tenant_access_token"]
    if card:
        body = {"receive_id": chat_id, "msg_type": "interactive",
                "content": json.dumps(content, ensure_ascii=False)}
    else:
        body = {"receive_id": chat_id, "msg_type": "text",
                "content": json.dumps({"text": content}, ensure_ascii=False)}
    res = post("https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
               body, {"Authorization": "Bearer " + at})
    if res.get("code") != 0:
        raise RuntimeError("飞书发送失败: %s" % res)
    return res["data"]["message_id"]


def main():
    dry = "--dry-run" in sys.argv
    force = "--force" in sys.argv
    try:
        items = parse_list(get(LIST_URL))
    except Exception as e:
        items = []
        print("抓列表异常:", e)
    if not items:
        msg = "学校通知监控抓取失败：nbw 列表页解析为空。可能是站点改版或校园网断了，需要看一眼。"
        print(msg)
        if not dry:
            feishu(msg)
        return 1

    seen = {}
    if os.path.exists(SEEN):
        seen = json.load(open(SEEN, encoding="utf-8"))
    first_run = not seen

    new = items[:8] if force else [it for it in items if it["aid"] not in seen]

    if first_run and not force:
        json.dump({it["aid"]: it["title"] for it in items}, open(SEEN, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        msg = "学校通知监控已上线。已记住当前 %d 条，之后每天 08:00 / 21:00 有新通知才推给你。" % len(items)
        print(msg)
        if not dry:
            feishu(msg)
        return 0

    if not new:
        print("无新增通知")
        return 0

    sums = summarize(new)
    card, plain = render(new, sums, len(new))
    print(plain)

    if dry:
        print("\n[dry-run] 未推送")
        return 0

    feishu(card, card=True)

    # 台账：重点条目写进多维表格
    ledger_sync([{"id": "sz:" + it["aid"], "task": it["title"][:60],
                  "days": _days_left((sums.get(it["aid"]) or {}).get("action") or ""),
                  "source": "学校"}
                 for it in new
                 if str((sums.get(it["aid"]) or {}).get("prio", "")).lower() == "high"])

    for it in items:
        seen.setdefault(it["aid"], it["title"])
    json.dump(seen, open(SEEN, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("\n已推送 %d 条" % len(new))
    return 0


def _self_test():
    """python push.py --self-test —— 只验渲染分支，不联网。"""
    a = {"aid": "1", "col": "1018", "title": "T1", "url": "u1"}
    b = {"aid": "2", "col": "1019", "title": "T2", "url": "u2"}
    c = {"aid": "3", "col": "1020", "title": "T3", "url": "u3"}
    sums = {"1": {"what": "w1", "action": "截止 11月1日", "prio": "high"},
            "2": {"what": "w2", "action": "与你无关"},
            "3": {}}
    card, plain = render([a, b, c], sums, 3)
    els = card["body"]["elements"]
    md = [e for e in els if e.get("tag") == "markdown"]
    assert card.get("schema") == "2.0", "没走卡片 2.0"
    assert md[0]["content"] == "**⚡ 重点**", "重点分区标题该加粗：%r" % md[0]["content"]
    body = [e for e in md if not e["content"].startswith("**")]
    assert body and body[0]["content"].startswith("▎T1"), "条目该用 ▎ 且不整句加粗"
    assert any(e.get("tag") == "hr" for e in els), "条目之间缺分隔线"
    assert "与你无关" not in plain, "无关的 action 漏到正文里了"
    assert "u1" in plain and "u3" in plain, "链接丢了"
    assert "**" not in plain, "纯文本预览里残留 markdown"
    print("self-test ok")


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        _self_test()
        sys.exit(0)
    sys.exit(main())
