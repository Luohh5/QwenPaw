---
name: mailbox
description: "当用户需要任何邮箱/邮件操作时使用此技能——包括查看、阅读、搜索、发送、回复、转发、整理或删除邮件，管理会话线程，以及绑定已有邮箱账号或注册新邮箱。此技能是所有邮件任务的**统一且唯一入口**，利用 qwenpawmail-mcp MCP 服务器提供的各项工具编排操作（支持 12 个邮箱服务商，IMAP/SMTP 协议自动路由）。"
metadata:
  builtin_skill_version: "1.0"
  qwenpaw:
    emoji: "📧"
    requires:
      mcp: ["qwenpawmail-mcp"]
---

# 邮箱操作 (qwenpawmail-mcp)

此技能引导 Agent 完成邮箱账号绑定/注册，并使用 **qwenpawmail-mcp** MCP 服务器提供的工具执行邮件操作。

## 支持的邮箱服务商

以下 12 个域名自动路由到对应的 IMAP/SMTP 服务器：

| 分组 | 域名 |
| --- | --- |
| 网易个人邮箱 | `163.com`、`126.com`、`yeah.net` |
| 腾讯个人邮箱 | `qq.com`、`foxmail.com` |
| 新浪 | `sina.com`、`sina.cn` |
| 阿里个人邮箱 | `aliyun.com` |
| 谷歌 | `gmail.com` |
| 腾讯企业邮箱 | `exmail.qq.com` |
| 阿里企业邮箱 | `qiye.aliyun.com` |
| 网易企业邮箱 | `qiye.163.com` |

对于以上未列出的域名，需要显式提供 `imap_host` 和 `smtp_host` 环境变量或作为参数传递给 `set_credentials`。

## 调用规则

当用户的请求涉及任何类型的邮箱/邮件操作——查收邮件、发送邮件、阅读邮件、搜索邮件、管理文件夹/消息/线程、绑定已有邮箱账号或注册新邮箱——Agent 必须使用此技能，且只能使用此技能作为入口。不要自行编造替代的邮件工作流程。

## 工作流程：绑定或注册邮箱账号

在执行任何邮件操作之前，确保已绑定账号。凭据始终来自 `agent.json`——绝不硬编码或捏造凭据。

### 第 1 步 — 读取 `agent.json`

读取 `agent.json` 中的 `"mail"` 字段。预期格式：

```json
{
  "mail": {
    "is_new_account": false,
    "credential": {
      "name": "myaccount",
      "domain": "163.com",
      "auth_code": "ABCDEFGHIJKLMNOP",
      "password": "...",
      "phone_number": "..."
    }
    ...
  }
}
```

### 第 2a 步 — 若 `is_new_account` 为 `false`：管理用户邮箱

调用 qwenpawmail-mcp 的 `set_credentials` 工具绑定用户账号：

- `email` = `agent.json` 中的 `"name"` + `"@"` + `"domain"`（如 `myaccount@163.com`）
- `auth_code` = `agent.json` 中的邮箱授权码（`"auth_code"`）——这是在邮箱服务商网页设置中生成的 16 位授权码，不是登录密码。

然后调用 `check_auth` 验证 IMAP/SMTP 登录是否成功。

### 第 2b 步 — 若 `is_new_account` 为 `true`：为 Agent 注册新邮箱

需要为 Agent 注册一个新邮箱。邮箱用户名、域名、密码和手机号来自 `agent.json` 的 `"mail" / "credential"` 下的字段：`"name"`、`"domain"`、`"password"`、`"phone_number"`。

#### 首选路径 — 自动化浏览器注册：

如果有浏览器自动化工具或技能可用，通过自动化浏览器在服务商官方注册页面创建邮箱。调用 `browser` 技能在一个可视化浏览器窗口中操作，让用户可以观看。当需要短信验证码时，请求用户提供——绝不猜测。

注册网站映射：

| 域名 | 注册 URL | 备注 |
| --- | --- | --- |
| 163.com、126.com、yeah.net | https://zc.reg.163.com/m/regInitialized | 网易统一注册入口，注册时选择域名后缀；需手机短信验证 |
| qq.com、foxmail.com | https://zc.qq.com/ | 注册QQ号后自动开通邮箱；也可在 mail.qq.com 首页注册独立英文邮箱（@qq.com 或 @foxmail.com）；需中国大陆手机号 |
| sina.com | https://mail.sina.com.cn/register/weixin.php | 通过微信授权注册 @sina.com |
| sina.cn | https://mail.sina.cn/register/regmail.php | 通过手机短信注册 @sina.cn |
| gmail.com | https://accounts.google.com/signup | Google 标准账号注册；中国大陆需代理访问 |
| aliyun.com | 不可用 | 个人邮箱已于 2025年3月停止新用户注册，仅服务存量用户 |
| exmail.qq.com | https://exmail.qq.com/ | 企业邮箱注册需要企业认证等额外步骤 |
| qiye.aliyun.com | https://qiye.aliyun.com/ | 企业邮箱注册需要企业认证等额外步骤 |
| qiye.163.com | https://qiye.163.com/ | 企业邮箱注册需要企业认证等额外步骤 |

注意：通过浏览器注册邮箱时，每一步都要仔细检查截图以准确跟踪注册状态。

**成功标准**：只有在以下情况之一得到明确确认时才认为注册成功：
- 屏幕上显示明确的注册成功消息，或
- 注册后成功进入收件箱/邮箱界面

不要基于假设或不完整的证据判定成功——始终根据实际截图内容进行验证。

**处理阻塞步骤**：如果遇到无法自行完成的步骤（如接收短信验证码，或需要用户手动进行手机验证的步骤），不要终止浏览器进程或标记为失败/完成。而是暂停并请求用户手动完成该特定步骤，待用户确认后继续浏览。

**报告变更**：如果在注册过程中修改了原始请求的用户名或密码（如因用户名冲突或格式要求不满足），请在任务结束时告知用户变更原因和最终使用的值。

#### 备选路径 — 引导式手动注册：

如果没有浏览器自动化工具/技能可用，调用 qwenpawmail-mcp 的 `create_mailbox` 工具（`domain` = 目标域名，`username` = `agent.json` 中的 `"name"`）。该工具验证用户名格式，在需要时建议替代方案，并返回注册 URL 和逐步指引，交互式引导用户在自己的浏览器中注册邮箱。请用户报告注册成功并提供 16 位授权码。注意：注册后用户还需在服务商网页设置中开启 IMAP/SMTP 并获取 16 位授权码（仅显示一次——提醒用户立即保存）。

### 第 3 步 — 注册成功后

1. 编辑 `agent.json`：将 `"mail" / "is_new_account"` 设为 `false`（如适用，将获取的授权码记录到 credential 中）。
2. 用新账号的邮箱地址（`name@domain`）和授权码调用 `set_credentials` 进行绑定。
3. 调用 `check_auth` 确认连通性。
4. 读取 "CONTACTS.md" 获取已有的邮件联系人信息。

## 可用工具

账号绑定后，根据用户的实际需求选择最合适的 qwenpawmail-mcp 工具。

### 只读工具

| 工具 | 用途 |
| --- | --- |
| `list_folders` | 列出所有邮箱文件夹（中文名已解码） |
| `list_messages` | 列出文件夹中的消息元数据，最新在前（支持 limit+offset 分页） |
| `get_message` | 按 UID 获取单条消息：正文 + 附件元数据 |
| `get_attachment` | 按文件名或索引下载/获取附件内容 |
| `search_messages` | 按关键词、发件人和/或日期范围搜索 |
| `check_auth` | 验证 IMAP/SMTP 凭据连通性 |
| `create_mailbox` | 新邮箱注册引导（用户名验证、替代建议、URL、步骤） |
| `list_threads` | 列出会话线程（支持增量同步） |
| `search_threads` | 按关键词搜索线程 |
| `get_thread` | 获取线程内所有消息 |
| `get_mailbox_stats` | 邮箱统计（最近 N 天收发量等） |

### 写操作工具

| 工具 | 用途 |
| --- | --- |
| `send_message` | 发送纯文本邮件（to/cc/bcc） |
| `reply_message` | 回复邮件（设置 In-Reply-To/References + "Re:" 前缀） |
| `forward_message` | 转发邮件（以 rfc822 附件形式 + "Fwd:" 前缀） |
| `mark_messages` | 标记消息为已读/未读/星标/取消星标 |
| `move_message` | 将消息移动到其他文件夹 |
| `create_folder` | 创建新邮箱文件夹 |
| `set_credentials` | 设置/更新运行时邮箱凭据（邮箱地址 + 16 位授权码） |
| `clear_credentials` | 清除运行时凭据（回退到环境变量） |
| `update_thread` | 添加或移除线程自定义标签 |

### 破坏性工具（谨慎使用）

| 工具 | 用途 |
| --- | --- |
| `delete_message` | 永久删除消息（不可撤销） |
| `delete_thread` | 将线程移入垃圾箱 |

## 注意事项

- 绝不猜测授权码、密码或短信验证码。凭据来自 `agent.json`；短信验证码必须实时向用户索取。
- `auth_code` 是邮箱服务商网页设置中生成的 16 位授权码——不是登录密码。
- 消息 UID 是文件夹级别的且可能变化；操作消息前先用 `list_messages` 或 `search_messages` 刷新。
- `delete_message` 和 `delete_thread` 具有破坏性且不可逆——调用前务必向用户确认。
- 邮箱注册需要真实手机号和短信验证，无法完全通过协议自动化，因此使用浏览器（首选）或引导式手动（`create_mailbox`）路径。
- 对于 12 个自动路由域名以外的域名，`set_credentials` 需要显式提供 `imap_host` 和 `smtp_host`。
- 凭据变更后，务必先调用 `check_auth` 再执行其他邮件工具。
- 在整个任务过程中，留意遇到的任何新联系人信息，及时更新 CONTACTS.md 保持其最新。
