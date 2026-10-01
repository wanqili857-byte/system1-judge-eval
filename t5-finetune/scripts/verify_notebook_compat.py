#!/usr/bin/env python3
"""notebook 兼容性验证：用**官方 build_training_item 逻辑**跑我们的 train/held-out jsonl。

验四件事（任一不过都不要上 Kaggle 烧 GPU）：
  1. 字段路径对得上（state/questions/gold 都是 json 字符串，questions 用 type/instructions/criteria）
  2. `gold.probabilities` 是**按选项名的 dict**（官方用 .get(k) 取，list 会静默打成全零）
  3. tokenize 后 marker 数 == 选项数（官方 `if len(markers) != k: return None` 会整条丢弃）
  4. target 归一化后非全零、和为 1

用法：~/.local/share/laya-venv/bin/python verify_notebook_compat.py
"""
import json
import os
import sys
import collections
import glob

from huggingface_hub import snapshot_download
from transformers import AutoTokenizer
from laya.agent import _fix_tokenizer_config
from laya.common import build_sequence, render_options, QTYPES

MODEL_ID = "convaiinnovations/laya"
DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")


def build_training_item(tok, cfg, state, q, gold_q):
    """逐字照抄官方 notebook cell 6 的实现"""
    t = q["type"]
    crit = q.get("criteria", {})
    if t == "choice":
        keys = list(crit.keys())
        target = [gold_q["probabilities"].get(k, 0.0) for k in keys]
    elif t == "noul":
        target = [gold_q["probabilities"].get("false", 0.5), gold_q["probabilities"].get("true", 0.5)]
    elif t == "score":
        n_levels = len(crit) if isinstance(crit, list) else 4
        target = [gold_q["probabilities"].get(str(i), 0.0) for i in range(n_levels)]
    else:
        return None, "unknown type"
    s = sum(target)
    if s <= 0:
        return None, "target 全零（probabilities 键名不匹配）"
    target = [v / s for v in target]
    label = target.index(max(target))
    k = len(render_options({"t": t, "crit": crit}))
    seq, markers = build_sequence(tok, state, {"t": t, "ins": q["instructions"], "crit": crit},
                                  cfg["max_len"], cfg["head_max_len"])
    if len(markers) != k:
        return None, "marker 数 %d != 选项数 %d" % (len(markers), k)
    return {"ids": seq, "markers": markers, "qtype": QTYPES[t], "target": target, "label": label}, None


def main():
    model_dir = snapshot_download(MODEL_ID)
    _fix_tokenizer_config(model_dir)
    sub = os.path.join(model_dir, "multilingual")            # 域内微调起点 = multilingual 权重
    tok = AutoTokenizer.from_pretrained(os.path.join(sub, "tokenizer"))
    cfg = json.load(open(os.path.join(sub, "rl_agent_config.json")))
    print("模型目录:", sub, "| max_len=%d head_max_len=%d" % (cfg["max_len"], cfg["head_max_len"]))

    for name in ("t5_train.jsonl", "t5_heldout.jsonl"):
        p = os.path.join(DATA, name)
        if not os.path.exists(p):
            print("  %s: 缺文件" % name)
            continue
        items, errs, lens, lab = [], collections.Counter(), [], collections.Counter()
        for line in open(p, encoding="utf-8"):
            row = json.loads(line)
            state = json.loads(row["state"])
            questions = json.loads(row["questions"])
            gold = json.loads(row["gold"])
            for qid, q in questions.items():
                if qid in gold:
                    it, err = build_training_item(tok, cfg, state, q, gold[qid])
                    if it is None:
                        errs[err] += 1
                    else:
                        items.append(it)
                        lens.append(len(it["ids"]))
                        lab[["allow", "ask", "deny"][it["label"]]] += 1
        print("\n%s: 行 %d → 可用训练样本 %d" % (name, sum(1 for _ in open(p)), len(items)))
        if errs:
            print("  丢弃:", dict(errs))
        else:
            print("  丢弃: 0 ✓")
        if lens:
            print("  序列长度: p50=%d p90=%d max=%d（max_len=%d）" % (
                sorted(lens)[len(lens) // 2], sorted(lens)[int(len(lens) * .9)], max(lens), cfg["max_len"]))
            print("  token 后标签分布:", dict(lab))


if __name__ == "__main__":
    main()
