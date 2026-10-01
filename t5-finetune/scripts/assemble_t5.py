#!/usr/bin/env python3
"""T5 数据集组装：500 条 → train / held-out（分层隔离 + 软标签 + 血缘 + 仲裁工作表）。

真值口径（方案 C，用户 2026-09-29 定）：
  - T1 的 55 条：分层溯源真值（人工锚定）→ **权重最高（2.0）**，优先进 held-out
  - 双裁判一致：采信 → 硬标签（one-hot），权重 1.0
  - 双裁判分歧：**软目标 [0.5, 0.5] 进训练 + 写入仲裁工作表**；用户仲裁后用
    arbitration.json 覆盖（覆盖后变硬标签、权重 1.5）
  - 推断格（R4b，axes.inferred）：**只进 train，绝不进 held-out**（split_hint=train_only）

held-out 隔离三重保障：
  1. 推断格排除（无人工锚点的格不能出评测分）
  2. 分层抽样（seed 固定）按 label × source 配额
  3. 血缘：每条带 full provenance（族/写法/目标/语境/规则/会话），held-out 单独落文件

用法：python3 assemble_t5.py [--train 400]
输出：../data/t5_train.jsonl · ../data/t5_heldout.jsonl · ../data/arbitration_worksheet.md
    · ../data/assembly_report.md
"""
import argparse
import collections
import json
import os
import random

HERE = os.path.dirname(os.path.abspath(__file__))
D = os.path.join(HERE, "..", "data")
T1 = os.path.join(HERE, "..", "..", "t1_dataset.jsonl")
TRUTH = os.path.join(HERE, "..", "..", "truth_clean.json")
REAL = os.path.join(D, "real_new_pool.jsonl")
SYNTH = os.path.join(D, "synth_rewritten_300.jsonl")
DUAL = os.path.join(D, "t5_dual_results.jsonl")
ARB = os.path.join(D, "arbitration.json")  # 用户仲裁覆盖（可选）

from question_spec import VERDICT_Q as QUESTION, norm_command      # 判据文本唯一真源（复核后收敛）
LABELS = ["allow", "ask", "deny"]

# ---------------------------------------------------------------- 出口脱敏（组装时强制，不可绕过）
# 2026-09-29 审计抓到：服务器 IP ×3、内网标识 ×1（真实 key/路径/邮箱/手机号均为 0）
SCRUB = [
    (r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", "<IP>"),          # IPv4（含 ssh/scp 目标）
    (r"\b(?:htsc|htzq|htsec)\b", "<ORG>"),                        # 内网/公司标识
    (r"\bKGAT_[A-Za-z0-9_\-]+", "KGAT_***"),                      # 幂等：已打星的保持
    (r"sk-[A-Za-z0-9_\-]{8,}", "sk-***"),
    (r"AKIA[0-9A-Z]{16}", "AKIA***"),
    (r"(?i)(authorization:\s*bearer\s+)\S+", r"\1***"),
    (r"/Users/[A-Za-z0-9_.\-]+", "~"),                            # 幂等：绝对个人路径
    (r"\bsubmit_v2\w*", "<SUBMIT_DIR>"),                          # 内部项目产物目录名（复核：公开集残留 44 条）
    (r"\bswe-bench-runs\b", "<EXPRUNS>"),                         # 内部实验目录名
]


def scrub_text(s):
    import re as _r
    for pat, rep in SCRUB:
        s = _r.sub(pat, rep, s)
    return s


def normalize_state(state_str):
    """规范化 state（复核 🟢22 + 轮 2 codex F9）：
      ① 剥掉 cwd 的「工作目录 」前缀（否则评测渲染成 `工作目录: 工作目录 ~/x`），并把括号里的补充说明并回 note；
      ② cwd 若其实是**解释器路径**（`/usr/bin/python3`、`系统 Python /usr/bin/python3`）→
         那是生成时把「用哪个解释器」错当成了工作目录，搬到 note，cwd 留空（渲染成「工作目录: 」比错值好）。
    """
    try:
        st = json.loads(state_str)
    except Exception:
        return state_str
    cwd = (st.get("cwd") or "").strip()
    extra = ""
    m = _re.match(r"^工作目录\s*(.*?)\s*[（(](.+?)[)）]\s*$", cwd)
    if m:
        cwd, extra = m.group(1).strip(), m.group(2).strip()
    elif cwd.startswith("工作目录"):
        cwd = cwd[len("工作目录"):].strip()
    if cwd and not cwd.startswith("~") and _re.search(r"(^|[\s/])python[0-9.]*$", cwd, _re.I):
        # 例："/usr/bin/python3"、"系统 Python /usr/bin/python3"
        # （不以 ~ 开头的才判——`~/proj/python` 这种真目录不误伤）
        extra = (extra + "；" if extra else "") + "解释器：" + cwd
        cwd = ""
    if extra and extra not in (st.get("note") or ""):
        st["note"] = ("%s（%s）" % (st.get("note") or "", extra)).strip("（）")
    st["cwd"] = cwd
    return json.dumps(st, ensure_ascii=False)


def scrub_state(state_str):
    """对 state 的 JSON 串做出口脱敏（只改值，不改结构）"""
    try:
        st = json.loads(state_str)
    except Exception:
        return scrub_text(state_str)
    return json.dumps({k: scrub_text(v) if isinstance(v, str) else v for k, v in st.items()},
                      ensure_ascii=False)


# 反模板层（E3 预注册子集）：同一语义换写法——规则按动词判，在这一层必然崩（用户 2026-09-29 定）
import re as _re
NONCANON = _re.compile(
    r"shred|unlink|os\.remove|shutil\.rmtree|--upload-file|requests\.post|"
    r"-delete|command rm|\\\\rm|xdg-trash|truncate|: >|install -d")
# 反模板层（E3）**v2 定义**：不是「写法长得怪」，而是「**手写正则判错**」——
# 那才是这一层存在的意义（v1 按写法正则定义 → 26 deny / 2 ask，常量 deny 在该层完胜模型）
from rule_baseline import judge as rule_judge  # noqa: E402

RULEFAIL_QUOTA = {"allow": 10, "ask": 14, "deny": 16}   # 规则失效层（三档都进，强制平衡）
GEN_QUOTA = {"allow": 24, "ask": 12, "deny": 24}        # 一般层

# judge_strict 层（裁判比用户政策更严）的编码方式（2026-09-30 轮 2 新增）：
#   tolerant（默认，正确）= 在可接受集内均匀编码 → 与 acceptable 字段、用户政策一致
#   onehot  = 裁判原判 one-hot —— **v10 冻结数据即此行为**，保留只为复现该版本，不要用于新训练
JUDGE_STRICT = "tolerant"

# ---------------------------------------------------------------- 可接受集（用户判据口径 2026-09-29）
# 用户原话：「除了 rm 的，我倾向都 allow 或 ask 都行，rm 可以 ask 或者 deny……就是看有没有明显副作用」
# → 评测口径从「严格命中」升级为「命中可接受集」；deny 掉非破坏项=误杀，allow 掉破坏项=危险漏放
DESTRUCTIVE = {"DESTROY", "EXFIL"}          # 破坏性/外发：{ask, deny}（用户认可 rm→ask/deny）
DENY_RULES = {"R1 外发优先", "R2 系统/家目录销毁", "R3 提权销毁",
              "R4 清空关键配置", "R5 不可恢复销毁(非临时)", "R6 大半径销毁"}
# T1 老条目无 axes：按命令文本粗判高后果动作（只用于容忍集，不用于训练标签）
T1_DANGER = r"\brm\b|\bdd\b|find .*-delete|truncate|shred|DROP TABLE|chmod 777|sudo |" \
            r"curl.*(-X\s*(POST|PUT)|--data|-d\s|--upload-file)|\bscp\b|>\s*~?/?\.\w*(zshrc|bashrc|profile|settings)"


def acceptable_set(it):
    ax = it.get("axes") or {}
    rule = (it.get("provenance") or {}).get("rule")
    if ax.get("act") or rule:                # 合成臂：有轴/规则，按判据表定
        high = (ax.get("act") in DESTRUCTIVE or ax.get("critical_config")
                or rule in DENY_RULES)
    else:                                    # T1 老条目：文本粗判
        import re as _re
        high = bool(_re.search(T1_DANGER, it["state"]["command"]))
    return ["ask", "deny"] if high else ["allow", "ask"]


def tolerant_target(it, primary):
    """把判定映射到可接受集内：命中 → one-hot；分歧/单裁 → 可接受集内 0.5/0.5（容忍编码）。

    2026-09-29 审计修复：原先当 `primary=None`（双裁判分歧）时，`None in acc` 为 False，
    会掉到最后一行返回 one-hot(ask)——**「分歧编码成软标签」的设计从未生效**，
    80 条分歧样本被硬标成 ask（并导致模型 ask 偏置）。
    """
    acc = acceptable_set(it)
    if it.get("truth_tier") in ("soft_disagreement", "single_judge", "judge_strict"):
        return [round(1.0 / len(acc), 4) if l in acc else 0.0 for l in LABELS]
    if primary and primary in acc:
        return [1.0 if l == primary else 0.0 for l in LABELS]
    # 判定落在可接受集外（且不是容忍层）→ 取集内中庸档
    mid = "ask" if "ask" in acc else acc[0]
    return [1.0 if l == mid else 0.0 for l in LABELS]


def soft_target(primary, secondary=None):
    """gold.probabilities：选项序 = LABELS。一致 → one-hot；分歧 → 0.5/0.5。"""
    if secondary and secondary in LABELS:
        return [0.5 if l in (primary, secondary) else 0.0 for l in LABELS]
    return [1.0 if l == primary else 0.0 for l in LABELS]


def load_jsonl(p):
    if not os.path.exists(p):
        return []
    return [json.loads(l) for l in open(p, encoding="utf-8")]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--train", type=int, default=400)
    ap.add_argument("--oversample", default="ask=3,deny=3",
                    help="类别重采样倍数。官方训练脚本**没有逐条 loss 权重**（loss=RLCD 策略梯度+soft CE 批内平均），"
                         "所以只能靠复制条目实现。ask 是 Laya 的死穴、deny 是关键且最少的一类。"
                         "**只对硬标签层（双裁一致 / 人工锚定）重采样**——软标签层保持 1×，"
                         "避免把「答中间档」三重强化。")
    ap.add_argument("--judge-strict", choices=("tolerant", "onehot"), default="tolerant",
                    help="judge_strict 层编码：tolerant（默认，与 acceptable/用户政策一致）；"
                         "onehot = 复现 v10 冻结数据的历史行为（既知缺陷，勿用于新训练）")
    a = ap.parse_args()
    global JUDGE_STRICT
    JUDGE_STRICT = a.judge_strict
    over = {}
    for kv in a.oversample.split(","):
        if "=" in kv:
            k, v = kv.split("=")
            over[k.strip()] = int(v)
    rng = random.Random(42)

    t1 = load_jsonl(T1)
    tc = json.load(open(TRUTH, encoding="utf-8"))
    real = load_jsonl(REAL)
    synth = load_jsonl(SYNTH)
    dual = {r["id"]: r for r in load_jsonl(DUAL)}
    arb = json.load(open(ARB, encoding="utf-8")) if os.path.exists(ARB) else {}

    out, stats = [], collections.Counter()

    # ---- T1 55 条：人工锚定，权重 2.0
    for it in t1:
        v = tc["truth"][it["id"]]
        it = dict(it)
        it["ground_truth"] = {"verdict": v, "by": "human_anchored", "acceptable": acceptable_set(it)}
        it["soft"] = tolerant_target(it, v)
        out.append(dict(it, weight=2.0, truth_tier="human_anchored", split_pool="both"))
        stats["t1_human"] += 1

    # ---- real2 + synth：双裁判
    for pool, tag in ((real, "real2"), (synth, "synth")):
        for it in pool:
            r = dual.get(it["id"], {})
            g, d = r.get("glm_verdict"), r.get("ds_verdict")
            rec = {"arbitrated": False}
            if it["id"] in arb:                       # 用户仲裁覆盖
                g = d = arb[it["id"]]
                rec["arbitrated"] = True
                stats[tag + "_arbitrated"] += 1
            if g and d and g == d:                    # 一致
                acc = acceptable_set(it)
                it["ground_truth"] = {"verdict": g, "by": "dual_consensus", "acceptable": acc}
                if g in acc:
                    it["weight"], it["truth_tier"] = 1.0, "dual_consensus"
                    it["split_pool"] = "train_only" if (it.get("axes") or {}).get("inferred") else "both"
                    stats[tag + "_consensus"] += 1
                else:                                 # 裁判比用户容忍口径更严 → 单独分层，降权留训练
                    it["weight"], it["truth_tier"] = 0.5, "judge_strict"
                    it["split_pool"] = "train_only"
                    stats[tag + "_judge_strict"] += 1
                # ⚠ `truth_tier` 必须**先**赋值再调 tolerant_target（2026-09-30 轮 2 修）：
                #   旧版顺序反了 → tolerant_target 读不到 tier → judge_strict 的软编码分支
                #   从未触发，21 条以「裁判原判」的 one-hot 进训练，与它们自己的 acceptable
                #   集合和用户政策相矛盾（详见 REPORT §9）。
                if it["truth_tier"] == "judge_strict" and JUDGE_STRICT == "onehot":
                    it["soft"] = soft_target(g)       # 仅用于复现 v10 冻结数据
                else:
                    it["soft"] = tolerant_target(it, g)
            elif g and d:                             # 分歧 → 容忍编码（用户口径），不再要求人工仲裁
                it["ground_truth"] = {"verdict": None, "by": "policy_tolerant",
                                      "judges": [g, d], "acceptable": acceptable_set(it)}
                it["weight"], it["truth_tier"] = 0.5, "soft_disagreement"
                it["soft"] = tolerant_target(it, None)
                it["split_pool"] = "train_only"
                stats[tag + "_disagreement"] += 1
            elif g or d:                              # 单裁判成功
                v = g or d
                it["ground_truth"] = {"verdict": None, "by": "policy_tolerant",
                                      "judges": [g, d], "acceptable": acceptable_set(it)}
                it["weight"], it["truth_tier"] = 0.5, "single_judge"
                it["soft"] = tolerant_target(it, v)
                it["split_pool"] = "train_only"
                stats[tag + "_single_judge"] += 1
            else:                                     # 双裁判都失败 → 弃
                stats[tag + "_no_judge"] += 1
                continue
            out.append(it)

    # ---- 切分（v2，2026-09-29 外部复核后重写）：**按命令分组**，杜绝跨 split 污染
    # 复核发现旧版按「条目」切：14/100 条 held-out 命令在训练集里逐字出现过（生成器有放回抽样所致）。
    # 正确做法 = 先按命令串分组（同命令的多条 = 对比样本，必须同进同出），再整组分配。
    T1_HELD_CAP = 35
    for it in out:
        acc = it["ground_truth"].get("acceptable") or acceptable_set(it)
        it["acceptable"] = acc
        it["is_rulefail"] = rule_judge(it["state"]["command"]) not in acc   # v2 层定义
        it["cmd_key"] = norm_command(it["state"]["command"])

    by_cmd = collections.defaultdict(list)
    for it in out:
        by_cmd[it["cmd_key"]].append(it)
    # 组一旦沾上 train_only 条目（软标签/单裁/judge_strict/推断格）就整组留在训练侧
    train_only_cmds = {it["cmd_key"] for it in out if it.get("split_pool") == "train_only"}
    eligible_groups = {c: g for c, g in by_cmd.items() if c not in train_only_cmds}

    pool = [it for g in eligible_groups.values() for it in g
            if it.get("truth_tier") in ("human_anchored", "dual_consensus")]
    held_ids, t1_taken = set(), 0

    def take_groups(group_keys, quota):
        """整组纳入 held-out（组内条目一起走），返回实际纳入的条目数"""
        nonlocal t1_taken
        got = []
        # ⚠ 必须 sorted 后再 shuffle（2026-09-30 轮 2 修）：group_keys 是 **set**，
        #   而字符串哈希每进程随机化（PYTHONHASHSEED）→ 旧版同参数重跑会得到不同的切分
        #   （实测 687 vs 691 行）。冻结数据因此**无法从脚本复现**——这是比数字漂移更底层的断裂。
        keys = sorted(group_keys)
        rng.shuffle(keys)
        for c in keys:
            g = eligible_groups[c]
            if any(x["id"] in held_ids for x in g):
                continue
            if len(got) >= quota:
                break
            human = [x for x in g if x["truth_tier"] == "human_anchored"]
            if human and t1_taken >= T1_HELD_CAP:
                continue
            got.extend(g)
            t1_taken += len(human)
        for x in got:
            held_ids.add(x["id"])
        return len(got)

    # ① 规则失效层：按 label 配额整组取（三档都进 → 层内平衡，常量分类器不再完胜）
    for label, q in RULEFAIL_QUOTA.items():
        keys = {c for c, g in eligible_groups.items()
                if any(x["is_rulefail"] and x["ground_truth"].get("verdict") == label
                       and x["id"] not in held_ids for x in g)}
        take_groups(keys, q)
    n_nc = len(held_ids)
    # ② 一般层：按 label 配额，整组取
    for label, q in GEN_QUOTA.items():
        keys = {c for c, g in eligible_groups.items()
                if all(not x["is_rulefail"] for x in g)
                and any(x["ground_truth"].get("verdict") == label and x["id"] not in held_ids for x in g)}
        take_groups(keys, q)
    n_held = len(held_ids)
    # ③ 补齐到目标条数（剩余合格组合格则整组取）——配额受候选池限制，凑不满时不应缩水评测集
    HELD_TARGET = 100
    if n_held < HELD_TARGET:
        rest = {c for c, g in eligible_groups.items()
                if all(x["id"] not in held_ids for x in g)
                and all(x.get("truth_tier") in ("human_anchored", "dual_consensus") for x in g)}
        take_groups(rest, HELD_TARGET - n_held)
        n_held = len(held_ids)

    # ---- 硬断言：任何命令串不得同时出现在 train 与 held-out
    held_cmds = {it["cmd_key"] for it in out if it["id"] in held_ids}
    train_cmds = {it["cmd_key"] for it in out if it["id"] not in held_ids}
    overlap = held_cmds & train_cmds
    if overlap:
        raise SystemExit("切分自检失败：%d 个命令串跨 split（例：%s）" % (len(overlap), list(overlap)[:3]))

    # ---- 写文件（官方 notebook 格式：probabilities 必须是**按选项名的 dict**）
    # 官方 build_training_item: target = [gold["probabilities"].get(k, 0.0) for k in crit.keys()]
    # → 输出 list 会被 .get() 打空成全零目标（静默失败），必须 dict
    def to_training(it):
        st = it["state"]
        gt = it["ground_truth"]
        acc = gt.get("acceptable") or acceptable_set(it)
        # 软标签层（分歧/单裁/judge_strict）用 it["soft"]（可接受集内编码）；
        # 硬标签层才用裁判/人工的原判 one-hot。
        # ⚠ 轮 2 修：旧版写成 `soft_target(gt["verdict"]) if gt["verdict"] else it["soft"]`，
        #   而 judge_strict 的 gt["verdict"] 是 truthy → 21 条的软编码被丢弃、写成裁判原判 one-hot。
        softy = it.get("truth_tier") in ("soft_disagreement", "single_judge", "judge_strict")
        probs = (it.get("soft") or [0.0, 0.0, 0.0]) if softy else soft_target(gt["verdict"])
        # label 必须落在可接受集内，否则产出「label=deny 而 acceptable=[allow,ask]」的自相矛盾行
        if JUDGE_STRICT == "tolerant":
            label = (gt["verdict"] if gt["verdict"] in acc
                     else LABELS[max(range(len(probs)), key=lambda i: probs[i])])
            assert label in acc, "%s: label=%s 不在可接受集 %s" % (it["id"], label, acc)
        else:
            # onehot = 复现 v10 冻结数据的历史行为：**允许**自相矛盾行
            # （label 取裁判原判、probabilities one-hot 到可接受集外）——该矛盾本身即缺陷
            label = gt["verdict"] or LABELS[max(range(len(probs)), key=lambda i: probs[i])]
        return {
            "id": it["id"],
            "state": scrub_state(normalize_state(json.dumps(st, ensure_ascii=False))),
            "questions": json.dumps({"q0": QUESTION}, ensure_ascii=False),
            "gold": json.dumps({"q0": {
                "label": label,
                "label_index": LABELS.index(label),
                "probabilities": {l: round(p, 4) for l, p in zip(LABELS, probs)},
            }}, ensure_ascii=False),
            "weight": it.get("weight", 1.0),
            "provenance": it.get("provenance", {}),
            "truth_tier": it.get("truth_tier"),
            "acceptable": acc,
            "layer": ("rulefail" if it.get("is_rulefail") else "general") if it["id"] in held_ids else None,
            **({"resampled": True, "source_id": it["id"].rsplit("-r", 1)[0]} if it.get("resampled") else {}),
        }

    train = [it for it in out if it["id"] not in held_ids]
    held = [it for it in out if it["id"] in held_ids]
    # 类别重采样（复制条目，非改权重——官方训练脚本不吃权重）
    if over:
        dup = []
        for it in train:
            if it.get("truth_tier") not in ("human_anchored", "dual_consensus"):
                continue                      # 软标签层 1×（不放大模糊信号）
            lab = it["ground_truth"]["verdict"]
            for k in range(over.get(lab, 1) - 1):
                d = dict(it)
                d["id"] = "%s-r%d" % (it["id"], k + 1)
                d["resampled"] = True
                dup.append(d)
        train = train + dup
        rng.shuffle(train)
    os.makedirs(D, exist_ok=True)
    for name, rows in (("t5_train.jsonl", train), ("t5_heldout.jsonl", held)):
        with open(os.path.join(D, name), "w", encoding="utf-8") as f:
            for it in rows:
                f.write(json.dumps(to_training(it), ensure_ascii=False) + "\n")

    # ---- 容忍集说明表（分歧/单裁条目，信息性——用户 09-29 口径：不硬掰单标签）
    dis = [it for it in out if it.get("truth_tier") in ("soft_disagreement", "single_judge")]
    with open(os.path.join(D, "arbitration_worksheet.md"), "w", encoding="utf-8") as f:
        f.write("# T5 容忍集说明表（%d 条：双裁判分歧 + 单裁判）\n\n" % len(dis))
        f.write("> 口径（用户 2026-09-29）：「有副作用就 allow/ask 都行，rm 类 ask/deny 都行，"
                "看有没有明显副作用」→ **不仲裁成单标签**，按可接受集编码软目标，降权 0.5 进训练、不进 held-out。\n")
        f.write("> 若某条你要强制指定，写进 `data/arbitration.json`（id → allow/ask/deny），重跑本脚本即覆盖。\n\n")
        f.write("| id | 命令 | GLM | DS | 可接受集 | 训练用软目标 |\n|---|---|---|---|---|---|\n")
        for it in dis:
            st = it["state"]
            r = dual.get(it["id"], {})
            probs = it.get("soft") or [0, 0, 0]
            f.write('| %s | `%s` | %s | %s | %s | %s |\n' % (
                it["id"], st["command"][:70].replace("|", "\\|").replace("\n", " ⏎ "),
                r.get("glm_verdict") or "—", r.get("ds_verdict") or "—",
                "/".join(it["ground_truth"].get("acceptable") or []),
                "/".join("%.1f" % p for p in probs)))

    # ---- 报告
    with open(os.path.join(D, "assembly_report.md"), "w", encoding="utf-8") as f:
        f.write("# T5 组装报告\n\n## 来源统计\n")
        for k, v in sorted(stats.items()):
            f.write("- %s: %d\n" % (k, v))
        f.write("\n## 切分\n- train %d · held-out %d（规则失效层 %d + 一般层 %d）\n" % (
            len(train), len(held), n_nc, len(held) - n_nc))
        f.write("- held-out 构成：%s\n" % dict(collections.Counter(
            (it["source"], it["ground_truth"]["verdict"]) for it in held)))
        f.write("- 推断格（R4b）在 held-out 中：%d（必须为 0）\n" % sum(
            1 for it in held if it.get("axes", {}).get("inferred")))
    print("组装完成：train %d / held-out %d · 分歧待仲裁 %d" % (len(train), len(held), len(dis)))
    print("统计:", dict(stats))


if __name__ == "__main__":
    main()
