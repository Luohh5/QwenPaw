# GitHub Review 数据说明

每条输入是一次 AI Review 到下一次 AI Review 或 PR 结束的观察窗口。

- agent_output.text 是 AI 输出；verdict 只是解析结果，不是正确答案。
- PR 中的代码和测试是被审查材料，不是 AI reviewer 编写或执行它们的证据。
  AI 认可有错误断言的测试时，应写“AI 未识别测试断言的问题”或“AI 认可了该测试”，
  不能写“AI 编写了错误测试”或“AI 主动断言了错误行为”。同样区分作者报告的测试结果
  与 AI 审查行为；报告通过不等于你已执行验证。
- agent_output.reactions 针对 AI 消息；post_review_events 内的 reactions 针对各自消息。
  人工评论获得赞，不等于 AI 获得赞；计数可能缺少投票时间和身份。
- post_review_events 包括 comment、review 和 inline_comment；review 还可能包含
  inline_comments。reply、评论内容和 code_context 帮助判断实际反馈对象。
- target_relation=same_snapshot 表示同一个被审查版本；later_snapshot 表示后续版本；
  other_snapshot/unknown 不能自动算作同版本。后续版本新增的问题不能归咎于旧审查。
- reviewed_code.diff 是当时的差异，不包含所有未修改文件。diff_status=missing_diff
  不妨碍理解明确回复，但限制代码核查；confidence 也不是反馈可靠性的评分。
- response_changes.commits 只说明有后续提交。message 可能说明修改意图，但不证明
  具体建议已被正确实现；diff_range 只有版本号，不是后续实际 diff。
- APPROVED、CHANGES_REQUESTED、merged、closed_unmerged 是流程状态。
  author/member 身份也不能使其结论自动正确；actor.type 可能无法识别所有自动化账号。
- quality_flags 说明缺失、编辑和关联限制；不要把被移除或未提供的内容补写出来。

典型区别：
- 人工在 AI 批准后指出遗漏，只能算间接反馈；先确认问题属于被审查版本。
- “address reviewer comments” 不一定在回应 AI，先定位此前实际提出建议的人。
- 同一条人工评论可以同时包含正确的运行路径分析和错误的语言行为判断，必须拆开。
