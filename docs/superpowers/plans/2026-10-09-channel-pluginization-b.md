# Channel 首批插件化 B 实现计划

> **2026-10-10 方向更新：** 步骤一至五保留为已完成的同进程候选及测试基线。后续改为「共享依赖、渠道子进程」，按[多进程实施计划](2026-10-10-channel-shared-process-b.md)推进。下文原步骤六、最终接入方式与剩余工作量估计由新计划替代。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** B 在 A 完成插件系统之前，先交付可独立验证的飞书、钉钉渠道实现、插件包和测试；随后通过一次集成完成正式接入与旧入口迁移。

**Architecture:** 普通渠道继续同进程运行，通过现有 `register(api)`、`api.register_channel(...)` 和 `BaseChannel` 接入。插件负责平台连接、消息转换与自身资源，宿主继续负责队列、访问控制、会话与 Agent 调用。先完成飞书样例，再按同一结构整理钉钉；宿主接入处集中在最后一步调整。

**Tech Stack:** Python、asyncio、现有 PluginApi / BaseChannel、lark-oapi、DingTalk SDK、pytest。

**Spec:** [初版方案](../../初版方案.md)、[插件机制重构](../../插件机制重构.md)、[channel-isolation 设计](../../proposals/channel-isolation/DESIGN.md)。以用户已确定的同进程插件化范围为准，选择性复用 isolation 的实现。

## 全局约定

- 工作分支为 `codex/channel-pluginization`；规划时基线为 `79a469b95`。完成情况以各步骤复选框为准。
- 首批只做飞书、钉钉；Console 保留内置。两者的渠道键继续为 `feishu`、`dingtalk`，不为绕过内置名称限制而改名。
- 使用现有插件格式，不另建插件框架、Runner、独立依赖环境或 gRPC 通信层。
- 实现保持高效、精炼；功能和安全性相同时优先代码量更少的方案，不为减少行数牺牲可读性。
- 功能代码与测试代码由不同子 agent 编写；测试从需求和原有行为推导，失败优先定位功能实现。修改预期必须说明依据并经主 agent 核对，不能仅为通过测试而放宽断言。
- 读取说明、导入插件入口、执行注册与构造渠道实例时，不加载平台 SDK、不建立连接。由渠道 `start()` 加载 SDK 并启动服务。
- 依赖由 `requirements.txt` 声明，安装交给宿主；插件不自行运行 pip / uv。若发行元数据也需要依赖列表，由同一来源生成或校验一致。
- ChannelManager 管理渠道实例，渠道管理内部连接和任务；不再把同一条连接交给另一套全局清理器重复管理。
- 保留旧配置字段、默认值、会话映射和数据位置。停用只停止工作，不删除用户配置及长期数据。
- A 完成前保留现有内置入口与主包依赖，新插件通过独立测试验证。正式切换时再移除对应默认依赖和重复入口。

## 重点审查

1. 测试环境已预装 SDK 或全局模拟了 SDK，掩盖入口提前导入的问题——步骤一、五使用独立 Python 进程检查。
2. 线程收到停止信号却没有退出，被错误报告为停止成功——步骤三保留句柄、报告失败并验证可重试。
3. 两个工作区同时启用同一渠道，状态、补丁或回调串到另一个实例——步骤三、四验证隔离及旧回调失效。
4. 平台已接受消息，但本地等待超时，重试造成重复发送——步骤二、四覆盖发送结果不确定的场景。
5. 插件形式切换后，旧配置、主动发送地址或卡片恢复记录读不到——步骤一建立样本，步骤二、四、六验证。

## 目标文件与职责

以下为计划新增路径；实现时只迁移实际需要的辅助文件，不为凑目录增加抽象。

| 位置 | 职责 |
| --- | --- |
| `plugins/channel/feishu/`、`plugins/channel/dingtalk/` | 各自包含 `plugin.json`、`plugin.py`、`__init__.py`、`requirements.txt`、`README.md` |
| 各插件的 `channel.py` | 轻量的 BaseChannel 实现，承接现有配置、消息与生命周期接口 |
| 各插件的 `platform.py` | 平台 SDK、连接、HTTP 调用及相关资源；只在启动时导入 |
| 飞书的 `card_templates.py`、`utils.py`、`constants.py` 等 | 按实际调用迁入的平台专用辅助代码；公共算法仍复用宿主 |
| 钉钉的 `handler.py`、`ai_card.py`、`markdown.py`、`content_utils.py`、`utils.py`、`constants.py` | 保留平台业务职责；SDK 派生类所在的 handler 也必须延迟导入 |
| `tests/fixtures/channel_plugins/` | `conftest.py`、`registration_suite.py`、`feishu_suite.py`、`dingtalk_suite.py`、`lifecycle_suite.py`、`packaging_suite.py`；测试直接加载新插件 |
| `tests/unit/plugins/channels/test_channel_packages.py` | 以独立 Python 子进程运行上述测试，避免根目录 conftest 的全局 SDK 替身污染 |
| `tests/integration/test_channel_plugin_lifecycle.py` | A 可用后新增，使用真实插件框架验证安装、工作区启停和更新恢复 |

既有参考：`plugins/channel/azure_bot/` 提供包格式；`src/qwenpaw/app/channels/feishu/`、`dingtalk/` 提供行为基线；`tests/unit/channels/`、`tests/contract/channels/` 与 `tests/fixtures/channel_isolation/feishu/ch_0_007_feishu_suite.py` 提供测试样本。

## 步骤一 建立飞书轻量入口与兼容基线

**已完成（2026-10-09）。产出：不依赖 A 的包结构、注册测试和旧行为清单。**

验证：9 项独立注册/配置测试、51 项既有插件回归通过；相关文件 pre-commit 检查及独立审查通过。新包暂不发布，收发和实际启停按后续步骤实现。

**文件：** 新增飞书插件入口、清单和轻量渠道类；新增 `registration_suite.py`、测试夹具与子进程测试入口；两份插件 README 随各自实现补齐。

**接口：** 插件导出 `plugin = FeishuChannelPlugin()`，其 `register(self, api: PluginApi) -> None` 调用现有 `api.register_channel(channel_class, label, description, config_fields, icon, doc_url)`。渠道类保留 `channel = "feishu"`。

- [x] 从当前渠道和 isolation 测试中列出必须保留的收发行为、配置字段、数据文件及后台资源，写入飞书 README；以可复现行为为准，不把测试中对旧私有方法的断言照搬为要求。
- [x] 先添加 `test_register_without_platform_sdk`：在独立进程阻止 `lark_oapi` 导入，加载插件并注册到只记录声明的 FakeApi；断言仅登记 `feishu`、类继承 BaseChannel、没有平台 SDK 导入、连接和后台任务。
- [x] 添加 `test_probe_namespace_uses_relative_imports`：分别在正常和探测命名空间加载，断言各自引用本包模块，不串到旧版本模块。
- [x] 按现有格式实现入口和说明。`channel.py` 不能直接或间接导入旧的重型 `feishu.channel` 或新 `platform.py`。
- [x] 固定 `from_config` 的兼容入口：继续接受 `process`、原 FeishuConfig、`on_reply_sent`、`display_config`、`no_text_debounce=True`、`workspace_dir=None`；保留原字段转换和默认值。测试构造实例也不连接平台。
- [x] 运行注册测试，失败项先修正再推进。FakeApi 只验证插件声明，不模拟安装或更新成功，不修改生产注册器的内置键保护。

## 步骤二 完成飞书消息链路

**已完成（2026-10-09）。产出：可直接实例化、模拟平台边界收发测试通过的飞书渠道。**

验证：9 项注册/配置、40 项消息与安全回归、19 项 BaseChannel 契约、51 项既有插件回归通过；相关文件 pre-commit 与独立审查通过。功能与测试由不同子 agent 编写，未进行真实机器人联调。

审查修复了自定义网关遗漏、审批操作者身份丢失和授权前卡片显示完成的问题。共享群/话题使用合成身份时，`EXACT_REQUESTER` 审批暂时安全拒绝；普通 `AGENT` 策略继续由宿主处理。最终接入时需核对审批命令识别所复用的宿主 control 私有 token/registry 契约，并由宿主解决会话身份与操作者身份的分离。

**文件：** 飞书 `channel.py`、`platform.py` 及被引用的辅助文件；`feishu_suite.py`。

**接口：** 保持 BaseChannel 的 `build_agent_request_from_native`、`send`、`send_content_parts`、流式事件钩子与 `health_check` 签名；平台层复用 `LarkOapiPlatform.prepare(config, secret, media_work_dir)`、`connect(on_message, on_card)` 及发送接口。平台层不拥有 Agent 执行循环。

- [x] 先添加私聊、群聊、@ 判断、重复消息、主动发送、图片/文件、审批卡片与流式回复的测试；对照当前主分支行为及 isolation 中新增的有效案例。
- [x] 将 `LarkOapiPlatform` 和必要的平台辅助逻辑整理进新包，修正相对导入。复用它的连接、媒体、卡片和发送实现；缺失的现有产品行为从旧渠道补齐。
- [x] 用新 BaseChannel 实现连接平台层与宿主已有入口。访问控制、队列、会话和 Agent 调用继续走宿主，避免复制 isolation Runner 的职责。
- [x] 从 `FeishuDriver` 借鉴消息转换、回复定位及测试，不直接套整个 Driver。其 Runner 生命周期、协议对象和跨进程状态同步不迁入普通插件。
- [x] 添加并通过 `test_concurrent_replies_keep_original_thread`：两个请求倒序完成，仍回复各自话题；流式失败后的普通消息回退也保持目标。
- [x] 添加并通过 `test_ambiguous_send_does_not_blindly_retry`：平台可能已收消息但本地超时时，不无条件重复发送。明确向调用方暴露失败或不确定结果。
- [x] 使用旧配置和主动发送记录样本验证仍能解析原目标；执行新插件的消息测试及对应 BaseChannel 契约。

## 步骤三 把飞书启停和失败清理做完整

**已完成（2026-10-09）。产出：不依赖 A 的飞书生命周期实现和可供钉钉复用的测试规范。**

验证：新增 24 项生命周期测试、既有 68 项渠道注册/消息/接口测试及 51 项宿主插件回归通过；相关 pre-commit 检查和独立代码审查通过。审查发现的重连首条消息丢失窗口已补回归并修复。实测 SDK 为 lark-oapi 1.7.1；真实机器人联调、完整 SDK 版本范围验证及接入 A 仍待后续完成。

**文件：** 飞书 `channel.py`、`platform.py`；`lifecycle_suite.py`、`feishu_suite.py`。

**接口：** `async start(self) -> None`、`async stop(self) -> None` 沿用 BaseChannel。停止无法完成时抛出明确异常，保留可观察的资源引用；不返回 `False` 冒充停止回执。`StopReceipt` 由 A 的 ChannelManager 生成。

- [x] 先写测试覆盖未启动就停止、连续两次启动/停止、认证失败、连接超时和启动到一半失败；失败后不留下未管理资源。
- [x] 在 `start()` 内加载 SDK 和创建平台对象；缺依赖报告具体缺失包，不自行安装。构造及注册阶段保持轻量。
- [x] 明确停止顺序：停止接收新事件和重连，结束本实例已调度回调及发送收尾，断开 SDK，等待线程退出，关闭所属 HTTP 客户端。已经进入宿主队列的工作由 Manager/宿主协调，不从插件扫描取消全局 Agent 任务。
- [x] 补齐现有平台实现中“线程未退出只记 warning”的路径。`test_stop_failure_keeps_live_handle` 断言停止失败会抛错、线程引用仍保留、重复 start 不另起连接；资源随后退出时允许再次 stop 完成清理。
- [x] `join` 等阻塞操作离开事件循环；测试停止等待期间独立协程仍可运行。取消停止等待时保留清理结果和资源记录，按合同传播取消，不把取消当作资源已退出。
- [x] 添加两工作区测试：停用一方不影响另一方；旧实例晚到的 SDK 回调不能修改新实例状态。业务缓存、连接和媒体路径按实例/工作区归属，不放可变模块全局。
- [x] 运行消息与生命周期测试。渠道正常运行、启动失败和停止失败三种状态都可准确观察后，再进入钉钉迁移。

## 步骤四 按同一结构整理钉钉

**已完成（2026-10-09）。产出：独立的钉钉插件、旧行为兼容测试和与飞书一致的资源归属约定。**

功能与测试由不同子 agent 编写，独立审查通过。已覆盖 5 项轻量注册、66 项钉钉运行测试和 19 项通用接口契约；与飞书及打包的最终联合结果在步骤五记录。钉钉保留原异步连接方式，`start()` 完成本地资源启动后返回，远端连通性以 `health_check()` 为准。

**文件：** 新增钉钉插件目录内上述文件；`dingtalk_suite.py`；对两渠道参数化的注册、生命周期测试。

**接口：** 导出 `plugin = DingTalkChannelPlugin()`，注册 `channel = "dingtalk"`；`from_config` 保持现有 DingTalkConfig 和与飞书相同的宿主参数。对外使用同一套 BaseChannel 契约，平台内部不要求与飞书拥有完全相同的方法。

- [x] 先把现有私聊/群聊、SessionWebhook、主动发送、媒体、AI Card、流式回复、重连和旧卡片恢复用例指向新插件的行为接口。
- [x] 从当前三千多行的渠道实现中分离 SDK 与连接操作，复用已有 handler、AI Card 和内容工具文件；公共消息编排继续交给 BaseChannel，不重写平台行为。
- [x] 延迟加载 `dingtalk_stream`、阿里云 SDK，以及继承 SDK 类的 handler；同时处理模块级 SDK 类型别名和补丁，避免仅移动一条 import 后仍间接加载。
- [x] 优先将 endpoint、请求超时等调整限制在本实例客户端上；确需全局补丁时明确唯一归属及恢复条件，并测两工作区不同配置不会互相覆盖。
- [x] 应用步骤三的通用启停测试。重点修正同步 `join(timeout=3)` 阻塞主循环、未确认线程退出就清除客户端引用的问题；覆盖重连任务、消息合并定时器、AI Card 收尾与 HTTP 会话。
- [x] 验证旧 Webhook 地址、会话标识、AI Card 待恢复记录仍可读取；发送超时不自动重复产生消息或卡片。
- [x] 跑通新钉钉测试及对应 BaseChannel 契约，再跑飞书回归，确认抽出的共同约定未反向破坏第一份实现。

## 步骤五 完成插件包和接入前验收

**已完成（2026-10-09）。产出：两份本地候选插件包、独立验收及接入 A 的清单。**

验证：综合入口 91 项通过（7 个隔离套件入口、51 项宿主插件回归、33 项打包回归）；隔离套件包含 182 项渠道用例及 4 项解压包验收。另对最终交付 ZIP 执行 4 项验收，全部通过。相关 pre-commit 检查和独立代码审查通过，功能与测试分别由不同子 agent 编写。

候选包：[飞书](../../../dist/channel-plugins-preview/feishu-0.1.0.dev0.zip)、[钉钉](../../../dist/channel-plugins-preview/dingtalk-0.1.0.dev0.zip)；[清单与 SHA256](../../../dist/channel-plugins-preview/index.json)。已核对 ZIP 文件完整性及与源码的逐文件一致性；产物保持本地、未发布。包中不含 SDK 安装文件，大小不代表实际安装或内存降幅。

真实机器人联调因未提供测试凭据而待验证；完整 SDK 版本组合和最终宿主安装、启停、更新仍需后续验收。本阶段未切换旧渠道入口或移除主包默认依赖。

**文件：** 两插件的 `requirements.txt`、`plugin.json`、README；`packaging_suite.py`；必要时修改 `scripts/pack/generate_plugin_metadata.py` 并补 `tests/unit/plugins/test_generate_plugin_metadata.py`。

- [x] 对照实际导入逐项核对依赖，包括模块名与发行包名不同的情况。飞书从当前 `lark-oapi>=1.5.3`、钉钉从当前 SDK 约束起步，不顺手升级 SDK 或新增未经验证的上限。
- [x] 列出“可从主包移除的专用依赖”和“仍被其他能力引用的共享依赖”。现有审计脚本可复用解析逻辑，但其 CLI 当前审计的是 pyproject，不把它直接当成插件 requirements 验证。
- [x] 复用现有打包方式验证解压后的文件完整性。为打包脚本的 `KIND_DIRS` 补齐 `channel` 分类支持及测试；本阶段仅生成本地产物，不发布。
- [x] `test_archive_runs_without_builtin_platform_implementation`：从解压包加载，阻止导入原 `qwenpaw.app.channels.feishu/dingtalk` 平台实现，断言注册及消息测试仍可运行。稳定的宿主 BaseChannel、公共工具及暂留的配置模型不在此禁用范围。
- [x] 分开跑无平台 SDK 的导入测试、带真实 SDK 的导入兼容测试和模拟网络的行为测试；SDK 替身只能替代外部边界，不能把新插件整个消息方法替换掉再声称收发通过。
- [x] 已记录真实机器人联调待验证。尚无测试凭据，不将模拟网络测试等同于真实收发、断线重连和停用验收。
- [x] README 写明配置/数据兼容表、资源归属、依赖、测试命令和最终接入项，形成可提供给 A 的交接材料；不要求 A 先确认所有细节才能继续本阶段。

独立测试入口：

```bash
QWENPAW_WORKING_DIR=/tmp/qwenpaw-channel-plugin-tests python -m pytest -q tests/unit/plugins/channels/test_channel_packages.py
```

测试入口使用当前测试解释器，以 `-I -m pytest --confcutdir <suite_dir> <suite_files>` 启动子进程。缺少声明的测试依赖要显式失败，不能跳过后计为验收通过。每个步骤先运行相关用例，再运行此前已完成的渠道回归。

## 步骤六 A 可用后集中接入并切换旧入口

**只有这一阶段依赖 A。产出：用户可以正式安装、启用、停用和更新的渠道插件。**

**涉及文件：** `src/qwenpaw/app/channels/registry.py`、`manager.py`、`src/qwenpaw/plugins/registry.py` 及 A 最终提供的生命周期模块；配置/渠道列表相关入口、`pyproject.toml` 和发行清单；新增真实框架集成测试。

- [ ] 将 A 的实现合入工作分支，先运行其生命周期测试，再对照现有 register_channel / BaseChannel 适配。只有确有差异的接入代码需要调整，不重做渠道本体。
- [ ] 共同完成官方渠道归属迁移：允许有明确插件归属的 `feishu`、`dingtalk` 接管原键，保证一个键只有一个拥有者。不能简单取消所有内置键保护；旧入口与新插件不能同时建立连接。
- [ ] 补齐静态渠道说明、未安装/未启用/运行失败状态，以及原工作区配置的读取路径；验证全局停用与单工作区停用的区别。
- [ ] 使用真实 PluginLifecycle、WorkspaceProjector 和 Manager 跑安装、启用、收发、停用、再启用；确认三道条件——渠道可用、工作区有配置、enabled 为真——都满足才创建实例。
- [ ] 跑更新失败、停止失败和取消的集成测试：已停止且可恢复时恢复旧版本；资源未停时保留失败实例及报告，不启动新版，也不再启动一份旧版。
- [ ] 接管验证通过后，从默认依赖和注册器中移除两渠道对应项；共享依赖保留。旧实现若仅为 import 兼容而暂留，采用明确兼容入口，不长期维护两份业务逻辑。
- [ ] 验证新轻量安装不携带未选择的 SDK；完整预设和旧用户升级仍提供原已启用渠道，缺组件时保留配置并提供明确恢复路径。
- [ ] 在相同环境比较只开 Console 时的平台 SDK 导入、启动时间、空闲线程/连接及内存；再比较启用渠道后的功能与资源。停止能释放连接和任务，不承诺同进程卸载后 Python/SDK 内存立即完全归还系统。

## 完成标准与执行顺序

**顺序：轻量入口与基线 → 飞书收发 → 飞书启停 → 钉钉复用 → 包与独立验收 → 接入 A 并正式切换。** 每个实现步骤配套测试并形成可单独审查的提交；只提交相关路径，不批量纳入工作区已有未跟踪文档。

**A 完成前的完成标准：** 两份插件代码和候选包、轻量导入、收发行为、失败清理、多工作区与旧数据测试均通过；真实平台测试结果单列；主分支原入口继续可用。

**最终完成标准：** 在真实宿主上安装、启停、更新和恢复通过；旧用户可继续使用；默认安装/导入确实减少专用 SDK；交付可复用的 Channel 样例与验收用例。独立测试通过不能替代这一层验收。

## 工作量参考

按 B 单人推进、已有测试可复用估计：步骤一约 0.5 天；步骤二至三约 2–3 天；步骤四约 1.5–2 天；步骤五约 0.5–1 天。A 完成前可持续开展约 **4.5–6.5 个工作日**的工作；步骤六另预留 **1–2 天**联调，不含外部平台权限等待或新增框架缺陷的修复。A 提前可用时，可以先接入已完成的飞书，不必等待钉钉全部完成。
