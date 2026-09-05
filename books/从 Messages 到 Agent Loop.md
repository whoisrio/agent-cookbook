# 从三家 API 到 Agent Loop：字段在分家，逻辑在收敛

上一篇聊 Responses API，结尾我留了个观察：各家开源 Provider 未来很长一段时间，大概还是会把 `/v1/chat/completions` 当主要接口提供服务。

这篇接着往下走一步。既然连开源 Provider 都在往同一套接口上靠，那 OpenAI、Anthropic、Google 这三家闭源的，摊开摆一起看，到底还差多少？

我的结论是：**字段差别不小，但逻辑已经收敛完了，而且收敛的方向就一个——工具调用。** 这四套 API 的骨架是同一套东西的四种拼写。真正的分野不在 API 层，在你自己写的那个循环里。

所以这篇的结构是：先把四套字段差异扫一遍（快，半小时你自己也能翻完），然后讲两处各家力度明显不一样的能力（服务端工具、缓存），最后把剩下的篇幅全给 agent loop——因为那才是你真正要自己写、也真正会出事的部分。

---

## 一、四套拼写，一套逻辑

先摆事实。下面这张表不用背，看一眼就会发现规律。

第一列是 Chat Completions。它放在这儿不是因为它更好，是因为它是事实标准——国内几乎所有 Provider 都兼容它，你后面接任何一家，大概率还是照这一列写。

| | Chat Completions | OpenAI（Responses） | Anthropic（Messages） | Google（Gemini） |
|---|---|---|---|---|
| 系统提示 | `messages` 里一条 `role: "system"` | `instructions` | `system`（字符串或数组） | `systemInstruction` |
| 对话内容 | `messages` | `input` | `messages` | `contents` |
| 角色名 | `system` / `user` / `assistant` / `tool` | `user` / `assistant` / `developer` | `user` / `assistant` | `user` / `model` |
| 声明工具 | `tools[].function` | `tools` | `tools` | `tools[].functionDeclarations` |
| 工具选择 | `tool_choice` | `tool_choice` | `tool_choice` | `toolConfig.functionCallingConfig.mode` |
| 模型要调工具 | `message.tool_calls` | `output[].type == "function_call"` | `content[].type == "tool_use"` | `parts[].functionCall` |
| 你回传结果 | `role: "tool"` + `tool_call_id` | `function_call_output` | `content[].type == "tool_result"` | `parts[].functionResponse` |
| 停下来 | `finish_reason` | `status` + `incomplete_reason` | `stop_reason` | `finishReason` |
| 用量 | `usage.prompt_tokens` | `usage.input_tokens` | `usage.input_tokens` | `usageMetadata.promptTokenCount` |

字段名几乎没有一个对得上的。但你看第二段的往返流程：

```python
# 四套都是同一个四步循环，区别只在字段怎么拼
messages = [{"role": "user", "content": "北京今天天气怎么样"}]

while True:
    resp = call_model(messages, tools=TOOLS)   # 1. 带上工具清单问一次
    if not resp.wants_tool():                  # 2. 它不要调工具，说完事了
        break
    results = [run(t) for t in resp.tool_calls()]  # 3. 你来执行
    messages += resp.as_message() + results    # 4. 原样 + 结果，塞回去再来一轮
```

发三件套（系统提示 + 对话 + 工具清单）→ 模型说要调工具 → 你执行 → 把结果塞回去 → 再来一轮。这四套，以及国内所有兼容它的 Provider，跑的都是这同一个循环。

所以字段差异是"你怎么拼这个请求"的差异，不是"这个循环长什么样"的差异。也正因为这样，LangChain、Vercel AI SDK 这类抽象层才能活——它们干的活就是把上表后三列翻译成第一列。

字段之外还剩两处各家力度不一样的地方：服务端工具，和缓存。挨个说。

---

## 二、两处各家力度不一样的地方

### 服务端工具：你不用写实现，但也管不着

服务端工具指的是那些"你只声明名字、服务端自己跑完"的工具：

- **Anthropic**：`web_search`、`web_fetch`、`code_execution`、`computer use`、`browser use`，外加 MCP connector
- **OpenAI**：`web_search`、`code_interpreter`、`file_search`、`computer use`，以及 2026 年 2 月发的 Hosted Shell Containers（Debian 12 + Python 3.11 / Node 22 / Java 17 / Go 1.23，`/mnt/data` 持久存储）
- **Google**：`googleSearch`、`codeExecution`、`urlContext`、`computerUse`

三家都在做同一件事——把执行权从你手里拿走。但你被拿走的东西不一样：OpenAI 和 Anthropic 至少还在响应里给你一个明确信号（Anthropic 是 `pause_turn`，意思是服务端的采样循环跑满默认 10 次还没干完，你把响应原样发回去它接着跑）；Google 的 automatic function calling 是 SDK 层面直接把整个循环吃掉，你看到的只有最终结果。

国内 Provider 这边覆盖是零散的，多数只支持 web search 这一类，而且是各家自己实现的版本。所以**服务端工具是目前可移植性最差的一层**——用了基本等于把这段逻辑锁死在一家。

### 缓存：三种玩法，账也不是一种算法

目标都一样，前缀命中就打折。怎么配、怎么算钱，是三个模型。

**OpenAI 是零配置。** 不需要任何代码改动，够 1024 token 就自动命中，命中量在 `usage.prompt_tokens_details.cached_tokens` 里看。GPT-5.6 之后这块加了三个东西：`prompt_cache_key` 保证共享前缀的请求路由到同一台机器，官方说法是这个参数必须设命中才可靠；`prompt_cache_options.mode: "explicit"` 让你自己放断点、只认你放的；`cache_write_tokens` 把写入量单独报出来，写入按 1.25 倍计费。

**Anthropic 以前是最麻烦的**，要手动往内容块上标 `cache_control`，最多 4 个断点。这个印象今年过期了。2026 年 2 月 19 日 automatic caching GA，请求体顶层加一个字段就行：

```python
response = client.messages.create(
    model="claude-opus-5",
    max_tokens=1024,
    cache_control={"type": "ephemeral"},   # 就这一个
    system=SYSTEM,
    messages=messages,
)
```

官方原话是系统会自动把断点放在最后一个可缓存块上，随对话变长自动往前推，**No manual breakpoint management required**。

**Google 是第三种：显式缓存资源。** 你得先建一个缓存对象，拿着它的名字去发请求：

```python
cache = client.caches.create(
    model="gemini-3-pro",
    config=types.CreateCachedContentConfig(
        contents=[long_document], system_instruction=SYSTEM, ttl="300s"
    ),
)
resp = client.models.generate_content(
    model="gemini-3-pro",
    contents="这份材料里提到了哪些日期",
    config=types.GenerateContentConfig(cached_content=cache.name),
)
```

前两家的缓存是请求的副产品，你管不着也不用管；Google 的缓存是一个你创建、你管 TTL、你按存储时长付钱的对象。好处是命中范围和时长你说了算，代价是多一层生命周期要维护。

国内这边都有，但力度参差：DeepSeek 是全自动的，官方定价页写明缓存自动生效、不用改代码，命中按折扣价算；阿里云百炼给了两套，显式缓存要你自己打标记，隐式缓存自动命中但你控制不了命中什么；再往下数，Kimi、GLM、豆包、MiniMax 各家一套，最小触发长度、折扣比例、有效期全不一样。

所以缓存这一层的真实状况是：**能力到处都有，语义没有一处相同。** 你照着一家调好的参数，换一家大概率不生效，而且不报错——你只会看到账单没降。

---

## 三、Anthropic 这一年，一组特别干净的对照实验

聊完差异，说个更值得看的东西。

Anthropic 在 2026 年给了一组特别干净的对照实验。同一个公司，同一个"往服务端收"的方向，两件事：一件事收成了，一件事没收成。

收成的是 prompt caching。2 月 19 日 automatic caching 正式 GA，4 月 cache diagnostics 进 beta，7 月又发了 mid-conversation tool changes（可以用 `tool_addition` / `tool_removal` 块中途增删工具，`tools` 数组一个字节不动，缓存照样命中）——一年之内连着三刀，每一刀都是把"保住缓存前缀"这件事，从开发者的自觉纪律变成 API 提供的机制。

没收成的是服务端压缩。那个 `compact_20260112` 能力躺在 API 里，Anthropic 自家的 Claude Code 不用它，开源生态一家都没实现。

同一家公司，同一个方向，一个成一个不成。这个差别比任何单个功能都值得看，因为它给出的分界线特别清楚：**机制能收，策略收不了。**

保住缓存前缀是个机制问题。什么该缓存、缓存在哪、有没有命中，跟你的业务无关，是纯粹的工程细节。所以它可以被收走，而且收走是好事——现在还有 `cache_miss_reason` 告诉你第一次分叉出在 model、system、tools 还是 messages 哪一层。

压缩是个策略问题。压什么、什么时候压、压完留什么、哪些东西碰不得，只有你的应用知道。服务端把它做成一个字段卖给你，等于塞给你一个改不了的默认值。

这条分界线可以直接拿来用：**判断任何"厂商又替你做了一件事"的消息，问一句——这件事里有没有只有你知道的信息。** 有，它收不走；没有，迟早会被收走，而且收走是好事。

而收不走的那一堆，就是下面这个东西。

---

## 四、Agent Loop：真正要你自己写的部分

前面几节扫完 API，会发现一件挺讽刺的事：各家 API 收敛得越干净，剩下的复杂度越集中在一个地方——你自己写的那个 while 循环。

而且这个循环有个特点：**最小版本二十行就能跑通，跑通之后你会连续加两个月的东西。**

先看骨架：

```python
def agent_loop(user_input, tools, max_turns=50):
    messages = [{"role": "user", "content": user_input}]
    for turn in range(max_turns):
        resp = call_model(messages, tools)
        messages.append(resp.as_message())        # 整个 content，不是只挑文本
        if not resp.wants_tool():
            return resp.text
        messages.append(execute_all(resp.tool_calls(), tools))
    raise RuntimeError("超出迭代上限")
```

这段能跑，能调工具，能拿到答案。然后你把真实任务丢进去，会按顺序撞上下面七件事。前四层每轮都跑，后三层不每轮触发，但决定一个长任务能不能活着跑完。

### 1. 组装层：这一轮到底发什么

系统提示要拆成静态段和动态段。静态的放前面吃缓存，动态的（当前时间、request id、用户信息）放后面——这不是优化建议，是必需的：往 system 里插一个时间戳，缓存命中率直接归零，而且你没有任何提示。Anthropic 的 `cache_miss_reason` 里 `system_changed` 是最常见的那一种，原因基本都是这个。

工具集预先声明全，运行期靠 `tool_addition` / `tool_removal` 增删，别动 `tools` 数组本身。

消息就是追加。这里有个容易写错的点：`resp.as_message()` 必须是**整个 `response.content`**，思考块、工具调用块、压缩块全在里面。开了 thinking 的模型，思考块上挂着一个加密签名，装着模型完整的推理过程，改了、删了、重排了直接 400。所以那种"只把 `text` 挑出来存"的省 token 写法是错的——推理状态丢了，而且有时候不报错，只是模型悄悄变笨。

### 2. 决策层：看停止原因决定下一步

这是最容易写错的一层，因为大部分人只处理两种情况：要调工具、说完了。

Anthropic 的 `stop_reason` 官方处置指南列了七种。接国内 Provider 的话你多半用的是 Chat Completions，所以我把三列的等价物放一起：

| 情况 | Chat Completions | Anthropic | OpenAI（Responses） | 该干什么 |
|---|---|---|---|---|
| 正常说完 | `stop` | `end_turn` | `status: completed` | 收工 |
| 要调你的工具 | `tool_calls` | `tool_use` | `output` 里有 `function_call` | 执行，回传结果 |
| 输出被截断 | `length` | `max_tokens` | `incomplete_reason: max_output_tokens` | 内容是真的，可能断在 JSON 中间，接着要或者标记一下 |
| 撞上下文窗口 | 报错 | `model_context_window_exceeded` | 报错 | 压缩后重试 |
| 服务端工具没跑完 | — | `pause_turn` | — | 整个响应原样发回去，不要加 user 消息 |
| 安全拒绝 | `content_filter` | `refusal` | `incomplete_reason: content_filter` | 换模型重试或降级 |
| 命中自定义停止串 | `stop` | `stop_sequence` | — | 收工 |

（Chat Completions 还有个 `function_call`，是废弃值；`stop` 同时覆盖自然结束和命中停止串两种情况。）

两个最容易漏的值。`pause_turn` 是服务端工具在服务端跑采样循环（默认 10 次迭代）跑不完时返回的，处置办法跟 `tool_use` 完全不一样——`tool_use` 你要发 `tool_result`，`pause_turn` 你要把整个响应原样发回去，搞混了会撞上 `tool_use ids were found without tool_result blocks`。

`refusal` 有两个坑。第一它是 **HTTP 200**，不是错误，只看状态码的监控对它完全瞎。第二它的 `content` 数组是空的，所以代码顺序必须是先看停止原因再读内容，反过来的代码在拒答时会拿到空内容然后不知道发生了什么。

`model_context_window_exceeded` 是个新值，它的好处是你可以放心把 `max_tokens` 拉满，不用自己精算输入长度还能剩多少。

### 3. 执行层：工具在哪跑，结果能有多大

先说在哪跑。现在有五个选择：本地进程、沙箱容器、服务端工具、MCP server、子 agent。选择标准不是性能，是**权限边界划在哪**。本地进程快但你挡不住 `rm -rf`，沙箱慢但炸了不心疼，子 agent 能把一大段脏活隔离在独立上下文里。

再说结果能多大。Anthropic 官方工程博客里有句很直接的话：Claude Code 把工具返回限制在 25,000 tokens。这是 harness 层的决定，不是 API 强制，所以这一层你得自己写。官方给的办法是 pagination、range、filtering、truncation 的组合，并且要设合理默认值。25,000 这个数在 Claude Code 里对应环境变量 `MAX_MCP_OUTPUT_TOKENS`，默认就是 25,000，MCP 工具返回超了也按这个切。

截断不是把尾巴切掉就完事，得让模型知道它被截了。这里有个真实的坑值得记：Claude Code 最早把 `[PARTIAL]` 放在输出**末尾**，结果模型把截断前缀当成完整文件读了，后面没读到的规则静默消失，连个信号都没有。所以标记要放在结构上独立的位置——开头一个 banner，或者单独一个字段。放尾巴的标记，模型会当注释跳过。

错误处理同理。工具不要往上抛异常，把异常转成文本塞回工具结果，让模型自己决定怎么绕。官方的原话是让错误信息明确指出具体、可执行的改进方向，而不是甩一个错误码或堆栈。异常往上抛，循环就死了。

### 4. 消息层：追加、插入、清理

追加是常态，前面说了必须整个追加。

插入这边的用途是中途改规则——比如从第三轮开始要求所有 SQL 必须参数化。Anthropic 支持在 messages 中间插 system message（部分模型可用，不需要 beta header）。它的价值不是"能插"，是插了不破坏缓存：改顶层 `system` 字段会让整个前缀 hash 变掉，插在尾部不会。

清理这块有一条硬约束，各家都一样：**工具调用和工具结果是配对的，只能换内容，不能删结构。**

```python
PLACEHOLDER = "[旧工具结果已清除]"

def prune_tool_results(messages, keep_recent=3):
    """只换 body，不动结构：tool_result 块必须留在原位。"""
    cutoff = len(messages) - keep_recent * 2
    for i, msg in enumerate(messages):
        if i >= cutoff or msg.get("role") != "user":
            continue
        if not isinstance(msg.get("content"), list):
            continue
        msg["content"] = [
            {**b, "content": PLACEHOLDER}
            if isinstance(b, dict) and b.get("type") == "tool_result"
            else b
            for b in msg["content"]
        ]
    return messages
```

因为服务端会校验配对。你把结构删了，模型看不到"我调过这个工具"，请求直接被拒。只清内容留骨架，调用历史还在，上下文却瘦下来了。

同样碰不得的还有两样：带签名的思考块，和压缩块。这三样构成了"清理函数能碰什么"的边界。

### 5. 收敛层：什么时候该认输

迭代上限、token 预算、墙钟超时、连续失败检测。这层平时最没存在感，直到一个循环在半夜烧掉你两百块。

连续失败检测值得单独说。同一个工具连续失败三次，基本说明模型在这个任务上卡死了，继续循环只是重复烧钱。Claude Code 的做法是压缩连续失败 3 次就断电，这个 session 不再自动压。

### 6. 压缩层：触发时机、范围、用什么模型压

关于服务端压缩，我的判断是别在生产上依赖它。理由不是它没用，是它把三样东西藏起来了：

- 摘要用什么模型不可选，只能用你请求里指定的主模型（Opus 的历史按 Opus 的价格压一遍）
- 定义了 tools 的时候压缩可能**静默失败**——返回了压缩块但 `content` 是 `null`，不报错。官方给的规避办法是在 `instructions` 里写一句"写摘要时不要调用任何工具"
- 计费分叉，压缩多出来的采样记在 `usage.iterations` 里一条 `type: "compaction"`，顶层 `input_tokens` 不含它。用老逻辑做成本统计，账会差一个数量级

自己压的话，第一件事是选模型。这里有个硬天花板容易被忽略：**小模型的窗口装不下大上下文。** Haiku 4.5 的窗口是 200K，Opus 5 / Sonnet 5 / Fable 5 是 1M。一个跑到 800K 的会话，你让 Haiku 去摘要，物理上塞不进去。

所以旁路小模型要分域看。上下文还没超过小模型窗口时，它是纯赚——摘要请求要读全量历史，本来就几乎吃不到缓存，交给便宜模型省到底。超了之后只能趁早压加分段递归。

Claude Code 官方提供的 `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE`（取 1-100，默认约 95%）就是干这个的，官方文档自己的建议是"用 50 这类更低的值提前压"。调低的理由不是省 token，是让上下文永远停在压得动的尺寸。

还有个坑：**旁路必须是独立的 session 或独立的 provider，不能是在主会话里切模型。** 主会话切模型会把累积的缓存前缀烧掉，新模型得把整个历史重读一遍，省下的那点摘要钱不够赔。

### 7. 持久化层：为的是能回头

平时最没存在感，直到你需要"回到第五步重来一次"，或者"这个 session 为什么烧了两百块"。

客户端压缩相对服务端压缩最大的优势也就在这：本地 transcript 完整，能回放、能审计、能 rewind。服务端压缩把你的历史换成一块你必须原样回传、却不完全属于自己的东西。

---

## 五、代价与局限

先说这一整套的代价。

**用得越深，越难换。** `cache_control`、`context_management`、`tool_addition`、`pause_turn`、`cache_miss_reason`，全是 Anthropic 独有的语义；`cached_content` 是 Google 独有的；Hosted Shell Containers 是 OpenAI 独有的。你把哪一家的特性用得越顺手，迁移成本越高。这也是现在很多 agent 框架选择只吃各家 API 最大公约数的原因——不是不想用好特性，是用不起。

**收敛不等于统一。** 逻辑趋同只意味着你的循环骨架可以复用，不意味着参数能照抄。缓存的最小长度、工具结果的上限、压缩的触发点、计费的口径，每一处换 provider 都要重新量一遍。

**这七层没有标准答案。** 我给的数字（25,000 tokens、3 次失败、50% 提前压）都是从具体产品里扒出来的经验值，不是理论最优。你的任务不一样，最优解也不一样。这些数字的价值是给你一个起点，不是一个标准。

最后说回选模型这件事。这四套 API 收敛成同一套逻辑之后，选哪家对 agent 效果的影响在变小，你自己那个循环怎么写，影响在变大。

有个对照能说明这个变化：第二节讲的那些各家力度不一样的能力——服务端工具、缓存——恰恰是这两年竞争最激烈、各家都在猛发的地方。而第四节讲的七层，没有一家厂商在替你做，也没有一家打算替你做。
