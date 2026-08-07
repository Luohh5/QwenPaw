---
name: mailbox
description: "Use this skill whenever the user needs ANY mailbox/email operation — checking, reading, searching, sending, replying, forwarding, organizing or deleting email, managing threads, as well as connecting an existing mailbox account or creating a new one. This skill is the single entry point for all email tasks and orchestrates the tools of the qwenpawmail-mcp MCP server (supports 12 email providers over IMAP/SMTP with auto-routing)."
metadata:
  builtin_skill_version: "1.0"
  qwenpaw:
    emoji: "📧"
    requires:
      mcp: ["qwenpawmail-mcp"]
---

# Mailbox Operations (qwenpawmail-mcp)

This skill guides the agent through connecting/creating a mailbox account and performing email operations using the tools from **qwenpawmail-mcp** MCP server.

## Supported Providers

The following 12 domains are auto-routed to their corresponding IMAP/SMTP servers:

| Group | Domains |
| --- | --- |
| NetEase (Personal) | `163.com`, `126.com`, `yeah.net` |
| Tencent (Personal) | `qq.com`, `foxmail.com` |
| Sina | `sina.com`, `sina.cn` |
| Alibaba (Personal) | `aliyun.com` |
| Google | `gmail.com` |
| Tencent Enterprise | `exmail.qq.com` |
| Alibaba Enterprise | `qiye.aliyun.com` |
| NetEase Enterprise | `qiye.163.com` |

For any domain NOT listed above, you must provide explicit `imap_host` and `smtp_host` environment variables or pass them as parameters to `set_credentials`.

## Invocation Rule

When the user's request involves mailbox/email operations of any kind — checking mail, sending mail, reading mail, searching mail, managing folders/messages/threads, connecting an existing mailbox account, or creating a new mailbox account — the agent MUST use this skill, and ONLY this skill, as the entry point. Do not improvise an alternative email workflow.

## Workflow: Connect or Create the Mailbox Account

Before any email operation, ensure an account is connected. Credentials always come from `agent.json` — never hardcode or invent them.

### Step 1 — Read `agent.json`

Read the `"mail"` field of `agent.json`. Expected shape:

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

### Step 2a — if `is_new_account` is `false`: Manage User's Email

Call the `set_credentials` tool of qwenpawmail-mcp to connect user's account:

- `email` = `"name"` + `"@"` + `"domain"` from `agent.json` (e.g. `myaccount@163.com`)
- `auth_code` = the mailbox authorization code from `agent.json` (`"auth_code"`) — this is the 16-character authorization code generated in the provider's web settings, NOT the login password.

Then call `check_auth` to verify the IMAP/SMTP login succeeds.

### Step 2b — if `is_new_account` is `true`: Set Up an Agent's Own Email

A new mailbox must be created for the agent. The intended mailbox name, domain, password, and phone number come from `agent.json` under `"mail" / "credential"`: fields `"name"`, `"domain"`, `"password"`, `"phone_number"`.

#### PREFERRED path — automated browser registration:

If browser-use tools or skills are available, create the mailbox via an automated browser on the provider's official registration site. Invoke the `browser` skill to operate in a visible (headed) browser window so the user can watch. When an SMS verification code is required, ASK THE USER to provide it — never guess it.

Registration site mapping:

| Domain | Registration URL | Notes |
| --- | --- | --- |
| 163.com, 126.com, yeah.net | https://zc.reg.163.com/m/regInitialized | Shared NetEase registration page; user selects domain suffix; requires phone SMS verification |
| qq.com, foxmail.com | https://zc.qq.com/ | Register a QQ account → auto-activate email; can also register independent english-name email (@qq.com or @foxmail.com) from mail.qq.com homepage; requires China mainland phone |
| sina.com | https://mail.sina.com.cn/register/weixin.php | WeChat-authorized registration for @sina.com |
| sina.cn | https://mail.sina.cn/register/regmail.php | Phone SMS registration for @sina.cn |
| gmail.com | https://accounts.google.com/signup | Standard Google account signup; requires proxy in mainland China |
| aliyun.com | N/A | Personal registration closed since 2025-03; existing users only |
| exmail.qq.com | https://exmail.qq.com/ | Registering a enterprise email account requires additional steps such as business verification |
| qiye.aliyun.com | https://qiye.aliyun.com/ | Registering a enterprise email account requires additional steps such as business verification |
| qiye.163.com | https://qiye.163.com/ | Registering a enterprise email account requires additional steps such as business verification |

Note: When registering an email account through the browser, carefully examine the screenshot at every step of the process to accurately track the registration status.

**Success Criteria**: Only consider the email registration successful when you have explicit confirmation through one of the following:
- A clear on-screen message confirming successful registration, OR
- Successful access to the inbox/mailbox after registration

Do not assume success based on assumptions or incomplete evidence — always verify against the actual screenshot content.

**Handling Blocked Steps**: If you encounter a step you cannot complete on your own (e.g., receiving an SMS verification code, or a step requiring manual phone verification by the user), do NOT terminate the browser using process or mark it as failed/complete. Instead, pause and ask the user to manually complete that specific step, then continue the browsing once they confirm it's done.

**Reporting Changes**: If you modify the name or password from what was originally requested during the registration process (e.g., due to name conflicts or format requirements not being met), inform the user at the end of the task with the reason for the change and the final value used.

#### FALLBACK path — guided manual registration:

If no browser-use tool/skill is available, call qwenpawmail-mcp's `create_mailbox` tool (`domain` = target domain, `username` = `"name"` from agent.json). It validates the username format, suggests alternatives if needed, and returns the registration URL plus step-by-step instructions that interactively guide the USER to register the mailbox themselves in their own browser. Ask for the user to report back that registration succeeded and tell you the 16-character authorization code. Note: after registering, the user must also enable IMAP/SMTP in the provider's web settings and obtain the 16-character authorization code (shown only once — tell the user to save it immediately).

### Step 3 — After successful registration

1. Edit `agent.json`: set `"mail" / "is_new_account"` to `false` (and record the obtained authorization code in the credential if applicable).
2. Call `set_credentials` with the new account's email (`name@domain`) and authorization code to connect it.
3. Call `check_auth` to confirm connectivity.
4. Read "CONTACTS.md" to retrieve existing email contact information.

## Available Tools

Once the account is connected, pick the most suitable qwenpawmail-mcp tool for the user's actual need.

### Read-Only Tools

| Tool | Purpose |
| --- | --- |
| `list_folders` | List all mailbox folders (Chinese names decoded) |
| `list_messages` | List messages in a folder, newest first, envelope metadata only (supports pagination via limit+offset) |
| `get_message` | Fetch one message by UID: text/html bodies + attachment metadata |
| `get_attachment` | Download/get attachment content by filename or index |
| `search_messages` | Search a folder by keyword, sender, and/or date range |
| `check_auth` | Verify IMAP/SMTP credentials connectivity |
| `create_mailbox` | Registration guidance for a new mailbox (username validation, alternatives, URL, steps) |
| `list_threads` | List conversation threads with incremental sync support |
| `search_threads` | Search threads by keyword |
| `get_thread` | Get all messages within a thread |
| `get_mailbox_stats` | Mailbox statistics (send/receive counts over recent N days, etc.) |

### Write Tools

| Tool | Purpose |
| --- | --- |
| `send_message` | Send a plain-text email (to/cc/bcc) |
| `reply_message` | Reply to a message (sets In-Reply-To/References + "Re:" prefix) |
| `forward_message` | Forward a message as attached rfc822 with "Fwd:" prefix |
| `mark_messages` | Mark messages read/unread/flagged/unflagged |
| `move_message` | Move a message to another folder |
| `create_folder` | Create a new mailbox folder |
| `set_credentials` | Set/update mailbox credentials at runtime (email + 16-char auth code) |
| `clear_credentials` | Clear runtime credentials (falls back to env vars if set) |
| `update_thread` | Add or remove custom labels on a thread |

### Destructive Tools (use with caution)

| Tool | Purpose |
| --- | --- |
| `delete_message` | Permanently delete a message (cannot be undone) |
| `delete_thread` | Move an entire thread to trash |

## Notes

- Never guess authorization codes, passwords, or SMS verification codes. Credentials come from `agent.json`; SMS codes must be requested from the user in real time.
- The `auth_code` is the 16-character authorization code from the provider's web settings — it is NOT the login password.
- Message UIDs are per-folder and can change; refresh them with `list_messages` or `search_messages` before acting on a message.
- `delete_message` and `delete_thread` are destructive and irreversible — confirm with the user before calling them.
- Email registration requires a real phone number and SMS verification; it cannot be fully automated via protocol, hence the browser (preferred) or guided-manual (`create_mailbox`) paths.
- For domains other than the 12 auto-routed ones, `set_credentials` requires explicit `imap_host` and `smtp_host`.
- After any credential change, call `check_auth` before running other email tools.
- Throughout the task, stay alert for any new contact information encountered, and promptly update CONTACTS.md to keep it current.
