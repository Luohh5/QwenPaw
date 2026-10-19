# 插件公共框架边界收口 Implementation Plan

> **For agentic workers:** 后续执行使用 `superpowers:subagent-driven-development`，逐项完成并验收。功能代码和测试代码由不同子 agent 编写；测试作者依据下述行为契约验收，不能通过削弱断言迁就实现。2026-10-13 用户已授权按本规划实施；当前在原功能分支修改，未提交或推送。

**Goal:** 收口插件执行选择、运行实例归属和共享依赖使用者管理，使新的 Edge 执行适配器可以复用公共生命周期，而不需要继续向 Loader / Lifecycle 添加类型分支。

**Architecture:** 保留现有生命周期台账、目录事务、工作区投影和 `plugins/execution` 进程底座。在这些机制之间建立三个窄边界：执行后端、运行资源登记、依赖使用登记；先迁移现有 Python 与静态 Channel 路径，再以非 Channel 测试组件验证通用性。

**Tech Stack:** Python、asyncio、现有 PluginLoader / PluginLifecycle / WorkspaceProjector、PluginProcess、stdio JSON-RPC、pytest。

**Spec:** 本文“范围与设计约定”承接 2026-10-13 用户指定的架构评审第 1、2、5 项；兼容基线为 [C0–C7 接入计划](2026-10-12-channel-plugin-integration.md) 及其现有测试。

基线：`378e8efcf`，2026-10-13 核对。下列接口已实施；实际补充契约见文末“实施补充”。

## Global Constraints

- 代码高效、精炼；功能和安全性相同时选更少的代码。复用原机制，不另外建设插件调度器、安装器或进程监督器。
- 共享当前依赖环境；平台 SDK 的发现不得变成宿主导入，未启用的进程组件不启动。
- `LifecycleDelegate` 继续回答目录提交和依赖环境的责任归属；使用子进程不意味着把 `owns_commit` / `owns_dependency_env` 改成 False。
- 保留旧 Python `register(api)`、前端插件、PawApp、Memory 使用中保护及现有生命周期台账语义。
- 未停稳不能释放归属、替换目录或删除仍被使用的 staging；保留代际、取消、恢复及停止失败保护。
- 开机不自动安装依赖；仅显式安装、更新、修复允许受控安装。继续使用环境锁、安全新增 wheel、宿主保护和修复标记。
- 本轮不迁移真实 Memory / Computer Use，不改飞书、钉钉的平台业务规则、默认交付预设和用户配置格式；不宣称已完成架构评审的第 3、4、6、7 项。
- 全程使用临时工作区和受控平台 / 模型替身，不使用真实机器人。测试通过不等于原生 Desktop、Docker 或真实平台验收。

## Review Focus

1. 停用插件、前端插件、PawApp、无活动记录的卸装与崩溃恢复，不能因统一入口而失去原路径；由 F1.2、F1.3、F4 验收。
2. 候选改变执行后端、原地更新和候选启动失败时，必须按旧后端恢复旧代码与旧状态；由 F1.3 验收。
3. 启动取消、探测残留、旧工作区仍有任务时，不能删除归属或误停新实例；由 F2.1、F2.2 验收。
4. 相同解释器的不同环境、不同解释器共享 site、宿主已导入依赖，不能漏报或误合并使用者；由 F3.1 验收。
5. 两插件同时登记 / 安装依赖，或发布期间被取消，不能使用过期使用者清单提交并返回成功；由 F3.2 验收。

## 范围与设计约定

执行顺序：**F1.1 → F1.2 → F1.3 → F2.1 → F2.2 → F3.1 → F3.2 → F4**。分别对应评审的 **1 → 2 → 5**，最后做联合验收。每项独立可测试；若某项未通过，不继续扩大改动。

| 边界 | 负责 | 不负责 |
| --- | --- | --- |
| 执行后端 | 静态描述、准备 / 探测、取得定义、执行相关的启用校验与恢复状态 | 目录提交、全局事务、重复管理进程 |
| 运行资源登记 | 插件 / 能力 / 作用域身份，代际，仍被持有的资源，停止证据 | 消息排队、平台断连或 Memory 查询的业务实现 |
| 依赖使用登记 | 已声明需求、实际运行使用、对应环境与版本证据 | 下载求解器、随意替换已安装共享依赖 |

选择执行后端只发生在解析源版本时。旧版本、候选版本、恢复后的版本各自保留自己的后端和描述，不能根据可变磁盘或“有没有 descriptor”在事务中途重新猜测。候选从 staging 发布到正式目录后，按已确认的后端和源身份重建路径相关描述，不能继续使用旧临时路径。

插件类型与执行方式分开。第一版适配现有 Python 路径和静态 Channel 路径；通过**宿主注册的描述适配器**扩展其他类型。未知描述类型明确拒绝，不能回退到 Python 导入。暂不增加面向用户的万能执行模式配置，也不允许插件描述指定任意宿主后端代码。

### 文件归属

| 文件 | 变更与职责 |
| --- | --- |
| 新增 `src/qwenpaw/plugins/execution_backend.py` | 后端契约、描述结果、唯一解析入口 |
| 新增 `src/qwenpaw/plugins/python_backend.py` | 从 Loader 搬出 Python 入口加载、探测及导入状态恢复；行为保持兼容 |
| 修改 `src/qwenpaw/plugins/channel_adapter.py` | 现有静态描述 / 定义实现执行后端契约，容纳 Channel 专属启用判断 |
| 修改 `src/qwenpaw/plugins/architecture.py`、`loader.py`、`lifecycle.py` | 保存后端绑定；公共流程委托执行后端；保留事务 / 台账主干 |
| 新增 `src/qwenpaw/plugins/ownership.py` | 通用资源归属登记、代际与停止证据 |
| 修改 `src/qwenpaw/plugins/registry.py`、`workspace_projector.py`、`api.py` | 接入统一归属，投影使用通用 token / 回调 |
| 修改 `src/qwenpaw/app/channels/manager.py` | Channel 生命周期适配通用归属，继续管理渠道排队与排空 |
| 修改 `src/qwenpaw/app/multi_agent_manager.py`、`app/workspace/workspace.py`、`app/workspace/service_factories.py` | 替换工作区创建 / 交接的调用接缝，不重写工作区生命周期 |
| 新增 `src/qwenpaw/plugins/dependency_usage.py` | 声明和使用者快照；从统一归属读取活跃实例，不另建 owner 表 |
| 修改 `src/qwenpaw/plugins/dependency_gate.py`、`install_lock.py` | 接受通用输入、统一消费依赖清单和协调共享环境 |

测试文件列在各任务内；本轮无需新增生产平台插件或修改两渠道 Driver。

## F1.1：建立唯一执行后端入口

**接口：** 在 `execution_backend.py` 定义 `PluginDescription(manifest, source_path, requirements_path, payload)`、`BackendBinding(backend, description)`。`payload` 仅由该后端解释，公共生命周期不得读取它。

- `BackendResolver.register_descriptor(plugin_type: PluginType, backend: ExecutionBackend) -> None`：只供可信宿主组合入口登记，重复登记拒绝。
- `BackendResolver.resolve(source_path: Path, manifest: PluginManifest) -> BackendBinding`：按入口形状和已登记类型解析；无 descriptor 保持 Python / 无后端入口兼容路径。
- `ExecutionBackend.describe(source_path: Path, manifest: PluginManifest) -> PluginDescription`：读取必要静态信息，不启动组件。
- `prepare(description, *, allow_install: bool, probe: bool, wheels: tuple = ()) -> None`（async）：依赖准备和入口探测，分别受参数控制。
- `load_definition(description, config: Mapping, *, saved_definition: object | None = None) -> DefinitionResult`（async）：返回 `definition / enabled / status / diagnostics`，不自行提交目录或重复 `register(api)`。
- `before_activate(description) -> None`（async）、`validate_update(old, candidate, *, in_place: bool) -> None`：封装执行相关前置条件，不创建第二套事务。
- `capture_restore_state(record: PluginRecord) -> object | None`、`restore_state(state: object | None) -> None`：Python 保存 / 恢复导入状态；静态进程后端不伪造模块快照。

**Files:** 新增 `execution_backend.py`；修改 `architecture.py`。新增 `tests/unit/plugins/test_execution_backend_resolution.py`。

- [x] 测试作者先建立解析契约：现有 backend、仅 frontend、现有 Channel descriptor、宿主登记的非 Channel descriptor；冲突入口和未知类型必须拒绝。
- [x] 实现上述契约和解析器。Manifest 只做入口结构校验；“该类型是否有描述适配器”的校验移到解析器，保留明确报错。
- [x] `PluginRecord` 增加内部 `backend_binding`；不暴露到配置或 HTTP 序列化，不改变插件对外 `instance` 的含义。
- [x] 用阻断 import 的测试证明：解析静态组件不导入 Driver / SDK、不启动进程；同一绑定不会因源文件随后改变而切换后端。
- [x] 运行 `python -m pytest tests/unit/plugins/test_execution_backend_resolution.py -q`。原本针对新增行为失败的用例转为通过，才进入 F1.2。

**验收：** 能通过注册适配器识别非 Channel 静态入口；未知实现不被误执行。此项尚不切换正式加载链路。

## F1.2：迁移加载、启用、配置更新和依赖修复

**Files:** `python_backend.py`、`channel_adapter.py`、`loader.py`、`lifecycle.py`；新增 `tests/unit/plugins/test_execution_backend_lifecycle.py`，复用现有配置、加载测试。

**接口：** 消费 F1.1 的 `BackendBinding`。Python 定义导入与 `register(api)` 分开；公共 `_invoke_plugin_register()` 仍负责绑定 PluginApi、台账与异常清理。

- [x] 测试作者按 Python / Channel / 非 Channel 测试后端建立同一套“发现 → 加载 → 启用 → 配置修改 → 停用 → 修复”用例，并覆盖缺依赖、未启用和配置失败恢复。
- [x] 将原 Python 探测、模块加载和恢复相关代码搬入 `PythonBackend`，保留无后端、前端插件和 PawApp 原行为；不同时做无关清理。
- [x] 由 `channel_adapter.py` 实现现有静态 Channel 后端，把 `_channel_runtime_for()`、候选探测和 Channel 特有启用条件收进该适配范围。
- [x] 逐个替换 `_load_plugin_unlocked()`、`load_all_plugins()`、`activate_plugin_unlocked()`、`reregister_unlocked()`、`repair_dependencies()`、`_update_config_unlocked()`、`set_enabled()` 的执行选择。状态和诊断不能被统一压成“成功”。
- [x] 后端只接收必要的运行设置与服务接口，不持有完整 Loader 再调用其所有私有方法；注册、台账和目录事务仍由公共协调层调用，避免把分支挪进新文件却保留双向耦合。
- [x] 统一使用绑定对象；准备阶段仍复用既有进程和依赖方法，F2 / F3 再整理其内部实现，避免本项同时改全部边界。
- [x] 运行 `python -m pytest tests/unit/plugins/test_execution_backend_lifecycle.py tests/unit/plugins/test_plugin_config_update.py tests/unit/plugins/test_plugin_load_isolation.py tests/integration/test_plugin_types.py -q`。

**验收：** 这些公共操作不再根据 `entry.descriptor`、Channel 名称或运行设置自行选择执行方式；旧插件行为不变。

## F1.3：迁移更新、回滚及恢复的剩余分支

**Files:** `loader.py`、`lifecycle.py`、两执行后端；新增 `tests/unit/plugins/test_execution_backend_transactions.py`，复用 C5 和 reload / failure recovery 测试。

**接口：** 更新事务保存 `old_binding / candidate_binding / old_restore_state`；回滚读取已恢复的旧 Manifest，并选择旧描述对应的后端，不能拿候选后端恢复旧版本。

- [x] 测试作者覆盖加载中更新、停用重装、`force`、无活动实例卸装、依赖修复、prepared 恢复；故障覆盖探测失败、注册失败、启动失败和停止未完成。
- [x] 迁移 `reload_plugin_unlocked()`、`_load_plugin_from_path_unlocked()`、`_rollback_reload()` 和恢复相关执行判断；目录 staging / backup / marker 仍由原事务负责。
- [x] 候选发布后验证源身份并重建正式目录描述；回滚后重建旧目录描述。测试验证启动器不会引用已移走的 staging，也不会把候选摘要套在旧版本上。
- [x] 把静态进程源目录不可原地替换的限制交给后端 `validate_update()`；保持 Python 原有可支持行为。
- [x] 本轮明确拒绝同一已安装插件在更新中切换执行后端，且在停止旧实例、修改目录前拒绝。后续若要支持，另行设计迁移，不能隐式猜测。
- [x] 保留全局 enabled、工作区配置、代际、停止回执、`unchanged / needs_restart` 语义；候选准备不连接真实平台。
- [x] 运行 `python -m pytest tests/unit/plugins/test_execution_backend_transactions.py tests/unit/plugins/test_plugin_reload.py tests/unit/plugins/test_plugin_failure_recovery.py tests/unit/plugins/channels/test_c5_transactions.py -q`。

**验收：** 增加一个测试后端，无需修改 Loader / Lifecycle 的更新、配置或恢复分支。`LifecycleDelegate` 的五处责任判断继续保留。

## F2.1：建立统一资源归属并迁移探测进程

**Files:** 新增 `ownership.py`；修改 `registry.py`、`loader.py`、`lifecycle.py` 和执行后端；新增 `tests/unit/plugins/test_runtime_ownership.py`。

**接口：** `RuntimeKey(kind, name, scope_id)` 区分能力及稳定作用域；`RuntimeLease(plugin_id, key, instance_id, generation, code_root)` 是精确归属凭据。沿用现有 Channel 的对外 instance_id 生成方式，避免本轮改变消息身份。

- `RuntimeOwners.reserve(plugin_id, key, code_root, *, instance_id: str) -> RuntimeLease`：无 await 地检查并占位、分配代际；同一槽位跨插件也不得重复占用。
- `attach(lease, stop: Callable[[], Awaitable[RuntimeStopReceipt]]) -> None`：在首次异步启动前绑定停止适配器。
- `release(lease, receipt: RuntimeStopReceipt) -> bool`：核对精确 lease；仅停稳且无残留时释放，旧 lease 不得删新实例。
- `leases(plugin_id: str | None = None) -> tuple[RuntimeLease, ...]`、`stop_all(plugin_id: str) -> tuple[RuntimeStopReceipt, ...]`：统一枚举和清理。
- `RuntimeStopReceipt` 至少包含 `quiescent / detail / exited / returncode / forced / residual`；进程退出与业务排空不可相互替代。

这里登记的是清理责任和归属证据，不接管进程监督。现有台账记录同一 lease 的撤销动作，不能另启一份清理链。

- [x] 测试作者覆盖重复 reserve、精确释放、启动取消、停止异常、残留后重试、不同作用域并行，以及旧回调不能删除新代际。
- [x] 实现登记表及回执。构造尚未开始即失败时允许凭“未产生资源”的明确证据释放，不能把所有启动异常都当成无资源。
- [x] 将 `_channel_preparations` 迁入统一登记，覆盖 Channel 探测和 `_prepare_python_dependencies()` 的普通 `PluginProcess`，消除第二份可写 owner 表。
- [x] 将 staging 保留、目录切换前检查、全局退出枚举改为查询通用归属；即使 PluginRecord 丢失，也能找到并清理残留探测进程。
- [x] 停止失败保留 code_root 和 lease，返回 `needs_restart`；不能在 finally 中无条件删除登记。
- [x] 运行 `python -m pytest tests/unit/plugins/test_runtime_ownership.py tests/unit/plugins/test_plugin_process_execution.py tests/unit/plugins/channels/test_c5_transactions.py -q`。

**验收：** Loader 不需要知道仍占用目录的资源属于 Channel 还是其他组件。已有执行底座仍是实际进程的唯一监督者。

## F2.2：迁移 Channel 活实例与工作区投影

**Files:** `registry.py`、`api.py`、`workspace_projector.py`、`app/channels/manager.py` 及文件归属表列出的工作区调用点；新增 `tests/unit/plugins/test_generic_workspace_ownership.py`。

**接口：** Channel 保留自身 `ChannelHandle / StopReceipt`，在适配边界转换为 `RuntimeLease / RuntimeStopReceipt`。`WorkspaceIntent` 增加默认兼容的 `can_apply(workspace) -> bool`、`binding_is_current(workspace, token) -> bool` 回调。

- `project_workspace(workspace, *, kinds: Collection[str] | None = None)`（async）：按意图投影；调用者可以请求 Channel，但公共实现不访问 channel_manager。
- `replace_binding(kind, name, plugin_id, workspace, *, expected, token) -> bool`：以精确旧 token 和工作区身份更新绑定。
- `start_allowed(kind, name, plugin_id, workspace) -> bool`：统一拒绝撤销中 / 退休工作区的启动。

- [x] 测试作者先覆盖现有 Channel 交接，再用两个普通资源组件验证同一 Projector 能管理它们；覆盖正在准备的工作区、相同 agent_id 的旧新对象及撤销期间替换。
- [x] Manager 在启动前 reserve，绑定调用其完整停止流程的回调；原队列排空、在途 Agent 和连接停止逻辑保留。严禁用 `process.stop()` 代替 Channel 完整停止。
- [x] `_channel_owners / _channel_generations` 改用统一登记；旧辅助方法若暂保留，只能是转发入口，不能双写两套状态。
- [x] Channel 的配置门槛、`_defer_channel_start`、manager 身份检查放在 `_project_channel()` 的回调内；公共 Projector 不读取 `token.channel`。
- [x] 公共 Projector 处理通用停止结果；保留旧贡献撤销返回 None 的兼容行为，新进程组件必须给出明确停止证据。失败继续保留工作区和 token。
- [x] 将 `_has_channel_custody()`、`_shutdown_channel_owners()` 和 `unload_all()` 的特殊枚举替换为通用归属操作；不改变现有 Memory 使用中拒绝卸载规则。
- [x] 运行 `python -m pytest tests/unit/plugins/test_generic_workspace_ownership.py tests/unit/plugins/test_workspace_projection.py tests/unit/plugins/channels/test_workspace_handoff.py tests/unit/plugins/channels/test_workspace_runtime.py tests/unit/channels/test_channel_manager_lifecycle.py -q`。

**验收：** 公共归属和投影机制不认识 Channel 对象结构，仍能处理停不稳、重复启动、新旧实例交接与工作区退出。

## F3.1：统一依赖声明与运行使用者

**Files:** 新增 `dependency_usage.py`；修改 `registry.py`、执行后端、`ownership.py`；新增 `tests/unit/plugins/test_dependency_usage.py`。

**接口：** `DependencyEnvironment(executable, sites, marker_environment)` 记录真实环境；`DependencyDeclaration(plugin_id, source_revision, requirements, environment)` 记录已启用 / 正在准入的需求。`DependencyUse` 增加可选 `instance_id / generation / active / snapshot`，沿用现有 `DependencySnapshot`。

- `DependencyUsageRegistry.declare(declaration) -> DeclarationToken`、`withdraw(token) -> None`：精确撤销，候选不能覆盖旧版本声明。
- `observe(lease: RuntimeLease, snapshot: DependencySnapshot) -> None`：将子进程证据关联统一 lease，不另外管理进程。
- `snapshot(environment: DependencyEnvironment) -> UsageSnapshot`：返回 `revision / uses`；活跃资源来自 RuntimeOwners，停止失败的资源仍被纳入。

- [x] 测试作者覆盖 Channel、非 Channel、普通 Python 三种消费者；同一插件的旧实例、新候选声明不得互相覆盖。
- [x] 静态发现读取 requirements，不 import SDK；启用 / 准入登记声明，子进程 audit / hello 更新运行证据。仅安装且停用的插件不自动成为活跃消费者。
- [x] 迁移 `channel_dependency_uses()`，移除公共 Registry 对 `_launch_spec`、`channel_process` 的私有字段读取；Channel 适配层主动提供标准数据。
- [x] 普通 Python 插件也登记需求；宿主已经导入的依赖不能因为插件停用就宣称已从环境释放。宿主证据与子进程快照分清来源。
- [x] 环境按实际解释器和可见 site 判断：同一二进制的独立 venv 不自动合并；共享可写 site 的消费者不能因解释器名称不同而遗漏；markers 按消费者实际运行环境评估。
- [x] 卸载 / 回滚精确撤销声明；停止失败保留使用证据，新候选失败恢复旧声明；未能取得真实证据时不得伪报冲突已排除。
- [x] 运行 `python -m pytest tests/unit/plugins/test_dependency_usage.py tests/unit/plugins/channels/test_c4_dependencies.py tests/unit/plugins/test_local_dependency_policy.py -q`。

**验收：** 公共查询同时包含 Channel、普通 Python 及非 Channel 组件，无需增加 `memory_dependency_uses()` 等平行入口。

## F3.2：接入依赖准入、安装、修复与并发保护

**Files:** `dependency_gate.py`、`install_lock.py`、`loader.py`、执行后端；新增 `tests/unit/plugins/test_generic_dependency_gate.py`。

**接口：** `prepare_shared_dependencies()` 改为显式接收 `plugin_id`、`requirements_file: Path`、通用进程 / 审计 spec、`usage_registry`；公共 Gate 不再解析 Channel descriptor 或猜测 channel_key。Channel 后端负责从自身描述构造这些参数。

- [x] 测试作者覆盖 Channel 与非 Channel 的共享依赖冲突、只影响实际共享环境的实例、候选失败恢复、并发安装、发布取消和修复标记。
- [x] 将 `_prepare_python_dependencies()`、Channel 准备、`_acquire_dependency_wheels()`、修复流程全部接到同一依赖使用者查询；保留现有 GateDecision 和错误报告字段。
- [x] 复用环境级跨进程文件锁；环境标识、写入目标及安装锁使用同一套规范化路径。下载可在锁外，写入前必须在锁内重新 audit 并读取最新使用者。
- [x] 同进程内的声明 / 激活准入与发布通过同一个环境级协调入口串行化。锁顺序固定为“插件生命周期锁 → 环境协调 / 文件锁 → 无 await 的归属读写”；持有环境锁期间不等待业务排空或插件停止。
- [x] 发布前后重新核对当前声明和真实依赖；使用者在等待锁期间变化时重新评估，不能把函数调用前捕获的旧 tuple 一直用到发布结束。
- [x] 多进程环境内已安装包仍禁止被隐式替换；候选代码回滚不卸载可能被其他插件使用的新增依赖。冲突报告区分声明冲突、活跃实例和证据不足。
- [x] 运行 `python -m pytest tests/unit/plugins/test_generic_dependency_gate.py tests/unit/plugins/test_dependency_usage.py tests/unit/plugins/channels/test_c4_dependencies.py tests/unit/plugins/channels/test_c5_transactions.py tests/unit/plugins/test_install_lock.py tests/unit/plugins/test_loader_dep_probing.py -q`。

**验收：** 新的 Edge 后端只提交依赖声明和环境证据，不自行扫描 Channel，不另做一套安装 / 冲突检查；并发、取消和恢复保护保持有效。

## F4：联合验证，证明可扩展而不只是改名

**Files:** 新增 `tests/integration/test_plugin_framework_boundaries.py`、`tests/fixtures/plugin_execution/non_channel_backend.py`；复用现有插件执行、C4 / C5 / C7 测试。

- [x] 测试作者提供一个非 Channel 静态组件，通过宿主注册的测试适配器接入正式 BackendResolver / Loader；它使用真实 PluginProcess、代际和 IPC，不绕开正式加载路径。
- [x] 该组件跑通安装、加载、启用、配置修改、停用、更新失败恢复、依赖冲突和残留清理；支持两个稳定作用域。用本地候选目录和 wheel 替身，无外网下载。
- [x] 证明扩展只需新增测试适配器及 fixture，Loader / Lifecycle / RuntimeOwners / DependencyGate 不再添加该组件的名字或类型分支。
- [x] 回归普通 Python、PawApp、前端、Memory 保护、飞书、钉钉；保留真实 Manager / Projector / 子进程链，只替换外部平台和模型副作用。
- [x] 在提取后的发行 wheel 中回归现有 host → child 链，确认新增公共模块被正常打包；兼容当前 frozen runtime 合约，不宣称完成原生桌面实测。
- [x] 检索公共协调代码中的 `entry.descriptor`、`_channel_preparations`、`_channel_owners`、`channel_dependency_uses`、`token.channel`，逐处确认执行选择和归属读取已收口。类型专用适配器、旧配置迁移和发行清单不纳入“零 Channel 字样”要求。
- [x] 独立复核测试断言及异常路径；功能作者不得自行删改失败断言。只在功能有变化、失败或存在未解决疑点时扩大 / 重复测试。

验证命令：

```bash
python -m pytest tests/integration/test_plugin_framework_boundaries.py tests/unit/plugins/test_plugin_process_execution.py tests/contract/test_memory_backend_plugin.py tests/integration/test_plugin_types.py -q
python -m pytest tests/unit/plugins/test_plugin_lifecycle_contracts.py tests/unit/plugins/test_plugin_lifecycle_unload.py tests/unit/plugins/test_plugin_config_update.py tests/unit/plugins/test_plugin_reload.py tests/unit/plugins/test_plugin_failure_recovery.py tests/unit/plugins/test_workspace_projection.py -q
python -m pytest tests/unit/plugins/channels/test_c4_dependencies.py tests/unit/plugins/channels/test_c5_transactions.py tests/unit/plugins/channels/test_c6_runtime.py tests/unit/plugins/channels/test_c7_frozen_runtime_contract.py tests/integration/test_c7_released_process.py -q
git diff --check
```

所有命令在具备现有项目测试依赖的解释器中运行，普通源码测试显式设置 `PYTHONPATH="$PWD/src"` 并确认 `qwenpaw.__file__` 指向当前 checkout；发行包测试按 fixture 设置自己的隔离路径。`QWENPAW_WORKING_DIR`、`QWENPAW_SECRET_DIR` 指向本次临时目录；不得使用用户日常数据目录。新增测试必须先在缺实现时按预期失败，再通过；既有基线用例不要求人为制造失败。

## 实施补充：已经评审的公共接口约束

- 执行适配器可提供 `dependency_targets()`、`audit_dependencies()`；`prepare()` 可返回 `DependencySnapshot`。前者只提供等待目标，实际启用仍须取得真实环境证据。静态发现可以报告不可用状态，不能因此被迫导入 SDK。
- 统一归属表保留精确 lease、资源和停止回执。目录操作在同一归属机制内取得短期变更保护；停止失败继续保留来源目录与资源，不能仅因进程退出就假定业务已排空。
- `DependencySnapshot.dependency_sites` 表示实际依赖根，完整 `sites` 表示可见路径；二者不等于写入权限。不同插件各自的版本、来源与 marker 环境分别计算，不能把一个插件的全部依赖套给另一个插件。
- `prepare_shared_dependencies()` 处理显式依赖准备，`admit_shared_dependencies()` 处理最终启用前的检查；二者使用实时通用使用者登记。准确的声明 token 在当前任务和当前来源内传递，旧实例、候选和宿主已导入依赖不能互相覆盖。
- 静态目录登记保留同一声明 token，但不等于正在使用依赖。正式准入成功后才提升声明角色；真实运行实例和停止失败的残留始终纳入检查，不能借“只登记目录”绕过保护。
- 同一事件循环中的启用与写入共用异步环境锁，写入另保留跨进程文件锁。下载可在锁外；写入前后重新检查实际环境和使用者。业务连接、排空与停止在环境锁外进行；锁内只允许有界审计及其自身资源清理。
- 依赖仍只允许安全新增；回滚代码不卸载共享新增依赖。已有损坏标记必须明确报错，不根据“版本看起来满足”自动清除。已准备版本沿用固定声明，但最终启用仍检查新鲜的环境证据。
- 所有准备路径通过 `PluginLifecycle.prepare_backend()` 检查依赖环境归属；宿主 delegate 与单次操作限制同时生效。无写入权限时禁止自动获取和提供 wheel 的发布，但允许检查、使用已有依赖，且保留真实审计结果。

## 完成标准与计划自检

- 执行选择只有一个解析入口；新类型不需要修改公共操作分支。
- 探测、准备中、活跃及停止失败的运行资源使用同一归属源；旧代际不能释放新资源。
- 依赖准入看到各类型声明和运行使用者；不读取 Channel 私有字段。
- 非 Channel 的真实进程测试证明三条边界能一起复用；现有行为和恢复保护继续通过。
- 本计划已将五类 Review Focus 分配到对应任务；不包含移走平台规则、第三个正式渠道上线或新版通用发行格式。

F1.1–F4 已完成。分阶段评审、整轮评审及最后一项修正的限定复审均已完成；最终结论为 SPEC Compliant、QUALITY Approved，无剩余阻塞项。代码保留在当前 `codex/channel-pluginization` 分支，未提交或推送。最后验收使用仅包含本轮 1、2、5 改动的冻结副本，同目录另一个任务的 3、4 改动保留，其联合验收不计入本轮结论。

验证记录：

- 分阶段运行覆盖普通 Python、Channel、工作区投影、依赖冲突与取消、更新失败恢复及提取后的发行 wheel；使用非 Channel 组件和真实子进程验证公共边界，无真实机器人、Desktop 或 Docker 实测。
- 最终依赖权限修正先复现 3 项实际越权写入；修正后的完整定向文件为 14 项通过、1 项测试前置假设修正，随后该项与 3 项 Python/Channel 兼容检查全部通过。未将分次结果表述成新的一次全量通过。
- 早期联合运行有 1 次工作区测试的 2 秒超时，保持原断言的后续两次检查均通过；未复现原始原因，不宣称已修复该计时问题。既有第三方与兼容接口告警保留。
- 涉及文件的代码检查通过，`git diff --check` 通过。完整命令、失败归因和限定复审保存在 `.superpowers/sdd/2026-10-13-plugin-framework-boundaries/`。
