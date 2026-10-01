---
license: mit
base_model: convaiinnovations/laya
library_name: transformers
language:
  - zh
tags:
  - text-classification
  - safety
  - command-line
  - agent-safety
  - laya
  - rlcd
pipeline_tag: text-classification
---

# 模型卡 · Laya 命令安全判定（322M，RLCD 微调）

## 概要

把一个**只读的** 322M 文本编码器基座微调成命令安全三档分类器：给定「命令 + 工作目录 + 备注」，
输出 `allow`（直接执行）/ `ask`（执行前确认）/ `deny`（拦截不执行）。

| 项 | 值 |
|---|---|
| 基座 | Laya multilingual（322M 编码器） |
| 微调方式 | RLCD（proper-scoring-rule 奖励 + GRPO 式策略梯度 + 软交叉熵） |
| 训练数据 | 合成（抗模板改写）+ 脱敏真实会话；规模、指纹与「入集/梯度/温度拟合」三条条数见 [`REPORT.md`](REPORT.md) §5 区块 |
| 任务类型 | 三选一 typed decision（选项文本进编码器） |
| 权重指纹 | `model.safetensors` sha256 记于 `CHECKPOINT-SHA256.txt`（与训练数据指纹绑定） |
| 许可 | 见 `LICENSE` |

## 训练/评测口径

真值不是单点标签，是**可接受集合**：不可逆或高危 → `{ask, deny}`；其余 → `{allow, ask}`。
评测头条是 **严格命中** 与 **平衡准确率（macro-recall）**；容忍命中是业务口径，其上界为 100%（常量 ask 即满）。

<!-- RESULTS:START -->
| 指标 | 未微调 | **微调后** | 同题常量基线 |
|---|---|---|---|
| 严格命中 | 42.0% | **71.0%** | 常量 deny 42.0% |
| 平衡准确率 | 34.5% | **71.5%** | 33.3% |
| 危险漏放 | 12 | **4** | 常量 ask 0（故此项不证明判别力）|
| 误杀 | 35 | **1** | 常量 ask 0（同上）|

### 血缘（自动生成，勿手抄）

```
data/t5_train.jsonl (sha256 76927295bdfd…, 691 行)   ← FREEZE.json 冻结于 2026-09-30 21:48:04
        │  裁剪副本（仅 id/state/questions/gold/weight）
kaggle/dataset/t5_train.jsonl (sha256 a9bc15e3da57…, 691 行)   → 训练
        │
models/t5_ft/model.safetensors (sha256 cff1f7be90ce…)
```

校验：本仓库发布的 [`scripts/audit_t5_v2.py`](scripts/audit_t5_v2.py)
会逐环比对上面的血缘链。注意它需要**私有**的原始数据与 `FREEZE.json` 才能真跑；
公开件只够复核判据与数据形态，不能独立复算这组数字。
<!-- RESULTS:END -->

完整分层结果、域外退化与**局限**见 [`REPORT.md`](REPORT.md) §5–§9。引用上面的数字前先读 §9。

## 直接用途

- 作为命令执行前的**建议档**，接在人机确认链路里：`deny` 拦截、`ask` 弹确认、`allow` 放行。
- 作为规则引擎的**边界补充**：手写正则在「该问的判成拦、该放的判成问」这类边界上表现差（见 REPORT §6），模型的价值在这一段。

## 不适用

- **不要**当作权限控制的唯一防线。留出集只有一百条量级，一个条目 = 1 个百分点。
- **不要**把它当通用判断模型用：域外（检索结果有用性）上它**低于「全判有用」这个多数类基线**——
  窄任务微调**收窄**了基座的行为面（数字见 REPORT §5 区块的 E2 段）。
- **不要**读 `confidence` 当概率用：训练做了温度缩放拟合，但**未测 ECE**；全部结论只依赖 argmax。

## 已知偏差与局限

**完整局限以 [`REPORT.md`](REPORT.md) §9 为准**（那里有十余条，此处只列最要紧的）。其中：

1. 训练集以合成为主，合成样本经另一模型改写去模板化，仍可能与真实分布有系统差异。
2. 生成真值的裁判 prompt 与判据表曾有一处措辞分叉（「覆盖关键文件」vs「清空关键配置」），
   影响面已量化（对训练目标净影响 0 条）但**未按新措辞重判**。
3. 真实会话臂的真值依赖未公开的对话上下文，公开版只有合成臂可复核。
4. **权重不可逐字节复现**：训练未固定 torch 随机源。可复现的是**数据**，不是权重。
5. 模型臂在 Kaggle（CPU）评测，基线臂在本地 venv 评测，**环境不完全可比**（版本未钉）。
6. 公开的合成分集**不是**本模型训练集的子集（多 9 条 + 路径已改写），不能用来复现这份权重。
7. 判据表 17 条规则覆盖不足：T1 标注上 R6 无实例、R4b 零样本；组装数据上零锚点是 R3/R6（两个论域，别混用）。

## 关于 `rl_agent_config.json`

该文件继承自基座，其中 `fine_tuned_from_checkpoint` 仍为 `false`，**不代表本次微调的状态**——
它是基座自带的配置，本次训练与导出的实际产物以权重与 `CHECKPOINT-SHA256.txt` 为准。
`temperature` 字段是本次训练**温度拟合的产物**（首个温度 ≠ 基座占位值即为证据）。
