# 插件公共进程组件提取计划

> **For agentic workers:** 使用 superpowers:subagent-driven-development。功能代码与测试由不同子 agent 编写。用户已在会话中确认设计并要求实施；沿用当前分支，不提交或推送。

**Goal:** 将 B2 的进程管理与 IPC 提取为 `src/qwenpaw/plugins/execution`，非频道插件无需加载频道代码即可使用，旧频道通过薄适配复用。

**Architecture:** 模块管理器决定何时运行；公共组件只执行显式启动、通信、停止和回收。频道描述、频道握手与平台连接状态留在频道侧。公共组件不依赖 A 尚未交付的插件生命周期，也不接管安装、更新和依赖安装。

**Tech Stack:** Python asyncio subprocess、既有 Content-Length / JSON-RPC、pytest；不引入依赖。

**Spec:** 本次会话已确认的三层分工及以下接口约定；旧行为依据 `2026-10-10-channel-shared-process-b.md` B1/B2 与现有独立测试。

## 全局约束

- 工作目录及分支沿用用户当前 checkout；保留既有未提交内容。
- 中文新增说明统一称“频道”；现有 channel 标识符无需改名。
- 公共模块不得导入 `qwenpaw.app.channels`、`qwenpaw.channel_protocol` 或插件 loader/api/registry。保留 `qwenpaw.plugins` 原有公共导出，可改为惰性导出。
- 共用依赖环境；无自动装包、无 SDK 发现导入、无新增进程池或独立环境。
- 同一份通信与进程管理实现；旧路径只做兼容导出或业务适配。旧协议参数校验不能弱化。
- 保留 B2 的启动/停止预算、取消后归属、未知结果不重放、有限重启、日志排空、私密配置传输和进程树回收。未确认退出时保留句柄，不允许重叠启动。
- 本次不实现 B3/B4、A 的 Loader/Manager 接入、自动执行模式选择或正式插件发布；普通同进程插件不因公共组件存在而启动进程。
- 功能作者不改测试；测试作者不改功能。真实进程验收；不以模拟包绕过导入边界。

## 公共接口

`qwenpaw.plugins.execution` 导出：

- `PluginLaunchSpec(python_executable, support_root, code_root, source_revision, plugin_id, instance_id, generation, entrypoint, config, dependency_paths=(), requirements_path=None, enabled=True, startup_timeout=5.0, shutdown_timeout=2.0, restart_limit=0, restart_backoff=0.1)`。路径用 Path，`entrypoint` 为 `module:qualname` 字符串，配置不进入 repr。可选 requirements_path 必须是可信 code_root 内的文件，只检查已安装元数据，不安装。
- `PluginProcess()`：`register_method(name, handler)`、`register_notification(name, handler)`、`await start(spec)`、`await call(method, params=None, timeout=...)`、`await stop(timeout=...)`、`health_snapshot()`。只在显式 start 且 enabled 时启动；健康查询及构造不启动。
- `StopResult(exited, returncode, forced, residual)`、`ProcessError.reason_code`、`CallUnknown(request_id, method, generation)`；已发送但结果不明不重发。
- `PluginIdentity(plugin_id, instance_id, generation)`；`PluginContext.peer`、`.identity`、`await set_state(state)`。组件实现零参数构造、`bind(context)`、`async start(config)`、`async stop()`。
- 健康快照保留 B2 通用字段：state、pid、generation、returncode、restart_count、last_error、log_bytes、unknown_outcomes；通用业务状态用 component_state，允许 starting/ready/failed/stopped，与进程 ready 分开。
- 通用控制方法 `execution.hello/ready/state/error/stop`，身份用 plugin_id/instance_id/generation；握手核对协议、代码、实例与代际。频道兼容适配保留原 `runner.hello/shared.*` 与 channel_key/platform_state。
- 通信提取使用同一份 FramedTransport、RpcPeer 与基础消息/异常类型；频道 RpcPeer 注入既有 `validate_method_params`，旧类型导出保持同一对象。

## Review Focus

1. 非频道使用误带入频道/插件加载器：父子进程均阻断对应导入并实际通信。
2. 协议搬迁后类型或参数校验失效：既有协议/RPC 回归继续通过，通用请求与频道请求各自验证。
3. 启动取消、停止超时、旧进程通知及重启导致重复实例：复用原 B2 真实进程测试，并补通用生命周期覆盖。
4. 入口/依赖路径或源代码变化绕过启动校验：通用 worker 启动前验证可信路径及摘要。
5. 导入公共组件触发插件框架加载：惰性导出兼容验证与隔离导入测试。

## Task 1：独立测试与提取

**Files:** 新增 `src/qwenpaw/plugins/execution/`；调整 `src/qwenpaw/plugins/__init__.py`、`src/qwenpaw/app/channels/process_supervisor.py` 及 `src/qwenpaw/channel_protocol/` 中必要兼容层。新增 `tests/fixtures/plugin_execution/` 与 `tests/unit/plugins/test_plugin_process_execution.py`。原频道测试保持强度。

- [x] 测试作者先建立非频道公共 API、导入边界、真实双向 IPC 与生命周期测试，记录 RED。
- [x] 功能作者在 RED 后提取公共实现，将原频道实现收敛为适配；不复制第二份进程管理或 RPC。
- [x] 运行新测试与原 B1/B2、通信协议及插件导出回归，修复功能问题。
- [x] 独立 reviewer 检查职责、兼容、资源归属与测试有效性，处理阻塞问题。

## Task 2：交付验证与说明

- [x] 公共组件 README 说明最小调用方式、模块管理器的启动权、共享依赖与 A 接入 TODO。
- [x] 更新原 B1/B2 计划的文件归属，明确 B3 以后调用公共组件。
- [x] 检查本次差异和适用代码检查，记录实际平台覆盖与未解决限制。

## 验证命令

Python：`/tmp/qwenpaw-channel-merge-venv-2026-10-09/bin/python`。

新测试使用独立 `-I` 包装入口；B1 和 B2 fixture 不放在同一解释器运行，因为 B1 导入守卫有意禁止频道宿主。运行 `tests/unit/plugins/channels/test_channel_process_contract.py`、`test_channel_process_supervisor.py` 的包装入口分别隔离。

A 接入仍为 TODO：正式描述关联、启用/停用与更新事务、停止结果转换、共享依赖变更策略、执行模式选择。跨平台和冻结桌面验证沿用原 TODO-E2。

## 完成记录

2026-10-10：公共组件提取、频道兼容适配和文档完成，独立审查通过。最终所选频道与插件回归 **597 项通过**（157.17 秒）；其中隔离入口另执行通用组件 20 项、原 B2 42 项通过及 1 项 Windows 跳过，不重复计入顶层统计。功能与测试由不同作者完成；审查发现的停止参数及握手身份校验问题均经过独立 RED → GREEN 回归验证。适用格式和静态检查通过。所有运行证据来自 macOS，Windows/Linux 原生及冻结桌面仍待验证。改动保留在当前分支，未提交或推送。
