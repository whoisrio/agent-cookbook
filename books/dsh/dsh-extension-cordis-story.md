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
招商办公室即是`registry`
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

插件通过`registry`提供的`plugin`方法来注册：
```ts
// 注册插件，返回与 fiber 绑定的 PromiseLike（其 .then 委托给 fiber.await()）
const coffee = ctx.plugin({
  name: 'coffee',
  inject: ['water', 'power'],   // 开业条件条款（依赖）
  apply(ctx) { /* 依赖就绪后才执行，即真正的开业 */ },
})

```
注册后，得到的是`fiber`对象，也就是上面故事里的店长，`fiber`是插件注册到`Context`后的真正运行对象；
fiber负责
```ts
// fiber（店长）—— 插件注册到 Context 后的真正运行对象，由 registry 在受理申请时 new 出来
class Fiber {
  // ① 开业条件条款（门禁）：声明需要哪些服务，缺一项 apply 不跑，咖啡店停在 PENDING
  inject: string[]

  // ② 生命周期状态机：靠 epoch + _error + uid 三样算出
  //    DISPOSED(uid 已注销) / FAILED(apply 崩过) / ACTIVE(依赖齐) / PENDING(依赖没齐)
  //    LOADING · UNLOADING 是开业 / 撤场进行中的瞬态
  get state(): FiberState

  // ③ 开业流程本体：就是插件写的 apply，只有依赖就绪（epoch 翻成非 INACTIVE）时
  //    fiber 自己 _reload() → _execute(runtime.callback) 才执行——"依赖齐了才开业"是 fiber 决定的
  runtime: { callback: (ctx, config) => void }

  // ④ 撤场清单：所有 ctx.effect 登记项都收进这里，退租时倒序（LIFO）执行
  _disposables: DisposableList

  // ⑤ 默认的「整店退租单」：ctx.plugin() 在父 fiber 上挂的 effect；
  //    注销时把本店从名册摘掉、_setEpoch(INACTIVE) 通知依赖它的店停业
  dispose(): PromiseLike<void>

  // ⑥ 决策权在 fiber 自己：reflect 通知依赖变了，fiber 读自己的 store 自己翻牌
  //    依赖齐 → _reload 开业；依赖没 → _unload 撤场（reflect 只传声，不喊开业）
  _refresh(): void
  _setEpoch(epoch: string): void
  _reload(): PromiseLike<void>
  _unload(): PromiseLike<void>

  // ⑦ 启动期故障感知：apply 崩了记 _error，await() 把它重抛出来（fail-soft，按店隔离）
  await(): PromiseLike<this>
}
```



而`reflect`（ReflectService）则是楼管——它持有全楼服务的中央花名册（`store`），支撑 `ctx.get` / `ctx.provide`。（`ctx.inject` 其实是招商引资办 RegistryService 的能力，让某店"声明需要某块牌"，不归楼管管。）

```ts
const water = ctx.get('water')          // 依赖就绪后，从楼管花名册取到实现
ctx.provide('water', waterService)      // 把服务挂上楼管的中央花名册
```

楼管最关键的活，是"某块牌变了，挨个通知相关店长"。具体由 `notify(names)` 做，分五步：

- **挨个过名册**：遍历 `registry` 里登记的每一根 fiber（`runtime.fibers`），不漏一家；
- **只敲相关的门**：某店只在"它的 `inject` 清单里列了这项"时才理会（`name in fiber.inject`），不相关的店不惊动；
- **帮店长更新小账本**：对每个相关项调 `fiber._checkImpl(name)`，把最新实现物化进该店自己的 `fiber._store`；
- **让店长自己重算账**：再调 `fiber._refresh()`，店长据此重算 epoch（米齐没齐）；
- **开不开由店长定**：epoch 从 `INACTIVE` 翻成就绪，店长自己 `_reload` 开业；翻回 `INACTIVE`，自己 `_unload` 停业——楼管只通知、不拍板；
- **发广播**：走完再发一条 `internal/service` 广播，凡是盯着这块牌的都能收到。

这套"逐个重查"正是 §1.2 要逐拍重放的自动开业 / 自动停业。

`event`是消息总线，各个插件将自己希望发出的消息注册到总线（`ctx.on` 订阅，底层用 `ctx.effect` 登记，退租自动退订），插件的逻辑执行，常常是靠消息总线在驱动：
```ts
// 订阅频道：检修前通知我一声
ctx.on('water/maintenance', () => { /* ... */ })

// 发消息的几种派发模式
ctx.emit('water/maintenance')    // 发完不管
ctx.parallel('audit', record)     // 并发，等所有监听者 settle
ctx.serial('who-handles', inc)    // 顺序抢答：首个有效答案即停
ctx.waterfall('renovation', plan) // 接力加工：各家按顺序改同一份东西
```
发送消息的方式，可以是 `emit`、`parallel`、`serial`/`bail`、`waterfall` 这几种派发模式（区别详见 §1.1 广播系统段）。




## 结合代码重放一遍插件是如何在cordis里注册的
咱们结合代码，把咖啡店到大楼开店的过程，再走一次；

### 开店申请（Registry / Runtime / Fiber）

星瑞迪到招商引资办（Registry）交申请，对应 `ctx.plugin()`:
```ts
ctx.plugin({ name: 'coffee', inject: ['water', 'power'], apply(ctx) { /* 开业后干什么 */ } })
```

招商办的动作：取入口函数（`Registry.resolve`）→ 名册建档（`Plugin.Runtime`）→ 签发协议（`new Fiber`）。07 里就是 `const coffee = ctx.plugin(coffeePlugin)`（`07·Ln 124`）。两个易混点：套间是 `new Fiber` 构造时现场 `parent.extend({ fiber: this })` 分出来的，不是申请里提交的；`inject` 是协议上的开业条件条款——协议、配置、附件清单都长在 Fiber 自己身上，Fiber 就是协议本身。

### 店长核对资源（reflect / inject）
店长（Fiber）上岗第一件事是拿 `inject` 条款去找楼管（reflect）核对——这是协议主动出击（`_checkImpl` → `reflect._getImpl`），不是楼管代劳。07 里开局只登记了 coffee，供水供电还没挂牌，于是停在 PENDING 不开业（`07·Ln 125` 的 `[08:00]` 日志）。供水/供电挂牌对应 `provide`——Service 子类构造时自动调（`super(ctx, 'water')` 即挂牌），名字唯一、重复当场拒绝，详见 §2.4。

### 楼管挨个重查（notify → epoch → unload/reload）。**
此后谁挂牌、谁摘牌，楼管（reflect）都挨个打电话，对应 `reflect.notify()`：遍历名册里所有 Fiber，凡 `inject` 含该名字的就 `_refresh()`。店长把"当前是哪家供水"记成 epoch；epoch 变了，先 `_unload()` 撤场、再 `_reload()` 重跑 `apply`。07 里：10:00 供水供电挂牌 → coffee 自动开业（`07·Ln 129-132`）；14:00 供水退租（`ctx.registry.delete(WaterService)`，`07·Ln 172`）→ 自动停业；15:00 新供水挂牌 → 重新走一遍开业流程（`07·Ln 176-178`）。"当班营业账作废、营业总账交财务部"对应：闭包状态随重跑丢失，长期状态要放进 Service 实例。

**⑤ 广播系统（event：emit / parallel / serial·bail / waterfall）。**
各店用同一个 `ctx.on('频道名', 回调)` 订阅、只收自己关心的频道，区别只在"发的时候用哪个函数"（07 四种都演示了）：
- `emit`（单向，发完不管）：供水部门 `ctx.emit('water/maintenance', '今晚18:00 停水')`（`07·Ln 136`），咖啡店订阅后自行应对（`07·Ln 85`）。
- `parallel`（广播 + 等全员回执）：`ctx.parallel('notice/price-hike', 10)`（`07·Ln 144`），两家租户各自回执，等两家都处理完才返回。
- `serial` / `bail`（首个应答者拍板）：`ctx.serial('security/light-on', 3)`（`07·Ln 153`），巡逻员甲首位拍板，乙没被调到。
- `waterfall`（层层流转单）：`ctx.waterfall('power/request', ...)`（`07·Ln 163`）——管理处监听先审批（`07·Ln 158`）、供电科兜底；经手人不调 `next()` 即打回（短路）。

**⑥ 退租清理（effect / dispose，LIFO）。**
经营期间每添置一样东西，都要在协议附件（`ctx.effect()` 的 dispose 清单）上登记"撤场时怎么处理"。07 里咖啡店登记了两样——摘画、停报纸（`07·Ln 91-92`），退租时从最后一项往回执行（LIFO）：日志里"停掉报纸"排在"摘下画"之前，因为报纸后登记、先撤。"忘了登记的物业不管"：没包进 `ctx.effect` 的副作用（裸 `setInterval`）在附件上没有条目，退租时自然漏收。

**⑦ 楼层与隔离（extend / isolate / fiber 链查找）。**
楼层是 `ctx.extend({})` 出的子 Context；套间是 `ctx.plugin()` 自动为你 extend 的那一层。"你在店里喊一声'接供水'（`ctx.water`）"触发 Context 这个 Proxy 的 `get`，楼管把调用接到当前实现；"门口已接通的分机"是 `fiber.store`（本 Fiber 直接 provide 的、和 inject 且已解析的依赖），"没有再一层层上楼问"是沿 `fiber.parent` 向上爬，跨独立水表（`isolate`）的楼层会被拦下。07 里咖啡店就在一层、没演示这层，深入见 §2.1、§2.4。

**⑧ 收尾。**
回头看，每一拍都只是一两个方法调用；大厦（Context）替团队做的，是把这些调用登记在案，并在对的时机触发它们。星瑞迪什么电话都没打、什么供水都没盯，却总在正确的时间开业、停业、重新开业、退场——这就是 cordis 里一个插件的一生。写字楼可以退到幕后了——下一节起，五大核心概念逐一登场。


**角色对照。**
- 大厦 → 根 Context（`new Context()`）
- 楼层 → 子 Context（`ctx.extend()`）
- 套间 → 插件的子 Context（`ctx.plugin()` 自动 `extend`，每份协议一间）
- 入驻团队 → 插件实例（`ctx.plugin()` 注册）；分包小组 → 子插件 / 子 Fiber
- 店长（带着入驻协议上岗）→ Fiber；开业条件 → `inject`；协议附件上的撤场登记 → `ctx.effect()` 收集的 dispose
- 部门 / 挂牌 → Service / `ctx.provide()`；门口已接通的分机 → `fiber.store`

注意"团队"和"部门"是两个角色，但常常由同一个对象兼任：Service 子类插件被 `new` 出来时，这个实例**既是插件实例也是服务实现**（牌子挂的就是 `this`）——团队自己就是部门。
不过二者不必然一体：一个函数插件可以在 `apply` 里多次 `ctx.provide()` 挂好几块牌子，也可以像纯消费方那样一块都不挂。
- 招商引资办与名册 → Registry（`Plugin.Runtime` 的集合）
- 楼管 → reflect（`ReflectService`，Context 这个 Proxy 的 handler）
- 独立水表 → `isolate(name)`；楼层统一餐标 → `intercept(name, config)`
- 广播 → `emit` / `parallel`；会签单 → `waterfall`（`serial` / `bail` 是它的变体）
- 秘书处 → logger（`LoggerService`，开盘即驻的内置常设机构；默认只写内部 ring buffer，对外需挂 exporter）
- 财务部（保管长期账目的部门）→ 承载长期状态的 Service 实例（07 里挂在根上，咖啡店不靠它开业、用 `ctx.get` 借它记账）



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