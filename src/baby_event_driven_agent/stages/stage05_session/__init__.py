"""Stage 5a：会话与真相 —— log、投影与异常恢复。

事实层从"内存 history + 事件流"升级为 per-session 的 append-only entry 树
（pi 式轨迹 + Stage 4 的 CRC 落盘工程）。三层结构：

- transport/：Stage 4 传输层原样搬入（events / bus / persistence /
  outbound / subscribers），本章一字未改——传输层是上一章的成果，
  本章只定义"留下来的东西是什么语义"。
- session/：本章新增的会话层。
  - trajectory.py：entry 树（10 种类型按"对 LLM 调用的影响"分三组）、
    rewind（移指针）、fork（克隆路径）；
  - store.py：SessionStore（sid 分配、start/resume/close、fork）、
    残尾恢复、悬挂审批闭合。
- agent.py：agent + 投影（build_context）。history 换成轨迹层，
  改动只有三处（见模块 docstring）。怎么从树 build 上下文是 agent 的活，
  轨迹层不关心。
"""
