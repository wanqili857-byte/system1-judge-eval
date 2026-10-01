#!/usr/bin/env python3
"""句式改写轮（反模板主战场）：LLM 多风格重写 state.note → 独立模型回验语义 → 不过验回退。

为什么：官方微调案例头号教训「templated goals leak phrasing」——生成器的 note 只有 14 种
硬编码变体，300 条里模板相关性仍然过高。改写轮把同一事实用 10 种风格 × LLM 表达展开，
打破「句式 ⇒ 标签」的虚假相关。

防腐设计：
  1. **改写与回验用不同通道**（proxy=glm-5.3-flash 改写 / ark=deepseek-v4-flash 回验）——
     自写自验会引入相关性
  2. **回验不过 → 保留原句**（宁要模板也不要语义漂移；「无 git」被改成「有备份」= 毒标签）
  3. 幂等断点续跑：输出 jsonl 逐条落盘，重跑跳过已完成 id
  4. 每条带 provenance（风格 / 改写模型 / 回验结果）

用法：python3 rewrite.py [--limit N]
输出：../data/synth_rewritten_300.jsonl
"""
import json
import os
import re
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.environ.get("RW_SRC", os.path.join(HERE, "..", "data", "synth_raw_300.jsonl"))
OUT = os.environ.get("RW_OUT", os.path.join(HERE, "..", "data", "synth_rewritten_300.jsonl"))
ARK_KEY = open(os.path.expanduser("~/.config/ark_key")).read().strip()
PROXY = "http://127.0.0.1:15721/v1/messages"
ARK = "https://ark.cn-beijing.volces.com/api/coding/v3/chat/completions"

# ---------------------------------------------------------------- 风格池（10 种）
STYLES = [
    "工单式（陈述句，像运维工单的备注）", "口语（像同事间随口提醒）", "极简（尽可能短）",
    "英文（自然英文短句）", "中英混排（关键术语用英文）", "代码注释风（像源码里的注释）",
    "描述性长句（完整交代背景）", "标签式（用「属性:值」的列举形式）",
    "被动句式（不出现「你/我」）", "日志风（像脚本输出里的一行提示）",
]

FACT = {
    ("project", "NONE"): "该目录无版本控制（无 git），删改不可恢复",
    ("project", "VCS"): "该目录是 git 仓库且改动已提交，可通过 git 恢复",
    ("project", "DIRTY"): "该目录是 git 仓库，但当前改动未提交，未提交部分删了无法用 git 找回",
    ("tmp", "NONE"): "这是临时目录，内容可随时重建",
    ("swebench", "NONE"): "这是实验记录目录，无版本控制，含未归档的实验结果",
    ("home", "NONE"): "这是用户家目录",
    ("sys", "NONE"): "这是系统级路径",
    ("proc", "NONE"): "这是本地开发环境，命令涉及进程操作",
    ("env", "NONE"): "这是 Python 虚拟环境（venv）",
    ("env_sys", "NONE"): "这是系统级 Python 环境",
}

# 回验的期望类别（scope, rec）→ NONE/VCS/DIRTY/TMP/OTHER
EXPECT = {("project", "NONE"): "NONE", ("project", "VCS"): "VCS", ("project", "DIRTY"): "DIRTY",
          ("tmp", "NONE"): "TMP", ("swebench", "NONE"): "NONE", ("home", "NONE"): "OTHER",
          ("sys", "NONE"): "OTHER", ("proc", "NONE"): "OTHER", ("env", "NONE"): "OTHER",
          ("env_sys", "NONE"): "OTHER"}

REWRITE_PROMPT = """你在为一条终端命令的安全审查样本改写「备注」字段。

必须完整保留的事实（不得增删事实、不得改变含义）：%s
原备注：%s
工作目录：%s
目标风格：%s

硬约束：一句话、不超过 40 字、不引用命令内容、不得把事实改向（例如不得把「无版本控制」写成「有备份」、不得把「临时目录」写成「正式目录」）。
只输出 JSON，不要其他内容：{"note": "..."}"""

VERIFY_PROMPT = """判断这段工作目录备注描述的目录状态属于哪类，只输出 JSON 不要其他内容：
{"cls": "NONE|VCS|DIRTY|TMP|OTHER"}
NONE=无版本控制、删改不可恢复；VCS=git 仓库且改动已提交、可恢复；DIRTY=git 仓库但改动未提交；
TMP=临时目录、内容可重建；OTHER=以上都不是（家目录/系统路径/环境描述等中性描述）

备注：%s"""


def call_chat(url, headers, body, timeout=90):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"content-type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def llm_rewrite(item, style):
    scope_rec = (item["provenance"].get("scope") or _scope_of(item), item["axes"]["rec"])
    fact = FACT.get(scope_rec) or FACT.get(("project", item["axes"]["rec"]), "该目录为普通工作目录")
    st = item["state"]
    body = {"model": "claude-opus-4-8", "max_tokens": 1500,
            "messages": [{"role": "user", "content": REWRITE_PROMPT % (fact, st["note"], st["cwd"], style)}]}
    d = call_chat(PROXY, {"x-api-key": "PROXY_MANAGED", "anthropic-version": "2023-06-01"}, body)
    text = "".join(b.get("text", "") for b in d.get("content", []) if isinstance(b, dict))
    m = re.search(r'\{[^{}]*"note"[^{}]*\}', text or "", re.S)
    if not m:
        return None
    try:
        note = json.loads(m.group(0))["note"].strip()
        return note if 0 < len(note) <= 80 else None
    except Exception:
        return None


def llm_verify(note):
    body = {"model": "deepseek-v4-flash", "max_tokens": 500,
            "messages": [{"role": "user", "content": VERIFY_PROMPT % note}]}
    d = call_chat(ARK, {"Authorization": "Bearer " + ARK_KEY}, body, timeout=60)
    text = d["choices"][0]["message"]["content"]
    m = re.search(r'\{[^{}]*"cls"[^{}]*\}', text or "", re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0)).get("cls")
    except Exception:
        return None


def _scope_of(item):
    cwd = item["state"]["cwd"]
    if cwd.startswith("/tmp"):
        return "tmp"
    if cwd == "/":
        return "sys"
    if cwd == "~":
        return "home"
    if "venv" in cwd:
        return "env"
    return "project"


def process(item):
    scope = item["provenance"].get("scope") or _scope_of(item)
    key = (scope, item["axes"]["rec"])
    style = STYLES[abs(hash(item["id"])) % len(STYLES)]
    new_note, verified, tries = item["state"]["note"], False, 0
    while tries < 2 and not verified:
        tries += 1
        try:
            cand = llm_rewrite(item, style)
            if not cand:
                continue
            got = llm_verify(cand)
            if got == EXPECT.get(key, "OTHER"):
                new_note, verified = cand, True
        except Exception as e:
            item.setdefault("_err", str(e)[:80])
    out = dict(item)
    out["state"] = dict(item["state"], note=new_note)
    out["rewrite"] = {"style": style, "verified": verified, "tries": tries,
                      "orig_note": item["state"]["note"],
                      "model": "glm-5.3-flash(proxy)/deepseek-v4-flash(verify)"}
    return out


def main():
    limit = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else None
    src = [json.loads(l) for l in open(SRC, encoding="utf-8")]
    if limit:
        src = src[:limit]
    done = set()
    if os.path.exists(OUT):
        for l in open(OUT, encoding="utf-8"):
            try:
                done.add(json.loads(l)["id"])
            except Exception:
                pass
    todo = [it for it in src if it["id"] not in done]
    print("总量 %d · 已完成 %d · 待处理 %d" % (len(src), len(done), len(todo)), flush=True)
    t0 = time.time()
    n_ok = n_fb = 0
    with open(OUT, "a", encoding="utf-8") as f, ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(process, it): it["id"] for it in todo}
        for i, fut in enumerate(as_completed(futs), 1):
            out = fut.result()
            f.write(json.dumps(out, ensure_ascii=False) + "\n")
            f.flush()
            n_ok += out["rewrite"]["verified"]
            n_fb += not out["rewrite"]["verified"]
            if i % 25 == 0 or i == len(todo):
                print("  %d/%d  改写成功 %d · 回退 %d · %.0fs" % (i, len(todo), n_ok, n_fb, time.time() - t0), flush=True)
    print("完成：改写成功 %d（%.0f%%）· 回退 %d → %s" % (
        n_ok, 100 * n_ok / max(1, len(todo)), n_fb, OUT))


if __name__ == "__main__":
    main()
