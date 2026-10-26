# Channel 测试精简 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. 已按本清单完成精简；实际结果见末尾执行记录。

**Goal:** 将渠道插件运行测试收敛为每个渠道一个文件、宿主主链路一个文件、进程管理一个文件，只验证基本功能与稳定性。

**Architecture:** 平台测试验证各自 Driver/Platform 的正常输入输出；宿主测试集中验证一次安装、配置、Agent、会话和插件生命周期；进程测试集中验证真实子进程与 IPC。删除原来的专项回归与隐藏套件，重新整理少量无测试用例的辅助代码。

**Tech Stack:** pytest、pytest-asyncio、现有 PluginProcess/ChannelManager/插件加载器；平台 API 与模型输出使用受控替身。

**Spec:** 本次用户要求：①每个 channel 只留一个测试文件，主进程链路一个、进程管理一个，文件不要太长；②验证 QwenPaw 正常运行及渠道正常启停、收发等基本能力，不为每个小 bug 保留回归。

## 共同约束

- 本次精简渠道运行测试及本分支新增的关联专项测试；不删除其他产品模块、通用插件 SDK 的原有基础测试。
- 每个平台只保留约 5–7 类核心场景，优先 150–300 行；复杂渠道以约 350 行为整理目标，不靠压缩可读性凑行数。
- 主链路约 8 类、进程管理约 7 类场景，各自尽量控制在 350 行左右。
- 辅助代码总量争取不超过 1,500 行；不得把原有大套件改名成 helper 继续保留。最终规模预计约 6,000–8,000 行，执行后再统计。
- 不改功能代码、放宽断言、增加 skip/xfail 或拉长超时来让精简后的测试通过；发现功能异常时单独报告。
- 真正验证启停的用例使用真实子进程；不能用 mock 的 start/stop 证明进程可用。平台网络、机器人凭据和模型输出可以模拟。

## 整理时重点检查

- 同一平台在 unit、contract、integration 和 fixtures 中不能残留第二套独立运行测试。
- 不再由一个包装器递归启动数十个 suite；每个可见用例对应一个明确场景。
- 保留一次真实“入站→宿主 Agent→会话保存→出站”的证明，不能全部替换成手写回复生成器。
- 保留真实退出、崩溃隔离和强制回收，不能只检查 stop 被调用。
- 删除旧套件时同步处理其导入、runpy、隔离运行器及 CI 路径，避免留下收集错误。

## 一、最终保留的 21 个 Python 测试入口

以下计数针对渠道运行与进程管理；原有 CLI、前端、通用 SDK 等其他职责的测试不计入这 21 个文件。

18 个官方插件使用 `tests/unit/channels/test_<key>.py`。Console 使用已有的 `tests/contract/channels/test_console_contract.py`，合入其基本运行用例；删除重复的 `tests/unit/channels/test_console.py`。保留现有 Console 契约基类，避免改动内置渠道契约扫描规则。

各渠道均验证正常启动、接收、发送、停止；下表只补充各自值得保留的差异。来源均只提取核心断言，不复制整套进程/Agent/打包夹具。

| Channel | 唯一测试文件 | 额外保留的基本能力 | 主要合并来源 |
| --- | --- | --- | --- |
| Console | `tests/contract/channels/test_console_contract.py` | 文本/内容块输出、事件流正常完成、禁用无发送 | 现有 Console unit 与 contract |
| 飞书 | `tests/unit/channels/test_feishu.py` | 私聊/群提及、媒体收发、卡片最终回复、合法审批操作 | `feishu_process_suite.py` 中正常收发/媒体/流式场景，现有飞书 unit/integration |
| 钉钉 | `tests/unit/channels/test_dingtalk.py` | 私聊/群路由、Webhook/主动发送、媒体或卡片最终回复、合法审批操作 | `dingtalk_process_suite.py` 中正常入站/路由/卡片场景，现有钉钉 unit/integration |
| Telegram | `tests/unit/channels/test_telegram.py` | 群/话题回复目标、普通附件、流式最终文本、重复事件不重复执行 | `telegram_business/route/stream/polling_suite.py` |
| Slack | `tests/unit/channels/test_slack.py` | DM/线程路由、提及、流式最终回复、停止 Socket 连接 | `slack_suite.py`、`slack_route/platform_suite.py` |
| Discord | `tests/unit/channels/test_discord.py` | 私聊/群/线程目标、自身消息过滤、普通文本与附件 | `discord_suite.py`、`discord_platform_suite.py` |
| MQTT | `tests/unit/channels/test_mqtt.py` | 订阅入站、主题回复、发布确认、无连接时幂等停止 | `g2_official_process_suite.py`、`g2_legacy_mqtt_unit_suite.py` |
| Mattermost | `tests/unit/channels/test_mattermost.py` | DM/群提及/线程、普通附件、WebSocket 清理 | `g2_official_process_suite.py` 中 Mattermost 场景、现有 mock integration |
| QQ | `tests/unit/channels/test_qq.py` | 私聊/群目标映射、正常登录/连接、文字与一份媒体、心跳停止 | `qq_native_driver_suite.py`、现有 QQ mock integration |
| 企业微信 | `tests/unit/channels/test_wecom.py` | DM/群提及、processing 到最终回复、普通媒体、停止连接 | `wecom_native/closeout/stream_detail_suite.py`、现有 mock integration |
| 元宝 | `tests/unit/channels/test_yuanbao.py` | 认证连接、私聊/群身份与回复目标、正常 typing/媒体、停止 | `yuanbao_native_driver/wire/state_suite.py`、现有 mock integration |
| 小艺 | `tests/unit/channels/test_xiaoyi.py` | 认证、文本会话映射、最终结束帧、一次主备切换 | `xiaoyi_native/remaining_native_suite.py`、现有 mock integration |
| 微信 | `tests/unit/channels/test_wechat.py` | 已登录启动、首次扫码到连接、私聊/群路由、文本/图片、停止轮询 | `wechat_client/closeout_driver/runtime_auth/w4_waiting/closeout_send_suite.py`、现有 mock integration |
| Matrix | `tests/unit/channels/test_matrix.py` | 密码/token 正常登录、房间/发送者映射、附件、同步游标续接及停止 | `matrix_native_auth/driver_response/native_lifecycle_suite.py`、现有 mock integration |
| iMessage | `tests/unit/channels/test_imessage.py` | SQLite 新消息读取、会话目标、正常发送命令、停止关闭数据库 | `imessage_private_process/native/closeout_suite.py` |
| OneBot | `tests/unit/channels/test_onebot.py` | WebSocket 认证、私聊/群目标、文字与文件、停止释放端口 | `onebot_native/delivery/formal_closeout_suite.py`、原 OneBot unit/integration |
| Azure Bot | `tests/unit/channels/test_azure_bot.py` | 合法认证 Activity 入站、群会话/提及、正常回复、Webhook 停止 | `azure_bot_native/formal_agent_suite.py` |
| Voice | `tests/unit/channels/test_voice.py` | 合法呼叫入站、语音转文字后回话、最终语音、挂断清理 | `voice_native_suite.py`、现有 voice integration/webhook 测试 |
| SIP | `tests/unit/channels/test_sip.py` | 一个选定音频 backend、转写入站、TTS 回复、新一轮打断、停止 | `sip_native/formal_closeout/completed_text_suite.py` |

来源中提及的 `*_suite.py` 均位于 `tests/fixtures/channel_plugins/`。一条源用例如同时包含十余种异常注入，只迁入表中正常场景。

### 主进程：`tests/unit/channels/test_channel_host.py`

| 最终场景 | 保留的断言 | 主要来源 |
| --- | --- | --- |
| 静态发现/禁用 | 官方清单、schema、禁用配置可读；不导入 SDK、不创建子进程 | `c6_catalog_suite.py`、`catalog_suite.py`、`process_contract_suite.py` |
| 安装并启用 | 一次本地插件安装，必要的小依赖在临时环境可见，配置保留，从已安装目录启动真实 worker | `c5_transactions_suite.py`、`c4_dependencies_suite.py` |
| 完整对话 | 一种代表渠道经真实队列、Agent、模型边界替身、会话保存返回最终回复；模型在主进程、平台发送在子进程 | `g2_actual_agent_suite.py::test_mqtt_formal_zip_real_agent_session_and_worker_exit` |
| 工作区隔离/访问控制 | 两个工作区的消息、会话与回复目标隔离；允许者收到回复、被拒者不执行 | `workspace_runtime_suite.py` |
| 配置与重启 | 读取→保存→重启使用新配置，未编辑字段保留，旧进程退出后新实例接管 | `extension_process_suite.py`、`workspace_handoff_suite.py`、旧 config integration |
| 正常更新 | 已启用插件更新后旧 worker 退出，新版本正常回复；配置与用户数据保留 | `c5_transactions_suite.py::test_active_update_swaps_all_workspaces_after_old_exit` |
| 停用/卸载 | 停用可重新启用，卸载回收自己持有的 worker/资源，保留用户拥有的配置与业务数据 | `c5_transactions_suite.py`、现有生命周期基本场景 |
| 应用退出 | 宿主关闭后无所属残留 worker，已安装代码保留供下次启动 | `c5_transactions_suite.py::test_shutdown_stops_real_worker_and_preserves_installed_code` |

基础流式最终提交放在完整对话场景中验证一次。各平台只检查原生发送格式，不重复构造一套完整 Agent。

### 进程管理：`tests/unit/plugins/test_plugin_process.py`

| 最终场景 | 保留的断言 | 主要来源 |
| --- | --- | --- |
| 正常生命周期与 IPC | 真实 worker 握手、请求响应、反向调用/通知，少量日志不破坏通信；正常及重复停止后 PID 退出 | `plugin_execution/process_suite.py`、`process_supervisor_suite.py` |
| 崩溃与恢复 | 子进程异常退出不拖垮宿主，重启后新 generation 可继续调用宿主方法 | `process_supervisor_suite.py::test_host_registration_survives_fresh_generation` |
| 阻塞隔离 | SDK 初始化阻塞时主循环仍响应；可在停止时限内结束 worker | `process_supervisor_suite.py::test_blocked_sdk_keeps_parent_loop_responsive` |
| 强制回收 | 一个不配合退出的进程树，升级终止后主子进程均无残留 | `plugin_execution/process_suite.py::test_stop_reaps_descendant_and_revokes_restart` |
| 调用取消 | 取消一个在途调用后完成清理，下一次正常请求仍成功 | 现有 RPC/停止套件中一个普通取消场景，不保留取消竞态矩阵 |
| 正常媒体负载 | 一次超过内联帧大小的请求/回复，文本和附件字节完整往返 | `plugin_execution/payload_process_suite.py::test_actual_child_large_request_and_response_are_exact` |
| 运行目录与依赖可见性 | 使用明确的解释器、支持代码与依赖目录启动 worker；源码和冻结目录使用同一组小场景 | `shared_dependencies/visibility_suite.py`、`test_frozen_dependency_preparation.py` 的正常目录选择场景 |

冻结目录模拟仅证明运行目录选择和 worker 可启动，不宣称替代真实桌面发行构建或原生安装验证。

## 二、删除与归并清单

### 目录级删除：提取上一节核心用例后整组移除

| 删除路径 | 处理方式 |
| --- | --- |
| `tests/unit/plugins/channels/` | 删除全部包装器，核心断言已落入上述 21 个文件 |
| `tests/fixtures/channel_plugins/` | 删除全部专项 suite 及旧辅助链；仅抽取精简后的无测试辅助代码 |
| `tests/fixtures/plugin_execution/` | 删除进程、payload、Windows/CA 等专项矩阵；少量正常 worker 移到统一辅助目录 |
| `tests/fixtures/shared_dependencies/` | 删除升级、可见性、重启与冻结环境矩阵；正常依赖场景只留一次 |
| `tests/fixtures/dependency_leases/` | 删除文件锁/残留租约专项回归 |
| `tests/unit/channel_isolation/`、`tests/fixtures/channel_isolation/` | 删除旧方案阶段编号测试和旧实现夹具 |
| `tests/contract/channel_plugins/` | 删除 MQTT/Mattermost 的重复代理契约测试 |
| `tests/unit/app/channels/feishu/`、`qq/`、`wecom/` | 删除独立卡片、格式与工具守卫专项测试，正常卡片/媒体断言并入平台唯一文件 |

### `tests/unit/channels/` 的处理

- 上述 18 个官方平台文件重写为精简用例，不保留现有几千行文件或一行包装器。
- `test_base_core.py`、`test_base_stream_helpers.py`、`test_channel_manager_lifecycle.py`：只提取正常事件分发、最终回复和启停断言至 `test_channel_host.py`，随后删除。
- `test_console.py`：基本运行用例并入唯一 Console contract 文件，随后删除。
- 删除独立的 `test_dingtalk_send_parts_extended.py`、`test_onebot_channel.py`、`test_onebot_media.py`、`test_qq_media_helpers.py`、`test_qrcode_auth_handler.py`、`test_qrcode_auth_helpers.py`、`test_sip_helpers.py`、`test_sip_memory_bounds.py`、`test_wechat_quoted_msg.py`、`test_yuanbao_codec.py`、`test_utils.py`。基本媒体/登录场景进入对应平台文件，纯工具边角与边界枚举不再保留。
- `conftest.py` 与 `__init__.py` 仅保留确有用途的少量配置，不计为第二个测试文件。

### 旧 integration 文件

以下平台重复测试的正常收发场景归并后删除原文件：

```text
tests/integration/test_dingtalk_mock_im.py
tests/integration/test_feishu_mock_im.py
tests/integration/test_matrix_mock.py
tests/integration/test_mattermost_mock.py
tests/integration/test_onebot_reverse_ws.py
tests/integration/test_qq_mock_im.py
tests/integration/test_voice.py
tests/integration/test_voice_webhooks.py
tests/integration/test_wechat_mock.py
tests/integration/test_wecom_mock_gateway.py
tests/integration/test_xiaoyi_mock.py
tests/integration/test_yuanbao_mock.py
```

以下主链路/阶段验收重复文件归并后删除：

```text
tests/integration/test_channel_config.py
tests/integration/test_channels_config.py
tests/integration/test_channels_helpers_module.py
tests/integration/test_channels_manager_module.py
tests/integration/test_proactive_send_channels.py
tests/integration/test_channel_extension_process.py
tests/integration/test_channel_management_process.py
tests/integration/test_c6_channel_config.py
tests/integration/test_c7_released_process.py
tests/integration/test_c7_staged_runtime.py
tests/integration/test_g2_official_channel_process.py
tests/integration/test_plugin_framework_boundaries.py
```

`mock_*` 本地服务只在剩余其他测试仍使用时保留；不为了上述删除的 integration 保留整套服务器。共享 `conftest.py`、`helpers.py` 不整文件删除，只清除已无引用的渠道夹具。

### 本分支新增的公共专项回归

下列文件在核心断言归并后删除，不再逐 bug 保留：

```text
tests/unit/plugins/test_config_dependency_admission.py
tests/unit/plugins/test_dependency_custody.py
tests/unit/plugins/test_dependency_lease_cleanup.py
tests/unit/plugins/test_dependency_publication_permissions.py
tests/unit/plugins/test_dependency_upgrades.py
tests/unit/plugins/test_dependency_usage.py
tests/unit/plugins/test_dependency_wheel_acquisition.py
tests/unit/plugins/test_execution_backend_lifecycle.py
tests/unit/plugins/test_execution_backend_resolution.py
tests/unit/plugins/test_execution_backend_transactions.py
tests/unit/plugins/test_frozen_cli_runtime.py
tests/unit/plugins/test_frozen_dependency_preparation.py
tests/unit/plugins/test_generic_dependency_gate.py
tests/unit/plugins/test_generic_workspace_ownership.py
tests/unit/plugins/test_plugin_process_execution.py
tests/unit/plugins/test_plugin_stop_confirmation.py
tests/unit/plugins/test_python_host_observation.py
tests/unit/plugins/test_python_host_observation_compat.py
tests/unit/plugins/test_reload_dependency_admission.py
tests/unit/plugins/test_runtime_ownership.py
tests/unit/plugins/test_sdk_channel_stop.py
tests/unit/plugins/test_shared_dependency_visibility.py
tests/unit/plugins/test_transport_close.py
tests/unit/plugins/test_wheel_entry_point_metadata.py
tests/unit/plugins/test_wheel_script_publication.py
tests/unit/plugins/test_windows_worker_home.py
tests/unit/plugins/test_worker_ca.py
tests/unit/plugins/test_worker_inspection_startup.py
tests/unit/app/channels/test_manager_batch_acl.py
tests/unit/app/routers/test_channel_config_transactions.py
tests/unit/tauri/test_runtime_metadata.py
tests/unit/cli/test_channel_upgrade_worker.py
tests/unit/cli/test_doctor_descriptor_boundary.py
tests/unit/cli/test_managed_dependency_maintenance.py
tests/unit/cli/test_missing_official_config_boundary.py
tests/unit/pack/test_channel_installer_upgrade.py
tests/unit/pack/test_channel_wheel_upgrade.py
tests/unit/pack/test_docker_channel_upgrade.py
tests/unit/pack/test_desktop_dependency_wheels.py
tests/unit/pack/test_c7_distribution.py
tests/unit/pack/test_c7_deployment_commands.py
```

退出确认、依赖可见性等正常断言迁入进程/宿主文件；wheel solver 伪生态、权限矩阵、回滚注入、打包完整重建等不迁入。

### 前端

沿用原来的通用 `ChannelDrawer.test.tsx`、`useChannels.test.ts`、`useChannelQrcode.test.ts`、页面及插件管理基础测试。只补入必要的“正常表单能保存”“扫码入口可见”“普通插件安装可发起”断言，不按渠道再开文件。

删除本分支新增的专项文件：

```text
console/src/api/modules/channelUpgrade.test.ts
console/src/hooks/useAutoSave.wechat-job.test.tsx
console/src/pages/Control/Channels/ChannelsPage.qrcode-state.test.tsx
console/src/pages/Control/Channels/ChannelsPage.wechat-save.test.tsx
console/src/pages/Control/Channels/c6_useChannels.test.ts
console/src/pages/Control/Channels/useChannels.wechat-scope.test.tsx
console/src/pages/Control/Channels/components/ChannelDrawerOfficialForms.test.tsx
console/src/pages/Control/Channels/components/ChannelDrawerQrcodeAvailability.test.tsx
console/src/pages/Control/Channels/components/ChannelDrawerUpgrade.test.tsx
console/src/pages/Control/Channels/components/c6_ChannelDrawer.test.tsx
console/src/pages/Control/Channels/components/useChannelQrcode.wechat-scope.test.tsx
console/src/pages/Settings/PluginManager/dependencyUpgrade.test.tsx
console/src/pages/Settings/PluginManager/hooks/useUpgradeInstall.test.ts
```

### 已有公共测试的边界

- 保留主分支已有的通用插件加载/卸载、SDK author/host/channel bridge、安装事务与隔离测试；其中适配新接口、正确排空队列的必要修正不能回退。
- `test_sdk_author_host.py` 中本分支把 3 次重载扩大到 20 次的压力修改恢复为 3 次。
- 已有文件中仅因本分支小修补新增的独立源码重载、取消时序、精确错误文本等用例，可删除新增用例；不要删除整份原有 SDK 文件，也不要恢复已经失效的旧接口 mock。
- 保留原有 CLI 配置向导、配置迁移、桌面制品检查，以及与本次渠道运行拆分无关的鉴权、代理、日志安全等产品测试。
- 这次主动放弃的是逐 bug、竞态、容量边界、协议畸形输入、完整发行重建的持续回归覆盖；不能再宣称精简后的测试与原矩阵覆盖等价。

## 三、执行顺序与验收

### 1. 先建立公共骨架

- [x] 在 `tests/channel_support/` 整理少量无测试辅助代码：受控平台边界、最小真实 worker、实际 Agent/会话绑定。每个 helper 必须被最终用例直接使用，不复制整个旧 backend。
- [x] 将上述 8 类宿主场景迁入 `test_channel_host.py`，7 类进程场景迁入 `test_plugin_process.py`；先验证一次这两个文件，再开始删来源。
- [x] 如需绕开全局 conftest 的 SDK mock，只保留一个精简隔离运行器，执行明确的单个场景；不再递归 pytest 整个 fixture 目录。

### 2. 平台归并

- [x] 按第一节逐个重写平台唯一文件，合并 Console 运行测试到现有 contract。基础收发不要求真实机器人账号。
- [x] 每批只运行这一批平台测试；宿主与进程辅助代码未变化时不重复运行公共测试。
- [x] 用真实 Driver/Platform 验证原生数据映射，不以构造标准化消息替代平台解析。不在每个平台重复完整插件安装与实际 Agent 链路。

### 3. 删除旧套件并消除引用

- [x] 按第二节删除旧文件，清除失去使用者的隔离脚本 `tests/{isolated_pytest,matrix_isolated,onebot_isolated,qq_isolated,sip_isolated,wecom_isolated}.py` 及旧 mock 服务。
- [x] 删除 `tests/unit/scripts/test_check_channel_contracts.py` 中新增的 `test_repository_has_complete_official_plugin_contract_coverage`，将清单完整性断言归入宿主发现测试；保留内置 Console 扫描测试。
- [x] 全仓搜索被删除路径、`runpy`、旧 helper 导入和 `pytest_plugins`，确认没有悬空引用。同步清除集成测试夹具中对已删除服务的引用。
- [x] 现有 CI 使用 `tests/unit`/`tests/contract` 目录入口，保留这些入口、SDK 制品验证及生产打包校验，不添加独立 Channel 工作流。

### 4. 最终验证只集中做一次

- [x] 对受影响的 Python 测试目录做一次收集检查，确认旧文件未被引用、官方平台 18 个文件加 Console 共 19 个入口，没有隐藏 suite。
- [x] 一次性运行最终 21 个渠道/宿主/进程测试文件；只额外运行发生修改的原有 SDK、CLI、脚本或前端测试，不跑所有产品全量测试。
- [x] 使用仓库固定版本格式工具检查修改文件；统计保留文件数、实际用例数、测试及辅助代码行数，报告真实减少量。
- [x] 不修改功能代码，不自动 commit/push；如核心测试发现功能异常，单独定位并说明。


## 执行结果（2026-10-26）

- 最终渠道运行测试入口：21 个（18 个官方插件、Console、宿主主链路、进程管理）。
- 核心测试 5,519 行；7 个无测试用例的辅助文件共 913 行。加上既有 Console 契约基类 332 行，相关代码共 6,764 行。
- 删除 637 个旧测试、包装器和夹具文件；本次改动净减少约 13.9 万行。没有将旧 suite 改名迁入 helper。
- 最大平台文件为 Matrix（417 行），主要是本地 Matrix HTTP 边界及真实 Driver 收发验证；其他平台文件均低于 400 行。
- 原有通用插件 SDK、CLI 及其他产品测试保留；只恢复 SDK 重载用例原先的 3 次循环，并清除旧官方契约套件引用。
- 21 个核心入口集中验证：149 passed，42.64 秒；前端表单/安装基础验证：52 passed；原有契约检查器与 SDK 重载验证：74 passed。
- 最后整理行宽与 wheel 辅助代码后，仅复核相关安装、iMessage 与 Mattermost：13 passed。
- 受影响目录收集检查：3,209 tests collected，无收集错误。收集时使用临时工作目录，避免导入其他模块时使用真实用户目录。
- Black 23.3.0（行宽 79）、Flake8、所改前端文件的 Prettier 和差异空白检查通过。
- 独立只读审查提出的 Matrix Driver 链路、完整官方目录两项缺口均在既有用例中补齐，复审通过。
- `src/`、`plugins/`、生产脚本和工作流没有修改；未 commit、未 push。
