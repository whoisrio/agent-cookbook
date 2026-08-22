我来深入研究一下 DeepSeek 依赖的 Cordis 插件系统源码。先定位 vendor 里的 Cordis 源文件和 DeepSeek 的插件注册用法。

让我先探索目录结构和关键文件。
Cordis 是这套 harness 的框架层（vendored，`@deepseek-ai/cordis` 4.0.0-rc.7）。核心机制全在 `vendor/cordis/src/` 下几个文件里。我把关键源码都读一遍。
`Context` 是一个被 `Proxy` 包裹的类（L74），核心服务 `reflect/registry/events/logger` 都挂在 root 上。现在读 `registry.ts`、`reflect.ts`、`fiber.ts`、`service.ts`、`events.ts`、`utils.ts`。
核心机制逐渐清晰。`RegistryService.plugin()`（registry L316）创建 `Fiber`；`ReflectService` 作为 Proxy handler（reflect L135）做服务解析，`provide()` 注册 `Impl` 并 `notify` 唤醒依赖方；`mixin` 把 `plugin/inject/on/emit` 等挂到 `ctx`。现在读最关键的 `fiber.ts`（持有插件实例、Effect、生命周期）、`service.ts`、`events.ts`、`utils.ts`。
Fiber 是整个运转的核心：生命周期状态机、`_checkImpl`/`_refresh`/`_setEpoch`/`_reload`/`_unload` 的 epoch 驱动逻辑、`effect()` 的 disposer 注册与逆序清理。现在读 `events.ts`（消息总线 `EventsService`）、`service.ts`、`utils.ts`（symbols、DisposableList、effect meta）。
核心源码已通读。现在找 DeepSeek 自带的插件实现作为真实例子——一个继承 `Service` 的 service、一个 `inject`/`provide`/注册 effect 的插件。搜一下实际用法。
选几个真实且简洁的例子。读 `core/agent-default-model`（service 子类）、`core/system-prompt`（inject + provide）、`core/tools`（典型 provide service）。
两个例子已经很好：
- `AgentDefaultModelConfig extends Service` → `super(ctx, 'agentDefaultModel')` → 底层走 `ctx.reflect.provide()` 注册 `Impl`（reflect L277）。
- `SystemPrompt` 用 `this.layers.effect(this.ctx, …)` 这类 effect 注册器声明可清理资源。

再看一个"既 inject 又 provide、并注册 effect"的典型插件——`core/agent-loop`（提供 agentLoop，注入 systemPrompt/llm/tools），以及确认 harness 里插件是如何被 mount 的（cordis.yml / bundle）。
让我确认 `agent-loop` 的 Service 类定义与 `inject`/`provide` 声明，以及它的插件导出形式。
`AgentLoop` 在构造里直接访问 `ctx.agents`、`ctx.systemPrompt`、`ctx.get('sessionPersistence')`，这些服务必须先就位——说明它的依赖是在插件挂载处（cordis.yml 条目）声明的，而不是写在类上。我确认一下 bundle 里 `agentLoop` 条目的 `inject` 声明方式，以及 headless 插件如何被 mount。
agent-loop 的 YAML 条目没写 `inject`，但构造里访问了 `ctx.agents`/`ctx.systemPrompt`。我确认 `AgentLoop` 类本身是否声明了静态 `inject`（之前的 grep 只匹配了 `inject:` 行首）。
我已经把 Cordis 核心 9 个源文件 + DeepSeek 自带插件（`agent-default-model`、`system-prompt`、`agent-loop`）逐行读完了，关键链路也确认了：`AgentLoop` 在 L297 用 `static inject = ['agents','sessions','llm','tools','systemPrompt']` 声明依赖，构造里通过 `super(ctx, 'agentLoop')` 提供自身服务（L320），并用 `ctx.effect(...)` 注册可清理资源（L349-380）。

下面先给你两张图把对象关系和"依赖如何驱动激活"画清楚，再逐层拆解源码。
两张图把骨架画出来了。下面按"对象 → 注册 → 解析 → 激活 → 清理"的顺序，逐层对照源码讲清楚，最后用 DeepSeek 自带插件的真实代码串一遍，并解码那些"少见 TS 语法"。

---

## 1. 你那 4 个判断，对应到源码是什么

| 你的理解 | 源码落点 |
|---|---|
| ① Context 持有 Registry / Reflect，靠消息总线控制激活 | `Context` 构造器（context.ts:71-84）一次性 new 出 `reflect/registry/events/logger` 四个内置服务；激活信号一边走 `ReflectService.notify` 直接遍历 fiber，一边经 `EventsService` 发 `internal/plugin` / `internal/status` / `internal/service` 事件 |
| ② 真正运行的插件实例由 Fiber 持有 | `Fiber`（fiber.ts:184）持有 `inject`（依赖表）、`_store`（已满足的 Impl 快照）、`_disposables`（清理器）、`runtime`；插件的回调/实例就是 `_runner.execute`（fiber.ts:247-263） |
| ③ `Registry.plugin()` 注册自身+依赖，依赖满足时 Cordis 驱动激活 | `RegistryService.plugin`（registry.ts:316）建 `Fiber`；`fiber.ts:611 _refresh()` 算 `epoch`，`fiber.ts:625 _setEpoch()` 在 epoch 变化时触发 `_reload`/`_unload` |
| ④ Effect 声明资源并返回清理函数 | `Fiber.effect`（fiber.ts:418）、`ReflectService.provide`（reflect.ts:277）、`EventsService.on`（events.ts:288）底层全是 effect；disposer 逆序执行（fiber.ts:675-696） |

先记住一个总纲：**Context 是一个 Proxy；每次 `ctx.plugin()` 创建一个 Fiber；每个 Fiber 通过 `parent.extend({ fiber: this })` 得到一个"子 Context"；插件在子 Context 里读服务（经 Proxy get trap 解析）、provide 服务（写进 ReflectService.store）、注册 effect（塞进 `_disposables`）。**

---

## 2. Context：为什么是个 Proxy，以及方法怎么挂上去

`Context` 类本身只持有几个 symbol 字段和四个服务（context.ts:71-84）：

```ts
const self = new Proxy<this>(this, ReflectService.handler)   // context.ts:74
this.root = self
this.fiber = new Fiber(self, {}, Object.create(null), null, ...)  // :77  root fiber
this.reflect = new ReflectService(self)
this.registry = new RegistryService(self)
this.events  = new EventsService(self)
this.logger  = new LoggerService(self)
return self
```

关键点：
- **root fiber 的 `runtime` 是 `null`**（fiber.ts:320-332），它永远 `ACTIVE`、且 `store = {}`。它是所有服务解析的"树根"。
- `ctx.on` / `ctx.plugin` / `ctx.get` / `ctx.provide` 这些"看起来像属性"的方法，**不是 Context 类上的方法**，而是 `ReflectService` 构造时通过 `mixin` 挂上去的访问器（reflect.ts:219-222）：

```ts
this.mixin('reflect',  ['get', 'set', 'provide', 'accessor', 'mixin'])
this.mixin('registry', ['inject', 'plugin'])
this.mixin('events',   ['on','once','parallel','emit','serial','bail','waterfall'])
```

`mixin` 本质是给 `ctx` 加一组 `accessor`（reflect.ts:364-390），读取时转发到对应服务对象。所以你写 `ctx.plugin(...)` 实际落到 `ctx.registry.plugin(...)`。

- `extend()`（context.ts:99-107）做原型继承式子上下文，**不改父**。这是 Cordis 隔离/作用域的基础——每个插件跑在自己的子 ctx 上，但能向上读到父级服务。

---

## 3. Registry：插件是怎么被"登记"的

`RegistryService.plugin`（registry.ts:316-336）做了四件事：

1. `resolve(plugin)`（registry.ts:222）把"函数 / 类 / `{apply}` 对象"三种形态统一成可执行的 `callback`；
2. 在 `_internal: Map<Function, Plugin.Runtime>` 里**按 callback 身份**取/建一个 `Runtime`（registry.ts:322-328）——同一个插件函数被多次 `plugin()` 会共享同一个 Runtime，但每个调用是一个独立 `Fiber` 挂到 `runtime.fibers`（registry.ts:140）；
3. `new Fiber(this.ctx, config, Inject.resolve(inject), runtime, ...)` 建出这个插件的 Fiber（registry.ts:330）；
4. 返回一个**thenable 包装**（registry.ts:331-335），所以 `await ctx.plugin(X)` 能等到加载完成（fiber.ts:704 `await`）。

注意：`Plugin.Base` 上的 `inject / provide / Config`（registry.ts:100-111）就是插件声明依赖与能力的元数据。`Inject.resolve`（registry.ts:71-88）把数组或对象形式的依赖规整成 `{ 服务名: 拦截配置 }` 的纯 map。

---

## 4. Reflect + Proxy trap：服务是怎么"读出来"和"注册进去"的

这是 Cordis 最绕、也最精妙的部分。

**读取（get trap）** reflect.ts:135-171：当你在插件里写 `ctx.systemPrompt`，Proxy 的 get 陷阱接管。它先查 `props`（声明过的 service/accessor），否则沿 **fiber 树向上**找 `Impl`（reflect.ts:152-167）：

```ts
let fiber = (ctx[symbols.shadow] ?? ctx).fiber
while (true) {
  const impl = fiber.store?.[prop]          // 当前 fiber 提供的服务
  if (impl) return getTraceable(ctx, impl.value)
  if (prop in fiber.inject) throw error      // 声明了却没满足 → 报错
  if (!fiber.runtime) throw error            // 到 root 还没找到
  if (fiber.parent[symbols.isolate][prop] !== key) throw error
  fiber = fiber.parent.fiber                 // 向上一级
}
```

这里有两处硬约束，是你理解 Cordis 设计哲学的关键：
- **"without inject" 报错**（reflect.ts:144, 160）：读一个服务前，必须先在 `inject` 里声明它。这就是依赖声明的强制化——不声明就访问等于 bug，框架直接抛 `cannot get property "X" without inject`。
- **隔离作用域 `isolate`**（reflect.ts:154, 164）：服务按 `symbols.isolate[name]` 这个"隔离标签"做 key。两个插件用不同 `label` 调 `ctx.isolate(name)`，就能读到不同实现而不互相影响（context.ts:121-125）。

**注册（provide）** reflect.ts:277-305：

```ts
provide(name, value, check) {
  return this.ctx.fiber.effect(() => {
    this.ctx.root[symbols.isolate][name] ??= Symbol(name)   // 分配隔离标签
    const impl = { name, value, fiber: this.ctx.fiber, check }
    if (this.store[key]) throw new Error(`service "${name}" already registered`)
    this.store[key] = impl                 // 写进 root reflect 的 store
    this.ctx.fiber.store![name] = impl     // 也记到本 fiber 的 store
    if (this.ctx.fiber.state === ACTIVE) this.notify([name])   // 唤醒依赖方
    return async () => {                   // 清理：删 Impl + 再 notify
      delete this.store[key]
      const fibers = this.notify([name])
      await Promise.allSettled(fibers.map(f => f.await()))
      delete this.ctx.fiber.store![name]
    }
  }, `ctx.provide(${name})`)
}
```

注意 `provide` 本身**就是一个 effect**——它返回的清理函数会把服务摘掉并再次 `notify`。这正好呼应你的第④点。

**唤醒（notify）** reflect.ts:314-336：遍历 `registry` 里**所有** runtime 的所有 fiber，若某 fiber 的 `inject` 包含刚变化的服务名，就 `_checkImpl(name)` + `_refresh()`。这是"依赖驱动激活"的引擎。

---

## 5. Fiber：epoch 驱动的激活状态机（核心）

每个 Fiber 有个 `inject`（自己声明要的服务）和一个 `_store`（当前**实际满足**的 Impl 快照）。激活与否，完全由 **epoch** 这个字符串决定。

**`_refresh`** 算 epoch（fiber.ts:611-623）：

```ts
_refresh() {
  let epoch = ''
  for (const name of Object.keys(this.inject)) {
    const impl = this._store[name]
    if (!impl) { epoch = INACTIVE; break }   // 有依赖没满足 → INACTIVE
    epoch += ':' + impl.fiber.uid             // 否则拼接各依赖提供者的 uid
  }
  this._setEpoch(epoch)
}
```

**`_setEpoch`** 是状态机的开关（fiber.ts:625-639）：只有当 epoch **真的变了**才动作——从 `INACTIVE` 变到非空 → `_reload()`（激活）；从非空变回 `INACTIVE` → `_unload()`（清理）。

**`_reload`**（fiber.ts:646-673）是"真正运行插件"的地方：
- 先把 `_store` 拷成 `this.store`（供插件读依赖）；
- `await Promise.resolve()` 后校验 epoch 没被并发失效（本地 fork 的硬化点，`vendor README` #6）；
- `_resolveConfig` 跑 schema 校验（`fiber.ts:50 resolveConfig`，用 `@standard-schema`）；
- `await this._execute(this._runner)` 执行插件回调/构造实例（fiber.ts:656）——**这一步里插件才能 `provide` 新服务、注册 effect**；
- 抛错则 `_error` 记下、状态转 `FAILED`，且 logger 吞掉错误不向上冒。

**`_unload`**（fiber.ts:675-696）把所有 `_disposables` **逆序**清空并执行——也就是运行你 effect 返回的清理函数。

`_checkImpl`（fiber.ts:597-609）负责把某个服务"是否当前满足"反映进 `_store`：查 `reflect._getImpl(name, true)`（只认 ACTIVE fiber 提供的，reflect.ts:241），过 `check` 谓词，满足才写 `_store[name]`，否则删掉。

所以整条链路是：**某服务 provide → notify → 各 fiber `_checkImpl` 更新 `_store` → `_refresh` 重算 epoch → `_setEpoch` 触发 `_reload`/`_unload` → 回调里再 provide 新服务 → 级联**。这就是你第③点描述的机制，精确到行。

---

## 6. Effect：统一的一切副作用入口

你第④点说"Effect 返回清理函数，失效时调用"。源码里 **所有**会随 fiber 卸载而撤销的操作都是 effect：

- `ctx.provide`（reflect.ts:278）
- `ctx.on` / `ctx.once`（events.ts:254 `register` 内 `ctx.fiber.effect`）
- `ctx.accessor` / `ctx.mixin`（reflect.ts:345, 366）
- 插件本身（fiber.ts:265，插件被 dispose 时卸载）

`Fiber.effect`（fiber.ts:418-561）的机制：
- 返回的 `dispose` 调用一次就执行所有收集到的 `disposables`，**逆序**（fiber.ts:431 `disposables.splice(0).reverse()`）；
- 它同时是 **thenable**（fiber.ts:555），所以 `await ctx.on(...)` 拿到的是清理器；
- 支持多种 body 形态（`_execute`，fiber.ts:356-400）：返回单个清理函数、返回 Promise、返回 `Iterable`（同步生成器，**边 yield 边收集**）、返回 `AsyncIterable`（异步生成器，每次 `next()` 产出一个清理器）。这就是源码里那些 `function*` 形 effect 的来源；
- 本 fork 对 reentrancy 做了硬化（`vendor README` #6）：effect 在 `execute` 跑之前就先把自己挂到 owner 的 `_disposables`，异步清理在 settle 前对外部 owner 可见，便于外层 effect join 清理。

---

## 7. 事件总线：激活/卸载如何被"广播"

`EventsService`（events.ts:131-319）是真正的消息总线，支持 `parallel/emit/serial/bail/waterfall` 五种分发（events.ts:183-243）。关键内置事件（events.ts:329-352）：
- `internal/plugin`（fiber.ts:302 发布新 fiber）、
- `internal/status`（fiber.ts:586 状态变化时 emit）、
- `internal/service`（reflect.ts:333 provide/摘掉服务时 emit）、
- `internal/update`（配置热更新 waterfall，可 veto）、
- `internal/get` / `internal/set`（Proxy 读写拦截 waterfall）。

`dispatch`（events.ts:165-175）还实现了**上下文过滤**：带 `thisArg` 的派发会用 `Context.filter` 过滤监听者——这是"某插件只听到自己作用域内的事件"的基础。而 `ctx.on` 注册监听本身也是 effect，所以监听器随 fiber 卸载自动移除（events.ts:256）。

---

## 8. DeepSeek 自带插件的真实写法（把上面串起来）

**① 提供方——`AgentDefaultModelConfig`**（`packages/core/agent-default-model/src/index.ts:64-82`）：

```ts
export class AgentDefaultModelConfig extends Service {
  constructor(ctx: Context, config: Config) {
    super(ctx, 'agentDefaultModel')   // → Service 构造调 ctx.reflect.provide('agentDefaultModel', this)
    ...
  }
}
export default AgentDefaultModelConfig
```

`Service` 基类构造（service.ts:42-59）就是 `self.ctx.reflect.provide(name, self, this[Service.check])`。所以这个类**既是插件（被 `ctx.plugin(AgentDefaultModelConfig, config)` 启动），又是服务（把自己 provide 成 `ctx.agentDefaultModel`）**。它在 base bundle 的 `cordis.patch.yml:63-64` 被挂载。

**② 提供方 + effect 注册——`SystemPrompt`**（`packages/core/system-prompt/src/index.ts:338-455`）：同样 `extends Service` 且 `super(ctx, 'systemPrompt')`。它提供 `section()` / `context()` / `variable()` 等方法，内部一律走 `this.layers.effect(this.ctx, ...)`（reflect.ts 风格的 effect 注册器），返回的清理器正是 Cordis effect disposer——这就是你第④点的标准用法：注册一段运行时状态，卸载时自动撤销。

**③ 依赖方 + 提供方——`AgentLoop`**（`packages/core/agent-loop/src/index.ts:296-380`）：

```ts
export class AgentLoop extends Service implements AgentFactory {
  static inject = ['agents', 'sessions', 'llm', 'tools', 'systemPrompt']   // :297 声明依赖
  static Config = z.object({ ... })                                          // :300 schema 校验
  constructor(ctx: Context, config: Config) {
    super(ctx, 'agentLoop')                                                  // :320 提供 agentLoop
    ctx.effect(() => () => this.ownership.dispose(), 'agentLoop.transactions()')   // :349
    ctx.effect(() => ctx.agents.setFactory(this), 'agentLoop.setFactory()')         // :350
    ctx.systemPrompt.variable('provider', c => c.agent?.options.provider)           // :351
    ...
    ctx.effect(() => {                                                        // :370
      const fiber = ctx.inject(['sessionPersistence'], childCtx => { ... })
      return fiber.dispose
    }, `agentLoop.resume(${id})`)
  }
}
```

它把第 2、3、4、6 节全串上了：
- `static inject`（agent-loop:297）声明要 `agents/sessions/llm/tools/systemPrompt`——这些服务没全部满足前，`AgentLoop` 的 Fiber 停在 `PENDING`（epoch = INACTIVE）；
- `super(ctx, 'agentLoop')` 把自己 provide 出去，唤醒依赖 `agentLoop` 的下游；
- `ctx.effect(...)` 注册的清理器（ownership dispose、factory 注册、resume 监听）会在 fiber 卸载时逆序执行；
- `ctx.inject(['sessionPersistence'], ...)`（agent-loop:371）就是 `RegistryService.inject` 的简写（registry.ts:300），等价于一个"依赖满足才跑、变了就重跑"的临时插件。

**挂载驱动**：这些插件不是手写 `ctx.plugin()` 调起来的，而是 `loader` 插件读 `cordis.yml` / `cordis.patch.yml`（如 `packages/bundle/base/cordis.patch.yml`，以及 headless 里的 `inject: [headlessStartup]` 条目）去调用 `ctx.plugin(Module, config)`。Loader 是 Cordis 生态的"组装器"，但底层依然是 `RegistryService.plugin` → `new Fiber` 这一条路。

---

## 9. 那些"少见 TS 语法"解码

你看到的"奇奇怪怪"的写法，大部分是 Cordis 为"Proxy + 声明合并 + 零运行时开销"服务的有意设计：

1. **`declare module './context.ts' { interface Context { ... } }`**（如 context.ts:164、reflect.ts:7、events.ts:34）—— TypeScript 的**声明合并 / 模块增强**。因为 `Context` 是 Proxy，具体类型靠各服务/插件往 `Context` 接口上"补"属性（如 `ctx.agentLoop`、`ctx.on`）。运行时无代价，只为类型检查。

2. **`static readonly effect: unique symbol = symbols.effect`**（context.ts:44）—— `unique symbol` 给品牌类型，配合 `Symbol.for('cordis.effect')`（utils.ts:60）。`Symbol.for` 让符号**跨 realm 全局唯一**，所以 `Context.is`（context.ts:61）能用全局符号判断"这是不是 Cordis 的 ctx"，即使有多份 Cordis 副本或被 iframe/worker 隔离也能识别。

3. **`const enum FiberState`**（fiber.ts:147）—— 内联枚举，编译后**没有运行时对象**，直接用整数（`PENDING=0`…`UNLOADING=5`），省一次查表。状态比较全是整数比较。

4. **条件类型推导配置**（`registry.ts:148-162`）：
   ```ts
   type GetPluginConfig<P> = P extends Plugin.Transform<infer S, any> ? S
     : GetPluginParameters<P>[0]
   type Spread<T> = undefined extends T ? [config?: T] : [config: T]
   ```
   从插件形态（函数/类/对象/Transform）**反推**出 `ctx.plugin(X, config)` 的 config 类型，并据此决定 config 是否可选。这是为什么 TS 能帮你校验 `agentLoop` 的 config 形状。

5. **`this` 多态类型**（`root: this`，context.ts:22；`get(): undefined | this[K]`，reflect.ts:17）—— 让子类型/链式调用保持精确返回类型。

6. **计算 symbol 索引签名**（`[symbols.isolate]: Dict<symbol>`，context.ts:18）—— 用全局 symbol 作 key 把"隐藏元数据"（隔离表、拦截表）塞进 `Context` 接口，既类型安全又不污染普通属性名。

7. **`namespace` 与函数合并**（`export function Inject(...) {}` + `export namespace Inject { export function resolve(...) }`，registry.ts:37-89）—— `Inject` 同时是**装饰器函数**和**带工具方法的命名空间**，调用 `Inject('x')` 是装饰器，`Inject.resolve(...)` 是工具。

8. **Generator / AsyncIterable effect 类型**（`fiber.ts:83-93`）：effect body 可以是 `function*` 同步生成器或 `async function*` 异步生成器，**每 yield 一个 disposer 就立即登记一个**——适合"边建边登记、边拆边清"的资源（如一串监听器）。

9. **`Object.create(null)`**（如 reflect.ts:209 `store = Object.create(null)`）—— 造一个**无原型**的纯字典，避免 `__proto__` / `constructor` 等原型键被误读，也避免 `in` 操作符命中原型。Cordis 的 `store/props/inject/_internal` 全用这个。

10. **`!!js` 不是 TS，是 schemastery 的 YAML 方言**（见 `cordis.patch.yml` 里的 `mode: !!js process.env.DSH_TOOLS_MODE`、`task: !!js ctx.headlessStartup.task`）—— 它让 cordis.yml 里能内嵌**在加载时被求值的真实 JS 表达式**（读环境变量、读其他服务）。这是 Loader 组装配置时的能力，和 Cordis 运行时无关，但你在插件配置里会高频撞见。

---

### 一句话收尾
Cordis 的运转本质就是：**Proxy 做服务解析 + 基于 `inject` 声明的依赖图 + 由 `epoch`（依赖满足快照）驱动的 Fiber 状态机 + 一切副作用都被收编成"可逆序清理的 effect"**。读服务前必须 declare inject（强制依赖显式化），provide 服务会经 `notify` 唤醒依赖方，依赖齐了 `epoch` 变非空就 `_reload` 跑插件，缺了就 `_unload` 逆序清 effect——这就是你说的"依赖满足条件时 Cordis 驱动激活"。

需要的话，我可以再带你跟一遍 `loader` 如何把 `cordis.yml` 翻译成 `ctx.plugin()` 调用（组装入口），或者深入 `getTraceable`/`createTraceable`（那套"让嵌套调用里的 `ctx` 永远指向正确调用方上下文"的追踪代理，是 Cordis 最容易被忽略但最关键的魔法）。