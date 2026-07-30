# QwenPaw-Mail 0.1.0 版本汇报

> 版本：0.1.0　　日期：2026-07-27
>
> 本文档汇报 QwenPaw-Mail 邮箱能力的底层实现、与 QwenPaw 智能体的集成方式、现有基础功能，以及可用于演示的典型案例。

---

## 1. Email 功能底层实现（MCP）与 QwenPaw 集成方式

### 1.1 MCP Server 概述

QwenPaw-Mail 的邮箱能力由独立的 MCP 服务 **qwenpawmail-mcp** 提供：

- 基于 **FastMCP** 框架（mcp>=1.28,<2.0）构建 MCP 服务器；
- 通过 **imap-tools** 库（>=1.13,<2.0），以标准 **IMAP/SMTP** 协议与邮箱服务器通信；
- 支持 **RFC 2971 ID 命令**（网易系域名登录必需）与 **IMAP modified UTF-7**（中文文件夹名编码）；
- 内置域名（自动路由 IMAP/SMTP 服务器）：**163.com / 126.com / yeah.net / qq.com / foxmail.com**；其他域名可在运行时通过 `set_credentials` 指定 `imap_host`/`smtp_host` 接入。

### 1.2 工具能力全景（22 个工具）

MCP server 当前共实现 **22 个工具**，覆盖「收发搜删分类 + 线程聚合 + 统计分析 + 账户管理」四个维度，按功能分组如下：

**邮件读取与检索（6 个）**

| 工具 | 说明 |
|------|------|
| `list_folders` | 列出邮箱所有文件夹，中文文件夹名自动从 IMAP 修改 UTF-7 解码 |
| `list_messages` | 按文件夹列举邮件（最新在前），支持 limit/offset 分页，仅返回信封元数据 |
| `get_message` | 按 UID 获取单封邮件完整内容（text/html 正文 + 附件元数据），不下载附件内容 |
| `search_messages` | 按关键词、发件人、日期范围检索邮件，keyword 支持 UTF-8，返回匹配 UID 列表 |
| `get_attachment` | 按文件名或索引下载附件，支持 base64 返回或保存到本地路径 |
| `get_mailbox_stats` | 邮箱统计（最近 N 天）：收发总量、未读/标记数、Top 发件人/收件人、日趋势、响应时长、待回复线程、附件统计、最大邮件 |

**邮件发送与回复（3 个）**

| 工具 | 说明 |
|------|------|
| `send_message` | 发送纯文本邮件，支持 to/cc/bcc（数组或逗号分隔字符串），支持中文主题与正文 |
| `reply_message` | 回复单封邮件，自动设置 In-Reply-To/References 头和 Re: 主题前缀 |
| `forward_message` | 转发邮件（作为 message/rfc822 附件），自动设置 Fwd: 主题前缀 |

**邮件操作与管理（4 个）**

| 工具 | 说明 |
|------|------|
| `mark_messages` | 标记邮件为 read/unread/flagged/unflagged，uids 参数支持字符串或数组 |
| `move_message` | 移动单封邮件到目标文件夹 |
| `delete_message` | 永久删除邮件（设置 \Deleted 标记 + EXPUNGE） |
| `create_folder` | 新建文件夹，中文名自动编码为 IMAP 修改 UTF-7 |

**账户与凭据管理（4 个）**

| 工具 | 说明 |
|------|------|
| `check_auth` | 诊断凭据：执行 IMAP/SMTP 登录测试（IMAP 含 RFC 2971 ID 命令） |
| `create_mailbox` | 新邮箱注册引导：校验用户名、生成备选名、返回注册 URL 和分步指引（需浏览器完成） |
| `set_credentials` | 运行时设置/更新凭据（email + 16 位授权码），无需环境变量注入；非内置域名可指定 imap_host/smtp_host |
| `clear_credentials` | 清除运行时凭据，回退到环境变量或等待重新设置 |

**线程管理与统计（5 个）**

| 工具 | 说明 |
|------|------|
| `list_threads` | 列出对话线程（最新在前），按 In-Reply-To/References 或规范化主题+参与者分组，支持按标签/发件人/收件人/主题/日期筛选 |
| `search_threads` | 按关键词搜索线程，结果按命中数和时间排序 |
| `get_thread` | 获取单个线程的全部邮件（最旧在前），返回信封列表 |
| `update_thread` | 在线程上新增/移除自定义标签；系统标签（inbox/sent/spam/trash）只读（由文件夹推导） |
| `delete_thread` | 将线程所有邮件移入垃圾箱，自动检测 trash 文件夹名变体（Trash/已删除/Deleted Messages 等） |

另有 1 个工具 `get_credential_status`（查询凭据设置状态）当前处于注释禁用状态。

### 1.3 关键实现要点

- **参数宽容化（LLM 容错）**：
  - `coerce_str_list()` 将 LLM 传入参数自动归一化为字符串列表，兼容真实数组、JSON 字符串数组、逗号分隔字符串、单个字符串、中文全角逗号（`，`），应用于 to/cc/bcc/labels/uids 等参数；
  - `coerce_int()` 将字符串数字转换为整数并约束在合法范围（如 limit 1-100、offset 0-10000）；
  - 所有工具异常统一转换为 ToolError，客户端收到 `isError=true` 与用户友好的错误消息。
- **ThreadStore 线程状态持久化**：本地 `thread.db` 存储线程状态与自定义标签，使标签和线程关系跨会话存活；支持 UIDVALIDITY 变化检测（服务器端 UID 重编号时自动重置本地基线）。
- **中文文件夹 UTF-7 编码**：文件夹创建/移动/列举时对非 ASCII 名称统一进行 IMAP modified UTF-7 编解码，中文文件夹全链路可用。
- **RFC 2971 ID 命令**：登录网易系邮箱（163/126/yeah.net）时按其要求发送 ID 命令，保证连接稳定。

### 1.4 QwenPaw 集成机制

- **Driver Card（stdio 子进程）**：QwenPaw 在智能体工作区内自动生成 `drivers/mcp/qwenpawmail.yaml` driver card，以 **stdio 子进程**方式启动 MCP server（command 为 MCP 虚拟环境 Python，args 为 `-m qwenpawmail_mcp`）。
- **工具暴露与命名**：MCP 注册的 22 个工具在 QwenPaw 智能体中自动以 **`qwenpawmail__` 前缀**暴露，例如 `qwenpawmail__send_message`、`qwenpawmail__list_threads`。
- **凭据注入**：当 agent.json 中邮箱凭据完整且非新账户时，driver card 的 `endpoint.env` 注入 `QWENPAWMAIL_EMAIL` 与 `QWENPAWMAIL_AUTH_CODE`；新账户或凭据不完整则不注入（支持"无凭据启动"，此时仅 `create_mailbox` 可用）。
- **运行时凭据**：智能体也可调用 `set_credentials` 在运行时于内存中设置/覆盖凭据，优先于环境变量；MailClient 延迟初始化，首次调用凭据相关工具时才创建连接。
- **状态目录**：`endpoint.env` 同时注入 `QWENPAWMAIL_STATE_DIR` 指向智能体工作区的 `workspace/mail_state` 目录，线程状态随智能体工作区持久化；未设置时默认落在 `~/.qwenpawmail-mcp/state/<email>/`。

---

## 2. 现有基础功能介绍

### 2.1 会话内聊天管理邮箱

用户在 QwenPaw 会话中用自然语言描述目标，智能体自主规划并串联邮箱工具完成任务，无需用户了解任何 IMAP/SMTP 细节。

**示例一：条件搜索 + 阅读 + 归档**

用户输入："帮我找一下上周来自 `xx@example.com` 的所有邮件，把其中有『发票』的都移到『票据』文件夹。"

智能体自主执行链路：

1. `qwenpawmail__search_messages(from_address='xx@example.com', since='2026-07-22', limit=50)` 获取候选邮件 UID；
2. 对每个 UID 调用 `qwenpawmail__get_message(folder='INBOX', uid=UID)` 读取邮件正文；
3. 本地判断正文是否包含「发票」；
4. 对匹配邮件调用 `qwenpawmail__move_message(folder='INBOX', uid=UID, target_folder='票据')`，目标文件夹不存在时自动创建（中文名自动 UTF-7 编码）。

**示例二：线程回顾 + 回复 + 标签**

用户输入："找到关于『XX 项目合作』的邮件往来，总结进展；如果对方最后一封还没回，起草催办回复并打上『跟进中』标签。"

智能体依次调用 `search_threads` 定位线程 → `get_thread` 按时间正序通读 → 总结并判断最后发言方 → `reply_message` 发送回复 → `update_thread` 添加自定义标签「跟进中」。

若配置了邮件推送，相关操作还会生成 `new_email` / `auto_handled` 事件进入收件箱，用户可在 Inbox 页面点开「处理过程」查看完整工具调用链。

### 2.2 自动响应管理邮箱

#### 监听机制：IMAP IDLE 实时监听 + 轮询降级

**MailMonitorService** 在后台 worker 线程维持一条长连接 IMAP 会话，通过 **IDLE（RFC 2177）** 检测新邮件：

- IDLE 超时时间按服务商差异化配置：网易系（163/126/yeah.net）25 分钟（RFC 标准），QQ/Foxmail 2 分钟（该类服务商不可靠推送 EXISTS，需短节拍兜底检查）；
- IDLE 连续失败 3 次后自动降级为 **NOOP + UID SEARCH 轮询**（默认间隔 120 秒）；
- 每轮处理流程：连接、LOGIN、RFC 2971 ID（网易必需）、SELECT INBOX → 读取 UIDVALIDITY 并与本地持久化值比对（变化则重置基线）→ UID SEARCH 与 `_last_uid` 对比找出新邮件 → 逐封处理并更新基线。

#### 四种推送模式与四种规则动作

**推送模式**（`push.mode`）：

| 模式 | 行为 |
|------|------|
| `off` | 禁用邮件推送，MailMonitorService 不启动 |
| `rules_only` | 只执行规则动作（mark_read/move/notify），不唤醒智能体 |
| `rules_then_agent` | 先执行规则；若命中 `wake_agent` 动作或无规则匹配，则唤醒智能体 |
| `agent_all` | 每封新邮件无条件唤醒智能体 |

**规则动作**（`rule.action`）：

| 动作 | 行为 |
|------|------|
| `mark_read` | 邮件标记为已读 |
| `move` | 移动到指定文件夹（`param` 为目标文件夹名，自动建夹 + UTF-7 编码） |
| `notify` | 发送 `new_email` 事件到收件箱 |
| `wake_agent` | 唤醒智能体（`param` 为附加指令） |

规则匹配字段支持 `from` / `subject` / `content` 三种，`contains` 为大小写不敏感的子串匹配，内容匹配覆盖发件人、主题与正文预览（正文前 2000 字符）。

#### 「确定性规则打底 + AI 兜底」三步管道

每封新邮件依次经过三步处理：

1. **规则匹配与确定性动作**：匹配所有命中规则并依次执行 mark_read / move / notify，记录 `wake_agent` 的附加指令；
2. **按模式唤醒智能体**：由 `should_wake_agent(mode, matched_rules)` 判定，需要时异步提交唤醒协程到主事件循环；
3. **无条件生成 `new_email` 事件**：写入收件箱，payload 携带 uid/folder/发件人/主题/日期/`matched_actions`（命中的规则动作列表）/mode/正文预览。

该设计使确定性动作（快、可预期、零成本）与 AI 智能处理（灵活、能理解语义）分层配合。

#### 智能体唤醒与 `auto_handled` 事件

唤醒时按模板构造 prompt，携带发件人、主题、时间、uid、folder 与规则附加指令，要求智能体"合理调用各项工具解决邮件中的需求，如需回复则最后调用 `reply_message`"，并要求"回复时结合 CONTACTS.md，回复后更新 CONTACTS.md 中的联系人列表"。

智能体运行（`workspace.stream_query`，最长 600 秒）结束后生成 **`auto_handled`** 事件：

- **body**：智能体最终输出摘要（最多 500 字符，取自最后一条 text block 或 tool_result）；
- **payload.trace**：完整工具调用轨迹——遍历会话消息增量，将 `tool_use` 与 `tool_result` 按 id 配对生成 `{type: "tool_call", name: "qwenpawmail__...", summary: "输入 => 结果"}` 条目，并收集 assistant 文本块；最多 50 条，每条摘要最多 200 字符。

**前端展示**：收件箱列表区分展示 `new_email` / `auto_handled` 事件；点开事件详情弹窗查看 body 与 payload；若存在 `payload.trace`，可展开「处理过程」查看完整工具链路，实现自动处理全程可视化、可审计。

#### agent.json 邮箱配置示例

邮箱配置存放于 agent.json 的 `mail` 字段（`AgentMailConfig`），包含凭据（`credential`）与推送监听配置（`push`）：

```json
{
  "id": "assistant-1",
  "name": "邮箱秘书",
  "mail": {
    "is_new_account": false,
    "credential": {
      "name": "myaccount",
      "domain": "163.com",
      "auth_code": "abcdef1234567890"
    },
    "push": {
      "mode": "rules_then_agent",
      "poll_interval_seconds": 120,
      "rules": [
        {
          "field": "content",
          "contains": "发票",
          "action": "wake_agent",
          "param": "这是一封关于发票的邮件，请自动下载附件并整理"
        },
        {
          "field": "from",
          "contains": "noreply@",
          "action": "mark_read"
        }
      ]
    }
  }
}
```

---

## 3. 演示案例

以下案例均基于当前版本真实已实现能力，可直接复现。每个案例仅描述任务与触发方式，并用一行点出串联的能力。

### 3.1 复杂任务演示（多次工具调用 / 与其他 tool & skill 联合完成）

**案例 1：发票报销全自动管家**（推荐主打 Demo）

- 任务：出差族每月收十几封发票邮件。配置规则「内容包含『发票』→ 移动到『票据』文件夹 + 唤醒智能体」；新发票邮件到达后自动归档，智能体被唤醒读取邮件与附件，在工作区写入/更新 `报销台账.md`（日期/金额/开票方）。
- 能力串联：内容规则匹配 × 自动建夹（中文 UTF-7）× 附件下载 × 智能体唤醒 × 工作区文件写入 × trace 追踪。

**案例 2：每周邮箱洞察报告**

- 任务：给智能体建一个每周一 9:00 的定时任务，自动生成本周邮箱洞察报告（总量/未读、Top 发件人、活跃时段），再找出未回复的重要线程列成待办，最后把报告发送到用户自己的邮箱。
- 能力串联：cron 定时任务 × `get_mailbox_stats` 统计 × `search_threads` 线程搜索 × `send_message` 发信。

**案例 3：自学习通讯录（CONTACTS.md 闭环）**

- 任务：陌生联系人（如署名"你主人的同事小王"）来信触发智能体唤醒，回复后自动把小王写入 CONTACTS.md；之后在控制台会话中说"给小王发封邮件问他文档好了没"，智能体从 CONTACTS.md 解析出小王的邮箱地址并直接发信。
- 能力串联：邮件唤醒通道 × 工作区文件读写 × 跨会话记忆 × 自然语言联系人解析。

**案例 4：邮件线程追踪与催办**

- 任务：会话中要求"找到关于『XX 项目合作』的邮件往来，总结双方谈到哪一步；若对方最后一封未回，起草礼貌催办回复并打『跟进中』标签"。
- 能力串联：线程搜索 × 全线程正序阅读 × 智能总结 × `reply_message` 回复 × `update_thread` 自定义标签。

**案例 5：旅行行程自动整理**

- 任务：会话中要求"把携程和航司发来的所有预订确认邮件找出来，整理一份行程单（航班号/时间/酒店/订单号）存成 `行程单.md`，并把这些邮件都移到『旅行』文件夹"；可追加"明早 8 点提醒我值机"。
- 能力串联：多关键词搜索 × 批量读取与移动 × 自动建夹 × 结构化信息提取写文件 × 可叠加定时提醒。

**案例 6：多规则叠加智能分拣流水线**

- 任务：同一智能体叠加三条规则（发件人含 `noreply` → 已读；内容含『账单』→ 移动到『账单』文件夹；内容含『账单』→ 唤醒智能体总结要点），发一封同时命中的邮件，三个动作依次全部执行，`payload.matched_actions` 完整记录动作列表。
- 能力串联：规则叠加 × 动作顺序执行 × 事件追踪 × 「确定性规则打底 + AI 兜底」分层决策。

**案例 7：验证码即时提取播报**

- 任务：配置规则「内容包含『验证码』→ 唤醒智能体，附加指令『提取验证码并通知我』」；网站验证码邮件到达后智能体自动从正文提取验证码，结果直达收件箱事件详情（QQ/Foxmail 节拍下全程延迟 ≤2 分钟）。
- 能力串联：规则匹配 × 实时监听 × 正文文本提取 × 收件箱通知。

### 3.2 专用垂域场景演示

**垂域 1：HR 招聘——候选人简历自动分拣与面试安排**

- 任务：配置规则「主题/内容含『简历』或『应聘』→ 移动到『简历』文件夹 + 唤醒智能体」；智能体读取简历邮件（含附件元数据），提取候选人姓名、岗位、联系方式写入工作区 `候选人台账.md`，回复标准面试邀约邮件，并结合定时任务创建面试前提醒。
- 能力组合：规则分拣 × 自动建夹 × 智能体唤醒 × 附件处理 × 台账文件写入 × 回复邮件 × 定时提醒。

**垂域 2：财务——月末对账邮件自动归集与对账摘要**

- 任务：配置规则「内容含『账单』/『对账』→ 移动到『账单』文件夹」；月末通过定时任务唤醒智能体，搜索本月账单类邮件并逐封阅读，汇总各方金额生成对账摘要，用 `send_message` 发送给财务负责人。
- 能力组合：规则归集 × cron 定时任务 × 搜索与批量阅读 × `get_mailbox_stats` 统计辅助 × 汇总发信。

**垂域 3：销售/客服——客户咨询实时响应与跟进管理**

- 任务：模式设为 `rules_then_agent`，客户咨询邮件到达即唤醒智能体；智能体结合 CONTACTS.md 识别是否为老客户及其历史背景，自动起草个性化回复，并用 `update_thread` 为该线程打上『跟进中』标签，方便后续用 `list_threads` 按标签筛选跟进。
- 能力组合：实时唤醒 × CONTACTS.md 客户识别 × 自动回复 × 线程自定义标签 × 标签筛选。

**垂域 4：学术/科研——审稿与会议通知的截止日期管家**

- 任务：配置规则「主题/内容含『review』/『审稿』/『deadline』→ 唤醒智能体」；智能体读取期刊审稿邀请或会议通知邮件，提取截止日期与任务要点，创建对应的定时提醒任务，并将邮件移动到『学术』文件夹归档。
- 能力组合：规则匹配唤醒 × 正文关键信息提取 × 定时提醒创建 × 自动归档。

---

## 4. 已验证质量保障与已知边界

**质量保障**

- MCP server 单元测试与 QwenPaw 侧 E2E 测试已覆盖工具调用、参数宽容化、监听与事件链路等主要路径，**295+ 测试通过**；
- 全部 7 个复杂演示案例已验证可复现。

**已知边界**

- `get_message` 仅返回附件元数据，附件内容需显式调用 `get_attachment` 下载；
- 内置自动路由域名限于 163.com / 126.com / yeah.net / qq.com / foxmail.com，其他域名需通过 `set_credentials` 手动指定 IMAP/SMTP 服务器；
- QQ/Foxmail 的 IMAP 服务器不可靠推送 EXISTS 通知，实时性依赖 2 分钟短节拍兜底，最坏延迟约 2 分钟；
- `send_message` 当前发送纯文本邮件；系统标签（inbox/sent/spam/trash）为只读，仅自定义标签可增删。
