# 渠道共享进程：A 交接

B5 交付现有 `plugins/channel/feishu`、`plugins/channel/dingtalk` 候选目录及安装目录验收。清单保持 `publish: false`；内置渠道和默认依赖仍保留。没有新增 Channel 打包器、wheel、可执行文件或专属依赖环境。资源数据与复现命令见 [测量记录](2026-10-12-channel-process-measurements.md)。

以下是 B 候选布局建议，不表示现有 PluginLoader 已支持版本根。建议每个插件版本使用独立、稳定的代码根，例如 `installed-code/feishu-version/feishu/` 与 `installed-code/dingtalk-version/dingtalk/`。共享依赖环境不要求共享代码根。以飞书为例：

```text
installed-code/feishu-version/   # LaunchSpec.code_root
  feishu/                       # 完整复制的插件目录
    __init__.py, driver.py, platform.py, ...
    plugin.json, channel.json, requirements.txt
trusted-host/src/               # LaunchSpec.support_root
  qwenpaw/channel_protocol/shared_worker.py
  qwenpaw/plugins/execution/...
shared-python/                  # 已安装宿主和 SDK 的选定解释器
workspace/                      # 配置、媒体、路由与恢复记录
```

钉钉按同一布局保留 `dingtalk/` 包及 `driver.py/transport.py/platform.py`。仅受控不可变制品或开发时才考虑共享代码根：摘要覆盖整个根，兄弟插件更新也会改变摘要，需协调重启。

包名与当前插件 ID 一致；`code_root` 必须包含 `feishu/` 或 `dingtalk/`，不能直接指向包内目录，否则平铺 `platform.py` 会遮蔽标准库。`support_root` 是可信宿主公共代码根，不是原渠道源码回退。共享依赖来自 `python_executable` 的安装环境，或绝对可信 `dependency_paths`；显式路径不执行 `.pth`。`requirements.txt` 是唯一依赖声明来源，启动只检查已安装分发元数据，不安装包。

显式 `LaunchSpec` 使用绝对的 `python_executable/support_root/code_root/descriptor_path`；描述路径为 `code_root/<channel>/channel.json`。`source_revision` 是所选完整代码根的摘要，另传 `channel_key`、`instance_id`、`generation`、`qwenpaw_version`、实例配置及启停预算。入口由描述确定为 `feishu.driver:FeishuDriver` 或 `dingtalk.driver:DingTalkDriver`。宿主创建 `IsolatedChannelProxy(process, launch_spec=spec, workspace_dir=workspace)`，设置入队回调，再显式 `start/send/stop`；完整示例见 [公共组件 README](../../../src/qwenpaw/plugins/execution/README.md)。

父进程静态读取 `channel.json` 的 schema、配置字段和共享依赖契约；不要执行 `plugin.json` 旧进程内入口的 `register()` 或平台 probe。子进程核对摘要、依赖和 hello，再导入生产 Driver、平台与 SDK。宿主持有队列、ACL、真实审批操作者、Agent 和 TaskTracker。安装验收检查实际模块文件来源与主/子 PID，禁止回退到原插件或内置渠道源码。

进程握手就绪与平台连接就绪分别呈现；代理 `start()` 等待 `platform_state=connected`。启动失败、超时或崩溃仍应由持有者执行 `stop()`，预算内回收子进程及其进程树。`ChannelProcess.stop()` 返回 `StopResult`；代理在 `exited=False` 时抛出 `CHANNEL_STOP_INCOMPLETE`，保留归属，A 必须映射真实 StopReceipt，禁止覆盖旧句柄或重叠启动。手动 stop/start 推进代际。未知 RPC/平台副作用保留意图与收据，不自动重发；进程退出不等于回复已完成。

停止保留宿主配置、工作区媒体、旧接收地址/Webhook/卡片信息，以及 `channel_state/` 中的入站去重、路由和未知/未完成回复记录。显式 `workspace_dir` 才提供跨代理生命周期的宿主恢复；没有工作区时不保证父进程重启恢复。

验收采用复制安装目录、真实 SDK/代理/ChannelProcess/生产 Driver，外部网络由测试替身提供；慢 SDK 导入、导入失败与运行期崩溃注入均属于测试边界。冷导入测量与受控网络连接就绪测量分开，记录当前 RSS、父/子/相加及停止后状态。相加不是物理 RAM，结果不能推出正式 Console 启动收益，也不是正式安装/更新/回滚或线上机器人验收。

A0–A6 仍需完成事务/依赖归属拆分、Loader/Registry 静态发现及同名键唯一归属、WorkspaceProjector/Manager 生命周期、核心与渠道就绪分离、安装更新回滚、共享 DependencyGate、旧入口/默认依赖/桌面发行切换。E1–E3 仍需真实机器人、原生平台与 frozen 入口/回收、最低 SDK 和共享依赖冲突组合验证。详细验收条件保留在 [原计划](2026-10-10-channel-shared-process-b.md)；B5 不关闭这些 TODO。

A4/A6 安装接入需确定每插件不可变代码根及导入布局。当前 Loader 使用平铺 `<plugin_dirs>/<id>`，直接把其父目录作为代码根会摘要整个可变兄弟插件目录；应避免这种归属。B5 只验收复制后的候选布局，不重构 Loader。
