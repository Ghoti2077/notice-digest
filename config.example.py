# -*- coding: utf-8 -*-
"""复制成 config.py 填自己的；config.py 已在 .gitignore 里，不会被提交。"""

# ---------- 飞书自建应用 ----------
FEISHU = {
    "app_id": "cli_xxxxxxxxxxxxxxxx",
    "app_secret": "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
    "chat_id": "oc_xxxxxxxxxxxxxxxxxxxxxxxxxxxx",   # 消息发到哪个会话
    "user_open_id": "ou_xxxxxxxxxxxxxxxxxxxxxxxxxxxx",  # 建待办时指派给谁
}

# ---------- 模型（按顺序降级，前一个挂了自动换下一个）----------
LLM = {
    "glm": {
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "api_key": "",
        "models": ["glm-4.6", "glm-4-flash"],
    },
    "ark": {                                        # 可选，没有就留空
        "base_url": "https://ark.cn-beijing.volces.com/api/plan/v3",
        "api_key": "",
        "model": "ark-code-latest",
    },
}

# ---------- 网页源：VSB 站群 ----------
WEB = {
    "list_url": "https://example.edu.cn/list.jsp?urltype=tree.TreeTempUrl&wbtreeid=1029",
    "base": "https://example.edu.cn/",
    # 栏目 id 从文章 URL 里认：info/<栏目id>/<文章id>.htm
    "columns": {"1018": "教务", "1019": "科研", "1020": "行政", "1021": "学工"},
    "detail_columns": ["1018", "1021"],   # 这几栏写摘要，其余只给标题+链接
    "audience": "某大学 2026 级大一本科生",
}

# ---------- 微信群源 ----------
WECHAT = {
    "group": "某某班通知群",
    # 微信数据目录名，形如 wxid_xxxx_1234；用 list_accounts() 查
    "account": "wxid_xxxxxxxxxxxx_0000",
    "audience": "某大学某专业大一学生",
}

# ---------- 飞书多维表格台账（可选，app 留空就不写）----------
# 需要应用有 bitable:app 权限，且是这张表的「可编辑」协作者
BITABLE = {"app": "", "table": ""}

# ---------- 运行目录（state.json / seen.json 存这）----------
STATE_DIR = "."
