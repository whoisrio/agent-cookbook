"""Stage 4：消息机制 —— 事件离开 agent 之后要走多远。

本章只强化 outbound（事件离开 agent 之后怎么到达它的消费者）：
异步分发 → QoS 分道 → 背压合并 → 信封（seq / correlation_id）→ 治理 → 落盘与位点。
inbound 的 publish 与 stage02/03 一字不改：上下行不对称是本章的**前提**。
"""
