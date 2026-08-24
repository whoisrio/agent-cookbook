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

### declare module：类型层注册 vs 运行时注册
有一点需要说明一下，插件是随时可被激活或者卸载的，运行时cordis的机制保证了你的插件依赖肯定存在；
但是你的插件代码如果需要使用别的插件提供的能力，是没办法通过import的方式引入依赖的，为了让你的ts代码不飘红，要通过declare的方式来扩展定义；

比如声明你的插件提供的service或者函数，
```ts
declare module '@deepseek-ai/cordis' {
  interface Context {
    sell: (cups: number) => void
  }
}

declare module '@deepseek-ai/cordis' {
  interface Context {
    water: WaterService
    power: PowerService
    finance: FinanceService
  }
}
```

比如声明你的插件会发一个`water/maintenance`的事件
```ts
// services.ts:14-17
declare module 'cordis' {
  interface Events {
    'water/maintenance'(message: string): void   // ← 补一个频道：名 + 回调参数类型
  }
}
```

## 结合代码重放一遍插件是如何在cordis里注册的

咱们现在把同一件事**从代码维度**走一遍。
>示例用 `examples/dsh/cordis/coffeeshop/`（多层级版：根 → 楼层管理 → 咖啡店 → 自营保洁），所有行号都指这个目录下的文件。先给一张「故事角色 ↔ 代码实体」对照表，后面每一拍都落在这张表上：

### 0. 先看全景：样例里的 7 个插件与依赖关系

coffeeshop 一共 7 个插件，依赖关系如下，咱们主要看coffee、water和power：

| 插件 | 文件 | 依赖（inject） | 注册位置 | 对外提供 |
|---|---|---|---|---|
| `coffeePlugin` 咖啡店 | coffee.ts | `water`, `power` | floor 名下 | `sell` |
| `WaterService` 供水 | services.ts | 无 | 根 | `water` |
| `PowerService` 供电 | services.ts | `water` | 根 | `power` |
| `floorManagerPlugin` 楼层管理 | floor.ts | 无（注册即激活） | 根 | `meetingRoom` |
| `bakeryPlugin` 面包店 | floor.ts | 无 | floor 名下 | — |
| `CleaningService` 保洁 | cleaning.ts | 无 | coffee 名下 | `cleaning` |
| `FinanceService` 财务 | services.ts | 无 | 根 | `finance` |

### 1. 第一步：`new Context()` —— 盖楼 + 开盘

首先，我们先创建Context(`main.ts:9`)：

```ts
const ctx = new Context()
```

这一行在 cordis 里完成了「盖楼 + 开盘」：Context构造函数，建出根 `Context`、挂好根 fiber（依赖为 `null`，永远 ACTIVE）、把 `reflect` / `registry` / `events` / `logger` 四个常设服务实例化并混到 `ctx` 上，
最后**返回一个被 `reflect` 包起来的 Proxy**——你之后写 `ctx.water`、`ctx.plugin(...)` 每次属性访问都先经过Proxy(楼管)。

### 2. 开店申请：注册插件 + 声明依赖

注册动作就是调 `ctx.plugin(...)`（它是 `registry` 的门面）。coffeeshop 里出现**两种注册形态**：

**形态 A —— 对象插件**，靠 `inject` 声明开业条件（咖啡店就是这种）：
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
```
对于coffee这类业务插件，通过如上注册，等待Cordis的调度，满足依赖条件之后，开始运行自身的逻辑就可以了；
如果希望你的插件服务也可以被其他插件使用，比如咱们的例子里，面包店希望和咖啡店合作，卖咖啡，coffee就需要把自己的能力在apply的时候`provide`出来；
```ts
ctx.provide('coffee', coffeeFiber)            // ← 把「咖啡店 fiber」也挂出去，方便楼外 await
```
如果你的插件不对外提供服务，使用对象插件或者更简单的函数插件的方式就可以了。

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

供水、供电plugin注册在楼层管理之后()`main.ts:29`）。通过继承Service类的插件注册，通过调用父类构造函数完成。
对于提供基础能力的插件，通过Service的方式注册，激活的同时，也将自己的能力provide到上下文中方便其他插件调用。
```ts
  //Service类构造函数构造函数
  constructor(protected ctx: Context, name: string) {
    name ??= this.constructor['provide'] as string
    ...
    self.ctx.reflect.provide(name, self, this[symbols.check])
    return self
  }
```
在咱们的例子里，coffee插件工作时，就调用了water和power提供的能力，
```ts
ctx.logger.info('咖啡店开业！供水=' + ctx.water.supply() + ' 供电=' + ctx.power.available() + 'kW')
```

顺便提一下，service类型的plugin，也可以声明依赖，比如我们的power插件，依赖water；
```ts
export class PowerService extends Service {
  static inject = ['water']             // ← Service 子类照样能声明依赖（fiber 级 inject，和对象插件同机制）
  constructor(ctx: Context) {
    super(ctx, 'power')     // ← 占位名，先不挂真名 'power'
    ctx.logger('power').info('供电所建成，但得等通水才挂牌营业')
  }
}
```

`main.ts` 是「楼外 client」——它跑在根 fiber 里，没有 `inject` 门禁，所以要用 `ready()` 等服务上线、用 strict `ctx.get()` 查服务是否还在。
当前的调用顺序如下，
```ts
async function main() {
  const ctx = new Context()
  ctx.logger.exporter({ /* 把秘书处台账接到控制台 */ })

  // [08:30] 挂楼层管理：coffee / bakery 随之登记，但都因依赖未齐而 PENDING
  await ctx.plugin(floorManagerPlugin)

  // [09:00] 挂公用部门：finance 一挂牌，water→power→coffee→bakery 的级联被触发
  await ctx.plugin(PowerService)
  await ctx.plugin(WaterService)
  await ctx.plugin(FinanceService)

  // 楼外 client：按服务名等咖啡店 ACTIVE，不持有 coffee 的 fiber
  const sell = await ready(ctx, 'sell')
  sell(5, 'main ')

  ctx.emit('water/maintenance', '今晚18:00 停水')

  // [14:00] 停水：coffee 同步离开 ACTIVE，strict get 当场返回 undefined
  ctx.registry.delete(WaterService)
  try {
    const s = ctx.get('sell', true)          // 同步快照：删水后必为 undefined
    if (!s) throw new Error('coffee shop gone, cannot sell')
    s(2, 'main ')
  } catch (error) {
    console.error('[error] ' + (error as Error).message)
  }

  // [15:00] 复水：等咖啡店重新开业、sell 重新 provide
  await ctx.plugin(WaterService)
  const reopenedSell = await ready(ctx, 'sell')
  reopenedSell(3, 'main ')

  // [18:00] 楼层退租：三楼整层（coffee / bakery / cleaning）级联清退
  ctx.registry.delete(floorManagerPlugin)
}
```

下面这段是上面代码真实跑出的日志（`cd examples/dsh/cordis && npx tsx coffeeshop/main.ts`）。
我们按时间分五拍，逐行说清楚「这一行是谁、因为什么打出来的」。

```shell
[08:00] 大厦开张，供水/供电/财务部还没挂牌
[08:30] 先挂楼层管理 → 咖啡店入驻，但 inject 缺 water/power → PENDING，不开业
  [秘书处] INFO [floor-manager] 楼层管理挂牌（三楼）
[09:00] 依次挂牌供电/供水/财务部
  [秘书处] INFO [water] 供水部门挂牌（大厦公用）
  [秘书处] INFO [power] 通水了，供电部门正式挂牌（大厦公用）
  [秘书处] INFO [finance] 财务部挂牌（长期账本归这里）
  [秘书处] INFO [coffee] 咖啡店开业！供水=自来水 供电=100kW
  [秘书处] INFO [cleaning] 保洁挂牌（瑞迪星自营，随咖啡店退租一并清退）
  [秘书处] INFO [coffee] 咖啡店叫自家保洁：地板已拖净
  [秘书处] INFO [coffee] 咖啡店借楼层会议室：三楼会议室已预订
  [秘书处] INFO [bakery] 面包店开业（三楼兄弟租户）
  [秘书处] INFO [coffee] bakery卖出 2 杯（本班 2 / 全店 2）
  [秘书处] INFO [coffee] main 卖出 5 杯（本班 7 / 全店 7）
[14:00] 供水退租
[error] coffee shop gone, cannot sell
[15:00] 新供水挂牌 → 咖啡店重新走一遍开业流程
  [秘书处] INFO [coffee] 咖啡店停业（本班营业账 7 杯作废；财务部总账仍在）
  [秘书处] INFO [water] 供水部门挂牌（大厦公用）
  [秘书处] INFO [bakery] 面包店撤场（随三楼一并退）
  [秘书处] INFO [cleaning] 保洁撤场（随咖啡店一并退）
  [秘书处] INFO [power] 通水了，供电部门正式挂牌（大厦公用）
  [秘书处] INFO [coffee] 咖啡店开业！供水=自来水 供电=100kW
  [秘书处] INFO [cleaning] 保洁挂牌（瑞迪星自营，随咖啡店退租一并清退）
  [秘书处] INFO [coffee] 咖啡店叫自家保洁：地板已拖净
  [秘书处] INFO [coffee] 咖啡店借楼层会议室：三楼会议室已预订
  [秘书处] INFO [bakery] 面包店开业（三楼兄弟租户）
  [秘书处] INFO [coffee] bakery卖出 2 杯（本班 2 / 全店 9）
  [秘书处] INFO [coffee] main 卖出 3 杯（本班 5 / 全店 12）
财务账本累计（跨停业保留）= 12
[18:00] 楼层管理退租 → 三楼整层清退
  [秘书处] INFO [bakery] 面包店撤场（随三楼一并退）
  [秘书处] INFO [coffee] 咖啡店停业（本班营业账 5 杯作废；财务部总账仍在）
  [秘书处] INFO [cleaning] 保洁撤场（随咖啡店一并退）
```

**第一拍（08:00–08:30）——只有楼层管理开业。**

- `[08:00]` / `[08:30]` 两行是 `main.ts` 自己的 `console.log`，纯剧本旁白。
- `[floor-manager] 楼层管理挂牌` 来自楼层管理 `apply` 里的 `ctx.logger.info`（`floor.ts:34`）。
  楼层管理没有 `inject`，注册即激活，所以它的 `apply` 在 `await ctx.plugin(floorManagerPlugin)` 期间同步跑完。
- 注意这一拍**没有** coffee、bakery、cleaning 的日志。
  floor 的 `apply` 里确实 `ctx.plugin(coffeePlugin)` 和 `ctx.plugin(bakeryPlugin)` 了（`floor.ts:45-46`），但 coffee 缺 `water/power/finance`、bakery 缺 `sell`，两个 fiber 都停在 PENDING，`apply` 根本不执行。

**第二拍（09:00）——公用部门挂牌，级联把整栋楼叫醒。**

- `[09:00] 依次挂牌…` 是 main 的旁白。
- `[water] 供水部门挂牌` 是 `WaterService` 构造函数里的日志（`services.ts:25`）。
  `super(ctx, 'water')` 这一句把供水以 `'water'` 之名 provide 到根上下文。
- `[power] 通水了，供电部门正式挂牌` 是 `PowerService` 构造函数（`services.ts:41`）。
  供电声明了 `static inject = ['water']`（`services.ts:37`），所以它在供水挂牌前一直 PENDING；水一到，它才被调度执行构造函数、`super(ctx, 'power')` 挂牌。
  这一行出现在 water 之后，正是「依赖就绪才激活」的可见证据。
- `[finance] 财务部挂牌` 是 `FinanceService` 构造函数（`services.ts:55`）。
  财务部没有 `inject`，注册即挂牌。
- `[coffee] 咖啡店开业！` 是 coffee `apply` 的第一行（`coffee.ts:22`）。
  它要等的三个依赖 `water/power/finance` 到这一刻才全部 ACTIVE，楼管 `notify` 把它从 PENDING 翻成 ACTIVE，`apply` 才开始跑。
  它能直接读到 `ctx.water.supply()` 和 `ctx.power.available()`，是因为 `inject` 门禁向它保证了这两个服务在。
- `[cleaning] 保洁挂牌` 来自 `CleaningService` 构造函数（`cleaning.ts:17`）。
  它不是 main 注册的，而是 coffee 在 `apply` 里 `await ctx.plugin(CleaningService)`（`coffee.ts:27`）注册的，所以挂在 coffee 名下。
- `[coffee] 咖啡店叫自家保洁：地板已拖净` 和 `[coffee] 借楼层会议室：…` 是 coffee `apply` 从 cleaning 的 `await` 恢复后继续打的日志（`coffee.ts:35-36`）。
  前者用 `ctx.get('cleaning')` 取自营保洁，后者沿父链读到楼层管理 provide 的 `meetingRoom`。
- 这里有个关键时序：`await ctx.plugin(CleaningService)` 让 coffee 的 `apply` 让出了一次微任务，所以真正的 `ctx.provide('sell', sell)`（`coffee.ts:40`）要更晚才执行。
  这也是为什么楼外的 main **不能**在挂完 finance 后立刻同步读 `sell`——它必须 `await ready(ctx, 'sell')`。
- `[bakery] 面包店开业` 是 bakery `apply`（`floor.ts:25`）。
  bakery 声明了 `inject: ['sell']`（`floor.ts:23`），所以它的激活被排在 coffee 之后：coffee provide 出 `sell`、fiber 翻成 ACTIVE，楼管通知 bakery，它才开业。
- `[coffee] bakery卖出 2 杯` 是 bakery 在 `apply` 里调 `ctx.sell(2, 'bakery')`（`floor.ts:26`）打进 coffee 的日志。
  卖咖啡的函数是 coffee 定义的，所以日志名是 `[coffee]`；`本班 2 / 全店 2` 里「本班」是 coffee 这次开业的局部计数、「全店」是根上财务部的累计。
- `[coffee] main 卖出 5 杯` 是 main 的 `ready(ctx,'sell')` resolve 后调 `sell(5, 'main ')`（`main.ts:36-37`）打进的。
  `本班 7 / 全店 7` 说明 bakery 的 2 杯和 main 的 5 杯记在同一本账上。

**第三拍（14:00）——停水，咖啡店同步离场。**

- `[14:00] 供水退租` 是 main 的旁白，紧接 `ctx.registry.delete(WaterService)`（`main.ts:45`）。
- `[error] coffee shop gone, cannot sell` 是 main 的 catch 打出来的（`main.ts:60`）。
  删水后 coffee 的 fiber 在**同一个调用栈内**就被切出 ACTIVE（epoch 变 INACTIVE），所以 strict `ctx.get('sell', true)` 当场返回 `undefined`，main 主动抛错并接住。
  这里不能用 `await ready()`——`ready` 是「等服务上线」，水刚删还没挂回来时它要么挂起、要么（不做 strict 复查时）把正在卸载的旧闭包当有效值返回。
- 注意这一拍**看不到**「咖啡店停业」「保洁撤场」。
  那些是卸载 disposer，跑在 `delete` 之后的微任务里；而 main 从 `delete` 到 15:00 之间没有任何 `await`，微任务一直没机会 flush，所以这些清理日志被推迟到了下一拍。

**第四拍（15:00）——复水，旧店收尾与新店开业交织。**

- `[15:00] 新供水挂牌…` 是 main 的旁白；紧接着 `await ctx.plugin(WaterService)`（`main.ts:63`）让出微任务队列，上一拍积攒的卸载 disposer 终于执行。
- `[coffee] 咖啡店停业（本班营业账 7 杯作废…）` 是上一轮 coffee `apply` 返回的清理函数（`coffee.ts:43`）。
  它在这一刻才跑，正好解释了「本班 7」——上一轮开业累计的 7 杯（bakery 2 + main 5）随旧店作废；财务部总账不受影响。
- `[water] 供水部门挂牌` 是新供水的构造日志。
- `[bakery] 面包店撤场` / `[cleaning] 保洁撤场` 是上一轮 bakery、cleaning 的撤场 effect（`floor.ts:27`、`cleaning.ts`），同样是被推迟到这一拍才 flush。
- `[power] 通水了…` 是新供水到达后，供电重新激活的构造日志。
- 之后 `[coffee] 咖啡店开业` → `[cleaning] 保洁挂牌` → `[coffee] 叫保洁/借会议室` → `[bakery] 面包店开业` → `bakery卖出 2 杯` → `main 卖出 3 杯`，和第二拍完全同构，是新一轮开业的完整重演。
  `本班 2 / 全店 9`、`本班 5 / 全店 12` 说明局部账重新从 0 开始（2、5），而财务总账在旧账 7 的基础上继续累计到 9、12。
- `财务账本累计（跨停业保留）= 12` 是 main 最后读 `ctx.finance.balance()`（`main.ts:67`）。
  12 = 第一轮 7 + 第二轮 5（bakery 2 + main 3），证明长期状态挂在根上的财务部、不随门店停业清零。

**第五拍（18:00）——楼层退租，整层级联清退。**

- `[18:00] 楼层退租…` 是 main 最后的旁白，对应 `ctx.registry.delete(floorManagerPlugin)`（`main.ts:71`）。
- `[bakery] 撤场` → `[coffee] 停业（本班 5 杯作废）` → `[cleaning] 撤场` 三行是三楼子树自下而上的 disposer。
  floor 一退，挂在它名下的 coffee、以及 coffee 名下的 cleaning、和 floor 名下的 bakery 全部连带清退——这就是「父级卸载，子树级联销毁」。
  它们的顺序（LIFO + 子先于父）由 fiber 的 disposable 列表保证。

整段日志串起来想说明三件事。
一是**激活顺序由依赖决定，不由注册顺序决定**：power 在 water 之后开业、bakery 在 coffee 之后开业，都是 `inject` 门禁驱动的。
二是**上线异步、下线同步**：开业要跨 `PENDING → LOADING → ACTIVE` 的微任务链，所以楼外要 `await ready`；删依赖时 fiber 当场离场，strict `get` 立刻能查到。
三是**闭包引用脱离生命周期**：main 手里那个旧 `sell` 闭包在停水后理论上仍能调用（它只引用根上的 finance），所以停业后必须重新 `ctx.get` 而不能复用旧引用——这也是第三拍用 strict get 拦截的原因。

### 3. 通知

上一拍日志里反复出现「水一到，供电/咖啡店/面包店依次醒来开业」「水一退，整串依次停业」。
驱动这一切的是楼管 `reflect` 的**通知机制**：任何服务挂牌或摘牌，`reflect` 都遍历整棵树，找到 `inject` 了这个名字的 fiber，让它重新核对依赖、决定自己开业还是停业。

**通知发起点是 `provide` / 卸载。**
`provide(name, value)` 把 `impl = { name, value, fiber }` 写进 `ReflectService.store` 后，会调 `notify([name])`（`reflect.ts`）。
服务卸载（提供方 fiber 退场、impl 从 store 删除）同样触发 `notify([name])`。
在咱们的例子里，供水 `super(ctx, 'water')`、coffee `ctx.provide('sell', sell)`、`registry.delete(WaterService)` 都是通知发起点。

**`notify` 只叫醒「inject 了这个名字」的 fiber。**
它遍历 `registry` 里所有 runtime 的 fibers，对每个 fiber 检查 `name in fiber.inject`，命中才处理，不命中直接跳过（`reflect.ts` 的 `notify`）。
所以挂供水只会唤醒 inject 了 `water` 的供电和 coffee，不会惊动面包店；挂 `sell` 只会唤醒 inject 了 `sell` 的 bakery。

**被叫醒的 fiber 做两件事：`_checkImpl` 抄账，`_refresh` 重算状态。**

- `_checkImpl(name)`：消费者去总账 `reflect.store` 查这条依赖，查得到（且提供方 ACTIVE）就把这条 `impl` 抄进自己私有的 `fiber._store`，查不到就从 `_store` 删掉（`fiber.ts`）。
  它只是「查总账 → 写/清自己这本账」的刷新动作，**绝不调用 `provide`**——`provide` 永远只在插件 `apply` 里由开发者写。
- `_refresh()`：只读自己的 `_store`，遍历 `inject` 逐项核对，全部齐了算出一个非空 `epoch`，缺任何一项就是 `INACTIVE`（`fiber.ts`）。
- `_setEpoch()` 做真正的状态跃迁：`INACTIVE → 就绪` 就 `_reload()`（跑 `apply` 开业），`就绪 → INACTIVE` 就 `_unload()`（跑 disposer 撤场）。
  这对应故事里「开不开店由店长自己定」——`reflect` 只负责通知，决策和执行都在 fiber 自己手里。

**用第二拍的级联把这条链走一遍。**

1. `WaterService` 构造 → `super(ctx,'water')` → store 有了 `water` → `notify(['water'])`。
2. `notify` 发现供电和 coffee 都 inject 了 `water`，对它们调 `_checkImpl('water')` + `_refresh()`。
   供电只缺 water，这一项抄进 `_store` 后 epoch 非空，于是 `_reload()` → 构造 `PowerService` → 挂牌 `power` → 又 `notify(['power'])`。
3. `notify(['power'])` 叫醒 coffee（coffee 也 inject 了 `power`）；此时 finance 也已挂牌，coffee 的 `water/power/finance` 全齐，`_reload()` 跑 coffee 的 `apply`。
4. coffee `apply` 里 `provide('sell', sell)` → `notify(['sell'])` → 叫醒 inject 了 `sell` 的 bakery → bakery `_reload()` 开业。

这就是为什么日志里 water → power → coffee → bakery 严格按依赖链依次出现，即使它们在代码里的注册顺序是 floor 先把 coffee、bakery 都登记了。

**`internal/service` 事件是给「树外观察者」的，不是级联驱动力。**
`notify` 末尾会 `emit('internal/service', name, value)`（`reflect.ts`），但 fiber 之间的级联唤醒在这之前已经由 `notify` 直接调 `_refresh` 完成了。
这个事件的真正消费者是楼外代码——比如咱们的 `ready()` 助手（`ready.ts`）就靠监听它来「等某个服务上线」，SDK/测试工具也用它观测服务变化。
把这两件事分开很重要：**级联靠 `notify` 直接调 fiber，事件只是旁路通知。**

**下线是同一条链反向走。**
`registry.delete(WaterService)` 让 water 的 impl 离开 ACTIVE → `notify(['water'])` → 供电、coffee 的 `_checkImpl('water')` 查不到 → `_store` 清掉该项 → `_refresh` 算出 `INACTIVE` → `_unload()`。
coffee 卸载又使 `sell` 消失 → `notify(['sell'])` → bakery 跟着 `_unload()`；cleaning 作为 coffee 的子 fiber 随父级回收。
状态翻转在 `delete` 的同步调用栈内就完成了（所以 strict `get` 当场返回 `undefined`），但 disposer 函数体是 `async` 的，真正打日志要等后续微任务——这正是第三拍看不到撤场日志、第四拍才看到的原因。

> 同一个插件模块可以在不同 `Context` 下挂载多次，每次都有**独立的 Fiber**——「Fiber 是运行实例而非插件定义本身」。`notify` 遍历的是这些运行中的 fiber，不是插件定义。



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




**形态 C —— 函数插件**
还有一种更简洁的函数插件形式，你只需要实现你的apply函数，声明依赖即可;
>不过在咱们的样例里并没有使用他

```ts
export const name = 'coffee'
export function apply(ctx: Context) {
    console.log('hello')
  }
```