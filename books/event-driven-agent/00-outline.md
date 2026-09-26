# 事件驱动 Agent 架构：从接收事件到评测（大纲 v6）

> 状态：v6。章节编号与文件名对齐（01 / 02 / 03 / 03b / 03c / 04 / 05 / 06），
> 旧编号 Stage 4 → 3b（消息机制）、5a → 4（轨迹）、5b → 5（压缩策略）。
> 新增 **3c「能力升级」**：工具、业务世界、长程任务场景一次定稿，
> 04（轨迹）与 05（压缩策略）骑在这个场景上讲机制——同一张任务单的两种命运。
> 每章定稿后再写正文和代码；各章状态在小节里如实标注。
>
> 配套代码：`src/baby_event_driven_agent/stages/` 下各阶段自成包：
> stage01_receive_events / stage02_inbox_steering / stage03_interrupt /
> stage03b_message_bus / stage04_trajectory。**3c 与 04 同包**（stage04_trajectory）：
> 3c 落工具层与长程任务场景，04 落轨迹层与手动压缩；stage05_compaction /
> stage06_eval 随 05 / 06 落地。

## 写作约定

- 不绑单一框架的硬核 agent 设计。工具是运营世界的六件套（3c 定稿）：
  query_inventory / update_inventory / search_rules / update_rules / list_tasks /
  get_task——读写成对、任务域驱动多轮；分页契约、批次句柄等"为压缩策略配套"
  的工具留给 05。
- UI 是 CLI，支持流式事件呈现——Stage 3b 的背压合并缓冲就在 CLI 上讲。
- 每章结构：一个新需求到来 → 现有设计哪里接不住 → 机制落地（agent 始终真实可用）
  → 下一章的需求预告。
- 叙事纪律：不是造烂代码再修。每一章的 agent 在它的能力范围内都是真实可用的，
  接不住的是新需求，不是旧代码。
- 实现新机制时踩的坑必须是真的：优先用现有代码注释里记录过的真实坑（如
  asyncio.Queue 双队列竞速 500/500 丢事件），不编造。
- session log 从第 1 章就有，append-only。history 是 log 的投影（投影的完整定义
  在 04；压缩动作本身也进 log——手动压缩在 04，自动策略在 05）。
- 每章末尾有"验证"小节：跑了什么、实测输出是什么、有没有未实跑项，如实标注。
- 代码目录不跟书走，放 `src/baby_event_driven_agent/stages/`：pytest / uv / 包管理
  开箱即用，阶段目录自成包可独立运行；正文里每章开头给代码路径和关键 diff。

## Stage 1：只是接收事件（01-receive-events.md，定稿）

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

## Stage 2：收件箱 + 总线分方向（02-inbox-steering.md，定稿）

- 需求：回答还在跑，用户要能继续发消息——要么插进当前回答（steering），
  要么排到回答之后（followup）。
- 先诊断 Stage 1（结构性上限，不是 bug，量小的时候无害，一旦要排队和分方向就必须拆）：
  - **投递与执行绑死**：publish 一路 await 到整个 turn 结束，消息无处排队。
  - **publish 语义混杂**：同一个方法一会儿当"投递命令"（user_input）、一会儿当
    "扇出事件"（agent_delta），一条通道背两种语义。
  - **单 task 串行**：没有收件箱，也没有控制通道。
- 机制一：**总线分方向**。inbound 的 publish 收成同步入队——只把命令投进目标
  agent 的 inbox 就返回；outbound 拉出独立的 emit，负责把 agent 发出的事件扇出
  给订阅者（本章的 emit 仍是 await handler 的简单扇出，异步分发 / QoS / 背压 /
  治理留到 3b）。这是"投递与执行分离"的完整落地，属于 Stage 2 自己的需求，
  不是额外改动。
- 机制二：**收件箱 + 常驻 worker**。每个 session 一个收件箱加一个常驻 worker
  （首次消息时启动），worker 循环"取消息 → 跑 turn"。step 边界 drain 到的消息
  拼进当前上下文（steering）；worker 空闲后取到的消息开新 turn（followup）。
  turn_end 升级成总线真事件。
- 关键论点：分类时机在消费端，不在提交端。消息自己不背语义。
- 关键论点：publish 变同步（返回 None）之后，"调用方乱 create_task"在结构上就
  不可能了——串行不再靠调用方自觉，而由"收件箱 + 单 worker"保证。
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
  （等待时长取决于在飞请求，可能是 0.24 秒也可能是 8 秒）——用户想直接掐掉，
  而且掐完之后是结束还是接着干，得能自己定。

## Stage 3：打断与转向（interrupt + redirect）（03-interrupt.md，定稿）

- 需求：回答跑偏/等不及了，用户要打断正在飞的那一步；而"打断"有两种意图——
  只是想停（turn 结束），还是"我改主意了，接着干活"（turn 不结束，原地转向）。
- 一个原语：**把"正在飞的一步"变成可取消的 task**。
  - 每 session 记录当前在飞的 step task（_inflight）；on_interrupt 无 await 地
    查表并 cancel（原子，无信号残留问题）。
  - step 包成可取消单元（流消费 + 工具执行一起），工具结果由 turn 协程在 task
    成功后统一 append，保证 history 里永远是合法序列，被掐的 step 不留半截消息。
- 两种收尾策略：
  - **interrupt**：掐掉，turn 以 interrupted 收尾。取消粒度是单步不是执行体
    （worker 活着、history 完好）；取消与完成竞速，信号落在 task.done() 之后
    就是落空（如实打印）；中断不作废历史——被中断的问题留在 history，实测模型
    下一轮主动补答（不想保留就在中断时撤掉该 user 消息，两路都通）。
  - **redirect**：掐掉，turn 不结束。被掐断的 assistant 输出是残缺的，而 provider
    对消息序列格式是严格的，要补齐：usr → assistant → tool_result → assistant →
    tool_result，然后补一条真实 user message，同一 turn 内重发。已完成的工作保留，
    turn 不重来。
- 消息修复 Case 矩阵（照搬 hermes 笔记 books/hermes/redirect-message-shapes.md，
  源码基线 opensource-refs/hermes-agent，正文里实测重演；**单独成节，别被"取消"
  一笔带过**）：
  - 模型生成中、有可见文本：补 assistant（可见文本）+ user（新输入带上下文标注）
  - 模型生成中、纯 thinking：补 assistant 空壳
  - 尾巴已是 assistant：不补，折进 user
  - 工具执行中：降级为 steer（工具没法安全作废，hermes Case 4）
  - 响应已返回的竞态：丢弃响应按前三种处理
  - 同 turn 多次 redirect：文本累加
- 三兄弟对照（正文讲清边界）：
  - steering = 等 step 边界，不打断在飞的（Stage 2）
  - interrupt = 掐掉在飞的，turn 结束
  - redirect = 掐掉在飞的 + 补消息，turn 不结束，立刻重发
- 设计取舍（正文讲清为什么不开高优先级队列）：竞速取两个 asyncio.Queue 会
  静默丢事件（早期实现的真实坑）——中断不给队列就没有可丢的东西。
- stop()（进程收尾，连在飞的 step 和 worker 一起取消）与中断（单步）是两个入口，
  不要混。
- 下章需求预告：loop 现在往总线上 emit 的东西越来越多（生命周期 + token 流），
  上行的量一上来，总线和 UI 就顶不住了。

## Stage 3b：消息机制——事件离开 agent 之后要走多远（03b-message-mechanism.md，定稿）

（原 Stage 4「上行洪峰」与原 Stage 5「生产消息机制」合并：两者是同一个问题的两段——
进程内先被洪峰挤垮，跨进程才谈得上落盘与重放。章节重排后归位为 3b。）

- 需求：agent 在 loop 中间把生命周期事件和 token 流不断 emit 回总线，上行一下子
  变成高频、可丢、多观察者的大流量。CLI 流式呈现（回答一个字一个字往外蹦）只是
  它**看得见的样子**，不是原因。
- 真正的驱动：**上下行不对称**。下行（命令）低频、不可丢、要排队；上行（事件）
  高频、可丢、可合并。两者共用一条 await 到底的通道，迟早互相挤垮。
- 要过的坎（按出现顺序，每个都是被需求逼出来的）：
  1. 慢消费者拖死 loop → 上行必须异步分发
  2. token 流洪峰淹没 stop 信号 → QoS 分级，命令通道和流式通道分离
  3. UI 刷不动 → 背压合并缓冲：满了或者到了帧界就刷，二者其一（不能只等满，
     否则 UI 一顿一顿）
  4. 事件多了没法定 位 → 事件信封：type / session_id / seq / correlation id
     （seq 下一章要当轨迹坐标用：压缩区间、回放位点、eval 切片都靠它）
  5. 发出去的消息要不要管 → 拦截/否决/改写治理（现有 extensions.py 并入本章，
     篇幅压成一小节，细节可挪附录）。**并补上参考实现里没有的那半**：人工确认——
     规则只判"要不要问人"（当场，`Decision.ask`），答复由人给（之后，
     `approval_required` → future → inbound 的 `user_approval` 旁路 resolve）；
     超时按拒绝（fail-closed），一问必有一答（回执强制），迟到/号不对的答复也留痕
     （`approval_reply`），走 `bus.record` 这条同步落盘入口
  6. 事件多到内存放不下、进程一退就没了 → **落盘**：崩溃安全（长度前缀 + CRC）、
     分段与索引、保留与脱敏；以及**消费位点**（重放从哪开始、断点续放）
- 关键论点：上行和下行不对称。下行低频不可丢，上行高频可丢，混在一条通道
  迟迟早早出事故。
- 本章只强化 outbound：Stage 2 拉出来的 emit 从"简单 await 扇出"升级为
  "异步分发 + QoS + 背压 + 信封 + 落盘 + 位点 + 治理"，inbound 的 publish 不动。
- 边界声明：本章只解决"事件怎么到达它的消费者"（**传输层**），不解决"状态是什么"
  （Stage 4）。落盘、位点、重放在这里是工程实现，它们的语义由下一章定义。
- 降级项（按"需求真出现才上"的判据）：多进程部署 / Kafka / 多副本 / 按 session
  分区序 —— 本书的 agent 从未真的走向多进程，收成本章末一节"走出单机：真要多进程
  部署时"，或挪附录，不占整章。
- 下章需求预告：机制面齐了，业务面还是查库存的 demo——用户要甩来一张真正的
  任务单（3c）；会话本身的问题（状态、崩溃、上下文）随后由 04 / 05 接手。

## Stage 3c：能力升级——从四件套到一张真正的任务单（03c-agent-ability-upgrade.md，设计定稿，代码已落地）

（"场景基建"章：工具、业务世界、长程任务剧情一次定稿；04 与 05 骑在这个场景上
讲机制。配套代码与 04 同包：`stage04_trajectory`。）

- 需求：用户甩来一张任务单——"今天仓库的补货核查和盘点差异，你处理一下"。
  四件套接不住：**没有"任务"这个实体**（工作无法被发现，只能用户口述）；
  **撑不出长会话**（结果形态单一，一轮问答七条消息到头）——而回退、压缩、恢复
  全都以"会话真的会变长"为前提，没有素材，机制就是纸上谈兵。
- 落地机制：
  1. **任务域两工具**：list_tasks / get_task。任务单只读、无状态字段——进度住在
     会话里（这正是压缩要保的东西；一旦有状态文件，摘要的职责就被架空）。
     读写分类：六件套 = 4 读 + 2 写。
  2. **审批声明从"判工具"到"判量"**：`requires_approval: bool` 升级为
     `approval_check`（参数 → 问人的理由或 None），声明仍在工具身上。
     update_inventory 的线和 rules.txt 的补货规则是同一条：业务规则告诉模型
     "超 50 要报备"，工具声明告诉 harness"超 50 要问人"；判不了的参数按
     fail-closed 问人。
  3. **业务世界自带 data/**：inventory 副本扩 4 个品类、rules 副本加补货与
     盘点差异两条、tasks 新文件（2 张单 7 个条目，静态手写，确定性）；
     共享 knowledge-base 一个字节不动（前面各章的测试断言了它的具体值）。
     写操作保持**设值语义**（写目标值不是增量）：rewind / 重放之后哪怕重做，
     "补到 50"还是 50，副作用不叠加。
  4. **长程任务八幕剧情（全书贯穿示例）**：领任务 → 先干起来 → 用户纠偏 →
     规则路线（马克杯报备被拒）→ 换单 → 压缩 → 继续 → 崩一刀。
     七个条目四种结局：直接补 / 不用补 / 被拒 / 挂起——摘要才有真内容可压。
     3c 的 demo 演无轨迹的命运（只能往前追加、上下文只增不减、崩了全丢），
     04 的 demo 演轨迹的解法（branch / compact / resume）——同一张任务单，
     两种命运。
  5. **ScriptedLLM 回归 demo 脚手架**：离线段可断言、可反复跑（3b 起被删，
     本章请回来；04 正文承诺的"前几段离线"随之恢复成立）。
- 关键论点：工具是轨迹故事的道具，不是主角。分页（"截断必须配取回通道"）、
  批次句柄（写不可重放）是 05 为压缩策略配套的教学点，不提前搬。
- 下章需求预告：试错回不去（只能往前追加）、上下文压不下（无刀可用，消息数
  单调递增）、崩了全丢（EventLog 有账读不回"聊到哪了"）——04 接手。

## Stage 4：会话与真相——log、投影与异常恢复（04-trajectory.md，2026-09-26 实测定稿）

（原 Stage 5「会话与轨迹」拆分的前半，章节重排后归位为 4；与 3c 同包。）

- 需求（两个一起来，各驱动一半）：
  - 用户第二天回来说"接着上次报销的事聊"，agent 一问三不知；更糟的是进程重启后
    同一个 session_id 会静默变成一段新历史，而磁盘上还躺着上一段。
  - 上一条逼出 resume，resume 就撞上异常路径：崩在半路的 log 尾部长什么样、
    怎么判、怎么修——恢复不是附加题，是"以 log 为基准"的另一半。
- 落地机制（用的是 3b 造好的零件：落盘、位点、seq）：
  1. **会话身份与生命周期**：session_id 由 `SessionStore` 分配（不是调用方随口
     给）；`start` / `resume` / `close` 三入口，判据是"store 里有没有这个 sid"；
     `session_started` / `session_resumed` / `session_end` 都要进 log——否则一个
     log 文件里两段进程的历史首尾相接，回放时看不出中间断过（和 Stage 2
     "排队的消息连 log 里都没痕迹"是同一类坑）；同 session 单写者。
  2. **投影形式化**：三层模型——事实层（append-only log）/ 视图层（messages）/
     投影函数 `f(log, policy)`。Stage 1 起那句"history 是 log 的投影"在这里才
     完整。两条纪律（对照 pi / hermes 的实现讲，见 books/pi、books/hermes）：
     **占位符在投影层补、不写回 log**（hermes 的措辞："durable transcript kept
     them"——原始行原封不动，修复只作用于喂给模型的内存序列）；发给 provider
     前再有一道 sanitize 收口（pi 的 `transformMessages`），保证消息序列约束
     永远满足，修复分两档：只读断尾丢弃、有副作用的断尾补 UNKNOWN 占位。
  3. **异常恢复（逐级收口前面各章埋的线头）**：
     - 字节级残尾：长度前缀 + CRC 判定（3b 的落盘工程，这里只消费）——
       "没写完的不算已发生"，读到残尾为止，前面的一条不少；
     - 语义残尾·半截 turn：崩在半路的 log 尾部可能是"user + 半截 assistant"，
       或缺一半的 tool 结果——第二次复用 Stage 3 的消息形状表（第一次是
       redirect）修剪，同样遵守"投影层修，不改事实层"；
     - 语义残尾·悬挂审批：孤立 `approval_required`（后面没有 `approval_decided`）
       按"未授权"闭合，补 `abandoned` 裁决——"一问必有一答"在崩溃路径上也成立；
       人的答复晚到（没人在等）已由 3b 的 `record` 留痕，这里接得住；
     - 重放幂等：resume = 位点续读，checkpoint = 位点（读到哪）+ 视图快照
       （上下文是什么），平行 Kafka consumer offset，物理实现在 3b；
       重放不产生重复事实。
  4. **手动压缩（已落地，形状由 3c 锁定）**：compact_request 控制事件
     （旁路，同 interrupt 的待遇，step 边界生效）+ Summarizer 协议
     （ScriptedSummarizer 离线确定性 / LiveSummarizer 裸 chat 封顶）+
     追加 `compaction` entry（summary + keep_from_id，投影认第一刀不动）；
     demo 第 13 段实测：长任务的轨迹解法——幕 3 branch / 幕 6 compact /
     幕 8 resume，骑在 3c 的场景上（投影 32 → 13 条，全树 58 条原文都在）。
- 关键论点：append-only 的是**事实层**；视图层允许有损，但每一次有损变换都要在
  事实层留痕——否则 log 不是唯一真相，只是"唯一真相的一半"。
- 关键论点：恢复的前提有两个——log 本身**可判定**（写一半能认出来，3b 的
  工程）+ 事实**自描述**（读得懂"那次没走完"，本章的定义）。缺一个，恢复都
  只能靠猜。
- 下章需求预告：会话立住了、也能从崩溃里重建了，但会话一长上下文装不下；
  一旦压缩，"重放出来的上下文跟当时不一样"——可复现性没了。

## Stage 5：压缩与上下文——有损变换的纪律（05-compaction.md，设计稿，代码未落地）

（原 Stage 5 的后半，章节重排后归位为 5。压缩 = 对投影的又一次变换，
纪律从 4 继承。）

- 需求：会话跑长了上下文装不下；一旦压缩，"重放出来的上下文跟当时不一样"——
  可复现性没了。
- 落地机制：
  1. **压缩是事件**：`context_compacted {from_seq, to_seq, summary, policy_version,
     hash}` 本身进 log——有损变换在事实层留痕，Stage 4 那条关键论点的正面落地。
  2. **只在 step 边界触发**——与 steering / interrupt / redirect 并列的边界动作
     （手动压缩在 4 已落地为第四种边界动作，本章补自动触发）。
  3. **不变式**：给定 `(log, policy_version)` → 唯一 messages。这是 eval 的地基：
     golden trajectory 存事实层 + policy_version，不存压缩结果（Stage 6 用）。
  4. **大工具结果指针化**（blob + 引用 + hash），顺带解决 log 膨胀与脱敏。
  5. **自动触发策略**：按 token 检测（真实 usage 锚定 + 增量字符估算）、
     按轮次下刀（保留最近 N 轮，刀口落在 turn 边界）、水位与窗口、
     滚动折叠（新摘要吞旧摘要、投影认最后一刀——4 的"认第一刀"保守做法
     在此补全）、兜底阶梯（每一级请求体严格变小，fail-open 留痕）。
- 工具层：分页契约（"截断必须配取回通道"）与批次句柄（写不可重放、防压后重做），
  在 3c 的六件套上加参数、加工具——原"工具集扩充"一节由 3c 吸收 list/get_task。
- 收官闭环：session log 从 Stage 1 埋到现在终于闭环，重启回放重建状态。
- 下章需求预告：能跑了、能重放了，但"改了 prompt、换了模型、重构了 loop，行为
  有没有变坏"仍然没有答案。

## Stage 6：rubric 和 eval（06-rubric.md，骨架）

- 起点：agent 跑通了，但"改了 prompt、换了模型、重构了 loop，行为有没有变坏"
  没有答案。靠手感回归就是靠运气。eval 为薄弱环节的读者（包括作者自己）补课。
- 地基是前面各步攒下的：3b 的落盘与位点、4 的投影与恢复、5 的压缩
  不变式，正是 eval 的输入——eval = 重放录制好的 session + 对结果打分。
  没有 log 就没有可重复的 eval。
- 落地机制：
  - rubric：把"这个 agent 干得好不好"写成可判定的评分标准。硬断言和软评分分开：
    硬断言是确定性检查（工具调用序列对不对、redirect 补出来的消息格式合不合法、
    中断后状态完不完整）；软评分是 LLM-as-judge 按 rubric 清单打分。
  - eval harness：golden trajectories（录制）→ 重放 → 硬断言 + rubric 评分 →
    聚合报告。
  - Stage 2~3 的行为语义全部变成 eval 用例：steering 生效时机、redirect 竞态
    降级、ctrl 插队不丢事件——以前只能靠手点，现在进回归集。
  - golden trajectory 的粒度 = 一个 session：存**事实层 + policy_version**，不存
    压缩后的 messages（否则被某一次压缩策略绑死）；硬断言走事实层，judge 的输入
    走视图层、报告注明"模型当时看到的是压缩后的上下文"；换过压缩策略的两次评测
    不可直接比较，需标注。
- 要过的坎：LLM-as-judge 不稳定——同一轨迹两次打分不一样。处理：评分输出结构化
  （强制 JSON + 逐项理由）、rubric 逐条独立判、软评分只看趋势不当断言。
- 关键论点：event log 不只是调试用的，它是 eval 的数据源。append-only 是从
  Stage 1 贯穿到 Stage 6 的同一根线。
- 同一根线上的其他投影（后记）：eval 切片（`f(log, slice)`）和**记忆提取**
  （`f(log, memory_policy) → 记忆条目`）是对同一事实层的另外两种投影，与压缩
  同构——都受 4 的两条纪律约束：可以有损，但不改事实层；记忆条目要能回指 log
  （seq 区间），否则审计不了"这条记忆是从哪段经历来的"。
- 结尾：六步回头看一张对照表（问题 → 机制 → 代价）。

---

tool的设计，
- 异常兜底
- tool分类
- tool的可中断执行
- tool的安全检测，命令安全，权限安全
