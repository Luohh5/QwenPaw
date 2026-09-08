# Analyze Agent：逐条分析与跨案例归纳

## 在 QwenPaw 中启动

使用本分支代码启动 QwenPaw 服务，在 Console 选择 QwenPaw 后端且已配置模型的
Agent。输入 `/` 可以看到 `/analyze`；发送不带参数的 `/analyze` 可查看帮助。
源码更新后需要重启服务；若使用打包的 Console，命令菜单变化还需要重新构建前端。
即使旧前端没有菜单项，也可以直接输入完整命令。

注意不要误启动其他副本。本目录的 `.venv-test` 原先绑定过另一份项目；如果使用它，
可在本项目根目录用 `PYTHONPATH=src .venv-test/bin/python -m qwenpaw app` 启动，
或在实际使用的环境中重新安装本目录源码。不要与占用相同端口的旧服务同时启动。

```text
/analyze "/Users/luohh/Documents/Qwenpaw/QwenPaw-main_selflearn/work/selflearn/episodes.v3.selflearn.jsonl" --limit 20
/analyze status
/analyze stop
```

`--limit 20` 表示只处理文件前 20 条；省略则处理全部记录，不做信号规则筛选。
路径是 **QwenPaw 服务所在机器** 上的文件，不是浏览器上传。相对路径从当前 Agent 的
`project_dir` 解析，没有项目目录时从 Agent 工作目录解析。路径有空格时加双引号。
初版仅允许 Console 使用，群聊渠道不能通过该命令读取服务器文件。

命令立即返回并后台执行，同一 Agent 同时只运行一个分析批次。使用 `/analyze status`
查看实时进度和结果路径；完成后不会自动把每条结果刷到当前聊天。
停止、关闭客户端或重启服务都不删除已落盘结果；客户端关闭不停止后台任务。
服务重启后需要重新发送命令，不会自动恢复未完成的模型调用。

## 运行逻辑

提示词通过统一的 `instructions(...)` 加载：先读共享的 `BACKGROUND.md`，
再拼接当前阶段指令。第一阶段使用 `AGENTS.md` 和 `github_review.md`，
第二阶段使用英文的 `SUMMARIZE.md`。共享背景只维护一份；后续阶段也可使用同一入口。
第二阶段按 Task、Inputs、Evidence Access、Workflow、Output Contract 组织，
说明文字仍要求用中文输出，原文引用不翻译。

1. 解析路径，确认模型已配置，通过现有 TaskTracker 启动后台任务。
2. 读取预处理后的 JSONL；raw collector 输出不是这个入口的输入。
3. 固定本批次的分析指令和模型配置，根据输入内容及分析版本跳过已成功完成的记录。
4. 每条 episode 新建一个 QwenPawAgent 分析实例，使用当前 Agent 配置的模型，
   不加载当前聊天、长期记忆、业务 Skills、写入工具或生产插件的结束钩子。
5. 读取完整的 AI 输出、后续事件和质量信息。小 diff 随输入传入；超过 16,000 字符
   的 diff 用位置说明替代，并提供绑定到本条原始数据的只读 `read_episode` 工具。
   工具按字段、关键词和行号读取，每次最多返回 200 行，不丢弃原始 diff。
6. 通过 QwenPaw/AgentScope 的结构化输出能力得到标签。程序只校验结构、字段位置和
   引用是否存在，不用关键词代替模型的语义判断。引用校验会一次列出全部错误的
   `findings[i].evidence[j]`、原文位置和失败引用；最多修正一次，不放宽逐字匹配。
7. 每条成功结果立即追加到 JSONL。每次回复最多 12 轮工具循环，每条案例最多 10 分钟；
   失败单独记录并继续下一条，不能把执行失败伪装成“没有反馈”。

第一轮只做单案例分析，不聚类、不判断重复发生频率、不读取目标 Harness，
也不生成修改任务单。下一阶段才结合 Harness 目标清单做跨案例归纳。
当前工具只读本条输入，不联网、不执行代码；缺少历史文件时输出证据缺口，
不会拿当前仓库文件冒充当时版本。

### 上下文管理（两个阶段共用）

分析会话复用 QwenPaw 的工具结果裁剪、完整结果缓存及 Scroll，不加载生产 Skill，
也不开放 Shell、Python 执行和文件修改。读取当前 Agent 的 `running.light_context_config`：

- `tool_result_pruning_config` 控制是否裁剪、结果字节上限和缓存保留时间。
- `strategy=scroll` 且 `context_compact_config.enabled=true` 时接入 Scroll，
  通过现有结构化 `recall_history` 回读；不开放 Python REPL 或无沙箱执行。
- `strategy=native` 时使用底层压缩；关闭 `context_compact_config.enabled`
  则不自动压缩。工具裁剪仍由它自己的 enabled 开关控制。

每条第一阶段分析将历史库/缓存放在 `traces/<session_id>/`，独立于普通聊天。
第二阶段目前用于开发调试，在当前聊天中运行，继承该聊天的历史和工作区上下文存储配置。
因此旧聊天可能影响归纳；比较模型或 Harness 时建议使用新的、专用的调试聊天。
不通过聊天入口调用的 `run_summary` 仍使用 `proposals/<run_id>/context/` 隔离存储。
完成、异常和取消都会关闭管理器。历史库与缓存按原有保留时间清理。

裁剪前完整工具结果会先保存。受限 `read_file` 只能读当前分析所配置的
工具结果缓存和对话归档目录，不能读取任意文件或通过符号链接越界。
取证工具以多行 JSON 返回，使现有按行裁剪器能够生成续读提示；原始字段值不变。
超长单行可用 `start_char` 分页：按返回的 `next_start_char` 继续，并保持相同的
`file_path/start_line/end_line`。缓存行号不是原始证据行号；回读内容仍须遵守原文引用校验。

不会自动关闭 thinking 或修改模型/超时/裁剪预算。默认裁剪上限是 50,000 字节，
如需更小的工具预览，可在当前 Agent 的 `tool_result_pruning_config` 中调整
`pruning_recent_msg_max_bytes`（例如调试时尝试 6,000）；Scroll 模式也对旧预览
使用此上限，不使用 `pruning_old_msg_max_bytes`。
Scroll 的触发阈值是 `compact_threshold_ratio × model.context_size`，并优先保护
当前尚未结束的任务；接入这些能力不保证消除模型流超时，定向读取仍然重要。

引用应使用连续短句或单行代码，保留原文格式，不添加省略号、不拼接片段。
分析指令还明确区分 PR 中的代码/测试与 AI 审查行为，不能把“AI 认可了错误测试”
写成“AI 编写了错误测试”。引用校验通过不等于语义判断正确，仍需抽查分析结果。

第二阶段已经提供，入口、数据范围和输出见下文“第二阶段”。

### 流空闲超时的时间诊断

在启动 QwenPaw 服务的终端中设置 `QWENPAW_LLM_TIMING_LOG=1`，然后按原来的方式
重启服务并重新运行分析。默认关闭；设为 `0` 或移除变量后重启即可关闭。
不需要调整 thinking、超时、重试或 `/analyze` 命令参数。

日志写入现有服务日志，前缀为 `llm_timing`，可从默认日志中过滤：

```sh
rg 'llm_timing' /Users/luohh/.qwenpaw/qwenpaw.log
```

记录三个位置：

- `raw`：DashScope 返回的数据包，已被 OpenAI SDK 解码，但尚未经过 AgentScope
  转换。记录字段名、thinking/文本/工具参数的字符数、请求 ID、工具调用 ID。
  这不是 TCP/SSE 字节抓包，不能据此断言网络完全没有字节到达。
- `sdk`：AgentScope 的 DashScope 适配器转换后的内容块，记录类型及相同的长度信息。
  `stream_id` 关联同一次请求的 raw/sdk；`seq` 是各层自己的序号，不要求一一对应。
- `tool_start` / `tool_end`：self-learn Agent 的实际工具执行路径，含工具封装层，
  不包含模型生成参数的时间；记录名称、调用 ID、会话 ID、耗时和结果状态。
  通过 `call_id` 与前两层对应，适用于 `GenerateStructuredOutput` 和其他分析工具。

`at` 是 UTC 时间；`gap_ms` 是与同层上一条记录的间隔；`elapsed_ms` 是累计耗时，
均包含调度/日志等开销，不等同于服务端纯计算时间。`stream_start` 从接口返回流后计时，
不覆盖此前的请求等待；`stream_end` 记录完成、异常或取消类型。取消也可能是上层
流空闲超时触发，需结合本次任务的错误消息判断。

判断时看具体内容：raw 持续有非空 thinking 或参数而 sdk 没有对应内容，才值得怀疑
转换层；空包和 usage 包不代表有效模型输出。若只有工具名称、没有同一 `call_id` 的
`tool_start`，说明尚未进入实际工具执行。只有出现 `tool_start` 后才开始衡量工具耗时。
日志不记录正文、工具参数值、密钥或异常正文；开启时每个数据包都会产生日志，排查后关闭。

## 结果文件

输出在当前 Agent 工作目录的 `selflearn/` 下，不覆盖原始数据：

```text
selflearn/
├── status.json
└── <批次标识>/
    ├── manifest.json
    ├── episode_analyses.jsonl
    ├── errors.jsonl          # 发生失败时才生成
    └── traces/
        ├── <session_id>.json
        └── <session_id>/    # 独立历史库和裁剪缓存
```

- `status.json`：最新批次的状态、输入和结果路径、总数、本次完成数、复用数、失败数、
  当前案例、起止时间和任务级错误。`completed_with_errors` 表示遍历结束但有失败案例。
- `manifest.json`：输出格式版本、输入文件路径、分析实现与指令及配置的摘要、选择的模型。
- `episode_analyses.jsonl`：供人工检查和后续归纳使用，每行一条成功分析。
- `errors.jsonl`：失败案例的 `episode_id`、`session_id`、错误和发生时间。
- `traces/<session_id>.json`：案例 ID、读取数据工具的参数、原始最终回复；
  `replies` 可能包含修正前后两次回复。这是诊断记录，不是完整生产运行轨迹。

相同输入路径和分析配置复用同一输出目录。按 `episode_id + 输入内容摘要` 续跑，
失败案例下次会重试；改变指令、实现或模型配置会使用另一个目录。
同一文件中的某条内容变化时，新分析追加保留；后续读取者按 `episode_id` 取最新结果。

## episode_analyses.jsonl 的全部字段

| 字段 | 含义 |
|---|---|
| `episode_id` | 回查原始案例的标识，由程序复制，不由模型编造 |
| `findings` | 按具体观点拆分的分析列表，可以为空 |
| `findings[].ai_claim` | 对应 AI 的原结论或遗漏事项；没有对应结论时为 null |
| `findings[].relation` | direct 直接反馈；indirect 间接反馈；unrelated 无关；unclear 不明确 |
| `findings[].stance` | support 支持；reject 反驳；clarify 澄清；neutral 中性；unclear 不明确 |
| `findings[].signal_strength` | strong 强、medium 中、weak 弱、none 无信号；表示反馈明确程度，不是正确率 |
| `findings[].feedback_reliability` | supported 有证据支持；contradicted 与证据冲突；unverified 未核实 |
| `findings[].ai_assessment` | correct 正确；incorrect 错误；missed 遗漏；uncertain 无法判断；not_applicable 不涉及正确性 |
| `findings[].evidence` | 支撑这个分析项的证据列表 |
| `findings[].evidence[].pointer` | 原始 episode 字段位置，如 `/post_review_events/1/text`；数组从 0 计数 |
| `findings[].evidence[].quote` | 该位置中真实存在的原文片段；非文本值使用 JSON 表示 |
| `findings[].reason` | 简短说明反馈对象、判断依据和局限 |
| `findings[].problem` | 可复用的问题描述，不是已确认的 Harness 根因；没有问题时为 null |
| `summary` | 这一条案例的简要总结 |
| `missing_evidence` | 还缺哪些证据，可以为空 |
| `run.input_hash` | 原始案例内容摘要，用于检查输入是否改变 |
| `run.session_id` | 这次独立分析的标识，也对应 traces 文件名 |
| `run.model` | 分析结束时模型包装器报告的模型名；不代表每次回退调用的完整清单 |
| `run.at` | 结果保存时间，使用 UTC |

## 用真实案例理解标签

以下是根据现有数据写的说明示例，不是已运行模型的输出，也不是硬编码的标准答案。

### #6065：人工反馈明确，但不同观点要分别核查

`agentscope-ai/QwenPaw#6065:comment-4966350740` 中，AI 认为被删除的模块没有调用方。
`/post_review_events/1/text` 的人工评论指出 `builder.py` 仍然导入该模块。

这一观点可以标记 `relation=indirect`、`stance=reject`、`signal_strength=strong`。
如果输入没有提供当时的完整调用方文件，`feedback_reliability=unverified`、
`ai_assessment=uncertain`，并在 `missing_evidence` 中要求该历史文件。
`problem` 可写“可能遗漏删除模块后的跨文件引用”，不能写“已确认 Skill 未执行搜索”。

同一评论还断言后面的 `pytest.skip()` 使前面的 `importorskip()` 不可达。
这一顺序判断可以对照 diff 独立反驳，不得因为同一条评论的另一个观点可信就整体采纳。

### #7401：接受一部分，澄清另一部分

`agentscope-ai/QwenPaw#7401:comment-5519798122` 中，作者明确接受关闭生命周期建议，
又说明异常处理只部分采纳：记录日志，但仍然重新抛出异常。
应该拆成不同 findings，不能把整条回复统一标为“接受”。
“adopted” 可以明确说明作者接受意图；但该案例缺少审查 diff 和后续完整修改内容，
不能进一步声称实现已正确修复或所有测试已被分析 Agent 验证。

### #7112：点赞不是正确性证明

`agentscope-ai/QwenPaw#7112:comment-5365085782` 有直接针对 AI 消息的 `+1`。
其证据可以引用 `/agent_output/reactions/0/reaction`，原文为 `+1`。
它是 `direct / support / medium`，但没有说明具体认可哪一结论，
因此不能据此把整份 AI Review 标为 correct。
后续人工 review 的 `LGTM.` 也不应自动视为在认可这条 AI 消息。

## 第二阶段：跨案例归纳

第一阶段完成后，用 `/analyze status` 找到 `episode_analyses.jsonl`，再发送：

```text
/analyze summarize "第一阶段结果的绝对路径/episode_analyses.jsonl" --targets "/Users/luohh/Documents/Qwenpaw/QwenPaw-main_selflearn/scripts/selflearn/harness_targets.json"
```

第二阶段直接在当前聊天中流式运行，显示任务输入、工具调用/结果、模型输出、
校验反馈和最终任务单；继承当前聊天历史，结束或中断时走普通聊天的会话保存流程。
它只使用专用分析工具，不继承普通聊天的可执行工具和业务 Skill。
可点击聊天停止按钮，也可用 `/analyze stop` 停止；`/analyze status` 查看状态和结果路径。
断开客户端沿用普通聊天的后台运行、重连机制，不会另开一个隐藏的分析任务。
同一 Agent 不会同时启动两个分析任务。每次仍新建输出目录，不覆盖上一轮任务单。
只使用第一阶段已有的成功结果，不要求先跑完全部 146 条，也不自动补跑失败案例。

输入结果旁必须保留第一阶段的 `manifest.json`，其 `source` 指向原始预处理数据。
已有第一阶段结果可直接使用，不需要因新增第二阶段而重做打标。
若原始案例内容已经变化，则先重跑相应第一阶段，避免把旧标签套到新数据上。

### 使用哪些字段

| 数据 | 用途 |
|---|---|
| `episode_id`、`findings` 的原数组位置 | 唯一定位某条分析项 |
| `ai_claim`、`relation`、`stance` | 确认反馈指向什么、表达什么态度 |
| `feedback_reliability`、`ai_assessment` | 区分人工观点可靠性与 AI 结论正确性，不作为标准答案 |
| `evidence`、`reason`、`problem` | 回查依据，按问题机制归类 |
| `summary`、`missing_evidence` | 概览和证据缺口 |
| 原始 `subject` | 按仓库和 PR 去重，不把多次 review 当成多个独立案例 |
| 原始 `window`、`reviewed_code`、`quality_flags` | 识别版本、时间和数据限制；diff 按需读取 |
| 原始 AI 输出、后续事件、提交、结果 | 通过只读工具按需复核，不一次性把所有原文放进提示词 |
| `run.input_hash` | 仅供程序核对第一阶段对应的原始内容，不传给模型作判断 |

第一阶段仍生成 `signal_strength`。第二阶段使用显式字段清单排除它：索引、案例快照、
读取工具都不暴露这个字段，也不用它排序、加权或筛选。
第二阶段提示词不再单独介绍这个被排除的字段，输入部分只说明实际提供的材料。
正向反馈、被反驳的人工观点和没有 findings 的案例仍保留，用于反例和范围判断。

### Harness 目标清单

`scripts/selflearn/harness_targets.json` 是针对当前 Review Bot 的示例清单，
不是分析 Agent 自身的配置。可另建清单并通过 `--targets` 选择。

顶层 `name` 是名称，`root` 相对于清单文件所在目录；每项包含：

- `id`：任务单引用的稳定目标名称。
- `kind`：prompt、skill、knowledge、context、tools 或 verification。
- `path`：相对 root 的文件位置。
- `scope`：修改的适用范围。
- `change_mode`：text_only 仅文字；config_only 仅配置；add_file 候选新增文件；
  engineering 需要人工工程改造。
- `allowed_changes`：具体可调整部分及禁止越过的边界。

示例包含任务提示词、新增审查 Skill、新增项目知识、change map 预算、
只读工具能力、任务内验证要求六项。新增 Skill 和知识文件目前并不存在，
不会被这个命令创建；相关建议必须说明如何接入实际加载流程。
Python 模板文件只作为只读取证材料，清单不授权任意源码改动。
这些文件内容会提供给模型，不要把含密钥的完整配置文件加入清单。

### 框架与责任划分

1. **程序准备证据**：每个 episode 取最后一条分析，核对输入摘要和原文引用，
   去掉强度字段，保留所有类型案例；按原始仓库和 PR 建立分组。
2. **程序冻结 Harness**：读取清单指定文件，保存当时内容、内容摘要和可修改范围。
   分析期间即使原文件改变，模型读到的仍是这一份快照。
3. **当前聊天归纳**：使用专用分析提示词，载入聊天历史，再看本次案例索引。
   任务输入和校验修正消息会显示在聊天中并保留到会话记录；普通聊天历史不是案例证据。
   通过 `read_case` 和 `read_harness` 回查证据，
   按机制聚类，寻找反例，检查当前 Harness 是否已经处理该问题。
   Harness 先按目标说明搜索，默认附带前后 3 行；用 `start_line/end_line` 精确展开。
   无搜索、无结束行时默认读 60 行，其他读取最多 200 行，按 `next_start_line` 续读。
   同一文件相同片段在本次会话内去重，不同 target_id 也适用；需要重新取得原文时
   加 `reread=true`。不做语义去重，不更改完整快照，不把搜索无结果当作不存在。
4. **模型提出任务单**：给出原因假设、首选位置、最小改动意图和验证计划。
   当前文件不是历史配置，缺少轨迹时只能提出假设，不能声称已确认历史根因。
5. **程序校验并保存**：案例和 finding 索引必须存在，目标必须在清单内，
   文件引用必须来自快照。独立 PR 数由程序计算，不采纳模型自行声称的次数。
   首次生成后，格式/引用校验最多修正三次（总共最多四次生成）；不按强弱设置阈值。
   修正沿用同一会话，保留此前取证和错误信息，不放宽引用校验，也不重试网络异常。

每次回复最多 32 轮工具循环，整轮归纳最多 20 分钟。
没有合适建议时可以正常输出空的 proposals；异常或取消时不会生成半成品任务单。

### 第二阶段输出

```text
<Agent 工作目录>/selflearn/proposals/<本次会话标识>/
├── improvement_proposals.json
├── cases.json
├── harness_snapshot.json
├── manifest.json
├── input.json              # 分析指令、本轮任务输入及运行前聊天状态
├── trace.json
└── context/                # 仅非聊天调用使用；聊天入口沿用工作区上下文存储
```

`cases.json` 保存去掉强度字段的分析及对应原始 episode；`harness_snapshot.json`
保存实际取证的文件和范围；`manifest.json` 保存本次分析指令、覆盖范围、模型信息；
也记录本次上下文配置、最大修正次数和 `chat_session_id`，配置不作为案例字段输入模型。
`input.json` 保存分析提示词、任务输入和运行前聊天状态，方便复查输入；不是每次网络请求抓包。
`trace.json` 保存读取动作、每轮输入（包括校验错误）和最终回复。
完整流式过程同时由普通聊天记录和上下文历史保存。失败时可能只留下准备好的文件，错误见 status。

`improvement_proposals.json` 顶层包含：

- `schema_version`：任务单格式版本。
- `summary`：整体发现。
- `proposals`：下面的具体任务单列表。
- `limitations`：覆盖范围、缺失历史配置/轨迹等限制。
- `coverage`：原始数据路径、已分析案例数、原始数据案例数和独立 PR 数。
- `run`：输入路径、清单路径、案例/文件/指令/实现摘要、运行标识、模型和保存时间；
  `session_id` 对应本次输出目录，`chat_session_id` 是实际使用的普通聊天会话。

每项任务单包含：

| 字段 | 含义 |
|---|---|
| `problem` | 共同问题或需要澄清的候选问题 |
| `supporting_findings` | 支持该问题的 `episode_id + finding_index`，索引从 0 开始 |
| `counter_evidence` | 反例或限制该解释的分析项，同样通过 ID 和索引定位 |
| `independent_pr_count` | 支持案例涉及多少独立 PR，由程序去重计算，不是已证明的缺陷次数 |
| `root_cause_hypothesis` | 原因假设、依据和不确定性 |
| `primary_target` | 清单中的首选修改目标 ID，没有明确位置时为 null |
| `harness_evidence` | 文件依据，每项为 `target_id + quote` |
| `change_intent` | 最小修改意图，不是补丁；不修改时可为 null |
| `alternatives` | 其他原因或可选修改位置 |
| `verification_plan` | 后续验证原因、确认生效和评测的计划，不是已执行的结果 |
| `status` | propose 值得做候选实验；needs_evidence 先补证据；no_change 不宜持久修改 |

例如，同一个 PR 的两次 review 都涉及调用方遗漏，独立 PR 数仍为 1。
如果当前 Prompt 已要求检查调用方，则不应重复追加相同文字；可以先要求补充工具
记录，或针对检索版本提出待验证假设。只有形成候选并经过后续评测，才讨论实际修改。
