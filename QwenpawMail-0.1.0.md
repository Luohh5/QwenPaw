# QwenpawMail 0.1.0

> QwenPaw 邮箱管理功能版本总结 —— 稳定性优化与 MCP 部署形式重构

## 概述

QwenPaw 的 agent 邮箱管理能力由 **qwenpawmail-mcp**（基于 stdio 传输的 MCP server，提供 22 个邮箱操作工具，支持 12 个邮箱服务商自动路由）承载。本版本围绕两大主题完成了系统性收口：

1. **稳定性优化**——对本地 stdio 部署方案进行了全面的鲁棒性风险评估与修复；
2. **MCP 部署形式重构**——将 qwenpawmail-mcp 合并进 QwenPaw 主仓库（monorepo 子包），实现所有安装途径的零配置集成。

---

## 一、稳定性优化

在方案评估阶段，对「本地 stdio MCP」与「远程 HTTP 服务 / CLI 化」两种路线做了对比推演。结论：远程化会引入凭据托管信任风险、邮箱服务商异地登录风控、单点故障等结构性代价，对个人本地 AI 助手是负优化；而本地 stdio 方案的所有问题均为可修复的工程缺陷。因此选择**留在本地 stdio，并逐项修复**：

| # | 风险 | 风险预估 | 解决方案 |
|---|------|----------|----------|
| 1 | 环境依赖重，用户必须安装 Node 等额外运行时，带不进 Docker | 高（部署阻断） | 优化为**纯 Python 实现**（mcp SDK + imap-tools，Python ≥3.10），与 QwenPaw 主项目共用同一运行时，随 pip 一起安装，Docker 天然可用。 |
| 2 | IMAP 连接无超时，网络半开或服务器挂起时工具调用永久卡死 | 高（最高危卡死源） | 所有 IMAP 连接显式设置 30 秒超时（SMTP 原已有），网络异常时快速失败并返回可读错误。 |
| 3 | 工具 handler 同步阻塞事件循环，大附件下载等 I/O 会冻结整个 MCP server | 中 | 全部 22 个工具 handler 改为 async，阻塞 I/O 用 `asyncio.to_thread` 包装，事件循环与取消信号始终保持响应。 |
| 4 | MCP server 被用户误杀后无法恢复；持续启动失败时产生 spawn 风暴 | 中 | QwenPaw 客户端侧已有自动重连，本次补齐**指数退避**（1s→60s + 抖动）与**熔断机制**（连续 5 次失败停止重试并上报，5 分钟后自动探测恢复）。 |
| 5 | 主进程崩溃时 MCP 子进程可能变成僵尸/孤儿进程 | 中 | MCP 侧注册 SIGTERM/SIGINT 优雅退出；配合 stdio 管道 EOF 自愈与客户端侧进程清理兜底，进程生命周期全链路闭环。 |
| 6 | driver card 硬编码开发者本机绝对路径，换机器/容器必挂 | 高（无法分发） | driver card 改为动态注入 `sys.executable`，MCP 永远由运行 QwenPaw 的同一 Python 解释器启动，零路径配置。 |
| 7 | 运行时切换邮箱后 ThreadStore 未重置，线程索引与新邮箱数据错位，极端情况下可能误操作邮件 | 低（隐患型） | `set_credentials` 时同步重建 ThreadStore，且存储目录按邮箱地址做命名空间隔离，多邮箱数据互不污染。 |
| 8 | agent 批量操作时高频建连，触发服务商（163/QQ 等）并发连接数限制 | 中 | 同域名连接节流（0.5 秒最小间隔），平滑化连接峰值，避免触发限连或临时封禁。 |

所有修复已通过语法验证与逐项代码核查（16/16 检查点通过）。

---

## 二、MCP 部署形式

### 2.1 monorepo 合并

qwenpawmail-mcp 已从独立外部项目合并进 QwenPaw 主仓库，作为 monorepo 子包维护，**不发布 PyPI**：

```
QwenPaw/
├── packages/
│   └── qwenpawmail-mcp/          # 邮箱 MCP 子包（独立 pyproject.toml，可单独安装）
│       ├── src/qwenpawmail_mcp/  # 核心源码（server / mail_client / providers / thread_store 等）
│       ├── pyproject.toml
│       ├── README.md             # 英文版（双语，附 README_zh.md）
│       └── README_zh.md
├── src/qwenpaw/                  # 主项目
└── ...
```

优势：一次 clone 全部到手、版本天然同步、CI 统一管理；同时保留独立包结构，未来如需可低成本拆出或发布。

### 2.2 各途径安装部署方式

| 途径 | 方式 | 说明 |
|------|------|------|
| 本机开发 | `make install-dev` | 一键安装主项目 + MCP 子包（内部使用 `$(PYTHON) -m pip`，自动探测 python3，兼容 pip/pip3 各种环境） |
| 仅装子包 | `make install-mail-mcp` 或 `pip install -e packages/qwenpawmail-mcp` | 独立可编辑安装 |
| Docker | `docker compose up -d` | Dockerfile 已内置 `COPY packages/qwenpawmail-mcp` + 安装步骤，容器内邮箱功能开箱即用 |
| 其他 MCP 客户端 | stdio 配置 `python -m qwenpawmail_mcp` | 兼容 Claude Code、Cursor、OpenClaw 等任意 MCP stdio 客户端，README 附配置示例 |

运行时无需任何手工启动：agent 绑定邮箱后，QwenPaw 自动生成 driver card 并用 `sys.executable -m qwenpawmail_mcp` spawn 子进程，凭据通过环境变量注入。

### 2.3 Skill 加入默认技能池

- `mailbox` 技能（原 `skills/mailbox_operations/SKILL.md`）已优化并以**中英双语**加入 QwenPaw 内置技能池：
  - `src/qwenpaw/agents/skills/mailbox-en/SKILL.md`
  - `src/qwenpaw/agents/skills/mailbox-zh/SKILL.md`
- 技能系统自动发现（目录名匹配 `{name}-{en|zh}` 且含 SKILL.md 即注册），无需额外配置，agent 创建时默认可用。
- 技能内容同步更新：
  - 支持域名从 5 个扩充为 **12 个**（网易系、腾讯系、新浪、阿里、Gmail 及三家企业邮）；
  - 工具表从 14 个更新为 **22 个**，按只读（11）/ 写操作（9）/ 破坏性（2）三类分组；
  - 补全全部服务商的**官方注册页面 URL 映射**（含注册方式备注：手机验证、微信授权、代理需求等；阿里个人邮箱已停止注册并标注）。

### 2.4 配套收尾

- 子包 README 重写为规范的开源文档结构（Features / Providers / Installation / Configuration / Tools Reference / Security Notes），中英双语互链；
- 主项目 `pyproject.toml` 与 driver card 中不再存在任何硬编码用户路径；
- 明确了子包内可安全清理的非运行必需文件清单（tests / docs / scripts / 临时脚本 / .env 等），其中含真实凭据的 `.env` 已被标记为优先删除项。

---

## 新用户部署速览

```bash
git clone <repo> && cd QwenPaw
make install-dev          # 主项目 + 邮箱 MCP 一键装好
qwenpaw init --defaults
qwenpaw app               # http://127.0.0.1:8088/
```

用户唯一需准备的材料：在邮箱服务商网页设置中开启 IMAP/SMTP 并获取 16 位授权码。其余（MCP 启动、路由、技能加载）全部自动化。
