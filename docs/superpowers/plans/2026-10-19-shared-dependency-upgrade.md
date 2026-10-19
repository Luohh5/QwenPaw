# 共享依赖受控升级与插件安装恢复

用户已批准聊天中的六项方案并授权实现。本文固定实施边界；功能代码与测试代码由独立 agent 编写，保留当前分支已有改动，不自动 commit/push。

## 目标与规则

- 已满足依赖直接安装；安全新增依赖复用现有补装路径。
- 兼容的依赖替换先准备、确认，再安全停止消费者、升级、验证并恢复安装。
- 全体已安装插件、宿主及传递依赖共同参与兼容检查；真实冲突不写环境。
- 仅共享一套解释器和依赖，不建立每插件 venv；禁止让重复可见的 distribution 绕过来源检查。
- 未确认不替换依赖、不停正在运行的渠道；下载和构建只在暂存目录执行。
- 升级计划绑定可信来源副本、源码摘要、解释器、实际包来源/版本及环境状态；应用前复核。
- 资源归属与停止使用现有生命周期接口；停止失败或证据不足不写依赖。
- 主进程也使用环境时需要独立维护进程等待后端退出；系统/Conda/受保护的内置环境交所属安装方式处理。
- 使用跨进程排他与维护状态阻止其他安装和新实例进入；不能只查询本进程 registry。
- 依赖与插件源码分别能恢复；持久化操作记录，异常退出后先恢复再激活。
- CLI、HTTP 共用服务；在线保护性拒绝不能回退离线安装绕过检查。
- 仅针对受影响测试；不放宽断言，不增加 skip/xfail，不安装真实平台依赖或调用真实机器人。

## 统一交互契约（实施前由 source/test author 核对）

- 安装/上传遇到可升级依赖时，返回结构化 `409`：`detail.code = DEPENDENCY_UPGRADE_REQUIRED`、可读 `message` 与 `operation`。
- 操作至少包含 `id`、`plugin_id`、`status`、版本变更、受影响实例、重启要求与可执行的后续动作。
- `POST /plugins/dependency-upgrades/{id}/apply` 确认执行，`GET` 查看结果，`DELETE` 取消尚未提交的操作。
- source/ZIP 在操作持有期间保留，不依赖请求结束即清理的临时目录。
- 界面显示升级影响与确认入口，取消不修改环境；进度可恢复，CLI 给出同一服务的入口。

## 实施 checklist

### 1. 公共升级规划与准备

- [x] 核对原 gate、wheel 下载与版本锁、共享环境来源与生命周期。
- [x] 独立测试先在现有入口复现 raw failure，固定有行为意义的 RED。
- [x] 抽公共升级计划及结构化诊断，区分缺包、替换、真实冲突及受保护环境。
- [x] 求解兼容版本并保留未受影响包的约束，下载/构建候选 wheel，不改活动环境。
- [x] 保存源码与计划指纹并防止旧计划/恶意路径提交。

### 2. 受控提交、恢复及重启

- [x] 环境维护排他、启动/安装准入与真实停止回执。
- [x] 管理目录暂存替换；宿主使用环境时转独立维护进程，不能热换已导入包。
- [x] 环境实际来源决定升级目标；避免同包多目录遮蔽。
- [x] 提交前复核，依赖备份与日志，验证失败回滚，异常退出恢复。
- [x] 新进程验证版本/来源，恢复之前运行状态并继续插件安装。

### 3. HTTP、CLI、Console 接入

- [x] 安装/上传/修复统一调用公共升级服务，保留操作来源。
- [x] 确认、状态与取消 API，结构化错误码及可读信息。
- [x] CLI 的在线拒绝不绕过为离线写入，支持查询/确认/取消。
- [x] 官方市场、第三方市场、URL/ZIP 安装统一确认与进度；恢复连接后继续查询。
- [x] 仅展示用户决策所需的影响、结果与操作，内部错误保留诊断。

### 4. 独立验证与审查

- [x] Matrix/QQ 旧版本场景、无消费者场景、纯新增回归。
- [x] 有效兼容计划与真实版本冲突，已有消费者保护、停止失败不写入。
- [x] 暂存/计划篡改、过期、并发、取消和崩溃恢复。
- [x] 新进程实际导入及来源，依赖和源码回滚，配置保留及恢复。
- [x] HTTP/ZIP/CLI/界面入口与用户确认；主进程重启路径用受控子进程验证。
- [x] 只运行受影响测试；独立 reviewer 审查生产差异与测试证据。


## 实施与验收结果（2026-10-19）

实现已完成，保留未提交状态供审阅。公共计划、确认、提交、恢复服务被插件管理页及渠道配置页的安装/修复入口共同使用。

| 验证范围 | 结果 |
| --- | --- |
| 新升级、真实子进程与渠道恢复、依赖框架、可见性 | 122 个不同用例通过；按受影响范围分批执行，修复后仅重跑相关失败项 |
| 前端升级、API、安装入口与恢复 | 115 个范围内用例通过 |
| 扩展运行的原有 Drawer 测试 | 11 个失败及 7 个未处理错误；用未修改的 HEAD Drawer 覆盖复现相同结果，原断言与 mock 未改动 |
| 独立代码审查 | 集中修复及最后锁/错误类型差异通过；无未解决的 Critical/Important |
| 静态检查 | Python 编译、新模块 Black、前端 TypeScript/格式/ESLint 与 diff 检查通过 |

测试采用本地构造的真实 wheel、实际导入路径、进程通信与退出，以及真正的强制退出故障注入；没有调用真实机器人或改动实际系统依赖。功能与测试由不同 agent 编写。

验证边界：Windows/macOS 原生冻结包和真实平台账号尚未实机验证；自动进程保护覆盖接入读租约的 QwenPaw 实例，不覆盖任意外部 Python 程序。系统/Conda/受保护来源交环境管理者处理。已知普通 app 启动器支持自动退出、维护和重启；未知启动器或中断恢复可使用保留的绝对路径脚本继续。

完整证据：`.superpowers/sdd/2026-10-19-dependency-upgrade/tests/TEST_REPORT.json`、`final-review.md` 与 `completion.json`。原有工作区差异已核对保留，未 commit/push。

## 实施取舍记录

- Ruling: execute approved chat plan in the current feature checkout and record the detailed spec here without a new approval round — user explicitly authorized this implementation and this checkout — if wrong, changes remain reviewable and uncommitted.
- Ruling: affected-only test commands override skill's full-suite advice; independent TEST author overrides source-author test writing — direct user requirements — confidence is limited to exercised scope, reported explicitly.
- Ruling: no per-channel or per-plugin environments; replace verified provider at actual source and retain strict duplicate-provider rejection — approved architecture and current source audit require this — unknown ownership causes explicit refusal rather than unverified filesystem mutation.
- Ruling: existing update worker can be reused only for lifecycle mechanics; dependency rollback and source continuation require real new handling — existing worker upgrades package then asks manual restart — prevents false success but adds bounded orchestration code.
- Ruling: offline controller must execute directly with -I -S and stdlib only; normal qwenpaw import may be denied during repair. Worker-controlled verification/continuation must avoid deadlocking its own maintenance exclusion — approved restart/recovery requires this — bypass authority is scoped to private persisted operation, never a client-selected environment flag.
- Ruling: hardcrash fixture precondition tracks durablecommitintent rather than a superseded privatephase string; observable recovery assertions remain strict — source changed commit ordering as required — this preserves semantic regression without tying tests to obsolete internals.
- Ruling: active runtime-only/orphan consumers without restart/config ownership must refuse BEFORE any stop/write; ephemeral audit/preparation children may finish normally — approved restore-running-state requirement — reduces automatic upgrades in damaged runtime state rather than silently losing user services. Existing generic-success testfixture had no loadrecord; TEST must add stronger orphan no-stop/no-write assertion and real registered consumer restoration case, not weaken state preservation.
