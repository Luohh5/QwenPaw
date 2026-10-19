# Channel-isolation retired code cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 删除旧隔离方案中当前和后续迁移都不再需要的实现，保持飞书、钉钉及公共插件系统行为。

**Architecture:** 保留当前 shared 子进程链路及其传递依赖；保留尚有后续迁移价值的 OneBot/Voice 原型与测试。已被公共依赖机制替代的专用环境安装器及其独占测试退出维护，历史文档标记为参考并链接当前实现。

**Tech Stack:** Python、pytest、现有 JSON-RPC worker、GitHub Actions。

**Spec:** 本次用户明确批准上一轮分类清理方案：保留正式复用能力及未来迁移参考，删除无用旧实现，保证已完成插件的可用性。

## Global Constraints

- 高效、精炼；不新增平行兼容层、安装器或管理机制。
- 功能代码和测试代码由两个不同子 agent 编写；功能作者不得修改测试断言。
- 当前分支 codex/channel-pluginization，基线 03d271905；不提交、不推送、不变更分支或 index；保留用户无关文档改动。
- 不删除 app/channels 中尚未迁移的官方渠道、BaseChannel、ChannelManager。
- 不删除当前 ChannelAdapter / ChannelProcess / shared_worker / Driver 所依赖的协议、握手、回复状态、依赖和归属模块。
- OneBot/Voice 旧 Runner 原型及其行为测试仍有迁移价值，相关 v1 bootstrap/协议依赖不能仅凭无正式调用删除。
- legacy_isolated_proxy 和 legacy_contract 的插件兼容路径保留。
- 不安装桌面或 Docker，不使用真实机器人；仅模拟平台边界，其余加载/进程/事务走正式实现。

## Review Focus

1. 子进程按路径加载的代码不能因静态 import 扫描遗漏而误删：测试真实 worker 启动及提取后发行包。
2. 混合模块不得整文件删除公共算法：检查 current adapter/Driver 的依赖闭包，保留消息与恢复语义。
3. 测试目录名称不是历史代码依据：保留 RPC、可靠投递、回复生命周期及平台行为测试，仅删除专属退役机制测试。
4. 旧 OneBot/Voice 原型要保持可参考和可验证，含动态 bootstrap：保留原型链，必要时运行对应已有测试。
5. 文档、CI 和脚本必须与剩余代码相符：有效入口有当前文档；历史设计明确标记退役项，CI 不引用已删除路径。

### Task 1: 清理退役实现并验证兼容

**Files:**
- Remove candidates: src/qwenpaw/app/channels/env_manager.py、environment_installer.py；tests/unit/channel_isolation/test_ch_1_002.py、test_ch_1_003.py（由测试作者删除）。
- Inspect before any extra deletion: scripts/channel_isolation、channel_protocol、tests/fixtures/channel_isolation、.github/workflows/channel-isolation-g0.yml。
- Docs: docs/proposals/channel-isolation 下增加简短当前状态/迁移说明，原文档保留历史技术参考。
- Production/test exact deletion allowlists recorded in scratch reports after independent dependency audit. Additional candidates need controller ruling before edits.

**Interfaces:** 公共接口和消息格式不变；当前插件依赖 Gate、共享解释器、Generic Channel 契约保持原实现。

- [x] 独立测试作者核实源码 origin，在临时工作/密钥目录跑删除前基线；独立审计候选的调用及动态路径。
- [x] 功能作者复核删除闭包；基线确认后删除退役生产实现，更新必要文档/CI引用，不修改业务接口。
- [x] 测试作者仅删除已退役实现的专属测试，保留当前安全和行为断言；执行当前飞书/钉钉真实 worker、生命周期/事务、通用插件和发行测试。
- [x] 检查余留引用、代码检查和 git diff --check；记录与基线相同的告警或未解决问题，不掩盖失败。
- [x] 独立 task review 和最终 composition review；如有真实问题，原功能作者修正、测试作者验证，仅复审修正范围。
- [x] 更新删除清单、保留理由与验证结果，所有改动留在工作区供用户提交。

## Execution rulings

这是已获用户批准的清理型重构。沿用用户指定 checkout 和已有功能分支，不创建新工作树。没有新功能或行为变化，不为文件删除编写镜像实现的“文件不存在”测试；采用独立调用审计及现有行为基线→回归证明。出现实际行为缺陷时，再由独立测试作者先复现并交给功能作者修正。只在新变更、失败或未解决疑点时追加验证。

## 完成记录

- 删除两份退役生产模块共 3,331 行，删除两份独占测试共 1,724 行。其余正式运行源码、插件和脚本未改；公共协议、动态 worker、依赖 Gate、兼容层及 OneBot/Voice 原型保留。
- 三份保留的隔离测试明确绑定 checkout 源码，原断言和超时保持。历史文档正文保留并补入当前路线说明；所有当前接入链接均指向已跟踪文件。
- 删除后的关键分组回归共 30 项通过，覆盖飞书/钉钉收发、重启、停止、更新/恢复、第三渠道、公共插件、提取发行包和原型动态 bootstrap。现成本地 Black、flake8、pylint、AST 及 diff 检查通过；完整 pre-commit/mypy 受环境限制，未宣称通过。
- 既有 7 项历史布局基线失败、完整飞书包装器基线超时、默认收集的 3 项错误及另一导入模式的 helpers 收集错误均保留记录；本任务未修复这些问题，不宣称全项目全绿或真实平台验收。
- 独立任务评审和最终组合评审均通过，无本任务剩余阻塞项。改动未提交、未推送；完整命令、日志及审查报告在 `.superpowers/sdd/2026-10-14-channel-isolation-cleanup/`。
