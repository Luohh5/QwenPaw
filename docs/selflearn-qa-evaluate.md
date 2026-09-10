# 答疑机器人评测

新版流程见 [完整使用说明](qa-bot-self-improvement-guide.md)。

回答只放在工作区 `eval/*.jsonl`。评分放在 `selflearn/score/回答名称/`，
分数登记在 `selflearn/score/index.json`，基线指针在 `selflearn/baseline.json`。

```text
/selflearn evaluate /完整路径/2026-08-20.txt
/selflearn score /完整路径/A.jsonl --references /完整路径/评分标准目录
```

再次执行相同命令会复用已完成结果，不需要随机 run_id。
基线成绩有效时不重复评分。默认并发 4 题，过程在 session 可见。
