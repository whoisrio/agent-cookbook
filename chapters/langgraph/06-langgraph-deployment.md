# 部署

LangGraph 有三种跑法，核心区别就一句话：图在你自己进程里跑，还是起个 server 跑，还是丢给云平台跑。

## SDK 嵌入

就是 `uv add langgraph`，直接在自个儿后端进程里 import、compile、invoke。图和你应用跑一块。

优势：不用单独起服务，跟着后端一起部署，数据库、鉴权这些基础设施全复用；checkpointer、store 你想接 sqlite 还是 postgres 还是自己写的都行，完全自己控；数据不出你环境。

劣势：没有 LangGraph 那套 API server。前端库依赖的 EventStream、Studio 可视化调试、并发任务管理这些用不上（LangSmith trace 不受影响，设个环境变量本地就能自动追踪）。想要前端对接的话得自己用 FastAPI 把图包成流式接口，等于把后面两种的活自己干一部分。

## Agent Server 独立部署

按照Agent Server的方式来部署Langgraph Agent，能够享受LangSmith 提供的很多基础能力，先理解一下 Agent Server部署的整体架构，主要是两大组件：

- API Servers，提供Agent Server的后端API服务，管理workflow配置(assistant)，启动，运行workflow 等等，配合前端提供的Langgraph库，可以很方便的构建workflow UI；
- Worker Containers，Workflow的真正的运行时容器；

基础依赖:
- Redis, API Server和 Workers 的消息交互中心，API Server和Worker是通过Pub/Sub方式交互；
- PostgreSQL，Store、Checkpoint的默认存储，Workflow相关配置存储；
- MongoDB，可选，可以将Workflow运行的Check point存储到MongoDB中；

API Servers和Worker Containers，支持集中部署在一块或者分布式的部署

```mermaid theme={"theme":{"light":"catppuccin-latte","dark":"catppuccin-mocha"}}
flowchart TB
    User["User"]

    API["API Servers"]

    subgraph WorkerContainer["Worker Containers"]
        QueueLoop["Queue Loop"]
        W1["Worker"]
        W2["Worker"]
        Wn["..."]
        QueueLoop -->|dispatch| W1
        QueueLoop -->|dispatch| W2
    end

    DB[(Postgres)]
    Redis[(Redis)]

    User -->|request| API
    API -->|create run| DB
    API -->|notify| Redis

    Redis -->|wake| QueueLoop
    QueueLoop -->|claim next run| DB

    WorkerContainer -->|save checkpoints / update status| DB
    WorkerContainer -->|publish events| Redis

    Redis -->|stream events| API
    API -->|SSE response| User

    style User fill:#F2FAFF,stroke:#40668D,stroke-width:2px,color:#2F4B68
    style API fill:#EBD0F0,stroke:#885270,stroke-width:2px,color:#441E33
    style DB fill:#E5F4FF,stroke:#006DDD,stroke-width:2px,color:#030710
    style Redis fill:#F8E8E6,stroke:#B27D75,stroke-width:2px,color:#634643
    style WorkerContainer fill:#F6FFDB,stroke:#6E8900,stroke-width:2px,color:#2E3900
    style QueueLoop fill:#FDF3FF,stroke:#7E65AE,stroke-width:2px,color:#504B5F
    style W1 fill:#F2FAFF,stroke:#40668D,stroke-width:2px,color:#2F4B68
    style W2 fill:#F2FAFF,stroke:#40668D,stroke-width:2px,color:#2F4B68
    style Wn fill:#F2FAFF,stroke:#40668D,stroke-width:2px,color:#2F4B68
```


**Assistant**:

Workflow(Graph)设计完成之后，在Graph节点使用不同的prompt和模型或者其他参数等，可以用于不同的场景，
- 比如业务场景相同的A用户偏好GPT，B用户偏好GLM
- 比如相同的Workflow流程，使用不同的模型或者Prompt做 A|B 测试验证
- 或者特定的任务，指定特定模型来进行迭代
等等等

![assistant](https://mintcdn.com/langchain-5e9cc07a/IMK8wJkjSpMCGODD/langsmith/images/assistants.png?w=1100&fit=max&auto=format&n=IMK8wJkjSpMCGODD&q=85&s=c54cde5d8a052ceac26d67131407aa73)

AgentServer 将这个概念包装成 **Assistant**，通过AgentServer的接口可以指定Graph来创建Assistant，通过Assistant来管理一整套Graph实例化后的运行时自定义上下文，按照版本的维度进行管理；

创建assistant，
```python
from langgraph_sdk import get_client

# Initialize the client with your deployment URL
client = get_client(url=<DEPLOYMENT_URL>)

# Create an assistant for the "agent" graph
# The first parameter is the graph ID (also called graph name)
openai_assistant = await client.assistants.create(
    "agent",  # Graph ID of the deployed graph
    context={"model_name": "openai"},
    name="Open AI Assistant"
)

print(openai_assistant)
# Output includes the assistant_id (UUID) that uniquely identifies this assistant
```

指定Assistant运行workflow
```python
# Create a thread for the conversation
thread = await client.threads.create()

# Prepare the input
input = {"messages": [{"role": "user", "content": "who made you?"}]}

# Run the graph using the assistant's configuration
# Pass the assistant_id (UUID) as the second parameter
async for event in client.runs.stream(
    thread["thread_id"],
    openai_assistant["assistant_id"],  # Assistant ID (UUID)
    input=input,
    stream_mode="updates",
):
    print(f"Receiving event of type: {event.event}")
    print(event.data)
    print("\n\n")
```


graph节点中，需要根据不同的运行时来获取的配置(比如prompt|model|model的参数等等)，node的执行逻辑，从上下文获取对应的参数，在启动graph运行时，指定对应Assistant和版本

```python
from langgraph.runtime import Runtime

def node_a(state: State, runtime: Runtime[ContextSchema]):
    llm = get_llm(runtime.context.llm_provider)
    # ...
```

>如果不用Agent Server方式的部署，自行管理配置注入到上下文，也是可以的

更多详细的介绍，参考 [Agent-server官方文档介绍](https://docs.langchain.com/langsmith/agent-server)


### Assistant、Thread、Run 三者的关系

代码里反复出现三个对象：assistant、thread、run。先把各自是什么说清楚，再讲怎么配合。

首先，Assistant 是"图的配置实例"。图（graph）本身是中性的，跑的时候得指定用哪个模型、哪套 prompt、什么参数，这些运行时配置打包在一起就是一个 Assistant。同一份图可以配出好几个 Assistant——A 用户用 GPT、B 用户用 GLM，就是两个 Assistant。Assistant 带版本，改了配置升一版，方便 A/B 比对和回滚。

其次，Thread 是"这一次任务的上下文容器"。起 run 之前先建一个 thread，之后所有 run 都跑在这个 thread 上。图每一步的快照（checkpoint）都挂在 thread 下面。同一个 thread 上多次 run，上下文是连续的——多轮对话、interrupt 暂停后隔天回来点审批、崩了从最近的 checkpoint 续跑，全靠 thread 把状态存着。不同用户的任务用不同 thread，天然隔离，互不干扰。

另外，Run 就是"在 thread 上跑一次 assistant"。一次用户输入触发一个 run，图从起点走到终点（或者卡在 interrupt 等人），这就是一个 run。一个 thread 上可以跑很多个 run——多轮对话就是一轮一个 run，串在同一个 thread 里。

三句话记关系：Assistant 管"怎么跑"（配置），Thread 管"这次跑的上下文"（状态），Run 管"跑的那一次动作"（执行）。代码里的顺序也是这个：建 assistant、建 thread、用 `client.runs.stream(thread_id, assistant_id, ...)` 起 run。

为什么一定要 Agent Server 才有意义：SDK 嵌入时 state 在你进程内存里，进程一重启就没了；Agent Server 下 thread 的 checkpoint 落在 Postgres（就是架构图里那个 DB），进程挂了、浏览器关了，thread 还在，下次 resume 接着跑。前面讲的 HITL 和 time travel，底层都站在这个 thread + checkpoint 机制上。


#### 样例

```mermaid actions={false} theme={"theme":{"light":"catppuccin-latte","dark":"catppuccin-mocha"}}
flowchart TB
    subgraph deploy[部署]
        G[图代码<br/>━━━━━━━━━<br/>已部署的逻辑]
    end

    subgraph config[配置]
        A1[助手1<br/>GPT-4，正式风]
        A2[助手2<br/>Claude，随意风]
    end

    subgraph state[状态]
        T1[线程1<br/>用户A]
        T2[线程2<br/>用户B]
    end

    subgraph runs[Runs]
        A1T1["Run: A1 + T1"]
        A1T2["Run: A1 + T2"]
        A2T1["Run: A2 + T1"]
    end

    A1 -.-> T1
    A1 -.-> T2
    A2 -.-> T1

    A1T1 --> G
    A1T2 --> G
    A2T1 --> G

    style G fill:#E5F4FF,stroke:#006DDD,stroke-width:2px,color:#030710
    style A1 fill:#B3E0F2,stroke:#4A90E2,stroke-width:2px,color:#1E3A5F
    style A2 fill:#B3E0F2,stroke:#4A90E2,stroke-width:2px,color:#1E3A5F
    style T1 fill:#FFE0B3,stroke:#7E65AE,stroke-width:2px,color:#504B5F
    style T2 fill:#FFE0B3,stroke:#7E65AE,stroke-width:2px,color:#504B5F
    style A1T1 fill:#B3F2C9,stroke:#10B981,stroke-width:2px,color:#2E3900
    style A1T2 fill:#B3F2C9,stroke:#10B981,stroke-width:2px,color:#2E3900
    style A2T1 fill:#B3F2C9,stroke:#10B981,stroke-width:2px,color:#2E3900
```

这张图演示了一个 **run** 是怎么把 assistant 和 thread 组合起来、去执行那张图的：

* **Graph（浅蓝）**：你部署的代码，装着 agent 的逻辑
* **Assistant（蓝）**：配置项（模型、prompt、工具）
* **Thread（橙）**：装着对话历史的状态容器
* **Run（绿）**：一次执行，把某个 assistant + 某个 thread 配对起来跑

**组合的例子：**

* **Run：A1 + T1**：用助手1的配置去跑用户A的对话
* **Run：A1 + T2**：同一个助手在服务用户B（不同的对话）
* **Run：A2 + T1**：换一个助手去跑用户A的对话（配置切换了）

跑一个 run 时要注意：

* 每个 run 可以有自己的输入、配置覆盖和元数据。
* run 可以无状态（不挂 thread），也可以有状态（挂在一个 [thread](https://docs.langchain.com/langsmith/use-threads) 上以保存对话）。
* 多个 run 可以共用同一份 assistant 配置。
* assistant 的配置会影响底层图怎么执行。



### Agent Server 的其他关键能力（速览）

Agent Server 除了上面这些概念，还内置了一堆能力。这块先不展开，把清单列出来，用到哪个去官方文档查（下面链接都是用 docs-langchain 这个 MCP 从官方文档实搜出来的真实页面）：

- **后台运行（Background Runs）**：长任务（跑几十分钟那种）不占连接，支持轮询拿状态、Webhook 跑完通知、断线重连、心跳保活。短任务用普通 run，长任务用它。详见 [background-run](https://docs.langchain.com/langsmith/background-run)
- **Double-texting 防护**：用户在前一条响应没回来时又发一条，内置 reject / queue / rollback / step-back 等策略处理，实时交互必用。详见 [double-texting](https://docs.langchain.com/langsmith/double-texting)
- **MCP 端点（/mcp）**：你的 graph 自动暴露成 MCP server，任何 MCP 客户端能直接连，不用自己写适配层。详见 [server-mcp](https://docs.langchain.com/langsmith/server-mcp)
- **服务端鉴权（Custom Auth）**：用自己的函数返回 user / permissions，每个 node 通过 runtime config 拿到，支持 JWT / API key / mTLS。详见 [custom-auth](https://docs.langchain.com/langsmith/custom-auth)
- **定时任务（Cron）**：按 cron 表达式定时起 assistant，比如每天发摘要邮件。详见 [cron-jobs](https://docs.langchain.com/langsmith/cron-jobs)
- **部署版本与上线（Revisions）**：deploy 出 revision，promote 从 staging 到 prod，支持灰度。详见 [deployments API](https://docs.langchain.com/api-reference/deployments-v2/list-revisions)
- **API 级时间旅行**：通过 API replay 某一步、fork 出分支，对应前面讲的 time travel。详见 [human-in-the-loop-time-travel](https://docs.langchain.com/langsmith/human-in-the-loop-time-travel)
- **无状态水平扩展**：server 实例无状态，worker 走任务队列，流量突增直接加实例。详见 [agent-server 总览](https://docs.langchain.com/langsmith/agent-server)
- **Studio 可视化调试**：本地 step through nodes、运行中改 state、fork 分支，和 time travel 呼应。详见 [agent-server 总览](https://docs.langchain.com/langsmith/agent-server)

官方文档入口（都是 MCP 实搜出来的真实页面）：

- Agent Server 总览：https://docs.langchain.com/langsmith/agent-server
- Assistants 概念：https://docs.langchain.com/langsmith/assistants
- 持久化（threads / checkpoints / store）：https://docs.langchain.com/oss/python/langgraph/persistence
- 线程用法：https://docs.langchain.com/langsmith/use-threads
- API Reference：https://docs.langchain.com/langsmith/server-api-ref
- 自托管 / 扩展：https://docs.langchain.com/langsmith/self-hosted 、https://docs.langchain.com/langsmith/agent-server-scale


### 本地部署

用 langgraph-cli 在本地起 server，从 langgraph.json 加载你的图。这里其实有两种模式，差很远，别混为一谈：

- `langgraph dev`：轻量开发服务器，不用 Docker，装 `langgraph-cli[inmem]` 就能跑（默认端口 2024）。状态存内存、顺手 pickle 到本地目录，改代码热重载，自带 IDE 调试。适合本地快速迭代。
- `langgraph up`：模拟生产的测试环境，要 Docker（默认端口 8123）。它把你的代码打成镜像，起 API server、PostgreSQL、Redis 三个容器，状态全落 Postgres，最贴近上云后的真实表现。

优势：本地就有完整的 API server + EventStream，前端库直接对接，还能连 Studio 本地调，开发体验和上云一致；数据不出本机，不花钱。

劣势：单台机器跑，没高可用、没自动扩缩容、没团队协作那层管理 UI；`langgraph up` 还得背 Docker 和一堆依赖（具体看下面）。真上生产还是得出去。


#### 本地部署的限制与依赖

先说 `langgraph dev`：

状态是内存 + 本地 pickle，不是真正的数据库。进程一重启，内存里的 checkpoint 就没了；pickle 到本地目录能留一部分，但本质是开发玩具，不能当生产持久层。

再说 `langgraph up`（本地最能打的部署，限制也在这）：

依赖 Docker：要拉 `langchain/langgraph-api` 基础镜像，起三个容器（API server、PostgreSQL、Redis——Redis 负责事件实时推送的 pubsub），吃内存和磁盘。资源不够就跑不起来，官方建议给 Docker 留够 RAM；不够就退回 `langgraph dev`。

依赖 LangSmith key，而且得联网校验 license：本地测试得有 LangSmith API key；生产用途还要 license key，server 会周期性去 LangChain 那边验证 license、统计跑了几个 run。也就是说它不是完全离线/私有化，得能连外网做授权校验。

单机，没有高可用：所有容器跑在一台机器上，没有自动扩缩容、没有多副本、没有跨实例容灾。流量一上来只能手动加资源。

没有托管的团队能力：团队共享的 Studio、部署版本灰度（revisions/promote）、托管 cron 面板这些是云平台（或企业自托管）才有。`langgraph up` 本身有运行时能力（后台运行、double-texting、MCP 端点、cron、自定义鉴权），但缺那层管理和协作 UI。

存储后端受限：checkpoint/store 默认 Postgres；想换 Mongo 得额外配，而且 Postgres 仍然必须留着存其它 server 数据（assistants、threads、runs、cron）。

一句话：本地部署适合开发调试和单机验证，别拿它当生产环境。要真上生产，要么接真数据库 + 自己做高可用（本质是在复刻平台干的事），要么直接上云端。


### 云端部署（LangGraph Platform）

把图部署到 LangChain 托管的 Platform，或者企业自托管。

优势：server 不用你运维，自动扩展，持久化 checkpointer 平台管，认证、多租户、cron、LangSmith trace 全有，Studio 团队共享，前端库直接对接。

劣势：花钱；数据出你环境（介意就选自托管或私有部署）；粘 Platform 的部署格式和 API，定制受平台限制（checkpointer、store 得用兼容的）；有网络延迟。

三种方式最终怎么选，放到下一节《本地 vs 云端：核心差异》末尾统一给一份带代价的对照，这里不重复。


### 本地 vs 云端：核心差异

把两边放一块比，主要差这几条：

首先是运维。本地你管 Docker、管容器生命周期、管资源；云端 server 不用你运维，扩缩容、故障转移平台兜底。

其次是数据和合规。本地数据全在你机器或 Docker 里，不出环境；云端托管版数据会进 LangChain 的基础设施（介意就选自托管或私有部署）。

然后是能力和协作。云端有团队共享 Studio、部署版本灰度、托管 cron、自动扩缩、多租户；本地 up 有运行时能力，但缺这层管理 UI。

最后是成本和依赖。本地不收平台费，但要自己出机器资源，且 up 模式强制依赖 Docker + LangSmith key + 联网 license 校验；云端按用量或套餐付费，省心，但数据出环境、定制受平台约束（checkpointer、store 得用兼容的）。

落到选型没有标准答案，看你在意什么、又能放弃什么：

- 你已经有后端、想把图当库直接调，或者合规上连托管平台都不能碰——选 SDK 嵌入。代价是 Agent Server 那套 API、Studio、并发和后台能力（background runs、double-texting、cron）得自己补，或者接受没有。
- 你在本地开发、想用和上线一样的 API 和 Studio 调试——起本地 server：`dev` 快速迭代，`up` 更逼近生产。但它只是单机验证，别当生产环境。
- 你要的是不运维、要自动扩缩、要团队共享 Studio 和灰度——上云端（数据敏感又想要这些，走企业自托管，数据仍留自己机房）。代价是花钱、数据出环境、checkpointer/store 受限平台兼容。

顺带说一句：数据敏感不等于只能 SDK 嵌入。本地 server 和自托管平台数据也都不出你环境，区别只在你要不要那层托管能力。



