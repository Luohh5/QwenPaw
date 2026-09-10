---
name: qa-history-analyze
description: 分析 QwenPaw 答疑机器人的历史 JSONL、工具轨迹和用户反馈，在线下识别回答问题并推断反馈关联。用于 selflearn 的逐条分析阶段。
---

你分析的是已结束的答疑过程。目标是找出值得补充知识的问题，不在本阶段重答问题或改知识库。

引用和工具调用使用流程提供的稳定 record_id。原始 request_id 可能在多轮中重复，不能把它直接当作单条问答编号，也不能仅凭重复就确认反馈关系。原始字段保持不变。

## 阅读与反馈关联

- 先读问题、input.messages、answer，再用 read_history 展开 trace 中的工具参数、结果和错误。长内容按 next_start_line 续读，不将未展开等同于缺失。
- 线上不负责关联或判断反馈。用 list_history 搜索主题关键词、原回答片段，read_history 回读可能有关的记录，自行判断追问、纠错、确认是在回应哪次回答。允许从当前 input.question 识别对旧回答的反馈。
- 优先使用消息 ID、引用、明确的对话上下文；其次结合语义和具体措辞。相邻时间、相同主题不能单独证明同一会话或同一个人。session_id 为 null 不阻断分析。
- 将推断存入 feedback_links，写明 target_record_id、relation、confidence、reason，并引用两端证据。不确定就保留低置信度/unclear，不编造会话 ID。已有 feedback 或额外字段是线索，仍需核查。
- 每项关联必须涉及本次给定的 record_id；不要把另外两条记录之间的关系填入当前记录。evidence 同时包含当前 record_id 和 target_record_id 的原文。
- 后续追问可能是新需求。沉默不等于满意，用户说“不对”也不自动证明原答错；清楚区分反馈态度和事实正确性。

## 分析回答和过程

逐项判断回答是否切题、完整、有证据，是否重复已被用户否定的方法。结合 retrieve_knowledge 等工具的实际返回，区分：

- knowledge_gap：现有材料缺少回答所需知识；仅检索无结果时仍是疑似缺口。
- outdated：知识可能过时或版本不符。
- scattered：知识分散，适合整理为直接可用的 FAQ。
- retrieval：查询方式或召回有问题。
- answering：已经返回有效资料，回答却忽略、误用或无依据扩写。
- tool_failure：工具异常、重复失败等流程问题。
- other / uncertain：其他问题或尚不能判断。

同一回答可以有多个具体问题。缺少工具记录不代表“没有调用”；status 为 null 不代表失败。没有后续反馈也可依据问题、答案和检索内容发现可核实的疑点。保留正确回答和无问题案例，为下一阶段识别高频需求与避免误改提供对照。

## 输出约定

按 QAAnalysis 结构输出中文 topic、summary、answer_quality、feedback_links、findings、missing_evidence。每项 finding 说明 problem、kind、assessment（confirmed/suspected）、reason。

所有事实引用使用 HistoryEvidence：record_id、原始记录 JSON Pointer、连续原文 quote。例如 /input/question、/answer、/trace/1/content/output/0/text。不得翻译、拼接、加省略号；不得引用自己的推断作为原始事实。缺失的具体证据写入 missing_evidence。findings 和 feedback_links 可以为空，不强行制造问题。

优先引用字符串叶子字段，例如 /trace/0/content/input/query，而不是复制整个 input 对象的排版。读取工具使用与校验相同的 JSON 表示；行号不是原文，不要把缩进或展示用行号放进 quote。
