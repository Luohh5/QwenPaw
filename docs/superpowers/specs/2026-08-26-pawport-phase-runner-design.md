# Pawport Phase Runner 重构设计

## 目标

将 Pawport 的 Agent 语义判断和 Mission 兼容性修复统一到一个专用
Phase Runner，删除重复的串行包装接口，并集中管理 worker 访问权限和
阶段完成状态。

本次重构只改变内部执行结构，不改变迁移行为：

- 不修改 `triage_prompt` 和 `repair_prompt` 的任务语义。
- 不修改安全暂存区、待修复区、待迁移区、丢弃区的状态转换规则。
- 不改变三路滚动并行、同一资产断点续读、逐资产修复预算和 Mission
  多轮重试行为。
- 不改变最终 `completed` / `stopped_limit` 状态及 summary 格式。

## 当前问题

`adaptation_loop.py` 同时负责工具能力、AgentRequest 构造、session 绑定、
heartbeat、并行池、triage 重试、Mission 轮次和最终完成判断。两个阶段通过
`_run_agent`、`_run_bound_agent`、`_bounded_parallel`、`_triage_asset` 和
`_repair_asset` 多层函数组合，生命周期与完成条件分散。

访问控制也分散在三处：模块级 `_ACTIVE_CONTEXTS`、
`ActiveAdaptationContext._binding()` 和依赖字符串比较的
`_require_repair_phase()`。Mission 的私有调用则需要调用方正确配对
`start_internal_mission()`、`check_internal_mission()` 和
`finish_internal_mission()`。

## 设计

### 1. 专用 Phase Runner

新增 `qwenpaw.portability.adaptation_phase`，包含：

- `PhaseSpec`：阶段名称、允许工具、prompt factory、进度标签和是否允许修改。
- `PhaseOutcome`：`completed`、`remaining` 和 `reason`。它只描述一个阶段的
  执行结果，不取代 `RunState` 或四区状态。
- `AdaptationAccessGuard`：按 session 绑定 context、PhaseSpec 和唯一资产。
- `PhaseRunner`：统一 request 构造、Agent stream、heartbeat、取消清理、
  三路滚动并行和逐资产异常隔离。

定义两个不可变的阶段规格：

- `TRIAGE_PHASE`：值仍为 `triage`，使用现有 triage prompt 和三种只读/分类
  工具，禁止写入与原生测试。
- `REPAIR_PHASE`：值仍为 `mission_repair`，使用现有 repair prompt 和六种
  工具，允许修改、测试以及从待修复区进入待迁移区。

Phase Runner 不决定四区转换。Agent 仍必须调用现有
`CompatibilityStore.classify()`，它仍是状态转换的唯一真源。

### 2. 访问守卫

`AdaptationAccessGuard.bind()` 返回 context manager。进入时注册
`session_id -> binding`，退出时无条件清理；重复 session、未绑定调用和
context 不匹配全部 fail closed。

`ActiveAdaptationContext` 从守卫取得当前 binding：

- `phase` 继续返回现有字符串，保持 prompt、日志和测试兼容。
- 资产访问必须匹配 binding 中的唯一 `asset_key`。
- 修改、结构化更新和原生测试改为检查 `PhaseSpec.mutable`，不再比较
  `"mission_repair"` 字符串。
- 分类仍根据 PhaseSpec 的来源区决定：triage 只能处理 staging，repair
  只能处理 repair。

`QwenPawLocalWorkspace` 对内置私有工具函数身份的校验保持不变；Phase
Runner 只统一其后的请求级能力边界，不放宽插件覆盖防护。

### 3. Internal Mission session

将 `MissionMode` 的三段式私有接口替换为：

```python
with mode.internal_mission(session_id, loop_dir) as mission:
    completed = await mission.check()
```

进入 context 时激活现有 `MissionGate`，退出时始终 reset 对应 session。
`check()` 仍直接复用 `MissionGate.check()`，不创建第二套完成规则。

删除以下旧接口：

- `MissionMode.start_internal_mission()`
- `MissionMode.check_internal_mission()`
- `MissionMode.finish_internal_mission()`
- adaptation loop 中被 Phase Runner 替代的 Agent/并行包装函数

### 4. 两阶段流程

第一阶段：

1. 读取 staging 资产键。
2. Phase Runner 并行运行 TRIAGE worker。
3. 模型失败但已消费预算并产生阅读进展时，用同一 session 恢复该资产。
4. staging 清空时返回完成；否则返回当前已有的工具上限或未完成原因。

第二阶段：

1. 准备现有 Mission PRD，并进入 internal Mission session。
2. 每轮选择仍在 repair、未耗尽预算且未超逐资产次数的资产。
3. Phase Runner 并行运行一轮 REPAIR worker。
4. 同步现有 Mission PRD，并让原生 MissionGate 检查完成状态。
5. repair 清空时完成；到达逐资产次数时返回现有 `stopped_limit` 原因。

`run_adaptation_loop()` 只负责准备、依次调用两个阶段、调用
`CompatibilityStore.complete()/finish()` 和生成 summary。

## 错误处理

- 单个 Agent 异常继续记录为当前中文 warning，不阻断同批其他资产。
- triage 只有在已有进展且仍有预算时才恢复同一 session。
- Phase Runner 退出时必须取消并等待未结束的 stream，同时清除访问 binding
  和 activity。
- Mission 初始化或 gate 检查失败仍保留资产在 repair，并产生现有
  `无法完成 QwenPaw Mission` 停止原因。
- 不新增总时长限制，不改变逐资产工具预算。

## 测试契约

先增加会在旧结构下失败的接口测试，再实施重构：

- 访问守卫在正常结束、Agent 异常和取消后都清理 binding。
- 未绑定、跨资产和错误 context 调用继续拒绝。
- triage 禁止写入、更新和测试；repair 保持允许。
- Phase Runner 保持三路滚动并行、后续资产不被未完成资产阻塞。
- triage 失败后保留进展并用同一 session 恢复。
- repair 保持按资产轮询，单项失败不会独占全部资源。
- internal Mission context 在成功和异常时都清理 gate。
- prompt factory 仍调用现有 `triage_prompt` / `repair_prompt`，请求中的
  `portability_phase` 和 allowed tools 不变。
- 四区转换、summary、`completed` / `stopped_limit` 继续由阶段 0 保护矩阵
  验证。

## 非目标

- 不修改 prompt 文本。
- 不修改 CompatibilityStore、CompatibilityTester 或四区数据模型。
- 不将 Phase Runner 泛化给普通 Goal、Loop 或用户 Mission。
- 不改变模型、重试、工具调用预算和并发配置。
- 不处理进程崩溃恢复或持久化 rollback journal。
