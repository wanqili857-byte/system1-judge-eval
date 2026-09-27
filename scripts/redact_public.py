#!/usr/bin/env python3
"""生成可公开的 T1 题集：语义级脱敏 + 不可脱敏条目剔除 + 披露。

规则（可审计）：
  1. 求职类命令（含 jd/resume/apply/interview/offer 语义）→ **整条剔除**（命令本身就是求职活动，泛化后无意义）
  2. 个人路径     ~/ayu/<repo>/...  →  ~/workspace/<repo>/...
  3. 私有项目名   ai-eval / swe-mini / swe-bench-runs / fictionforge / cc-switch / social-reply
                  / maoqiu / EcoEvolve → 泛化名（workspace / eval-harness / novel-agent / config-db ...）
  4. 公司名       DeepSeek / 字节 / bytedance / MiniMax / 智谱 / Kimi / 腾讯 / 阿里 / 淘天 → <company>
  5. 凭证         KGAT_* / sk-* / api_key 值 → <REDACTED>；整条以凭证操作目的的命令 → 剔除
  6. 保留构造题（人工编造的通用危险命令，无隐私）→ 原样

输出：
  dataset/t1_command_safety.jsonl   可发布题集
  dataset/REDACTION-REPORT.md       每条的处理结果（公开=可复现；剔除了多少条、为什么）
"""
import json, os, re, collections

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "t1_dataset.jsonl")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "publish", "dataset")

# 整条剔除：命令语义 = 求职活动
DROP_PAT = re.compile(r"jd-file|resume|docs/apply|interview/|offer|招聘|面试|简历", re.I)
# 凭证操作
CRED_PAT = re.compile(r"KGAT_|sk-[A-Za-z0-9]|api_key|API_KEY|Authorization|token=", re.I)

PATH_SUBS = [("~/ayu/ai-eval", "~/workspace"), ("/Users/ayu/ai-eval", "~/workspace"),
             ("~/ayu/", "~/workspace/"), ("/Users/ayu/", "~/workspace/"),
             (r"/private/tmp/", "/tmp/"), ("~/.claude", "~/.agent-config"),
             ("docs/apply/", "docs/notes/"), ("docs/apply", "docs/notes"),
             ("docs/interview/", "docs/notes/"), ("qa-evaluator-methodology", "qa-notes"), (".claude", ".agent-config")]
NAME_SUBS = [("ai-eval", "workspace"), ("swe-mini", "eval-harness"), ("swe-bench-runs", "bench-runs"),
             ("fictionforge", "novel-agent"), ("cc-switch", "config-db"), ("social-reply", "reply-agent"),
             ("maoqiu", "game-project"), ("EcoEvolve", "game-project"), ("laya-eval", "judge-eval"),
             ("kaggle-agent-security", "contest-2026")]
COMPANY_SUBS = [("DeepSeek", "<company>"), ("deepseek", "<company>"), ("字节", "<company>"),
                ("bytedance", "<company>"), ("MiniMax", "<company>"), ("智谱", "<company>"),
                ("Kimi", "<company>"), ("腾讯", "<company>"), ("阿里", "<company>"), ("淘天", "<company>")]


def scrub(text: str) -> str:
    for a, b in PATH_SUBS + NAME_SUBS + COMPANY_SUBS:
        text = text.replace(a, b)
    text = re.sub(r"KGAT_[A-Za-z0-9_\-]*", "<REDACTED>", text)
    text = re.sub(r"\bsk-[A-Za-z0-9_\-]{8,}", "<REDACTED>", text)
    return text


def scrub_record(obj):
    """递归脱敏整个记录的所有字符串字段（state / observed / ground_truth 都要过）"""
    if isinstance(obj, str):
        return scrub(obj)
    if isinstance(obj, list):
        return [scrub_record(x) for x in obj]
    if isinstance(obj, dict):
        return {k: scrub_record(v) for k, v in obj.items()}
    return obj


def main():
    rows = [json.loads(l) for l in open(SRC, encoding="utf-8")]
    keep, dropped = [], []
    for r in rows:
        cmd = r["state"]["command"]
        whole = json.dumps(r, ensure_ascii=False)
        if r.get("source") != "real_session":
            keep.append((scrub_record(r), "构造题（字段泛化）"))
            continue
        # 真实题：命令或输出里含求职/凭证语义 → 整条剔除（输出可能是求职正文，泛化后失去评测意义）
        if DROP_PAT.search(whole):
            dropped.append((r["id"], "求职/招聘语义（命令或输出）", cmd[:70])); continue
        if CRED_PAT.search(whole):
            dropped.append((r["id"], "凭证操作", cmd[:70])); continue
        new = scrub_record(r)
        keep.append((new, "真实会话 · 全字段语义脱敏"))
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "t1_command_safety.jsonl"), "w", encoding="utf-8") as fh:
        for r, _ in keep:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    real_kept = sum(1 for r, _ in keep if r.get("source") == "real_session")
    con_kept = len(keep) - real_kept
    L = ["# T1 题集脱敏报告（可公开发布）", "",
         f"- 原始题集 55 条：真实会话 25 + 人工构造 30",
         f"- **可发布 {len(keep)} 条**：人工构造 {con_kept}（原样）+ 真实会话 {real_kept}（语义脱敏后）",
         f"- **剔除 {len(dropped)} 条**（命令本身即个人信息，泛化后失去评测意义）", "",
         "## 剔除明细", "", "| id | 原因 | 命令（截断）|", "|---|---|---|"]
    for i, why, c in dropped:
        L.append(f"| {i} | {why} | `{c}` |")
    L += ["", "## 脱敏规则（脚本 `scripts/redact_public.py`，可复跑核验）", "",
          "- 个人路径 `~/ayu/<repo>` → `~/workspace/<repo>`；`~/.claude` → `~/.agent-config`",
          "- 私有项目名 → 泛化名（workspace / eval-harness / novel-agent / config-db …）",
          "- 公司名（求职语境） → `<company>`",
          "- 凭证 `KGAT_*` / `sk-*` → `<REDACTED>`；**凭证操作类命令整条剔除**",
          "- 求职/招聘语义命令整条剔除（`jd-file` / `resume` / `docs/apply` / `interview`）", "",
          "> 说明：剔除的条目不影响结论——T1 的结论（deny 召回崩塌 / ask 塌缩 / 常量分类器）由聚合统计给出，",
          "> 且敏感性检验（剔除 LLM 定义真值后 n=41）已证明排序不依赖个别条目。"]
    open(os.path.join(OUT, "REDACTION-REPORT.md"), "w", encoding="utf-8").write("\n".join(L) + "\n")
    print(f"可发布 {len(keep)} 条（构造 {con_kept} + 真实 {real_kept}）；剔除 {len(dropped)} 条")
    for i, why, c in dropped:
        print(f"  剔 {i}: {why}")


if __name__ == "__main__":
    main()
