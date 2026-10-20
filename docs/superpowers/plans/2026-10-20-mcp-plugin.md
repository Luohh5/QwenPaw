# MCP 插件化与 DriverProtocolSpec 实施方案

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 MCP 实现为可选官方插件，并增加公开 `DriverProtocolSpec` SDK，使已有 MCP 用户升级后保留配置和功能，未使用 MCP 的用户升级后不自动安装或启用插件。

**Architecture:** 保留协议中立的 Driver 存储、策略、审批和能力分发，改为按需创建运行时。MCP 插件通过公开协议接口提供连接、发现、调用和关闭；宿主负责工作区、来源和调用的归属。配置监测保留现有两秒扫描，仅在插件启用且工作区需要监测时运行；前端保留原入口和初始界面，由用户在页面内安装插件。通过一次性使用情况判定决定升级时是否安装插件，避免把默认配置误判为用户使用。

**Tech Stack:** Python、现有插件 SDK、Driver runtime、MCP Python SDK、现有两秒配置轮询、FastAPI 兼容路由、React/TypeScript、pytest。

**Spec:** 本文的[设计与验收约定](#设计与验收约定)是后续任务共同遵守的设计契约。

基线：`codex/mcp-plugin`，`56be46ddf`。实施进度以各项勾选和实际验证记录为准。

## Global Constraints

- 本期只有两个交付物：MCP 官方插件，以及包含 `DriverProtocolSpec` 的公开 SDK 扩展。
- 不实施 Agent Mail 或 ACP 的插件化，不为它们增加新功能、迁移方案或独立实施任务；已有消费者通过宿主兼容接口继续工作。
- 插件 ID 固定为 `qwenpaw-mcp`，协议名固定为 `mcp`。SDK 从 `1.0.0` 增量升级到 `1.1.0`，新增 capability `drivers`，保持 SDK 1.0 插件兼容。
- 新 MCP 插件只能依赖公开 SDK；不得导入 `qwenpaw.drivers.*`、`qwenpaw.app.*`、内部 Registry/Manager，或使用 monkeypatch。
- 保留已有 DriverCard、凭据引用、OAuth 信息、策略、工具白名单、工具名称、capability ID 和各来源的 `enabled` 状态。
- 不使用删除配置或凭据的方式实现禁用、卸载和减负；不因插件不可用而丢弃旧配置。
- 保留现有两秒配置扫描机制，只改变扫描的启停条件。配置监测需求与服务连接状态分开，来源暂时停用不能自动取消用户需要的手工编辑监测。
- 保留 MCP 的现有入口、路由和初始界面；未安装时在原页面提供安装操作，禁用或卸载插件不能使入口消失。
- 启动升级不得联网下载插件，不得后台运行 pip/uv，不得覆盖已有用户插件代码；已安装但明确禁用或卸载的插件不得被自动恢复。
- 本期目标是 QwenPaw 的运行时减负。由于仍有共享消费者和上游传递依赖，不以安装环境完全没有 `mcp` 包或进程完全不导入 `mcp.*` 作为本期验收条件。
- 测试必须显式使用当前仓库 `src` 和临时配置、工作区、凭据目录。目标依赖与项目声明一致，尤其是 `agentscope==2.0.10`；不读取或修改开发者真实配置。
- 支持项目现有 Python 版本和 macOS、Linux、Windows；桌面 frozen 构建与 Python 安装使用相同的安装判定。

## Review Focus

1. 默认禁用 AnySearch、其历史迁移卡片和单独的迁移水位，不能触发安装；任务 1、6、8 固定此行为。
2. 自定义但当前已停用的来源必须保留，升级不能把停用状态变成连接；任务 1、6、8 覆盖。
3. 升级中途退出、文件复制失败和用户显式卸载后重启，不能产生半安装或自动复活；任务 6、8 覆盖。
4. 两个工作区存在同名服务和工具时不能串配置、凭据、权限或连接；任务 3、4、8 覆盖。
5. 初始化一半、正在调用或关闭超时时禁用/更新插件，必须保留完整资源归属；任务 3、5、8 覆盖。

## 设计与验收约定

### 1 范围与完成标准

用户可从现有 MCP 入口进入初始界面，在页面内安装插件；也可通过插件管理界面启用、禁用和卸载 MCP。启用后的配置与使用方式保持连续。开发者可以只使用 `qwenpaw.plugins` 公开接口编写协议插件，宿主不再硬编码 MCP handler 的注册。

两个发布门槛必须同时满足：

| 用户类型 | 升级后的安装和启用状态 | 配置和运行要求 |
| --- | --- | --- |
| 已配置并启用 MCP 的用户 | 从本地发布资源事务式安装并启用 `qwenpaw-mcp` | 所有来源配置保留；原启用来源可连接、发现和调用工具；原停用来源不连接 |
| 从未使用 MCP、仅有系统默认残留的用户 | 不安装、不启用 | 插件安装目录无 `qwenpaw-mcp`；加载列表无此插件；无 MCP DriverManager、连接和专用定时扫描 |

自定义但全部停用的配置按“已有配置”保留：可安装管理插件，但不改变任何来源的停用状态。已有插件显式禁用/卸载记录具有最高优先级，升级不能代替用户重新启用。

本期仅保留已有消费者需要的兼容调用面。非空来源请求可以通过该通用调用面触发协议按需装载；空请求不能触发。显式禁用/卸载状态禁止隐式装载。这不要求修改或插件化现有消费者自身。

### 2 当前需要改变的启动路径

当前工作区固定注册 `driver_manager`，工厂无条件导入 MCP handler，随后为每个成功创建的 manager 启动默认 2 秒的 `DriverConfigWatcher`。空目录不会建立 MCP 连接，但仍承担初始化、具体模块导入与周期快照开销。

实施入口：

- [工作区服务注册](../../../src/qwenpaw/app/workspace/workspace.py)
- [服务工厂](../../../src/qwenpaw/app/workspace/service_factories.py)
- [Driver watcher](../../../src/qwenpaw/app/driver_config_watcher.py)
- [通用 DriverManager](../../../src/qwenpaw/drivers/manager.py)
- [请求期能力组装](../../../src/qwenpaw/runtime/builder.py)

改造后，核心启动仅注册一个轻量惰性服务。读取配置列表、空工具目录和空来源清理均不构造真正的 `DriverManager`；存在启用来源或明确运行请求时才创建。只为停用配置提供管理和监测时，不创建 transport；需要手工编辑后自动生效的工作区可以单独保留扫描任务。

### 3 安装状态与升级判定

“未安装”定义为：没有将插件发布到 `get_plugins_dir()/qwenpaw-mcp`，不进入插件发现/加载列表，也不写入 `enabled=true`。发布包可携带不可自动发现的离线插件资源，供旧用户迁移或用户显式安装使用；资源存在不等于插件已安装。资源目录不得加入 `PluginLoader.plugin_dirs`。

使用只读判定函数，不先构造 DriverManager、不执行旧迁移、不导入插件 entry、不解析凭据明文，也不访问外部服务。

| 判定结果 | 证据 | 操作 |
| --- | --- | --- |
| `unused` | 无 MCP 数据，或只有已知版本的系统默认禁用 AnySearch、默认迁移卡片、空配置/空凭据占位、迁移水位 | 不安装、不启用；每次启动只读判定，不写安装事务记录 |
| `configured` | 有用户启用的有效来源，或自定义来源、非默认策略/白名单/OAuth/凭据绑定等明确配置证据；允许当前停用 | 本地安装并启用管理插件，逐来源保留原 `enabled` |
| `explicit_off` | 插件已有明确禁用、卸载或不再自动安装记录 | 不自动安装或重新启用；保留全部数据 |
| `needs_review` | 文件损坏、未知旧结构、同名冲突，且不能安全确定状态 | 保留原文件，给出可操作诊断，不自动连接、不伪造迁移成功 |

判定必须覆盖所有已配置工作区，包括未启动、已禁用工作区。聚合时全局 `explicit_off` 优先；否则任一工作区为 `configured` 即可安装一次插件，但只恢复有效、启用的来源，不能连带启动其他工作区。其余工作区的 `needs_review` 不得被覆盖成成功；只有全部为 `unused` 时才完成“未使用”判定。

不能用以下条件判断“使用过 MCP”：`mcp` 字段存在、`clients` 非空、`drivers/mcp` 目录存在、`migration_version > 0`。旧版会自动提供 AnySearch，并可能为默认来源生成卡片。建立来自本次支持升级版本的默认指纹 fixture，比较规范化的 endpoint、配置、策略和凭据绑定，不以文件名或服务名相同直接认定为默认。任何自定义差异都不得被当成默认删除。

未使用状态不持久化为可跳过盘点的完成标记：用户可能在两次启动间新增配置，仍需读取最新证据。只读启动盘点与周期扫描不同；未安装或未启用插件时，仍不创建 MCP Manager、连接和专用周期任务。

历史版本没有可靠记录“曾经启用但后来删净配置”的事件；如果已恢复为完全相同的出厂状态且没有其他证据，则按默认状态处理。方案保证保留现存配置，不承诺恢复用户已经删除的数据。

### 4 公开 SDK 契约

新增 `qwenpaw.plugins.drivers`，公开下列有限类型。所有配置和身份快照必须深拷贝并只读；不返回内部对象或宿主文件路径。以下名称和签名是本方案的接口约定。

| 类型或接口 | 契约 |
| --- | --- |
| `DriverSource` | `source_id`、`protocol`、只读 `endpoint/config`、`enabled`；敏感值使用绑定引用，不能包含宿主对象 |
| `DriverCapabilitySpec` | 工具 `name`、`description`、`input_schema`、`output_schema`、`tool_name`、`enabled`、JSON `metadata`；宿主据此生成稳定 capability ID |
| `DriverCallResult` | `content: tuple[ContentBlock, ...]`、`is_error: bool`、`structured_content: JSONValue`、JSON `metadata`；不返回 MCP SDK/AgentScope 类型 |
| `DriverRequestContext` | 宿主提供的工作区实例、会话、用户、渠道身份、deadline、取消检查；插件不能修改身份或决定审批结果 |
| `DriverCredential` | 本来源凭据的 `kind`、`revision: int`、只读 `public/secrets`；只交给受限来源上下文，不进入日志或 API 响应 |
| `DriverCredentialAccess` | 只读 `aliases: tuple[str, ...]` 列出本来源声明的别名；`resolve(alias: str) -> Awaitable[DriverCredential]` 读取原始版本化记录；`resolve_runtime(alias: str) -> Awaitable[Mapping[str, JSONValue]]` 按已声明种类惰性生成只读运行值（复用通用凭据 provider，不暴露引用）；`update(alias, *, expected_revision, public, secrets) -> Awaitable[None]`；只允许本来源已声明别名 |
| `DriverContext` | `plugin_id/generation/workspace_id/workspace_instance_id/source_id/source_revision/session_scope`、`source: DriverSource`、`resources`、`logger`、`credentials`；`invalidate_capabilities() -> None` 仅标记本来源缓存失效；`subprocess_environment() -> Awaitable[Mapping[str, str]]` 按明确请求返回只读的用户持久化环境快照，不返回整个宿主进程环境 |
| `DriverConnection` | `open() -> Awaitable[None]`；`discover(request: DriverRequestContext) -> Awaitable[tuple[DriverCapabilitySpec, ...]]`；`invoke(name: str, arguments: Mapping[str, JSONValue], request: DriverRequestContext) -> Awaitable[DriverCallResult]`；`close() -> Awaitable[None]` |
| `DriverProtocolSpec` | `protocol: str`；`validate: Callable[[DriverSource], None]`；`create: Callable[[DriverContext, DriverSource], DriverConnection]` |
| `PluginContext.register_driver_protocol(spec: DriverProtocolSpec) -> Registration` | 登记协议描述及撤销操作；同名协议由其他插件占用时明确失败；注册阶段不连接、不扫描工作区 |
| `AppContext.driver_source(protocol: str, source_id: str) -> AbstractAsyncContextManager[DriverContext]` | 为当前已鉴权工作区的持久来源建立管理作用域；仅允许本插件登记的协议，不自动创建 manager/transport，不可访问其他会话的临时来源 |

`JSONValue` 沿用公开 JSON 类型；`ContentBlock` 沿用 `qwenpaw.plugins.messages`。`structured_content` 无值时为 `None`。`DriverSource` 的配置校验及回调输入不能借只读外壳保留可变嵌套引用。

`create()` 只创建连接对象，不产生外部副作用。宿主先创建来源资源作用域并登记清理，再调用 `open()`。凭据更新沿宿主加密存储与并发版本检查；别名不能转换成任意 credential ref。

管理路由通过 `AppContext.driver_source()` 获取配置和受限凭据上下文，以便插件完成 OAuth 等操作，不必导入宿主存储。跨工作区沿已有 `for_workspace()` 鉴权；OAuth callback 的兼容桥必须先验证已保存的 state 及其绑定身份，再创建同等受限的来源上下文，不能把 callback 参数当成授权。创建 OAuth 凭据前由宿主配置路径声明别名，插件不能自行扩展凭据访问范围。

宿主在公开连接外包一层内部 SDK Driver adapter：先做来源作用域、工具启用和策略/审批检查，再调用插件；`open/discover/invoke/close` 均纳入插件活动计数和受管清理。不能只把内部 `DriverHandler` 改名导出，也不能要求插件调用内部 `_guarded_execute()`。

复用现有请求期 Driver 工具组装，不使用普通 `register_tool()` 批量注册 MCP 工具，不把动态工具写入所有工作区的 builtin 配置。

### 5 运行状态与资源管理

| 状态 | 要求 |
| --- | --- |
| 插件缺失/禁用 | 无 MCP 实现导入、来源连接和专用 watcher；只读配置与诊断可用 |
| 插件启用、没有启用来源 | 没有其他 Driver 时真正的 manager 实例数为零、连接数为零；是否扫描按工作区的配置监测需求决定 |
| 来源启用 | 按工作区实例与来源建立连接，后台连接状态可观察，不把全部连接错误推迟到首次调用 |
| 来源配置变化 | 校验并准备新 revision，再切换；失败保留旧实例，不对其他来源重连 |
| 来源停用/删除 | 阻止新调用，等待结束或受控取消，再关闭；删除操作与禁用操作的数据语义保持区别 |
| 插件禁用/更新/卸载 | 撤销所有来源，遵守忙检测与中断规则；关闭失败保留归属并阻止覆盖 |
| 最后一个来源退出 | 回收相关连接；无其他 Driver 时回收 manager。若仍需手工编辑后自动生效，保留扫描任务；插件停用或工作区退出时停止该任务 |

来源归属至少包含插件 generation、真实工作区实例 token、来源 ID/revision 和会话 scope。连接不能挂在一次工具调用结束即清理的临时作用域下。

必须修复 `DriverManager.build_drivers()` 的初始化取消窗口：部分连接已经打开、其余仍在初始化时，成功资源即使还没有进入 `_handlers`，也必须能被取消和关闭。失败清理不能仅记录日志后宣布已停止。

配置更新继续沿现有路径：Console/API 保存后直接应用来源变更；手工 YAML 编辑由现有 `DriverConfigWatcher` 每两秒扫描后生效。本期不替换配置监测机制，扫描行为固定如下。

| 情况 | 扫描行为 |
| --- | --- |
| MCP 插件未安装或未启用 | 完全不启动 MCP 配置扫描 |
| 插件启用，工作区需要监测 MCP 配置 | 保留现有两秒扫描 |
| 服务暂时停用，但用户仍需要手工编辑配置后自动生效 | 可以继续监测，不建立服务连接 |

工作区监测需求依据已有用户配置和明确的使用/监测意图判断，不能仅由当前活动 handler 数量决定，也不能因其他工作区启用 MCP 就给全部空工作区启动扫描。无需新增用户必选开关。watcher 可使用轻量卡片存储和惰性服务，不依赖已创建的真实 manager；扫描发现启用的来源后，再按需建立连接。保持现有 snapshot、变更比较和两秒间隔，插件停用、卸载或工作区退出时停止对应扫描。

### 6 API 与界面

保留 `/mcp` 深链、现有全局及 agent-scoped MCP API、OAuth callback URL、请求和响应形状。宿主保留轻量配置/兼容入口，插件承接协议执行与 OAuth 网络流程。一般插件管理 HTTP 使用现有公开 PawApp，兼容路径在宿主转发，不公开任意 FastAPI app。

读取配置列表不激活运行时；插件缺失时能展示“配置已保存，插件未安装/未启用”。工具发现、连接测试和 OAuth 等运行操作在插件不可用时返回一致的 503 与稳定错误码 `mcp_plugin_unavailable`，不返回裸 ImportError。已有 API 的错误响应形状保留，错误码放入其可扩展 detail 结构。

核心保留现有 MCP 导航入口、`/mcp` 路由和初始界面。在原本适用的 backend 与权限范围内，插件是否安装不改变入口可见性。初始界面展示功能说明、插件状态和安装操作，不能只剩一个没有内容的跳转壳，也不能通过进入页面自动安装或启动后端运行时。

| 插件状态 | 原 MCP 页面内的行为 |
| --- | --- |
| 未安装 | 展示初始界面和“安装 MCP 插件”操作，用户点击后安装 |
| 已安装但未启用 | 保留初始界面，展示“启用 MCP 插件”操作 |
| 安装/启用中或失败 | 在当前页面展示进度或错误与重试操作，不丢失入口 |
| 已启用 | 在同一 `/mcp` 页面提供原有配置管理和使用能力，不要求跳转到陌生位置 |
| 禁用或卸载后 | 回到初始界面，继续保留导航入口和路由 |

与运行插件绑定的完整管理组件可由插件前端动态挂载，未安装时不请求该 bundle；初始界面的组件、样式和安装引导保留在核心。复用公开 route 包装等挂载能力并补齐 SDK 发布声明，卸载时只清理插件注入的部分，不移除核心 MCP 入口、路由和初始界面。其他 backend 原有的 discovery 入口继续保留。

### 7 本地发布资源与升级事务

插件源码位于 `plugins/protocol/mcp/`，离线发布资源位于 `qwenpaw/plugins/bundled/optional/mcp/`。两者是同一构建输入的发布形态，不能维护两套实现。资源仅在 `configured` 升级或用户明确安装时进入真实插件安装目录。

新增宿主升级入口，顺序固定为：只读盘点 → 分类 → 校验本地资源和依赖 → 使用现有插件安装事务发布文件 → 持久化插件设置 → 注册协议 → 恢复启用来源 → 完成升级记录。Driver 协议插件必须在工作区来源启动前完成登记；启动顺序按通用 `drivers` capability 识别，不能给 MCP 单独开隐藏启动通道。

独立升级记录放在运行根目录的 `.mcp-plugin-migration.json`，schema version 为 `1`，记录每个工作区判定、资源版本/摘要和事务阶段，不记录秘密。它与 `MCPConfig.migration_version` 分离：前者防止插件重复安装/复活，后者仍负责旧配置转换。

失败和重启按阶段恢复；文件未完整发布不得写入 `enabled=true`，配置转换未成功不得提前推进旧水位。复用现有插件安装锁、staging 和恢复能力，不再实现一套目录交换框架。存在用户安装版本时原样保留，显式禁用/卸载标记优先。

## 文件职责与实施顺序

下列路径为计划修改位置；新增文件不表示已经存在。实施时只做对应职责的改动，避免同时重写其他子系统。

| 单元 | 新增或主要修改文件 | 职责 |
| --- | --- | --- |
| 使用情况判定 | 新增 `src/qwenpaw/app/mcp_plugin_migration.py` | 只读盘点、默认指纹、决策与升级编排；逻辑过长时将纯判定拆入相邻模块 |
| 公开 SDK | 新增 `src/qwenpaw/plugins/drivers.py`；修改 `sdk.py`、`sdk_version.py`、`compliance.py` | 有限协议声明、资源上下文与公开导出 |
| 宿主桥接 | 新增 `src/qwenpaw/plugins/_drivers.py`；修改 `registry.py`、`lifecycle.py`、`testing.py` | 协议归属、内部 adapter、活动计数与测试宿主支持 |
| 惰性运行时与配置扫描 | 新增 `src/qwenpaw/app/driver_runtime_service.py`；修改工作区、现有 `driver_config_watcher.py` 与通用 Driver 相关文件 | 无来源时不创建 manager；兼容原调用；按插件状态与监测需求启停现有两秒扫描 |
| MCP 实现 | 新增 `plugins/protocol/mcp/` | entry、manifest、连接/transport、结果转换、OAuth 处理和前端 |
| API 兼容 | 修改 `app/mcp/`、`app/routers/mcp.py`、`mcp_oauth.py`、`driver_config_service.py` | 原地址与配置数据保留，执行委托插件 |
| 发行与前端 | 修改 `pyproject.toml`、`scripts/pack-tauri/qwenpaw.spec`、Console MCP 初始界面与 SDK 发布声明 | 不可自动发现的离线资源、原页面内安装引导、插件管理组件挂载及类型声明 |
| 验收 | 新增分类、SDK、生命周期与升级集成测试；更新插件中英文文档 | 两类升级用户、功能、减负和发布产物验收 |

### Task 1 固定升级用户判定与 fixture

**Files:** 新增 `src/qwenpaw/app/mcp_plugin_migration.py`、`tests/unit/app/test_mcp_plugin_classification.py`、`tests/fixtures/mcp_plugin_upgrade/`。

**Interfaces:** 产出 `classify_mcp_usage(snapshot: MCPUsageSnapshot) -> MCPUsageDecision`。`MCPUsageSnapshot` 包含工作区原始配置/卡片快照、默认版本指纹及显式插件状态；`MCPUsageDecision` 包含 `unused/configured/explicit_off/needs_review`、工作区决策和不含秘密的原因。

- [x] **Step 1:** 写 `test_pristine_and_migrated_defaults_are_unused`：无配置、旧默认 AnySearch、迁移后的默认卡片、仅迁移水位四类均为 `unused`；默认指纹不能吞掉定制差异。
- [x] **Step 2:** 写 `test_custom_disabled_sources_are_preserved`、`test_explicit_off_wins`、`test_inventory_includes_inactive_workspaces`、`test_invalid_snapshot_requires_review`，固定对应判定，断言不导入 transport、不修改文件。
- [x] **Step 3:** 运行 `PYTHONPATH=src python -m pytest -q tests/unit/app/test_mcp_plugin_classification.py`，确认因缺少判定接口而失败。
- [x] **Step 4:** 实现纯判定及版本 fixture。原始配置读取不得通过默认填充后的 `MCPConfig` 来猜测用户操作；只有规范化默认指纹一致才可排除。
- [x] **Step 5:** 重跑同一命令，全部通过；检查没有真实凭据进入 fixture 或原因字段，单独提交本任务。

### Task 2 发布 DriverProtocolSpec SDK 契约

**Files:** 新增 `src/qwenpaw/plugins/drivers.py`；修改 `sdk.py`、`sdk_version.py`、`compliance.py`、必要的 `__init__.py`；新增 `tests/unit/plugins/test_sdk_driver_contract.py`，更新 `tests/typing/plugin_sdk_author.py`。

**Interfaces:** 产出设计第 4 节全部公开类型与 `PluginContext.register_driver_protocol()`，SDK `1.1.0` 与 capability `drivers`。

- [x] **Step 1:** 写公开导入、嵌套只读快照、JSON/schema 校验、重复协议拒绝和旧 SDK 插件兼容测试；`test_driver_plugin_rejects_internal_imports` 必须拒绝内部 Manager 导入。
- [x] **Step 2:** 运行 `PYTHONPATH=src python -m pytest -q tests/unit/plugins/test_sdk_driver_contract.py`，确认新接口相关用例失败。
- [x] **Step 3:** 实现 DTO/Protocol 和登记接口；同步 PUBLIC_API、API_EXPORTS、版本、capability、类型样例。公开模块顶层不能导入 MCP 或宿主运行时。
- [x] **Step 4:** 重跑测试；用独立进程仅导入 `qwenpaw.plugins.drivers`，核对没有新增自有 MCP handler/transport 导入。
- [x] **Step 5:** 更新公开 API 清单与对应检查，独立提交。本任务不包含实际 MCP 连接。

### Task 3 将协议回调接入宿主治理与生命周期

**Files:** 新增 `src/qwenpaw/plugins/_drivers.py`；修改 `registry.py`、`lifecycle.py`、`testing.py`、`src/qwenpaw/drivers/manager.py` 和必要的内部 adapter；新增 `tests/unit/plugins/test_sdk_driver_lifecycle.py`。

**Interfaces:** 消费 Task 2；内部桥将 `DriverConnection` 转成现有 Driver 能力，提供经鉴权的 `AppContext.driver_source()` 管理作用域。测试宿主增加 `open_driver(source, *, workspace_id)`、`list_driver_capabilities(source_id, *, workspace_id)`、`call_driver(source_id, name, arguments, *, workspace_id)`、`close_driver(source_id, *, workspace_id)` 四个测试入口，沿生产策略和桥接路径执行，不直接调用插件业务函数。

- [x] **Step 1:** 使用只依赖公开 SDK 的 fake 协议写 red tests：发现/调用/结果转换、allow/deny/ask、凭据别名越权拒绝、同名来源的双工作区隔离；管理作用域读取停用来源不创建连接，跨插件/会话来源访问被拒绝。
- [x] **Step 2:** 写 `test_driver_calls_block_plugin_reload`、`test_partial_open_cancel_closes_every_created_connection`、`test_failed_close_retains_owner`、`test_old_revision_cannot_publish`。
- [x] **Step 3:** 运行 `PYTHONPATH=src python -m pytest -q tests/unit/plugins/test_sdk_driver_lifecycle.py`，确认缺少桥接和清理能力导致失败。
- [x] **Step 4:** 实现桥接、来源级资源账本、受限凭据能力和调用活动计数；修复并发初始化取消窗口。审批和白名单检查由宿主先执行，插件不能绕过。
- [x] **Step 5:** 重跑新测试及 `tests/unit/drivers/test_transient_scopes.py`，全部通过后提交；保留现有内部来源调用签名。

### Task 4 实现仅依赖 SDK 的 MCP 插件后端

**Files:** 新增 `plugins/protocol/mcp/plugin.json`、`plugin.py`、`requirements.txt`、连接/transport/结果转换/OAuth 模块；新增 `tests/unit/plugins/mcp/`，迁移原 MCP handler/transport 测试。

**Interfaces:** 消费 Task 2、3；manifest 声明 `sdk.version=1.1.0` 和 `drivers`，通过 `DriverProtocolSpec(protocol="mcp", ...)` 登记。资源在安装前不可自动发现。

- [x] **Step 1:** 通过公开协议入口验证 stdio、SSE、streamable HTTP 的发现、治理与调用；同时迁移保留细粒度 transport 单元断言，固定超时、HTTP 协议兼容/回退、认证、重连、取消、白名单、名称转换和结构化结果。
- [x] **Step 2:** 运行 `PYTHONPATH=src python -m pytest -q tests/unit/plugins/mcp`，确认插件缺失或不符合契约导致失败。
- [x] **Step 3:** 迁移已有实现并适配公开 DTO，不顺便替换协议栈。移除内部导入；连接运行时与管理 HTTP 分文件，entry 仅登记协议和管理能力。
- [x] **Step 4:** 通过 Task 3 的宿主测试入口验证发现、策略、实际调用和关闭；保持原 capability ID、工具名称、内容块、错误和结构化结果。
- [x] **Step 5:** 执行 `PYTHONPATH=src python -m qwenpaw.plugins.check plugins/protocol/mcp`；第三方 MCP 依赖允许，宿主内部依赖为零；测试通过后提交。

### Task 5 改造工作区为惰性 Driver 服务并按需保留两秒扫描

**Files:** 新增 `app/driver_runtime_service.py`；修改 `app/workspace/workspace.py`、`service_factories.py`、`driver_config_service.py`、`driver_config_watcher.py`、`runtime/builder.py`；新增 `tests/unit/app/test_lazy_driver_runtime.py`、`test_mcp_config_watcher_activation.py`。

**Interfaces:** 内部 `LazyDriverService` 保留当前消费者实际使用的方法和存储访问面；`workspace.driver_manager` 兼容入口可返回该轻量 facade。它不是 SDK 公共 Manager，空查询不创建实际 manager。现有 watcher 读取轻量卡片存储并调用惰性服务；扫描启停由插件状态和工作区监测需求决定，默认间隔保持 `2.0` 秒。

- [x] **Step 1:** 写未安装、未启用以及工作区无 MCP 来源且无监测需求测试：没有其他 Driver 时真正的 manager 构造计数为零，工具目录为空，无连接、子进程、MCP 扫描任务和周期 snapshot；空清理不触发装载。
- [x] **Step 2:** 写首次非空来源并发触发只创建一次、最后来源退出回收 manager、工作区替换不复用旧实例、其他协议仍可用的测试。最后一个来源停用后，只要仍需手工编辑自动生效，扫描必须继续且连接数为零。
- [x] **Step 3:** 写 `test_monitoring_keeps_two_second_interval`、`test_disabled_source_can_be_enabled_by_manual_edit`、`test_plugin_disable_stops_config_scan`；保留文件新增/删除/原子替换/目录首次创建的扫描回归测试。用可控时钟验证下一轮扫描后更新，不引入文件事件监听。
- [x] **Step 4:** 运行 `PYTHONPATH=src python -m pytest -q tests/unit/app/test_lazy_driver_runtime.py tests/unit/app/test_mcp_config_watcher_activation.py`，确认现有无条件初始化和扫描路径无法通过。
- [x] **Step 5:** 实现惰性 facade 及现有 watcher 的条件启停；移除工作区无条件注册具体 MCP handler 和 watcher 的路径，保持两秒间隔与 snapshot/变更检测逻辑。已有兼容方法委托通用来源服务，不修改现有消费者业务模块。
- [x] **Step 6:** 重跑同一命令及现有 Driver 工作区生命周期集成测试，核对不使用时无扫描、需要监测时保留扫描、停用服务时只监测不连接三条路径后提交。

### Task 6 实现有条件安装与可恢复升级

**Files:** 扩展 `app/mcp_plugin_migration.py`；修改 `app/_app.py`、插件启动筛选和 `pyproject.toml` 本地资源映射；必要时扩展现有 loader 安装/卸载记录；新增 `tests/integration/test_mcp_plugin_upgrade.py`。

**Interfaces:** 消费 Task 1–5。产出 `ensure_mcp_plugin_for_upgrade(snapshot: MCPUsageSnapshot) -> Awaitable[MCPPluginUpgradeResult]`，结果为 `unchanged/installed/skipped/needs_review/failed`，附无秘密诊断。明确使用请求复用同一安装协调器，不能绕过显式关闭或另写安装器。

- [x] **Step 1:** 写旧启用用户升级测试：从真实旧格式 fixture 启动，在网络被禁用时安装一次插件，完整保留多来源配置，并通过 fake 协议服务完成 list/call。
- [x] **Step 2:** 写未使用用户升级测试：默认配置及默认迁移卡片均不创建插件安装目录、不调用安装器/DependencyGate、不出现 loaded plugin、不写 `enabled=true`。
- [x] **Step 3:** 写重复启动、显式禁用/卸载、复制中断、配置提交中断、已有用户插件版本、单个损坏工作区测试。失败不得推进完成状态或破坏原文件。
- [x] **Step 4:** 运行 `PYTHONPATH=src python -m pytest -q tests/integration/test_mcp_plugin_upgrade.py`，确认当前没有条件安装流程而失败。
- [x] **Step 5:** 将同一插件源码打成不可发现的离线资源；实现事务编排、独立升级记录及启动前协议登记。启动只核验现有依赖；缺失时明确诊断，不能联网补装。
- [x] **Step 6:** 重跑测试，连续启动两次并比较安装目录、配置与 marker；在各事务阶段注入中断并验证恢复后提交。

### Task 7 保留 MCP 入口和初始界面并接入插件管理能力

**Files:** 修改 `app/mcp/`、`app/routers/mcp.py`、`mcp_oauth.py` 与相关兼容路由；插件增加管理 PawApp 与前端；保留 Console 现有 MCP 路由、菜单和初始界面，增加页面内安装/启用状态及前端 SDK 类型；新增 `tests/integration/test_mcp_plugin_api.py` 和对应 Console 测试。

**Interfaces:** 保留现有 URL、认证与响应契约；插件执行入口通过公开 PawApp 挂载，旧地址调用宿主内部兼容桥。核心持续拥有 MCP 导航、`/mcp` 路由和初始界面；插件通过发布 SDK 在原页面挂载管理能力，只撤销插件自己注入的部分。

- [x] **Step 1:** 固定现有 CRUD、工具列表、策略、状态切换及 OAuth callback 的请求/响应测试，保存配置查询不得建立连接；无效、过期、重放或跨来源 OAuth state 必须被拒绝。
- [x] **Step 2:** 写未安装时导航和初始界面仍可见、点击安装后在原页面进入管理界面、禁用时可启用、失败可重试、卸载后回到初始界面的测试；同时验证只读状态、503 稳定错误、`/mcp` 深链、未安装不加载插件 bundle、切换工作区不串数据。
- [x] **Step 3:** 先运行 Python 和相关 Console 用例确认失败，再实现兼容桥、公开管理路由及原 MCP 页面内的安装/启用流程；进入页面本身不得自动安装、启用或创建运行时。
- [x] **Step 4:** 补齐前端挂载能力的发布声明及清理，验证禁用/卸载仅撤销插件组件和请求，核心入口、路由和初始界面持续存在；插件不得直接调用内部 Console registry。
- [x] **Step 5:** 重跑 API/Console 测试，验证原页面内安装、启用、管理、禁用和卸载各状态，以及旧工具名、权限和 OAuth 路径后提交。

### Task 8 完成升级与减负发布验收

**Files:** 扩展升级集成测试；新增 `tests/integration/test_mcp_plugin_optional_runtime.py`、`test_mcp_plugin_distribution.py`；更新 `scripts/pack-tauri/qwenpaw.spec`、SDK 检查及 `website/public/docs/plugins*.md`、插件 README；添加发布检查记录。

**Interfaces:** 消费全部前序任务，不引入新的运行接口。

- [x] **Step 1:** 实现下方完整用户矩阵，用子进程冷启动验证安装、加载、工具调用与配置持久化；初始 fixture 和升级结果都必须可检查。
- [x] **Step 2:** 使用本地测试服务验证三种 transport、OAuth mock 服务及断线重连；测试不得访问外部账号或模型服务。
- [x] **Step 3:** 在 1、10、50 个无 MCP 工作区下，各预热后采样 60 秒，记录 5 次空闲 CPU、RSS、启动时间、文件扫描次数和相关任务数；对比同依赖环境的基线中位数，不用未实测百分比作为结论。
- [x] **Step 4:** 对 wheel 和 frozen 资源做静态检查及冷启动：离线资源存在，未使用用户无安装目录/加载记录，旧用户离线可安装；桌面启动不通过另一条路径无条件 seed 插件。
- [x] **Step 5:** 执行新测试、既有插件/Driver 回归、前端测试和 SDK 发布检查；修复失败后，更新 SDK 1.1、安装规则、迁移与故障恢复文档并提交。

实际命令、用户矩阵映射、三十条性能样本和限制见[验证记录](../reports/2026-10-20-mcp-plugin-validation.md)。frozen 验证覆盖 spec 资源布局的冷启动，不代表已构建或签名原生可执行文件；中断恢复采用确定性故障注入，不代表物理断电实验。

## 必须通过的验收矩阵

所有下列项目在本次实施完成前保持未勾选。只读配置中的秘密以受控测试值比较，日志中不得输出明文。

### 已有 MCP 用户升级

- [ ] 旧 `mcp.clients` 尚未转换为 DriverCard：升级后一次性转换，原服务和工具可调用。
- [ ] 已迁移为 DriverCard：不重复转换、不复活已删除客户端、不重置旧迁移水位。
- [ ] 混合 stdio/SSE/streamable HTTP、多工作区同名服务：全部配置保留，互不串用。
- [ ] `command/args/cwd/env/url/headers/timeout`、显示名、凭据引用、OAuth、策略、白名单逐项保真；`tools=[]` 仍表示全部禁止，不能变成无限制。
- [ ] 原启用来源正常连接、发现、调用和重连；原停用来源不连接。
- [ ] 自定义但全部停用：配置可管理、无需重新输入，连接数为零。
- [ ] 插件启用且工作区仍需监测：保持两秒扫描；来源暂时停用期间不建立连接，手工编辑启用后下一轮扫描正常生效。
- [ ] 显式禁用/卸载插件后升级或重启：不恢复安装和启用；数据保留。
- [ ] 断电/退出后继续升级：无半安装、无重复注册、无秘密泄漏。
- [ ] 禁用再启用和插件更新：配置继续可用，失败更新保持可恢复状态。

### 未使用 MCP 用户升级

- [ ] 从无 MCP 字段、旧默认禁用 AnySearch、默认迁移卡片三种起点分别升级，均不安装、不启用。
- [ ] `get_plugins_dir()/qwenpaw-mcp` 不存在；插件 discover/load 列表中无 `qwenpaw-mcp`。
- [ ] 未调用插件安装器、DependencyGate、pip/uv、OAuth 或任何 MCP 外部连接。
- [ ] 核心未加载本项目 MCP handler/transport；上游可能已加载的 `mcp.*` 另行记录，不混淆归因。
- [ ] 真正的 DriverManager 实例数为零；MCP 连接、子进程、专用周期任务和周期 snapshot 次数为零。
- [ ] 重启两次结果一致；迁移判定不因系统默认数据出现而反复改成安装。
- [ ] 用户随后主动安装并配置 MCP：无需修改核心或重新创建工作区即可使用。
- [ ] 原 MCP 导航入口和初始界面仍可见；进入页面不会自动安装或启动运行时，点击安装后可在同一页面继续配置和使用。

### 前端入口与初始界面

- [ ] 插件未安装、未启用或安装失败时，原 MCP 入口、路由和初始界面均保留。
- [ ] 未安装时可在初始界面选择安装；已安装未启用时可在同一页面启用，失败时可重试。
- [ ] 启用后在原 `/mcp` 页面提供管理能力；禁用或卸载后返回初始界面，不移除导航入口。
- [ ] 未安装时不加载插件 bundle；现有其他 backend 的 discovery 入口不受影响。

### SDK 与资源安全

- [ ] SDK 1.0 插件仍可加载；要求 `drivers` 的插件在旧 SDK 上于导入 entry 前明确拒绝。
- [ ] fake 非 MCP 协议也能只靠 `DriverProtocolSpec` 完成发现和调用，证明接口没有硬编码 MCP。
- [ ] 来源、凭据和身份跨工作区/来源越权被拒绝；策略 allow/deny/ask 与既有行为一致。
- [ ] 初始化中断、在途调用取消、工作区替换和关闭超时后无失去归属的任务、连接或进程。
- [ ] SDK 静态检查无内部导入；测试宿主与生产走相同 adapter 和治理路径。
- [ ] 保留现有两秒扫描机制：未安装/未启用 MCP 插件时无扫描，需要监测的工作区继续扫描，暂时停用来源时可以只监测而不连接。

## 最终检查与交付记录

- [ ] 逐条核对 Global Constraints、Review Focus 和用户矩阵，记录实际命令及输出。
- [ ] 在与项目要求一致的 Python/依赖环境运行；不能以另一个 checkout 或 AgentScope 版本的结果代替。
- [ ] 运行 `PYTHONPATH=src python -m pytest -q tests/unit/plugins tests/unit/drivers`，以及本方案新增 app 测试与集成测试。
- [ ] 运行 `npm --prefix console run test:run -- src/plugins src/pages/Agent/MCP` 及 `npm --prefix console run format:check`，记录输出。
- [ ] 运行 `scripts/check_plugin_sdk_artifacts.py`，传入本次构建的 `--wheel`、`--sdist`、已安装该 wheel 的外部 `--python`、`--source-root` 和仓库外新建的 `--output`；不能用源码安装结果替代 wheel 验证。
- [ ] 运行 `scripts/check_plugin_sdk_frontend.py`，以本次 wheel 解包出的 SDK 为 `--package`、已有声明依赖目录为 `--dependencies`、新的隔离目录为 `--output`，核对发布类型和独立作者用法。
- [ ] 对 SDK 类型、协议生命周期、升级判定、前端 API 和发行资源做一次整体复核。
- [ ] 交付 MCP 插件、SDK 1.1 文档、升级 fixture/测试、性能对比及已知限制；本期没有 Agent Mail/ACP 插件化改动。

计划检查只能证明文档完整；所有实现、测试和发布 checklist 必须以实际结果勾选，不能因本方案写完而视为已完成。
