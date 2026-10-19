# 渠道扩展边界代码修改规划

> **For agentic workers:** 后续使用 `superpowers:subagent-driven-development` 或 `superpowers:executing-plans` 逐项执行。功能代码与测试代码分别由两个子 agent 编写；测试根据本文件的行为要求独立设计。本轮已完成实现与分项验收；实际验证范围和既有测试限制见文末。

**Goal:** 新增一个普通多进程渠道时，主要新增插件包和测试，不再修改宿主的平台名单、配置模型映射和公共代理中的平台分支；飞书、钉钉已有行为保持兼容。

**Architecture:** 渠道描述负责静态配置与能力声明，Driver 负责平台事件、会话规则和回复目标转换，公共代理负责标准消息、权限、队列及 Agent 调用。复用已有 PluginLoader、ChannelManager、共享进程和事务恢复机制。

**Tech Stack:** Python、Pydantic、项目已有的 jsonschema、现有 stdio JSON-RPC、pytest、Console 现有 Schema 表单与 Vitest。

**Spec:** 本轮已明确的第 3 项“移除飞书/钉钉准入与配置硬编码”、第 4 项“公共代理退出平台业务判断”，以及本文的“边界与接口约定”。既有行为参考 [C0–C7 接入计划](2026-10-12-channel-plugin-integration.md)，以当前源码和独立行为测试为准。

源码基线：`378e8efcf`，2026-10-13 核对。此前计划中的实现状态说明不作为本次未执行任务的完成证据。

## 范围与方案选择

采用“描述驱动准入 + 标准消息与回复契约 + 有界旧协议兼容”。只扩大白名单不能解决代理的平台分支；把全部插件执行后端一起重构又超过本次范围。

本次允许对 Loader 的渠道运行选择接缝做小改动；不同时实施统一执行后端、通用实例归属、全插件依赖消费者和官方交付重构。官方预装清单、旧配置字段和现有平台专用 UI 可以保留，它们不能再成为第三方渠道接入的必经修改点。

## Global Constraints

- 同一套 Python 依赖；平台代码和 SDK 在子进程导入。发现、列配置、编辑未启用渠道不得启动 Driver 或安装依赖。
- 保留部署允许、插件全局启用、工作区启用三层控制；发现了插件不等于允许启动。
- 保留内置渠道名称保护、插件归属、源码身份、实例代次、停止失败保留资源和更新失败恢复。
- 不改配置存储键、现有会话 ID、已保存回复路由和历史文件。插件缺失时配置仍可读、可编辑，不能据此启动。
- 权限及审批操作者校验留在宿主；共享会话身份不能替代 `acl_sender_id`。
- 保留发送结果 unknown/timeout 语义，不能改成自动重发或成功；协议调整不能抹掉回复资源的持久化与去重。
- 功能与测试分开编写。只允许因本计划明确改变的旧限制更新对应断言，不得放宽其他安全和兼容测试。
- 测试使用真实本地子进程和 IPC、模拟平台及模型；不要求真实机器人或原生桌面/Docker 验收。

## 边界与接口约定

以下名称是本计划拟新增的接口，不是当前已存在的函数。

| 边界 | 约定 |
| --- | --- |
| 运行准入 | `ChannelRuntimeSettings.allows(channel_key: str) -> bool`。`selected_channels=None` 表示允许安装后通过校验的描述型渠道；显式空集合表示全部禁止，非空集合为部署白名单。默认值保持空集合，默认宿主引导显式传入 `None`。 |
| 配置校验 | 新增 `plugins/channel_config.py`：`validate_channel_config(schema: Mapping, values: Mapping) -> dict` 与 `channel_config_defaults(schema: Mapping) -> dict`。统一处理公共字段、静态默认值和 Schema 校验，禁止加载插件 Python 校验器。 |
| 入站转换 | 新增纯数据模块 `channel_protocol/normalization.py`：`normalize_inbound_event(event: InboundEvent) -> dict`，输出代理现有 payload 形状及标准合并键。平台会话规则由 Driver 计算，不由该函数按渠道名称推断。 |
| 主动发送 | 新增 `channel_protocol/targets.py`：`encode_reply_target(*, user_id: str, session_id: str) -> str`、`decode_reply_target(value: str) -> dict | None`。采用带版本标记的 JSON 数据封装，保持 BaseChannel 同步返回字符串的接口；旧平台地址视作 opaque 字符串转交 Driver。 |
| 回复策略 | 新增 `channel_protocol/response_policy.py` 中的 `ResponsePolicy`，字段为 `streaming: bool`、`prepare: bool`、`completion_reaction: bool`。`channel.response.describe` 根据已建立的回复路由返回这三个字段，无平台发送副作用；公共代理不读取 `message_type`、`feishu_thread_id` 等平台字段。 |
| 契约选择 | 新契约通过描述/握手中的 `channel.generic.v1` 能力声明选择，并检查双方一致。旧候选进入冻结兼容路径；未支持的新契约在准备阶段拒绝，不开始目录替换。不要改现有封闭消息的必填字段来隐式升级协议。 |

新契约优先复用 `InboundEvent`、`conversation`、`metadata`、`response_handle`、`channel.response.open/finish`，不再平行建立一套入站协议。标准 metadata 的含义在 G0 固定：`session_id`、`user_id`、`debounce_key`、`is_group` 及现有提及字段；平台扩展值可以随消息传递，但公共代理不解释平台专属键。

旧协议兼容只服务现存飞书/钉钉包：复用公共基类，保留必要的小型旧行为覆盖，禁止复制整份代理，禁止让第三个渠道进入此路径。旧分支保留到已发布旧包的兼容窗口结束；本次不承诺删除兼容层。

## Review Focus

- 自定义配置经过 dict、Pydantic 和 `SimpleNamespace` 三种表示后，SDK 仍未进入主进程，嵌套字段和默认值不丢失：G1/G2 验证。
- 插件禁用、缺包、部署禁用和安装失败不能绕过启用控制，也不能回退加载同名旧实现：G2 验证。
- 共享群会话中的两位成员、线程消息和审批命令不能因标准化而串会话或混淆操作者：G0/G3 验证。
- cron 主动发送、重启后的旧目标和已持久化的回复句柄都能找到原目标：G4 验证。
- 卡片创建或发送后丢失回执、切换插件代次、更新失败时，不能重复发送或丢掉待清理资源：G5/G6 验证。

## 文件职责

| 文件或目录 | 本次职责 |
| --- | --- |
| `plugins/channel_adapter.py`、`channel_bootstrap.py`、`plugins/loader.py` | 去掉固定平台准入和工厂配置映射；只调整渠道选择接缝。 |
| 新增 `plugins/channel_config.py` | 公共配置与描述 Schema 校验、默认值。 |
| `app/channels/registry.py`、`manager.py`、`catalog.py`、`config/utils.py` | 描述型渠道选择、配置数据转换、目录状态及部署允许范围。 |
| `app/routers/config.py`、Console Channels 现有组件 | 单项/批量配置保存、通用表单与运行状态。 |
| `app/channels/isolated_proxy.py` | 消费标准事件、目标、回复策略，移出平台判断。 |
| 新增 `app/channels/legacy_isolated_proxy.py` | 旧协议必要兼容覆盖，不承担新插件接入。 |
| `channel_protocol/normalization.py`、`targets.py`、`response_policy.py` | SDK 无关的纯数据契约；不各自建立管理器或注册系统。 |
| `channel_protocol/shared_response_driver.py`、`shared.py`、`shared_worker.py`、`models.py`、`app/channels/process_supervisor.py` | 复用现有 RPC 与回复生命周期；增加契约校验、标准回复策略。 |
| `plugins/channel/{feishu,dingtalk}/` | 平台会话、目标、卡片、流式策略及能力声明。 |

表内省略的源码前缀为 `src/qwenpaw/`；两个独立渠道包在仓库根目录 `plugins/channel/`。功能文件按职责保持精简，公共辅助模块不得反向导入平台实现。

## G0 固定兼容基线与接口测试

**依赖：** 无。两方面共用的第一步。

**测试文件：** 新增 `tests/fixtures/channel_plugins/extension_contract_suite.py`，由新增 `tests/unit/plugins/channels/test_channel_extension_contract.py` 在隔离 Python 中运行；复用 `feishu_process_suite.py`、`dingtalk_process_suite.py`。

- [x] 测试 agent 从当前外部行为建立基线：私聊、共享/独立群会话、线程、媒体合并、卡片、流式、cron、审批。固定会话 ID、合并范围和回复目标，不能调用待替换函数来计算期望结果。
- [x] 冻结 `channel.generic.v1` 的标准 metadata、目标编码和回复策略字段；未知契约明确拒绝，旧契约进入兼容路径。形成纯数据编解码测试。
- [x] 保存最小旧描述/旧 Driver 测试夹具及旧持久化状态样本，验证兼容路径不要求用户改配置、重新绑定会话。
- [x] 建立名称可参数化的 `sample_chat` 测试插件夹具。它使用不同于两个官方渠道的配置字段和消息格式，不依赖飞书/钉钉 Driver，不把名字加入宿主名单。
- [x] 先运行已有行为基线，记录通过项和环境限制；新增第三渠道/新契约测试应因当前明确限制失败，不能因缺少测试依赖失败。

**完成条件：** 后续修改可以用独立断言判断是否保持行为，以及第三个渠道是否真正无需修改宿主接入。

## G1 第 3 项 统一配置校验和默认值

**依赖：** G0。

**修改：** `plugins/channel_config.py`、`plugins/channel_adapter.py`、`app/channels/manager.py`、`app/channels/catalog.py`、`app/routers/config.py`；两个渠道的 `channel.json`。

- [x] 测试 agent 增加配置测试：标量/数组/嵌套对象、默认值深拷贝、非法公共字段、飞书 domain、未知字段、未安装插件配置保留。
- [x] 使用项目已有 jsonschema 实现统一校验；固定 Draft 2020-12，兼容现有无 `$schema` 的描述，拒绝外部网络 `$ref`。默认值是独立的显式填充步骤，不误认为校验器会填默认值。
- [x] 公共字段依据 `BaseChannelConfig` 校验；插件 Schema 不能放宽启用、ACL 等宿主约束。保留现有公共字段规范化行为，平台字段按 Schema 校验，不发明一套隐式类型转换。
- [x] Schema 允许的未知字段保留；禁止时明确返回字段错误，不能悄悄删除。已有缺包配置仍按当前方式保存；插件恢复后、启动前再校验。
- [x] 移除 `create_proxy()` 的两个 Config 类映射；修正 dict/Pydantic/`SimpleNamespace` 到普通字典的转换，不将展示元数据或解释器/路径等宿主控制参数混入运行配置。
- [x] 单项 PUT、批量 PUT、代理构造使用同一校验规则；非法输入在配置持久化和运行替换前失败。给飞书 Schema 补等价 domain 约束，不移除旧配置类的兼容读取。
- [x] 新描述默认值接入目录和 Schema 表单；保留已有飞书/钉钉专用表单，新增渠道使用通用表单。

**完成条件：** 第三渠道不新增宿主 Config 类即可校验、显示、保存配置；现有默认值和配置错误行为保持一致。

## G2 第 3 项 打通通用准入和启用链路

**依赖：** G1。

**修改：** `ChannelRuntimeSettings`、`resolve_default_channel_runtime()`、`PluginLoader._channel_runtime_for()`、`selected_process_channels()`、`instantiate_channel()`、`get_configurable_channel_catalog()`；核对 `get_available_channels()` 和配置路由。

- [x] 测试 agent 增加准入矩阵：None、空名单、指定名单、未安装、全局禁用、工作区禁用、撞内置名称和同名旧实现。
- [x] 实现 `allows()` 并移除两平台限制；默认引导使用描述准入，显式部署白名单继续生效。只读取静态信息，不因准入检查 import 插件。
- [x] 统一描述型渠道的选择入口；官方保留键和已登记描述键均不能回退到旧类实现。未迁移的内置渠道继续走已有类注册路径。
- [x] 使第三渠道经过正常 Loader 注册、Manager 构造和子进程启动，不通过测试注入私有 owner 表来绕开接入。
- [x] 目录对所有描述型渠道提供 `process_mode`、`global_enabled`、availability 和运行状态；第三方插件不能获得虚构的 `bundled:<id>` 修复来源。
- [x] 验证安装/列配置不启动 SDK；只在三层控制均允许后启动。停用回收进程但保留配置，配置更新失败沿用现有恢复行为。

**完成条件：** 一个非官方名称的渠道能完成发现、配置、启用、健康查询和停用，不修改官方预装清单、不扩大部署权限。

## G3 第 4 项 统一入站消息和会话规则

**依赖：** G0 的契约；交付验收使用 G2 链路。

**修改：** `normalization.py`、`IsolatedChannelProxy._admit_batch()`、`build_agent_request_from_native()`、`resolve_session_id()`、`get_debounce_key()`、两个 Driver 的 `_session()` / `_message()` / `_emit()`，以及旧协议兼容类。

- [x] 测试 agent 先锁定两个平台的旧会话 ID、共享群用户语义、不同成员合并边界、线程处理、无文本媒体合并与审批操作者。
- [x] 由两个 Driver 提供标准 `session_id`、`user_id`、`debounce_key`；平台旧 ID 算法只在插件中保留，禁止重生成全部历史会话 ID。
- [x] `normalize_inbound_event()` 校验标准字段，输出公共 payload；组别由标准 conversation 确定，提及状态使用统一语义，平台扩展 metadata 不影响宿主保留字段。
- [x] 公共代理移除按平台重算 session、读取 `feishu_sender_id`、提取钉钉 webhook 等分支。新契约消息缺必要字段时返回明确协议错误，不猜平台规则。
- [x] 保留宿主 ACL、审批 actor、代次、事件去重、队列容量与批次回执；标准会话身份不能覆盖认证操作者身份。
- [x] 旧契约必要回退收进小型兼容覆盖；新路径不调用旧兼容代码。两个官方 Driver 产生新契约数据时通过相同的公共转换函数。

**完成条件：** 三个渠道使用同一入站代理，官方渠道会话与权限行为保持原样。

## G4 第 4 项 统一回复目标和主动发送

**依赖：** G3。

**修改：** `targets.py`、代理 `to_handle_from_target()` / `get_to_handle_from_request()` / `_route()`、`SharedResponseDriver` 与两个 Driver 的 `_open()` / `_target()`。

- [x] 测试 agent 覆盖普通回复、无入站上下文的 cron 发送、跨重启旧目标、未知目标、不同工作区/代次和已保存 response_handle。
- [x] 复用现有 `response_handle`；新增编码只用于 user_id/session_id 形式的主动发送目标，不重写历史句柄、不在编码中保存密钥或 webhook。
- [x] 在 Driver 内解析中性目标并转换为平台地址；宿主不再拼接 `dingtalk:sw:` 或 `feishu:open_id:`。旧字符串原样转交相应 Driver 兼容解析。
- [x] 继续通过 `channel.response.open` 建立回复路由，避免新增一条绕开持久化和归属校验的发送路径。
- [x] 目标不存在时明确返回 `OUTBOUND_TARGET_UNKNOWN`；禁止猜收件人、转发到默认会话或在结果未知时自动重试。

**完成条件：** 正常回复与 cron 都不依赖公共代理的平台目标规则；旧任务仍能发往原目标。

## G5 第 4 项 统一卡片准备和流式能力选择

**依赖：** G4。

**修改：** `response_policy.py`、`SharedResponseDriver.bind()`、两个 Driver、两个 `channel.json`、代理 `_before_consume_process()` / `on_streaming_start()` / `_on_process_completed()`、契约握手校验。

- [x] 测试 agent 锁定钉钉 markdown/card、飞书普通/线程消息、启停流式、准备失败、完成反馈与发送后丢回执行为；第三渠道采用不同能力组合。
- [x] 实现只读 `channel.response.describe`；Driver 根据配置和当前回复路由返回 ResponsePolicy。策略按路由缓存，不对每个 token 增加 RPC。
- [x] 静态能力、实际路由策略和用户开关共同决定能否执行可选操作；每条消息可有不同策略，不能用一个全局 supports_streaming 代替。
- [x] 复用 `channel.response.prepare` 作为受能力控制的准备操作，并复用已有投递记录、幂等 ID 和结果分类。准备必须在宿主权限检查后执行，不在只读 describe 中创建卡片。
- [x] 公共代理根据策略执行准备、流式和完成反馈，移除 `message_type == card`、`feishu_thread_id`、渠道名判断；平台卡片内容和 SDK 行为留在 Driver。
- [x] 加入描述与实际握手能力一致性检查；旧契约使用冻结兼容行为，未知契约在候选准备时失败，不能先替换再发现无法通信。
- [x] 验证取消、失败和停止仍走 `response.finish` 及现有资源清理，不把 prepare 失败、unknown/timeout 或清理失败吞成成功。

**完成条件：** 平台不同呈现行为通过同一回复契约接入；公共代理不认识平台专有字段。

## G6 第三渠道证明与完整回归

**依赖：** G1–G5。

**测试文件：** 新增 `tests/integration/test_channel_extension_process.py`；复用配置、正式安装、平台行为、依赖、事务、工作区切换和发行测试。

- [x] 测试 agent 用 `sample_chat` 跑真实插件安装、配置 API、启用、真实子进程 IPC、入站、普通回复、cron、停用、更新失败恢复；平台和模型用测试替身。
- [x] 将同一夹具换成另一个合法名称及不同配置/能力组合后再次接入；不得改 Loader、Manager、公共代理、官方清单，不得 monkeypatch 掉准入与协议校验。
- [x] 从主进程导入记录和子进程 PID 验证 SDK/Driver 只在子进程执行。未知字段、错误身份、失效代次、跨工作区目标仍被拒绝。
- [x] 执行旧契约兼容、两个官方渠道行为、配置 API 和界面回归。旧断言 `test_unknown_config_model_fails_at_factory_not_discovery` 改为通用配置行为测试，单独注明这是解除旧限制；保留坏 Schema、禁用和撞名负例。
- [x] 运行现有 C4/C5、工作区切换和 C7 发行回归；若新增纯数据文件，确认 wheel、sdist、support 目录可携带它们，不能只在源码树测试成功。
- [x] 检查公共新代理没有飞书/钉钉名称分支和平台字段解释；人工确认不是将同样分支搬到另一个“通用”文件。旧兼容覆盖和平台插件中的名称允许保留。
- [x] 补一个最小 Channel 开发样例，说明配置 Schema、标准消息、目标和回复策略。样例来自通过端到端测试的夹具，不另写无法运行的伪代码。

**完成条件：** “新增普通渠道只增插件包和测试”获得端到端证据；对既有平台没有功能、权限和生命周期回归。

## 验证命令与分工

在可用开发虚拟环境中执行，使用临时 `QWENPAW_WORKING_DIR` 和 `QWENPAW_SECRET_DIR`。以下为参考测试入口；本次实际执行结果见文末。新增测试与功能代码由不同 agent 编写。

```bash
python -m pytest tests/unit/plugins/channels/test_channel_extension_contract.py -q
python -m pytest tests/unit/plugins/channels/test_feishu_process.py tests/unit/plugins/channels/test_dingtalk_process.py -q
python -m pytest tests/integration/test_channel_extension_process.py tests/integration/test_c6_channel_config.py -q
python -m pytest tests/unit/plugins/channels tests/contract/plugin_channels -q
python -m pytest tests/unit/channel_isolation tests/unit/pack/test_c7_distribution.py tests/integration/test_c7_staged_runtime.py tests/integration/test_c7_released_process.py -q
```

前端在 `console/` 下执行：

```bash
npm run test:run -- src/pages/Control/Channels
npx tsc --noEmit
```

每步使用其归属的测试先证明当前缺口，再实现、复测和评审。阶段末运行跨模块回归；失败定位到功能或明确变更的契约，不通过删除断言解决。所有测试通过且无非预期 skip 才满足相应验收项。

顺序为 **G0 → G1 → G2 → G3 → G4 → G5 → G6**。契约稳定后，测试 agent 可提前编写后续步骤测试，功能 agent 按上述依赖实现；不因追求并行而同时修改公共代理。代码要求保持高效、精炼，同等功能与安全性下优先更少代码。本文不包含自动提交、推送或发布动作。


## 实施与验收结果（2026-10-13）

第 3、4 项已完成；保留当前分支已有的执行后端、资源归属和依赖消费实现。准入通过现有 `ChannelBackend` 接入，没有重新加入 Loader 平台选择分支。

- **通用准入与配置**：按描述发现渠道，统一 Schema、默认值与保存校验。错误 Schema 在静态发现时拒绝；合法但未配置的插件仍可展示。发现和配置不导入 SDK。
- **通用消息与回复**：Driver 提供会话、用户、合并键和回复策略；公共代理使用标准事件、中性发送目标和按路由策略。旧官方协议保留有限兼容层。
- **清理与兼容**：直接消息和控制命令在权限检查后进入回复生命周期；失败、取消与未知发送结果不会跳过清理。公共会话接口保留无元数据时的通用回退，新协议入站仍严格校验。
- **开发样例**：`tests/fixtures/channel_plugins/sample_chat/` 是可运行的离线插件样例，使用正式安装、工作区接入及真实本地子进程测试；同一包改名为另一个合法渠道也能接入。

### 已执行的关键验证

| 验证范围 | 结果 |
| --- | --- |
| 新增纯协议与真实子进程链路 | 62 项通过 |
| 最终准入与队列准入 | 69 + 12 项通过 |
| 最终静态 Schema 修正的准入/目录覆盖 | 153 项通过（与上一行有重叠，不累计） |
| 飞书 / 钉钉完整进程行为 | 52 / 97 项通过；后续清理修正各补跑 15 项通过 |
| 两平台公共接口契约 / BaseChannel | 42 项 / 98 项通过，BaseChannel 1 项跳过 |
| 插件通用生命周期 / 配置 API 与安全 | 175 项 / 98 项通过，配置测试 2 项跳过 |
| Console 渠道界面 | 129 项通过 |
| 飞书依赖、事务、工作区运行与切换 | 原批次 109 项通过、2 项跳过；3 项旧模拟消息补齐协议字段和有效回复目标后通过 |
| 钉钉事务、工作区运行与切换 | 64 项通过；另 3 项对应模拟消息回归通过 |
| wheel、sdist、staging、frozen 及发布包实际子进程 | 24 项通过 |

功能与测试分别由不同 agent 编写。分项复审和最终交叉复审提出的问题均已修正并复审通过，新增缺陷有独立失败→通过证据。测试模拟平台与模型，不包含真实机器人、原生桌面安装或 Docker 实机验收。

### 已知限制

旧 `tests/unit/channel_isolation` 与插件契约联合运行最初为 507 通过、14 失败。其中 4 项公共会话接口回归已修复并由 42 项契约测试复验；其余 10 项有明确的既有源码/环境证据：6 项引用旧内置目录或注册表，4 项涉及本地监听权限或隔离子进程误载全局旧包。因此不宣称仓库全量测试通过。飞书完整内部进程套件耗时约 306 秒，现有外层 300 秒包装可能超时；未放宽超时限制。

跟踪任务路径的请求元数据副本可能让同一路由查询两次回复策略；不会对每个 token 查询，属后续可优化项，未为此新增全局缓存。

### 实施选择与代价

- 延用用户当前有未提交改动的分支；若以后需要独立工作树，需要再分离改动。
- 用现有 `ChannelBackend` 承接准入选择；若后端契约继续变化，适配点仍需同步。
- 按准入/协议/验收三组交付，功能与测试独立；代价是单次复审范围较大。
- 增加可选的静态配置资格预检，在拆卸前拒绝不允许的配置更新；后端需维护这个小型钩子。
- 保留公共会话接口的通用回退，同时严格校验新入站消息；调用者应区分二者。
- 可选预创建明确失败时保留诊断、允许正常回答；未知/超时/取消仍不重发。需要“预创建必须成功”的插件应另行定义明确契约。

改动保留在当前工作区；未创建提交、推送或合并。详细命令、日志路径及复审记录保留在 `.superpowers/sdd/2026-10-13-channel-extension-boundaries/`，避免未提交阶段丢失验证证据。
