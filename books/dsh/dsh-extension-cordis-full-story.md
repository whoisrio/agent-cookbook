# cordis 核心工作原理

## 从一个故事开始

有一座孔太斯大楼（Context），瑞迪星咖啡集团（插件）想到这里开店；
大楼要求每家到这里提供的服务供应商，开店和闭店都必须准备好标准流程，店铺由大楼委派的店长来运营；
店长负责要根据集团要求确认自己店铺能不能营业，营业依赖的条件何时能够满足，由楼管来通知；


**开店申请。**
星瑞迪向大楼的招商引资办（registry）递交一份开店申请：必须写明店名（`name`），以及开店必需的资源需求（`inject`，比如"需要供水、需要供电"）。
招商办受理后在名册上建一页（Runtime），签发一份入驻协议，并派下一位店长（fiber），咖啡店被分配在一楼A01（插件子 Context）。

**店长核对资源。等待开业**
店长上岗第一件事，是拿着资源需求清单去找楼管（reflect）核对：供水供电现在能不能被满足。
如果满足了就直接营业，如果还没有满足，店长就去店铺门口坐着等（PENDING），连招牌桌椅都不能摆——这些布置要等正式开业才能动手。
此后只要有人来大楼里提供服务，招商引资办都会通知楼管。楼管挨个给名册上的店长打电话，核对"这次变动有没有影响你的开业资源"，由店长自己确认是否能够营业。
楼管会就"某家店状态变了"发广播（对应 `internal/status` 事件）：哪家店开业了、停业了，凡是依赖它的店都会收到，跟着开或跟着停——一家倒，靠它的店也跟着倒。

**咖啡店开业**
没过多久，供水供电的供应商也通过招商引资办(registry)完成了注册，楼管挨个通知已经提交过营业申请的店铺，和每一个店长(fiber)沟通；
店长核实到咖啡店的依赖都齐了，于是店长按照瑞迪星集团的标准流程着手开始布置店铺，摆好桌椅，挂好招牌，开门营业。这些布置每做一样，
店长都顺手记在协议附件上（这就是 `effect` 登记），写明"停业时怎么收"——布置只在开业这几天存在，停业那天一律照单收回。


**咖啡店意外停业、恢复**
有一天，供水服务的水管爆了。楼管发现了这个情况，赶紧挨个通知各个店长，店长发现咖啡店依赖清单里的"供水"空了，赶紧关停了咖啡店，收起招牌、桌椅，回到门口等待(pending)楼管通知供水服务恢复。
好在没多久供水服务就恢复了，楼管第一时间通知了店长重新营业，于是店长又重新摆好桌椅，挂起招牌。


**秘书处。**
大楼还有个秘书处（logger），是开盘时就驻好的常设机构，默默把楼里发生的每件事写进台账：谁办了手续、谁开了业、谁停了业。


**广播系统。**
大楼提供一套广播系统（event），供各店按兴趣订阅频道。
- **单向广播，发完不管(emit)。** 比如供水部门今晚发一条 `emit` 广播"今晚 6 点停水"，所有订阅了供水频道的店各自做好应对，供水部门可不管你怎么应对。
- **广播，全员按顺序响应(waterfall)** 比如供水商要改造水路管线，需要商户逐个反馈改造影响，每家商户反馈完要主动通知下家继续反馈。
- **广播,等全员响应。(parallel)** 还是涨价这类公告，如果大楼要求"必须确认每家都回执了"，就换成 `parallel`——它会 await 所有监听器，等每家都处理完才返回。和 `emit` 的区别就在"大楼等不等回复"。
- **首个应答者拍板。(serial/bail)** 比如供电商希望断电检修，但是不能影响任何商家，于是通过这个应答拍板的方式通知各个上家，收到通知的任意一家商户有影响反馈，就不需要关心其他的反馈了。


## cordis核心对象简介
如上，这个小故事，就是Cordis插件的核心运作机制里；

大楼就是所有插件的顶层容器`Context`；
招商办公室即是`registry`，所有的插件注册要通过regisry；
楼管则是`reflect`，任何插件的状态变化，都由`reflect`来通知`fiber`做依赖核对，满足条件就上岗营业；
`events`是挂在`Context`上的事件系统，提供`emit` / `parallel` / `serial` / `bail` / `waterfall`**五种分派模式**，驱动各个插件协同工作；

```ts
export interface Context {
  ...
  root: this
  /** Base URL used to resolve relative plugin/module specifiers, if the runtime sets one. */
  baseUrl?: string
  /** The event bus. Its methods are also mixed onto `ctx` (`ctx.on`, `ctx.emit`, ...). */
  events: EventsService
  /** The logging service. Call `ctx.logger(name)` for a named logger. */
  logger: LoggerService
  /** The reflection layer backing the context proxy (`ctx.get`, `ctx.provide`, ...). */
  reflect: ReflectService
  /** The plugin registry. Its methods are mixed onto `ctx` (`ctx.plugin`, `ctx.inject`). */
  registry: RegistryService
}
```

`Context`在顶层定义中持有一个根级别的`fiber`对象，

```ts
//context 初始化：根 fiber 的 inject 为空集合，_refresh() 算出的 epoch 是空串 '' 而非 INACTIVE，
//所以一开局就处于就绪（ACTIVE）状态——并非"第四个参数填 null"这种因果。
  constructor() {
    this[symbols.isolate] = Object.create(null)
    this[symbols.intercept] = Object.create(null)
    const self = new Proxy<this>(this, ReflectService.handler)
    this.root = self
    this.baseUrl = undefined
    this.fiber = new Fiber(self, {}, Object.create(null), null, () => [])
    this.reflect = new ReflectService(self)
    this.registry = new RegistryService(self)
    this.events = new EventsService(self)
    this.logger = new LoggerService(self)
    this.fiber._disposables.clear()
    return self
  }

  [Symbol.for('nodejs.util.inspect.custom')]() {
    return `Context <${this.fiber.name}>`
  }
```


所有插件定义，需要声明自己是谁，自己需要谁(可选)，自己执行服务的核心逻辑，要把自己提供的服务扩展到`context`中
```ts
declare module '@deepseek-ai/cordis' {
  interface Context {
    sell: (cups: number) => void
  }
}

export const coffeePlugin = {
  name: 'coffee',
  inject: ['water', 'power'] as const,
  async apply(ctx: Context) {
    // 开业流程：每次依赖就绪都会「重新走一遍」——本班营业账在此归零
    ...

    // 协议附件登记撤场处理（LIFO：后登记的先执行）
    ctx.effect(() => () => ctx.logger.info('撤场：摘下门口的画'))
    ctx.effect(() => () => ctx.logger.info('撤场：停掉订阅的报纸'))
    ...

    ctx.provide('sell', sell)

    // 退租清理：依赖消失时这段被调用，从最后一项往回执行
    return () => {
      ctx.logger.info('咖啡店停业（本班营业账 ' + shiftNote + ' 杯作废；财务部总账仍在）')
    }
  },
}
```

插件通过`registry`提供的`plugin`方法注册时，通过`inject`来声明依赖，
```ts
// 注册插件，返回与 fiber 绑定的 PromiseLike（其 .then 委托给 fiber.await()）
const coffee = ctx.plugin({
  name: 'coffee',
  inject: ['water', 'power'],   // 开业条件条款（依赖）
  apply(ctx) { /* 依赖就绪后才执行，即真正的开业 */ },
})
```

注册后，得到的是`fiber`对象，也就是上面故事里的店长，`fiber`是插件注册到`Context`后的真正运行对象；

```ts
// fiber（店长）—— 插件注册到 Context 后的真正运行对象，由 registry 在受理申请时 new 出来
class Fiber {
  // ① 开业条件条款（门禁）：声明需要哪些服务，缺一项 apply 不跑，咖啡店停在 PENDING
  inject: string[]

  // ② 生命周期状态机：状态由 epoch 跃迁 + _error + 是否 disposed 共同决定
  //    DISPOSED(dispose 已调) / FAILED(_reload 抛错，_error 被记) / ACTIVE(依赖齐) / PENDING(epoch=INACTIVE)
  //    LOADING · UNLOADING 是开业 / 撤场进行中的瞬态
  //    注：uid 只是 epoch 字符串里编码"依赖来自哪个 provider"的片段，不是独立参与状态计算的变量
  get state(): FiberState

  // ③ 开业流程本体：就是插件写的 apply，只有依赖就绪（epoch 翻成非 INACTIVE）时
  //    fiber 自己 _reload() → _execute(runtime.callback) 才执行——"依赖齐了才开业"是 fiber 决定的
  runtime: { callback: (ctx, config) => void }

  // ④ 撤场清单：本 fiber 用 ctx.effect() 登记的清理回调都收进这里（DisposableList），
  //    _unload() 时 clear() 倒序（LIFO）执行——即 effect 返回的逆序 dispose 函数
  _disposables: DisposableList

  // ⑤ 本 fiber 的「注销入口」：ctx.plugin() 时 framework 在父级 fiber 上替本 fiber 挂的 effect；
  //    其清理函数三步：① 从父 runtime.fibers 名册摘掉自己 → ② _setEpoch(INACTIVE)
  //    → ③ 触发本 fiber 的 _unload()（进而倒序清 _disposables）。
  //    注：_setEpoch(INACTIVE) 通知的是「依赖了本 fiber 所提供服务的消费者」重算并可能停业，
  //    不是简单「依赖它的店」。
  dispose(): PromiseLike<void>

  // ⑥ 决策权在 fiber 自己：reflect 通知依赖变了，fiber 读自己的 store 自己翻牌
  //    依赖齐 → _reload 开业；依赖没 → _unload 撤场（reflect 只传声，不喊开业）
  _refresh(): void
  _setEpoch(epoch: string): void
  _reload(): PromiseLike<void>
  _unload(): PromiseLike<void>

  // ⑦ 启动期故障感知：_reload 期间（config 校验或 apply 执行）抛错会被 catch 下来 →
  //    记到本 fiber 的 _error、并把 epoch 置回 INACTIVE（这家店直接停业，但不连坐其他店）；
  //    await() 等本次生命周期过渡结束后，若 _error 有值就把它重抛给调用方（按店隔离，错误不向上吞）。
  //    注：不是"静默 fail-soft"——错误被显式留存、由 await() 决定何时暴露，且出错店已 INACTIVE。
  await(): PromiseLike<this>
}
```

`ReflectService` 的关键方法是 `notify`：某个服务被 `provide` 时，按服务名反查依赖方（只命中 `inject` 含该名字的 consumer fiber，不遍历全场），逐个通知它们重算依赖——这就是故事里「楼管挨个打电话」的源码对应。

某个服务 `provide` 时，按服务名反查依赖方（只命中 `inject` 含该名字的 consumer fiber，不遍历全场），逐个通知它们重算依赖。
provider 在 `apply` 里主动 `ctx.provide(name, value)` → 写入 `ReflectService.store` 并触发 `notify([name])` → `notify` 反查命中者调 `_refresh()` → consumer 重算 `epoch`，经 `_setEpoch` 决定开业（`_reload`）或撤场（`_unload`）。
reflect 只传声不拍板，开不开由 fiber 自己的 `epoch` 跃迁定；
若 consumer 开业时又 `provide` 新服务，就进入下一轮 `notify`，形成级联。




```ts
  //reflect provide & notify
  provide(name: string, value?: any, check?: () => boolean) {
    return this.ctx.fiber.effect(() => {
      if (!this.props[name]) {
        this.props[name] ??= { type: 'service' }
      } else if (this.props[name].type !== 'service') {
        throw new Error(`property "${name}" is already declared as ${this.props[name].type}`)
      }
      this.props[name] = { type: 'service' }

      this.ctx.root[symbols.isolate][name] ??= Symbol(name)
      const key = this.ctx[symbols.isolate][name]
      const impl: Impl = { name, value, fiber: this.ctx.fiber, check }
      if (this.store[key]) {
        throw new Error(`service "${name}" has been registered at <${this.store[key].fiber.name}>`)
      }
      this.store[key] = impl
      this.ctx.fiber.store![name] = impl
      if (this.ctx.fiber.state === FiberState.ACTIVE) {
        this.notify([name])
      }
      return async () => {
        delete this.store[key]
        const fibers = this.notify([name])
        await Promise.allSettled(fibers.map(fiber => fiber.await()))
        // ensure self access before dependencies cleanup
        delete this.ctx.fiber.store![name]
      }
    }, `ctx.provide(${JSON.stringify(name)})`)
  }

  notify(names: string[], filter = (ctx: Context, name: string) => ctx[symbols.isolate][name] === this.ctx[symbols.isolate][name]) {
    const fibers: Fiber[] = []
    for (const runtime of this.ctx.registry.values()) {
      for (const fiber of runtime.fibers) {
        let hasUpdate = false
        for (const name of names) {
          if (!(name in fiber.inject)) continue
          if (!filter(fiber.ctx, name)) continue
          hasUpdate = true
          fiber._checkImpl(name)
        }
        if (!hasUpdate) continue
        fiber._refresh()
        fibers.push(fiber)
      }
    }
    for (const name of names) {
      const self: Context = Object.create(this.ctx)
      self[symbols.filter] = (target: Context) => filter(target, name)
      this.ctx.events.emit(self, 'internal/service', name, this._getImpl(name, false)?.value)
    }
    return fibers
  }
```


```ts
//fiber _checkImpl & _refresh & _setEpoch
  _checkImpl(name: string) {
    const impl = this.ctx.reflect._getImpl(name, true)
    if (!impl) return delete this._store[name]
    try {
      if (impl.check && !impl.check.call(getTraceable(this.ctx, impl.value))) {
        return delete this._store[name]
      }
    } catch (error) {
      impl.fiber.ctx.logger.error(error)
      return delete this._store[name]
    }
    this._store[name] = impl
  }

  _refresh() {
    let epoch: string | boolean = false
    epoch = ''
    for (const name of Object.keys(this.inject)) {
      const impl = this._store[name]
      if (!impl) {
        epoch = INACTIVE
        break
      }
      epoch += ':' + impl.fiber.uid
    }
    this._setEpoch(epoch)
  }

  private _setEpoch(epoch: string) {
    const oldEpoch = this._runner.epoch
    if (epoch === oldEpoch) return
    this._runner.epoch = epoch
    if (this.inertia) return
    this._updateState(() => {
      if (epoch !== INACTIVE && oldEpoch === INACTIVE) {
        this.inertia = this._reload()
        return FiberState.LOADING
      } else {
        this.inertia = this._unload()
        return FiberState.UNLOADING
      }
    })
  }
```

（同一个插件模块可以在不同 `Context` 下挂载多次，每次挂载都有**独立的 Fiber**——这正是"Fiber 是运行实例而非插件定义本身"的体现。）



## 结合代码重放一遍插件是如何在cordis里注册的

咱们现在把同一件事**从代码维度**走一遍。
>示例用 `examples/dsh/cordis/coffeeshop/`（多层级版：根 → 楼层管理 → 咖啡店 → 自营保洁），所有行号都指这个目录下的文件。先给一张「故事角色 ↔ 代码实体」对照表，后面每一拍都落在这张表上：

### 1. 第一步：`new Context()` —— 盖楼 + 开盘

首先，我们先创建Context(`main.ts:9`)：

```ts
const ctx = new Context()
```

这一行在 cordis 里完成了「盖楼 + 开盘」：Context构造函数，建出根 `Context`、挂好根 fiber（依赖为 `null`，永远 ACTIVE）、把 `reflect` / `registry` / `events` / `logger` 四个常设服务实例化并混到 `ctx` 上，
最后**返回一个被 `reflect` 包起来的 Proxy**——你之后写 `ctx.water`、`ctx.plugin(...)` 每次属性访问都先经过Proxy(楼管)。

### 2. 开店申请：注册插件 + 声明依赖

注册动作就是调 `ctx.plugin(...)`（它是 `registry` 的门面）。coffeeshop 里出现**两种注册形态**：

**形态 A —— 对象插件（普通团队）**，靠 `inject` 声明开业条件（咖啡店就是这种）：
```ts
// coffee.ts:17-19
export const coffeePlugin = {
  name: 'coffee',
  inject: ['water', 'power'] as const,   // ← 开业条件条款：缺一项 apply 不跑
  async apply(ctx: Context) { ... }      // ← 依赖就绪后才执行，即真正的开业
}
```
为了模拟嵌套关系，我们把 `coffeePlugin` 的注册放在楼层管理插件的 `apply` 里（`floor.ts:48`），也就是咖啡店「一楼A01」挂进来，而楼层管理插件本身没有 `inject`，注册即激活；
```ts
// floor.ts:48-49
const coffeeFiber = ctx.plugin(coffeePlugin)  // ← 招商办受理：建档 + new Fiber，返回 fiber
ctx.provide('coffee', coffeeFiber)            // ← 把「咖啡店 fiber」也挂出去，方便楼外 await
```

**形态 B —— Service 子类**：

```ts
// services.ts:20-24
export class WaterService extends Service {
  constructor(ctx: Context) {
    super(ctx, 'water')                 // ← 构造时向根 Context 挂牌 'water'，名字唯一
    ctx.logger('water').info('供水部门挂牌（大厦公用）')
  }
}
```

它的注册在入口处（先挂楼层管理之后，`main.ts:29`）：`await ctx.plugin(WaterService)`。
`Registry.resolve` 会 `new WaterService`，其 `super(ctx,'water')` 触发 `ctx.provide('water', this)`
**这个实例既是插件实例，也是 'water' 服务的实现**（牌子挂的就是 `this`）。`main.ts:30,31` 同理挂上 `power`、`finance`。

> `ctx.plugin` 背后 registry 做的「resolve → 建档 → `new Fiber`」、以及「fiber 是 `ctx.plugin` 现场 `extend`+`new` 出来的、`inject` 是写进 Fiber 的开业条件条款」这些机制，已在 §cordis核心对象简介 的 `registry` / `Fiber` 两段讲透，这里对照代码即可：`floor.ts:48` 那次 `ctx.plugin` 返回的就是新 `Fiber`，`services.ts:22` 那个 `super(ctx,'water')` 同时完成了「插件实例 + 服务挂牌」。

**两种形态的本质区别。** 二者都经 `ctx.plugin()` 受理、都 `new` 出 fiber、走同一套管线——区别不在"怎么注册"，而在**注册出来的对象"是什么角色"**：

| | 形态 A（对象插件） | 形态 B（Service 子类） |
|---|---|---|
| 角色 | 纯消费方 / 只有行为（故事里的「租户 / 团队」） | 既是插件又是服务（故事里的「部门」） |
| 挂牌 | 自己**不对外挂牌**，只靠 `inject` 声明"开业前得先借到谁" | 构造时 `super(ctx,'water')` 把 `this` 挂成 `ctx.water`，**注册即挂牌** |
| 被人使用 | `await` 它返回的 fiber，或它 `apply` 里主动 `provide` 的东西（如咖啡店 `ctx.provide('sell',…)` → 楼外 `ctx.sell(3)`） | 别人只要 `inject:['water']` 或 `ctx.water` 就能借到，无需知道它叫什么插件 |

一句话：**形态 A 要"一段可被依赖驱动启停的逻辑"，形态 B 要"一个可被借用的服务"。** 二者不互斥——对象插件在 `apply` 里多次 `ctx.provide()` 可挂多块牌子（团队+多部门）；Service 子类若 `inject` 了别人，就是"既当部门又当租户"。

#### 2.1 declare module：类型层注册 vs 运行时注册

前面 §2 讲的 `ctx.plugin` / `ctx.provide` / `ctx.on` 全是**运行时**动作——代码真正跑起来才建档、挂牌、订阅。但还有一个绕不开的问题：coffeeshop 里你写 `ctx.water.supply()`、`ctx.emit('water/maintenance', ...)` 时，TS 凭什么不报红？`water` 这个属性、`water/maintenance` 这个频道名，编译器是怎么知道的？

答案是 **`declare module 'cordis'`**——这是 TypeScript 的「接口合并（declaration merging）」，用来给 cordis 的 `Context` / `Events` 接口**补类型**。它**只活在编译期，不在运行时创建任何东西**，所以严格说它不是"注册"，而是"类型层承诺"。

**两条扩展路径，对应两类名字：**

**(a) 事件频道名 —— 扩 `Events` 接口。** coffeeshop 里频道类型的约束写在 `services.ts:14-17`：

```ts
// services.ts:14-17
declare module 'cordis' {
  interface Events {
    'water/maintenance'(message: string): void   // ← 补一个频道：名 + 回调参数类型
  }
}
```

补完之后，`ctx.emit('water/maintenance', msg)` 和 `ctx.on('water/maintenance', cb)` 的**频道字符串和回调参数**才受类型保护——拼错频道名（如 `'water/mantenance'`）、传错 `msg` 类型，编译期直接报错。运行时真正把监听挂上、把消息发出去的，依旧是 `EventsService`（`on`/`emit`），`declare` 一句都没参与。

**(b) 依赖注入的服务名 —— 扩 `Context` 上的属性。** 那些能直接 `ctx.water` / `ctx.power` / `ctx.meetingRoom` 点出来的属性，运行时是 `Service` 构造时 `super(ctx,'water')` → `ctx.provide('water', this)` 挂上去的；但要让 `ctx.water` 在 TS 里点得出来、拿到的类型正确，同样靠 `declare module 'cordis'` 给 `Context` 补索引签名 / 具名属性。否则 `ctx.water` 就是 `any` 甚至编译报错。

**一句话区分三层：**

| 层面 | 谁负责 | 动作 | coffeeshop 落点 |
|---|---|---|---|
| 类型层 | `declare module 'cordis'` | 接口合并，给 `Context`/`Events` 补名字与类型 | `services.ts:14-17`（Events） |
| 运行时-服务 | `ctx.provide` / `super(ctx,'key')` | 真正把实例挂到 fiber 上 | `services.ts:22`（`super(ctx,'water')`） |
| 运行时-插件 | `ctx.plugin` | resolve → 建档 → `new Fiber` | `main.ts:22` / `floor.ts:48` |
| 运行时-订阅 | `ctx.on` / `ctx.emit` | 调 `EventsService` 动态挂监听 / 发消息 | `coffee.ts:35` / `main.ts:34` |

**插件提供的 service 名、事件频道名，要在 TS 工程里做到类型安全，都得靠 `declare module 'cordis'` 扩接口**；但 `declare` 只解决"写代码不报红、拼错能查出来"，真正的注册（`plugin` / `provide` / `on`）是运行时那一摊。故事侧重运行时，所以没单列 `declare`；落到真实 TS 项目，这两类名字缺了类型扩展，IDE 和编译器都不会认。

#### 2.2 Service 子类也有依赖，且有两种 provide 时机

前面把 Service 子类当成"构造即挂牌的部门"，容易让人以为它和依赖无关。其实**Service 子类照样能声明依赖**，而且它的"挂牌时机"有两种写法，语义完全不同——这正是 coffeeshop 里 `WaterService` 和 `PowerService` 的对照。

**(a) 构造时直接 provide（无依赖、硬挂牌）—— `WaterService`**

```ts
// services.ts:20-28
export class WaterService extends Service {
  constructor(ctx: Context) {
    super(ctx, 'water')                 // ← 构造即挂牌：Service 基类在这里调 ctx.provide('water', this)
    ctx.logger('water').info('供水部门挂牌（大厦公用）')
  }
  supply(): string { return '自来水' }
}
```

`super(ctx,'water')` 在构造器里**同步**把 `this` 挂成 `ctx.water`。只要这个 Service 自己没有 `inject`，它一注册（`main.ts:29`）就立刻挂牌生效——供水部门不依赖任何人，所以"开盘即营业"。这是最常见的写法：**部门无前置条件，挂上去就能用**。

**(b) 依赖就绪后、apply/init 里 provide—— `PowerService`**

```ts
// services.ts:30-53
export class PowerService extends Service {
  static inject = ['water']             // ← Service 子类照样能声明依赖（fiber 级 inject，和对象插件同机制）
  constructor(ctx: Context) {
    super(ctx, 'power-placeholder')     // ← 占位名，先不挂真名 'power'
    ctx.logger('power').info('供电所建成，但得等通水才挂牌营业')
  }
  [Service.init]() {                    // ← Service 版的 apply：依赖齐了才跑
    this.ctx.provide('power', this)     // ← 这时才把 'power' 真正挂出去
    this.ctx.logger('power').info('通水了，供电部门正式挂牌（大厦公用）')
  }
  available(): number { return 100 }
}
```

`PowerService` 声明了 `static inject = ['water']`：供电所自己得先通水才能运转。fiber 等 `water` 就绪后才 `_reload`、跑 `[Service.init]`，里面的 `ctx.provide('power', this)` 才执行——**'power' 这块牌子是"依赖齐了才挂"的**。运行日志印证了这点：

```
[09:00] 供水部门挂牌（大厦公用）          ← water 先上
[09:00] 供电所建成，但得等通水才挂牌营业  ← power 构造完成、仍 PENDING
[09:00] 通水了，供电部门正式挂牌（大厦公用）← water 就绪 → init 跑 → power 挂牌
[09:00] 咖啡店开业！... 供电=100kW        ← 此刻 coffee 的 inject[water,power] 才全齐
```

**两种时机的本质差别（一句话）：**

| 写法 | 挂牌时机 | 适用 | coffeeshop 落点 |
|---|---|---|---|
| 构造时 `super(ctx,'key')` | 注册即挂，不看过路依赖 | 部门无前置条件 | `WaterService` / `FinanceService` |
| `apply`/`[Service.init]` 里 `provide` | 依赖齐了才挂 | 部门自己也依赖别人 | `PowerService` |

补充一点：`Service.init` 就是 Service 子类的 `apply`——fiber 判定 `runtime.callback` 是构造函数时，会 `new` 出实例、再调 `instance[Service.init]()`（见 §cordis核心对象简介 的 `Fiber` 执行段）。所以"对象插件的 `apply`"和"Service 子类的 `init`"是**同一个机制的两张脸**：都是"依赖齐了才执行的一段逻辑"，区别只是对象插件这段逻辑不自动挂牌、Service 子类常在这段逻辑里 `provide` 自己。

> **一个实战坑（复业等待的写法）：** 既然激活是异步的，复业时**不能**写 `await ctx.get('coffee'); ctx.sell(2)`——`get` 拿到 fiber 后 `await` 是非 thenable 的、立即返回，而咖啡店是异步 reload 的，`sell` 还没挂上就会 `TypeError`。coffeeshop 复业段（`main.ts:46-49`）改成了**等 `internal/service` 事件**：cordis 每完成一次 provide 都会 `emit('internal/service', name, value)`，监听 `name === 'sell'` 才真正动手。这个事件类型也在 `services.ts` 的 `declare module` 里补了签名（呼应 §2.1：运行时注册和类型声明要配套）。

### 3. 店长核对资源 / 等待激活（PENDING）

Fiber 一创建，就拿着自己的 `inject` 清单去核对每一项依赖有没有人 `provide`——查全了才 `_reload()` 跑 `apply` 开业（ACTIVE），没查全就停在 **PENDING**（`apply` 不执行）。这套「协议主动出击、楼管只传声」的机制（含 `_checkImpl` / `epoch` 状态机）已在 §cordis核心对象简介 的 `Fiber` 与 `reflect.notify` 两段讲透，这里只对照 coffeeshop 的时间线：

**一个重要的时间线事实：** 在这个多层示例里，`main.ts:22` 先 `await ctx.plugin(floorManagerPlugin)`、进而在 `floor.ts:48` 注册咖啡店；而 `WaterService` / `PowerService` / `FinanceService` 要等到 `main.ts:29-31` 才挂到根上。所以**咖啡店被注册的那一刻，water/power 还没挂牌**，它一出生就停在 **PENDING**，直到 `[09:00]` 供水/供电挂牌、依赖齐了才自动翻成 ACTIVE 开业——和故事里「开局 PENDING → 挂牌后自动开业」的节奏完全对齐。

那「等待激活」在 coffeeshop 里体现在哪？两个真实落点：

1. **开局 PENDING**：`main.ts:22` 注册咖啡店时 water/power 未挂牌 → 停在 PENDING，日志里此时没有任何「咖啡店开业」输出；`main.ts:29-31` 挂牌后自动开业。这是最直观的「等依赖就绪」演示。
2. **复业等待**（见 §6）：先删供水让咖啡店停业，再重新挂牌供水，此时咖啡店从停业→复业，`main.ts:46` 用 `await ctx.get('coffee')` 卡住等它重新激活完成（注意 `floor.ts` 里 `await coffeeFiber` 是另一个显式等待点，在楼层管理 `apply` 内等咖啡店 fiber 就绪）。

（单文件 `07-coffeeshop.ts` 也演示了开局 PENDING，但 coffeeshop 多层级版现在同样演示了——而且还能顺带展示「显式 await 等待点」和「复业等待」两种形态。依赖驱动启停 / 复业的状态机图见 §cordis核心对象简介 的 `Fiber` 状态机部分。）

### 4. 激活后开始工作（apply 执行）

依赖齐了，`apply(ctx)` 才跑——这就是「开业」。`coffee.ts:20-55` 里每一行都是开业后要干的活，逐一看：

```ts
// coffee.ts:22-23  ① 本班营业账清零（每次重新开业都重走一遍，账归零）
let shiftNote = 0
ctx.logger.info('咖啡店开业！供水=' + ctx.water.supply() + ' 供电=' + ctx.power.available() + 'kW')
```

```ts
// coffee.ts:28-29  ② 注册自营保洁（子插件，挂在咖啡店名下 → 随店清退）
await ctx.plugin(CleaningService)
ctx.logger.info('咖啡店叫自家保洁：' + ctx.get('cleaning')!.clean())
```
注意：查找链只**向上**（咖啡店→楼层→根），够不到自己挂的「孩子」，所以要用 `ctx.get('cleaning')` 直查，而不是 `ctx.cleaning`。查找链的向上穿透与 `ctx.get` 直查机制见 §cordis核心对象简介 的 fiber 链一节。

```ts
// coffee.ts:32  ③ 借楼层的共享会议室：楼层管理 provide 的，沿链向上命中，不用 inject
ctx.logger.info('咖啡店借楼层会议室：' + ctx.meetingRoom.book())
```

```ts
// coffee.ts:35-37  ④ 订阅广播（接收通知，见 §5）
ctx.on('water/maintenance', (msg) => {
  ctx.logger.info('收到大楼广播：' + msg + ' → 准备提前歇业')
})
```

```ts
// coffee.ts:40-41  ⑤ 协议附件登记撤场处理（LIFO：后登记的先执行）
ctx.effect(() => () => ctx.logger.info('撤场：摘下门口的画'))
ctx.effect(() => () => ctx.logger.info('撤场：停掉订阅的报纸'))
```

```ts
// coffee.ts:44-49  ⑥ 卖咖啡 + 把自己能提供的服务挂出去（provide）
const sell = (cups: number) => {
  shiftNote += cups
  ctx.get('finance')!.record(cups)        // 长期账交财务部（ctx.get 借根上常驻部门）
  ctx.logger.info('卖出 ' + cups + ' 杯（本班 ' + shiftNote + ' / 全店 ' + ctx.get('finance')!.balance() + '）')
}
ctx.provide('sell', sell)                 // ← 咖啡店对外挂牌 'sell'
```

```ts
// coffee.ts:52-54  ⑦ 退租清理（依赖消失时被调用，从最后一项往回执行）
return () => {
  ctx.logger.info('咖啡店停业（本班营业账 ' + shiftNote + ' 杯作废；财务部总账仍在）')
}
```

`ctx.provide('sell', sell)` 就是故事里「咖啡店对外挂牌」——之后楼外 `main.ts:37` 的 `ctx.sell(3)` 才能调到。**provide 出去的名字 = 别的插件/代码能 `ctx.xxx` 借到的分机。**

### 5. 如何发通知 / 如何接收通知

coffeeshop 里这条链路是**最简的 emit（单向，发完不管）+ on（订阅）**：

**发通知**（供水部门，作为发起方全网广播）：

```ts
// main.ts:34
ctx.emit('water/maintenance', '今晚18:00 停水')
```

`emit` 走的是 `events` 总线，把消息推到 `water/maintenance` 频道，**发完不管**——供水部门不关心谁收到、怎么应对。频道名在 `services.ts:14-17` 用 `declare module` 做了类型约束：

```ts
// services.ts:14-17
interface Events {
  'water/maintenance'(message: string): void
}
```

**接收通知**（咖啡店订阅自己关心的频道）：

```ts
// coffee.ts:35-37
ctx.on('water/maintenance', (msg) => {
  ctx.logger.info('收到大楼广播：' + msg + ' → 准备提前歇业')
})
```

`ctx.on(频道, 回调)` 是订阅，只收自己关心的频道。区别**只在「发的时候用哪个函数」**，广播系统共有四种模式（coffeeshop 只演示了 emit，其余三种在单文件 `07-coffeeshop.ts` 里演示）：

- `emit`：单向，发完不管（本示例即用）。
- `parallel`：广播 + **等全员回执**（await 所有监听器，都处理完才返回）。
- `serial` / `bail`：首位应答者拍板（任意一家有影响反馈就短路，其余不被调到）。
- `waterfall`：层层流转单（经手人不调 `next()` 即打回/短路）。

一句话：**发 = `ctx.emit/on/parallel/serial/waterfall`，收 = `ctx.on`**，频道字符串是双方的约定。频道名的类型层约束（`services.ts:14-17` 的 `declare module` 扩展 `Events`）与运行时 `on`/`emit` 的关系，已在 §2.1 讲透，这里不重复。

### 6. 依赖驱动启停 + 复业

这是 coffeeshop 最精彩的一段，把「等待激活 / 自动停业 / 自动复业」全演示了：

```ts
// main.ts:42  供水退租 → reflect 挨个通知依赖 'water' 的店长 → 咖啡店自动停业、自营保洁随店一并清退
ctx.registry.delete(WaterService)

// main.ts:45-47  新供水挂牌 → 楼管再核对 → 咖啡店重新走一遍开业流程（apply 重跑）
await ctx.plugin(WaterService)
await ctx.get('coffee')   // ← 等咖啡店重新激活完成（sell 重新挂上）再卖
ctx.sell(2)
```

谁挂牌/摘牌，楼管 `reflect.notify()` 就**按服务名反查**所有 `inject` 含该名字的 fiber，凡命中的就重算依赖——齐了自动开业，缺了自动撤场（**楼管只传声、不拍板，开不开由店长自己定**）。这套「`notify` 反查 + epoch 重算 + 级联」机制已在 §cordis核心对象简介 的 `reflect.notify` 一段讲透，这里对照代码看效果即可。

注意「本班营业账作废、营业总账交财务部」：闭包里的 `shiftNote` 随 `apply` 重跑丢失，所以**长期状态必须放进 Service 实例**（`FinanceService` 挂在根上，`balance()` 跨停业保留，`main.ts:49` 打印累计账本）。

最后 `main.ts:53` `ctx.registry.delete(floorManagerPlugin)` 触发**层级联清退**：三楼整层（咖啡店 / 面包店 / 保洁）一并撤场，每个 `ctx.effect` 登记的撤场项按 LIFO 倒序执行。

### 7. 退租清理（effect / dispose，LIFO）+ 层级联清退

经营期间每添置一样东西，都要在协议附件（`ctx.effect()` 的 dispose 清单）上登记「撤场时怎么处理」。咖啡店在 `coffee.ts:40-41` 登记了两样——摘画、停报纸；退租时从最后一项往回执行（LIFO）：日志里「停掉报纸」排在「摘下画」之前，因为报纸后登记、先撤。

```ts
// coffee.ts:52-54  apply 末尾 return 的清理函数，依赖消失时被调用
return () => {
  ctx.logger.info('咖啡店停业（本班营业账 ' + shiftNote + ' 杯作废；财务部总账仍在）')
}
```

`cleaning.ts:20` 也登记了撤场项，所以保洁随咖啡店一并清退。**「忘了登记的物业不管」**：没包进 `ctx.effect` 的副作用（比如裸 `setInterval`）在附件上没有条目，退租时自然漏收。

层级联清退：`floor.ts` 的楼层管理退租（`main.ts:53`）会让**父级 Context 对应的 fiber** 名下所有子 fiber（咖啡店、面包店）先撤场，咖啡店撤场又带动它名下的保洁撤场——一层套一层，整层清空。

### 8. 楼层与隔离（extend / isolate / fiber 链查找）

「楼层」不是 cordis 的内置概念，而是注册结构长出来的：楼层管理本身是一个插件（容器），挂在大厦根上，它的 `apply` 里再 `ctx.plugin` 挂本层租户（`floor.ts:48`）。于是咖啡店的查找链变成：咖啡店 → 楼层管理 → 大厦根——中间多出「三楼」这一层。

- **父级服务对子树可见**：咖啡店不 inject、直接 `ctx.meetingRoom` 借到楼层的共享会议室（`coffee.ts:32`，沿链向上命中楼层管理 `floor.ts:40` 的 provide）。
- **兄弟层不互通**：面包店 `ctx.cleaning` 会报错（`floor.ts:25`），因为自下而上查找只经过自己的父链，够不到旁支（咖啡店名下的 cleaning）。
- **ctx.get 直查**：咖啡店要用自己挂的保洁、或借根上的财务部，都用 `ctx.get('cleaning')` / `ctx.get('finance')`——查找链向上够不到「孩子」，得主动直查。
- **isolate / intercept**：跨独立水表（`isolate(name)`）的楼层会被拦下；`intercept(name, config)` 给楼层设统一餐标。隔离与拦截的查找链行为见 §cordis核心对象简介 的相关说明。

### 角色对照（故事 ↔ 代码）

- 大厦 → 根 Context（`new Context()`）
- 楼层 → 子 Context（`ctx.extend()`）
- 套间 → 插件的子 Context（`ctx.plugin()` 自动 `extend`，每份协议一间）
- 入驻团队 → 插件实例（`ctx.plugin()` 注册）；分包小组 → 子插件 / 子 Fiber
- 店长（带着入驻协议上岗）→ Fiber；开业条件 → `inject`；协议附件上的撤场登记 → `ctx.effect()` 收集的 dispose
- 部门 / 挂牌 → Service / `ctx.provide()`；门口已接通的分机 → `fiber.store`

注意「团队」和「部门」是两个角色，但常常由同一个对象兼任：Service 子类插件被 `new` 出来时，这个实例**既是插件实例也是服务实现**（牌子挂的就是 `this`）——团队自己就是部门。
不过二者不必然一体：一个函数插件可以在 `apply` 里多次 `ctx.provide()` 挂好几块牌子，也可以像纯消费方那样一块都不挂。
- 招商引资办与名册 → Registry（`Plugin.Runtime` 的集合）
- 楼管 → reflect（`ReflectService`，Context 这个 Proxy 的 handler）
- 独立水表 → `isolate(name)`；楼层统一餐标 → `intercept(name, config)`
- 广播 → `emit` / `parallel`；会签单 → `waterfall`（`serial` / `bail` 是它的变体）
- 秘书处 → logger（`LoggerService`，开盘即驻的内置常设机构；默认只写内部 ring buffer，对外需挂 exporter）
- 财务部（保管长期账目的部门）→ 承载长期状态的 Service 实例（挂在根上，咖啡店不靠它开业、用 `ctx.get` 借它记账）



----
## 1.12 从 cordis 的视角看这一天

带入到cordis的视角，context就是这间大楼，
每一个希望在这间大楼里提供服务的团队就是一个插件（Plugin），比如coffeeshop，
coffeeshop到大厦的招商引资办注册服务，要告诉招商引资办(registry)我是谁，我开店依赖供水和供电，依赖的服务ready了就告诉我开工；

```typescript
ctx.plugin({ name: 'coffee', inject: ['water', 'power'], apply(ctx) { /* 开业后干什么 */ } })
```

招商引资办(registry)把coffeeshop的开店要求记录好，交给专门负责这家店的店长，去楼管(reflect)查找能够提供coffeeshop依赖的服务，如果没有，就要求楼管在有合适的供水商ready的时候通知他，这个时候coffeeshop因为还没有等到依赖就只能pending开店营业；
当供水团队到context注册之后，楼管(reflect)就和各个店长核对一遍；
当供电团队到context注册之后，再核对一遍——供水供电都齐了，coffeeshop 的店长收到通知、重新检查开业条件，条件满足就从 pending 变成开业（ACTIVE），`apply` 开始执行。
之后任何服务挂牌或退租，楼管都会挨个通知相关店长重新核对——这就是 §1.2 要逐拍重放的"自动开业、自动停业、自动复业"。


**咖啡店反复关停，账单丢了么**
原本用的供水退租了，依赖它的店长被通知当天停业。注意是"重新走一遍开业流程"——店长脑子里临时记的当班营业账作废，要长期保存的营业总账得交到财务部（承载长期状态的 Service）手里。


**秘书处。**
大楼还有个秘书处（logger），是开盘时就驻好的常设机构，默默把楼里发生的每件事写进台账：谁办了手续、谁开了业、谁停了业。
它按部门自动打标签（日志名取自店长所属品牌），所以"星瑞迪写的 error"天然带星瑞迪前缀。秘书处默认只把记录写在楼内台账（ring buffer，容量 1000 条），要对外（比如打到控制台）得额外接一根出口（exporter）。


---
<details><summary>级联机制补充说明（源码细节）</summary>

- **Provider 写入实现**：`provide(name, value)` 把 `impl = { name, value, fiber }` 写进 `ReflectService.store`（`reflect.ts:292`），同时 `notify([name])`。
- **反查并通知消费者**：`notify` 对 `inject` 含该名字的 consumer 调 `_checkImpl(name)` 再 `_refresh()`——此时的「本 fiber」是**消费者插件**，不是 provider，也不是 reflect。
- **`_checkImpl`：把依赖抄进自己的 `_store`**：consumer 去 `ReflectService.store` 查该依赖，`fiber.ts:608` 有则把这条 `impl` 写进**消费者私有的 `fiber._store`**，无则删掉自己 `_store` 里的该项。它只是「查 store → 写/清自己这本 `_store`」的刷新动作，**绝不调用 `provide`**（`provide` 永远只在 `apply` 里由开发者写）。
- **`_refresh`：消费者重算 epoch**：只读自己的 `_store`（`fiber.ts:614`，不碰总账），遍历 `inject` 逐项取 `this._store[name]`，齐了算非空 epoch，缺一项就 `INACTIVE`。
- **`_setEpoch` 跃迁**：`INACTIVE` → 就绪则自己 `_reload()` 开业，翻回 `INACTIVE` 则自己 `_unload()` 撤场（对应故事里「开不开店由店长自己定」）。
- **观察者事件**：`notify` 末了 `emit('internal/service', name, value)` 是给**外部观察者**用的（如 §2.2 用它 `await` 等 `sell` 挂牌），**不是级联驱动力**——级联靠 `notify` 直接调 `_refresh`。

（同一个插件模块可在不同 `Context` 下挂载多次，每次都有**独立的 Fiber**——正是「Fiber 是运行实例而非插件定义本身」的体现。）

</details>


----
| 故事角色 | 代码实体 | 在 coffeeshop 里的落点 |
| --- | --- | --- |
| 孔太斯大楼 | 根 `Context`（`new Context()`） | `main.ts:9` |
| 招商引资办 | `registry`（`ctx.plugin` 是它的门面） | `main.ts:22,29` / `floor.ts:48` |
| 店长 | `Fiber`（注册后 `new` 出来的真正运行对象） | `floor.ts:48` 返回的 `coffeeFiber` |
| 楼管 | `reflect`（`ReflectService`，Context 这个 Proxy 的 handler） | 框架内部驱动，无直接调用 |
| 开业条件条款 | `inject: [...]` | `coffee.ts:19` |
| 部门 / 挂牌 | `Service` 子类构造时 `super(ctx,'key')` | `services.ts:22,32,45` |
| 秘书处 | `logger` | `main.ts:12-17` |
| 广播系统 | `events`（`ctx.emit` / `ctx.on` …） | `main.ts:34` / `coffee.ts:35` |