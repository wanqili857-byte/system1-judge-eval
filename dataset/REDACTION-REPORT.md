# T1 题集脱敏报告（可公开发布）

- 原始题集 55 条：真实会话 25 + 人工构造 30
- **可发布 44 条**：人工构造 30（原样）+ 真实会话 14（语义脱敏后）
- **剔除 11 条**（命令本身即个人信息，泛化后失去评测意义）

## 剔除明细

| id | 原因 | 命令（截断）|
|---|---|---|
| real-000 | 求职/招聘语义（命令或输出） | `python3 assemble.py --jd-file /tmp/ds-harness-jd.txt --company "DeepSe` |
| real-004 | 求职/招聘语义（命令或输出） | `grep -n -A3 "padding: 5mm 5mm" docs/resume/html/render.py | head -8` |
| real-005 | 求职/招聘语义（命令或输出） | `grep -rn "出话忠实度\|agent版RAGAS\|agent 版 RAGAS\|忠实度归因" docs --include="*.` |
| real-008 | 求职/招聘语义（命令或输出） | `grep -n "## Kaggle" docs/apply/generic/resume.md docs/apply/generic/re` |
| real-010 | 求职/招聘语义（命令或输出） | `ls ~/ayu/ai-eval/docs/apply/kaggle-agent-security/; echo "=== 比赛状态（ver` |
| real-011 | 求职/招聘语义（命令或输出） | `open "~/ayu/ai-eval/docs/apply/minimax/ai-eval/resume.pdf"` |
| real-013 | 求职/招聘语义（命令或输出） | `grep -n "^## Q" ~/ayu/ai-eval/docs/interview/qa-evaluator-methodology.` |
| real-017 | 求职/招聘语义（命令或输出） | `echo "--- RAGAS in apply/ ---" && grep -rn "RAGAS\|ragas" docs/apply/ ` |
| real-019 | 凭证操作 | `DB="file:$HOME/.cc-switch/cc-switch.db?mode=ro"
echo "=== common_confi` |
| real-021 | 求职/招聘语义（命令或输出） | `find ~/ayu/ai-eval/docs -name "11.md" 2>/dev/null; echo "---"; ls ~/ay` |
| real-023 | 凭证操作 | `export KAGGLE_API_TOKEN=KGAT_***REDACTED*** && rm -rf submit_v2_out &&` |

## 脱敏规则（脚本 `scripts/redact_public.py`，可复跑核验）

- 个人路径 `~/ayu/<repo>` → `~/workspace/<repo>`；`~/.claude` → `~/.agent-config`
- 私有项目名 → 泛化名（workspace / eval-harness / novel-agent / config-db …）
- 公司名（求职语境） → `<company>`
- 凭证 `KGAT_*` / `sk-*` → `<REDACTED>`；**凭证操作类命令整条剔除**
- 求职/招聘语义命令整条剔除（`jd-file` / `resume` / `docs/apply` / `interview`）

> 说明：剔除的条目不影响结论——T1 的结论（deny 召回崩塌 / ask 塌缩 / 常量分类器）由聚合统计给出，
> 且敏感性检验（剔除 LLM 定义真值后 n=41）已证明排序不依赖个别条目。
