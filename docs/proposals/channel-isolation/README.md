# Channel isolation：当前路线与历史参考

截至 2026-10-15，18 个官方外部渠道已有 `dependency_mode=shared` 的描述式插件，
Core 注册器只保留 Console。源码存在不代表各渠道均已验收；逐项进度见
[统一插件化 checklist](../../superpowers/plans/2026-10-14-official-channel-pluginization.md)。
静态 descriptor 由 `ChannelAdapter` 读取，平台 SDK 在子进程内加载；依赖准入和
变更由公共插件依赖 Gate 管理。新增渠道应参考当前插件契约与实现：

- [公共 PluginProcess 与执行组件](../../../src/qwenpaw/plugins/execution/README.md)
- [ChannelAdapter](../../../src/qwenpaw/plugins/channel_adapter.py)、
  [依赖 Gate](../../../src/qwenpaw/plugins/dependency_gate.py)
- [Generic Channel descriptor](../../../tests/fixtures/channel_plugins/sample_chat/channel.json) 与
  [Driver 骨架](../../../tests/fixtures/channel_plugins/sample_chat/sample_chat/driver.py)
- [飞书插件](../../../plugins/channel/feishu/README.md)与
  [钉钉插件](../../../plugins/channel/dingtalk/README.md)

本目录的 [DESIGN](DESIGN.md)、[实施计划](IMPLEMENTATION_WORK_PLAN.md)、
[Core/Runner 边界](CH-0-001_CORE_RUNNER_BOUNDARY.md)和
[Chat 提示词](CODEX_CHAT_PROMPTS.md)保留为原方案的设计与验收记录。
其旧独立依赖环境和 lease Controller 路线不再是当前接入要求；当前共享协议与回复生命周期继续使用。

本次清理退役以下专用实现及其独占测试：

- `src/qwenpaw/app/channels/env_manager.py`：CH-1-002 独立 venv 选择与验证。
- `src/qwenpaw/app/channels/environment_installer.py`：CH-1-003 独立 venv 安装。
- `tests/unit/channel_isolation/test_ch_1_002.py`、`test_ch_1_003.py`。
- CH-1-001 独立渠道 release/lock/archive 模型、构建器、旧 Core 依赖扫描工具及其
  fixture/测试；当前安装与升级使用公共插件依赖 Gate。`artifacts.py` 继续保留
  `code_root_digest`，供当前 ChannelAdapter 与管理操作验证源码 revision。
- 旧 `LifecycleController`、Core 生命周期适配器、独立 `runner_bootstrap.py` 与其专用
  outbound 状态机；当前渠道通过公共执行组件和 `shared_worker.py` 启动。
- 无当前调用方的 v1 descriptor、独立环境身份与旧控制/语音 DTO；保留当前共享
  descriptor、消息 DTO、重试策略及已发布的 v1 hello 兼容契约。

保留 `channel_protocol` 中当前 worker、Driver 使用的协议、消息、可靠投递、回复生命周期、
源码摘要与 descriptor 模型，以及当前共享进程授权的 endpoint 注册与路由边界。
旧 bootstrap 测试中仍覆盖公共管道传输和输出隔离的用例转向公共执行组件；
OneBot/Voice 历史测试入口现已转向当前插件 Driver 与共享宿主的生命周期回归，
`legacy_isolated_proxy` / `legacy_contract` 继续支持现有兼容路径。
已完成原功能对照和接入验收的渠道逐项移除旧 Core 实现；尚未收口的旧实现仅作
迁移参考，不恢复为注册入口。`BaseChannel` 和 `ChannelManager` 继续承担宿主职责。
`test_ch_0_001_boundary.py` 同时保留历史矩阵自洽检查，并检查当前
Console＋18 插件、Core 队列、ACL、DTO 和宿主路径边界。

退役前代码和完整历史文档可从本次基线
`03d27190540032983cd4178d682a5cd414b4d1f7` 查阅，例如：

```sh
git show 03d271905:src/qwenpaw/app/channels/env_manager.py
git show 03d271905:src/qwenpaw/app/channels/environment_installer.py
git show 03d271905:docs/proposals/channel-isolation/DESIGN.md
```

这些命令只读取 Git 历史；当前运行行为和接入方式以现有插件契约、实现及行为测试为准。
