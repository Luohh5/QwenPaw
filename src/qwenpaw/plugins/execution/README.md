# 公共插件进程组件

当前接入与验收以[统一插件化方案](../../../../docs/superpowers/plans/2026-10-14-official-channel-pluginization.md)为准；下文 B3–B5 与 A 待办保留早期设计背景，不代表当前 Loader 尚未接入。

模块管理器决定何时启动插件；`PluginProcess` 只负责显式启动、双向 RPC、停止和进程树回收。构造、注册处理函数、查询健康与 `enabled=False` 均不会启动进程。普通同进程插件不受影响。

组件无需频道描述、频道标识、频道 SDK 或插件 Loader。共享依赖来自所选 Python 的环境及显式 `dependency_paths`；可选 `requirements_path` 必须位于可信 `code_root`，只检查已安装分发元数据，不安装包，也不执行显式目录中的 `.pth` 文件。

宿主保管某个执行实例的非执行资源时，可用 `RuntimeOwners.reserve(..., dependency_owner=lease)` 关联精确父实例；父必须仍有效，且插件、代码目录、实例一致。资源仍参与停止和源码占用保护，但不虚构第二套依赖环境。独立执行的 worker 必须使用默认执行 lease，不能借此省略依赖验证；父退出或换代后遗留的资源 claim 恢复为未知依赖，继续阻止不安全变更。

仅保存扫码会话等纯宿主状态、没有执行父实例的记录，可显式使用 `custody_only=True`。它与 `dependency_owner` 互斥，不代表已验证某个运行环境；真正执行管理操作的 worker 仍独立登记并验证依赖。该分类只由宿主代码声明，不接受子进程、配置或描述符指定；停止失败仍保留记录与源码锁，不能按资源名称自动豁免。

代码目录中的 `indexer.py`：

```python
class Indexer:
    def bind(self, context):
        self.context = context
        context.peer.register_method("index.count", self.count)

    async def start(self, config):
        self.documents = config["documents"]
        await self.context.set_state("ready")

    async def count(self, params, request):
        return {"count": len(self.documents)}

    async def stop(self):
        await self.context.set_state("stopped")
```

宿主显式启动；`support_root` 指向包含 `qwenpaw/plugins/execution` 的可信 `src` 目录。摘要由调用方对选定代码目录计算，启动时 worker 再次核对：

```python
import hashlib
from pathlib import Path
import sys
from qwenpaw.plugins.execution import PluginLaunchSpec, PluginProcess


def artifact_digest(root):
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            for data in (path.relative_to(root).as_posix().encode(),
                         path.read_bytes()):
                digest.update(len(data).to_bytes(8, "big"))
                digest.update(data)
    return digest.hexdigest()


async def index_documents(code_root: Path, support_root: Path):
    process = PluginProcess()
    try:
        await process.start(PluginLaunchSpec(
            python_executable=sys.executable,
            support_root=support_root.resolve(),
            code_root=code_root.resolve(),
            source_revision=artifact_digest(code_root),
            plugin_id="document-indexer", instance_id="workspace-one",
            generation=1, entrypoint="indexer:Indexer",
            config={"documents": ["one", "two"]},
        ))
        return await process.call("index.count", {})
    finally:
        result = await process.stop()
        if not result.exited:
            # 模块管理器继续持有 process，处理 result.residual；不得重叠启动。
            raise RuntimeError(result.residual)
```

`bind(context)` 同步注册处理函数；`start(config)` 和 `stop()` 为异步方法。`context.peer` 可调用宿主注册的请求或通知处理函数；`context.identity.to_mapping()` 包含 `plugin_id/instance_id/generation`。参数和结果必须能编码为 JSON。

进程 `state=ready` 表示握手完成、组件已绑定；`component_state` 独立表示 `starting/ready/failed/stopped`。启动不会等待外部连接工作完成。配置仅通过 stdin 传递；普通 stdout/stderr 被排空丢弃，健康快照仅记录字节数与固定错误码。

已发送请求在超时、取消或断链后可能具有未知结果。`CallUnknown` 与 `unknown_outcomes` 保留请求 ID、方法和代际，不记录载荷、不自动重放。`stop()` 先撤销重启，再在预算内回收整棵进程树；退出未确认时保留归属与残留证据。

公共控制方法为 `execution.hello/ready/state/error/stop`，不可注册为业务方法。旧 `ChannelProcess` 保留频道 `LaunchSpec`、`runner.hello/shared.*` 和 `platform_state`；频道 RPC 包装器继续使用原 DTO 校验。通信、监督与 worker 生命周期均复用公共实现。

A 的 Loader/Manager 接入、描述关联、执行模式选择、启用停用及更新事务、停止结果转换、共享依赖变更策略仍为 TODO。B4 已提供 DingTalk Driver；B5 以现有插件目录的安装布局验收为主，不要求独立打包器。发布系统与正式安装事务仍未接入。当前真实运行验证覆盖 macOS；Windows 仍依赖既有管道/Job 实现与模拟管道回归，原生 Windows、Linux 及冻结桌面验收另行完成。父进程保护仍继承 B2 的边界：守护线程无法保证在永久占用 GIL 的原生扩展中及时执行。


## B3：显式 Feishu 宿主代理

`qwenpaw.app.channels.isolated_proxy.IsolatedChannelProxy` 是 SDK-free `BaseChannel` 子类，显式构造如下；它不会发现插件或安装依赖。`process` 是宿主 Agent 回调，队列、ACL、审批真实操作者、TaskTracker 与消息合并仍由宿主处理。

```python
from qwenpaw.app.channels.isolated_proxy import IsolatedChannelProxy
from qwenpaw.app.channels.process_supervisor import LaunchSpec

# code_root 是包含 feishu/ 包的可信制品根，非 feishu/ 本身。
spec = LaunchSpec(
    python_executable=python_path,
    support_root=trusted_src,
    code_root=artifact_root,
    descriptor_path=artifact_root / "feishu/channel.json",
    source_revision=artifact_digest(artifact_root),
    channel_key="feishu", instance_id="workspace-one", generation=1,
    qwenpaw_version=host_version,
    config=feishu_config,
)
channel = IsolatedChannelProxy(
    process, launch_spec=spec, workspace_dir=workspace_root,
)
channel.set_enqueue(enqueue)
try:
    await channel.start()
    await channel.send("feishu:open_id:ou_target", "hello", meta={})
finally:
    await channel.stop()
```

Feishu 描述入口是 `feishu.driver:FeishuDriver`；制品应包含 `feishu/__init__.py`、`driver.py`、`platform.py` 及其相对导入辅助文件。这避免平铺 `platform.py` 遮蔽 Python 标准库。SDK、下载上传、卡片、回复目标和 WebSocket 只在子进程执行，宿主不导入候选 Feishu Channel/平台。`from_config` 同样需要显式 `launch_spec`，可选复用 `channel_process`。启动等待平台连接；手动停止后再次启动推进代际，未确认退出则保留进程归属。

数据 RPC 复用 `event.batch`、`channel.send/reaction/response.finish`；`channel.response.open` 接收身份、`to_handle`、JSON `meta` 和可选 `session_id`，返回冻结目标的响应句柄。流序列、已发送资源、未知结果归属该句柄，线程回复保留原始主题，取消只收尾本次响应。普通消费与 TaskTracker 两条路径、独立发送/审批、媒体缓冲合并均在共享响应作用域中收尾。

宿主仅在队列接受后确认和记录事件去重。子进程对暂时拒收和不确定 ACK 使用同一个事件/批次/句柄进行最多五次有界重试，延迟为 0.1/0.2/0.4/0.8 秒；永久拒收不重试。耗尽后安全清理并移除确定未入队的新路由，后续重新投递可重新打开；曾有不确定 ACK 的路由保留，因为消息可能已入队。连接代际变化和停止中断重试。此机制不提供跨进程死亡的持久入站队列。固定原因码的 `channel.diagnostic` 通知只携带身份，宿主记录安全日志和一个 `last_diagnostic`，不传正文、目标或凭据，也不改变实际连通状态。

`workspace_dir` 指定时，宿主以原子写保存每个频道/实例的有界 JSON 状态；旧 `feishu_receive_ids.json` 两种元组顺序兼容。发送与收尾在 SDK 副作用前保存未知意图，未知结果不会自动重发或转成通用错误消息。完成的确定收据按条目数和字节预算回收；活动、未知及未完成收尾证据保留。未知收尾返回结构化 `RESPONSE_FINISH_FAILED`，恢复时不重放未知副作用，其他消息仍可连接处理。未指定工作区时仅保留当前代理生命周期中的状态，父进程重启恢复不作保证。

此节记录 B3 飞书边界；B4 已提供同契约的 DingTalk Driver。A 的 Loader/Manager 自动接入及 E1/E2/E3 的真实平台、跨平台和最低 SDK 版本验收仍待完成；也不声称完成真实凭据、线上 SDK/网关和原生 Windows/Linux 验收。测试使用真实代理、公共进程组件和生产 Driver，仅替换 SDK/网络边界。

B5 安装目录、共享代码/依赖布局及 A 剩余工作见 [交接说明](../../../../docs/superpowers/plans/2026-10-12-channel-process-handoff.md)，验收与资源数据见 [测量记录](../../../../docs/superpowers/plans/2026-10-12-channel-process-measurements.md)。代理启动等待平台连接；独立宿主协程可继续运行的边界验收不表示正式应用启动链已接入核心/渠道就绪分离。
