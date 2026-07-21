## 进阶功能

根据State，Node和ConditionalEdge，基本上你就能搭建出任意你想得到的工作流姿势了，但是要让你的工作流变得更可靠，那么还需要引入LangGraph提供的高级一些的能力。

 - HITL，Human In The Loop，在关键的决策节点，引入人工确认；
 - Checkpoint，记录Graph的运行快照，基于Checkpoint的能力，可以方便的将工作流从错误中恢复；
- Store，提供用户粒度的长期记忆能力，支持语义检索；
- Time Travel，从任意的Checkpoint节点，重放工作流或者注入不同的参数Fork出一个新的执行分支；

下面，我们挨个来看
#### checkpoint，Graph存档
Checkpoint让每一次Graph运行，以Super Step的粒度生成graph的运行快照，是**中断后恢复**的基础，也是问题定位和重试基础；如下Graph，checkpoint会在__START__ | genJoke | translate 节点执行后保存checkpoint快照，默认情况下是保存节点执行结果的完整信息

```shell
__START__ -> genJoke -> translate -> __END__
```

>一般情况下，每一个节点都是一个Super Step，当执行步骤会同时拉起多个节点并行执行的时候，这些并行执行的节点归为一个Super Step

checkpoint的存储介质，可以在内存或者数据库，langGraph提供了一些列默认的实现，内存、本地数据库、RDB；

| Backend                                                                                                         | Package                                                                                      | Source                                                                                                                          |
| --------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| [In-memory](https://reference.langchain.com/python/langgraph.checkpoint/memory/InMemorySaver)                   | [`langgraph-checkpoint`](https://pypi.org/project/langgraph-checkpoint/)                     | [langchain-ai/langgraph](https://github.com/langchain-ai/langgraph/tree/main/libs/checkpoint)                                   |
| [SQLite](https://reference.langchain.com/python/langgraph.checkpoint.sqlite/SqliteSaver)                        | [`langgraph-checkpoint-sqlite`](https://pypi.org/project/langgraph-checkpoint-sqlite/)       | [langchain-ai/langgraph](https://github.com/langchain-ai/langgraph/tree/main/libs/checkpoint-sqlite)                            |
| [PostgreSQL](https://reference.langchain.com/python/langgraph.checkpoint.postgres/PostgresSaver)                | [`langgraph-checkpoint-postgres`](https://pypi.org/project/langgraph-checkpoint-postgres/)   | [langchain-ai/langgraph](https://github.com/langchain-ai/langgraph/tree/main/libs/checkpoint-postgres)                          |
| AWS (DynamoDB, Bedrock, Valkey)                                                                                 | [`langgraph-checkpoint-aws`](https://pypi.org/project/langgraph-checkpoint-aws/)             | [langchain-ai/langchain-aws](https://github.com/langchain-ai/langchain-aws/tree/main/libs/langgraph-checkpoint-aws)             |
| MongoDB                                                                                                         | [`langgraph-checkpoint-mongodb`](https://pypi.org/project/langgraph-checkpoint-mongodb/)     | [langchain-ai/langchain-mongodb](https://github.com/langchain-ai/langchain-mongodb/tree/main/libs/langgraph-checkpoint-mongodb) |
| Azure Cosmos DB NoSQL                                                                                           | [`langchain-azure-cosmosdb`](https://pypi.org/project/langchain-azure-cosmosdb/)             | [langchain-ai/langchain-azure](https://github.com/langchain-ai/langchain-azure/tree/main/libs/azure-cosmosdb)                   |
| Redis                                                                                                           | [`langgraph-checkpoint-redis`](https://pypi.org/project/langgraph-checkpoint-redis/)         | [redis-developer/langgraph-redis](https://github.com/redis-developer/langgraph-redis)                                           |
| [Cockroach DB](https://docs.langchain.com/oss/python/integrations/providers/cockroachdb#langgraph-checkpointer) | [`langchain-cockroachdb`](https://pypi.org/project/langchain-cockroachdb/)                   | [cockroachdb/langchain-cockroachdb](https://github.com/cockroachdb/langchain-cockroachdb)                                       |
| [Aerospike](https://docs.langchain.com/oss/python/integrations/providers/aerospike#langgraph-checkpointer)      | [`langgraph-checkpoint-aerospike`](https://pypi.org/project/langgraph-checkpoint-aerospike/) | [aerospike-community/aerospike-langgraph](https://github.com/aerospike-community/aerospike-langgraph)                           |

使用checkpoint，在graph运行的时候要指定threadId；

>threadId在这里并不是指单纯的线程，langGraph是通过threadId来归类graph运行实例，根据threadId在存在并发场景的时候控制graph的运行；

```python
{"configurable": {"thread_id": "1"}}
```

#### HITL，Human In the Loop，人工交互
再来看看HITL(Human In the Loop)，启用HITL，依赖checkpoint的能力，workflow运行到需要人工确认的节点时，会抛出中断异常，人工确认之后，workflow从中断节点开始重新执行；

因此，需要使用HITL的时候，应该在对应的执行步骤的初始逻辑即抛出HITL的中断，避免数据丢失；
##### Interrupt，中断
抛出中断，调用LangGraph提供的`interrupt`方法，


```python
from langgraph.types import interrupt
```


interrupt接受json参数，参数的属性框架没有约束，只要是可序列化的json对象即可，在实际使用interrupt发出中断时，需要和客户端约定好参数的属性，一部分用于呈现当前中断时workflow的处理状态(比如下方代码的question)，一部分用于给用户操作的指导(比如下方代码的review部分)；

```python
interrupted = interrupt({
            "question": f"How do think about this joke ,{state['content']}",
            "review": f"here is the review result, {state['reviewResult']}"
        })

        print(f'\n human comment: {interrupted.get("comment","")}')
        if interrupted["is_approved"]:
            return "translate"
        else:
            return "genJoke"
```

在这个示例中，interrupt恢复时返回的 `interrupted` 变量在恢复时通过Command传入的信息，`{ approved: true, editedResponse: "..." }`。
##### Command
中断的恢复，通过Command来恢复，`interrupt` 方法接受的返回值是any，需要和Command执行resume传入的参数对齐
```python
// 模拟人工审核后恢复执行
resumed = workflow.invoke(Command(resume={"is_approved":True,"comment":"i think it's good"}), config={"configurable": {"thread_id": "foo", "max_retries": 1}})
```

>Command除了恢复中断，还可以用于node中的执行逻辑控制，在HITL的场景，配合Interrupt，Command必须使用 resume的模式


##### 阶段示例
为我们的genJoke工作流添加上HITL的能力，当review节点判定joke not funny时，由人工介入评审Joke是否funny，关键改造如下:

- Grpah compile时指定checkpoint的持久化方式；
```python
# grpah compile时指定checkpoint的持久化方式
workflow = graph.compile(MemorySaver())

# 运行时指定 thread_id
result = workflow.invoke(
{"topic":"wednesday","content":"","reviewResult":None,"retryCount":0},
    {"configurable": {"thread_id": "foo", "max_retries": 1}},version="v2")
    
```


- 为了方便获取humanReview的信息，在AgentState中增加了humanReviewResult的属性；

```python
class CriticResult(BaseModel):
    isFunny: bool = Field(description="is joke funny or not")
    opinion: str = Field(description="how to improve")



class AgentState(TypedDict):
    topic: str
    content: str
    reviewResult: Optional[CriticResult]
    humanReviewResult: Optional[CriticResult]
    retryCount: int
```
- 增加 humanReview节点，用于处理humanReview的中断，当AI尝试次数超过最大次数仍然没有自动评审通过，则抛出中断；
```python
def humanReview(state: AgentState):
    config = get_config()
    max_retries = config.get("configurable", {}).get("max_retries", 3)

    interrupted = None
    if state["retryCount"] >= max_retries:
        print(f"⚠️ 达到最大重试次数 {max_retries}，need human approve")
        interrupted = interrupt({
            "question": f"How do think about this joke ,{state['content']}",
            "review": f"here is the review result, {state['reviewResult']}"
        })

    humanReviewResult = None
    if interrupted:
        humanReviewResult = {"humanReviewResult":CriticResult(isFunny=interrupted["is_approved"],opinion=interrupted["comment"])}
    
    return humanReviewResult
```

- 增加decideNextStep的条件边，用于判断综合的review结果；

```python
def decideNextStep(state: AgentState) -> Literal["genJoke","translate"]:
    if state["humanReviewResult"]:
        print(f'## human decision: {state["humanReviewResult"]}')
    if state["humanReviewResult"] and state["humanReviewResult"].isFunny or state["reviewResult"] and state["reviewResult"].isFunny:
        return "translate"
    
    return "genJoke"
```

- 模拟中断恢复，
```python

if result.interrupts:
    resumed = workflow.invoke(Command(resume={"is_approved":True,"comment":"i think it's good"}), config={"configurable": {"thread_id": "foo", "max_retries": 1}})
    final = resumed
```

#### Store
Store是为了保存和获取用户的长期记忆而设计的，所以memory的标识是按照namespace的维度来标识的，namespace由用户定义，一般由userId和memory的类型来组成namespace的标识

```python
user_id = "1"
namespace_for_memory = (user_id, "memories")
```

langgraph也提供了InMemoryStore，Sqllite，PG，Mongo，Redis作为Store存储的实现，当然也可以自行扩展。

##### 写入

写入memroy的时候按照命名空间来写入，如下示例中命名空间指定为`("user-x","memory")`
```python
from langgraph.store.memory import InMemoryStore

store = InMemoryStore()
memory_ns = ("user-x","memory")

store.put(memory_ns,"mem1",{"joke_preference":"dark humor"})
```

##### 检索
不同的store backend的存储和查询行为可能不一致，比如InMemoryStore默认按照Insert顺序降序返回； PostgreSql会按照updatedAt降序返回；

```python
//查询store时，指定 ns 来查询
store.search(memory_ns)
```

##### 语义检索

前面的 `store.search()` 做的是精确/排序检索——按 `namespace` + `limit` 返回最新 N 条。但长期记忆真正有价值的地方在于**语义检索**：用户问"之前聊过的那个 Python 性能优化方案是什么"，不需要记精确的 memoryId，靠语义相似度就能命中。

LangGraph Store 支持为存储条目配置 `index` 和 `embed` 参数，写入时自动向量化指定字段，检索时用自然语言做语义搜索：

```python
// 1. 初始化 Store 时配置 embedding 后端
//    以 PostgresStore + OpenAI embeddings 为例
store = InMemoryStore(
    index={
        "embed": init_embeddings("openai:text-embedding-3-small"),  # Embedding provider
        "dims": 1536,                              # Embedding dimensions
        "fields": ["food_preference", "$"]              # Fields to embed
    }
)

// 2. 写入时，指定 index 和 embed 参数，标记需要语义索引的字段
store.put(
  ["alice", "memories"],
  "mem-001",
  {
    content: "Python 性能优化最佳方案是用 PyPy 替代 CPython，JIT 编译让纯 Python 代码提速 4-7 倍",
    summary: "PyPy 替代 CPython 做性能优化",
    type: "semantic",    // 记忆类型标记
    timestamp: Date.now(),
  },
);

// 3. 语义检索：用自然语言查询，Store 自动做 embedding + 向量相似度搜索
items = store.search(
  ["alice", "memories"],
  {
    query: "Python 代码太慢怎么优化",  // 自然语言，自动向量化后做语义匹配
    limit: 5,
  },
);
```

如上是Langgraph提供的用于写入和检索长期记忆的基本能力，长期记忆其实不光是写入和基本的语义检索这么简单，更复杂的是如何有效的进行写入的长期记忆的冲突和更新，市面上有不少其他非常优秀的长期记忆框架，比如honcho，mem0等，不用Langgraph的Store其实也是可以的。

#### Time Travel

再来看看非常有用的Time Travel能力；
Time Travel 是 LangGraph 基于 Checkpoint 机制提供的**分支回溯能力**——让你可以从任意历史 Checkpoint 恢复执行，走一条与原路径不同的新分支。它由两个核心操作组成：`retry`（原路重来）和 `fork`（分叉探索）。

> **前提条件**：Time Travel 依赖 Checkpoint，必须在 compile graph 时配置 `checkpointer`，且每次 invoke 时传入 `thread_id`。

##### retry — 从某个 Checkpoint 重跑
当某个节点执行失败，或者你想指定从某个节点重新跑一次逻辑时，不需要从头跑整个 graph，只要指定目标 Checkpoint ID 即可从该点恢复。

失败恢复，不需要指定输入的数据，workflow会根据thread_id找到失败的执行记录重启执行；
```python
config = {"thread_id":"1"}
result = workflow.invoke(NONE, config = config)
```


指定从某个节点重新执行，比如重新执行名叫"node_b"的节点，则要找到 next = node_b 的checkpoint开始重新执行
```python
cp = next(s for s in history if s.next == ("node_b",))
graph.invoke(None, cp.config)  # 从 node_b 开始重放，node_a 跳过
```



![timetravel-replay](https://mintcdn.com/langchain-5e9cc07a/dL5Sn6Cmy9pwtY0V/oss/images/re_play.png?w=1100&fit=max&auto=format&n=dL5Sn6Cmy9pwtY0V&q=85&s=b322f66ef96d6734dcac38213104f080)
典型的应用场景：

- **异常恢复**：某个节点因为 LLM 调用超时、外部 API 抖动等原因失败，修好环境后从失败前的最后一个 Checkpoint 重试，而不是重跑整条链路。

通过`workflow.get_state_history(xx)`获得graph执行的完整步骤回放
```python
history = list(workflow.get_state_history(configs))

for state in history:
    print(f"next={state.next}, checkpoint_id={state.config['configurable']['checkpoint_id']}")
```


运行结果，history返回的item是按照graph执行顺序的倒序排列的，即后执行的排在最前面:
```shell
next=(), checkpoint_id=1f180ec8-8ad1-6e1e-8004-d0c04cc3d2e6
next=('translate',), checkpoint_id=1f180ec6-d657-6d0e-8003-f4eabb7ef1de
next=('humanReview',), checkpoint_id=1f180ec6-d643-6e1c-8002-e6b0464158c4
next=('review',), checkpoint_id=1f180ec6-d640-6096-8001-2e2efe615e8f
next=('genJoke',), checkpoint_id=1f180ec6-79c5-6e56-8000-5a6f24076e84
next=('__start__',), checkpoint_id=1f180ec6-79c4-61c8-bfff-653106308fbe
```


`get_state_history` 返回的 `StateSnapshot`包含如下属性，可以把这些属性作为过滤条件，查找到对应的checkpoint；

```python
class StateSnapshot(NamedTuple):
    """Snapshot of the state of the graph at the beginning of a step."""

    values: dict[str, Any] | Any
    """Current values of channels."""
    next: tuple[str, ...]
    """The name of the node to execute in each task for this step."""
    config: RunnableConfig
    """Config used to fetch this snapshot."""
    metadata: CheckpointMetadata | None
    """Metadata associated with this snapshot."""
    created_at: str | None
    """Timestamp of snapshot creation."""
    parent_config: RunnableConfig | None
    """Config used to fetch the parent snapshot, if any."""
    tasks: tuple[PregelTask, ...]
    """Tasks to execute in this step. If already attempted, may contain an error."""
    interrupts: tuple[Interrupt, ...]
    """Interrupts that occurred in this step that are pending resolution."""
```

比如对translate翻译结果不满意，找到translate节点，重新执行translate，
```python
## 这个是retry
last_translate_index = next((i for i, s in enumerate(history) if 'translate' in s.next),None)
if last_translate_index:
    snapshot = history[last_translate_index-1]
    workflow.invoke(None,snapshot.config,version="v2")
```

重新执行translate，并不会改变已有checkpoint的信息，但是会多出一条checkpoint的记录
```shell
next=(), checkpoint_id=1f180ed6-6dbe-6990-8005-ecc8796fb7aa #新增的translate记录
next=(), checkpoint_id=1f180ed6-6db7-6a78-8004-e510598edb0a
next=('translate',), checkpoint_id=1f180ed5-efe8-6e30-8003-711e4cec15cd
next=('humanReview',), checkpoint_id=1f180ed5-efd2-6b8a-8002-308fc3e4a101
next=('review',), checkpoint_id=1f180ed5-efce-6cba-8001-a8a4589a0774
next=('genJoke',), checkpoint_id=1f180ed5-b71c-67f0-8000-f2e97e0357dd
next=('__start__',), checkpoint_id=1f180ed5-b71a-6662-bfff-814ddd39da72
```

##### fork — 从同一个 Checkpoint 分叉出两条路

fork 是 retry 的升级版：从同一个 Checkpoint 出发，创建一个独立的执行分支。两条分支共享同一个"根"状态，但后续的节点选择、工具调用、人工决策可以完全不同。

![timetravel-fork](https://mintcdn.com/langchain-5e9cc07a/-_xGPoyjhyiDWTPJ/oss/images/checkpoints_full_story.jpg?w=1100&fit=max&auto=format&n=-_xGPoyjhyiDWTPJ&q=85&s=29776799d5a22c3aec7d4a45f675ba14)

这是 LangGraph 相比通用工作流引擎最独特的能力。典型场景：
- **A/B 对比**：同一个对话上下文中，分别走不同的工具链或 Prompt 变体，对比两条分支的最终结果，选择更优的路径。
- **回滚 + 修正**：发现某个决策导致了错误结果，从决策前的 Checkpoint fork 一条新分支重新决策，同时保留旧分支用于复盘。


先将history倒序，而后找到第一个  `humanReview`节点索引
```python
reversed_history = list(reversed(history)) # 现在正序：最早 -> 最新
snapshotIndex = next((i for i, s in enumerate(reversed_history) if 'humanReview' in s.next),None)
```

使用 `workflow.update_state` 更新该checkpoint的agentstate数据，然后重新执行，

```python
snapshotIndex = next((i for i, s in enumerate(reversed_history) if 'humanReview' in s.next),None)

if snapshotIndex:
    snapshot = reversed_history[snapshotIndex]
    fork_config = workflow.update_state(snapshot.config, {"humanReviewResult":CriticResult(isFunny=True,opinion="BIG funny"),"retryCount":1})
    workflow.invoke(None,snapshot.config)
```

在我们genJoke的例子里，如上的重置直接更新了human review之前的review节点的state值为(True)，后续就直接跳到translate节点；

```shell
next=(), checkpoint_id=1f180f3c-aabc-628c-8005-4bddb13b6fa0 #Fork
next=('translate',), checkpoint_id=1f180f3c-6d4e-626a-8004-b77ea0aceafb #Fork
next=('humanReview',), checkpoint_id=1f180f3c-6d4a-612e-8003-9e4b820b7df7 #Fork
next=(), checkpoint_id=1f180f3c-6d44-6670-8008-6c5da2847498
next=(), checkpoint_id=1f180f3c-6d3b-6692-8007-96116b4cb33c
next=('translate',), checkpoint_id=1f180f3c-3368-6230-8006-f87d156bf960
next=('humanReview',), checkpoint_id=1f180f3c-335c-66ec-8005-352601035a29
next=('review',), checkpoint_id=1f180f3c-3358-6df8-8004-1fb6845f80bd
next=('genJoke',), checkpoint_id=1f180f3b-fa28-6a60-8003-64e118d6b65f
next=('humanReview',), checkpoint_id=1f180f3b-fa14-60b0-8002-50f77d608b1f
next=('review',), checkpoint_id=1f180f3b-fa0f-6aba-8001-b900205b5886
next=('genJoke',), checkpoint_id=1f180f3b-79c3-6870-8000-c9508adb6609
next=('__start__',), checkpoint_id=1f180f3b-79c1-65a2-bfff-e49ef44bde21
```



## 核心概念总结

回顾一下，LangGraph 的核心概念可以拆成三个层次，逐层叠加：

**第一层：图的基础骨架（StateGraph + Node + Edge）**

这是最小可用单元。你只需要定义 State（业务数据上下文）、Node（处理逻辑）、Edge（流转规则），就能跑起一个确定性的工作流。这一层解决的是"把业务流程从代码的线性调用链里拎出来，变成显式的图拓扑"。

**第二层：Workflow 模式（组合范式）**

Prompt chain、Router、Orchestrator-Worker、Generator-Evaluator、嵌套子图、Agent Loop，这六种 Workflow 模式不是 LangGraph 的"功能"，而是用第一层的基础骨架搭出来的**组合范式**。你不需要学新 API，换一种 Edge 的连接方式，就能从顺序执行切换到条件分叉或并行汇聚。Agent 本质上就是一个带了 LLM 做路由决策的循环图，并不需要特殊语法。

**第三层：高级控制（Checkpoint / HITL / Store / Time Travel）**

这一层解决的是**生产级 Agent 的核心需求**：

| 需求  | 对应能力                      | 解决的问题                           |
| --- | ------------------------- | ------------------------------- |
| 可恢复 | Checkpoint                | 节点失败后从断点恢复，不重跑整条链路              |
| 可干预 | HITL（Interrupt + Command） | 关键决策点插入人工审批，审批通过/驳回后继续执行        |
| 可回溯 | Time Travel（retry + fork） | 回到历史状态重试或分叉对比，调试和迭代不丢历史         |
| 可记忆 | Store（语义检索）               | 跨 session 记住用户偏好和历史，Agent 越用越聪明 |
下一节，咱们看看 Stream，流式输出；




userId , retry的场景不包含重新嵌入prompt，AB测试