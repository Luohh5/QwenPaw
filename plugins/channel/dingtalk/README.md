# 钉钉渠道插件

步骤四的独立插件实现；`publish: false`。渠道键仍为 `dingtalk`，
内置入口和主包依赖保持不变，正式宿主切换属于后续工作。

## 入口与职责

- `plugin.py` 导出 `plugin = DingTalkChannelPlugin()`，使用现有注册 API。
- `channel.py` 继承宿主 BaseChannel，保留配置、会话、Webhook 路由、内容组合及流式钩子。
- `platform.py` 为同一渠道实例提供 SDK 请求、媒体传输、连接、健康检查和资源清理。
- `sdk.py` 集中运行期 SDK 依赖、长类型别名及实例级 Stream HTTP 适配。
- `handler.py`、`ai_card.py`、`markdown.py`、`content_utils.py`、`constants.py`、`utils.py`
  迁入原渠道的处理器和辅助逻辑，不导入旧内置钉钉模块。

入口导入、注册和 `from_config` 不导入平台 SDK、不连接、不创建媒体目录。
仅 `start()` 导入 SDK 和继承 SDK 类的 handler。
`requirements.txt` 为依赖声明来源，安装交给宿主，插件不执行 pip/uv。
消息排队、Agent 执行、访问控制及公开渠道契约继续使用宿主 BaseChannel。

## 配置与数据兼容

保留 `DingTalkConfig` 字段、默认值、环境变量构造及宿主参数顺序。
只有 `message_type=card` 时启用流式回复。配置中的可变名单按实例复制。
显式 `media_dir` 优先，其次工作区 `media/`，最后原 `DEFAULT_MEDIA_DIR`。

仍读取工作区（无工作区则原配置目录）的 `dingtalk_session_webhooks.json`
和 `dingtalk-active-cards.json`。支持旧字符串 Webhook 记录、新对象记录、
直接 URL、`dingtalk:webhook:`、`dingtalk:sw:` 及原私聊/群聊会话映射。
共享群会话保持完整会话 ID 的散列隔离。停止不删除配置、路由、媒体或长期数据。
旧待恢复卡片使用原卡片 ID 收尾；停止收尾失败的卡片保留为待恢复记录。

## 连接与停止

`start()` / `stop()` 都是 `async -> None`，由实例锁串行化。
与原钉钉渠道一致，`start()` 完成本地资源初始化后返回，首次网络连接异步进行。
此时 `health_check()` 返回 `connecting` 且非健康；确认 socket 存在并通过 Ping
后才健康。SDK 自带循环处理断线重连，状态为 `reconnecting`。
网关错误公开安全的异常类型和 HTTP 状态，不在健康结果中公开凭据。
宿主正式接入时应使用健康检查判断连通性，不能把 `start()` 返回当作握手成功。

Stream 仍使用真实 SDK 的 start、帧路由、ACK 和重连实现。
SDK 的 gateway/token HTTP 方法在本实例子类覆盖，保留请求协议，
设置 `(10, 30)` 秒请求超时和实例 endpoint；不修改 SDK 模块常量或全局 requests。
阿里云客户端各自使用实例配置和连接/读取超时。HTTP Session 也按实例创建。
SDK 会吞掉一次取消，因此私有线程的停止观察器持续取消主任务直到退出。
线程内只清理该线程私有 loop 的任务，不扫描宿主 Agent 任务。

停止立即阻止新事件和新平台请求；在线程外等待 join，并确认线程已退出。
然后等待消息合并定时器、已发起的请求，收尾 AI Card，关闭 HTTP Session。
线程仍活跃、请求未完成或 HTTP 关闭失败时，停止抛错并保留资源引用；
重新启动拒绝覆盖未清理实例，可以再次停止完成清理。
调用者取消等待不会被视为网络工作结束；实例保留受保护的清理/请求任务。
重复成功启停不会创建重复连接。`health_check()` 保留原 `status` 并增加 `state/error`。

## 有意修正的行为

- Webhook、媒体或卡片发送超时、连接中断，以及 Webhook 响应截断、空响应、
  非法 JSON 或无法确认的 errcode 属于结果不确定，直接暴露错误，
  不自动换渠道重发；识别真实 Tea 包装异常和可选的新 Dara 网络异常，
  保持旧 Tea/OpenAPI 依赖兼容。初始卡片创建超时保存在本次请求中，后续流式钩子不再建卡。
  明确的 Webhook API 拒绝仍可按原行为回退 Open API。
- SDK 回调保留协议 ACK，移除额外发送空格的 Webhook 请求。
- 手输审批命令及其宿主注册别名使用完整、实际操作者身份；缺失身份时拒绝构造请求。
  普通消息仍保持旧昵称后缀/共享群身份及会话路由。
  因旧普通会话身份与完整操作者不同，`EXACT_REQUESTER` 审批可能安全拒绝；
  后续宿主区分会话身份和操作者身份前，不冒充昵称或 `group` 放行。

## 验证范围

独立进程验证无 SDK 注册/构造；运行期测试保留真实 Stream SDK、线程、
事件循环、aiohttp 会话和阿里云请求模型，仅替换外部网络边界。
不使用真实机器人、凭据或真实网络。宿主正式切换与真实机器人联调尚未执行。

```bash
QWENPAW_WORKING_DIR=/tmp/qwenpaw-channel-plugin-tests python -m pytest -q tests/unit/plugins/channels/test_channel_packages.py
```

## 接入兼容表

| 项目 | 候选行为 |
| --- | --- |
| 注册 / 配置 | 原 `dingtalk` 键、`DingTalkConfig` 字段及默认值、环境变量、构造参数顺序 |
| 消息 / 会话 | 原私聊、群聊、共享群隔离和 Webhook / Open API 路由；card 模式流式回复 |
| 文件 | 原媒体目录、`dingtalk_session_webhooks.json`、`dingtalk-active-cards.json` 及旧记录 |
| 停用 | 只停止本实例资源，保留配置、媒体、路由和未完成卡片恢复记录 |
| 宿主契约 | BaseChannel、公共工具及暂留配置模型仍由宿主提供 |

| 资源 | 所有者 / 释放方式 |
| --- | --- |
| 渠道实例、队列、Agent、ACL | 宿主 ChannelManager / BaseChannel |
| Stream 线程、私有 loop、socket、重连 | 本平台实例；取消主任务并确认线程退出 |
| HTTP Session、阿里云客户端、在途请求 | 本平台实例；排空请求后关闭 HTTP 资源 |
| 合并定时器、AI Card 收尾 | 本渠道实例；停止等待收尾，失败保留恢复记录 |
| 配置、媒体、Webhook、卡片记录 | 工作区长期数据；stop 不删除 |

## 依赖核对

`requirements.txt` 是唯一安装声明来源，没有在发行清单复制另一份依赖列表。

| 导入模块 | 发行包 / 约束 | 说明 |
| --- | --- | --- |
| `aiohttp`、`requests`、`certifi` | `aiohttp>=3.9.0`、`requests`、`certifi` | 直接导入；Stream SDK 另约束 aiohttp>=3.10.0、requests>=2.27.1 |
| `dingtalk_stream` | `dingtalk-stream>=0.24.3` | 保留原 SDK 最低版本 |
| `alibabacloud_dingtalk` | `alibabacloud-dingtalk>=2.2.42` | 保留原 SDK 最低版本 |
| `alibabacloud_tea_openapi` | `alibabacloud-tea-openapi>=0.4.4` | 保留原约束与旧 Tea 栈兼容 |
| `alibabacloud_tea_util` | `alibabacloud-tea-util>=0.3.0` | 直接导入 models |
| `Tea.exceptions` | `alibabacloud-tea` | 直接导入，显式声明，不另造最低版本 |
| `alibabacloud_credentials` | `alibabacloud-credentials>=0.3.0` | SDK 传递依赖，保留原约束 |
| `darabonba.exceptions` | `darabonba-core` | 可选 RetryError 支持；新 OpenAPI 栈传递声明，旧 Tea 栈无需强制安装 |
| `PIL.Image` | `pillow>=10.0.0` | 原宿主约束；生成视频封面，实现中缺失时降级 |

SDK 的 endpoint/gateway/openapi 工具、websockets 等传递依赖由其发行元数据解析。
真实 SDK 验证环境为 dingtalk-stream 0.24.3、alibabacloud-dingtalk 2.2.53、
tea-openapi 0.4.5、tea-util 0.3.14、Tea 0.4.3、darabonba-core 1.0.8；
这些是已测试版本，不是新增最低版本，也不代表穷举全部允许组合。

最终切换时 `dingtalk-stream`、`alibabacloud-dingtalk` 可作为专用默认依赖移除候选。
`tea-openapi`、`credentials`、`tea-util` 仍由 `qwenpaw.market.providers.aliyun` 使用；
Pillow 也被宿主头像、图片及视觉处理使用，aiohttp、requests/certifi、websockets
服务其他能力或传递依赖，不能因迁出钉钉直接从主包删除。
此阶段主包依赖和内置入口仍保留。

## 候选包与接入清单

版本 `0.1.0.dev0`，声明宿主最低版本 `2.2.2b4`；这不是已正式接入所有该版本宿主的保证。
源清单保持 `publish: false`，渠道键不变。现有打包器支持 `channel` 分类，
公开扫描仍跳过本候选。交付以本插件目录为主；如需私下验证 ZIP，可复用
`scripts/pack/generate_plugin_metadata.py` 的 `_zip_plugin`，解压后仍保留包目录。
不要求专用打包器、wheel、可执行文件或每渠道依赖环境。
压缩包不包含 SDK 安装文件，包大小不能代表实际安装体积或运行内存节省。

交给 A / 最终宿主接入时逐项完成：

- 安装由宿主读取 `requirements.txt` 完成；确认现有注册 API 能加载同名渠道，
  正式切换时处理旧内置入口冲突，保留 Console。
- ChannelManager 持有渠道实例，统一调用 `start/stop` 并处理 StopReceipt；
  渠道管理内部连接和任务，同一连接不再交给全局清理器重复管理。
- 核对原配置、工作区、媒体和路由数据迁移前后可读；停用保留长期数据。
- 先跑分层自动化验收，再使用提供的测试机器人验证真实收发、断线重连和停用。
  当前没有提供测试机器人凭据，真实平台联调仍待验证。
- 最终切换时审计专用与共享依赖，随后才移除重复内置入口和专用默认依赖。

自动化入口分别在独立进程验证无平台 SDK 注册、真实 SDK 导入兼容、模拟网络消息行为，
以及解压包加载。解压包测试禁止两种旧内置平台实现，允许稳定宿主 BaseChannel、
配置和公共工具；测试只替换外部 SDK / 网络边界，缺少声明的测试依赖直接失败。

```bash
QWENPAW_WORKING_DIR=/tmp/qwenpaw-channel-plugin-tests python -m pytest -q tests/unit/plugins/channels/test_channel_packages.py
QWENPAW_WORKING_DIR=/tmp/qwenpaw-channel-plugin-tests python -m pytest -q tests/unit/plugins/test_generate_plugin_metadata.py tests/unit/pack/test_generate_plugin_metadata.py
```

该包装入口隔离运行 `tests/fixtures/channel_plugins/` 中的注册、运行期消息、
契约、生命周期及 `packaging_suite.py`。也可只运行对应分层：

```bash
QWENPAW_WORKING_DIR=/tmp/qwenpaw-channel-plugin-tests python -I -m pytest --confcutdir tests/fixtures/channel_plugins tests/fixtures/channel_plugins/dingtalk_registration_suite.py -q
QWENPAW_WORKING_DIR=/tmp/qwenpaw-channel-plugin-tests python -I -m pytest --confcutdir tests/fixtures/channel_plugins tests/fixtures/channel_plugins/dingtalk_suite.py -q
QWENPAW_WORKING_DIR=/tmp/qwenpaw-channel-plugin-tests python -I -m pytest --confcutdir tests/fixtures/channel_plugins tests/fixtures/channel_plugins/packaging_suite.py -q
```

## 共享进程候选契约（B1）

`channel.json` 是纯数据的 schema v2 候选描述，显式声明
`process_mode=runner_process`、`dependency_mode=shared`。静态发现只读取 JSON，
不导入配置模型、渠道类、平台模块或 SDK。完整 `config_schema` 包含继承的显示、
访问策略和名单字段；开发/打包时运行以下命令检查与现有配置的一致性：

```bash
python scripts/channel_isolation/sync_shared_channel_descriptors.py --check
```

去掉 `--check` 可重新生成。只有该开发命令导入配置模型；依赖仍只声明在
`requirements.txt`。共享环境审计只读取发行包元数据，报告缺包或版本不满足，
不会导入 SDK、连接平台或自动安装。

共享 hello 使用 protocol v2，携带代码摘要、实例与代际、实际解释器 ABI、平台、
已安装直接依赖版本及子进程 marker environment，不虚构专属环境 ID 或 lock。
复用宿主既有 framing/RPC 与消息 DTO，v1 专属环境协议保持不变。
平台代码和 SDK 属于子进程；宿主负责监督、ACL、队列、会话和 Agent；
跨进程只传协议数据，凭据由受控一次性通道提供。

描述中的 `dingtalk.driver:DingTalkDriver` 已提供共享进程候选入口，使用方式见下节。
B1 不接入正式 PluginLoader，也不替换当前 `plugin.py` / `channel.py` 路径；
清单仍为 `publish: false`。上述旧入口行为说明仍仅指现有进程内候选实现。

## 共享进程 DingTalk Driver / 宿主代理（B4）

宿主显式创建 `IsolatedChannelProxy`，通过 `ChannelProcess` 使用
`qwenpaw.plugins.execution` 的公共进程组件。构造方式与飞书一致，见
[公共组件 README](../../../src/qwenpaw/plugins/execution/README.md)。
`code_root` 包含 `dingtalk/` 包，描述位于 `code_root/dingtalk/channel.json`；
不要把包内的 `platform.py` 平铺到导入根目录。

- `driver.py` 负责入站确认、回复路由及恢复，复用飞书同用的 `SharedResponseDriver`；
  不另建进程管理或通信协议。
- `transport.py` 与原进程内 Channel 共用平台实现，SDK、Stream 线程、Webhook、
  媒体和卡片留在子进程。子进程不构造或导入 BaseChannel。
- 主进程保留队列、ACL、真实审批操作者、Agent 和 TaskTracker；会话短 ID、
  共享群会话及主动发送句柄沿用原规则。入队确认后才向平台确认接收，
  暂时拒收或回执不明使用同一事件重试，不重复执行已接收消息。
- 通过宿主访问检查后，`channel.response.prepare` 通知子进程显示处理反馈并预创建卡片。
  首段流式回复复用该卡片，非流式多段内容合并到同一张卡片，任务结束时统一收尾。
  表情失败不阻止 Agent；明确的预创建拒绝允许继续处理，结果不明时保留记录并停止补发。
- 回复在平台操作前记录意图；卡片部分成功、发送或最终收尾结果不明时保留证据，
  不自动改用另一条消息补发。旧 Webhook 和待恢复卡片继续读取，恢复使用原卡片 ID。
- 停止会回收本实例的线程、连接和子进程，保留工作区数据及未完成回复记录；
  进程已退出与回复已完成分别判断。重启可继续处理已知卡片，未知最终收尾不自动重放。

独立验收使用真实宿主代理、公共进程组件及生产 Driver，仅替换外部 SDK/网络边界：

```bash
QWENPAW_WORKING_DIR=/tmp/qwenpaw-channel-plugin-tests python -I -m pytest --confcutdir tests/fixtures/channel_plugins tests/fixtures/channel_plugins/dingtalk_process_suite.py -q
```

正式发现、Manager 启停、安装更新和内置入口切换仍等待 A 接入；安装目录验收与资源比较
属于 B5。真实机器人、原生 Windows/Linux、桌面打包入口及最低 SDK 组合另行验证。
当前仍保留内置渠道和默认依赖，不据此宣称正式应用的安装体积或启动资源已降低。

## B5：安装目录与 A 交接

将本目录复制到可信安装根的 `dingtalk/` 下，`code_root` 指向其父目录；
`descriptor_path=code_root / "dingtalk/channel.json"`。完整 LaunchSpec、共享宿主与
依赖布局见 [A 交接说明](../../../docs/superpowers/plans/2026-10-12-channel-process-handoff.md)。
`support_root` 提供可信宿主公共代码，SDK 来自选定解释器的共享环境或显式
`dependency_paths`；启动只检查依赖，不安装包。

A 的静态发现应读取 `channel.json`，按 `runner_process + shared` 创建代理；
`plugin.json` 中的 `plugin.py` 是保留的进程内候选入口，不能用于此路径的
宿主注册或探测。平台与 SDK 在子进程导入，安装制品不得回退到原插件源码或旧内置平台。

安装目录验收与测量结果见 [B5 测量记录](../../../docs/superpowers/plans/2026-10-12-channel-process-measurements.md)。
这些是候选边界验收：真实 SDK、代理、进程组件和生产 Driver 配合外部网络替身；
不等同于正式安装/更新/回滚或线上机器人验证。冷导入与受控连接就绪分别测量，
当前 RSS 的父子相加不等于物理内存，也不能推出正式 Console 的启动改善。
