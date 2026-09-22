"""会话层：Stage 5a 的事实层。

trajectory：per-session 的 append-only entry 树（认父不认子、rewind 移指针、
fork 克隆路径）。store：SessionStore（sid 分配、start/resume/close/fork）、
残尾恢复、悬挂审批闭合。上下文 build 不在这里——那是 agent 的活。
"""
