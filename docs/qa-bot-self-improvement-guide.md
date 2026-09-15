# 答疑机器人知识优化：使用说明

在 QwenPaw Console 的 session 中执行。代码更新后需要重启服务，刷新网页不会加载新代码。
流程只更新本地知识库和基线，不上传服务器。

## 文件放在哪里

以下位置相对于当前 Agent 工作区，默认是 `~/.qwenpaw_selflearn/workspaces/default`。
一批历史使用一个名称，例如 `A` 或 `2026-09-01_2026-09-07`。

```text
analyze/
  history_jsonl/A/
    A.jsonl                    原始历史，保持不变
    A_train.jsonl              约 80%，只用这部分生成知识
    A_test.jsonl               约 20%，用于本轮专用测试
    split.json                 对话分组、固定种子、记录归属及校验值
    specialized/
      A_specialized.jsonl      恢复了必要上下文的可执行测试题
      references.jsonl         参考答案、要点、引用来源快照
      SCORING_RULES.md
      SCORING_PROMPT.md
      manifest.json            固定题集、标准及无法出题的记录
    round.json                 将切分、标准与 TXT 关联起来
  txt/A.txt
  sessions/A.md
  .state/                      续跑检查点，不需要日常查看

eval/
  cases_dev--知识库--模型--thinking_false.jsonl
  A_specialized--知识库--模型--thinking_false.jsonl

selflearn/
  config.json
  baseline.json
  score/
    index.json
    回答名称/
      score.json
      REPORT.md
      session.md
  comparisons/A/
    comparison.json
    REPORT.md
    run.json                   本轮固定配置及原基线
```

只在标准生成失败时留下 `specialized/pending.json`，供查看原因、继续处理。
内部续跑文件集中在 `.state` 或对应评分目录，不放进回答目录。

## 第一次配置

在 QwenPaw 环境变量里配置 `DASHSCOPE_API_KEY` 和 `GITHUB_TOKEN`，脚本直接继承。
评分与分析使用当前 QwenPaw Agent 配置的模型；下面的 `--model` 指被测试的答疑机器人模型。

```text
/selflearn eval-init --alias-root /path/to/alias_qa_repo/alias --collection qwenpaw_faq_old --model qwen3.8-max --thinking false
```

可用 `--cases` 和 `--references` 指定通用题集与评分标准。标准目录需要
`references.jsonl`、`SCORING_RULES.md` 和 `SCORING_PROMPT.md`。
初始化保存配置，不会立即调用机器人。

## 第一步：切分历史、固定测试标准、生成 TXT

将原始文件放进 `analyze/history_jsonl/A/A.jsonl`，发送：

```text
/selflearn qa /完整工作区路径/analyze/history_jsonl/A/A.jsonl
```

程序先按对话关联固定约 8:2 切分（种子 42），然后两条支线独立进行：

- **训练 → TXT**：按容量分批集中阅读问答和轨迹，筛选回答不好、反复搜索等低效过程、高频问题或重复错误；归并主题后，只为可通过知识改善的问题做必要查证并生成 FAQ。没有信号则不凑 FAQ。FAQ 拼接为 TXT，不再调用独立核验 Agent。
- **测试 → 评分标准**：从留出历史恢复问题，生成有来源的参考答案和评分点。完成后做程序结构与引用校验，不再调用独立核验 Agent。

两条支线共用 `--concurrency` 上限，默认最多 3 个模型任务同时运行；设为 1 时串行。
筛选批次最多 10 条且限制摘要容量，完整轨迹按需回读。筛选阶段与每个 FAQ/测试标准生成阶段最多 7 分钟；归并最多 5 分钟。阶段内格式或引用错误可有限修正，不代表会无限重试。
训练工具只能读取训练历史，测试工具只能读取测试历史；测试标准不得读取新增 TXT。

Session 显示批次筛选结果、主题、FAQ 和测试标准结果，不刷工具日志。
过程保存在一份 `analyze/sessions/A.md`，内部续跑数据在 `analyze/.state/A-dataset.sqlite`。
程序校验只确认格式、引用与来源对应，不能保证模型语义判断正确。

一边失败不会取消另一边。TXT 已产出时可直接查看；有未完成主题时可能先导出部分 FAQ。
`round.json` 分别记录两条支线状态；仅两边完成后允许评测。重发原命令复用完成的结果，只继续失败部分。
语义上无法可靠出题的测试记录会注明原因；执行超时不会被当作有效排除理由。
原始历史、来源或训练 Skill 改变时请用新名称，防止混用不同版本的产物。

参数：`--name A_v2` 创建新批次；`--limit` 只限制训练历史数量；`--max-topics` 限制最终知识主题；
`--repo`、`--knowledge`、`--skills-dir`、`--offline` 仍可使用。未提供旧知识快照时，不声称已做全库查重。

### 从头重跑

最简单且保留旧成果的方式：原命令加 `--name A_v2`。
如果希望沿用 A 名称，不要只删除 `history_jsonl/A` 的衍生文件：检查点和 Session 也在其他位置。
对**尚未生成 TXT、没有 round.json 的批次**，可运行以下脚本，默认先列出范围：

```bash
.venv/bin/python scripts/selflearn/reset-qa.py /完整工作区路径/analyze/history_jsonl/A/A.jsonl
# 确认范围后执行备份迁移，原始 A.jsonl 留在原位：
.venv/bin/python scripts/selflearn/reset-qa.py /完整工作区路径/analyze/history_jsonl/A/A.jsonl --apply
```

旧切分、测试标准、检查点和 Session 会移入 `analyze/.archive/A/时间/`，同时备份原始历史，便于溯源。
脚本不会操作知识库、评分、基线或其他批次；有进行中的工作区任务会拒绝执行。
已有产出关联的批次请使用新名称，不清理历史引用。

## 第二步：双题集评测

```text
/selflearn evaluate /完整工作区路径/analyze/txt/A.txt
```

从当前 `baseline.json` 读取 old collection，复制并加入 TXT，创建 `qwenpaw_faq_A`。
不会直接向旧库追加。首次没有基线记录时，从初始化配置的 collection 开始。

| 题集 | Old | New |
| --- | --- | --- |
| generalized：固定通用题集 | 有效回答及同标准评分可复用 | 本轮生成、评分 |
| specialized：本轮测试题集 | 本轮生成、评分 | 本轮生成、评分 |

同一题集的新旧使用相同问题、配置、评分标准和复评方法；评分不提供 old/new 标签。
评分标准来源使用冻结快照。两套成绩分别报告，不合并成一个总分。
报告包括改善/退步题、新增关键错误、无法出题的历史，以及训练数据与通用题的明显重叠。
重叠检测目前包括规范化问题匹配，不能保证发现全部语义重复。

本地基线更新条件：

- 两套题集均非空，所需评分完整；
- generalized 的 new >= old；specialized 的 new >= old；
- 两套都没有新增关键错误，并且实际新增了知识。

没有至少 10 题的限制，小题集或两套同分也可更新；报告会保留实际题数。
不满足时保留旧基线，不删除候选库。通过时新 collection 成为下一轮基线，保留父版本与 TXT 来源。
下一轮继续复用当前基线有效的通用成绩；专用题集变化后重新测试当前基线，不跨批混用专用分数。

## 恢复、复用和单独评分

`/selflearn status` 主动查看状态，`/selflearn stop` 停止任务。
重新发送原命令可以继续未完成工作；切分、已固定标准和已完成评分不会重新抽样或覆盖。
已完成批次再次执行只返回原成果；修改历史、TXT、模型或标准时应使用新批次名称。
建库中断时保留失败候选，为避免重复追加，按提示用新名称从 qa 开始。

评分方法更新后，有效机器人回答仍可复用；程序会将需要重新评分的结果存到新的评分目录，保留旧评分。
旧版 TXT 没有训练/测试切分，不能直接作为新版双题集评测的输入。用原始历史和新名称重新运行 qa；历史结果保留。

单独对某份回答评分：

```text
/selflearn score /完整路径/回答.jsonl --references /完整路径/评分标准目录 --concurrency 4
```

## 可修改的 Skill

代码目录 `src/qwenpaw/selflearn/skills/`：

- `qa-history-split`：只识别对话和重复问题的关联，不评价回答好坏。
- `qa-build-specialized-eval`：从测试部分恢复题干，生成参考标准并由程序校验引用。
- `qa-history-select`：集中筛选训练历史并归并主题，不再调用逐条分析和单独的旧版提案流程。
- `qa-knowledge-write`：必要查证与 FAQ 写作一次完成；不再调用 qa-knowledge-verify。
- `qa-answer-score`：评分和复评。

终端也可使用项目中的 `./scripts/selflearn/qa-eval.sh qa ...` 或 `evaluate ...`，
与 Console 共用工作区、结果和基线记录。


### 人工跳过证据不足的 FAQ

当 TXT 已生成，但部分主题证据不足时，先查看待处理主题：

```text
/selflearn qa-skip /完整工作区路径/analyze/txt/A.txt
```

确认不把这些问题纳入本轮 TXT 后，使用同一命令加 `--all`；只跳过某条时加 `--topic 上一步显示的主题ID`，可重复指定。未加参数时只查看，不做修改。

跳过不会调用模型或改写 TXT。决定、时间、原因和历史依据保存在原批次的 round.json 与执行记录中，Session 中也会显示确认结果。全部未完成主题都已跳过且测试标准已完成时，可以继续 `/selflearn evaluate /完整工作区路径/analyze/txt/A.txt`。

此入口仅允许跳过证据不足项；超时、工具错误和测试标准生成失败仍需重试。已跳过的主题不再自动重试，需要重新研究时请另开批次。旧批次有完整执行记录时也可直接使用，无需重新生成已有 FAQ。


如果人工编辑过 TXT，请在确认跳过的命令后加 `--accept-edited-txt`：

```text
/selflearn qa-skip /完整工作区路径/analyze/txt/A.txt --all --accept-edited-txt
```

这表示使用当前修改版继续评测。修改前后的 TXT 快照保存在该批原有 SQLite 执行记录中，`round.json` 登记版本变化和确认时间，不额外散落文件。命令不会覆盖当前 TXT，也不会重跑模型。已经完成的批次若再修改 TXT，应另开批次以保留旧评测关联。


### 训练集补充评价

```text
/selflearn train_eval /完整工作区路径/analyze/txt/A.txt
```

默认使用本批双题集比较记录中的旧库与新库，即使 baseline 后来更新，也不会换成新的对比对象。若尚未做双题集评测，可以指定已经存在的两个知识库：

```text
/selflearn train_eval /完整工作区路径/analyze/txt/A.txt --old-collection qwenpaw_faq_old --new-collection qwenpaw_faq_A
```

命令从 A_train.jsonl 恢复题目，独立查证并生成评分标准，再分别运行、评分旧库与新库，给出总分、改善题、退步题和新增关键错误。默认评分并发 4，可用 `--concurrency` 调整。标准生成不读取新增 TXT 或新旧回答；知识证据不足的题会列出并排除，执行失败需重试。

这属于训练集诊断，不能代表泛化效果，也不参与 baseline 更新或服务器上传。同参数重跑复用已完成项，修改配置后可用 `--name A_train_eval_v2` 保存新的一次评价。

产物位置：
- 题集及评分标准：`analyze/history_jsonl/A/train_eval/A_train_eval/`
- 回答：工作区 `eval/` 下，以训练题集、collection、模型命名的 JSONL。
- 评分：`selflearn/score/回答文件名/score.json`，同时登记 `score/index.json`，标记 `suite=train`。
- 对比结论：`selflearn/comparisons/A_train_eval/comparison.json` 与 `REPORT.md`。
