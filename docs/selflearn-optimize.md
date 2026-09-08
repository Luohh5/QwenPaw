# Self-learn Optimize（初版）

Optimize 将 Analyze 的一个提案变成可检查、可重建的本地 Harness 候选。
不发布、不 push、不修改原项目，不把静态检查通过等同于效果提升。
需要本机安装 Git，不需要 GitHub 账号、远端仓库或项目本身是 Git 仓库。

## 在 QwenPaw 中启动

更新代码并重启使用此源码的 QwenPaw 后，在 Console 聊天框输入：

```text
/optimize "/绝对路径/improvement_proposals.json" --proposal 1
/optimize status
```

命令会启动后台任务，使用当前 Agent 的模型、thinking 和上下文预算，但使用独立会话。
它不会把当前聊天记录带入优化，也不会加载普通聊天的插件或写入工具。
继承工具结果裁剪、Scroll，并将缓存和历史隔离在本次运行目录。
文件读写跟随当前 worktree；换版本不会重建会话，也不会改变整个服务进程的工作目录。

参数：

- `--proposal 1`：文件中的第几项提案，从 1 开始，默认第一项。
- `--base auto`：默认值。Agent 按需查阅历史、判断提案与代码的关系，再选择起点。
  不是固定选最新版本或叶子，也不把自己的判断当作评测分数。
- `--base snapshot`：指定 Analyze 的冻结快照作为初始起点，同一快照只导入一次。
- `--base 候选ID`：指定保存版本为初始起点；也可使用本 Harness 的完整 commit SHA。
  Agent 先核查用户指定版本，之后仍可说明原因、保存或放弃尝试，再换路线。
- `--targets "路径"`：指定可编辑目标清单；默认沿用 proposal 中的 `run.targets_source`。
  新清单的目标路径、用途和允许范围必须仍与原快照一致。
- `--also-target 目标ID`：明确允许同时编辑另一个目标，可重复使用。
  默认仅允许 `primary_target`；不能通过此参数跳过该目标自身的编辑规则。

相对文件路径从当前 Agent 的项目目录解析；没有项目时从工作目录解析。
输入文件旁边必须保留 Analyze 的 `cases.json` 和 `harness_snapshot.json`，系统会检查内容指纹。
`needs_evidence`、`no_change` 提案不会启动模型，也不强制每批次生成修改。

推荐先运行已有的 `53f1a5a5ebb349ed811b44397df661a2` 文件的第 1 项。
`cfe99ac32edd49cbb906f9d2c68e5096` 的第 1 项可以另起一个候选进行对照；
其第 2 项标记为 `needs_evidence`，不能直接用于自动修改。
`15118e484c58417b94c3af8e0bed19f1` 涉及工具允许说明，当前 verification
编辑规则不允许改变 Tool Usage；Agent 应报告 `needs_scope`，不能偷偷扩大权限。

## 输入和运行过程

每次只给模型一个提案的 problem、原因假设、修改意图、短引用、验证计划、
支持/反例索引，以及全部 limitations；不输入顶层 summary、完整运行元数据、
完整 Harness、全部案例或 signal_strength。alternatives 按需读取。

1. 校验提案、冻结数据及目标清单，导入不可变的基线。
2. Agent 用 `read_finding` 读取支持项和反例，用 `list_versions` 分页查询相关历史，
   用 `read_version` 按需查看某个版本的来源、父版本、差异或指定文件。
3. `start_candidate(base, reason)` 选择起点并创建独立 worktree；显式 `--base` 已由程序创建。
4. 用 `read_harness` 搜索、分页读取当前文件；重复片段去重，必要时 `reread=true`。
   换 worktree 后必须重新读取，旧版本的读记录不能充当新版本的核查记录。
   用 `read_evidence` 定向读取原始 episode，不默认展开完整 diff。
5. 用 `edit_harness` 唯一原文替换；每次替换先检查允许范围，再写入候选。
6. 用 `inspect_candidate` 查看差异及固定检查。
7. 必要时保存检查点、放弃或换路线，以及合并、选择性应用和撤销（见下节）。
8. Agent 返回实际修改说明、限制与最终候选 ID。程序检查并保存最终修改，
   仅所选版本标为 `ready_for_evaluation`；无必要改动时可以不产生新版本。

### Agent 的自主版本操作

| 工具 | 作用 |
|---|---|
| `list_versions` / `read_version` | 按目标、问题查历史；分页读取摘要、差异和文件，不把版本树塞入 prompt |
| `start_candidate` | 从 snapshot、已有候选或完整 SHA 创建新的工作目录 |
| `checkpoint_candidate` | 保存不可改写的检查点；继续编辑需要从它创建后继 |
| `reject_candidate` | 放弃本次当前尝试，保留草稿和原因；不否定其他运行的历史候选 |
| `integrate_candidate` | 在当前干净草稿中执行 merge、pick 或 revert，保留结果供检查，不自动提交 |

默认一轮只走一条路线、选一个最终候选，不强制产生多个。若先保存 A，尝试 B 后发现
重复，可以放弃 B，最终返回 A 的 `candidate_id`。中途检查点不等于最终候选。
换版本前必须保存或明确放弃脏草稿；放弃不会删除文件。

merge 合并两个分支；pick 只应用来源候选相对父版本的差异，不带入其祖先修改；
revert 只撤销该差异。合并冲突可以通过 `read_harness`、`edit_harness` 修正，
`inspect_candidate` 即使检查失败也返回差异。无法在授权范围内解决时放弃草稿。

新起点相对 Analyze 基线的修改必须仍符合当前目标清单的开放区域，受保护代码、
工具权限等不兼容时不能直接复用。当前草稿只能编辑本轮 `editable_targets`；
合并不会继承历史候选更宽的权限。没有任意 shell、Git 命令或发布工具。

固定检查覆盖：允许的文件和文本区域、Python 语法、f-string 插值、预算默认值、
文件删除/符号链接/执行权限及未解决的合并冲突。不会执行 Harness Python。
这些检查不验证模型回答质量，也不能证明新 Skill 已正确加载；它们由后续 Evaluate 负责。
例如没有历史工具日志时，仍不能把“未展示搜索证据”写成已证明的“没有搜索”。

## 可编辑范围

`scripts/selflearn/harness_targets.json` 新增 `edit_rules`，旧 proposal 无需重跑 Analyze。
规则来自当前目标清单，保存到实验记录；Agent 不能自行改规则。

| 目标 | 自动编辑范围 |
|---|---|
| `review.task_prompt` | `_build_enhanced_prompt`、`_build_fallback_prompt` 的返回文本；保持插值和执行代码 |
| `review.verification` | SOUL_MD / AGENTS_MD 的 Review Methodology 小节；其他小节不变 |
| `review.context` | 四个 MAP 预算环境变量的正整数默认值 |
| `review.skill`、`review.project_knowledge` | 指定的 Markdown 文件；加载接入需要另行授权相应目标 |
| `review.tools` | 未开放自动编辑，仍需人工工程处理 |

自定义目标也需要显式配置 `edit_rules`：

```json
{"sections": ["Review Rules"]}
```

表示只允许编辑 Markdown 的该标题下内容，直到下一个同级或更高层标题。
`{"sections": ["*"]}` 表示允许整个 Markdown 文件；只有确实希望开放全文时才使用。
Python 支持 `function_returns`、`string_sections`、`env_defaults`，参考现有清单。
初版不提供任意 Python 工程修改或通用 JSON 配置修改。

## 查看、比较和管理版本

```text
/optimize list
/optimize show 候选ID
/optimize diff 候选ID
/optimize diff 候选A 候选B
/optimize stop
```

- `show` 显示候选记录，包含父版本和完整 commit SHA。
- 单参数 `diff` 比较候选与其起点；未提交的候选显示当前草稿。
- 双参数 `diff` 比较两个已保存版本。
- `stop` 保留草稿和日志，不自动 commit。重启后 `status` 会识别中断的任务。
- 同一工作空间一次运行一个 Optimize；Agent 自主选择相关版本，不固定选“最新”。

选择起点、拒绝路线、合并和撤销依据代码与当前提案的关系，不要求先完成 Evaluate。
这些是实现方案判断，不证明质量提升。基线与全部历史保留，允许从祖先重新分叉；
每轮最终版本仍要交给独立 Evaluate 检查效果和回归。

### 合并和选择性撤销

```text
/optimize merge 候选A 候选B
/optimize pick 候选A --base 候选B
/optimize revert 候选A
/optimize revert 候选A --base 候选B
```

- merge：从 A 创建新的 worktree，合并 B；不修改 A、B。
- pick A --base B：从 B 创建新候选，只应用 A 相对其父版本的改动，不带入 A 的其他祖先改动。
- revert A：从 A 创建新候选，撤销 A 相对于其父版本的改动。
- revert A --base B：从 B 创建新候选，只撤销 A 的那次修改，保留其他兼容改动。
- 两个候选必须属于同一 Harness；冲突保留在新 worktree，绝不自动选择任意一边。
- 合并/撤销后的版本重新等待评测，不能沿用来源候选的评测结论。

人工编辑冲突文件、移除冲突标记后，可以保存：

```text
/optimize save 候选ID
```

save 仍执行相同范围检查；不能用来改写已保存的候选。如果需要新的修改，使用
`/optimize ... --base 候选ID` 创建后继。save 也可保存失败或停止后经人工检查的草稿。

```text
/optimize reject 候选ID
/optimize clean 候选ID
```

reject 只标记放弃，不删除记录。clean 仅移除没有未保存改动的工作目录；
commit、分支和实验记录保留，可以从该候选重新创建 worktree。脏目录不会强制清除。

## 产物

```text
<workspace>/selflearn/
  optimize_status.json
  optimization_runs/<run_id>/
    run.json
    cases.json
    harness_snapshot.json
    manifest.json
    input.json
    trace.json
    context/
  harnesses/<harness_id>/
    harness.json
    repo.git/
    worktrees/<candidate_id>/
    experiments/<candidate_id>/
      candidate.json
```

运行级证据、模型记录和 Scroll 缓存只保存一份，候选用 `run_output` 指向所属运行。
`run.json` 记录尝试列表、当前候选、`selected_candidate_id` 及操作理由；
status 会显示运行记录和最终选择。旧候选目录仍可查询和复用，无需迁移。
手动命令的合并、撤销不调用模型，来源仍沿 source_candidates 追溯。

candidate.json 的主要字段：

- `candidate_id` / `harness_id`：候选和 Harness 标识。
- `base_revision` / `candidate_revision`：起始 commit、保存后的 commit；未保存时后者为空。
- `status`：running / checkpoint / ready_for_evaluation / not_selected /
  no_change / needs_evidence / needs_scope / stopped / interrupted / failed / conflict / rejected。
- `worktree` / `output`：候选文件目录、此记录位置。
- `task`：来源 proposal 的路径、内容指纹、序号、原始提案和限制；或版本操作及来源候选。
- `targets`：本次明确开放的目标及编辑规则。
- `checks`：程序生成的检查结论及修改文件；只表示静态检查，不是效果分数。
- `result`：最终 Agent 的 status、summary、limitations、candidate_id。
- `run_output` / `binding`：所属运行，以及模型、项目版本、Harness 快照、案例指纹与时间。
- `integration` / `rejection_reason`：合并操作及来源，或放弃该尝试的原因（按需出现）。
- `summary` / `error`：保存说明或失败原因。
- `created_at` / `finished_at`：本次实验时间。

Git 记录实际文件和父子关系；不把完整 diff、版本图重复塞入 JSON 或模型上下文。
manifest 另记录 thinking、上下文设置与实现指纹。历史摘要按需返回绑定信息，
Agent 需对照本轮模型、项目和时间重新核查；缺失的旧元数据按未知处理。
项目 Git SHA 是本次运行时可读取的版本，不代表历史 PR 当时的环境；无 Git 时为 null。
`tracked_changes` 标明项目是否还有已跟踪文件的未提交修改；有修改时 SHA 不能完整代表项目内容。
这些绑定用于追溯，不是完整运行环境快照，也不能代替独立评测和旧经验的重新校准。

当前不创建 active.json，也没有 release / push 命令。后续发布需要独立评测、人工批准
以及实际运行端的加载/部署接入；不会通过改一个指针假装 Review Bot 已经更新。
