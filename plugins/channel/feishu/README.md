# 飞书渠道插件

已完成步骤一注册/配置、步骤二消息链路和步骤三实例生命周期。
`publish: false`；内置飞书仍保留，本包尚未接管正式注册表。
正式切换和最终集成仍待完成。

## 进程内候选基线（以下旧入口说明）

## 入口与边界

- `plugin.py` 注册现有 `feishu` 渠道键；配置使用宿主 `FeishuConfig`。
- `channel.py` 是 BaseChannel 适配层，保留公开发送、请求转换、流式和健康检查接口。
- `platform.py` 提供本包的 `LarkOapiPlatform`，负责 SDK、媒体、卡片和平台发送。
- `constants.py`、`utils.py`、`card_templates.py` 复用平台需要的纯辅助逻辑。
- `requirements.txt` 是平台依赖声明来源，由宿主安装，插件不运行 pip/uv。

导入入口、注册和 `from_config` 不导入 SDK、不建立连接、不创建目录。
`start()` 才导入本包平台实现，并调用 `prepare(config, secret, media_work_dir)`、
`connect(on_message, on_card)`。未启动时发送明确失败；`stop()` 关闭实例的平台资源。
不导入旧 `qwenpaw.app.channels.feishu`，不包含 isolation Runner、跨进程协议或状态存储。
访问控制、队列、会话及 Agent 执行继续由宿主管理。

## 消息兼容

支持私聊/群聊、@ 判断、去重、引用、文本和图片/文件/音视频，
普通和流式卡片、审批回调、主动发送与原话题回复。
审批回调保留原请求会话/话题和回复目标，将实际点击者同时作为 `user_id` 和
`acl_sender_id` 交给宿主授权链。平台仅中性确认收到操作，不提前替换审批卡片；
真实处理结果由宿主回复。
手输审批命令及宿主注册到审批处理器的别名也使用实际操作者；缺失该身份时拒绝构造请求。
识别复用宿主 control token/registry 与处理器类型，不改变普通群聊/话题的会话映射。

共享群/话题会话使用 `group` / `thread:*` 合成身份：此类请求的
`EXACT_REQUESTER` 审批目前安全拒绝，不能通过冒充合成身份放行。
后续需宿主区分会话身份与操作者身份后再支持；`AGENT` 策略仍由宿主按原规则处理。

回复路由和流式状态保存在每个请求的元数据内。并发完成顺序不改变回复话题；
话题内保持旧行为，使用普通回复而不创建 CardKit 流式卡片。
明确失败的流式创建可回退文本；结果不确定的发送直接暴露错误，不盲目重发。
保留每次发送的 `bot_prefix` 覆盖、拒绝文本、思考折叠面板与可选自动收起。

## 配置与数据

继续使用原 `FeishuConfig` 字段与默认值，包括域名/网关、凭据、访问策略、
显示选项、流式开关及共享群会话。配置按实例复制。
自定义网关原样传给 SDK，仍受 SDK 的域名校验约束。
`from_config` 参数顺序不变，`no_text_debounce` 使用宿主传入值；支持旧环境变量构造。

媒体目录：显式 `media_dir` 优先，否则为工作区 `media/`，无工作区时使用
`DEFAULT_MEDIA_DIR`；路径展开 `~`，仅运行阶段创建目录。
接收地址保存为工作区 `feishu_receive_ids.json`，无工作区时用原全局配置目录。
读取兼容 `[receive_id_type, receive_id]` 与旧反向顺序；保留 `feishu:sw:`、
`feishu:open_id:`、`feishu:chat_id:` 和旧昵称后缀查找。
会话继续使用飞书原本的末 8 位 ID，群聊带 app_id 末 4 位前缀；话题和共享群
用户身份映射与旧渠道一致。停止不删除配置、媒体或接收地址记录。

## 复用与待办

迁入原平台及三个辅助文件，补入旧渠道的思考卡片布局/收起逻辑。
新增适配层负责 BaseChannel 接口和原 JSON/会话兼容，没有复制 Agent 循环。
平台调用保持原接口，仅为审批原会话和思考卡片增加可选参数。

`start()` / `stop()` 由实例锁串行化；重复成功启停不会复制连接。
意外断线由渠道自身监督：先停止旧连接并等待线程退出，再以 1 秒起步、
最多 30 秒的退避重连。SDK 的 `_connect` 方法只在本实例绑定私有 loop，
不修改 SDK 模块全局 loop；原 SDK 的协议、握手和事件实现继续复用。

停止先禁止新事件和重连，等待本实例回调的取消收尾及已发起的发送、同步工作，
再断开 socket、在线程外等待 join，最后关闭所属 HTTP 客户端。
线程、socket、回调或 HTTP 清理失败会抛错并保留资源引用；再次 `start()` 拒绝
替换，修复后可再次 `stop()` 完成清理。调用者取消停止等待时，取消向上传播，
实例保留继续运行的清理任务和结果；同步认证等工作不会因取消 await 被视为退出。
平台回调及渠道回调均绑定连接代际，旧回调不能进入重启/重连后的状态。
不扫描或取消宿主 Agent 任务，也不删除持久数据。

`health_check()` 保留 `status`，补充 `state` 和 `error`，可区分启动、运行、
重连、停止、启动失败和停止失败。测试在独立进程运行实际 SDK 握手与断开逻辑，
只替换网络边界；尚未进行真实飞书机器人联调。

## 验证

```bash
QWENPAW_WORKING_DIR=/tmp/qwenpaw-channel-plugin-tests python -m pytest -q tests/unit/plugins/channels/test_channel_packages.py
```

该入口分进程检查 SDK-free 注册/构造、配置和实际新插件消息行为。
消息测试仅替换 SDK 网络边界，不连接真实机器人，也不调用旧渠道实现。

## 接入兼容表

| 项目 | 候选行为 |
| --- | --- |
| 注册 / 配置 | 原 `feishu` 键、`FeishuConfig` 字段及默认值、环境变量、构造参数顺序 |
| 消息 / 会话 | 原私聊、群聊、话题、共享群映射；主动发送和话题回复目标不变 |
| 文件 | 原媒体目录及 `feishu_receive_ids.json` 路径与旧格式可读 |
| 停用 | 停止本实例资源，保留配置、媒体和接收地址记录 |
| 宿主契约 | BaseChannel、公共工具及暂留配置模型仍由宿主提供 |

| 资源 | 所有者 / 释放方式 |
| --- | --- |
| 渠道实例、队列、Agent、ACL | 宿主 ChannelManager / BaseChannel |
| 私有 loop、线程、socket、重连 | 本渠道平台实例；停止等待任务收尾及线程退出 |
| HTTP 客户端、发送、回调任务 | 本平台实例；停止排空在途工作后关闭 |
| 配置、媒体、接收地址 | 工作区长期数据；stop 不删除 |

## 依赖核对

`requirements.txt` 是唯一安装声明来源，没有在发行清单复制另一份依赖列表。
直接导入 `httpx` → `httpx>=0.27.0`，`lark_oapi` → `lark-oapi>=1.5.3`；
保留原 SDK 最低版本，不新增未经验证的上限。`pkg_resources` 是旧 setuptools API，
现有兼容分支处理缺失，不新增 setuptools 强制依赖。
Lark SDK 的 requests、requests-toolbelt、pycryptodome、websockets 等依赖由 SDK 元数据解析。
真实 SDK 验证环境使用 lark-oapi 1.7.1、httpx 0.28.1，未宣称穷举最低版本组合。

最终切换时 `lark-oapi` 可作为专用默认依赖移除候选；`httpx`、websockets 等仍被宿主
其他能力或 SDK 使用，不根据本插件迁出直接删除。此阶段主包依赖和内置入口仍保留。

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
QWENPAW_WORKING_DIR=/tmp/qwenpaw-channel-plugin-tests python -I -m pytest --confcutdir tests/fixtures/channel_plugins tests/fixtures/channel_plugins/registration_suite.py -q
QWENPAW_WORKING_DIR=/tmp/qwenpaw-channel-plugin-tests python -I -m pytest --confcutdir tests/fixtures/channel_plugins tests/fixtures/channel_plugins/feishu_suite.py -q
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

描述中的 `feishu.driver:FeishuDriver` 已由 B3 实现；显式共享进程入口见下节。
B1 不接入正式 PluginLoader，也不替换当前 `plugin.py` / `channel.py` 路径；
清单仍为 `publish: false`。上述旧入口行为说明仍仅指现有进程内候选实现。


## 共享进程 Feishu Driver / 宿主代理（B3）

`driver.py` 运行在公共 `ChannelProcess` 子进程内，复用本包平台实现；宿主使用
`qwenpaw.app.channels.isolated_proxy.IsolatedChannelProxy(BaseChannel)`，不导入本包
Channel、平台或 SDK。该路径与上面的 `plugin.py/channel.py` 进程内基线分别存在。

构造接口为 `IsolatedChannelProxy(process, *, launch_spec, channel_process=None,
workspace_dir=None, on_reply_sent=None, display_config=None)`；`from_config` 也需要
显式 `launch_spec`。队列、ACL、真实审批操作者、Agent 和 TaskTracker 仍由宿主管理。
完整 LaunchSpec/启停示例见 [公共组件 README](../../../src/qwenpaw/plugins/execution/README.md)。

入口 module 是 `feishu.driver`：可信 `code_root` 应包含 `feishu/` 包，描述位于
`code_root/feishu/channel.json`。不要将包本身作为 code_root 平铺导入，否则
`platform.py` 会与标准库模块冲突。源码摘要覆盖所选制品根；开发时可用
`plugins/channel`，独立制品通常只包含这个 `feishu` 包。

宿主在入队接受后确认事件；子进程使用已有 RetryPolicy/events_for_retry，以同一
事件、批次和响应句柄重试暂时拒收或不确定 ACK，最多五次，退避
0.1/0.2/0.4/0.8 秒。永久拒收不重试；确定未入队的新路由在耗尽后安全清理并移除，后续重新投递可重新打开，
曾有不确定 ACK 的路由保留。停止和连接代际变化中断重试，SDK 快速 ACK 不被当作
会自动重新投递的保证；此阶段不实现跨崩溃持久入站队列。

回复使用冻结的响应句柄，支持原话题、并发流、主动发送和旧 receive_ids 两种顺序。
响应作用域覆盖普通消费、TaskTracker、独立发送/审批以及合并和媒体缓冲的全部句柄。
SDK 副作用前保存发送/收尾意图；未知结果保留证据，不自动重发或发送第二条错误消息。
完成的确定收据按条目和字节预算回收，活动、未知和未完成收尾保留。
未知收尾返回结构化 `RESPONSE_FINISH_FAILED`，恢复时不重放未知操作，连接仍可正常启动。
`channel.diagnostic` 只传身份与固定原因码，宿主记录一条 `last_diagnostic` 和安全日志；
诊断不会把实际连通的平台假报成断开，也不携带凭据、正文或目标。

显式 `workspace_dir` 下宿主使用原子写保存频道/实例有界状态；无此参数仅承诺当前代理
生命周期中的恢复。手动 stop/start 推进代际，未确认退出时保留所属进程。
A 的自动发现/Loader/Manager 接入，以及
E1/E2/E3 的真实平台、跨平台和最低 SDK 版本验收仍待实现；
清单保持 `publish: false`，不声明已完成真实凭据、线上机器人或原生 Windows/Linux 验收。

独立 B3 验收入口：

```bash
QWENPAW_WORKING_DIR=/tmp/qwenpaw-channel-plugin-tests python -I -m pytest --confcutdir tests/fixtures/channel_plugins tests/fixtures/channel_plugins/feishu_process_suite.py -q
```

该套件运行真实宿主代理、公共进程组件、生产 Driver 和平台逻辑，只替换 SDK/网络边界。

## B5：安装目录与 A 交接

将本目录复制到可信安装根的 `feishu/` 下，`code_root` 指向其父目录；
`descriptor_path=code_root / "feishu/channel.json"`。完整 LaunchSpec、共享宿主与
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
