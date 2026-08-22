# LangGraph 简介：为什么用、核心概念与高级控制

LangGraph 是 LangChain 生态中的有状态工作流编排框架，用有向图（Node + Edge + State）来建模 Agent 的执行逻辑。它解决的核心问题是：多步推理、工具调用、人工审批交织的复杂流程时，如何可靠地管理和追踪执行路径。

>与 LangChain 的 Chain 机制不同——Chain 适合简单的线性调用，但在多分支、有状态的 Agent 中封装过厚、不易调试。LangGraph 只封装执行拓扑，节点内部怎么写它不管，裸调 API 或使用 LangChain 组件都可以。也LangChain 自家的 DeepAgent，也是基于 LangGraph 构建的。

## 与通用工作流引擎的差异

使用通用工作流(Temporal/Prefect等)引擎也能实现 Agent 编排所需的 Checkpoint、HITL、流式事件等能力。LangGraph 的独特之处在于三点：

- **Time Travel（分支复现）**——从任意历史 Checkpoint 恢复后走不同路径（fork），对比两条分支的执行结果。如果你的调试和迭代依赖"在同一个历史点上试不同的工具选择或 Prompt 变体"，这是很重要的能力。

- **LangSmith Trace 自动映射**——使用 LangSmith 做链路可视化时，LangGraph 的调用链会自动映射，无需额外写映射代码（仍需设置 `LANGSMITH_TRACING` 与 API key 开启）。

- **LangChain 生态的零摩擦集成**——如果技术栈已包含 LangChain 的 Tool、Prompt、Model 抽象，LangGraph 是同生态内的编排层，不需要学习第二套工具链。

如果你的场景正好踩中上述需求，LangGraph 可能是目前最高效的起点。本系列会依次覆盖：核心概念（State / Node / Edge）、Checkpoint 与 Time Travel、人机协作（HITL）模式，LangGraph的底层Pregel引擎逻辑，部署以及实战案例。本文先从最基础的状态图开始，逐步构建一个带人工审批、可回溯的 Agent 工作流。

# 核心概念

## 了解 LangGraph

下面以LangGraph提供的GraphAPI为例，看看LangGraph提供了哪些能力。

>LangGraph提供了python和ts版本，本系列的示例代码都以python版本为主
### 基础能力

- **GraphState**，整个workflow共享的上下文数据对象，每一个处理单元的处理逻辑都是针对这个数据对象；
```python
from typing import TypedDict

class GraphState(TypedDict):
    content: str
```

- **GraphNode**，workflow的流程节点，最基本的任务处理单元；
```python
from langchain_core.runnables import RunnableConfig

def node(state: GraphState, config: RunnableConfig) -> dict:
    # 从 state 中获取上下文数据
    content = state["content"]
    # config 可携带 workflow 运行时的自定义配置
    return {"content": content}
```

- **ConditionalEdgeRouter**，条件判断节点，根据上下文数据对象的状态来决定workflow进入的下一个流程节点(Node)；
```python
def route_joke(state: GraphState) -> str:
    if state["funnyOrNot"] == "funny":
        return "Accepted"
    else:
        return "Rejected + Feedback"
```

- **Edge**，流程节点的边，建立Node之间的联系，可以在workflow compile的时候显式的建立，也可以在GraphNode处理业务逻辑的时候动态指定；
```python
#创建graph时，显示添加
graph.add_edge(START,"node1")

#节点中动态路由下一跳
def next_node(state: GraphState): 
	...
	return Command( update={"classification": classification}, goto="human_review" )
```

- **StateGraph**，即根据如上介绍的这些信息拼装在一块组成的流程图，`compile`之后，就得到可运行的工作流。
基于如上基本对象，就能够构建任意场景的workflow；

```python
from langgraph.graph import StateGraph, START, END

builder = StateGraph(GraphState)
builder.add_node("node1", node1)
builder.add_node("node2", node2)
builder.add_edge(START, "node1")
builder.add_edge("node1", "node2")
builder.add_edge("node2", END)
chain = builder.compile()

result = chain.invoke({"content": "init"})
print(result)
```

> 如上代码示例基于LangGraph提供的GraphAPI，LangGraph还提供了Functional API，更易于集成到已有的业务流程


#### 基本工作流示例

如下就是一个最基本的Prompt Chain工作流示例，工作流只有两个节点：
- 生成Joke
- 翻译为中文

```mermaid
graph LR

__START__-->genJoke
genJoke-->translate
translate-->__END__
```

示例代码如下，定义工作流程共享的AgentState，包含topic用于输入Joke的主题，content用于承载LLM生成的Joke；

>在定义State的时候，属性默认都是LastValue类型的，表示这些属性值只接受最新的更新值

```python
class Joke(BaseModel):
    joke: str


class AgentState(TypedDict):
    topic: str
    content: str

def genJoke(state: AgentState):
    response = model.chat.completions.create(
            response_model=Joke,
            messages=[{"role":"user","content":f"gen a Joek about {state['topic']}"}])
    return {"content": response.joke}

def translate(state: AgentState):
    response = model.chat.completions.create(response_model=Joke,
                                             messages=[{"role":"user","content":f"translate to chinese: {state['content']}"}])

    return {"content": response.joke}

graph = StateGraph(AgentState).add_node("genJoke",genJoke)\
                              .add_node("translate",translate)\
                              .add_edge(START,"genJoke")\
                              .add_edge("genJoke","translate")\
                              .add_edge("translate",END)
```

运行效果如下:
```shell
 content: Why did the calendar go to therapy on Wednesday? Because it was having a mid-life crisis!

#### result: 
{'topic': 'wednesday', 'content': '为什么日历在星期三去看心理医生？因为它正在经历中年危机！'}
```



如上是一个最基本的prompt chain形式的工作流，咱们添加上Joke质量审核的流程节点，在如上工作流的基础上，我们加上了如下内容:
 - review的节点，来判断工作流是否funny，来决定joke是否进入translate阶段，还是需要重新生成；State同时增加了review结论的属性；
 - 添加了判断review结果的条件边，用于决定工作流的走向；
 - 为了避免工作流死循环，加上了最大的尝试的次数；

```python

class CriticResult(BaseModel):
    isFunny: bool = Field(description="is joke funny or not")
    opinion: str = Field(description="how to improve")


class AgentState(TypedDict):
    topic: str
    content: str
    reviewResult: Optional[CriticResult]
    retryCount: int


def reviewJoke(state: AgentState):
    response = model.chat.completions.create(
        response_model=CriticResult,
        messages=[
            {"role":"system","content":"You are a strict joke reviewer."},
            {"role":"user","content":f'is this Joke funny or not ? Joke: {state["content"]},\
             if not let me know how to improve it'}
        ]
    )
    print(f'## review: \n is funny: {response.isFunny}, opinion: {response.opinion}')
    return {"reviewResult": response, "retryCount": state["retryCount"] + 1}

def checkReviewResult(state: AgentState) -> Literal["genJoke", "translate"]:
    config = get_config()
    max_retries = config.get("configurable", {}).get("max_retries", 3)

    if state["reviewResult"] and state["reviewResult"].isFunny:
        print("✅ 评审通过")
        return "translate"

    if state["retryCount"] >= max_retries:
        print(f"⚠️ 达到最大重试次数 {max_retries}，强制继续")
        return "translate"

    print(f"❌ 评审未通过，重试第 {state['retryCount'] + 1}/{max_retries} 次")
    return "genJoke"

graph = StateGraph(AgentState).add_node("genJoke", genJoke)\
                              .add_node("review", reviewJoke)\
                              .add_node("translate", translate)\
                              .add_edge(START, "genJoke")\
                              .add_edge("genJoke", "review")\
                              .add_conditional_edges(
                                  "review", checkReviewResult,
                                  {"genJoke": "genJoke", "translate": "translate"})\
                              .add_edge("translate", END)
```

运行效果如下:
```shell
# gen 
 gen message: gen a Joke about wednesday
## review: 
 is funny: False, opinion: The joke relies on a mundane observation rather than a subversion of expectations or clever wordplay. To improve it, utilize a pun or a cultural association with Wednesday. For instance, lean into the 'Hump Day' trope: 'Why is Wednesday so good at keeping secrets? Because it is too busy getting over the hump to talk.' Alternatively, use phonetic wordplay: 'Because it is a Wed-nes-day, and you never kiss and tell after a wedding.'
❌ 评审未通过，重试第 2/2 次
# gen 
 gen message: gen a Joke about wednesday, consider the opinion: The joke relies on a mundane observation rather than a subversion of expectations or clever wordplay. To improve it, utilize a pun or a cultural association with Wednesday. For instance, lean into the 'Hump Day' trope: 'Why is Wednesday so good at keeping secrets? Because it is too busy getting over the hump to talk.' Alternatively, use phonetic wordplay: 'Because it is a Wed-nes-day, and you never kiss and tell after a wedding.'
## review: 
 is funny: False, opinion: The phonetic pun on 'Wed-nes-day' is incredibly forced and doesn't sound enough like 'wedding day' to elicit a genuine laugh. To improve it, rely on a stronger play on words using the prefix 'Wed' or reference Wednesday being the middle of the week. For example: 'Why did the couple get married on a Wednesday? Because they wanted to get the Wed out of the way before the weekend!'
⚠️ 达到最大重试次数 2，强制继续
#### result: 
{'topic': 'wednesday', 'content': '为什么新娘和新郎要在星期三（Wednesday）结婚？因为他们想直奔“Wed”（Wedding，婚礼）！', 'reviewResult': CriticResult(isFunny=False, opinion="The phonetic pun on 'Wed-nes-day' is incredibly forced and doesn't sound enough like 'wedding day' to elicit a genuine laugh. To improve it, rely on a stronger play on words using the prefix 'Wed' or reference Wednesday being the middle of the week. For example: 'Why did the couple get married on a Wednesday? Because they wanted to get the Wed out of the way before the weekend!'"), 'retryCount': 2}
```

到这里，你的工作流已经从prompt chain改造成了 generator and evaluator的工作流了，生成和评估，是当下harness的最重要的设计模式。


#### 稍微复杂一点的例子: 并行执行
以Orchestrator Worker工作流模式为例，在Orchestrator节点进行任务规划后，通过`assign_worker` 调用`SEND`来委派任务给worker执行，

```python
from typing import Annotated, List
import operator
from pydantic import BaseModel,Field
from langgraph.graph import START,StateGraph,END


# Schema for structured output to use in planning
class Section(BaseModel):
    name: str = Field(
        description="Name for this section of the report.",
    )
    description: str = Field(
        description="Brief overview of the main topics and concepts to be covered in this section.",
    )


class Sections(BaseModel):
    sections: List[Section] = Field(
        description="Sections of the report.",
    )

from langgraph.types import Send
from typing import TypedDict

# Graph state
class State(TypedDict):
    topic: str  # Report topic
    sections: list[Section]  # List of report sections
    completed_sections: Annotated[
        list, operator.add
    ]  # All workers write to this key in parallel
    final_report: str  # Final report


# Worker state
class WorkerState(TypedDict):
    section: Section
    completed_sections: Annotated[list, operator.add]


# Nodes
def orchestrator(state: State):
    """Orchestrator that generates a plan for the report"""

    # Generate queries
    task1 = Section(name="task1",description="task1")
    task2 = Section(name="task2",description="task2")
    report_sections = Sections(sections=[task1,task2])

    return {"sections": report_sections.sections}


def llm_call(state: WorkerState):
    """Worker writes a section of the report"""

    print(f'working ,task : {state["section"].name} , description:{state["section"].description}')

    # Write the updated section to completed sections
    return {"completed_sections": [f'task: {state["section"].name} finished']}


def synthesizer(state: State):
    """Synthesize full report from sections"""

    # List of completed sections
    completed_sections = state["completed_sections"]

    # Format completed section to str to use as context for final sections
    completed_report_sections = "\n\n---\n\n".join(completed_sections)

    return {"final_report": completed_report_sections}


# Conditional edge function to create llm_call workers that each write a section of the report
def assign_workers(state: State):
    """Assign a worker to each section in the plan"""

    # Kick off section writing in parallel via Send() API
    return [Send("llm_call", {"section": s}) for s in state["sections"]]


# Build workflow
orchestrator_worker_builder = StateGraph(State)

# Add the nodes
orchestrator_worker_builder.add_node("orchestrator", orchestrator)
orchestrator_worker_builder.add_node("llm_call", llm_call)
orchestrator_worker_builder.add_node("synthesizer", synthesizer)

# Add edges to connect nodes
orchestrator_worker_builder.add_edge(START, "orchestrator")
orchestrator_worker_builder.add_conditional_edges(
    "orchestrator", assign_workers, ["llm_call"]
)
orchestrator_worker_builder.add_edge("llm_call", "synthesizer")
orchestrator_worker_builder.add_edge("synthesizer", END)

# Compile the workflow
orchestrator_worker = orchestrator_worker_builder.compile()


# Invoke
result = orchestrator_worker.invoke({"topic": "Create a report on LLM scaling laws"}) # type: ignore
print(f'report : {result}')

```


LangGraph的SEND机制没有默认的并发控制，Orchestator规划了多少并行任务，SEND之后所有Worker会一起冲，python的环境下，按照executor的默认配置，`min(32, CPU cores + 4)`,所以，在这个工作流场景，必须要加上并发限制，在调用工作流时显式的指定，或者自行做并发控制，
```python
config={"max_concurrency": 10, "recursion_limit": 50},
```
####  可视化你的工作流
LangGraph的workflow API提供了默认的方法，可以方便的将你的工作流可视化为
- ASCII
- Mermaid
- 或者直接生成图片(依赖graphviz)

```python
workflow.get_graph().print_ascii()
```

```shell
+-----------+  
| __start__ |  
+-----------+  
      *        
      *        
      *        
 +---------+   
 | genJoke |   
 +---------+   
      .        
      .        
      .        
  +--------+   
  | review |   
  +--------+   
      .        
      .        
      .        
+-----------+  
| translate |  
+-----------+  
      *        
      *        
      *        
 +---------+   
 | __end__ |   
 +---------+   
```


```python
workflow.get_graph().draw_png('./promptchain.png')
```

![prompChain.png](../assets/promptchain.png)

