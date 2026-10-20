# PR #7565 SDK 更新兼容合并计划

> **For agentic workers:** 使用 superpowers:subagent-driven-development，功能代码与测试代码由不同作者负责。

**Goal:** 将 PR #7565 最新 head 合并到当前渠道插件化分支，保留已有渠道能力与多进程运行方式，通过受影响范围的回归后完成本地合并。

**Architecture:** 新 SDK 仍为 opt-in；现有官方渠道不增加 `sdk` 声明。SDK 作者接口、普通 Python 插件与描述式进程渠道共用 Loader/注册表/生命周期，但保留各自的描述、执行和资源归属契约。

**Tech Stack:** Python、pytest、JSON 描述与本地 IPC、TypeScript/Vitest、Git merge。

**Spec:** 本次用户合并要求及本文 Global Constraints；合并前本地 HEAD 为 `8df4b57353671b7a3e607410f12b63ac946993f4`。

## Global Constraints

- 保留 `entry.descriptor`、静态发现、共享依赖、多进程执行入口及官方 owner/key 映射；现有渠道不迁移 SDK。
- 同时保留 PR 的 SDK、通用生命周期修复与原插件兼容性；自动合并的代码也必须检查调用链。
- 功能与测试由不同子 agent 编写，代码高效精炼，不放宽断言、不添加 skip/xfail、不靠增加 timeout 过关。
- 只运行受影响的测试和必要的代表链路；不重复已通过且未受后续改动影响的测试，不运行全仓测试或全仓收集。
- 保护所有已有未跟踪文件和用户配置；使用临时工作区/合成凭据，不连接真实机器人，不推送远端。
- 沿用用户指定的当前 feature 分支原地合并；已建立本地备份分支。最终本地 merge commit 在兼容检查及定向回归通过之后创建。

## Review Focus

- 未声明 SDK 的描述式渠道与已声明 SDK 的普通插件共存，注册接口及工作区投影不能交叉误用。
- 未启用、缺依赖及仅查看配置时，不导入平台实现或启动进程；开机不安装依赖。
- 停止失败保留真实 runtime owner 和源码占用，更新失败恢复原实现、配置和会话；不能清空资源归属来绕过检查。
- 共用依赖检查兼顾已导入宿主包与仍在执行的 worker，SDK 安装分支不得绕过已有共享依赖事务。
- 原生渠道收发与新 SDK 的最小工具链路分别成立；SDK 检查入口及相关前端自动合并处不引入悬空接口。

## Task 1：固定两侧版本与冲突清单

- [x] Fetch PR 最新 head 到单独引用；记录 base/ours/theirs、原始工作区状态与恢复分支。
- [x] 执行 `merge --no-commit --no-ff`，保存原始冲突及双方相关 diff，分类为功能、测试和资源冲突。

## Task 2：功能合并与语义兼容

**Files:** 按实际冲突清单，重点为 `src/qwenpaw/plugins/{__init__,architecture,api,loader,registry,lifecycle,workspace_projector,dependency_gate}.py`、渠道静态适配与构建入口；不预设所有文件均需修改。

- [x] 功能作者逐处说明两边意图，解决生产代码冲突；保留描述式 backend 与 SDK opt-in 分派。
- [x] 检查自动合并的注册/投影/依赖准备/卸载恢复路径，修复经证据确认的不兼容。
- [x] 保存生产源码 freeze；测试作者只在稳定版本上执行定向用例。

## Task 3：独立定向验证

**Files:** 受影响的现有插件、执行组件、渠道与 SDK 测试；仅在旧测试不覆盖新组合边界时补回归。

- [x] 独立测试作者核实最小基线与测试隔离环境，保留原业务断言并解决测试文件的真实冲突。
- [x] 验证静态描述/缺包配置、真实 worker 收发与停用、旧配置/卸载墓碑、停止失败与更新失败恢复。
- [x] 验证新增 SDK 插件和未声明 SDK 的旧插件可共存，并执行与实际改动对应的 SDK/前端检查。
- [x] 对真实故障记录 RED，交功能作者修复后仅重跑失败职责及受影响控制；明确环境/fixture 问题，保留原始结果。

## Task 4：独立审查与完成本地合并

- [x] 审查原始冲突及最终 diff，对照两侧意图和所有兼容性要求；不重复运行已有有效证据。
- [x] 无未解决冲突、无悬空导入，必要静态检查及定向回归通过。
- [x] 兼容检查、回归与独立审查完成；冲突解决表、验证命令和限制已记录。最终本地 merge commit 及双方 parent 由 Git 核验记录确认，不纳入无关用户文件。

## 实际合并记录

- 当前分支：`codex/channel-pluginization`。
- 合并前：`8df4b57353671b7a3e607410f12b63ac946993f4`。
- PR #7565：`56be46ddf7672aa042e06b5c25f8cf2194e9fb93`。
- 恢复分支：`codex/backup-channel-before-pr7565-sdk-20261020`。
- 原始文本冲突：17 个文件；生产与测试由不同作者处理。现有 18 个渠道 manifest 未修改，未增加 SDK 声明。

| 冲突文件 | 两侧分别想保留什么 | 最终处理 |
| --- | --- | --- |
| `console/src/api/modules/plugin.ts` | 渠道侧要准确显示安装及依赖错误；PR 要支持任务中断确认和变化通知 | 两种错误与操作流程同时保留，失败仍能告诉界面具体原因 |
| `console/src/hooks/useAutoSave.tsx` | 渠道侧避免保存错 Agent 的配置；PR 避免关闭窗口时丢失或遗漏修改 | 保存任务保留原 Agent 和表单快照，同时串行处理后续修改、失败关闭确认 |
| `console/src/locales/en.json` | 共享依赖升级文案与插件忙碌文案 | 两组英文文案都保留 |
| `console/src/locales/zh.json` | 同上 | 两组中文文案都保留 |
| `console/src/pages/Control/Channels/components/ChannelDrawer.tsx` | 二维码、配置快照和工作区身份校验；PR 要处理保存失败后关闭 | 保留身份校验和二维码归属，关闭前确认保存结果，失败可继续编辑或明确放弃 |
| `console/src/pages/Control/Channels/index.tsx` | 按原 Agent/渠道提交；PR 要重置表单、返回真实保存结果 | 保留固定提交对象，合入表单重置和成功/失败结果 |
| `plugins/apps/qwenpaw-creator/backend/vendor/media_toolkit/frame_stats.py` | Windows 临时文件兼容；PR 要避免 FFmpeg 滤镜路径问题 | 改用标准输出传数据，保留超时和错误处理，去掉不再需要的临时文件 |
| `scripts/pack/generate_plugin_metadata.py` | 渠道打包；新增 Hub 打包 | 两种分类同时支持，保留原官方渠道资源构建 |
| `src/qwenpaw/app/_app.py` | 静态渠道、进程资源与恢复初始化；新增 Hub 初始化 | 两套初始化共存，完成后再报告市场就绪 |
| `src/qwenpaw/app/routers/plugins.py` | 依赖升级接口和准确运行状态；PR 要支持插件忙碌/中断 | 保留全部接口，按真实安装和运行结果返回状态，贯通中断选项 |
| `src/qwenpaw/cli/plugin_commands.py` | 准确显示安装结果；新增诊断信息 | 原结果不变，同时输出诊断说明 |
| `src/qwenpaw/plugins/__init__.py` | 延迟导入保护启动速度；新增 SDK 公开导出 | 在延迟导出表中补 SDK 名称，不提前加载 Loader 和平台 SDK |
| `src/qwenpaw/plugins/lifecycle.py` | 描述式后端、启停及失败恢复；SDK 任务管理和清理 | 共用生命周期，停用失败继续保留资源归属，成功激活后才保存状态 |
| `src/qwenpaw/plugins/loader.py` | 静态发现、多进程及共享依赖事务；SDK 加载和更新保护 | 保留统一后端流程；只有主动声明 SDK 的插件才包装新接口；更新保护覆盖完整替换与恢复 |
| `src/qwenpaw/runtime/commands/control/approval_handler.py` | 审批身份与事件；PR 要提供清晰的已处理/过期答复 | 身份、事件与清晰答复同时保留 |
| `tests/unit/app/routers/test_plugins_router_lifecycle.py` | 真实安装状态断言；PR 新增诊断参数覆盖 | 合并两侧覆盖，保留原业务断言 |
| `tests/unit/app/test_shutdown_lifecycle.py` | 停机顺序和可恢复的测试注册；PR 新增弃用提示断言 | 保留资源先清理再关闭工作区的断言，同时验证新提示 |

另外修复了自动合并不会报告的兼容问题：SDK 重启错误正确传递；SDK 渠道继续遵守统一的官方名称保护；SDK 依赖安装走现有共享事务，禁止覆盖已装包，并在解析前后环境变化时拒绝发布。受限的传递 wheel 引用仅允许复用原先已安装的同名同版包，仍审计其旧依赖。

原始双方版本、冲突快照、源码哈希、测试命令与输出保存在本地 `.superpowers/sdd/2026-10-20-pr7565-sdk-compat-merge/`；验证结果见下；最终合并提交及其双方 parent 可通过本次 Git 记录核对。

### 实际发现并修复的自动组合问题

1. 配置窗口的一条兼容提交路径忽略保存失败的返回值：保留失败结果与关闭确认，同时保留原依赖冲突取消语义。
2. 新 SDK 的 `PluginTestHost.start_channel()` 引用了旧位置的 helper：改为复用现有 API helper，不复制判断逻辑。
3. 新增 SQLite 备份代码在包初始化时提前导入 Agent：将两个 history helper 延后到实际备份、恢复使用时导入，保留 SQLite 行为。
4. 同名同版 wheel 引用在安装前通过、发布后被 worker 拒绝：抽出一个纯校验函数供两处共用；保留旧基线、精确版本、传递依赖及错误引用拒绝。

本地生成的渠道资源随宿主更新到 `2.2.3b1` 后，使用原正式 builder 重建。资源身份、SHA-256 及旧配置保护没有放宽；现有渠道 manifest 没有改为 SDK。

### 定向验证结果

- 合并后 **66 个唯一行为案例通过：51 个后端、15 个前端**；另有合并前 4 个小基线通过。
- 覆盖飞书/钉钉真实 worker 的模拟收发、ACL/会话、停用回收与重启、旧配置与卸载墓碑、停止失败保留归属、更新/配置失败恢复、旧插件与 SDK 共存、共享依赖拒绝与真实 worker 审计。
- Console `tsc -b --noEmit` 通过；手工修改的 Python/JSON/TS 解析与对应格式、Python pyflakes 检查通过。
- r5 源码冻结为 23 个手工生产文件，聚合 SHA-256：`27af7301c0c384da1aecf85984c26b6c7885a51d177f7d813824e34dd4056946`。测试作者独立核对无漂移，所有测试进程均终态。
- 功能与测试由不同作者编写，第三位作者独立审查。原始 RED、环境/fixture 原因、修复前后命令及退出码均保留；没有通过 skip、增加超时或放宽业务断言过关。
- `uv 0.8.15` 仅作为临时测试工具准备，wheel 经 PyPI 官方 SHA-256 核对；其本地只读 dry-run 因 macOS sandbox 限制失败后，经自动审批允许的单次沙盒外执行通过。系统和项目生产依赖未改变。
- 验证限模拟平台及临时工作区：未使用真实机器人，未安装桌面版或 Docker，未运行全仓测试，也未构建完整发行产物。现有测试环境 AgentScope 为 2.0.9，PR 的依赖声明为 2.0.10；结论限本次实际执行的合并兼容链路。

详细逐案例证据见本地 `.superpowers/sdd/2026-10-20-pr7565-sdk-compat-merge/test-report.md` 与 `test-passed-cases.json`；独立审查见同目录 `review-report.md`。

### 提交钩子收尾

正常提交钩子发现格式及静态检查项后，没有绕过钩子：编码头、尾逗号与仓库版本 Black 格式均保留；内部 SDK 测试宿主、渠道控制等有意的私有桥接和集中生命周期逻辑，用精确局部说明标明设计边界。实际动态工具的可调用性已核实，没有修改调用行为或全局 lint 规则。

最终 r7 手工生产清单为 25 个文件，聚合 SHA-256 为 `761890e09b6e35811f2423bf7897c10e8261e0383561a7c81d011bb1d6ecead4`。19 个 Python 文件中，18 个相对行为测试冻结 AST 完全相同，另一个仅排序无依赖的标准库导入；3 段测试内嵌源码换行也逐段证明 AST 等价。因此没有重复执行不受影响的行为测试。失败的静态检查已局部复查通过；最终提交仍执行正常仓库钩子。
