## 1. Evolving 闭环架构
![](https://intranetproxy.alipay.com/skylark/lark/__mermaid_v3/c5c6fb9f93eea7e16b7744efd918b0b4.svg)

整个闭环遵循一个核心分工：

> 分析和修改对应梯度计算和梯度下降；评测负责比较新旧版本是否带来增益，发布流程负责人工审批、灰度与回滚。
>

从 Bot 场景 agent 执行轨迹与用户反馈出发，基于 QwenPaw 开发一套自进化系统，周期性分析真实对话，提出改进并生成候选版本；候选只有通过离线评测、人工审批和线上灰度后，才能成为新的生产版本。新版本产生的运行数据与用户反馈再次进入分析阶段，形成完整闭环。

以插件形式实现，包含skill+sub-agent

## 2. 设计目标与边界
### 2.1 设计目标
**harness 参数：**$ \theta = (\theta_{\text{prompt}}, \theta_{\text{tool}}, \theta_{\text{middleware}}) \in \Theta $

+ `prompt`：系统提示词、行为规范和各种指令；
+ `tool`：工具集合、工具接口、参数、描述及实现；
+ `middleware`：运行时控制逻辑，包括钩子、环境设置和 agent loop。

**优化目标：**让 harness 在整个目标任务分布上取得最高期望性能，而不是只修某个样例。

$ J(\theta) = \mathbb{E}_{(x,y^*)\sim \mathcal{T}} \mathbb{E}_{(\tau,\hat{y})\sim P_\theta(\cdot\mid x)} [\mu(\hat{y}, y^*)]
 $，任务级评价函数记为$ \mu(\hat{y}, y^*)
 $



进化系统需要完成四件事：

+ 从真实用户对话中发现高价值、可复现的 Harness 缺陷；
+ 将用户反馈归因到具体 Agent 行为和 Harness 实现位置；
+ 生成最小、可解释、可回滚的候选修改；
+ 通过离线与线上证据判断修改是否真正具有泛化收益。

每次改进完整证据链：

```latex
用户反馈 → 失败轨迹 → 根因假设 → Harness 修改 → 评测结果 → 发布记录
```

### 2.2 系统核心能力概括
+ **先观测，后优化**：没有消息关联、版本快照和 Skill/Tool 激活记录时，不启动自动修改。
+ **提出修改和判断效果分开**：Agent 可以提出修改，但不能决定自己的修改是否有效。
+ **解决重复问题，而不是修补单个案例**：优化反复出现的问题模式，不为单条对话堆叠特例。
+ **最小修改与单一主假设**：一次候选尽量只验证一个主要因果假设。
+ **能力改进优先于规则堆叠**：优先修复检索、工具、验证和控制流等能力缺口。
+ **泛化优先于局部修复**：原对话变好并不等于系统变好，候选还必须通过独立样本和回归验证。
+ **人机协同发布**：初版允许自动分析和自动生成候选，但只允许人工批准上线。

## 3. Analyze Agent：从对话轨迹中发现可归因的 harness 缺陷
**需要解决的问题：** Bot类型的agent（群聊场景、github场景）是一段多人、异步、带有隐式反馈的连续事件流。Bot 回答错误原因可能来自对话切分、上下文、知识、检索、Skill 触发或工具执行。如果只阅读聊天文本并总结，很容易把相关性误判为因果关系。

**总体方案：** 先把聊天记录与 Agent 运行轨迹还原成结构化证据，再进行反馈理解、因果归因和缺陷聚类，最终形成可以直接交给改进 Agent 的问题分析报告。

### 3.1 提取对话和完整rollout
系统首先采集 Bot 的 inbound/outbound message、reply/quote、mention、reaction、检索结果、工具调用、Skill 激活以及模型和 Harness 版本。

这些事件被组装成结构化对话轨迹：用户问题、上下文、检索、工具、Bot 输出和用户反馈是主要节点，同时记录它们之间的信息依赖、执行顺序和反馈指向。它不只描述“Bot 说了什么”，还描述“Bot 为什么会这样回答”。

群聊切分优先依赖 reply/quote 和原生 thread，其次结合 mention、参与者、时间邻近和主题相似度。对于隐式回复和跨日追问，可以使用 Agent 辅助推断，但必须保留置信度，不能把推断结果当作原始事实。

### 3.2 识别用户反馈&构造数据&打标
参考 [HarnessFix](https://arxiv.org/abs/2606.06324) 的 HTIR 和 CHIEF 的分层因果图，比单纯把聊天记录丢给 agent 更适合我们的场景。

建议为每个对话线程构造图：

+ 节点：用户消息、Bot 输出、引用内容、检索结果、工具调用、skill 激活、模型调用、人工答疑、reaction、后续追问、状态变化。
+ 边：
    - `reply_to`、`quote_from`
    - `included_in_context`
    - `retrieved_from`
    - `tool_result_of`
    - `generated_under`
    - `feedback_on`
    - `supersedes/corrects`
+ 实现锚点：本次回答实际使用的 skill、prompt、知识条目、retriever、tool schema、middleware hook、模型及版本。

不把所有用户行为简单压缩成一个分数，而是保留反馈来源、对象和可信度：

+ **强信号**：管理员明确纠错、用户明确确认解决、可验证工具结果、人工答疑给出不同结论。
+ **中信号**：针对 Bot 消息的赞踩、引用回复，以及“不对”“可以了”等直接表达。
+ **弱信号**：重复提问、换一种方式追问、转向人工、对话中断或 LLM 辅助评审。

强信号可以进入主要评测指标；中弱信号更适合用于发现候选问题，并通过人工抽样持续校准。特别需要避免把沉默直接解释为满意，也不能把后续追问一律解释为前一轮失败。

不要根据一次负反馈马上修改 harness。把多个具有相同证据链的诊断合并后再进入改进流程。

### 3.3 计算梯度
根据用户反馈生成 harness 缺陷作为优化方向：

1. 确定用户不满意的是哪一个具体结论或行为。
2. 沿对话与运行轨迹回溯该结论依赖的上下文、知识、Skill 和工具结果。
3. 同时提出主根因与备选假设，例如知识过期、检索遗漏、Skill 未激活、工具失败或用户问题本身含糊。
4. 通过移除错误上下文、替换正确证据或强制激活对应能力，判断回答是否可能改变。

> 不只是给出一个标签，还要给出责任步骤、对应的 Harness 位置、证据、反例和置信度。这样才能把“回答不好”转化为可优化的问题。
>

### 3.4 优化稳定性
单条失败可能只是模型采样波动或特殊案例。系统按照责任步骤、Harness 层和根因，将相似诊断聚合成重复出现的问题模式；只有达到案例数量、反馈可信度和影响范围阈值后，才进入修改阶段。

分析阶段输出问题分析报告，核心内容包括失败症状、因果链、主根因与备选假设、支持案例、对应的 Harness 位置、影响范围、风险等级和证据有效期。

> 避免局部最优
>

## 4. Optimizer Agent：agent harness的文本梯度下降
**需要解决的问题：** 用户反馈不一定应该变成全局规则，而自由修改 Prompt 或 Skill 又容易产生规则堆叠、越权修改和不可解释回归。

**总体方案：** 以问题分析报告为输入，先明确改进假设与作用范围，再生成改进任务单，约束独立的改进 Agent 在隔离环境中生成可审查的候选版本。

### 4.0 可修改的 Harness 范围（开放的参数）
+ 项目知识来源和 Skill
+ 工具 schema、参数校验、错误反馈和工具实现；
+ 模型路由、生成预算和候选验证策略；
+ session/thread 切分、上下文组装和生命周期控制；
+ middleware、retry、timeout、loop guard 和终止条件。

### 4.1 确定待优化的参数
系统首先判断问题应该在哪一层解决：

+ 持久且普遍的行为缺陷 → 全局 harness/skill。
+ 项目事实变化 → 知识库，不要污染全局 prompt。
+ 特定群、特定团队习惯 → channel-scoped overlay。
+ 特定用户偏好 → user-scoped memory。
+ 一次性事故或歧义 → 不持久化，最多加入诊断案例。

修改方式分为两类：

+ Capability Patch 修改可执行代码、能力或控制流程，相当于改变“能做什么”；
+ Steering Patch 只修改文本指导，相当于改变“已有能力应当怎样使用”。

> Capability Patch 可以类比为较大的学习率步长，把 Steering Patch 类比为较小步长
>

### 4.2 梯度下降
类似 git worktree 的概念，构造用于管理 harness 修改版本的树或DAG结构。

+ 每个节点代表一个曾经探索过的 harness；
+ 节点保存补丁意图、mini-batch 结果、开发集得分和反思经验；
+ 每条边代表父子 harness 之间的代码 diff。

Optimizer Agent 使用最小权限，只接收问题分析报告与改进任务单，并在worktree中工作。候选以独立 commit 保存，也不直接写入生产 workspace。

Agent 不会沿当前最佳版本无限累加修改，而是保留完整的候选版本历史，支持拒绝、重新基于新版本生成、撤销和选择性合并。每个候选都要记录它基于哪个版本、解决了什么问题、在哪些测试范围内有效以及引入了哪些回归。

注意的点：历史经验必须绑定模型、Harness、项目和时间版本。模型能力或项目状态变化后，旧经验需要重新校准，不能直接当作永久规则复用。worktree 也不应该直接塞入prompt，agent应该按需读取。

> 相当于避免陷入局部最优，或避免单一路线搜索塌缩
>

## 5. Evaluate Agent：证明候选版本真的变好
**需要解决的问题：** Agent 输出具有随机性，原对话重放变好可能只是偶然，也可能只是因为候选使用了更多 token、更多调用或针对已知样本过拟合。评测必须回答修改是否实际触发、是否修复目标根因、是否具有泛化收益，以及是否引入安全和成本回归。

**总体方案：** 采用“前置检查 + 改动生效检查 + 新旧版本成对重放 + 独立测试集 + 安全与成本检查”的多层评测。评测结果由固定程序汇总，并由人工决定是否允许发布，不能由负责改进的 Agent 自行决定。

### 5.1 对话重放沙箱
对话重放沙箱固定原始输入、对话上下文、Harness、知识库、模型配置和可模拟的工具结果。在相同任务、环境和资源预算下运行旧版本与候选版本，直接比较哪些案例被修复、哪些发生退化、哪些仍然失败、哪些保持通过。

### 5.2 mini-batch
原始失败对话用于验证目标问题是否修复；与其相邻但未参与修改的案例用于检查局部回归；另外保留一组未参与分析和修改的独立测试集，并按照时间、主题、用户和完整对话切分，用于判断能否泛化到未见样本。不能随机拆分同一段对话，否则会高估效果。

### 5.3 分层评测流程
```latex
范围与静态检查
  → 改动生效检查
  → 目标缺陷重放
  → 回归集与独立测试集
  → 安全与成本检查
  → 人工评审
```

改动生效检查：改进任务单必须说明如何通过运行日志确认新的 Skill、知识、工具或控制流被实际使用；如果新机制没有触发，即使分数提高，也不能把收益归功于这次修改。

对于高风险或结果波动较大的任务，应进行多次重复运行和显著性检查。同时让旧 Harness 在同等 token、重试次数和调用预算下运行，区分“Harness 变好”与“只是投入了更多计算”。

### 5.4 多维评测指标
agent-as-judge：

+ 任务正确率、事实与引用一致性；
+ 一次解决率、重复追问率、用户纠错率和人工接管率；
+ 澄清、拒答和转人工是否合理；
+ 敏感信息、权限、安全策略和工具副作用；
+ 延迟、token、模型调用和工具调用成本。

最终输出候选版本的目标修复率、回归率、独立测试集收益、安全结果、成本变化以及典型的修复和退化案例，更新worktree

## 6. Release Agent：将有效修改安全地带回线上
**需要解决的问题：** 离线通过并不等于线上稳定。真实用户、并发对话和外部工具状态都可能暴露新的问题，因此候选不能直接覆盖生产 Harness。

**总体方案：** 把每个候选作为独立版本管理，依次经过人工审批、影子运行、小流量灰度和持续监控，并始终保留快速回滚能力。

### 6.1 人工审批
候选版本需要记录 Git commit、Harness 配置、模型和知识版本、改进任务单、完整评测报告以及回滚点。审批界面重点展示目标问题、改进假设、代码或 Skill 差异、修复和退化案例、安全结果及适用范围。

初版只允许人工批准上线。Agent 可以整理证据和给出建议，但不能绕过评测门禁或审批策略。

### 6.2 shadow -> A/B
候选先进行影子运行：对真实请求同时运行生产版和候选版，但候选结果不发送给用户，外部写操作必须禁用或模拟。它用于确认候选在真实场景中确实使用了新的改动，并观察离线评测没有覆盖的问题。

影子测试无明显问题后开始A/B测试收集用户对候选版反馈。

### 6.3 晋级、监控与回滚
候选只有在目标指标稳定提升、安全指标不退化且成本处于预算内时，才能成为正式版本。系统保留上一稳定版本并支持快速切换；安全、权限、人工接管率和严重错误触发自动停止或回滚。

短期提升不代表永久有效。项目知识、模型或核心工具大版本变化后，已发布 Harness 应重新评测；线上持续产生的新反馈重新进入分析阶段，闭合下一轮进化。

### 6.4 主要痛点
+ 影子运行会增加模型和工具成本，带副作用的工具必须隔离。
+ 群聊中的用户和对话串线容易污染灰度实验分组。
+ 知识库或数据库等有状态修改需要独立的迁移与回滚方案。
+ 线上分布持续漂移，历史基线和评测集会逐渐失真。

## 7. 【TBD】支撑体系与 QwenPaw 集成形态
PawApp 管理界面 + 自进化后台服务 + QwenPaw 插件接入，插件内部包含：

+ PawApp 管理页面；
+ 对话与运行记录采集 Hook；
+ 模型和工具调用记录 middleware；
+ 对话分析 Agent Harness；
+ 改进 Agent Harness；
+ 对话重放与评测模块；
+ Git 候选版本管理；
+ 人工审批、发布和回滚；
+ 必要时由插件启动的独立评测进程。

也就是说，系统对用户表现为“安装一个插件”，但插件内部仍然有清晰的分析、修改、评测和发布模块。

## 9. 关键结论
1. 自进化系统首先是一个 **可观测、可实验、可发布的软件工程系统**，其次才是修改 Harness 的 Agent。
2. 对话分析的核心不是情感判断，而是把用户反馈连接到 Bot 行为、上下文来源和 Harness 实现的因果证据链。
3. Skill 只是可修改内容之一；更稳定的收益通常来自检索、工具、验证和控制流等能力改进。
4. “原对话重放变好”只能证明局部修复；改动实际生效、独立测试集、同等资源预算对比和线上灰度共同决定是否真正进步。
5. 初版应坚持离线、受限修改和人工发布，待数据与评测可信后再逐步提高自治程度。

## 10. 参考资料
+ [How Warp builds self-improving agents on Claude](https://claude.com/blog/how-warp-builds-self-improving-agents-on-claude)
+ [Warp Agents Demo: GitHub Issue Triage](https://github.com/warpdotdev/warp-agents-demo-github-issue-triage)
+ [AutoSaddler: Automatic Harness Optimization with Durable Updates from Agent Execution Traces](https://arxiv.org/abs/2608.23041)
+ [HarnessCompass: Guiding Automatic Harness Evolution toward Generalizable and Effective Agent Harnesses](https://arxiv.org/abs/2608.01918)
+ [DREvo: Distilling Recalibrated Historical Experience for Harness Self-Evolution](https://arxiv.org/abs/2607.26722)
+ [MemoHarness: Agent Harnesses That Learn from Experience](https://arxiv.org/abs/2607.14159)
+ [HarnessBank: Semantic Gene-Bank Search with Gated Verification for Agent-Harness Self-Evolution](https://arxiv.org/abs/2607.13683)
+ [Self-Harness: Harnesses That Improve Themselves](https://arxiv.org/abs/2606.09498)
+ [From Failed Trajectories to Reliable LLM Agents: Diagnosing and Repairing Harness Flaws](https://arxiv.org/abs/2606.06324)
+ [Evolving Agents in the Dark: Retrospective Harness Optimization via Self-Preference](https://arxiv.org/abs/2606.05922)
+ [Agentic Harness Engineering: Observability-Driven Automatic Evolution of Coding-Agent Harnesses](https://arxiv.org/abs/2604.25850)
+ [VeRO: A Harness for Agents to Optimize Agents](https://arxiv.org/abs/2602.22480)
+ [AgentDevel: Reframing Self-Evolving LLM Agents as Release Engineering](https://arxiv.org/abs/2601.04620)
+ [From Flat Logs to Causal Graphs: Hierarchical Failure Attribution for LLM-based Multi-Agent Systems](https://arxiv.org/abs/2602.23701)
+ [Rethinking the Evaluation of Harness Evolution for Agents](https://arxiv.org/abs/2607.12227)
+ [Harness Updating Is Not Harness Benefit: Disentangling Evolution Capabilities in Self-Evolving LLM Agents](https://arxiv.org/abs/2605.30621)
+ [Externalization in LLM Agents: A Unified Review of Memory, Skills, Protocols and Harness Engineering](https://arxiv.org/abs/2604.08224)
