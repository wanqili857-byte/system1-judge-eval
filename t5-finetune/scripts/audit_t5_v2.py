#!/usr/bin/env python3
"""T5 审计 v2（2026-09-29 外部复核后重写）。

v1 的死穴（复核 §A 实证）：`audit_t5.py` **一个文档文件都没打开**——它比的是
「数据文件 ↔ 脚本里写死的常量」，所以文档写错数字时它永远绿（实证：三关全绿的同时，
publish/README 印着 101 条的错混淆矩阵、模型卡写着 604 条与 0/30）。

v2 改为查**机制**（复核者建议：「把门禁从『重算数字』升级成『重跑发布动作』」）：

  A 文档数字抽取 ↔ 重算集合比对（解析 .md，不再抄进常量）
  B 分母一致性（`n/m` 形态）
  C 混淆矩阵一致性（解析报告里的矩阵块）
  D **模型 ↔ 数据指纹绑定**（sha256，复核 #1 的要害）
  E **判据文本四处一致**（训练数据 / 评测脚本 / 裁判 prompt / 模型卡）
  F 脱敏**全字段**扫描（v1 只比 state.command 全等）
  G 判据表锚点数（含零锚点/死规则体检）
  H held-out 隔离与污染（分层、软标签、跨 split 命令）

用法：python3 audit_t5_v2.py
"""
import collections
import hashlib
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
F = os.path.join(HERE, "..")
D = os.path.join(F, "data")
PUB = os.path.join(F, "publish")
FAIL, WARN = [], []


def load_jsonl(p, parse=()):
    if not os.path.exists(p):
        return []
    rs = [json.loads(l) for l in open(p, encoding="utf-8")]
    for r in rs:
        for k in parse:
            if isinstance(r.get(k), str):
                r[k] = json.loads(r[k])
    return rs


def bad(msg):
    FAIL.append(msg)
    print("  ✗ %s" % msg)


def warn(msg):
    WARN.append(msg)
    print("  ⚠ %s" % msg)


def ok(msg):
    print("  ✓ %s" % msg)


def check_docs():
    """A/B/C 合并成一条硬检查：**重跑发布动作**，把三份文档的结果区块与现算区块逐字节比对。

    轮 2 的教训：旧版只做「文档里抽出的数字 ∈ 重算集合」的集合成员判定，
    阈值是「过半对不上才红」——**E0/E-rule 两行用了旧 held-out 的 41 条分母**
    就是这样在门禁全绿的情况下进了对外文档。集合成员判定不可能抓住「分母错了」。
    """
    print("\n【A/B/C】文档结果区块 ↔ 现算区块（逐字节）")
    try:
        import refill_docs
    except ImportError:
        bad("无法 import refill_docs → 跳过文档区块比对"); return
    try:
        blocks = refill_docs.build_blocks()
    except SystemExit as e:
        bad("现算结果区块失败：%s" % e); return
    if not blocks:
        bad("无法生成结果区块（缺 E0/E1/rule 预测）"); return
    for name, want in (("REPORT.md", blocks[0]), ("publish/README.md", blocks[1]),
                       ("publish/model-card.md", blocks[2])):
        p = os.path.join(F, name)
        if not os.path.exists(p):
            bad("缺文档 %s" % name); continue
        s = open(p, encoding="utf-8").read()
        i, j = s.find(refill_docs.START), s.find(refill_docs.END)
        if i < 0 or j < 0:
            bad("%s 缺 RESULTS 标记区间" % name); continue
        got = s[i:j + len(refill_docs.END)]
        (ok if got == want else bad)("%s 结果区块%s" % (name, "一致" if got == want else " ≠ 现算（跑 refill_docs.py 回填）"))
        pl = refill_docs.prose_len(s)
        (ok if pl >= refill_docs.MIN_PROSE else bad)(
            "%s 区间外正文 %d 字（< %d = 被掏空）" % (name, pl, refill_docs.MIN_PROSE))


# ---------------------------------------------------------------- D 模型↔数据绑定
def check_binding():
    """D：真比对 sha，而不是「文件存在」。

    轮 2 的教训：旧版只 `print` 两个 sha，然后检查 `CHECKPOINT-SHA256.txt` **是否存在**——
    承诺的「断言模型 sha256 ↔ train sha256」根本没实现，官方口径的「血缘闭环」
    在门禁层面等价于「有两个文件」。
    """
    print("\n【D】模型 ↔ 数据指纹绑定")
    model = os.path.join(F, "models", "t5_ft", "model.safetensors")
    sha_file = os.path.join(F, "models", "t5_ft", "CHECKPOINT-SHA256.txt")
    train = os.path.join(D, "t5_train.jsonl")
    cut = os.path.join(F, "kaggle", "dataset", "t5_train.jsonl")      # as_trained 裁剪副本
    freeze_p = os.path.join(D, "FREEZE.json")
    if not os.path.exists(model):
        # 轮 3：这里是 warn+return，而 main() 只看 FAIL → 整段「模型↔数据绑定」可以被跳过而仍退 0
        bad("本地无微调权重（models/t5_ft/model.safetensors）→ 模型↔数据绑定无法校验")
        return
    for p, tag in ((sha_file, "CHECKPOINT-SHA256.txt"), (freeze_p, "FREEZE.json")):
        if not os.path.exists(p):
            bad("缺 %s → 无凭据能把模型绑到某一版数据（复核 #1）" % tag); return
    mh = hashlib.sha256(open(model, "rb").read()).hexdigest()
    th = hashlib.sha256(open(train, "rb").read()).hexdigest()
    nrow = sum(1 for _ in open(train, encoding="utf-8"))
    # 新鲜度断言：**不比文件 mtime**（轮 2 codex F4：数据一次确定性重建、内容逐字节不变也会刷新 mtime
    # → 误报红）。改为与 FREEZE 记录的冻结时刻比——那是一个被写下来的值。
    fr0 = json.load(open(freeze_p, encoding="utf-8")) if os.path.exists(freeze_p) else {}
    ts = fr0.get("frozen_at")
    if ts:
        import time as _t
        frozen = _t.mktime(_t.strptime(ts, "%Y-%m-%d %H:%M:%S"))
        if os.path.getmtime(model) < frozen:
            bad("权重 mtime 早于 FREEZE 冻结时刻 %s → 该模型不可能见过这版数据（复核 #1）" % ts)
        else:
            ok("权重 mtime 晚于 FREEZE 冻结时刻 %s" % ts)
    else:
        warn("FREEZE.json 无 frozen_at，跳过新鲜度断言")
    # ① 权重 sha ↔ 凭据
    txt = open(sha_file, encoding="utf-8").read()
    mm = re.search(r"model\.safetensors sha256\s*=\s*([0-9a-f]{64})", txt)
    mt = re.search(r"t5_train\.jsonl\s+sha256\s*=\s*([0-9a-f]{64})", txt)
    mr = re.search(r"train rows\s*=\s*(\d+)", txt)
    if not (mm and mt):
        bad("CHECKPOINT-SHA256.txt 不可解析（拿不到 sha 字段）")
    else:
        (ok if mm.group(1) == mh else bad)("权重 sha256 与凭据%s" % ("一致" if mm.group(1) == mh else "不一致"))
        (ok if mt.group(1) == th else bad)("训练集 sha256 与凭据%s" % ("一致" if mt.group(1) == th else "不一致"))
        if mr and int(mr.group(1)) != nrow:
            bad("凭据记的训练行数 %s ≠ 实际 %d" % (mr.group(1), nrow))
        else:
            ok("凭据的行数与实际一致（%d）" % nrow)
    # ①.5 **跨源**校验：评测 kernel 在权重所在处算出的凭据（不是本地生成的）必须与本地一致。
    # 轮 3 的最强反例：旧版只做「本地文件 ↔ 本地凭据」，而那份凭据是脚本对同一文件自算的 → 恒绿。
    kside = os.path.join(F, "kaggle_eval", "out7", "CHECKPOINT-SHA256.txt")
    if os.path.exists(kside):
        ktxt = open(kside, encoding="utf-8").read()
        km = re.search(r"model\.safetensors sha256\s*=\s*([0-9a-f]{64})", ktxt)
        kh = re.search(r"held-out sha256\s*=\s*([0-9a-f]{64})", ktxt)
        ka = re.search(r"t5_train\(as_trained\) sha256\s*=\s*([0-9a-f]{64})", ktxt)
        (ok if km and km.group(1) == mh else bad)("跨源：评测 kernel 权重 sha %s 本地" % ("==" if km and km.group(1) == mh else "!="))
        he_p = os.path.join(D, "t5_heldout.jsonl")
        if kh and os.path.exists(he_p):
            hh = hashlib.sha256(open(he_p, "rb").read()).hexdigest()
            (ok if kh.group(1) == hh else bad)("跨源：评测 kernel 留出集 sha %s 本地" % ("==" if kh.group(1) == hh else "!="))
        if ka and os.path.exists(cut):
            ch2 = hashlib.sha256(open(cut, "rb").read()).hexdigest()
            (ok if ka.group(1) == ch2 else bad)("跨源：评测 kernel as_trained sha %s 本地" % ("==" if ka.group(1) == ch2 else "!="))
    else:
        bad("缺 kaggle_eval/out7/CHECKPOINT-SHA256.txt（评测 kernel 的跨源凭据）——只有本地自证不算证据链")

    # ② FREEZE ↔ 本地全字段版 / as_trained 裁剪副本（轮 2：旧版只绑了本地版，没绑裁剪副本）
    fr = json.load(open(freeze_p, encoding="utf-8"))
    w = (fr.get("files") or {}).get("t5_train.jsonl", {}).get("sha256")
    (ok if w == th else bad)("FREEZE 的 t5_train sha %s 本地实际" % ("==" if w == th else "!="))
    at = (fr.get("as_trained") or {})
    if at.get("sha256") and os.path.exists(cut):
        ch = hashlib.sha256(open(cut, "rb").read()).hexdigest()
        crow = sum(1 for _ in open(cut, encoding="utf-8"))
        (ok if ch == at["sha256"] else bad)("as_trained 裁剪副本 sha %s FREEZE" % ("==" if ch == at["sha256"] else "!="))
        if at.get("rows") and crow != at["rows"]:
            bad("as_trained 副本行数 %d ≠ FREEZE 记的 %s" % (crow, at["rows"]))
        else:
            ok("as_trained 副本行数一致（%d）" % crow)


# ---------------------------------------------------------------- E 判据文本一致
def check_question_text():
    print("\n【E】判据文本四处一致")
    sys.path.insert(0, HERE)
    from question_spec import VERDICT_Q
    canon = json.dumps(VERDICT_Q, ensure_ascii=False, sort_keys=True)
    srcs = {"数据(train)": os.path.join(D, "t5_train.jsonl"),
            "数据(held-out)": os.path.join(D, "t5_heldout.jsonl"),
            "公开集": os.path.join(PUB, "dataset", "t5_synth_public.jsonl")}
    for name, p in srcs.items():
        rows = load_jsonl(p, ("questions",))
        if not rows:
            warn("%s 不存在，跳过" % name)
            continue
        q = rows[0]["questions"]
        q = q.get("q0", q) if isinstance(q, dict) else q
        got = json.dumps({k: q[k] for k in ("type", "instructions", "criteria") if k in q},
                         ensure_ascii=False, sort_keys=True)
        (ok if got == canon else bad)("%s 的判据文本与 question_spec 一致" % name
                                      if got == canon else "%s 的判据文本与 question_spec 不一致" % name)
    for script in ("run_e0.py", "run_ft.py"):
        s = open(os.path.join(HERE, script), encoding="utf-8").read()
        (ok if "question_spec" in s else bad)("%s import question_spec" % script)
    # 轮 2 追加：**裁判脚本也必须来自唯一真源**（旧版只查 run_e0/run_ft，正好漏掉真值的来源）
    laya = os.path.join(F, "..")
    for rel in ("run_dual_judge.py", "run_dual_t5.py", "run_e_arm.py", "run_t1.py"):
        p = os.path.join(HERE, rel) if rel == "run_dual_t5.py" else os.path.join(laya, rel)
        if not os.path.exists(p):
            warn("缺 %s" % rel); continue
        s = open(p, encoding="utf-8").read()
        (ok if "question_spec" in s else bad)("%s 引用 question_spec（裁判/基线不得自带判据文本）" % rel)
    # 判据文本的**实际**出现位置：只允许真源 + 数据文件 + 记录性注释。
    # deny 文案从真源取（不在本文件里再抄一份——否则检查自己就成了第 N 处散落）。
    sys.path.insert(0, HERE)
    from question_spec import VERDICT_CRITERIA
    deny_line = VERDICT_CRITERIA["deny"]
    # 白名单（每条写明理由）：真源本身；引用真源的脚本；文档里的判据释义；
    # 以及**生成产物** t5_eval.ipynb —— 它的判据文本由 make_eval_kernel 从 question_spec 注入，
    # 每次重新生成都会刷新，属于「同一真源的投影」而非第二份手抄。
    allowed = {"question_spec.py", "run_dual_judge.py", "run_e_arm.py", "run_t1.py",
               "REPORT.md", "model-card.md", "rubric-command-safety.md", "t5_eval.ipynb"}
    stray = []
    # 轮 3：旧版只做一层 os.listdir → 覆盖不到 kaggle_eval/*.ipynb 与 publish/scripts/。改 os.walk。
    for root in (HERE, laya, os.path.join(F, "publish"), os.path.join(F, "kaggle_eval")):
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in ("out", "out2", "out4", "out6", "_archive")]
            for fn in filenames:
                if not fn.endswith((".py", ".md", ".ipynb")) or fn in allowed:
                    continue
                p = os.path.join(dirpath, fn)
                try:
                    if deny_line in open(p, encoding="utf-8").read():
                        stray.append(os.path.relpath(p, laya))
                except (UnicodeDecodeError, OSError):
                    pass
    (ok if not stray else bad)("判据文本散落：%s" % (stray or "仅真源与数据文件"))


# ---------------------------------------------------------------- F 脱敏全字段
def check_scrub():
    print("\n【F】脱敏全字段扫描（v1 只比 state.command 全等）")
    PAT = {"IP": r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b",
           "凭证": r"(KGAT_[A-Za-z0-9]{6,}|sk-[A-Za-z0-9_\-]{8,}|AKIA[0-9A-Z]{16}|Bearer\s+\S+)",
           "绝对路径": r"/Users/[A-Za-z0-9_.\-]+",
           "内网标识": r"\b(htsc|htzq)\b",
           "内部目录名": r"\b(submit_v2\w*)\b",
           "邮箱/手机": r"([\w.\-]+@[\w\-]+\.[a-z]{2,}|\b1[3-9]\d{9}\b)"}
    # 轮 3：旧版只扫「公开集 + Kaggle 副本」，且查的是产线保证不可能存在的形态（/Users/<name> 已被替换）。
    # 现在扩到训练集/留出集/发布脚本，并**显式报告自家户名残留**（不是判红——私有副本允许保留，
    # 但必须可见；若日后要公开，先跑改写）。
    import getpass
    me = getpass.getuser()
    for name, p in (("公开集", os.path.join(PUB, "dataset", "t5_synth_public.jsonl")),
                    ("Kaggle 副本", os.path.join(F, "kaggle", "dataset", "t5_train.jsonl")),
                    ("train", os.path.join(D, "t5_train.jsonl")),
                    ("held-out", os.path.join(D, "t5_heldout.jsonl")),
                    ("发布脚本 gen.py", os.path.join(PUB, "scripts", "gen.py"))):
        if not os.path.exists(p):
            warn("%s 不存在" % name)
            continue
        hits = collections.Counter()
        for line in open(p, encoding="utf-8"):
            for k, pat in PAT.items():
                for m in re.findall(pat, line):
                    hits[k] += 1
        (ok if not hits else bad)("%s 全字段扫描：%s" % (name, dict(hits) or "干净"))
        if os.path.exists(p):
            n_me = open(p, encoding="utf-8", errors="ignore").read().count("/" + me)
            if n_me:
                warn("%s 含本机用户名 %r 共 %d 处（私有副本允许，公开前须改写）" % (name, me, n_me))
    # 血缘字段是否泄漏到外发副本
    kg = os.path.join(F, "kaggle", "dataset", "t5_train.jsonl")
    if os.path.exists(kg):
        first = json.loads(open(kg, encoding="utf-8").readline())
        leaked = [k for k in ("provenance", "truth_tier", "acceptable", "layer") if k in first]
        (ok if not leaked else bad)("Kaggle 副本字段裁剪：%s" % (leaked or "仅训练必需字段"))


# ---------------------------------------------------------------- G 判据表体检
def check_rubric():
    print("\n【G】判据表体检（锚点数 / 死规则）")
    sys.path.insert(0, HERE)
    import policy
    rows = load_jsonl(os.path.join(D, "t5_train.jsonl")) + load_jsonl(os.path.join(D, "t5_heldout.jsonl"))
    hit = collections.Counter()
    for r in rows:
        p = r.get("provenance") or {}
        if p.get("rule"):
            hit[p["rule"]] += 1
    zero = [n for n, _c, _v in policy.RULES if n not in hit]
    if zero:
        warn("零锚点规则（本数据集无实例）：%s" % zero)
    else:
        ok("每条规则至少有 1 个锚点")


# ---------------------------------------------------------------- H held-out 隔离
def check_isolation():
    print("\n【H】held-out 隔离与污染")
    he = load_jsonl(os.path.join(D, "t5_heldout.jsonl"), ("state", "gold"))
    tr = load_jsonl(os.path.join(D, "t5_train.jsonl"), ("state", "gold"))
    if not he:
        warn("缺 held-out")
        return
    from question_spec import norm_command          # 与装配侧同一个归一化（轮 3）
    hc = {norm_command(r["state"]["command"]) for r in he}
    tc = {norm_command(r["state"]["command"]) for r in tr}
    (ok if not (hc & tc) else bad)("跨 split 命令重叠：%d" % len(hc & tc))
    soft = sum(1 for r in he if sorted(r["gold"]["q0"]["probabilities"].values()) != [0.0, 0.0, 1.0])
    (ok if soft == 0 else bad)("held-out 含软标签：%d" % soft)
    inferred = sum(1 for r in he if (r.get("provenance") or {}).get("split_hint") == "train_only")
    (ok if inferred == 0 else bad)("held-out 含推断格：%d" % inferred)
    print("    层构成：%s" % dict(collections.Counter(r.get("layer") for r in he)))


def main():
    print("=" * 78)
    print("T5 审计 v2（解析文档 + 查机制）")
    check_docs()
    check_binding()
    check_question_text()
    check_scrub()
    check_rubric()
    check_isolation()
    print("\n" + "=" * 78)
    print("失败 %d 项 · 警告 %d 项 —— 引用时说「0 失败 / N 警告」，别只报失败数" % (len(FAIL), len(WARN)))
    for m in FAIL:
        print("  ✗ %s" % m)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
