#!/usr/bin/env python3
"""E-rule 基线：用 T1 那套正则规则直接判（用户质疑的检验——「规则这么清楚，还要微调吗？」）。

规则来自 build_t1.py 的 DANGER_PATTERNS（当年真实使用的版本），取**命中里严重度最高**的那条。
跑完与 E0（Laya multilingual）同口径对比：若规则拿高分，说明任务被口径塌缩了；若规则在
ask/deny 边界上崩，说明规则不足、微调有意义。

用法：python3 rule_baseline.py
输出：../data/pred_rule.jsonl
"""
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "..", "data", "t5_heldout.jsonl")
OUT = os.path.join(HERE, "..", "data", "pred_rule.jsonl")

RULES = [
    (r"\brm\s+-[rf]{1,2}\b", "deny", 3), (r"\brm\b", "ask", 2), (r"\bmv\b", "ask", 2),
    (r"\bsed\s+-i\b", "ask", 2), (r">\s*\S+", "ask", 2),
    (r"\bcurl\b.*(-X\s*(POST|PUT|DELETE)|--data|-d\s)", "ask", 2),
    (r"\bchmod\s+777\b", "deny", 3), (r"\bgit\s+push\b|--force", "deny", 3),
    (r"\b(kill|pkill)\b", "ask", 2), (r"\bdd\b", "deny", 3), (r"\bsudo\b", "deny", 3),
    (r"\bfind\b.*-delete", "deny", 3), (r"\btruncate\b|\b:\s*>\s*\S+", "deny", 3),
    (r"\bpip\s+install\b|\buv\s+pip\s+install\b", "ask", 2), (r"\bmkdir\b", "allow", 1),
    (r"\b(ls|cat|head|tail|grep|find|wc|du|df|pwd|which|type)\b", "allow", 1),
    (r"\bpython3?\b.*-c\b", "allow", 1), (r"\becho\b", "allow", 1),
    (r"\bexport\b|\benv\b", "allow", 1),
]


def judge(cmd):
    best = ("allow", 0)
    for pat, verdict, sev in RULES:
        if re.search(pat, cmd) and sev > best[1]:
            best = (verdict, sev)
    return best[0]


def main():
    rows = []
    for line in open(DATA, encoding="utf-8"):
        it = json.loads(line)
        st = json.loads(it["state"])
        rows.append({"id": it["id"], "pred": judge(st["command"]), "conf": None, "ms": 0})
    with open(OUT, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print("写出 →", OUT)

    import subprocess
    subprocess.run([os.sys.executable, os.path.join(HERE, "eval_tolerant.py"),
                    "--data", DATA, "--pred", OUT, "--title", "E-rule 正则规则（T1 那套）"])


if __name__ == "__main__":
    main()
