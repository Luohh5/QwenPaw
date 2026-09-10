---
name: qa-knowledge-propose
description: 汇总已分析的 QwenPaw 问答案例，识别高频需求和重复错误，提出具体知识补充任务。用于 selflearn 的跨案例归纳阶段。
---

用 list_analyses 分页读完本批次全部成功分析，必要时 read_history 回查原始问题、回答、检索结果和推断反馈。分析结果是判断材料，不是标准答案。

## 归并与选择

- 按用户需求和错误机制归并，不按字面相似强行合并。版本、部署方式和适用条件不同的答案应拆开或明确条件。
- 高需求频率和重复错误分开：被多次询问不代表多次答错；重复同一错误才是重复失败线索。
- 用 feedback_links 检查追问和纠错对应的原答案。反馈记录不是新的独立失败；相同会话连续追问也不算多个独立用户。没有 session_id 时只声称出现多少条相关记录。
- 优先处理反复答错、频繁询问且答案分散的问题；明确的单次缺口也可生成任务。正确回答可以提炼，但需说明整理价值。
- research：有明确可查证的问题，补充知识可能有帮助；任务应写出具体 research_questions。
- record_only：工具故障、回答策略等流程问题，不能靠添加产品事实解决；只记录，不强塞进 TXT。
- needs_evidence：问题尚不明确，缺少关键条件，先留待处理。
- 无需为每条案例都生成任务。同一个知识条目只生成一个任务；不能为凑高频把不相关案例归到一起。

## 输出约定

按 KnowledgePlan 输出 summary、tasks、limitations。每个 KnowledgeTask 包含规范化 question、problem、kind、action、reason、related_record_ids、supporting_findings（record_id + finding_index）、research_questions。

related_record_ids 引用本阶段已成功分析的记录；支持项必须真实存在。保留反例和不确定性，不把 suspected 升级成 confirmed。案例次数由程序计算，不生成独立用户数或自行宣称已验证收益。tasks 可以为空。
