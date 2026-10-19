# 其他官方 Channel 统一插件化方案与 checklist

> **For agentic workers:** 实施时使用 `superpowers:subagent-driven-development` 或 `superpowers:executing-plans`，按任务逐项验收。功能代码和测试代码必须由不同子 agent 编写；本文件只做规划，不授权立即实施。

**Goal:** 将剩余官方外部渠道迁入与飞书、钉钉一致的描述式插件和共享依赖子进程体系，保持原有用户配置、消息能力与数据可用。

**Architecture:** 主进程保留队列、会话、ACL、审批、Agent 执行及轻量代理；渠道插件负责 SDK、连接、平台消息、媒体和平台状态。复用现有 Loader、执行后端、工作区投影和进程组件，不为各渠道复制环境、进程管理或更新事务。

**Tech Stack:** Python、asyncio、静态 JSON 描述、现有双向数据 RPC、共享 Python 解释器及依赖目录、pytest、现有官方源码 ZIP 与发行资源生成机制。

**Spec:** 用户本轮要求；本文件第 1–4 节的统一约束；参考当前 `plugins/channel/feishu/`、`plugins/channel/dingtalk/`。`docs/初版方案.md` 中“普通渠道同进程”和统一要求 `plugin.py` 的旧表述，不作为本轮执行规范。

## 1. 完整范围

更新日期：2026-10-15。源码核对基线：`0cf784381` 及当前工作树；工作树中已有的公共扩展也纳入参考，不将其重复列为从零开发。渠道清单取注册器、`ChannelConfig` 和 `plugins/channel/` 的并集，而非仅看前端列表。未勾选项是本轮的交付验收要求，不代表相关代码一定尚不存在。

当前共 **19 个官方渠道**：飞书、钉钉已迁移；**16 个外部渠道需要迁移或对齐**；Console 保留 Core，并有单独的保留验收清单。

| 渠道 key | 当前源码 | 本轮目标 | 建议批次 |
|---|---|---|---|
| `mqtt` | `plugins/channel/mqtt/` | 功能迁移已验收；累计发布验收待完成 | 第一批 |
| `mattermost` | `plugins/channel/mattermost/` | 功能迁移已验收；累计发布验收待完成 | 第一批 |
| `discord` | `plugins/channel/discord/` | 功能迁移已验收；累计发布验收待完成 | 第二批 |
| `slack` | `plugins/channel/slack/` | 功能迁移已验收；累计发布验收待完成 | 第二批 |
| `telegram` | `plugins/channel/telegram/` | 功能迁移已验收；累计发布验收待完成 | 第二批 |
| `qq` | `src/qwenpaw/app/channels/qq/` | 新描述式插件 | 第二批 |
| `wecom` | `plugins/channel/wecom/` | 模拟功能已验收；公共发布验收待完成 | 第二批 |
| `yuanbao` | `plugins/channel/yuanbao/`，含 `proto/` | 模拟功能已验收；公共发布验收待完成 | 第三批 |
| `xiaoyi` | `plugins/channel/xiaoyi/` | 模拟功能已验收；公共发布验收待完成 | 第三批 |
| `wechat` | `src/qwenpaw/app/channels/wechat/` | 新描述式插件；兼容旧 key `weixin` | 第三批 |
| `matrix` | `src/qwenpaw/app/channels/matrix/` | 新描述式插件，保留加密及设备状态 | 第三批 |
| `imessage` | `plugins/channel/imessage/` | 模拟功能已验收；公共发布验收待完成 | 第三批 |
| `onebot` | `src/qwenpaw/app/channels/onebot/` | 复用平台拆分，改为当前 v2 接入 | 端点准备后，可与第二批并行 |
| `azure_bot` | `plugins/channel/azure_bot/` | 旧同进程插件升级为描述式插件 | 端点与身份准备后，可与第二批并行 |
| `voice` | `src/qwenpaw/app/channels/voice/` | Twilio 语音插件，含通话与隧道生命周期 | 最后一批 |
| `sip` | `src/qwenpaw/app/channels/sip/` | SIP 插件，保留 dev / LiveKit 双后端 | 最后一批 |
| `console` | `src/qwenpaw/app/channels/console/` | 保留 `builtin + in_process` | 全程回归 |
| `feishu` / `dingtalk` | `plugins/channel/{feishu,dingtalk}/` | 既有规范样例；公共改动必须回归 | 全程回归 |

批次表示接缝依赖和迁移难度，不要求上一批全部完成才开始下一批。普通渠道的拆分、测试整理、描述文件准备可并行；受公共接口缺口影响的接入验收需等待对应准备任务完成。

**本轮完成的含义：** 16 个渠道都能通过新版插件系统发现、安装、配置、启停和更新；Core 最终只保留 Console 的运行实现。轻量配置模型、旧数据 reader 和通用协议可以继续留在 Core，不以清空整个 channels 目录作为目标。

**评审重点：** 未安装插件仍能编辑旧配置（G1）；重复消息与不确定发送结果不导致误重发（G2）；扫码不抢占其他工作区凭据（G3）；旧 webhook URL 和在途通话不串实例（G4）；卸载/更新不丢认证及业务状态（C5–C6）。各项均须有对应失败路径测试。

### 1.1 先复用什么，仍缺什么

| 当前可复用的基础 | 后续渠道需要完成的工作 |
|---|---|
| `plugin.json → channel.json → ChannelBackend → IsolatedChannelProxy → ChannelProcess/PluginProcess` | 写自己的描述、Driver 和平台实现，接同一条安装/运行链；不新增渠道专用 Loader 或进程管理器 |
| `SharedResponseDriver` 的路由持久化、入站 ACK/重试、出站收据及响应结束 | 普通消息编辑、无卡片收尾、typing 准备还需 G2 补齐；当前收尾仍使用 `card_id/update_stream`，不能直接认为所有平台都适用 |
| 当前工作树的 `official_channel_catalog.py`、owner/key 分离、可信资源读取、按渠道迁移记账和发布前 schema 校验 | 每个正式交付的渠道追加目录记录、同步撤旧入口，并验收配置/发行；不把未完成的 16 个渠道提前声明为已交付 |
| 配置式工作区接入、已有后台连接启动、停用与更新事务 | `ChannelManager.from_env()` 仍只枚举有 Channel 类的注册；G2 要补描述式渠道的环境配置接入，避免迁移后原环境变量入口失效 |
| 历史扫码、端点和语音 platform 代码 | G3/G4 要接到新版数据协议；历史 v1 代码存在不等于已经可供 v2 插件直接使用 |

**迁移方法：保留平台业务算法，替换它与宿主连接的方式。** 飞书、钉钉用于参考插件产物、生命周期和可靠性契约；其卡片、目标编码和 SDK 细节不作为所有渠道的必备模板。

## 2. 所有渠道遵循的统一规范

### 2.1 插件产物及身份

```text
plugins/channel/<source_dir>/
  plugin.json
  channel.json
  requirements.txt
  README.md
  qwenpaw_channel_<channel_key>/
    __init__.py
    driver.py
    platform.py       # 按需拆分；不强制每个小渠道增加空文件
    ...              # 格式化、媒体、卡片、协议资源等
```

- 正式入口是 `plugin.json.entry.descriptor = "channel.json"`，不再要求额外的 `plugin.py` / `register()` 入口。
- `channel.json` 沿用当前 **schema v2、`runner_process`、`shared`、`entrypoint.scope=runner`、`requirements_file=requirements.txt`**；声明真实支持的 `capabilities`，新插件使用 `channel.generic.v1`。
- 描述中的 `config_fields` 必须完整对应 schema properties，字段、默认值、群聊会话行为和验证语义保持兼容。第一轮可保留 Core 中的轻量配置模型，由构建校验保证模型与描述一致，不顺带拆掉旧配置 reader。
- 新迁移代码统一使用 `qwenpaw_channel_<key>` 命名空间，避免 `telegram/`、`discord/` 等实现目录遮蔽同名 SDK，也避免 `platform.py` 平铺遮蔽标准库。飞书、钉钉现有无冲突命名不强制改名。
- `channel_key` 是已有配置/会话路由身份；`plugin_id` 是插件所有者身份；源码目录是构建输入。三者不能被框架假定永远相同。Azure Bot 保留 `plugin_id=azure-bot`、`channel_key=azure_bot`、现有源码目录名。
- 专属依赖以 `requirements.txt` 为唯一运行时清单；不能同时手写一份不同的 manifest dependencies。共享库仍按 Core 的真实使用关系保留，不能因为某渠道也用了 `httpx` 就从 Core 删除它。
- 先接现有私有官方资源流程，不为了迁移新建每插件 venv 或独立 wheel。公开市场发布单独验收；已有 Azure Bot 的 ID、版本升级路径和用户配置不能按新预览插件重置。

### 2.2 宿主与插件边界

| 保留宿主 | 移入渠道插件 |
|---|---|
| Loader / backend / delegate、工作区投影、代理、进程监督 | SDK 导入、认证、连接、重连、轮询、监听服务 |
| 消息入队、接收确认、会话与任务管理、ACL、审批授权、Agent Loop | 原生消息解析、平台发送、媒体上传下载、平台卡片和交互回包 |
| 公共响应作用域、去重及未知结果约束、轻量配置兼容 | 平台回复目标、编辑/流式策略、typing、平台业务状态 reader |

插件中可以使用 SDK-free 的公共协议和数据类型；不能靠导入旧 `channel.py`、构造原 `BaseChannel` 并传一个伪 Agent 回调来“完成迁移”。实际 Agent 执行和授权仍在宿主。

### 2.3 Driver 接口与消息契约

已有、可以直接使用的接口：

| 接口 / 实现 | 用法 |
|---|---|
| `bind(context)`、`async start(config)`、`async stop()` | 当前 worker 的组件生命周期；bind 只绑定协议，连接工作放 start |
| `context.identity.to_mapping()` | 绑定渠道、工作区实例和 generation；拒绝旧代际的回调 |
| `context.set_platform_state(...)` | 上报连接状态，不能把 worker 握手完成当作平台连接成功 |
| `InboundEvent` → `event.batch` | JSON 数据入站；带真实操作者 `acl_sender_id` 和 `session_id/user_id/debounce_key`；宿主接受后才确认 |
| `channel.send`、`channel.response.open/describe/finish` | 出站及一次响应的开始、策略、结束；需要时使用现有 prepare/reaction |
| `ResponsePolicy` | 按本次路由声明 streaming、prepare、completion_reaction，不在公共宿主写平台分支 |
| `SharedResponseDriver` 的 `_open(params, request)` / `_perform_send(params)` | 子类实现平台目标解析、响应路由建立和实际发送；复用基类的锁、入队重试、投递收据和结束状态机 |
| `_perform_reaction(params)` / `_before_cleanup_complete(snapshot)` | 只为有对应能力的平台实现反应和收尾；同时实现连接检查，不能只继承基类就宣称 Driver 完整 |
| `host.state.get/put/delete` | 小型、有界、按实例隔离的状态；不能用它塞整个数据库、音频流或无限历史 |

当前 SharedResponseDriver 的默认清理仍含 `card_id` / `update_stream` 的卡片假设。非卡片渠道需要在已有收尾 hook 中正确处理自己的消息、typing、连接资源，并验证未知结果；不能把 `message_id` 假装成 `card_id`。经过第二类平台验证确实共用的部分，由公共负责人提取一次。

可靠性统一要求：同一次 IPC 入站重试复用 event_id；平台提供稳定消息 ID 时再据此识别重复推送。MQTT 等没有业务消息 ID 的协议不能靠文本哈希去重，也不能承诺跨重连只执行一次。发送可能已成功但收据丢失时标记 unknown，不盲目再发；响应取消/结束只操作本次 response_handle。媒体路径必须对应已授权工作区或明确允许的外部资源。

卡片的真实点击者通过 `acl_sender_id` 传入宿主。宿主已有审批命令的身份校验，直接复用；原消息发送者只能作为上下文，不能代替点击者。平台可先确认“收到点击”，但只有宿主确认审批结果后才能展示“已批准/已拒绝”。

### 2.4 生命周期和兼容

- 查询列表/配置、缺包展示、未启用工作区不得导入渠道实现或 SDK，不启动常驻 worker。显式安装/修复才允许获取依赖，普通启动不装包。
- 共用所选 Python 解释器和依赖目录，SDK 在子进程导入；复用 `PluginProcess` / `ChannelProcess`，不为渠道另写进程管理器。
- 全局停用影响该插件的所有实例；工作区停用只影响该工作区；所有后台资源都要有明确归属。
- 停止必须先关闭入口、处理在途工作、断开连接并确认退出。不能只 join 超时、记录警告、清空引用就报告停止成功。停止失败保留实例/资源证据，禁止并行启动替代实例。
- 更新、回滚、卸载走现有插件事务及源目录保护。停用保留设置；卸载按现有 Loader 清除 `plugins.<plugin_id>` 安装设置并记卸载墓碑，但保留 `channels.<channel_key>` 用户配置、会话、媒体、认证文件、加密数据库和长期业务状态。不要把两类配置混为一谈。
- 保持原 channel key、session_id、回复目标、字段与环境配置入口。旧文件通过兼容 reader 读取；迁移有冲突时不覆盖用户数据，不以目录改名解决冲突。
- 明确区分源码已安装、全局启用、工作区启用、worker ready、平台 connected。复用 `ChannelManager.start_all()` 已有的后台连接调度；代理 start 仍等待 connected。验证插件探测及整体初始化不把慢连接重新带回 Core 启动关键路径，不另造启动器，也不把 ready 伪装成 connected。

## 3. 公共准备任务与缺口

这部分由公共框架负责人维护，渠道负责人不各自修改一套。新接缝属于规划新增，不能当作当前框架已具备。

### G1：扩展官方目录、所有权迁移与发行验收（基础已在工作树，按批次接入）

**主要文件：** `plugins/official_channel_catalog.py`、`app/channels/catalog.py`、`app/channels/registry.py`、`plugins/registry.py`、`plugins/channel_adapter.py`、`plugins/official_channels.py`、`config/config.py`、`app/routers/config.py`、`scripts/pack/{generate_plugin_metadata,validate_channel_contracts}.py`、`setup.py`、`scripts/pack-tauri/stage_channel_runtime.py`、`pyproject.toml`。

- [x] 复用 SDK-free `OFFICIAL_CHANNELS` 清单，记录 `channel_key/plugin_id/source_dir/icon/doc_url`；所有权表、生成器、修复入口和完整预设从同一清单派生。只追加本批已经能交付的描述式插件。
- [x] 验证已实现的 `describe_channel()`、`register_channel_descriptor()` owner/key 分离；仍核验 manifest、description、注册调用者的归属，保护 Console/官方保留名和第三方撞名，错误指明占用者。`ChannelBackend.runtime_for()` 按可信 channel_key 选择；普通更新不得改变已有 channel_key，以免旧配置和会话失联。
- [x] 复用 `bundled_channel_description(plugin_id)`、目录元数据和 `_validated_channel_config()`：未安装代码仍能显示、保存及校验配置，按 owner 查安装目录和修复地址。Azure 不在 `ChannelConfig.model_fields` 中，要覆盖 schema-only 路径。图标/文档接官方静态清单，不向封闭的 v2 描述私加字段。
- [x] 每个渠道切换所有权时，同一个可交付改动中移除 `_BUILTIN_SPECS` 的旧入口、加入官方记录、接通新插件。仅添加 owner 不够：当前 `register_channel_descriptor()` 仍会拒绝 BUILTIN_CHANNEL_KEYS。
- [x] 验证已泛化的 `build_official_channel_bundle()` 按清单与描述中的合法入口校验实际文件，支持 `qwenpaw_channel_<key>`。索引、ZIP 根、bundled 修复地址按 plugin_id，源码定位按 source_dir；新增渠道带辅助资源实测，Azure 别名和公开发行必须覆盖。
- [x] 复用 `initialize_channel_policy()` 与 `legacy_captured` 按渠道捕获旧启用意图，兼容现有 version=1 用户；新渠道只捕获一次，不能因全局 version>=1 跳过，也不能重复复活已卸载插件。
- [x] 扫描实际保存的全局/Agent 配置，保留全局停用、已有安装和卸载墓碑；旧活跃渠道可预置源码，缺 SDK 给显式修复入口，不在启动中联网安装。
- [x] 逐项移除主包中的渠道专属依赖；完整 extras 与插件 requirements 的版本/平台标记统一校验。SIP 后端、Matrix 加密等附加依赖的显式入口保留兼容，不向 v2 描述私加尚不支持的 profile 字段。
- [x] 所有正式发行入口继续执行 `validate_official_channel_contracts(project_root)`；保留模型与 descriptor 的字段、实际默认值、枚举/约束必须一致。校验不得导入 Driver/SDK，不得将 JSON Schema 的业务字段名或数据值当作注释删除；自定义验证不能用“忽略差异”绕过。
- [x] 回归源码 ZIP、wheel/sdist/editable、桌面资源清单及 Docker 构建脚本：发布资源确实含所需实现/辅助文件，父进程不因构建目录变化重新导入 SDK。将官方可信源校验与 `publish=false` 解耦，仍核验清单归属、版本及摘要，Azure 原公开发布路径不得因加入 bundle 而消失。

**完成证明：** 参数化覆盖全部已迁移 owner；Azure 的不同 ID/key 可安装、显示、启停与卸载；第三方不能冒占；已有飞书/钉钉用户升级不重复预置；新批次旧活跃渠道迁移只发生一次。

### G2：共用回复与渠道契约测试（与首批渠道同步）

**主要文件：** `channel_protocol/{shared_response_driver,response_policy,models}.py`、`app/channels/{manager,isolated_proxy}.py`、`plugins/channel_adapter.py`；新增 SDK-free 兼容 reader `config/channel_env.py`。复用 `tests/fixtures/channel_plugins/extension_*_suite.py`，新增各平台 process suite 与对应薄 wrapper。

- [x] 用 MQTT 或 Mattermost 先验证无卡片的完整 v2 链路；再用一个可编辑消息的渠道验证 streaming 与收尾，冻结可复用 hook 的行为。
- [x] 现有第三渠道扩展测试已证明基础通用接入；本轮在真实平台迁移上补齐非卡片完成、typing 和编辑收尾，不重写一套 generic proxy。只有现有 hook 无法表达且至少两类平台共用的能力才扩公共协议。
- [x] 将 `_cleanup()` 的平台效果拆为 `_needs_stream_cleanup(ref) -> bool` 和 `async _perform_stream_cleanup(ref, resource, sequence) -> None`；公共层保留收据、unknown、序号和 checkpoint，子类只执行效果。非卡片、无需外部效果的结束不以平台在线为前提，不伪造 `card_id` 适配卡片逻辑。
- [x] 将 `channel.response.prepare` 的身份/参数校验、路由锁和投递日志归入公共 Driver；平台仅实现 `_perform_prepare()`。准备操作有自己的内部类型，不将 typing 伪装为 `stream.start`；重复事件新建的孤儿 route 清理也复用 `_admit_event()`，不复制到每个渠道。
- [x] 在 `ChannelManager.from_env()` 接通描述式渠道，沿用配置式入口的代理构建；`read_channel_env(channel_key: str, environ: Mapping[str, str]) -> dict | None` 只读取和解析配置，不构造 Channel。保留原环境变量名、默认值、trim、布尔/数值解析及无效值回退；没有旧环境入口则返回 None。兼容映射集中为轻量数据/reader，不导入旧 Channel/SDK，不擅自给 v2 描述增加顶层字段。
- [x] 共用测试覆盖身份、去重/ACK、投递 unknown、响应结束、关闭失败、代际隔离和多工作区，不为平台拷贝 Supervisor/Loader 测试实现。
- [x] SDK/网络 fake 只替代外部平台边界，代理、实际子进程、Driver、投影、安装/更新事务都用生产实现。
- [x] 保留各渠道原测试的业务断言；旧 BaseChannel 专属测试按职责转为 Driver/platform/宿主测试，先建立对应关系再删除旧测试。
- [x] 公共机制改动同步回归飞书、钉钉、Console 和插件生命周期；功能与测试独立作者，不为通过而加 timeout、skip、xfail 或削弱断言。

### G3：扫码、验证和其他管理操作（微信、QQ、企业微信及 CLI 接入前置）

**主要文件：** `app/channels/qrcode_auth_handler.py`、`app/routers/config.py`、`cli/channels_cmd.py`、`cli/doctor_connectivity.py`、公共执行/渠道协议；迁移各插件中的认证适配代码。

- [x] 建立统一的显式管理操作接缝，覆盖扫码获取/轮询、连接验证、必要的状态查询。方法、DTO、权限和运行方式由公共负责人统一冻结后，渠道负责人只实现平台部分。
- [x] 冻结的契约至少包含：操作白名单、请求 ID、工作区/实例/代际、纯数据参数与结果、取消/超时、凭据提交方。沿用现有数据 RPC 和进程回收；不允许插件提交任意 Python 函数名让宿主执行，也不单独搭管理进程框架。
- [x] 保留已有配置 API 与返回字段；主进程只做访问控制、参数校验、配置提交和轻量图片生成，不导入 `wechat.client` 等平台实现。
- [x] 梳理 `channels_cmd.py` / `doctor_connectivity.py` 的逐渠道配置与探测：纯静态表单、通用网络检查可留宿主；需要平台客户端的检查转显式管理操作，不能删掉渠道目录后留下悬空 import。
- [x] 未启用渠道的显式扫码/验证允许有界的短时子进程，但不顺带启动长期消息消费；请求结束/取消必须回收。不能把现有 inspect 模式当作已经支持扫码运行。
- [x] 凭据只提交给请求所属工作区；缺插件/SDK准确报告，管理操作与运行实例同时存在时不抢占其连接、文件或账号状态。
- [x] 使用 WeChat 跑通管理链路；QQ/WeCom 接同一接缝，现有飞书/钉钉扫码入口也纳入不在父进程导入平台代码的回归。

G3 分层验收补记（2026-10-15）：五平台管理接缝复用现有 native/Session 证据，微信最后的空凭据分支已修复并定向通过 8 项；QQ/企业微信的真实保存边界与飞书/钉钉剩余 16 项已完成。此项不代表五平台全部业务或 C8 已验收。doctor 能力选择的新增故障已修复并通过 6 项控制；CLI 缺代码时的目录可见性已修复，原 3 个失败用例通过。微信实际 Session 扫码、工作区授权 PUT、持久化配置交给新 worker、真实 Agent 回复及账号/文件占用拒绝的组合验证通过；等待扫码期间 Console 也能执行真实 Agent。纯宿主扫码会话不再被误记为独立依赖执行环境，实际管理进程仍按原规则审计。这些证据不代表已加载完整 App 的自动热重载；工作区凭据、短时管理和运行账号/文件边界已由分层证据完成对应；不把这项结论扩展为真实平台账号通用接管能力。证据索引：项目 `.superpowers/sdd/2026-10-14-official-channel-pluginization/` 下 `g3-current-gap-audit.md`、`wechat-g3-native-closeout-report.md`、`doctor-capability-test-report.md`。

### G4：监听服务、语音轮次与宿主控制（按端点、通话两步交付）

**主要文件：** OneBot/Voice 已有 platform、`app/routers/voice.py`、`channel_protocol/host.py`、公共 v2 协议与代理、宿主任务控制入口；具体接缝不复活旧独立环境管理器。

- [x] 子进程持有 OneBot/HTTP webhook 的 listener；复用旧平台的端口冲突、认证和关闭能力。需要宿主查找端点时，按实例/generation 维护 SDK-free 登记，停止或更新后旧端点不可再路由。
- [x] 旧协议已有 `CoreEndpointRegistry`，但当前 generic proxy 未接端点 RPC；复用其有效校验并补 v2 接入与测试，不能把旧代码存在当作接入完成。OneBot/Azure 自有配置端口可先独立验收，不必等待语音控制全部实现。
- [x] 默认沿用渠道配置的监听端口；只有兼容原主机 URL 确有需要时增加统一 HTTP/WS 转发，保留认证所需 method/path/query/公开 URL，并验证流控、关闭和路由归属，不为每个平台写主进程 SDK 路由。
- [x] OneBot、Voice 旧 Driver 使用 `bind(peer, identity)` 和 prepare/commit，不能直接填进当前描述；只复用 platform 和既有测试，重接 `bind(context)/start(config)/stop()`。
- [x] 将 Voice/SIP 直接调用宿主 `_process` 改为标准入站、文本响应与通话轮次映射；新增控制操作若涉及取消/清会话，必须携带实例、代际、通话/任务范围并由宿主确认，不能传 Agent 对象或取消其他实例工作。
- [x] Voice/SIP 的 ASR、TTS、音频和设备连接留在子进程；IPC 传转写文本、文本输出与控制事件，不把高频 PCM 挤入通用 JSON 消息队列。
- [x] 保留现有三个 `/voice/*` 入口和状态回调，替换路由对旧 VoiceChannel 属性的直接访问。当前没有通话管理/外呼 API，不将其列为迁移交付物。
- [x] 区分旧行为和新增能力：Voice interrupt 当前只记日志，SIP speech-start 当前只停 TTS，XiaoYi cancel 主要清映射/回包。迁移先证明原链路；“真实取消宿主 Agent”作为单独的公共增强验收，不能写成原能力或偷偷改变语义。

**完成证明：** OneBot/Azure 监听关闭后端口可复用；旧代际端点拒绝；Voice/SIP 能经真实宿主链路收转写、回文本、结束通话；若交付取消增强，证明只取消对应通话当前轮次，失败不伪报成功。

G4 分层验收补记（2026-10-16）：端点、签名与转发限制沿用 A1/A3 已有证据，新增六项定向验证补齐连接建立时停用、清理残留及重试、双向阻塞取消、断链单次请求；Voice 正式通话与双工作区/SID 归属、SIP 双后端也已模拟验证。Voice 未配置时的 HTTP 兼容回归已修复，原两项及五项防误回退控制通过。候选资源启动后失败的回滚证据限于宿主提交前；不声称提交后自动回滚、真实电话或新增宿主 Agent 中断能力。

## 4. 每个渠道的共同验收 C1–C8

每个渠道必须逐项记录 C1–C8 结果，再执行第 5 节自身的 checklist。不能仅以“文件搬到 plugins 了”验收。

| 编号 | 可检查的完成条件 |
|---|---|
| C1 制品 | plugin.json/channel.json/requirements/README/实现完整；静态 schema、身份、摘要与发行制品一致 |
| C2 被动路径 | 查询配置、全局停用、未启用工作区、缺包状态不导入该渠道实现/SDK，不启动常驻进程 |
| C3 运行边界 | 实际共用解释器；SDK 只在 worker 导入；宿主 Agent、ACL、审批与队列继续真实工作；连接失败限于本实例 |
| C4 业务保真 | 原收发、私聊/群聊/线程、媒体、流式、交互和主动发送的已支持能力有对应测试，明确不适用项依据 |
| C5 生命周期 | 启停/重启/工作区停用/全局停用/卸载/更新回滚跑通；残留资源与停止失败可重试且不能重叠启动 |
| C6 数据兼容 | 配置及已有环境变量入口、旧会话/目标、认证和持久文件可读取；迁移尊重旧停用/卸载墓碑；卸载清插件安装设置，保留渠道用户配置和业务数据 |
| C7 隔离与可靠性 | 两工作区互不干扰；旧 token/generation 无效；同一事件 ID 的入队重试不重复执行；投递 unknown 不自动重发；取消资源归属准确 |
| C8 发布与减负 | 从实际制品安装并通过模拟业务；专属依赖退出默认主包；记录父/子及合计内存、启动、进程/线程、包体积的可比数据 |

资源测量必须同时看父进程和所有 worker；未启用时负担应下降，但启用更多渠道的总内存不保证下降。内置私有源码 ZIP 仍占包体积，不能把“SDK 按需安装”写成“未使用渠道的源码完全不下载”。

## 5. 其余每个渠道的独立 checklist

各负责人均需完成 C1–C8，再完成自己的清单。默认从 `src/qwenpaw/app/channels/<原目录>/` 迁入 `plugins/channel/<key>/qwenpaw_channel_<key>/`；Azure 保留现有插件根目录。下面列出的文件组合表示连同辅助代码一起迁移，不代表要机械地拆成相同数量的文件。

每个渠道按同一顺序执行：**独立测试作者整理原能力与失败用例 → 实现者编写描述和 Driver/platform → 接真实 worker 验证 → 接发行/旧数据迁移 → 删除旧入口并回归**。先跑新增测试确认未实现处失败，再实现；保留原有断言。共同测试与验证命令见第 6 节。

测试引用约定：下文简称 `unit/test_*.py` 对应 `tests/unit/channels/`，`contract/test_*.py` 对应 `tests/contract/channels/`，`integration/test_*.py` 对应 `tests/integration/`。每个渠道另新增 `tests/fixtures/channel_plugins/<key>_process_suite.py` 及 `tests/unit/plugins/channels/test_<key>_process.py` 薄入口；共用测试逻辑参数化，不复制 16 套框架。

### 5.1 MQTT（`mqtt`）

**源码/测试：** `mqtt/channel.py`；`unit/test_mqtt.py`、`contract/test_mqtt_contract.py`。**前置：** G1、G2；适合首个非卡片样例。

- [x] 搬迁 paho client、回调、loop 线程与发送逻辑，声明 `paho-mqtt`；子进程回调经标准事件入队，不再持有宿主 enqueue。
- [x] 保留 JSON `text/redirect_client_id` 与纯文本输入、topic 第二段回退、`mqtt:{sender}`、发布 topic 的 `{client_id}` 替换，以及原媒体 URL/音频占位行为。
- [x] 保留配置和环境变量的 port/qos/transport 默认值、TCP/WebSocket、TLS CA/客户端证书、认证、clean_session 与重连订阅。
- [x] 补齐发布收据：校验 `publish()` 返回值，并按 QoS 等待相应 SDK/Broker 确认；不能把调用受理当成功，确认丢失按 unknown 处理。这是接新投递契约需要的修正。
- [x] 同一次 IPC 重试复用事件 ID；MQTT MID 限定连接作用域，没有业务 ID 的两条相同文本均须接收，不做内容哈希去重。
- [x] stop 等待 loop、连接和在途发布结束；测试迟到回调、TLS 错误、重连、确认丢失、两工作区和停用更新，完成 C1–C8。

### 5.2 Mattermost（`mattermost`）

**源码/测试：** `mattermost/channel.py`；`unit/test_mattermost.py`、`contract/test_mattermost_contract.py`、`integration/test_mattermost_mock.py`。**前置：** G1、G2；适合与 MQTT 并行。

- [x] 搬迁 REST/WebSocket、bot identity、附件与 typing；`httpx/websockets` 按真实使用声明，不作为专属库直接从 Core 删除。认证成功后才报 connected。
- [x] 保留 DM、mention、已参与线程跟随、自身消息过滤；session 保持 `mattermost_dm:{channel}` / `mattermost_thread:{root或post}`，参与记录以宿主接受事件为准。
- [x] 保留首次上下文、历史排序、bot reply 截断、root 回复目标、长文切分及附件显示文件名；历史/参与缓存按实例隔离。
- [x] 当前没有原生流式和审批卡片，不虚报能力；typing 放在宿主接纳/授权后的响应准备及结束流程，拒绝的消息不残留 typing。
- [x] 保持已有主动发送目标解析；原线程目标缺少持久 root→channel 映射，不能承诺重启恢复线程主动发送。若补此能力，单独交付路由持久化及旧数据测试。
- [x] 监听、事件任务、typing、HTTP 关闭均有完成证据；mock server 验证线程跟随、并发、媒体失败、重连与停止失败，完成 C1–C8。

### 5.3 Discord（`discord`）

**源码/测试：** `discord_/channel.py`；`unit/test_discord.py`、`unit/test_discord_media_helpers.py`、`contract/test_discord_contract.py`。**前置：** G1、G2。

- [x] 搬迁 Client/Gateway、附件和 HTTP，声明 `discord-py`；payload 不命名为 `discord`，避免遮蔽 SDK。
- [x] 保留 bot 消息策略、用户/角色/@everyone mention、author ID 授权、DM/频道 session，以及 thread starter 与 `on_thread_create` 竞态处理。
- [x] 保留目标 cache→fetch→create_dm 回退、proxy/auth 和 intents；SDK 对象不进入 IPC，主动发送可从纯数据目标恢复。
- [x] 保留 2000 字分块与跨块代码围栏、媒体分类、data URL 文件名及临时文件清理；不要只验证文本。
- [x] 将 typing、reasoning、streaming 编辑和最终长文替换接 ResponsePolicy；目标被删、编辑失败或 unknown 不盲发替代消息。
- [x] 停止 Gateway、typing、下载及 thread-race 延时任务；新增角色 mention、线程竞态、取消/限流与两工作区测试，完成 C1–C8。

### 5.4 Slack（`slack`）

**源码/测试：** `slack/{channel,handler,sender,format,utils,constants}.py`；`unit/test_slack.py`、`contract/test_slack_contract.py`。**前置：** G1、G2。

- [x] 整体迁移 Socket Mode、handler、sender、格式化与客户端，声明实际直接依赖的 slack-bolt/slack-sdk 等；不只搬 channel.py。
- [x] 保留 message/app_mention 去重、DM/group disabled、线程 mention 规则、thread_ts、原 session 和 `SlackSender.resolve_route()` 的多种目标形式。
- [x] 保留 rich_text、unfurl、用户名与线程历史缓存、slash command 的即时 ephemeral 回包；平台 ACK 和宿主入队 ACK 分开。
- [x] 保留带认证的文件下载、允许下载的域名限制及 external upload 流程；proxy/NO_PROXY 对 REST、WS、上传保持一致。
- [x] 保留 mrkdwn、长文、thinking 分消息和流式编辑；网络失败的最终 edit 按 unknown 处理，不能直接 fallback 导致重复。
- [x] 停止 event handler、重连、Socket Mode、sender 和 HTTP；复用 slash/thread/proxy/route-lock 测试，并补实际 worker 的更新与关闭失败场景，完成 C1–C8。

### 5.5 Telegram（`telegram`）

**源码/测试：** `telegram/channel.py`、`format_html.py`、`cards/`；`unit/test_telegram.py`、`contract/test_telegram_contract.py`、`integration/test_telegram_mock_api.py`、`tests/unit/app/channels/telegram/`。**前置：** G1、G2。

- [x] 搬迁 Application、polling、格式化、媒体与 cards，声明 `python-telegram-bot`；payload 不使用 `telegram` 顶层包名，保留 proxy/base_url 与命令发布。
- [x] 保留编辑消息、引用、私聊/群聊；session 仍为 `telegram:{chat_id}`，forum 的 `message_thread_id` 只作为回复路由，不擅自拆分历史会话。
- [x] 保留 Rich Messages 表格及 32 KiB/20 列限制、rich edit、HTML/plain 降级、4000 字切分与媒体限制；网络错误不得按“不支持格式”重发。
- [x] typing、placeholder、edit 节流和最终收尾按 response_handle 管理；取消不留占位消息，媒体解码取消仍可传播。
- [x] cards 缓存改为实例有界状态；点击者为真实 `acl_sender_id`，宿主确认后更新审批结果，CallbackQuery 的平台回包不代表审批已通过。
- [x] polling 冲突/网络退避、Application/updater teardown 不因迁移丢失；验证 topic 路由、Rich Messages、伪冒点击、过期按钮和关闭失败，完成 C1–C8。

### 5.6 QQ（`qq`）

**源码/测试：** `qq/channel.py`、`cards/`；`unit/test_qq.py`、`unit/test_qq_media_helpers.py`、`contract/test_qq_contract.py`、`integration/test_qq_mock_im.py`、`tests/unit/app/channels/qq/`。**前置：** G1、G2；扫码验收依赖 G3。

- [x] 搬迁 token、WS 线程、heartbeat/seq helpers、HTTP、媒体与 cards；声明直接依赖如 websocket-client，模块级序号/卡片缓存改为实例有界状态。
- [x] 保留 C2C/group/guild/guild-DM 四类收发路径、原 session、msg_id/msg_seq、引用及媒体限制；guild-DM 卡片仍走 `/dms`。
- [x] 保留 identify/resume/invalid-session、heartbeat ACK 与快速断连退避；宿主接受后才记入站去重，旧连接回调不得再提交事件。
- [x] 保留 markdown→plain→URL 的验证错误限定；5xx/网络 unknown 不换格式重发。平台不支持的媒体类型明确失败。
- [x] cards 真实操作者入站；平台点击 ACK 与宿主审批结果分开。Portal bind/AESGCM 扫码逻辑接 G3，凭据只提交对应工作区。
- [x] stop 确认 WS 线程、heartbeat、ACK futures 和 HTTP 全退出；测试四类目标、恢复序号、媒体 fallback、审批与扫码，完成 C1–C8。

QQ C1–C7 模拟功能验收完成：原生四类目标、实际 Agent/会话、真实 Core 审批、两工作区及更新恢复已有对应证据；276 条原记录与 537 条 oracle 保留，组合/静态证明和新版失败契约单独标明。旧 Core 六文件已删除；转接后 19 个入口可收集、六个原 helper 断言通过，无实际旧导入。C8 仍统一收口。

### 5.7 企业微信（`wecom`）

**源码/测试：** `wecom/channel.py`、`utils.py`、`cards/`；`unit/test_wecom.py`、`contract/test_wecom_contract.py`、`integration/test_wecom_mock_gateway.py`、`tests/unit/app/channels/wecom/`。**前置：** G1、G2；扫码验收依赖 G3。

- [x] 搬迁 aibot client、WS、媒体和 cards，保留 SDK `wecom-aibot-python-sdk==1.0.2` 及直接依赖；私有 WS/上传 ACK 适配留在插件，不进入通用协议。
- [x] 保留 text/image/video/file/mixed、voice ASR、引用媒体解密、welcome、表格/图片处理；原群 session 及共享默认值保持不变，真实操作者与 `user_id=group` 分开。
- [x] 平台 frame 留子进程，IPC 只传路由数据；合并消息使用原有最后回复 frame，并能收尾合并涉及的全部 response handles。
- [x] 保留 processing keepalive→首个 stream 复用 sid→final 的时序；超时结束后新建 sid，重连不能让旧 stream 继续写。
- [x] 保留上传 init/chunks/finish ACK、串行锁与取消时等待文件 worker；卡片平台回包不等于审批成功，扫码接 G3。
- [x] 等待 SDK 线程、keepalive、上传任务和连接真实退出；以 gateway mock 覆盖流式竞态、混合媒体、审批、取消上传与旧群聊配置，完成 C1–C8。

验收记录（2026-10-15）：C1–C7 模拟功能验收通过；原 220 项职责已逐项对应，其中 190 项保留业务语义，30 项明确转为子进程所有权、严格失败或真实点击者审批等新契约，并非 220 项新增测试。正式 ZIP 经真实 Agent 收发、双工作区、更新恢复及媒体/卡片/合并收尾均有对应证据；旧 Core 七文件已删除，受影响测试入口 20 项收集通过。C8 仍由公共发行与资源对照收口。证据：本项目 `.superpowers/sdd/2026-10-14-official-channel-pluginization/wecom-closeout-report.md`、`wecom-entry-retarget-report.md`。

### 5.8 元宝（`yuanbao`）

**源码/测试：** `yuanbao/{channel,auth,media,codec,constants,utils}.py`、`proto/`；`unit/test_yuanbao.py`、`unit/test_yuanbao_codec.py`、`contract/test_yuanbao_contract.py`、`integration/test_yuanbao_mock.py`。**前置：** G1、G2。

- [x] 迁移完整认证、媒体及 codec，声明直接使用的 protobuf 等依赖；制品包含实际资源 `proto/conn.json`、`proto/biz.json`，不是不存在的 .proto 文件。
- [x] 保留 sign-token/HMAC、refresh、AuthBind、二进制帧与心跳、HTTP/COS 媒体鉴权、踢出/认证失败恢复。
- [x] 保留 C2C/group、短 ID 与真实目标映射、quoted/cloud_custom_data、mention、bot 过滤及 reply-status typing。
- [x] 兼容 `workspace/yuanbao_sessions.json`；无 workspace 时兼容原 `media_dir.parent` 路径，保留主动发送与 session，不覆盖已有映射。
- [x] 平台 needAck 与宿主接受 ACK 分开；使用稳定 msg_id，发送字节成功不等于平台业务送达，投递收据须表达可确认的真实范围。
- [x] 等待 receive/heartbeat/reconnect/typing/token refresh 退出；复用 codec 断言并测旧 map、坏帧、并发路由、COS fallback 和 unknown，完成 C1–C8。

验收记录（2026-10-15）：C1–C7 模拟验收通过，旧 152 项职责已逐项保留映射，文件锁阻塞故障已修复，正式 ZIP 的收发、双工作区和更新恢复通过，旧 Core 与测试导入已转接。C8 发行构建及被动路径观测已完成；完整应用/连接态资源基线仍由公共任务收口，不计作全部完成。证据：`.superpowers/sdd/2026-10-14-official-channel-pluginization/yuanbao-closeout-report.md`、`ready-channel-shared-release-report.md`。

### 5.9 小艺（`xiaoyi`）

**源码/测试：** `xiaoyi/{channel,auth,constants,utils}.py`；`unit/test_xiaoyi.py`、`contract/test_xiaoyi_contract.py`、`integration/test_xiaoyi_mock.py`。**前置：** G1、G2；doctor 接 G3，取消增强接 G4。

- [x] 搬迁 AK/SK HMAC、init/heartbeat、主域名与备 IP 双连接；保留自定义 ws_url 禁用 backup、任一可用连接的健康判断。
- [x] 旧全局连接登记改为实例/generation 所有权；保留 `xiaoyi:{native session}`、request/task/message ID、媒体和来源 server 路由。
- [x] response route 固定 task/message/server，避免同 session 的后一个任务覆盖前一个；现有映射为内存状态，不承诺重启后自动恢复主动发送。
- [x] 保留 reasoning/artifact chunks；结束时按顺序发 completed 状态与 final artifact，分别记录结果，不能只给最后一段加 final。
- [x] 兼容 clearContext/tasks-cancel 原协议回包；真实宿主取消/清会话若纳入交付，按 G4 单独验收，不把旧清映射行为当成已经取消 Agent。
- [x] stop 禁止重连、注销自身并关闭双连接/HTTP；移除 doctor 对旧 constants 的悬空导入。测试主备切换、并发 task、双连接重复事件及部分结束失败，完成 C1–C8。

验收记录（2026-10-15）：C1–C7 模拟功能验收通过；原 66 项直接职责及 19 项继承契约全部建立对应，其中 3 项停用/断连行为明确收紧为真实失败。正式插件的 Agent 收发、主备路由、双工作区、更新恢复与卸载通过；旧 Core 五文件和测试导入已转接。历史失败用例曾发生的 125 秒退出等待未取得定位证据，保留原始记录，不声称已修复或据此认定当前产品故障。C8 仍随公共发行/连接态对照收口。证据：`xiaoyi-last-oracles-test-report.md`、`xiaoyi-core-retirement-test-report.md`（位于本项目 `.superpowers/sdd/2026-10-14-official-channel-pluginization/`）。

### 5.10 微信（`wechat`）

**源码/测试：** `wechat/{channel,client,utils}.py`；`unit/test_wechat.py`、`unit/test_wechat_quoted_msg.py`、`unit/test_qrcode_auth_handler.py`、`contract/test_wechat_contract.py`、`integration/test_wechat_mock.py`。**前置：** G1、G2、G3。

- [x] 搬迁 ILinkClient、轮询线程、AES 媒体、typing 与合并缓冲；Crypto 对应 `pycryptodome`，显式核对依赖；保留 `weixin → wechat` 配置兼容。
- [x] 扫码获取/轮询接 G3，保持 scan_url/poll_token/status/credentials 返回形状及确认后的 token/baseurl；取消回收短时进程。旧代码没有自动失效重登，不将它列为已有能力。
- [x] 保留 config token 优于文件、context_tokens、引用、现有消息去重、媒体收发和 `ret=-2` 后本轮停止发送；重复文本与 IPC 重试分别测试。
- [x] 兼容旧默认 `WORKING_DIR/wechat_bot_token` 及同目录 `wechat_context_tokens.json`，显式自定义路径继续有效；新增工作区存储须先确定账号归属，冲突不覆盖或复制到所有工作区。
- [x] 将原单个 `_merge_buffer` 按响应隔离，保留 delay=0/邻接合并、媒体立即发送与主动消息 flush；并发响应不得串文。
- [x] stop 确认 poll 线程、线程 HTTP client、typing 与 merge timer 退出；验证扫码取消、旧 context 主动发送、引用/图片解密和关闭失败，完成 C1–C8。

验收记录（2026-10-15）：C1–C7 模拟功能验收通过；原 94 项直接、19 项继承及 44 项相邻职责已逐项对应，并明确内部结构迁移和严格失败契约，不等同于 157 项重新执行。正式插件的真实 Agent 收发、扫码与工作区凭据保存、媒体/引用、合并顺序及资源清理均有证据；最近 25 项针对性测试通过。宿主旧扫码平台实现和微信旧 Core 四文件已删除，原测试断言/快照保留，13 个入口收集及删除后的静态检查通过。最终 ZIP 的 11 个运行/描述成员与已验证版本逐字一致，C8 仍由公共任务收口。证据：项目 `.superpowers/sdd/2026-10-14-official-channel-pluginization/wechat-core-retirement-test-report.md`、`wechat-core-retirement-report.md`、`wechat-after-core-retirement-test-audit.json`。

### 5.11 Matrix（`matrix`）

**源码/测试：** `matrix/channel.py`；`unit/test_matrix.py`、`contract/test_matrix_contract.py`、`integration/test_matrix_mock.py`。**前置：** G1、G2。

- [x] 搬迁 nio、认证、sync、E2EE、媒体及格式化；完整映射 groups/group_allow_from、history/vision、structured_mentions、streaming 与群共享默认值，保留 raw config 的设备身份入口。
- [x] 保留显式 user/password 优先 access_token、whoami 校验、登录退避和 device_name 派生身份；当前恢复 auth state 使用 `restore_token=False`，不擅自启用缓存 token 自动登录。
- [x] 兼容旧全局 `matrix_sync_token`、`matrix_auth_state.json`、`matrix_crypto_store`；新存储按工作区/账号/设备归属，不覆盖 crypto DB、不把密钥数据库塞进 HostState。
- [x] 区分普通 nio 与 E2EE/OS 原生依赖并提供明确状态。当前缺加密后端会降级 encryption=false；保留对应事实与测试。若改为禁止明文降级，独立提交策略变更、提示和兼容说明，不混入搬迁。
- [x] 保留 catch-up 抑制重放、room keys、SAS/未知验证事件、有界缓存、加密媒体、DM 判定、mention/pill、typing/read receipt 与编辑式 streaming。
- [x] 等待 sync、验证维护、typing 和网络退出；新增旧设备/状态恢复、两工作区隔离、加密失败与编辑 unknown 的真实 Driver 测试，完成 C1–C8。

验收记录（2026-10-15）：C1–C7 模拟功能验收通过。原 102 项职责以保留断言、真实链路、明确契约或静态组合证据逐项对应，不等同于 102 项重跑；新增正式插件/真实 Agent、普通投递失败、群聊共享与隔离、目标及媒体、双工作区和鉴权失败后的更新恢复，共 30 项通过，原 71 项私有及 C6 九项证据复用。发现的普通发送 403 中断和目标回退故障均已修复，公共媒体路径回归也覆盖本渠道。旧 Core 两文件已删除，3 项原辅助测试通过、9 个入口收集通过；最终 ZIP 八个运行/描述成员保持已验证字节。C8 与真实加密对端互操作不在本次验收声明中。证据：项目 `.superpowers/sdd/2026-10-14-official-channel-pluginization/matrix-formal-final-report.md`、`matrix-ledger-correction-freeze.json`、`matrix-core-retirement-report.md`。

### 5.12 iMessage（`imessage`）

**源码/测试：** `imessage/channel.py`；`unit/test_imessage.py`、`contract/test_imessage_contract.py`。**前置：** G1、G2。

- [x] 将只读 SQLite watcher、imsg 命令和出站附件转换移入 worker；保留 db_path/poll_sec/media_dir/max_decoded_size、环境入口及 session/目标。
- [x] 保留启动读取 MAX(ROWID) 不重放、过滤自身/空文本/bot_prefix、chat_rowid/rowid；当前接收仅处理文本，不声称迁移后已支持入站附件。
- [x] imsg 按 macOS 外部资源处理，不写为 Python 依赖；补平台/可执行文件/数据库权限的明确诊断。列表仍可读，不自动安装 brew 或修改权限。
- [x] 保留出站文件/URL/data URL、文件名及严格 base64/大小校验，子进程路径处理不绕过授权。
- [x] 原 `_send_sync` 无 timeout，迁移时补命令归属、取消/期限与子孙回收；中断后可能已发送时返回 unknown。
- [x] 测试使用临时 Messages schema 和受控 imsg，证明不重放、发送、权限错误、watcher/SQLite/命令关闭；不读取用户 chat.db，完成 C1–C8。

验收记录（2026-10-15）：C1–C7 已在模拟平台环境通过；新增 32 项通过，原 37 项业务职责及 19 项继承契约已映射，旧 Core 与测试导入已转接。C8 的公共 wheel/sdist/editable 构建及被动路径观测已完成，完整应用/连接态资源基线仍单独收口，不计作全部完成。证据：`.superpowers/sdd/2026-10-14-official-channel-pluginization/imessage-closeout-report.md`。

### 5.13 OneBot（`onebot`）

**源码/测试：** `onebot/{channel,driver,platform,media}.py`；`unit/test_onebot_channel.py`、`unit/test_onebot_media.py`、`contract/test_onebot_contract.py`、`integration/test_onebot_reverse_ws.py`；旧 `tests/fixtures/channel_isolation/onebot/`。**前置：** G1、G2、G4 端点部分。

- [x] 复用 platform/media 的有效实现，重接当前 v2 Driver；对照原 Channel 完整 `OneBotInboundMedia` 下载链路，不能只搬旧隔离 Driver 导致媒体能力缩水。
- [x] 保留 `/ws`、loopback 默认、外部绑定必须 token、loopback 配置 token 也校验、Bearer/Token 比较及 host/IPv6/DNS 规范化。
- [x] 保留私聊/群聊、share_session、CQ/数组 segments、meta event、引用 get_msg 与真实 QQ user 的 ACL 身份。
- [x] 保留媒体大小/base64 策略、群文件 URL API、MIME/内容校验、原子下载、并发上限、失败占位和出站文件顺序。
- [x] listener/watchdog、echo reader 与 per-session worker 留子进程；引用等待不阻塞 echo；对接 G4 端点代际，API 超时可能成功时返回 unknown。
- [x] stop 先关入口，再结束在途 API/事件及 listener；把旧本地 WS、端口交接、背压、断线和停止失败断言迁到新 worker，完成 C1–C8。

OneBot C1–C7 模拟功能验收完成：真实官方包、宿主 Agent 收发、两工作区、更新回滚及停止恢复已验证；134 条原职责分别对应 117 条证明和 17 条明确契约迁移，另 19 条继承契约通过。旧 Core 五文件已删除，15 个受影响测试入口收集成功；旧 v1 等待 ACK 的 quiesce 语义不声称与新版取消/unknown 等价。C8 仍统一收口。

### 5.14 Azure Bot（`azure_bot` / 插件 ID `azure-bot`）

**源码/测试：** 现有 `plugins/channel/azure_bot/{plugin,channel,auth,constants}.py`、manifest/requirements；目前没有 Azure 专属测试，新增业务测试和统一 process suite。**前置：** G1 的不同 ID/key 与发行策略、G2；共享端点接入依赖 G4。

- [x] 在原插件根增加 descriptor/命名空间实现；保留 `plugins.azure-bot` 与 `channels.azure_bot`，backend 切到 descriptor 后只有一个执行入口；版本从已有 1.0.1 正常递增。
- [x] 从 plugin.py 提取动态字段、顺序、翻译、图标/docs 元数据；不假定 Azure 已有 ChannelConfig 固定字段；manifest max=2.1.0 改为经测试的兼容范围。
- [x] 搬迁 listener、MSAL、JWT/JWKS、activity/媒体；保留 `/api/messages`、读取 body 前鉴权、签名算法/issuer/audience/expiry 校验及租户 token。
- [x] 保留 Teams/Slack 群判定、mention、aadObjectId 授权身份、`azure_<channel>#<conv后10位>` 会话及 invoke/conversationUpdate 回包。
- [x] 保留工作区优先、全局 fallback 的 `azure_bot_refs.json`、per-activity bot ID/serviceUrl、主动发送与附件策略；可能成功的上传不盲目 fallback 重发。
- [x] 确认 refs 保存、HTTP/listener/watchdog 退出；新增 JWT 拒绝、旧 refs 恢复、owner/key、版本升级、缺 SDK 展示与停止失败测试，完成 C1–C8。

验收记录（2026-10-15）：C1–C7 模拟功能验收通过。正式 ZIP 经真实 Loader、子进程、Agent 和会话落盘完成收发，固定 AAD issuer 兼容、媒体、旧目标恢复、双工作区及更新保留均有对应证据。五个旧根目录实现已删除；原十项 UI 字段保存为独立测试基线，字段断言通过，退休后受影响入口收集通过。最终 ZIP 的九个运行/描述成员与已验证版本逐字一致。C8 连接态资源对照及最终发行构建仍由公共任务收口。证据：项目 `.superpowers/sdd/2026-10-14-official-channel-pluginization/azure-formal-final-test-report.md`、`azure-core-retirement-resources-freeze.json`、`legacy-registration-portability-report.md`。

### 5.15 Twilio Voice（`voice`）

**源码/测试：** `voice/`、`app/routers/voice.py`；`unit/test_voice.py`、`contract/test_voice_contract.py`、`integration/test_voice.py`、`integration/test_voice_webhooks.py`；旧 `tests/fixtures/channel_isolation/voice/`。**前置：** G1、G2、G4 的端点与通话接入。

- [x] 复用 runner-safe platform/webhook 事务，重接 v2；旧 ConversationRelay 直接 `_process` 改标准宿主入站/出站，不能把 Agent 搬入 worker。
- [x] 保留根路径 `/voice/incoming`、`/voice/ws`、`/voice/status-callback`；宿主路由不再导入 Twilio SDK、TwiML/relay 实现或直接访问旧 Channel 私有对象。
- [x] 保留公开 URL/query 验签、单次 WS token、setup/prompt/interrupt/dtmf/终态回调、welcome 与 TTS/STT/language；旧协议里有效的 token/代际保护纳入新链路。
- [x] 建立 call→轮次→response_handle，保留 `voice:<callSid>` 及电话号码授权身份；流式 token/last 不能因复用旧 platform 的单条 final 发送而退化。
- [x] 复用 webhook snapshot/apply/归属校验回滚与 Cloudflare 隧道清理；区分启动失败回滚与正常停止，不覆盖用户后来改写的 webhook。当前无外呼 API，不新增此交付项。
- [x] 用真实宿主链路模拟两通话、签名/token、流式结束、断线与停止失败；真实 Agent 中断若实现，单独证明只影响当前轮次。旧无配置 webhook 测试不代表完整通话通过，完成 C1–C8。

Voice C1–C7 模拟功能验收完成：正式包经真实路由、worker、Agent 与会话收发；双号码/工作区隔离、同号码预先拒绝、提交前候选资源回滚已有实际证据。未配置时 HTTP 200/XML 与回调 204 的回归已修复并通过七项定向验证。85 条原职责完整对应，新旧接口差异单独说明；九个旧 Core 文件已删除，10 个入口可收集。未执行真实电话、桌面或 Docker，C8 统一收口。

### 5.16 SIP（`sip`）

**源码/测试：** 整个 `sip/`；`unit/test_sip_helpers.py`、`unit/test_sip_memory_bounds.py`、`contract/test_sip_contract.py`。**前置：** G1、G2、G4 通话接入。

- [x] 迁移 backend、registrar、RTP、ASR/TTS，保留 dev/LiveKit 双后端及按 backend 延迟导入；不为每个后端另写进程管理器。
- [x] 保留 sip/sip-livekit 显式依赖入口，声明 pyVoIP、dashscope/realtime、livekit/api、Python 3.13 的 audioop-lts；未选择后端不导入，启动不安装。
- [x] 保留 SIP/RTP、外部认证、loopback registrar、room/trunk/reconnect、welcome、超时与并发上限；多实例端口冲突明确失败，不偷偷共享无归属的 registrar。
- [x] `_on_transcript` 改标准入站；保持 `sip:<call_id>` / from_uri 身份。音频队列、采样转换、ASR/TTS 留 worker，IPC 只传文本和控制。
- [x] 原 speech-start 停 TTS 行为保留；真实宿主取消属 G4 增强。无论是否增加取消，挂断或换代后的迟到输出不能播放给其他通话。
- [x] 两后端均新增模拟 process suite，验证背压/队列界限、采样转换、并发、挂断、backend/reader/STT/TTS/registrar 的退出；现有 helper 测试不能代替此闭环，完成 C1–C8。

验收记录（2026-10-15）：C1–C7 模拟功能验收通过，原 70 项职责已对应证据；新增正式 ZIP 音频→真实 Agent/会话→语音回复、19 项公共契约、两后端双工作区/更新恢复及本地 UDP 注册服务，共 24 项通过。旧 Core 十文件已删除，9 个转接入口收集通过，原断言保留；最终 ZIP 仅 README 改变，18 个运行/描述成员与已验证版本逐字一致。不包含真实电话、LiveKit 服务或桌面环境验收；C8 由公共任务继续收口。证据：项目 `.superpowers/sdd/2026-10-14-official-channel-pluginization/sip-closeout-report.md`、`sip-entry-retarget-report.md`。

### 5.17 Console 保留清单（`console`）

- [x] 保持 `_REQUIRED_CHANNEL_KEYS` 中的 Console，`builtin + in_process`，不加入外部插件所有权表。
- [x] 无渠道插件、SDK 不全或全部外部连接失败时，最小聊天、会话和媒体上传仍可用。
- [x] 回归 streaming、stdout 关闭及错误展示；外部渠道慢连接不阻塞已有后台启动路径，停用一个渠道不误停 Console。
- [x] 缺失/停用的外部渠道配置仍可编辑；删除旧实现后，Core 的配置 reader、通用 renderer/协议/队列不能误删。

### 5.18 飞书、钉钉公共回归（不重新迁移）

- [x] G1 变更后，旧安装、channels-full 预置、显式卸载墓碑、缺包修复及桌面资源校验仍成立。
- [x] G2/G3/G4 变更后，真实 worker 收发、卡片/流式/审批、代际隔离、工作区启停和更新失败恢复仍通过。
- [x] 公共契约支持新渠道，不要求修改飞书/钉钉已有 ID、包名或用户状态；不得为推广插件化重新引入宿主 SDK 导入。

## 6. 分工、交付顺序与收口

| 分工 | 负责内容 | 不重复实现的内容 |
|---|---|---|
| 公共框架负责人 | G1–G4，公共类型/契约、目录/发行与迁移记录、接入失败处理；统一合入公共文件改动 | 不替每个渠道重新写业务 SDK |
| 各渠道负责人 | 自己插件目录、配置/依赖清单、原功能对照、状态兼容与停止证据 | 不自建 Loader、安装器、子进程框架、全局事务或另改协议 |
| 独立测试负责人 | 原测试迁移对照、真实 worker + 平台边界 fake、共同门禁及差异场景 | 不通过放宽断言来配合实现 |

**按依赖推进，不按渠道排队。** 不同渠道的私有实现、描述、依赖清单和测试可以并行；不需要等一个渠道全部验收，才启动下一个渠道。只有使用同一公共接缝的接入步骤，以及改动同一共享文件的合入窗口需要协调。

| 可并行的工作 | 真正的前置条件 |
|---|---|
| QQ、WeCom、Yuanbao、XiaoYi、Matrix、iMessage 的私有包与普通收发迁移 | 复用已冻结的 G1/G2 契约；各自缺口单独提交，不等待微信整体完成 |
| QQ、WeCom 的平台扫码算法、业务测试 | 最终扫码接入和验收依赖 G3；其他收发与生命周期工作照常推进 |
| OneBot、Azure 的监听和平台实现 | 公共端点接缝由 G4 统一维护；不等待 Voice/SIP 完成 |
| Voice、SIP 的平台、ASR/TTS 和媒体整理 | 宿主轮次、路由和控制接入依赖相应 G4 契约；不阻塞普通渠道 |

执行时按文件分工：每个渠道负责人只改自己的插件和经确认的旧实现迁移范围；公共协议、代理、注册表、配置兼容与打包文件由指定负责人单独修改，渠道负责人提交接入需求，不能同时各改一份。公共文件修改前协调正在运行的相关测试，避免混用前后两个版本的结果。

本轮按上述方式并行完成全部 18 个外部渠道的插件化、旧平台实现删除及模拟验收。功能代码与测试由不同作者负责，公共故障先修复，再做定向回归；未按渠道重复执行全仓测试。最终发行、资源测量和 checklist 证据已收口，具体范围及限制见 6.3。

1. **公共阻塞优先。** 影响多个渠道的协议、生命周期、旧配置或发行缺口先归入 G1–G4，由公共负责人修复；仅影响单渠道的问题留在该插件内。
2. **先验收元宝与 iMessage。** 复用已有业务测试，补正式 ZIP 经 Loader、worker、真实 Agent 的闭环及生命周期证据。发现故障先修复再验收，不能把“没有已知故障”当作 C1–C8 已完成。
3. **随后按剩余工作量收口。** 优先处理只差少量验证、测试入口转接或删旧的渠道，再处理仍有业务、数据迁移或连接故障的渠道。每轮根据实际缺口调整，不固定逐渠道串行开发；公共发行验证可被已就绪渠道共同复用。
4. **逐渠道删旧。** 业务职责有替代证据、旧导入已转接且无生产调用者后，就删除对应旧实现，不等其他渠道全部完成。Voice/SIP 等较多状态和通话控制工作独立推进，仍有效的协议、reader 和测试断言保留。

### 6.1 每个渠道的提交与验收顺序

每个渠道以“**一个可安装的正式插件 + 原业务保真 + 生命周期闭环 + 发行和旧数据兼容**”为最小交付单元。准备阶段可拆 PR，但未验收前继续保留旧 builtin；正式切换的 PR 同时包含以下改动：

| 落点 | 统一动作 |
|---|---|
| `plugins/channel/<source_dir>/` | 描述、依赖、实现、资源、README；业务实现只放自己的命名空间 |
| `plugins/official_channel_catalog.py` / `app/channels/registry.py` | 增加官方记录并撤旧 builtin；Azure 只撤旧 backend 入口，不伪造 builtin |
| `pyproject.toml` / 打包资源 | 专属依赖退出默认安装，完整预设和插件依赖保持一致，实际 ZIP 可安装 |
| `config/channel_env.py` / 旧状态 reader | 有旧入口才增加兼容映射；读取旧值和文件，不静默换默认值或搬用户数据 |
| 本渠道测试 / 公共门禁 | 保留原能力断言，补实际 worker 与失败路径；公共变更回归既有渠道 |

如需修改公共协议、代理或 Loader，应先归入 G1–G4 的独立公共改动，再被渠道复用；渠道实现不能私自增加一种进程模式或一组宿主平台分支。

- [x] 独立测试作者提交“旧能力→新测试”对应表，并新增失败用例；确认失败来自尚未实现的迁移要求，而非测试环境污染。
- [x] 实现者完成自身插件和必要的公共接入；生产代码与测试代码由不同子 agent 编写，公共缺口交给公共负责人处理。
- [x] 在受控外部 SDK/网络边界下运行真实 proxy、worker、Driver、Loader/投影和更新事务；不以 mock 整个 Driver 代替验证。
- [x] 审查被动路径无 SDK 导入、旧状态/会话保真、停用退出、更新失败恢复；检查打包后的 ZIP 能实际安装，不能只从源码目录运行。
- [x] 原业务测试、新 process suite 与公共门禁通过后，移除旧注册和仅供旧实现的依赖；搜索 CLI/doctor/router/test 中的旧 import，避免删除目录造成悬空引用。
- [x] 定向检查受影响的新旧测试入口能收集，并按变更范围回归；逐文件核对并交付改动与证据，避免纳入其他人的无关文档/研究产物；本轮不执行 commit/push。

### 6.2 验证命令与证据

按用户最新要求，只运行改动影响的测试，不按渠道或阶段例行执行全仓测试、全仓收集或全渠道矩阵。

- **渠道私有改动：** 选择对应函数、业务路径及失败场景的用例；不回归其他渠道。
- **公共改动：** 选择对应协议、代理、Loader 或进程测试，补最少必要的代表渠道回归；不默认运行整个公共测试目录。
- **复用执行证据：** 独立测试作者主跑，源码作者只做必要的最小诊断；协调者、审查者复用与当前代码对应的结果。没有新改动、失败或具体疑虑，不重复已通过的测试。正常 wrapper 与对应隔离内层选一个有效执行路径，避免固定双跑。
- **收集与最终验收：** 删除旧代码后，先搜索旧引用并定向收集受影响入口；仅在具体收集问题确需排查时扩大范围。最终按本次全部改动的受影响测试集合去重验收，不强制执行无关的全仓测试。

使用现有测试环境与隔离方式；下面是按改动择用的示例，不是一组每轮全部执行的命令。

```bash
# 渠道局部修复：只选对应责任组或其中的具体用例
python -m pytest 'tests/unit/plugins/channels/test_telegram_plugin.py::test_telegram_plugin[telegram_fix1_suite.py]' -q

# 公共契约变更：从相应测试中选择受影响项
python -m pytest tests/unit/plugins/channels/test_channel_extension_contract.py -q

# 描述与保留配置模型的一致性；不导入渠道 SDK
python scripts/pack/validate_channel_contracts.py --project-root .

# 新旧入口调整：只检查受影响入口能收集
python -m pytest tests/unit/plugins/channels/test_telegram_plugin.py --collect-only -q
```

新增 process wrapper 复用现有 `-I`、`--confcutdir` 隔离方式，避免仓库级 SDK mock 掩盖父进程偷导入或子进程缺依赖。原 unit/contract 测试按对应职责转接，不能简单删除后只保留新冒烟测试。

构建沿用 `scripts/pack/generate_plugin_metadata.py` 与既有 package/frozen-runtime 契约测试；模拟不同平台资源清单和缺包状态即可，不要求本轮安装桌面版、Docker、真实机器人或拨打电话。实际 OS、音频设备和平台互通的未验证项要如实记录，不用 skip/放宽断言伪装成已验证。

每个渠道 PR 必须交付：插件产物、原能力/旧文件对应表、C1–C8 结果、自己小节的 checklist、安装/启停/更新模拟命令与结果、依赖变化。公共负责人统一记录相同环境下的未启用/启用前后资源数据；各渠道补充自己的常驻进程、线程和大依赖变化，不要求每人重复搭整套性能工具。

### 6.3 最终验收记录（2026-10-16）

全部 **18 个外部官方渠道**已完成插件迁移、模拟功能验收和旧平台实现删除；Console 保留 Core。最终结论由各阶段受影响测试的去重证据组成，不代表执行过一次全仓测试。此前小节中的“C8 待完成”等记录属于阶段状态，以本节为准。

| 范围 | 已完成的验证 |
|---|---|
| 渠道功能 | 各渠道保留原能力对应表，验证正式 ZIP、真实 Loader/代理/worker、业务与生命周期；具体测试层级和平台边界替身见各渠道报告 |
| 公共机制 | G1–G4 的身份与所有权、旧配置、消息/回复、扫码、端点、关闭失败和更新恢复；最后修复了多工作区启动时提前创建进程渠道租约的问题，未放宽未知依赖审计 |
| Console | 真实应用聊天、会话与 SSE；补验真实上传、媒体链接处理、stdout EIO/BrokenPipe 和错误展示共 5 项；慢渠道不阻塞 Console 的已有证据复用 |
| 最终发行 | 当前源码实际构建 wheel、sdist、editable；sdist 重建 wheel 的 1,474 个成员含 RECORD 逐字节一致；18 包及索引与源码相符；隔离发行包读取 18 个描述、19 项目录，无平台 SDK、私有实现或旧渠道导入 |
| 资源 | 18 渠道各有版本和测试层级明确的定量记录；Console 与 iMessage 另有完整应用的新旧对照，按父进程、worker 及合计分别记录 |

**减负结果及代价：** Console-only 完整应用五组对照的空闲 RSS 中位数约 **288 → 258 MiB**，平台模块 **488 → 0**。iMessage 双工作区三组对照的主进程 RSS 约 **286 → 261 MiB**，但合计 RSS **286 → 371 MiB**、线程 **6 → 14**、就绪时间 **2.44 → 4.81 秒**。多进程降低宿主导入负担并提供按需启停、故障隔离，同时增加启用渠道后的总开销，不能宣传为整体更省内存或启动更快。RSS 合计会重复计算共享页，样本和缓存条件也不支持统计显著性结论。

默认直接依赖声明 **55 → 48**，不等于完整传递依赖或下载量减少相同比例。最终 wheel 为 **11,765,869 字节**（原基线 11,642,426），sdist 为 **11,133,543 字节**（原基线 10,655,313）；仍内置 18 个私有源码包及可重建输入，安装包体积未降低。

**验收范围：** 使用模拟平台、网络或模型服务边界；没有真实机器人、电话、音频设备及 Windows/macOS 安装包、Docker 镜像的实机验收。桌面/Docker 的资源与构建入口已有静态及模拟 staging 验证。18 个资源记录不是等价的全应用活跃通道基准：Voice 是就绪无通话并使用已选择的运行环境，不声称本次验证了与宿主同解释器；飞书/钉钉资源载体使用受控生成器，Discord 的历史资源载体使用 AgentStub。完整应用与真实 Agent 的代表性结果单独列出，不混算、不作跨渠道排名。用户后续实机验收仍需自己的平台配置与运行环境。

完整本地证据位于项目 `.superpowers/sdd/2026-10-14-official-channel-pluginization/`，重点索引：`final-checklist-evidence-review.md`、`final-release-postfix-verification.md`、`c8-resource-observations.md`、`c8-final-root-review.json`。失败原始日志、独立修复回归和源码冻结均保留；未执行 commit 或 push。

## 7. 当前源码依据

- 静态发现与实例构建：`plugins/channel_adapter.py`、`plugins/execution_backend.py`、`plugins/registry.py`。
- 进程、共享依赖与停止保护：`plugins/execution/`、`plugins/ownership.py`、`app/channels/process_supervisor.py`、`app/channels/isolated_proxy.py`。
- 入站/出站、身份、策略与回复资源：`channel_protocol/{models,normalization,targets,response_policy,shared_response_driver}.py`。
- 插件安装、撤销、工作区投影及恢复：`plugins/{loader,lifecycle,workspace_projector}.py`、`app/channels/manager.py`。
- 官方所有权与预置：`plugins/official_channel_catalog.py`、`app/channels/catalog.py`、`plugins/official_channels.py`、`scripts/pack/generate_plugin_metadata.py`；发布模型一致性门禁：`scripts/pack/validate_channel_contracts.py`。
- 旧配置/别名/平台限制：`config/config.py`、`app/channels/conflict.py`、各渠道源码及原 contract/unit/integration 测试。

源码引用均以 `src/qwenpaw/` 为前缀，构建脚本和 tests 除外。历史设计文档仅作参考；其中独立环境、v1 Driver、已经过时的未接入说明不覆盖当前代码事实。
