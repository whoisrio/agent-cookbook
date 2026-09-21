"""Stage 5a：会话与真相 —— log、投影与异常恢复。

事实层从"内存 history + 事件流"升级为 per-session 的 append-only entry 树
（pi 式轨迹 + Stage 4 的 CRC 落盘工程）：

- trajectory.py：entry 树（9 种类型按"对 LLM 调用的影响"分三组）、
  rewind（移指针）、fork（克隆路径）、build_context（f(轨迹, policy)）；
- session.py：SessionStore（sid 分配、start/resume/close、fork）、
  残尾恢复、悬挂审批闭合；
- agent.py：history 换成轨迹层，改动只有三处（见模块 docstring）。

bus / events / persistence / llm / outbound / subscribers 与 stage04 一字未改
——传输层是上一章的成果，本章只定义"留下来的东西是什么语义"。
"""
