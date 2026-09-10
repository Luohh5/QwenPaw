# 答疑机器人优化：使用说明

所有命令在 **QwenPaw Console 的 session** 中发送。分析与评分过程会在当前对话显示，
并各保存一份可阅读的过程文件。当前流程只操作本地，不上传服务器。

以下路径都相对于 `/Users/aoli/.qwenpaw_selflearn/workspaces/default`。

## 文件放在哪里

| 内容 | 位置 |
| --- | --- |
| 历史问答 | `analyze/history_jsonl/2026-08-20.jsonl` |
| 最终新增知识 | `analyze/txt/2026-08-20.txt` |
| 分析过程 | `analyze/sessions/2026-08-20.md` |
| 机器人回答 | `eval/回答名称.jsonl` |
| 某份回答的评分 | `selflearn/score/回答名称/score.json` |
| 评分报告 | `selflearn/score/回答名称/REPORT.md` |
| 评分过程 | `selflearn/score/回答名称/session.md` |
| 分数登记表 | `selflearn/score/index.json` |
| 当前基线 | `selflearn/baseline.json` |
| 模型、题集等配置 | `selflearn/config.json` |

日期范围用 `2026-08-20_2026-08-26`，单日用 `2026-08-20`。
同日期重做一个版本，用 `--name 2026-08-20_v2`，避免覆盖已有成果。
候选知识库为 `qwenpaw_faq_2026-08-20`，与历史和 TXT 的日期对应。
回答名包含题集、collection、模型和 thinking，例如：
`cases_dev--qwenpaw_faq_2026-08-20--qwen3.8-max--thinking_false.jsonl`。

正常使用只需看 TXT、session 和评分报告。程序另保留合并后的隐藏检查点数据库，
用于记住已完成的题、保存来源快照和中断后续跑；不会再留下逐题的 input、trace、context 目录。
隐藏数据不是另一套需要人工查看的输出。

## 第一次初始化

在 QwenPaw 环境变量中配置 `DASHSCOPE_API_KEY`、`GITHUB_TOKEN`。评测脚本直接继承。
评分模型使用当前 QwenPaw 的模型设置；下面的 `--model` 是被测试的答疑机器人模型。

```text
/selflearn eval-init --alias-root /Users/aoli/projects/alias_qa_repo/alias --collection qwenpaw_faq_old --model qwen3.8-max --thinking false
```

这一步保存配置。第一次 evaluate 会生成并评分基线；已有经过核对、已登记的回答和评分会复用。
`--baseline-answers` 可以提供已有结果，但自动采用需要同名运行 manifest 证明配置和知识库一致。
单独人工生成、没有这些记录的回答不会被悄悄当作当前知识库的基线。

本次已有数据已单独迁移：140 条回答和 132 条首评保留；旧基线尚未完成评分，不能冒充已有最终分。

## 第一步：历史问答生成 TXT

```text
/selflearn qa /Users/aoli/.qwenpaw_selflearn/workspaces/default/analyze/history_jsonl/2026-08-20.jsonl
```

也可提供其他位置的 JSONL，程序按文件名或 `received_at` 等日期字段推断日期，
复制到统一目录。没有可靠日期时，加 `--name 日期范围`，不会凭空猜日期。
可选参数：`--repo`、`--knowledge`、`--skills-dir`、`--limit`、`--max-topics`、`--concurrency`。

分析、提出建议、查证和核验仍由可编辑的 Skill 控制：
`src/qwenpaw/selflearn/skills/qa-history-analyze`、`qa-knowledge-propose`、
`qa-knowledge-write`、`qa-knowledge-verify`。
只导出有来源且通过核验的内容。没有通过的内容时 TXT 可以为空，不拿未核实的内容凑数。

## 第二步：评测新增知识

通常一条命令即可：

```text
/selflearn evaluate /Users/aoli/.qwenpaw_selflearn/workspaces/default/analyze/txt/2026-08-20.txt
```

它依次完成：

1. 从 `baseline.json` 读取当前基线；尚未建立时用配置中的 `qwenpaw_faq_old`。
2. 复制基线，加入 TXT，得到按日期命名的候选库。
3. 复用基线回答及同标准的有效评分，只生成、评分新库需要的内容。
4. 比较新旧成绩，保存候选评分目录下的 `comparison.json`。
5. 满足“评分完整、总分提高、无新增关键错误、有新增知识”时，更新 `baseline.json`。
   否则保留原基线。新旧知识库均保留，供你之后清理。

程序不重命名 `qwenpaw_faq_old`。基线更新后，下一轮从新基线复制，不会又回到最初的旧库。
已完成的同名评测再次执行，只显示原结果；要新开一轮需指定新日期或 `_v2` 名称。
单次得分提高表示这轮测试提高，不代表已经证明线上效果稳定提高。

## 也可以逐步执行

```text
/selflearn prepare /Users/aoli/.qwenpaw_selflearn/workspaces/default/analyze/txt/2026-08-20.txt
/selflearn batch --collection qwenpaw_faq_2026-08-20
/selflearn score /Users/aoli/.qwenpaw_selflearn/workspaces/default/eval/cases_dev--qwenpaw_faq_2026-08-20--qwen3.8-max--thinking_false.jsonl
```

独立评分的输入就是回答 JSONL 和评分标准目录。
标准目录包含 `references.jsonl`、`SCORING_RULES.md`、`SCORING_PROMPT.md`：

```text
/selflearn score /完整路径/A.jsonl --references /完整路径/评分标准目录 --concurrency 4
```

输出在 `selflearn/score/A/`；最终分数、逐题理由、抽检记录存于 `score.json`，
`index.json` 登记回答路径及校验值、标准、评分模型、成绩和评分文件位置。
默认最多同时评分 4 题，可设为 1–8。每题仍使用独立上下文；随机抽检的第二次评分不会看到首评。
同分且关键错误判定相同，不会仅因理由措辞不同而额外调用模型核查。

同一基线在回答、标准和评分模型未变时复用成绩。
换标准或评委模型后，先为旧回答生成一个新版本评分，再比较：

```text
/selflearn score /完整路径/A.jsonl --references /完整路径/新标准 --name A_v2
```

旧评分保留，不与新标准混算。新的评分索引会让后续 evaluate 找到可比较的版本。

## 停止与恢复

`/selflearn status` 查看状态；`/selflearn stop` 停止任务。
回答或评分中断后，再执行原来的命令即可续跑，不再需要找一串随机 run_id。
某题失败时会先保留其他题的结果，修正问题后只补未完成部分。
建库中断属于例外：为了避免重复追加，保留失败候选，并提示使用 `_v2` 创建新候选。

引用原句仅允许恢复唯一匹配的 Markdown 反引号；不能改写证据。
其他引用不匹配会明确指出字段和可回读原文，不能为了继续而给题目随意补分。

## 旧数据在哪里

本次目录整理的记录在 `selflearn/migration.json`，包含新旧路径映射和文件校验值。
旧目录合并到 `selflearn/archive/*-before-layout.zip`。归档逐文件核对后，原目录整体移入
`selflearn/archive/*-originals/`，全部原文件保留，没有删除。
原候选 collection 保留；按日期命名的候选是它的逐点一致副本，映射记录在迁移文件与回答索引中。
旧首评原样导入，独立复评没有执行的部分仍标为未完成。
旧分析能恢复的结论和来源合并进分析 session 文件；没有保存的对话不会补造。
清理 collection 前，请先看 `baseline.json`，不要删掉当前基线。

## 终端执行

```bash
cd /Users/aoli/projects/selflearn
./scripts/selflearn/qa-eval.sh evaluate /Users/aoli/.qwenpaw_selflearn/workspaces/default/analyze/txt/2026-08-20.txt
```

终端与 Console 使用同一目录与登记表。脚本默认使用 `~/.qwenpaw_selflearn`，
也可通过 `QWENPAW_WORKING_DIR`、`QWENPAW_QA_EVAL_AGENT` 指定其他实例或 Agent。
代码更新后需要重启 QwenPaw 服务，刷新网页不会加载新代码。
