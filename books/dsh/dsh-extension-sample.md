# cordis 核心工作原理（由浅入深）

> 面向 DSH 插件开发者与想理解 DSH 底层机制的读者。
> 事实依据：cordis 源码仓库（v4.0.0-rc.8）与 DSH 仓库 vendored 副本（v4.0.0-rc.7，`vendor/cordis/`，rescope 为 `@deepseek-ai/cordis`）。
> 注意 cordis 仍处于 active development，API 不稳定，本文以 v4-rc 为准；v3 时代的 `MainScope` / `EffectScope` / `FiberCollection` / `ctx.scope` 已不存在。



## 0. 一句话版本

cordis 是一个"元框架"（Meta-Framework of Spatiotemporal Composability），它只解决两件事：

- **空间维度的组合**：谁依赖谁。
  插件通过 `inject` 声明依赖、通过 Service 发布能力，cordis 负责把依赖图接起来。
- **时间维度的组合**：注册的东西什么时候撤销。
  插件运行期间登记的每一个副作用（监听器、定时器、服务、子插件）都是可逆的 effect，插件停掉时全部自动回收。

DSH 奉行 "everything is a plugin"，模型适配器、工具注册表、会话日志乃至 agent 循环本身，都是挂在同一棵 cordis 插件树上的节点。
所以理解 cordis 就是理解 DSH 的骨架。

## 2. 第二层：五大核心概念

### 2.1 Context —— 大厦与楼层

`Context` 是一切能力的挂载点，就是场景里的"大厦"。
根 Context（`new Context()`）本身就是一个 **Proxy**（构造时 `new Proxy(this, ReflectService.handler)`）；`extend()` 出的子 Context 是原型链上的普通对象，但属性访问最终仍落到同一个 Proxy handler 上。
属性读写不落在对象本身，而是被路由到"服务台"上——`ctx.shell` 实际是沿 fiber 链向上查找名为 `shell` 的服务实现。

Context 之间用**原型链**形成父子关系（大厦与楼层）：

```ts
const ctx = new Context()          // 根 context，自带一个已激活的根 Fiber
const child = ctx.extend({})       // 楼层：Object.create 原型继承，读不到就沿链向上找
child.isolate('database')          // 独立水表：'database' 服务在这层与大厦互不通用
child.intercept('logger', { name: 'foo' })  // 本层统一规矩：给 logger 附带配置
```

- `extend()`：创建子 context，父子共享服务台，但各有自己的 Fiber。
  日常写插件用不到它——`ctx.plugin()` 会自动为每个插件建子 context；需要手动 extend 的是加载器、沙箱这类框架代码。
- `isolate(name)`：独立水表，隔离是双向的——子树内 provide 的同名服务对外也不可见。
  典型场景：同一个服务（如 database）要在两个子树里各跑一份独立实现。
- `intercept(name, config)`：给某个服务在子树内的消费方附带配置（如 logger 的名字）。
  插件的 `inject: { logger: { name: 'foo' } }` 对象形式会在 Fiber 构造时自动并入 intercept，日常优先用它，而不是手动调 `ctx.intercept()`。

### 2.2 Plugin 与 Registry —— 入驻登记处

插件（入驻团队）有三种形态（`registry.ts`）：

```ts
type Plugin =
  | ((ctx: Context, config) => any)            // 函数插件（DSH 里最常用）
  | (new (ctx: Context, config) => any)        // 类插件
  | { name?, inject?, Config?, apply(ctx, config) }   // 对象插件
```

`ctx.plugin(plugin, config)` 的内部流程（签入驻协议的全过程）：

先明确主语：**按这个开关的是"楼外的加载方"**——DSH 里是 cordis-loader 按 `cordis.yml` 的条目批量调用，测试或手写代码里也可以直接 `ctx.plugin(...)`。
Service 自己永远不调它（Service 是被它触发构造的）；Registry 也不"调用"它——`ctx.plugin` 只是 mixin 到 Context 上的访问器，真正的办理逻辑就是 `Registry.plugin` 本身。

```
ctx.plugin(Foo, config)
   │
   ▼
Registry.resolve(Foo) ──取出入⼝函数（函数本身或 obj.apply）
   │
   ▼
建/取 Plugin.Runtime（同一插件的注册信息 + 全部 Fiber 实例清单）
   │
   ▼
new Fiber(ctx, config, inject 依赖表, runtime)
   │  · 创建子 context（extend({ fiber })）
   │  · 用插件的 Config schema 校验 config
   │  · 检查 inject 的每个开业条件是否已满足
   │  · 把自己挂进父 Fiber 的 effect（父团队退租则一并清退）
   ▼
条件齐全 → 执行插件回调（开业）→ 状态 ACTIVE
条件缺失 → 停在 PENDING，回调不执行，等通知
```

同一个插件可以多次注册（同一品牌开多家门店），每个实例是一个 Fiber。
`ctx.inject(deps, callback)` 是 `ctx.plugin({ inject: deps, apply: callback })` 的语法糖。
`Config` 字段是一个 Standard Schema（DSH 里用 schemastery 的 `z.object(...)` 书写），注册时同步校验 config，不合法直接抛错。

### 2.3 Fiber —— 入驻协议（v4 的核心）

v4 把 v3 的三层 Scope 结构坍缩成单一的 `Fiber` 类。
**每个运行中的插件实例 = 一张入驻协议；每个 Context 有且只有一个当前 Fiber（`ctx.fiber`）。**

Fiber 的状态机（门店的状态）：

```
PENDING → LOADING → ACTIVE → DISPOSED
   ▲                   │
   └─── 条件缺失/变化 ──┘  （FAILED / UNLOADING 为中间态）
```

核心 API 是 `ctx.effect`——在协议附件上登记一条"撤场时怎么处理"：

```ts
ctx.effect(() => {
  const timer = setInterval(tick, 1000)
  return () => clearInterval(timer)   // 返回 dispose 回调：撤场时停掉它
})
```

effect 接受多种返回形态：dispose 函数、dispose 的 generator、Promise<dispose>、异步迭代器。
所有 dispose 被收集到当前 Fiber 的清单里，**退租时按 LIFO（后登记先执行）逐个 await，错误只记日志不上抛**。

这就是"注册即可逆"：插件里写的每一个 `ctx.on()`、`ctx.effect()`、服务 `provide`，都自动挂在当前 Fiber 上，插件停掉时无需手动清理。
**Fiber 本身也是分层的**：每个插件 Fiber 记录自己的 `parent`（注册它时所在的 context），并把自己挂进父 Fiber 的 effect 里。
于是 Fiber 形成一棵树，且 fiber 链恰好等于 context 父子链——它既是上一节服务查找的"上楼"路径，也是退租时"主团队退租、分包小组一并清退"的级联路径。

### 2.4 Service 与 provide/inject —— 挂牌与开业条件

Service 是命名的能力发布点（职能部门）。
注意区分两个角色——**Definition** 只定义"大厦里可以有这个部门"，**Provider** 才是真正入驻挂牌的团队：

```ts
// Service Definition：抽象基类，定义部门类型，本身不入驻（所以没有 inject）
abstract class ShellExecutor extends Service {
  constructor(ctx: Context) {
    super(ctx, 'shell')   // 构造时自动 reflect.provide('shell', this)
  }
}

// Service Provider：被加载方（loader / 你的代码）通过 ctx.plugin() 注册的实现类
class LocalBashExecutor extends ShellExecutor {
  static inject = ['subprocess']          // 这类插件的开业条件，Registry 在构造前就要读
  static Config = z.object({ /* … */ })   // 入驻时校验的配置
}
```
调用链：`ctx.plugin(LocalBashExecutor)`（加载方按开关）→ `Registry.plugin`（办理签约、`new Fiber`）→ Fiber 激活时 `new LocalBashExecutor(ctx, config)` → 基类构造里 `provide('shell', this)`（挂牌）。

`inject` 之所以是 `static`：它描述的是"这类插件"的依赖，Registry 在 `new Fiber`、执行构造之前就要拿到它，等不到实例诞生。
DSH 的真实例子：Definition 在 `packages/shell/shell`（抽象 `ShellExecutor` + `declare module` 合并出 `ctx.shell` 的类型），Provider 在 `packages/shell/bash-local`（`LocalBashExecutor`，`static inject = ['subprocess']`，用 subprocess 实现全部抽象方法）；win32 平台换挂 pwsh 的 Provider，消费方代码一行不动。

`new` 一个 Service 子类时，基类构造函数替你做了两件事（`service.ts`）：
一是 `constructor(protected ctx, name)` 把 ctx 存成实例属性——**每个服务对象都持有自己插件的那个子 Context**（套间，不是根），之后随时可以用 `this.ctx.on()` / `this.ctx.effect()` 继续登记，全部记在自己团队的协议附件上；
二是构造函数最后一行调用 `ctx.reflect.provide(name, this)`——挂牌不是额外的注册步骤，而是 `new` 出实例那一刻的副作用。
由此有一条使用纪律：热替换时会 `new` 新实例重新挂牌，旧实例随撤场作废，所以消费方要始终通过 `ctx.shell` 现用现取，不要把服务实例缓存进自己的闭包——否则热替换后，你手里拿的是已撤场的旧东家。

关键机制（`reflect.ts` 的 `ReflectService`）：

- `provide(name, impl)` 本身是一个 effect——**部门的存活期 = 挂牌团队的存活期**。
  团队退租，部门自动摘牌。
  同名部门重复挂牌会同步抛错（物业当场拒绝）；旧部门摘牌后再挂同名是合法的，这正是热替换的前提。
- 部门挂牌/摘牌时 cordis 调用 `notify([name])`，遍历注册表里所有 Fiber，凡是开业条件含该名字的就触发 `_refresh()`。
  精确时机：挂牌团队已 ACTIVE 时 provide 立即通知；在插件回调执行期间 provide 的（Service 构造就属于这种），等 Fiber 激活时统一补发通知。
- 每个 Fiber 维护一个 **epoch**：把各依赖当前由哪家部门提供（提供方 Fiber 的 uid）拼成的字符串。
  换了一家供水公司 → epoch 变化 → 先 `_unload()` 旧 effect 再 `_reload()` 重走开业流程。
  **这就是热重载和依赖驱动启停的全部秘密：换实现 = 换 epoch = 自动重新开业。**

读取侧由 Context 的 Proxy handler 完成（`internal/get` 管道）。
存储分两级：全楼一本总账在根的 `reflect.store`（按 isolate 符号作 key）；每个 Fiber 另有一本小台账 `fiber.store`，只放两样东西——本 Fiber 直接 provide 的服务，和本 Fiber inject 且已解析的依赖（激活时拷入，卸载时清空）。
查找沿 fiber 父链（恰好等于 context 父子链）逐层向上：本层命中即返回——正常消费方在第 0 层就命中，因为 inject 的依赖已拷进自己的小台账，根本不用上楼。
没命中时的规则：声明了 `inject` 但没就绪，报"在 inactive context 中取必需服务"；没声明就继续向上爬，能借到父 Fiber inject 过或提供过的服务，但跨越 isolate 边界会被拦下；爬到根 Fiber 还没有，报 `cannot get property ... without inject`。
对插件而言，**inject 既是开业条件，也是访问许可**（根 Context 例外：取不到就静默返回 `undefined`）。

### 2.5 事件系统 —— 广播与会签

事件方法（`ctx.on/once/emit/…`）通过 `mixin` 挂到 Context 上，实现在 `events.ts`。
监听器通过 `ctx.fiber.effect` 登记，**随所属 Fiber 退租自动移除**。

| 方法 | 语义 |
|---|---|
| `emit` | 广播：同步依次调用全部监听器，忽略返回值；监听器异常不捕获，直接抛给调用方 |
| `parallel` | 同时询价：`Promise.allSettled` 并发，有失败则聚合为 `AggregateError` |
| `serial` | 逐级请示：依次 await，返回值不是 `null`/`undefined`/`false` 即有人拍板，短路返回（`0`、`''` 也算拍板） |
| `bail` | serial 的同步版 |
| `waterfall` | 会签单：末参是 `next`，层层传递；某一环不调用 `next()` 即打回，流程终止 |

`waterfall` 是 DSH 钩子（如 `tools/pre-execute` 权限闸门）的基础：监听器不调 `next()` 即短路否决。
配置更新、属性读取等内部流程（`internal/update`、`internal/get`）也走 waterfall，因此可以被拦截器接管。

### 2.6 附：reflect —— 楼管

`reflect`（`ReflectService`）是 Context Proxy 的 handler 实现者，就是场景里的楼管。
它管三件事：接线（`provide/get/set`，把 `ctx.shell` 这样的属性访问路由到当前实现）、属性混入（`mixin`，如把 `ctx.plugin` 代理到 `registry.plugin`）、挨个打电话（`notify`，部门挂牌/摘牌时通知所有依赖方）。
平时写插件不直接碰它，但读源码时所有"自动"（为什么 `ctx.shell` 能取到服务、为什么依赖方会自动停业）都在这。

## 3. 第三层：生命周期全景

### 3.1 一个普通插件的一生

```
new Context()                      大厦开张，根 Fiber 直接 ACTIVE
   │
ctx.plugin(MyPlugin, config)       团队签入驻协议
   │
   ├─ 建子 context、校验 config、检查开业条件
   ├─ 条件就绪 ──▶ LOADING ──执行回调──▶ ACTIVE（开业）
   │                 回调里的 ctx.on / ctx.effect / provide 全部登记到协议附件
   │
   ├─ 营业中：依赖的部门换了东家？
   │     └─ epoch 变化 ──▶ LIFO 撤场 ──▶ 重新走开业流程（热重载）
   │
   └─ 退租（手动 / 主团队退租 / 依赖的部门摘牌）
         └─ LIFO 逐个执行撤场登记（监听器、定时器、部门摘牌、分包小组清退…）
              └─ DISPOSED
```

### 3.2 一个带依赖的插件（`inject: ['timer']`）的完整往返

1. 入驻时大厦里没有 `timer` 部门 → 协议停在 PENDING，不许开业，但铺位留着。
2. 某团队入驻并挂出 `timer` 部门；待该团队开业时 `notify(['timer'])` → 等待中的团队被唤醒 → 开业（ACTIVE）。
3. `timer` 团队退租 → 部门自动摘牌 → 再次 `notify` → 依赖方 epoch 失效 → LIFO 撤场 → 回到 PENDING。
4. 新团队挂出 `timer` → 依赖方自动重新开业。

整个过程插件作者**不写一行业务外的代码**——这是 cordis 时空组合的威力所在。

### 3.3 三个容易踩的坑（大厦事故通报）

- **忘了登记的东西物业不管**：裸 `setInterval` 不会被回收，只有包进 `ctx.effect` 的定时器才随退租清理。
  vendored 的 `@deepseek-ai/cordis-plugin-timer` 把 `setTimeout` / `setInterval` / `throttle` / `debounce` 包装成自动登记版并挂到 ctx 上，优先用它。
- **停业再开业是重新开张，不是接着昨天继续**：依赖热替换 = 回调整体重跑，`apply` 闭包里的内存状态全部丢失。
  需要跨重启保留的账目要交到职能部门手里（放进 Service 实例——Service 通常是被依赖、较少重启的那一方），不要放在 `apply` 的局部变量里。
- 重新开业时回调抛错 → Fiber 进入 FAILED、epoch 失效，门店停摆直到下次依赖变化。
  反过来，撤场登记（dispose）里的错误只记日志，不会中断其余资源的回收。

## 4. 插件五问：从问题到 API

每个插件（入驻团队）都要回答五个问题，在 cordis 里各有明确的对应物：

| 问题 | 写字楼里的说法 | cordis 对应物 |
|---|---|---|
| 我是谁 | 店招 | `name`（插件对象/模块导出的 name，日志与调试的身份） |
| 我提供什么能力 | 挂牌成立部门 | 继承 `Service`（构造即 `provide`），或在回调里用 `ctx.effect` 注册 |
| 我依赖什么能力 | 开业条件 | `inject: ['shell', 'tools']`（数组为必选；对象形式可附带 intercept 配置） |
| 我需要什么资源 | 添置的东西 | 回调里登记的 effect：`ctx.on()`、`ctx.effect()`、定时器等 |
| 我如何取消资源 | 协议附件的撤场登记 | effect 返回的 dispose 回调；不写也行——退租时 LIFO 自动回收全部 |

DSH 的函数式插件把这五问写成最朴素的形式：

```ts
// packages/shell/tool-bash/src/index.ts 的形态
export const name = 'tool-bash'
export const inject = ['tools', 'shell', 'systemPrompt', 'shellEnv']

export function apply(ctx: Context) {
  ctx.tools.register({ /* … */ })   // 登记即 effect，退租自动回收
}
```

## 5. cordis 在 DSH 中的角色

- **vendored 而非依赖**：DSH 把 cordis 核心、官方插件（loader / include / group / timer / hmr / logger-console）以及基础库 cosmokit、schemastery 拷入 `vendor/`，rescope 为 `@deepseek-ai/*`，理由是 "fully owns its framework layer (auditable, patchable, pinned)"。
  本地偏离上游的改动逐条登记在 `vendor/README.md` 的 Local modifications 里（如 fiber.ts 的生命周期加固：UNLOADING 期间禁止新建 effect）。
  本文的机制描述以上游 v4.0.0-rc.8 为准，通读 vendored 源码时个别细节可能略有出入。
- **启动即组树**：`app-boot` 用 cordis-loader + include + group，把 profile / bundle / patch 分层组合后从 `cordis.yml` 驱动整棵插件树——相当于物业按一张招商清单批量安排入驻。
- **capability seam 三角色**：Service Definition（抽象服务 + `declare module '@deepseek-ai/cordis'` 声明合并出 `ctx.<key>`，即先规定"大厦里可以有 shell 这个部门"）→ Service Provider（实现，`static inject` 声明自己的开业条件）→ Consumer（`inject` 消费）。
- **没有封装层**：DSH 刻意直接使用 cordis 原语（`name` / `inject` / `apply` 函数插件、`Service` 子类），不设自己的 Plugin 基类。
  规矩是 "Registrations are effects"——一切贡献都要登记到协议附件上（`ctx.effect()` / `ctx.on()`）。

## 6. 进一步阅读

- DSH 仓库 `docs/cordis-primer.md`：官方入门（五大概念、waterfall 语义、loader 的 `!!js` 配置）。
- DSH 仓库 `docs/cookbook/extension-cookbook.md`：tool / hook / UI 插件模板。
- 源码：DSH `vendor/cordis/src/`（9 个文件，体量很小，推荐通读 `fiber.ts` 与 `reflect.ts`）。
- cordis 论文：《A Programming Paradigm for Spatiotemporal Composability》（github.com/cordiverse/paper）。
