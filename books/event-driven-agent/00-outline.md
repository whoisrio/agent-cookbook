# 事件驱动 Agent 架构：七步进化（大纲草稿 v2）

> 状态：骨架草稿 v3，遗留决定已清。每章定稿后再写正文和代码。
>
> 配套代码：`src/baby_event_driven_agent/stages/stage01_receive_events/` 起，
> 到 `stage07_eval/`，每阶段独立可跑（自成包，带 tests/）。
> 终态参考（答案卷）：现有 `src/baby_event_driven_agent/`（bus.py / agent.py / extensions.py）。

## 写作约定

- 不绑业务场景，硬核通用 agent 设计。工具就三个：read、write、search。
- UI 是 CLI，支持流式事件呈现——Stage 5 的背压合并缓冲就在 CLI 上讲。
- 每章结构：一个新需求到来 → 现有设计哪里接不住 → 机制落地（agent 始终真实可用）
  → 下一章的需求预告。
- 叙事纪律：不是造烂代码再修。每一章的 agent 在它的能力范围内都是真实可用的，
  接不住的是新需求，不是旧代码。
- 实现新机制时踩的坑必须是真的：优先用现有代码注释里记录过的真实坑（如
  asyncio.Queue 双队列竞速 500/500 丢事件），不编造。
- session log 从第 1 章就有，append-only，哪怕暂时没人回放。history 只是 log 的投影。
- 每章末尾有"验证"小节：跑了什么、实测输出是什么、有没有未实跑项，如实标注。
- 代码目录不跟书走，放 `src/baby_event_driven_agent/stages/`：pytest / uv / 包管理
  开箱即用，阶段目录自成包可独立运行；正文里每章开头给代码路径和关键 diff。

## Stage 1：只是接收事件

- 起点 v0.1，真实可用：同步 EventBus，handler 注册上去，UI 发事件、handler 直接在
  总线回调里跑完整个 agent loop。单用户、一问一答、turn 跑完再接下一条——完全正常。
- 模型是真的：.env 配 OpenAI 兼容端点（demo / 测试指向本地 ollama 的
  qwen3.5:4b-32k），流式调用。文本增量（agent_delta）和思考增量
  （agent_thinking，进窗口不进 log）边到边发 UI；工具调用增量边到边累积，
  流结束拼出完整 assistant 消息。
  Stage 1 不养假 LLM；Stage 2 / 3 的测试要确定性时序，才引入 FakeLLM。
- 工具四件套，读写成对：query_inventory / update_inventory 扮演业务接口
  （inventory.txt 模拟业务库），search_rules / update_rules 扮演规则检索
  与知识运营（rules.txt 模拟规则库，逐行匹配，不是真 RAG）。写操作真写文件。
- 落地机制：事件、总线、订阅分发、append-only session log、system prompt。
- 能力边界（下一章需求一到就撞上）：设计里没有"排队"和"打断"这两个概念。
  回答跑着的时候用户插话纠正，会原地起第二个并发 turn 写同一份 history；
  stop 事件发出去无人接收。
- 下章需求预告：用户想在回答还在跑的时候插话。

## Stage 2：followup 和 steering

- 需求：回答还在跑，用户连续发消息——要么插进当前回答（steering），要么排在
  回答之后（followup）。
- 机制：handler 只做一件事——投进收件箱立刻返回。每个 session 一个收件箱
  加一个常驻 worker（首次消息时启动），worker 循环"取消息 → 跑 turn"。
  step 边界 drain 到的消息拼进当前上下文（steering）；worker 空闲后取到的
  消息开新 turn（followup）。turn_end 升级成总线真事件。
- 关键论点：分类时机在消费端，不在提交端。消息自己不背语义。
- 竞态怎么消的：worker 永不自行退出（退出是 stop 的显式职责），"消息进队
  但 task 恰好死了"的窗口根本不存在；spawn 检查靠 asyncio 单线程里
  put/check 之间无 await 的天然原子性。一步的 turn 没有 step 边界，
  插话自然降级成 followup——语义自洽，不用特判。
- 可视化与临界降级：drain 消费时发 steering_consumed 事件（★ 上屏，
  不用翻 log）；demo 插话时机由 tool_call_started 事件驱动（step 确定在飞），
  但"发得早"不保证"被消化"——落在最后一个 drain 点之后的消息降级为
  followup（worker 下次 get() 天然接住），demo 必须等第二个 turn_end，
  不能在第一个 turn_end 就 stop()（否则收件箱里的消息被连人带队掐死）。
- 下章需求预告：steering 只能在 step 边界生效，正在飞的那一步等不了
  （等待时长取决于在飞请求，可能是 0.24 秒也可能是 8 秒）——
  用户想直接掐掉重来。

## Stage 3：用户中断

- 需求：回答跑偏了，用户不等了，按停止——正在飞的模型请求要能掐掉，而且
  不能把整个 agent 掐死。
- 机制：中断不是消息，不进收件箱——是控制信号，直接作用在"正在飞的那一步"。
  每 session 记录当前在飞的 step task（_inflight），on_interrupt 无 await 地
  查表并 cancel（原子，无信号残留问题）；step 包成可取消单元（流消费 + 工具
  执行一起），工具结果由 turn 协程在 task 成功后统一 append，保证 history
  里永远是合法序列，被掐的 step 不留半截消息。
- 设计取舍（正文讲清为什么不开高优先级队列）：竞速取两个 asyncio.Queue 会
  静默丢事件（早期实现的真实坑）——中断不给队列就没有可丢的东西。
- 关键语义：取消粒度是单步不是执行体（worker 活着、history 完好）；取消与
  完成竞速，信号落在 task.done() 之后就是落空（如实打印）；stop()（进程收尾）
  与中断（单步）是两个入口；中断不作废历史——被中断的问题留在 history，
  实测模型下一轮主动补答（不想保留就在中断时撤掉该 user 消息，两路都通）。
- 下章需求预告：中断之后 turn 结束了，用户的纠正要从头再来——能不能不结束？

## Stage 4：redirect（同一个 turn 内原地转向）

- 需求：中断要掐掉正在跑的回答还得重问一遍，用户想要的是"我改主意了，你
  接着干活"——同一个 turn 内原地转向。
- 语义（沿用 books/hermes/redirect-message-shapes.md，源码基线
  opensource-refs/hermes-agent）：取消当前这次模型请求，用户的纠正插成一条真实
  user message，同一 turn 内重试。已完成的工作保留，turn 不重来。
- 跟前三步的边界：
  - steering = 等 checkpoint，不打断在飞的（Stage 2）
  - interrupt = 掐掉在飞的，turn 结束（Stage 3）
  - redirect = 掐掉在飞的，turn 不结束，补消息格式后立刻重发
- 核心新问题：被掐断的 assistant 输出是残缺的，而 provider API 对消息序列格式
  是严格的。要补齐：usr → assistant → tool_result → assistant → tool_result。
- 消息修复 Case 矩阵（照搬 hermes 笔记，正文里实测重演）：
  - 模型生成中、有可见文本：补 assistant（可见文本）+ user（新输入带上下文标注）
  - 模型生成中、纯 thinking：补 assistant 空壳
  - 尾巴已是 assistant：不补，折进 user
  - 工具执行中：降级为 steer（工具没法安全作废，hermes Case 4）
  - 响应已返回的竞态：丢弃响应按前三种处理
  - 同 turn 多次 redirect：文本累加
- 机制：复用 Stage 3 的取消全套，新增消息修复函数 + turn 内续跑。
- 关键论点：中断解决"怎么停"，redirect 解决"停了之后 history 还是合法的"。

## Stage 5：agent 在 loop 中间发出消息

- 需求：CLI 要流式呈现——回答一个字一个字往外蹦，用户看得见 agent 正在干什么。
- 起点：loop 开始 emit 生命周期事件和 token 流，全部塞回原总线。
- 要过的坎（按出现顺序，每个都是被需求逼出来的）：
  1. 慢消费者拖死 loop → 上行必须异步分发
  2. token 流洪峰淹没 stop 信号 → QoS 分级，命令通道和流式通道分离
  3. UI 刷不动 → 背压合并缓冲：满了或者到了帧界就刷，二者其一（不能只等满，
     否则 UI 一顿一顿）
  4. 事件多了没法定 位 → 事件信封：type / session_id / seq / correlation id
  5. 发出去的消息要不要管 → 拦截/否决/改写治理（现有 extensions.py 并入本章）
- 关键论点：上行和下行不对称。下行低频不可丢，上行高频可丢，混在一条通道
  迟迟早早出事故。

## Stage 6：扩展到生产消息机制

- 需求：部署从单进程走向多进程、事件要落盘、出了问题要重放现场。
- 判据先讲清楚：多进程部署、持久化、replay 三个需求真的出现了才上，别一上来
  就 Kafka。
- 落地机制：持久化 log、at-least-once、按 session 分区序、消费位点。
- 收官闭环：append-only session log 从 Stage 1 埋到现在终于闭环——history 是
  log 的投影，重启回放重建状态。Kafka consumer offset 和 agent checkpoint
  是一对平行结构，放这讲。

## Stage 7：rubric 和 eval

- 起点：agent 跑通了，但"改了 prompt、换了模型、重构了 loop，行为有没有变坏"
  没有答案。靠手感回归就是靠运气。eval 为薄弱环节的读者（包括作者自己）补课。
- 地基是前六步攒下的：append-only session log + replay（Stage 6）正是 eval 的
  输入——eval = 重放录制好的 session + 对结果打分。没有 log 就没有可重复的 eval。
- 落地机制：
  - rubric：把"这个 agent 干得好不好"写成可判定的评分标准。硬断言和软评分分开：
    硬断言是确定性检查（工具调用序列对不对、redirect 补出来的消息格式合不合法、
    中断后状态完不完整）；软评分是 LLM-as-judge 按 rubric 清单打分。
  - eval harness：golden trajectories（录制）→ 重放 → 硬断言 + rubric 评分 →
    聚合报告。
  - Stage 2~4 的行为语义全部变成 eval 用例：steering 生效时机、redirect 竞态
    降级、ctrl 插队不丢事件——以前只能靠手点，现在进回归集。
- 要过的坎：LLM-as-judge 不稳定——同一轨迹两次打分不一样。处理：评分输出结构化
  （强制 JSON + 逐项理由）、rubric 逐条独立判、软评分只看趋势不当断言。
- 关键论点：event log 不只是调试用的，它是 eval 的数据源。append-only 是从
  Stage 1 贯穿到 Stage 7 的同一根线。
- 结尾：七步回头看一张对照表（问题 → 机制 → 代价）。
