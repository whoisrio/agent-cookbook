# Python 异步编程: 把 I/O 等待期间的 CPU 用起来

先看一段最朴素的代码: 一个程序要抓三个网页, 每次请求耗时 2 秒。

```python
import time

def fetch_page(url: str) -> str:
    time.sleep(2)  # 模拟一次 2 秒的网络请求 (I/O 等待)
    return f"{url} 的内容"

t0 = time.perf_counter()
for url in ("page-A", "page-B", "page-C"):
    result = fetch_page(url)
    print(f"{time.perf_counter()-t0:4.1f}s  {result}")
```

实测输出 (Python 3.13.12):

```text
 2.0s  page-A 的内容
 4.0s  page-B 的内容
 6.0s  page-C 的内容
 6.0s  总耗时
```

三个请求串行排队, 总耗时 6 秒。

问题不在这 6 秒, 而在这 6 秒里 CPU 的状态: 绝大部分时间它在 `time.sleep` 处空转, 什么都没干。

I/O 的本质就是"发起之后等结果"——HTTP 请求、数据库查询、读写文件, 全是同一类事, 等待期间 CPU 都是空闲的。

把这段空闲利用起来, 就是 Python 异步编程要解决的全部问题。

一句 GIL 先放这: GIL 限制的是多线程不能并行执行 Python 字节码, 它不妨碍本篇的思路——后文协程版照样在 GIL 开着的情况下把 6 秒砍到 2 秒。

配套可跑样例: [`examples/python/async-basics.ipynb`](../../examples/python/async-basics.ipynb), 文中输出均为真实运行结果。

## 一、Python 异步的三个标准库

先统一口径。"三个包"的说法不够准: 标准库里是 **两个包加两个模块**——`asyncio` 和 `concurrent.futures` 是包, `threading` 是模块, 它的底层是内置 C 模块 `_thread`。

还有一个容易看漏的: `concurrent` 本身单独 import 进来什么都没有, 它只是个命名空间壳, 真正有用的是子包 `concurrent.futures`。

三者的分工:

**asyncio 是主角。** 它提供了协程调度的一整套机制: 事件循环 (Event Loop)、Task、Future、异步队列和锁。Python 的"异步编程"日常指的就是 asyncio 这一套。

**concurrent.futures 是配角。** 它提供线程池 (`ThreadPoolExecutor`)、进程池 (`ProcessPoolExecutor`) 和一套 `Future` 抽象。当协程的世界里混进了没有 async 版本的阻塞调用, 或者碰上 CPU 密集任务, 都靠它兜底。

**threading 是多线程原语。** `Thread`、`Lock`、`Event`、`Condition`、`Semaphore` 都在这。日常写代码一般不直接开 `Thread`, 而是走 `ThreadPoolExecutor`, 所以它更多是"被 indirect 使用"的基础层。

本篇主线走 asyncio, 第五、六节再回头讲另外两个。

## 二、什么是协程

最简洁的说法: **协程函数被调用时, 返回的就是一个协程对象。**

```python
async def fetch_page(url: str) -> str:
    await asyncio.sleep(2)
    return f"{url} 的内容"
```

`async def` 定义的函数叫协程函数。它和普通函数的关键区别在调用时刻:

```python
coro = fetch_page("page-A")
print(type(coro))   # <class 'coroutine'>
```

调用 `fetch_page("page-A")` **不会执行函数体**, 返回的是一个协程对象, 此刻函数体一行都没跑。

这就是 Python 里"通过异步方式调用的函数, 默认返回值是一个协程对象"的准确含义——更精确一点说, 是调用协程函数得到协程对象。

协程对象有两种被"执行"的方式。

第一种, 直接 await 它:

```python
result = await fetch_page("page-A")   # 跑完函数体, 拿到返回值
```

第二种, 把它交给 Task, 由事件循环统一调度, 多个协程并发执行:

```python
t0 = time.perf_counter()
for coro in asyncio.as_completed([fetch_page(u) for u in ("page-A", "page-B", "page-C")]):
    result = await coro   # 先等结果, 再记时间——顺序反了打印出来的时刻是错的
    print(f"{time.perf_counter()-t0:4.1f}s  {result}")
```

实测输出:

```text
 2.0s  page-A 的内容
 2.0s  page-B 的内容
 2.0s  page-C 的内容
```

单线程, 总耗时从 6 秒降到 2 秒。

注意用词: 这是**并发**不是**并行**——三个协程没有同时跑在三个 CPU 核上, 它们只是把三段 I/O 等待重叠了。协程的世界里没有并行, 这一点到第六节讲多进程时会有对照。

协程对象还有一个更深的事实: 它可以被手动驱动。`coro.send(None)` 推进一步, 函数结束抛 `StopIteration` 并携带返回值:

```python
async def sample() -> str:
    return "ok"

coro = sample()
try:
    coro.send(None)
except StopIteration as e:
    print(e.value)   # ok
```

这个 `send` 机制 (PEP 342 的 generator 协议) 就是事件循环能调度协程的语言基础——事件循环干的事, 本质上就是在合适的时机替你调 `send`。第三节展开。

## 三、Event Loop 与 await 机制

### Event Loop 是什么

Event Loop 就是一个死循环, 它的调度靠两个容器 (实测类型):

- `_ready`: `collections.deque`, 存"现在就能执行"的回调;
- `_scheduled`: `list`, 存定时回调, 用堆 (`heapq`) 保持按时间有序。

循环的每一圈大致是: 算出本次 `select` 该睡多久 → 调 `selector.select(timeout)` 睡下等事件 → 把就绪 I/O 和到期定时器的回调塞进 `_ready` → 执行 `_ready` 里的回调 → 下一圈。

`timeout` 不是固定值, 每一圈现算 (实现在 `base_events.py` 的 `_run_once`), 分三种情况:

- `_ready` 里还有活, 或循环正在停止: `timeout = 0`——不睡, select 退化成一次非阻塞轮询, 干完活再说;
- `_scheduled` 里有定时回调: `timeout =` 最近一个定时回调的剩余时间, 负数截成 0, 上限 24 小时 (`MAXIMUM_SELECT_TIMEOUT`, base_events.py L68);
- 两者都没有: `timeout = None`——`select(None)` 无限期阻塞, 纯睡, 等内核唤醒。睡死也不用怕: 其他线程通过 `call_soon_threadsafe` 调 `_write_to_self`, 往一个注册在 selector 里的自管道 (self-pipe) 写一个字节, 就能把循环踢醒。

"事件"从哪来? 答案在 `selectors` 包: 它封装了操作系统的 I/O 多路复用机制, macOS 上是 kqueue, Linux 上是 epoll。把成千上万个 socket 注册给内核, 线程在 `select()` 处睡下, 哪个 socket 数据到了, 内核唤醒线程并告诉它是谁。实测 macOS 上的默认事件循环:

```python
loop = asyncio.new_event_loop()
print(type(loop))            # <class 'asyncio.unix_events._UnixSelectorEventLoop'>
print(type(loop._selector))  # <class 'selectors.KqueueSelector'>
loop.close()
```

实现代码在 `asyncio/base_events.py` (循环骨架) 和 `asyncio/selector_events.py` (selector 接入)。

### await 时发生了什么

用最直白的话说: 执行到 `await` 时, 当前函数在这里停住, await 后面的剩余逻辑被挂起等待; 当等待的 I/O 结束, 协程被事件循环唤醒, 从停住的地方继续执行。

在 await 处, 当前执行流把控制权交回事件循环, 事件循环去执行其他协程——CPU 就这样被填满了。

这句话背后的机制, 值得拆到源码级。分两段看。

第一段, `Future.__await__`, 在 `asyncio/futures.py`:

```python
def __await__(self):
    if not self.done():
        self._asyncio_future_blocking = True
        yield self  # This tells Task to wait for completion.   # L286
    if not self.done():
        raise RuntimeError("await wasn't used with future")
    return self.result()
```

`await future` 展开后就是这个协程: 没就绪就 `yield self`——把 future 自己抛给外层, 函数体在这里停住。

"外层"是谁? 第二段, `Task` 的推进逻辑, 在 `asyncio/tasks.py` 的 `__step_run_and_handle_result`:

```python
if exc is None:
    result = coro.send(None)      # L304: 推进协程一步
else:
    result = coro.throw(exc)      # L306: 向协程注入异常 (取消走这里)
...
elif blocking:
    ...
    result.add_done_callback(self.__wakeup)   # L341: future 完成时叫醒我
    self._fut_waiter = result                 # L343: 记下我在等谁
```

两段接起来, 一次 `await future` 的完整生命周期:

1. Task 调 `coro.send(None)` (L304), 推进协程;
2. 协程跑到 `await future`, 触发 L286 的 `yield self`, 控制权回到 Task 手里, 手里拿着那个 future;
3. Task 给 future 登记回调 `__wakeup` (L341), 记下 `_fut_waiter` (L343), 本轮结束——协程挂起, CPU 让出去了;
4. 将来 I/O 就绪, 事件循环把 future 的 `set_result` 跑掉;
5. `__wakeup` 被塞进 `_ready`, 下一圈执行;
6. `__wakeup` 再次 `coro.send(None)`, 协程从挂起点恢复, 拿到 `future.result()`。

"await 后面的剩余逻辑"并没有被复制到哪里去——它还是协程函数体的一部分, Task 保管着整个协程对象, 唤醒后从挂起点原地继续。没有魔法。

## 四、中断与 Future / Task

### 既然能挂起, 就天然能中断

上一节的机制里藏着一个重要推论: 协程的每一次挂起都是一个"可中断点"。

向 Task 发出取消请求, 本质就是走 L306 那条路——把 `CancelledError` 注入到协程当前挂起的位置:

```python
async def long_tool():
    try:
        await asyncio.sleep(10)
    except asyncio.CancelledError:
        print("  long_tool 在 await 点收到取消, 清理后重新抛出")
        raise

async def demo_cancel():
    task = asyncio.create_task(long_tool())
    await asyncio.sleep(0.1)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        print("demo_cancel: task 确认已取消")

async def demo_timeout():
    try:
        await asyncio.wait_for(long_tool(), timeout=1)
    except TimeoutError:
        print("demo_timeout: 1 秒没等到结果, 超时")

await asyncio.gather(demo_cancel(), demo_timeout())
```

实测输出:

```text
  long_tool 在 await 点收到取消, 清理后重新抛出
demo_cancel: task 确认已取消
  long_tool 在 await 点收到取消, 清理后重新抛出
demo_timeout: 1 秒没等到结果, 超时
```

取消不是杀线程, 而是在下一个 `await` 点抛异常, 给了函数体清理资源的机会。`wait_for` 的超时本质也是取消: 到点没完成就取消任务并抛 `TimeoutError`。

### Future: 还没结果的占位符

讲 Task 之前先讲 Future, 因为 Task 是它的子类。

`asyncio.Future` (定义在 `asyncio/futures.py`, 运行时是 C 加速版 `_asyncio.Future`) 是一个"结果还没到"的占位符, 核心行为两条: `await future` 没结果就挂起; `future.set_result(x)` 由别人把结果投进来, 唤醒所有等待者。

它是协程世界的"信箱", 也是事件投递的最小模型:

```python
async def waiter(future, name):
    result = await future
    print(f"{name} 收到事件: {result}")

async def event_source(future):
    await asyncio.sleep(1)                     # 模拟: 1 秒后事件到达
    future.set_result("紧急消息: page-D 插单了")

future = asyncio.get_running_loop().create_future()
await asyncio.gather(waiter(future, "agent"), event_source(future))
```

实测输出:

```text
agent 收到事件: 紧急消息: page-D 插单了
```

对照第三节的六步链: `set_result` 触发的正是"登记的回调进 `_ready` → `__wakeup` → `send(None)`"。消息队列、订阅通知, 底层都是这个模型的变体。

### Task: 会自己跑的 Future

`asyncio.Task` 定义在 `asyncio/tasks.py` (L71, `class Task(futures._PyFuture)`, 运行时 C 加速版 `_asyncio.Task`)。

它是 Future 的子类, 实测: `issubclass(asyncio.Task, asyncio.Future)` 为 True。

区别在驱动方式: Future 是裸占位符, 结果要别人 set; Task 包着一个协程, 创建后事件循环自动用 `call_soon` 把它排进 `_ready` 开始推进, 不需要任何人喂。

日常用法: `asyncio.create_task(coro)` 把协程变成并发任务; `asyncio.gather(...)` / `asyncio.wait(...)` 批量等待; `asyncio.as_completed(...)` 按完成顺序取结果 (第二节用过)。

这两个对象是第二篇的伏笔: Future 是信箱, Task 可中断——事件驱动 Agent 的两大语言级基石已经齐了。

## 五、多线程: 包装 I/O 耗时任务

协程之外, 标准库里的另一条路是多线程。

先补 GIL 的账。GIL (全局解释器锁) 保证同一时刻只有一个线程在执行 Python 字节码, 这就是多线程无法并行计算的原因。但线程阻塞在 I/O 上时 GIL 会释放, 其他线程可以跑——所以多线程解决 I/O 并发完全没问题, 解决 CPU 并行完全没用。实测确认当前构建的 GIL 状态:

```python
import sys
print(sys.version.split()[0], "GIL 启用:", sys._is_gil_enabled())
```

实测输出:

```text
3.13.12 GIL 启用: True
```

(顺带交代版本现状: 3.13 起有实验性 free-threading 构建, 3.14 起正式支持, 但默认构建仍带 GIL。它解决的是 CPU 并行, 不改变本篇结论。)

用 `threading` 直接开三个线程跑第一节的剧本:

```python
results = []
def worker(url: str):
    time.sleep(2)  # I/O 等待期间 GIL 释放, 其他线程可以跑
    results.append((url, time.perf_counter()-t0))

t0 = time.perf_counter()
threads = [threading.Thread(target=worker, args=(u,)) for u in ("page-A", "page-B", "page-C")]
for t in threads: t.start()
for t in threads: t.join()
```

实测总耗时 2.0 秒, 和协程版打平。

不过日常更推荐走 `concurrent.futures` 的线程池, 而不是手工开 `Thread`:

```python
from concurrent.futures import ThreadPoolExecutor

with ThreadPoolExecutor(max_workers=8) as pool:
    future = pool.submit(blocking_call, arg)   # 定义在 concurrent/futures/thread.py
    result = future.result()                   # 定义在 concurrent/futures/_base.py
```

这里出现了本篇的第二个 Future——`concurrent.futures.Future`。它和 `asyncio.Future` 同名但不是一个类, 实测:

```python
import asyncio
from concurrent.futures import Future as CFuture
print(asyncio.Future is CFuture)   # False
```

两者的边界: `asyncio.Future` 在事件循环里 `await`, 挂起不占线程; `concurrent.futures.Future` 只能 `.result()` 阻塞等, 没有 await。不混用。

顺带把概念归属钉死: Task 是 asyncio 的家当, `concurrent.futures` 和 `threading` 里没有 Task。

### 桥: 协程里怎么跑阻塞代码

真实项目里大量库没有 async 版本, 直接调用会卡死事件循环——同步代码不 yield, 循环一圈都转不动。

桥就是为这个准备的: 把阻塞调用丢进线程池, 循环继续转。入口两个, 3.9+ 优先用 `asyncio.to_thread` (定义在 `asyncio/threads.py`):

```python
result = await asyncio.to_thread(blocking_sdk_call, arg)
# 等价于:
result = await loop.run_in_executor(None, blocking_sdk_call, arg)
```

验证"循环没被卡死"最直观的方式, 是阻塞调用执行期间让另一个协程打心跳:

```python
blocking = loop.run_in_executor(None, blocking_sdk_call)  # 丢进线程池
heartbeat = asyncio.create_task(pinger())                 # 每 0.4s 打一次心跳
result = await blocking
```

实测输出:

```text
  0.4s  event loop 还活着
  0.8s  event loop 还活着
阻塞调用结果: blocking result, 总耗时 1.0s
```

Agent 执行工具时调阻塞 SDK, 全靠这一手。

## 六、多进程: CPU 密集任务的路

先看协程这条路对 CPU 密集任务为什么走不通。

"await 让出控制权"有个前提: 代码得有 await。纯 CPU 计算没有 await, 它会让事件循环彻底停摆。实测——一个协程纯 CPU 空转 2 秒, 期间每 0.5 秒打一次心跳的协程被完全饿死:

```python
async def cpu_heavy():
    x = 0
    t = time.perf_counter()
    while time.perf_counter() - t < 2:   # 纯 CPU 空转, 中间不 await
        x += 1
    return x

hb = asyncio.create_task(heartbeat())    # 每 0.5s 打一次心跳
x = await cpu_heavy()
```

实测输出:

```text
cpu_heavy 结束, 耗时 2.0s, 期间心跳全部停摆
  2.5s  心跳
  3.0s  心跳
```

0.5 秒、1.0 秒、1.5 秒、2.0 秒的四次心跳全部消失, 直到 CPU 空转结束才恢复。

所以 CPU 密集任务和协程、多线程都不对付 (GIL 也不许), 唯一的出路是多进程——每个进程有独立的解释器和独立的 GIL, 真并行。`concurrent.futures.ProcessPoolExecutor` (定义在 `process.py`) 把这些细节包掉了:

```python
from concurrent.futures import ProcessPoolExecutor

def cpu_consume(n: int) -> int:
    ...  # 每个任务纯 CPU 跑 1 秒

with ProcessPoolExecutor(max_workers=4) as pool:
    results = list(pool.map(cpu_consume, range(4)))
```

实测: 同样的 4 个 CPU 任务, 串行 4.0 秒, 4 进程并行 1.4 秒。

两个使用前提: 一, 示例必须写成独立脚本并把执行代码放在 `if __name__ == "__main__":` 保护下——macOS/Windows 默认用 spawn 方式起子进程, 子进程会重新 import 主模块, 没有保护会无限递归起进程; 二, 不要在 jupyter notebook 里跑 (notebook 的 `__main__` 不是文件, 子进程 import 不到, 直接报错)。这也解释了为什么本节没有 notebook 单元格。

到这里选择规则齐了: 纯 I/O 且有 async 版本, 走协程; 只有阻塞版本, 丢线程池; CPU 密集, 走进程池。

## 七、演出验证: 同一剧本三版实现

回到底那个抓网页的剧本, 三种写法实测:

同步版: 串行等待, 6.0 秒。

多线程版: 三个线程并行等 I/O, 2.0 秒。

协程版: 三段等待重叠, 2.0 秒, 且能按完成顺序取结果。

多线程和协程打平, 因为这个场景里大家都在等 I/O, 谁都不吃 CPU。差异在别处: 线程版要面对锁、切换开销和调试复杂度, 协程版单线程无锁, 但要求全链路 async、不能混入阻塞调用。

不过, 这三版实现有一个共同的死穴。三个场景它们全都接不住:

1. 任务执行中途, 第三方插进来一条紧急消息——同步只能靠预设检查点轮询, 检查点之间的延迟不可控;
2. 任务发起方中途喊停——阻塞在工具调用上时, 连轮询的执行权都没有;
3. 一百个会话同时在线, 要求同一会话串行、跨会话并行——线程版要手工加锁, 协程版还缺一个"谁来路由事件"的层。

第 1 条能靠轮询硬扛, 第 2 条和第 3 条是结构性的死局。

解开死局需要的, 不只是"能并发", 而是一套"事件随时可以到达、到达后能被路由到正确协程"的机制。本篇的地基已经备齐: Future 是信箱 (第四节), 取消在 await 点生效 (第四节), 事件循环会因 I/O 就绪唤醒协程 (第三节)。下一篇把这三块拼起来, 就是事件驱动的 Agent Loop。
