# MEMORY

## agent-cookbook 项目

- 事件驱动 Agent 教学书（books/event-driven-agent/），配套代码 src/baby_event_driven_agent/stages/，每章有 demo/pytest 实测。
- **章节/代码编号（2026-09-26 重排）**：01-receive-events→stage01_receive_events；02-inbox-steering→stage02_inbox_steering；03-interrupt→stage03_interrupt；**03b-message-mechanism→stage03b_message_bus**（原 stage04_message_bus）；**04-trajectory→stage04_trajectory**（原 stage05_session）；05-compaction→未来 stage05_compaction（尚无代码，书稿已按此写）。脚本入口 stage01~04 + stage03b-demo/test；recorder --stage 支持 03b。sessions/ 与 rec/ 数据目录同步改名（stage03b/stage04）。session log=trajectory 只记消息（生命周期事件只走总线）是 03 起的统一约定，04/05 章传播待做。
- Stage 3 中断语义（2026-09-26 定稿，取代 09-24 版）：工具执行**不打断**（Protocol block，on_interrupt 只掐 stream 段，_running_tool 已删）；三种"LLM 未完整返回"场景（已发未回/thinking/tool_call 流中）处理**完全一致**——整步丢，收尾一律补 **assistant 打断占位**，redirect 追加**纯纠正 user**（REDIRECT_NOTE、user 打断标记退役，不区分 thinking_seen）。占位文本按尾部角色：tool → "[本轮已被用户中断，不再基于上面的工具结果作答]"；user → "[response interrupted]"。依据：Hermes state.db 14 条实测（全补 assistant 占位"Operation interrupted."、纠正纯 user）+ pi 补 assistant 消息（stopReason=aborted）。06 章写压缩/eval 时会话内坐标沿用 entry id 而非 seq。
- demo 录制链：recorder.py（asciinema → agg gif / ffmpeg mp4，run-id=docs，index.json 带 title）；书稿 gif 引用 rec/stageNN/docs/<case>.gif，case 名以各 stage main.py --list 为准。重录后文档里的"真实 log 片段"要同步从 sessions/stageNN/session.jsonl 最新一轮刷新（文件 append-only，多轮混在一起，按 sid 最后一个连续块取）。
- 05a-trajectory.md（Stage 5a 会话与真相）：机制参考 pi coding agent 的 session 设计（entry 树、认父不认子、rewind 移指针、投影式上下文），落盘沿用 Stage 4 长度前缀 + CRC。事实层两本账：EventLog（坐标 seq，传输层账——恢复时位点续读闭合悬空审批）与 Trajectory（坐标 entry id/parentId，会话结构账）；correlation_id 在 5a 无结构性消费者（聚簇由树路径接管，只剩 log 观察字段）。04 章对 5a 的前向引用已按此对齐（2026-09-23），写 5b/6 时压缩与 eval 的会话内坐标应沿用 entry id 而非 seq。
- **待办：stage04/05 传播**——两处 agent.py 副本仍是旧收尾语义（user 打断标记 + REDIRECT_NOTE + thinking 空壳），对应 tests 与 04/05 章书稿待跟着改；仓库根 verify_stage03_tool_kill.py（当场掐死语义）待删或改写。

## 写作风格（用户明确要求）

- 禁 AI 腔短语："这不是巧合""容易纠结""容易xx""值得一提的是""设计收口""这不是洁癖"等；2026-09-23 新增："守门（规矩）""不猜""就三样/就两步/就一行""只有一条线索"这类修辞化计数/排比说法。
- 简洁直接：少铺垫、少论证尾巴，每段给结论；顺句叙事、"我们"视角；少用引用块和修辞式短语。
- 正文细节要与 src 下实际实现对齐（先查代码再写取值说明）。
- **多轮迭代后必须整节重读重写**：打补丁式的替换会留下对话残留（辩解式句子、破碎指代、"不是xx对象"这类反驳语气），书稿是给没参与讨论的读者的，要按章节上下文自洽，不是在回应作者的质疑。
- 设计取舍在书里只写选定方案及其理由，备选方案一笔带过；"需求真出现才上"——不预立空配置对象、不加没有消费者的旋钮。
- 代码架构口径（2026-09-24）：约束归声明方——"事件类型能被谁消费"由事件类型自己声明（events.require_consumable），订阅者/总线零事件侧知识；消费者投递方式由消费者声明（Mailbox 结构协议，提供 offer 即邮箱型，不用基类绑定）。
