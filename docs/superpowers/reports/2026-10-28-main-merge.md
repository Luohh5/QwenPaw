# main 合入 MCP 插件分支验证记录

## 范围与基线

- 开发分支：`codex/mcp-plugin`，合并前为 `28f07aaea`。
- 本次合入用户已更新的本地 `main`：`f496ef3428f2f0c9b5ec7bfea0e34372f25d2727`。
- 合并前备份：`codex/mcp-plugin-pre-main-20261028`。
- `origin` 和默认 push remote 仍为 `https://github.com/Luohh5/QwenPaw.git`。
- 本次保留 MCP 可选插件及 SDK 1.1 Driver 扩展；不实施 mail 或 ACP 插件化。
- 既有功能约定见 [MCP 实施方案](../plans/2026-10-20-mcp-plugin.md)。旧验收及性能数据不作为本次合并后的重新测量结果。

## 合并处理

上游将原插件重构经 squash 合入，Git 因而报告 105 个冲突文件，其中大量是同名文件的双方新增冲突。使用本分支开发所基于的原始 SDK 提交 `56be46ddf`，对冲突文件重新执行三方内容合并：96 个自动得到兼容结果，9 个进一步逐处处理。随后检查自动合并文件的实际行为，而不是整组选择 ours/theirs。

保留主分支的 SDK 修复、消息/模型/provider/memory 实现、工作区退休清理与 cron 接管、manifest/requirements 通用依赖处理和安装器安全要求。主分支已移除的 SDK 专属依赖拦截及其过时测试不恢复；对应的 `uv==0.8.15` 测试依赖也移除。

同时保留本分支的 `DriverProtocolSpec`、来源及凭据作用域、SDK 1.0 兼容、可选插件离线资源、安装收据、显式禁用/卸载意图、按需 DriverManager 和条件化配置监测。发布检查继续校验实际安装 wheel 的唯一版本元数据及逐文件字节身份，并接入主分支的作者测试 helper 导出修复。

## 经回归复现后修复的交界问题

1. **已提交的工作区替换被取消。** 主分支保护旧实例清理，本分支随后才激活新 Driver；前一步传递取消会跳过后一步。现在同一受保护交接完成旧实例清理、新 Driver 激活及调度恢复，再向调用方传递取消。cleanup/activation 两个取消时机均先验证失败，再验证修复。
2. **cron 在 MCP 准备好前启动。** 保留主分支原子接管中的执行时间、并发计数、历史及所有权转移，但在工作区交接中保持新 scheduler 暂停，待 Driver 激活完成后恢复。真实 APScheduler 回归覆盖正常及被取消的重载。`CronManager.handoff_from()` 默认行为保持原样。
3. **过期交接重新恢复退休调度器。** 新 Driver 激活结束后，只有仍为当前工作区、未进入停止或卸载流程的实例才能恢复调度；检查与恢复之间没有 await。替换、移除、停止三个竞争窗口均先红后绿。
4. **快速 A → B → A 切换复用旧就绪状态。** MCP 页面必须完成当前身份轮次的重新加载，才允许编辑；保留主分支加载错误、重试与 mutation generation 行为。

旧 MCP 集成测试显式安装插件后再验证原有 200/404 契约；未安装插件的 503 契约继续由独立测试覆盖，不改变全局测试服务器的默认安装状态。ACP 清理测试仅替换 handler 构造，继续走真实的来源 revision、作用域和关闭记账。

## 本次验证

使用 Python 3.13.10、`agentscope==2.0.10`、`reme-ai==0.4.2.0`、`reme-auto-fin==0.1.3`、`reme-daily-paper==0.1.3`、pip 26.2.2、certifi 2026.10.23、pytest 9.1.1。临时环境复用第三方依赖，按 distribution name 去重；源码测试显式绑定本仓库的 `src` 和 `packages/qwenpawmail-mcp/src`。测试工作、凭据、备份及 XDG 目录均隔离，keyring 使用 null backend。

| 检查 | 结果 |
| --- | --- |
| Driver、app、runtime、PawApp、插件 CLI、channel 生命周期及 SDK 导出脚本测试 | 4369 通过，1 跳过；首轮 4363 通过，6 个环境受限项修正环境后原断言全部通过 |
| 全部 `tests/unit/plugins` | 1458 通过，7 跳过；首轮 1448 通过，9 个本机端口权限失败和 1 个临时环境重复 pip 元数据失败分别修正后通过 |
| 最终 cron / 重载 / lazy Driver 回归 | 208 通过，包含本次新增 7 个时序回归用例；与上述后端大组存在重叠 |
| MCP 定向集成测试 | 137 项均通过；本机监听与进程查询限制项提升权限重跑，旧运行时测试补齐显式安装前置后通过 |
| ACP 兼容回归 | 303 项均通过；其中 session MCP 文件的 12 项在更新测试替身后重新通过 |
| Console MCP / SDK / registry / API 回归 | 42 文件，635 项通过 |
| Console TypeScript 与生产构建 | 通过；初始 bundle 9.89 MiB raw / 2.55 MiB Brotli，11 个资产 |
| 新 wheel 与 sdist 构建、独立 wheel 安装 | 通过，版本 `2.3.0b1` |
| 实际 wheel 的冻结资源映射、冷启动与字节身份验收 | 7 项通过 |
| wheel 中 SDK 前端包的独立作者检查 | NodeNext、bundler、runtime 全部通过 |
| SDK 严格作者类型检查 | 通过，mypy 1.7.0；实际导入来自独立 wheel 安装目录 |
| 完整 wheel SDK 七阶段检查 | 全部通过；327 项独立作者测试通过，3 个依赖弃用警告，474.91 秒 |
| 冲突标记、Python AST、修改文件格式、Flake8 与差异空白检查 | 通过；上游原样引入的 `.patch` 上下文空格保留，差异空白检查排除这些补丁文件 |

完整源码测试保留以下失败证据后原样重跑，没有删除断言或用 skip 掩盖失败：沙箱禁止本机 bind/进程查询；临时解释器的重复 pip 元数据；独立子进程清除 PYTHONPATH 后拾取其他 checkout。最终测试环境绑定本仓库，wheel 作者环境不含源码路径。

一次单独的解释器导入检查遗漏隔离变量，读取初始化尝试在真实用户目录创建恢复锁，被沙箱拒绝，没有完成该写入；后续检查及所有测试使用上述临时目录配置。

## 发布包与证据

- Wheel SHA256：`34431c94e9f7b946002d50790405b51fee848bdd2a655524cccd59f7ea5b6197`。
- Sdist SHA256：`fb091b62f16be19619e9a746cc2daf0bbc7556f5d9f7bf652c71601e15c58038`。
- Wheel 共有 3461 个成员，包含全部 13 个离线 MCP 资源。SDK 实现及全部 MCP 资源逐字节匹配最终生产源码；工作区交接仅经提交钩子整理逗号与排版，AST 完全一致；cron 文件仅在构建后缩短了一条 docstring 以满足 79 列检查，除此之外字节相同。该包用于本次验证，不是另行发布的发行物。
- 原始日志与独立包位于本机 `/private/tmp/qwenpaw-mcp-main-release/`，源码测试日志为系统临时目录下 `qwenpaw-mcp-main-*`；这些临时文件不作为仓库发布内容。
- Frozen 验证覆盖资源映射与模拟资源冷启动；没有制作或运行真实 Windows/macOS 桌面发行程序，也没有重新测量长期性能。
- 本次只完成本地合并，不 push。
