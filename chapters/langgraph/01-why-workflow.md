# 为什么还需要工作流
尽管LLM的能力已经很强了，但是目前仍然无法完全解决LLM的幻觉和任务执行的不确定性。最近 Claude 带火的动态工作流（dynamic workflows，让agent根据任务目标设计工作流），本质上就是想办法用尽量确定性的执行过程，去框住 LLM 的幻觉和不确定性。

>Anthropic 自己在讲这套设计时，点名了单个 agent 在长链路里会经常犯的三个错：
>- **智能体偷懒（agentic laziness）**：活儿干了一半就宣布"搞定了"。你在vibe coding的时候，即便你在AGENTs.md明确要求必须通过所有的lint和验证，AI 仍然经常性的忽略掉他认为不是他引入的基础问题。
>- **自我偏好偏差（self-preferential bias）**：让 agent 自己审自己的产出，它倾向于觉得自己是对的。裁判和球员不能是同一个人。
>- **目标漂移（goal drift）**：跑得越久越容易忘了最初要干啥，尤其是上下文被压缩过之后，那些"别做 XX"的负向约束最先丢。

这三个毛病都是当下在Transformer架构下，在长上下文场景中，注意力漂移的真实表现。哪怕你用 skill（甚至一堆 skill 串成的 skill set）去约束，也未必能到 100%。因为 skill 跑在 agent loop 里，"下一步调谁、调完干嘛"终究是 LLM 现场拍板的，没有强制保证，偷懒、漂移照样可能发生。

在可严格划分工作阶段的流程场景里，想获得更高的确定性，把流程顺序钉死，把每个环节的上下文隔离开，工作流仍是最好的方式。


# 工作流有哪些形态
先看看常见的工作流形态有哪些，

**Prompt chain**，顺序图，分步骤逐个顺序处理，上游节点的输出直接作为下游节点的输入。

  **典型场景**：
  - **文档翻译流水线**：原文提取 → 初译 → 术语统一 → 母语者润色，每步的产物都是下一步的输入
  - **简历解析**：PDF 文本提取 → 结构化字段抽取（姓名/经历/技能）→ 岗位匹配度评分
  - **数据 ETL**：从异构数据源抽取 → 清洗去重 → 标准化格式输出

```mermaid
%%{init: {'theme':'forest'}}%%
graph LR

_START_ --> NODE1
NODE1 --> NODE2
NODE2 --> __END__
```

- **Orchestrator and Worker**，编排-并行-汇聚，Parallel (额外依赖 SEND)。Orchestrator 负责任务拆解与分派，Worker 并行执行，Synthesizer 汇总结果。

  **典型场景**：
  - **行业调研报告**：Orchestrator 拆解为市场分析、竞品研究、技术趋势三个子任务 → 三个 Worker 并行搜集分析 → Synthesizer 整合为完整报告
  - **多维度代码审查**：Orchestrator 将 PR 分派给安全检查 Worker、性能分析 Worker、代码风格 Worker 并行审查 → Synthesizer 汇总审查意见
  - **多渠道信息聚合**：用户提问"公司X的最新动态"→ Orchestrator 同时调度新闻搜索 Worker、财报分析 Worker、社交媒体舆情 Worker 并行抓取 → Synthesizer 汇总为一份情报简报

> 通用 Agent 中的主子 Agent，也是类似于 Orchestrator-Worker 的工作方式，由主 Agent 通过 TOT 的方式推理出任务，主 Agent 调用 task 工具拉起子 Agent 并行处理后统一返回给主 Agent

```mermaid
%%{init: {'theme':'forest'}}%%
graph LR

_START_ --> Orchestrator
Orchestrator --> Worker1
Orchestrator --> Worker2
Worker1 --> Synthesizer
Worker2 --> Synthesizer
Synthesizer --> __END__
```

- **Router**，分类器，由 Router 节点识别输入特征并路由到对应的 Worker 节点。Router 本身不做业务处理，只做分发决策。

  **典型场景**：
  - **智能客服工单分流**：Router 识别用户意图 → 退换货请求走售后 Worker、技术问题走技术支持 Worker、投诉建议走客诉 Worker
  - **内容审核平台**：Router 按内容类型分发 → 图文走 OCR + 敏感词 Worker、视频走帧抽取 + 审核 Worker、长文走语义理解 Worker
  - **多模型调度网关**：Router 按任务复杂度选模型 → 简单问答走轻量模型 Worker、复杂推理走强模型 Worker、图像任务走视觉模型 Worker，兼顾成本与效果

```mermaid
%%{init: {'theme':'forest'}}%%
graph LR

_START_ --> Router
Router --classA--> Worker1
Router --classB--> Worker2
Worker1 --> _END_
Worker2 --> _END_
```

- **Generator and Evaluator**，生成-评估循环。Generator 生产内容，Evaluator 按规则或模型做对抗评审，不通过则打回重生成，直到满足质量标准或达到最大迭代次数。

  **典型场景**：
  - **AI 代码生成 + 自审查**：Generator 生成代码 → Evaluator 运行 lint / 测试 / 安全扫描，不通过则带着错误信息打回重写
  - **合同条款生成**：Generator 草拟条款 → Evaluator 做合规性与风险审查，有歧义或合规问题则退回修正
  - **教学题目生成**：Generator 出题 → Evaluator 校验难度、知识点覆盖度、答案唯一性，不达标则重新出题
  - **文案 A/B 优化**：Generator 产出广告文案 → Evaluator 按品牌调性、转化率指标、敏感词做多维度打分，低于阈值则重新生成

```mermaid
%%{init: {'theme':'forest'}}%%
graph LR

_START_ --> Generator
Generator --> Evaluator
Evaluator --reject--> Generator
Evaluator --approve--> _END_
```


- Workflow 中也可以继续**嵌套子图 workflow**来实现更高级（复杂）的工作流，将一组节点封装为可复用的子图模块。

  **典型场景**：
  - **多层审批流程**：将"部门审批→总监审批→法务审批"封装为一个审批子图，外层只需关注业务流程，审批细节在子图内部闭环
  - **微服务编排**：将每个微服务调用链封装为子图（如"用户注册"包含创建账号→发送验证→初始化工作空间），上层编排层只看子图的输入输出

```mermaid
%%{init: {'theme':'forest'}}%%
graph LR
    subgraph OrchestratorBox [Orchestrator Group]
        OrchestratorNode[Orchestrator]  
        Worker1
        Worker2                         
        Synthesizer
    end
    __START__ --> NODE1
    NODE1 --> OrchestratorNode
    OrchestratorNode --> Worker1
    OrchestratorNode --> Worker2
    Worker1 --> Synthesizer
    Worker2 --> Synthesizer
    Synthesizer --> __END__
```

- **Agent**，在 LangGraph 的语境下，本质就是一个有限循环（`<= max turn`）的 Loop 工作流：LLM 思考 → 决策（调工具或直接回答）→ 调用工具获取反馈 → 再思考，直到 LLM 认为可以终止。

 上面六种 Workflow 模式都可以组合成更复杂的 Agent。比如一个客服 Agent 可能是：Router（意图分发）→ Orchestrator-Worker（复杂任务拆解并行）→ Generator-Evaluator（回复生成自审）的组合体。

上面六种模式，基本就是日常工作流Agent的全部姿势了。道理不复杂，但真要跑起来，你得有个框架把节点、边、分支、人工审批点真正编排和管理好——出错能恢复、运行能追踪、还能让人中途插手。能支持这些能力的有 Temporal、Prefect 这类通用编排引擎；也有专门面向 LLM Agent 的框架，比如LangGraph，LangGraph 是最早推出、生态也最完整的一个LLM 工作流编排框架：它把上面这些模式直接变成一张可以编译、可以存档、可以人工干预的图。

下一章开始，我们就一起深入学习一下LangGraph。