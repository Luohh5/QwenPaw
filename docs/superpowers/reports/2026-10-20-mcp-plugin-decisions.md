# MCP 插件化实施决策记录

对应[实施方案](../plans/2026-10-20-mcp-plugin.md)。下表保留实施期间对方案空白、兼容性冲突及验证方式的裁定，便于后续修改时理解原因。功能完成情况以[验证记录](2026-10-20-mcp-plugin-validation.md)和方案 checklist 为准。

## Rulings I made

### 工作方式与迁移判定

| 决策 | 原因 | 判断有误时的代价 |
| --- | --- | --- |
| 在用户指定目录和 `codex/mcp-plugin` 分支工作 | 用户明确指定，不另建 worktree | 需要迁移本地工作 |
| 功能作者与测试作者分开，新增行为先取得独立失败证据 | 遵守用户要求，避免实现与测试互相迎合 | 文件边界协调失误会造成重复修改 |
| 预先固定不可变迁移快照 DTO，后续增加可选末尾字段 `config_path` | 独立作者需要统一接口；显式路径保留原始配置上下文 | 调整小范围内部适配器 |
| 不透明历史 `static:mcp/anysearch` 引用判为 `needs_review` | 不解密凭据就无法证实它只是默认数据；不能据此自动启用 | 这种少见旧配置需要用户手动确认安装 |
| 分类忽略非 MCP 卡片，只检查 MCP 内部重复项 | 其他协议不能触发 MCP 安装 | 补充迁移分类检查 |
| 监测需求和旧配置转换复用完整版本指纹及原始配置证据 | 名称、URL 或补齐默认值后的配置不足以区分自定义停用来源 | 调整共享分类适配器 |
| 在通用配置修复之前捕获、校验原始快照 | 避免默认值覆盖旧根目录和未激活工作区的使用证据 | 调整启动顺序 |
| 根配置损坏时先安全终止；单个工作区损坏时保留证据并继续有效工作区 | 自动修复不能先抹去不确定的迁移证据 | 增加人工配置恢复步骤 |

### 公开 SDK 与资源生命周期

| 决策 | 原因 | 判断有误时的代价 |
| --- | --- | --- |
| Task 2 提前实现最小注册所有权桥，传输与治理留给 Task 3 | 注册测试应验证真实重复注册和释放，避免空实现 | 移动少量内部代码 |
| `PluginTestHost.open_driver` 接收原始 DriverCard JSON，并提供工作区和会话范围 | 测试需要表达持久策略、凭据声明以及瞬态连接 | 增加公开重载 |
| 增加只读 `DriverCredentialAccess.aliases` | MCP 隐式认证需要枚举已声明别名，不能暴露任意凭据引用 | 调整一个 SDK 属性 |
| 增加 `DriverContext.subprocess_environment()`，只返回显式持久环境变量 | 保留 stdio 原有环境语义，避免改成暴露整个宿主 `os.environ` | 调整一个 SDK 方法 |
| 增加有限只读 `resolve_runtime(alias)`，原始 `resolve` 继续提供版本记录和 CAS | 复用通用凭据提供者，保留依赖私有引用的 AKSK 签名及 OAuth 刷新语义 | 调整 SDK 方法和内部适配器 |
| 宿主每次委托协议发现工具，MCP 插件保留十秒发现缓存 | 宿主永久缓存会让工具变化失效，不应为 SDK 添加 MCP 专用定时器 | 调整协议缓存 |
| 迁移原有细粒度传输测试，不强行全部改为端到端宿主测试 | 保留取消、重连、关闭等原有边界覆盖 | 后续维护测试路径 |
| 取消关闭失败后保留真实 HTTP 客户端；成功关闭时仍释放 | 资源必须可重试且有归属，不能为满足旧内部断言而丢掉引用 | 调整严格关闭契约 |
| 有界取消后仍保留未结束 RPC 的清理所有权，严格关闭不能虚报完成 | 原有放弃 RPC 的行为与本次无孤儿资源要求冲突 | 维护小范围清理账本 |
| 内部结果断言改读有限 `DriverCallResult.structured_content` | 新 SDK 不暴露旧内部结果包装类型；业务数据不变 | 调整断言路径 |
| SDK 凭据错误只返回安全别名诊断 | 原始存储错误可能包含私有引用或秘密 | 需要更细的安全诊断映射 |
| 宿主 CAS 在同一原子写入中更新 `updated_at` 并保留其他 metadata | 原 OAuth 写入的时间戳责任转移到宿主，不能让插件进行第二次写入 | 调整通用 CAS metadata 契约 |

### 按需运行与升级恢复

| 决策 | 原因 | 判断有误时的代价 |
| --- | --- | --- |
| 保留两秒扫描，通过协议注册状态及工作区监测需求启停 | 遵守用户修订；不用 MCP 的用户减负，使用者保持手工编辑体验 | 后续另行改造监测机制 |
| 空工作区的显式监测需求通过轻量 `.monitoring.json` 持久化 | 不能因全局启用插件就扫描所有空工作区；需求应跨重启保留 | 调整私有 metadata 格式 |
| 工作区在真实发布、reload 提交之后才激活协议运行时 | 保留严格对象身份检查，候选工作区不能提前连接 | 调整发布顺序挂钩 |
| 指定停用来源的能力查询保留 `DriverNotFoundError`；全局空查询返回 `[]` | 保留既有消费者语义，两个路径均不创建 Manager | 调整单个异常路径 |
| 卸载意图使用独立 sidecar，配置中仍移除插件行 | 兼容 SDK 1.0 卸载行为，同时持久记住显式卸载 | 调整 sidecar 格式 |
| 使用现有安装事务，增加 origin UUID、代码摘要和 `user_requested` | 离线自动升级需要区分用户代码与本次事务，不引入第二套复制框架 | 调整事务所有权 metadata |
| 修复旧转换器遗漏 `tools` 的问题 | 本次升级必须保留非空白名单、`[]` 全禁用和 `None` 不限制 | 小范围旧配置字段迁移 |
| 用户明确安装并启用的空 MCP 插件仍可注册；Manager 和扫描保持零 | 不能在每次重启后迫使这种用户重新启用插件 | 调整启动选择规则 |
| origin 存在但全局事务记录丢失时判为 `needs_review` | 无法证明完成提交，不能当作用户代码自动加载 | 少见 metadata 丢失需要人工恢复 |
| 显式重装在激活之前解除启动阻止，提交失败则恢复阻止 | 已保留的启用来源应立即恢复，无需手动刷新 | 调整激活与回滚顺序 |
| 成功显式安装后写入单个通用、代码摘要绑定的 commit receipt | 新 loader/CLI 也须能确认成功重装，退休旧自动事务；无凭据、摘要错误、代码改变仍保守处理 | 调整 receipt 格式和协调逻辑 |
| 安装前剥离源目录中携带的 receipt；自动安装不生成显式提交凭据 | receipt 不能被复制后冒充本次用户提交；停用意图始终优先 | 调整安装 metadata 清理 |

### MCP 入口、OAuth 与前端

| 决策 | 原因 | 判断有误时的代价 |
| --- | --- | --- |
| 保留 `/mcp` 导航、路由及初始界面，在原页面安装或启用 | 遵守用户修订，保证用户仍能发现功能 | 调整页面承载方式 |
| 保存配置和 OAuth 状态的被动读取走 metadata；可用插件的显式修改保留正常激活 | 访问页面不能连接服务；主动创建、启用和编辑仍应即时生效 | 调整读写路由分支 |
| 插件公开 `route.wrap` 承担管理准入和工具栏，核心保留一份表单与页面壳 | 避免重复 UI，同时让插件实际拥有管理扩展 | 后续移动部分管理界面 |
| 使用独立 `/api/mcp-plugin` 前缀 | 原 `/mcp/{client_key:path}` 允许 `plugin` 和 `plugin/install` 等客户端名 | 调整新增端点 |
| 旧 OAuth 路由经私有 dispatcher 创建新上下文，路由只保留有界授权关联 | 不保留跨请求的 live context、凭据对象或连接，仍保留旧回调入口 | 调整内部桥接 |
| 撤销后可保留别名，清除 access 和 refresh token | 保留配置关联，同时确保授权失效 | 调整存储表示 |
| 回调失败也消费 state，重放被拒绝 | 工作区、来源、代际、过期和单次使用要求优先于旧失败后保留会话行为 | 用户需要重新发起授权 |
| 保留无 token 状态的旧 DTO：未授权、过期值零、空 scope | 不能用新测试凭空改变旧业务响应 | 调整测试 fixture |
| 前端工作区切换和重试重建取消控制器时清除旧 busy | 已取消的生命周期请求不能永久禁用新页面操作 | 调整请求状态管理 |

### 验证方式与限制

| 决策 | 原因 | 判断有误时的代价 |
| --- | --- | --- |
| 导出的 SDK 测试使用当前包及唯一匹配 metadata 的窄隔离根 | 仓库相对 `src` 路径在真实 wheel 环境无效；保留原有字节、版本和不变更断言 | 维护测试隔离工具 |
| 依赖镜像排除外部 QwenPaw、editable 和现有 `.pth`；Windows 无 symlink 时复制 | 测试必须证明当前发行物，不能误用其他 checkout | 维护跨平台测试准备 |
| 暂不增加未经复现的“metadata 按包名仅取第一个”去重 | 它可能隐藏真实多版本歧义，当前 source/wheel 用例已通过 | 若出现具体环境问题再修 |
| 修复五个可移植性失败及安装字节守卫后，完整重跑一次 SDK 发布命令 | 分段成功不能代替要求的真实发布命令 exit 0 | 额外约六分钟执行成本 |
| 声明继承的 `p1` marker；不全局压制第三方弃用或 JSDOM 警告 | 清理可控警告，同时保留诊断可见性 | 未来升级依赖仍需处理警告 |
| 禁止子进程的冷启动 fixture 使用 Popen 子类 | 普通函数替换会破坏上游 `Popen[bytes]` 类型注解；零进程断言不变 | 维护测试 fixture 兼容性 |
| 为既有 UV 用例提供实际工具路径及已授权的 macOS 系统访问 | 环境缺失不能通过删除 resolver 断言来解决 | 测试环境准备成本 |
| 性能测量只包含真实 Workspace 的 Driver 相关服务描述符 | 隔离本次 MCP 减负，排除模型、频道等无关网络服务 | 不能据此推断整个应用的性能变化 |
| 作废首轮六条不完整扫描计数数据，增加直接枚举和 snapshot 计数后重测 | 仅统计 `AsyncStore.list_paths` 会漏记真实扫描 | 额外采样时间 |
| 作废第二轮十四条缓存不对等数据，为每个子进程设置全新 `PYTHONPYCACHEPREFIX` 并禁止写缓存 | `-B` 不禁止读取已有 `.pyc`；基线无缓存而当前源码有历史缓存 | 额外采样时间；保留两轮无效数据作诊断 |
| frozen 验证采用 spec 资源布局的冷启动子进程，并明确限制 | 当前环境没有 PyInstaller，不能声称构建并验证了原生可执行程序 | 原生打包、签名和目标平台启动仍需发布流水线验证 |
| 既有通过的广泛回归不因 fixture 修复反复全跑；重跑全部受影响用例 | 记录初次失败和修复结果，避免将覆盖证据伪称为一次全绿运行 | 若影响面判断有误，需要补充相关回归 |
| 持久保存全部三十条测量 JSON，只规范化机器绝对路径，并保留原始文件 SHA256 | 保留可检查的全部指标、环境及来源摘要，避免重复二进制归档和机器路径 | 临时原件清理后若需逐字节取证，还需另行取得原件；指标保真在交付前校验 |
| 未使用用户每次启动只读分类，不写一次性 unused 完成记录 | 后续新增配置仍需重新盘点；完成标记增加状态和写入，却不能省去必要读取。修正规划中的一次性记录措辞 | 保留每次启动的盘点 I/O；没有 MCP 周期扫描和运行时初始化 |
| 候选版本与已提交活动版本分离；旧连接仅可继续对应已接受且权限未变的替换快照 | 失败替换不能让旧连接失效；同时不能容许未知文件修改、停用、权限或凭据变更绕过失效检查，也不能回滚覆盖新编辑 | 调整内部版本和快照治理；权限变化仍保守拒绝旧调用 |
| 通用 AgentScope 转换保留 `file.text` 正文并继续保留附件 | SDK DTO 保真不等于最终工具结果保真；无需修改已有 DTO 或让宿主解析 MCP 私有类型 | 调整通用内容转换规则 |
| 在协调器内存中保留安全结果，状态接口增加升级和所选工作区诊断，原页面显示恢复提示 | 早期失败没有 PluginRecord；全局插件健康也不能掩盖单工作区阻止。复用现有协调锁和显式重试，不新增持久化框架 | 调整新增状态字段和提示映射；自由格式错误不得进入响应 |
| 保留三十条终审前性能样本及其原始源码摘要，明确版本；终审后验证无 MCP 工作区仍为零运行时工作 | 来源替换修复不应伪造已有测量的源码身份；数值表作为历史实测保留，当前零工作保证另行验证，不重复无关数值采样 | 最终补丁的 CPU/RSS/启动时间没有新的三十轮数值；若要量化最新补丁，需再次完整测量 |
| 复审发现真实的重试恢复漏项后，增加一个限定范围的完成修正 | 用户要求全部 checklist 完成；不能因工作流的一轮修正上限就保留已证实的正常使用故障。保留首次复审失败记录，仅修正资格刷新后的活动工作区投影 | 增加一次定向修正和复核时间，不重新开展整分支审查 |

用户指定保留当前开发分支，因此最终交付保留本地分支，不自动合并、push 或创建 PR。原生打包限制和实际测试平台范围必须随验证记录一起交付。

## 按实施顺序保留的裁定原文

以上表格列出决定、原因和判断错误的代价。以下按原始实施顺序保留包含裁定的日志行，包含同一阶段的上下文；测试结论以最终验证记录为准。

### 1

```text
- Ruling: work in the current checkout and branch, not a new worktree — user explicitly requested this folder and named branch — wrong choice would require relocating uncommitted work.
```

### 2

```text
- Ruling: split every task into a test-author agent and a production-author agent; production waits for RED evidence — direct user requirement overrides a single-implementer skill template — wrong coordination could cause overlapping file edits, so roles have disjoint ownership.
```

### 3

```text
- Ruling: snapshot DTO names/fields are fixed in task-1-contract.md — the plan leaves exact snapshot shape open and independent authors need one contract — wrong shape costs a small adapter change before Task 6.
```

### 4

```text
- Ruling: default-looking AnySearch with only an opaque historical static:mcp/anysearch binding is needs_review — without decrypting secrets its history is ambiguous, so do not assume unused or auto-enable — wrong classification costs a manual install prompt for this narrow legacy case, but preserves all data. Confirmable env-backed default fixtures remain unused.
```

### 5

```text
- Ruling: inventory may contain valid non-MCP cards; ignore them and check duplicates within MCP only — classification must not force MCP installation for unrelated protocols — wrong filtering can require a follow-up migration check.
```

### 6

```text
- Ruling: registration ownership bridge may start in Task2 to test real duplicate/release behavior, with transport/governance in Task3 — avoids a fake public registration stub — wrong boundary costs a small internal move only.
```

### 7

```text
Task3 base34007fcdf. Ruling: public testhost open_driver accepts a raw JSON DriverCard mapping (name/protocol/endpoint/credentials/config/policy/enabled), optionalsession_scope selects transient vs persistent; fakeplugin only publicSDK — source setup needs persistedpolicy/credential declarations absent from runtime DriverSource — wrong choice costs an overload before SDKrelease.
```

### 8

```text
- Ruling: add readonly DriverCredentialAccess.aliases property — MCP implicit auth and bare legacysecretrefs require enumerating declaredaliases, which sourceDTO omits — wrong choice costs a small APIaddition before1.1release; no arbitrarycredentialrefs exposed. Missingproperty RED verified1fail0.25s beforeimplementationrelease.
```

### 9

```text
- Ruling: add explicit DriverContext.subprocess_environment() for readonly persistedconfiguredvars — originalstdio passes load_envs, replacingwithos.environ would expose morehostsecrets — wrong choice costs a narrow SDKmethod before1.1release; no implicitenvreads or arbitraryhostservices. Missingmethod RED verified1fail0.23s beforeimplementationrelease.
```

### 10

```text
Task4 preparation ruling (no implementation begun): retain detailed transport unit assertions by rehoming to plugin alongside public-host integration tests; requiring every low-level state-machine unit assertion to pass through broad host routing would erase useful regression granularity. Task5 removes old core transports after equivalent plugin coverage; final one maintainedimplementation. DriverCredentialAccess resolve raw records, therefore plugin must preserve OAuth refresh viaCAS, not assume host providers refresh rawtokens. Draft contract task-4-contract.md pins managementroute shapes for laterTask7 bridge.
```

### 11

```text
Task4 authors: /root/task4_tests tests-only; /root/task4_code production-only, waiting explicitindependentRED. Standardmodelhigh for migration/integration with contractfixed; no Task4host/SDK changes without ruling.
```

### 12

```text
Ruling: add finite DriverCredentialAccess.resolve_runtime(alias)->Awaitable[Mapping[str,JSONValue]], rawresolve remainsversionedrecord/CAS — existingAKSKprovider HMAC includesprivatecredentialref, so rawcredentials alonecannotpreservehistoricalbindings; reusegenericproviders alsoavoidsduplicatingOAuthrefresh inMCPplugin — wrongchoicecosts smallSDKmethodrework before1.1release. Readonlyderivedruntimevalues only, declaredalias/lifetimechecks, lazyownedprovidercleanup, no arbitraryrefs or implicitnetworkonrawresolve. Task4hostscope narrowlyextended plugins/drivers.py and _drivers.py, testauthorstrictfixture. IndependentmissingmethodREDpending, no methodimplementationbeforeRED. This supersedes priorrequirement that pluginreimplementgenericOAuthrefresh; pluginstillowns MCP OAuthnetworkmanagement.
```

### 13

```text
Ruling: hostcatalog mustdelegatediscover perrequest; protocolownsdiscoveryTTL10 ratherthanhostforevercache — independentpublicMCPtoolinventorytestafter+11seconds confirmedstale['echo'] (task-4-tests-ttl-cas.txt1fail1pass0.89) — wrongchoicecostsprotocol-localcaching work, avoidsSDKTTLfield/backgroundtimer. Rootreleasednarrow _drivers.pyfix preservingawaitguards/currentmetadata. CASactualMCPblockedrefresh vsnewerstorewriter alreadyPASS; correctedruntimeownershiptest2PASS0.68 aftermethodimplementation, priorfixture403 notclaimedfeatureRED.
```

### 14

```text
Ruling: change one ported cancelled-aclose assertion from dropped _http=None to retain exactHTTPclient untilsuccessfulretry, preservingallotherassertions — oldcleanupassertioncontradictsrequiredretryablecustody; newtestindependentlyreproducedlostclientbeforefix — wrongchoicecostsrevisitingclosecontract, retainsresourceinstead ofsilentlyorphaningit. KeepoldOAuth401fallback _implNoneassertionwhenstrictcleanupactuallysucceeds; authorfixedunnecessaryretention. Approvedinertplugins/protocol/__init__.py todisambiguateprojectpackagefromthirdparty mcp forpylint, noexecution/discoverypathchange.
```

### 15

```text
Ruling: preserve retryable custody of unfinishedRPCs instead ofportedabandon/removal — bindingno-leak/strictcleanup rule takesprecedenceoverinheritedtransportbehavior — wrongchoicecostssmallcleanup-ledgerrefactor, normalrequestcancellationmustremainbounded. Reviewernamedplan-mandatedconflictsettledbeforefix; no broadprotocolstackrewrite.
```

### 16

```text
Task5base764484a4c; contracttask-5-contract.md, brief/notesprepared. Ruling: explicitblankworkspace monitoringintent hasprivate request_monitoring(protocol) seam andlightweightpersistentworkspace metadata — avoidglobalpluginenablewatchingallemptyspaces whilepreservingintentthroughrestart — wrongchoicecosts smallmetadata-layoutmigration; nonewmandatoryuserpreference, pagevisitdoesnotrequestmonitoring.
```

### 17

```text
Task5additionalcompatREDrootverified: SDKbridgegovernance driver_errorinsteadlegacydriver_policy_denied (task-5-tests-progress.txt), whitelisttool_disabled+driver/toolmetadata (task-5-tests-rehome-unit.txt39pass2fail). Productionrestoresalllegacyresultcodesfrombaseline. Ruling: adaptdirectinternal .value dict assertions topublicDriverCallResult.structured_content whilepreservingallbusinessdata/cases — finiteSDKresultisrequired, internalwrappertypechanges — wrongchoicecostsassertionpathreworkonly, no publicAPIresponsedatachange.
```

### 18

```text
Ruling: reuseTask1fullfingerprints andrawagent.json/card evidence forconfiguredlegacyconversion/defaultfilter — hydratedMCPConfig/name+URLheuristicscanlosecustomdisabledAnySearchintent — wrongchoicecosts smallsharedclassifieradapter; onlyunusedexcludedfrommonitoring, needs_review evidencepreserved, conversiononlyconfigured+registeredMCP.
```

### 19

```text
Ruling: real MultiAgentManager publishesWorkspace AFTERstart/candidatepreparation, so deferredruntimeactivationmustawaitpostpublish/post-reload-commit genericfacade.activate_registered_protocols — strictactualobjectguardmustnotbeweakened — wrongchoicecosts narrow publicationhookorderingrework; noMCPspecialbranch/no candidateconnections beforecommit. ApprovedsourceMultiAgentManagerfileextension; independentactualpipelinecreation/reloadtests requested.
```

### 20

```text
Task5actualMultiAgentManagerinitial/reloadpublicationpipeline testhonestGREEN1pass1.03 aftersourcefix (task-5-tests-publication-red.txt provisionalfilename, notRED); newapp23pass6.71 reported. Productionlaterlifecycle70pass1failnewnamed-disabledcatalogexpectation ambiguous. Ruling: preserveManager semantics fornamed list_driver_capabilities disabled/inactive =>DriverNotFoundError, globalempty list_capabilities=>[]; neitherconstructsManager — existingconsumerresult/exceptioncompatibilitytakesprecedence — wrongchoicecostsoneexceptionpathrework; no reducingzero-resourceassertions. Testauthorupdatesnewcase accordingly.
```

### 21

```text
Task6 preparation only: brief/contract/notes ready. Ruling: preservegeneric successfuluninstall config-row-removalcompatibility byper-plugin durableuninstallintent sidecar underpluginroot, lifecycle-ownedatomicwrite andvalidatedID, ratherthanretainingconfigtombstonerow — wrongchoicecostsnarrowsidecarlayoutmigration, avoidsbreakingSDK1.0 legacyuninstallprobe; startupcannotclearintent, onlysuccessful explicitreinstall/enable. Task6notreleasedbeforeTask5cleanreview.
```

### 22

```text
Task6initialREDrootverified task-6-tests-red.txt6FAIL0.58 expectedmissinginventory/ensure(rawinactive+damaged,4unusedvariants,actualinstall+SDKcallfixture). Productionreleased. Pinnedprepare_startup_plugins(loader,workspace_manager,*,app=None,snapshot=None) adjacentapp/mcp_plugin_upgrade.py returnsimmutableUpgradeResult; inventory/ensurelazyentrypoints inoriginalpuremodule. Ruling: addoptionaltrailingMCPUsageSnapshot.config_path:str|None tohonorexplicitrawinventorycontextandfreshsettingschecks — wrongchoicecostssmallinternalDTOmigration, old3posargs/pureclassifierunchanged. Approvedgenericinstall_origin dictstaged.plugin-install-origin.json UUID/digest beforeexistingcopyrename +user_requested flag(defaultTrue,startupFalse), notnewcopyframework; settingsintenthelpersperID.
```

### 23

```text
Ruling: actual_lifespan'sgenericworkspace migration/load_configrepairprecedebackgroundpluginstartup; capture rawsnapshot beforethosemutations andcarryintohelper, mergefreshrawinventorywithoutlosinguncertainevidence, supportoldrootMCPwithoutprofiles — wrongchoicecostsnarrowbootstraporderingrework, avoidsdefaultinference/missinglegacyusage. MalformedROOTmuststopstartup safelybeforeauto-repairwrites; damagedworkspaceonlycontinuevalidsiblings. Norestore-by-overwriting/no broadunrelatedstartuprewrite. Bothauthorsnotified.
```

### 24

```text
Task6independentprogress-2 25PASS3FAIL3.70 task-6-tests-progress-2.txt verified: two lateupsert/refresh sharedresolverResourceError inactiveSDKcontext, sourcefixactivatesdynamicallyregisteredplugin beforeexplicitruntime; startupregisteronlypreserved. Third reallegacyconverter dropsMCPClientConfig.tools. Ruling: narrowmcp_legacy_config.py conversionfix isrequired despitepreexistingbug becauseupgradeguaranteepreserveswhitelists; retain[]denyall/Noneunrestricted/nonempty semantics, no testexpectationweakening — wrongchoicecostssmalllegacyconversionfieldmigration. Bothauthorsnotified, independenttestauthorcovers3variants.
```

### 25

```text
Task6ruling: genericstartup_blocked_plugins set inload_allapprovedforoff/review/failure/uncommittedownedtarget, notblanketunusedexplicitpreinstalledplugin. ExistingdeliberateinstalledenabledMCPwith0sources mustretainregistration/frontendreadiness onrestart, realmanager/watcher0; no configautoenablewrites — wrongchoicecostssmallstartupselectionrefinementandwouldotherwiseforceusersre-enableeachrestart. Independentactualinstall/restartRED1FAIL1PASS1.19 task-6-tests-intent-marker.txt rootverified andnarrowproductionfixreleased; finalcompletedmarkercommit-interruption controlalreadyPASS. Existingnever-installeddefaultunused0installer/gate testsunchanged.
```

### 26

```text
Task6orphanindependentRED1FAIL1.12 task-6-tests-orphan-red.txt verified actualsettingscommitfault→ownedtarget/origin→deleteglobalmarker→restartincorrectlyunchanged+loaded. Rootreleasedorphanonly, sourcefix2GREEN1.28(withexplicituseremptypositive) task-6-code-orphan-green.txt verified. Ruling: origin-present/globalrecord-missing isneeds_review, notusercode; no mutation/autoload — wrongchoicecostsmanualrecordrecoveryforrareexternalmetadataloss, preservesuncertainownership.
```

### 27

```text
Task6explicitreinstallintegrationruling: cleargenericstartupblockbeforeexplicitloadactivation, restoreonfailedcommit (notafteractivation), so retainedenabledsourcesreopenwithoutmanualrefresh — wrongchoicecostsnarrowactivationrollbackorderingadjustment. IndependentextendedactualuninstallcaseRED1FAIL1.18 task-6-tests-reinstall-red.txt rootverified; producerreleasedafterRED, orphanalreadyfixed. Current34upgradecases(33+orphan; reinstallcaseextended), nofurtherexpansionwithoutnewrealissue.
```

### 28

```text
Task6fixround1ruling: approve singleprotocol-neutralin-target.plugin-install-commit.json receipt(schema/pluginid/randomcommit/digestactualcode) writtenatomicallyonlyaftersuccessfulexplicitcommit/after_load, beforeuninstallintentclear; autoFalse neverwriteswitness. Validcode-boundreceiptmayretirestale pendingautotxnwithoutenabled/configrewrite; offintentstillwins, orphanwithoutreceipt/invalidreceipt/changedcodefailclosed. SurvivesCLI/freshloader; nocoordinatorin-memorycallbackonly/noMCPIDinloader/no2-fileframework. Wrongchoicecostsgenericreceiptlayout/reconciliationrework; samefilesystemtrustmodelasexistingorigin, singlefileavoidsextraatomicboundary. SourceauthorreleasedminimalproposalafterindependentRED; testfrontierbehaviorunchanged, source/testauthorsseparate.
```

### 29

```text
Ruling: interpret Task7 savedconfiguration query no-connection guarantee as READ/metadata paths includinginactiveworkspaces; unavailablepluginwrites persistwithoutconnection, explicitavailableplugincreate/enable/edit retain normalapply/reload — preserve userexperience and distinguish passivepageview from activeuse — wrongchoicecosts narrowwritepathrework. Correctedoverbroad initialcontract reads/saves wording beforeimplementation; bothauthorsnotified.
```

### 30

```text
Task7initialRED rootverified5API failures0.97 (inactiveget_agent startup insteadmetadata/stable503),3Console failures+8oldpass1.33(staleworkspace data/error/loading). Productionreleased. Ruling: approve architecturetask-7-architecture.md GETstate/explicitPOSTtrustedlocalinstall + existinggenericlifecycle; meaningfulpublicroute.wrap ownspluginmanagementadmissionandtoolbarwhilecorekeepsonecopyforms/shell — avoidsduplicatingallUI andsatisfiespublicSDKmount — wrongchoicecosts movingmanagementUIlaterifscopeinsufficient. PrivatefreshPawAppdispatch+boundedroutinggrantapproved,no publicSDKextension/no retainedlivecontexts. Ruling: revokedcredentialaliasmayremainbutbothaccess/refreshtokenscleared, statusunauthorized; oldrepresentation-onlyassertionsadapt. Callbackfailurestateconsumed,replayrejected despiteoldkeep-sessiontest — bindingexpiry/replaysecurityandTask4publiccontract — wrongchoicecostsrequiringnewauthorizationstartafterexchangefailure. Exactbusinesserrorassertionspreserved.
```

### 31

```text
Task7endpointcorrectionrulingbeforeimplementation: GET /api/mcp-plugin, POST /api/mcp-plugin/install separaterouter because /mcp/{client_key:path} accepts plugin and plugin/install — preserveexistingkeycontracts — wrongchoicecostsnarrownewendpointmigration. Bothauthorsnotified, addlegacykeycollisioncase. Architecturefileoriginalproposalretainedasproposal, coordinationcontractauthoritative.
```

### 32

```text
Task7independent61PASS2FAIL actualOAuthconservation runprogress3 rootverified: missingmeta.updated_at; maskedrawstoreexception. Ruling: hostatomicCASupdate stampsupdated_at preservingothermetadata, noSDKmetaAPI/no secondwrite, allowstore.pyextension — oldOAuthmetadataresponsibilitybelongshostnow — wrongchoicecostsgenericCASmetadatacontractrework. Ruling: do NOTrestore rawcredentialstoreexceptionpopuptext, Task4safeerrorboundarypreventssecrets/refs; adaptonlydetailassertiontosafealiaserror, all400/nooverwrite/errorpage/stateassertionsintact plussecretnegative — wrongchoicecostsmoreexplicitdiagnosticmappinglater, notsecretleak. Bothauthorsnotified.
```

### 33

```text
Task7affectedPython289PASS1FAIL20.0 rootread: Task4OAuthrefresh exactmetaequalitynowgetsintendedupdated_at. Approvedonlytimestampbound+preservedallothermeta expectationadaptation bytestauthor; no sourceeditstests. Ruling: legacyGETOAuthstatus ispassive savedcredentialquery aswell, pluginavailable+inactiveworkspace mustmetadataread withoutget_agent; unavailableplugin503 retained. SmallhostcredentialpresentationpermittedlikeMCPconfigadapter, businessnetwork/start/callback/revokepluginowned — wrongchoicecostsreadonlypresentationrefactor, avoids passiveconnection. Testauthorrequestedactualinactive/registeredpluginRED beforestatusedit.
```

### 34

```text
Task8releasedaftercleanTask7review; no newruntimeinterfaces. Ruling: performance frontier realWorkspace lifecycle with driver-relateddescriptors only (baselinebothmanager/watcher,finalfacade) toisolatenoMCPworkspacecostunderidenticaldeps; reportasMCPworkspace-runtime NOTwholeapp memory/CPUclaim — wrongchoicecostsbroaderbenchmarklater, avoidsmodel/channelnetworknoise. Rootbaseline5keyfileshashesverified56be andsavedmanifest; no samplesyet.
```

### 35

```text
Task8externalSDKactual5FAIL322PASS5warnings344.17 rawrootread. All5exportedtransitivefixtures hardcodedparents[3]/src→ModuleNotFoundError; testauthorapprovedcurrentboundqwenpaworigin+solemetadatawithNARROWproject-onlymirror, activeabsolute3rdpartydistributionrootsfiltered/deduped/stdlib+qwenpaw/.pth/finderexcluded andWindowscopyfallback, allbyte/no-mutation/restartassertionspreserved. Sourcebroadalreadyrunningoldcollectioncanfinish; new5sourcefocusedafterfix. Ruling: mandatorySDKreleasegateONEcorrectedfullscriptfreshoutputwithnewguardafter5fixturefix, becauseinitialgatefailedandguardchangedandremainingexportstagesneedactualendtoendrc0; notrepeatpassedunchangedgatescosmetically — wrongchoicecostsextra~6mintoolruntime, ensuresactualreleasecommandcomplete. Sourceprior4tailcontinuationPASSdiagnosticonly. Registerinheritedp1markinpublicisolatedpytestconfig, notblanketsuppression; inheritedthirdpartydeprecationsfinaltriage.
```

### 36

```text
RootfoundCONCRETEbenchmarkcacheasymmetryafter12/30: src/qwenpaw950existing.pyc vsbaseline0; timerstartsBEFOREqwenpawimport, -Bdoesnotdisablecachereads. Ruling: invalidatecurrent12cache-asymmetricrows (preservepreflight) andrestart30uniformfreshperchild PYTHONPYCACHEPREFIX undertempROOT/-B, assertrecordpolicy — startupandRSS/CPUcannotbeattributedsolelyMCPunderunequalcacheconditions — wrongchoicecosts~12minextrawaitbutpreventsfalseperformanceclaims. Userinformed; testauthorrequestedstopownedjobsgracefully/smoke/restart,noproductionorbroadsuiterepeats. Functionalmanager/scan0proofunaffected, no numericconclusionpublished. FinalbenchOnlyremainingmatrixactivitybesidesscopedTask8+finalreviews/docs/checkmarks.
```

### 37

```text
Task8 matched-run owner verified 8/30 complete, each sample>=60s, fresh prefix nonexistent, stop tasks empty; final0Manager/snapshots/directenumerations/tasks/projectMCPimports. Counts_after_start are actually startup+3sec warmup totals; publish that precise label. Owner current benchmark Black/flake8/mypy/Pylint10 all0 /private/tmp/task8-benchmark-cache-hooks.log. Root durable rulings report and final-review handoff prepared, no review dispatched early.
```

### 38

```text
Ruling: publish all30 measurements in durable normalized JSON via docs author, retaining every metric/counter/environment/sourcehash/replicate plus original rawJSON SHA256; strip only user/temp absolute path prefixes, keep exact originals in /private/tmp, no redundant binary tarball — durable reviewable evidence without machine paths or duplicate archive — wrongchoicecosts later needing the originals for byte-for-byte forensic comparison after temp cleanup, while metric parity remains verifiable now. Test author owns scratch handoff; root verifies normalized parity before acceptance.
```

### 39

```text
Ruling: accept repeated read-only startup classification forunusedusers andcorrectspecwording, do notaddunusedcompletionmarker — lateruserconfigurationstillrequiresfreshinventoryandmarkeraddsstate/writeswithoutremovingperiodicwork — wrongchoicecostsstartupinventoryI/O ratherthanpersistentmarkerbookkeeping; allunusedManager/scans/task0andtworestarts remainbinding. M2docs-onlyrootfix, authorsinformed.
```

### 40

```text
Ruling: committedoldsource may stay callable only against exact accepted replacement snapshot with unchanged effectiveenabled/protocol/credentials/policy/tools; candidatefreshnessseparatefromcommittedidentity, no unknownrawedits/policyrevocation bypass andno persistedrollback — preparesafelywhilepreservinghostauthority — wrongchoicecosts internalrevision/snapshotguardrework; changedauthorityremainsfailclosed evenifcandidatefails. I2genericconsumerfile.textadditiveapproved, DTOunchanged. I3additivefiniteupgrade/workspacestatusschemaapprovedunderexistinglock; safeactivewarningcode mustnotfalselyclaimruntimefailed; sourceknowsregister-onlyloadednotactive. Tests/M1ownertestagent; allcoveringPython/Console tests bytestauthor, productionhooks/Consoleformatbycodeauthor, releaseartifactrefreshrootafterfreeze.
```

### 41

```text
Ruling: preserve numeric30samples as PRE-final-review measuredsource withoriginalSHA, neverrewritecurrentidentity; usepostfix1/10/50shortcounter-only actualdescriptorchecks forcurrentzero-workguarantee, no another30numericsforactive-replacementfix — unusedpathsneverconstructManager, historicalCPU/RSS/startupalreadybounded/no materialclaim — wrongchoicecostslackofnew30roundlatestpatchnumericperformance, requiresfullrebenchifwanted; documentsexplicitqualification. CurrentwheelmustberebuiltandfullSDK/frontendactualnewartifactgatesbecausepackagedruntimebyteschanged; old327+oldwheelhistorypreserved.
```

### 42

```text
Ruling: complete this realload-bearingresidual with narrowlyscopedI3 amendment andfocusedfollowup, despite SDD final one-fix-wave cap — higher-prioritydeveloperpersistenceanduserexplicitallchecklistscomplete prohibitparkingknownunfixednormalretrybehavior whenminimalpathisconcrete — wrongchoicecostsextratargetedfix/reviewtime, not anotherbroadbranchreview; preserveoriginalverdict/evidence anddo notpretendfirstwavefullyaddressedI3. OtherI1/I2/M1/M2/verificationsnotreopened. NewwheelwaitsfinalPythonstable.
```
