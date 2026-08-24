# DSH · Cordis 代码样例：孔太斯大楼咖啡店（视频讲解稿）

> 可跑示例：`examples/dsh/cordis/coffeeshop/`
> ```bash
> npx tsx coffeeshop/01-dependency-load.ts   # 演示1：依赖关系 + 加载 + 执行 sell
> npx tsx coffeeshop/02-water-shutdown.ts    # 演示2：供水停 → 依赖它的插件停业
> npx tsx coffeeshop/03-events.ts            # 演示3：集中事件消息 emit/parallel/waterfall/serial
> ```

---

## 一、开场：用真实样例过一遍故事里的内容

（旁白）第二部分我们已经把 Cordis 的核心机制拆完了。下面我们用一份真实可跑的样例，带大家过一遍故事里面的内容——就一个场景：孔太斯大楼里开咖啡店。

先把故事角色和代码对上号：

| 故事里的角色 | 代码里的对应 |
| --- | --- |
| 孔太斯大楼（顶层容器） | `Context` |
| 招商引资办 | `registry`（`ctx.plugin`） |
| 楼管 | `reflect` |
| 店长 | `fiber` |
| 秘书处（写台账） | `logger` |
| 广播系统 | `events`（`ctx.emit` / `ctx.on` …） |
| 大堂实时公告牌 | `NoticeBoard`（上一节故事里那块牌，落地成代码） |

样例里的商家（插件）和它们的依赖：

| 代码 | 故事里的商家 | 依赖 |
| --- | --- | --- |
| `WaterService` / `PowerService` / `FinanceService` | 供水 / 供电 / 财务部（楼级公用） | `power` 依赖 `water` |
| `coffeePlugin`（星瑞迪咖啡） | 主角咖啡店 | `water`, `power`, `finance` |
| `CleaningService` | 咖啡店自营保洁 | 随咖啡店退租 |
| `bakeryPlugin` | 三楼面包店（兄弟租户） | `sell`（由咖啡店提供） |
| `fridgePlugin` / `acPlugin` | 冰箱店 / 空调店（**本次新增**） | 只依赖 `power` |

这里特意新增了 **AC（空调店）和 Fridge（冰箱店）** 两个插件，它们都只依赖电力 `power`。引入它们的目的，是用「多个商家」来模拟一个真实场景：当供电部门发出一条涨价 / 停电通知（`waterfall` 派发）时，每家依赖电力的商家是怎么各自处理的。

---

## 二、公告牌（NoticeBoard）：机制与实现

（旁白）Cordis 的 `effect` / `dispose` 默认只是「注册清理函数 / 执行清理」，框架内部状态你不容易直接看到。我们放一张 `ctx` 持有的公告牌，把「插件在册 / 注销」变成随时可查的状态——副作用登记一条、事件订阅登记一条；插件卸载时自动撤下。

讲清两件事：**它被谁持有、条目怎么登记**——并回答一个常见疑问：为什么不是做个插件、靠监听消息来呈现 effect。

### 2.1 它被谁持有：根 Context 上的全局资源，不是插件

公告牌在 `loadBuilding` 里被挂到根上下文，是全局资源，不是插件：

```ts
// common.ts —— loadBuilding
export async function loadBuilding(ctx: Context) {
  ctx.provide('board', new NoticeBoard())   // 挂到根 Context，任何插件沿链用 ctx.board 读取
  await ctx.plugin(floorManagerPlugin)      // 楼层先挂，咖啡店因缺依赖停在 PENDING
  await ctx.plugin(PowerService)            // 再依次挂牌供电/供水/财务，级联开业
  await ctx.plugin(WaterService)
  await ctx.plugin(FinanceService)
}
```

任何插件不需要经过任何事件中转，直接 `ctx.board.add(...)` / `ctx.board.render()` 就能读写——这正是本样例要演示的「插件对一个全局资源的影响」。

### 2.2 副作用 / 订阅：登记发生在 effect 自身生命周期里

副作用和订阅都用包裹函数登记——**建立时 add，dispose 时 remove，全部在 `ctx.effect` 自己的生命周期内**，业务动作仍由你自己的 `logger` 打印：

```ts
// common.ts —— trackEffect：登记/注销都在 effect 回调里
export function trackEffect(ctx: Context, owner: string, label: string, fn) {
  return ctx.effect(() => {
    ctx.board.add(owner, 'effect', label)     // effect 建立 → 登记
    const dispose = fn()
    return () => {
      if (typeof dispose === 'function') dispose()
      ctx.board.remove(owner, 'effect', label) // effect 清理 → 注销
    }
  }, owner + ': ' + label)
}
```

`trackEvent` 同理：在 `ctx.effect` 里 `ctx.on(...)` 订阅、登记，返回的清理函数里 `off()` + 注销。

> 回答一个常见疑问：**公告牌不是插件，也没有靠监听消息去「渲染」effect。**
> 1. 它是根 Context 上的全局资源，插件用 `ctx.board` 直读；
> 2. 副作用 / 订阅的登记写在 `ctx.effect` 生命周期内（见上），根本不经过消息；
> 3. 插件 unload 时框架自动 dispose 其 effect，`trackEffect` / `trackEvent` 的清理函数就把对应条目撤下——无需手动 `off`。
>
> 所以公告牌只有两类条目：**副作用**和**订阅**，都来自插件自己。全楼开业后是 **15 条**；演示2 停水把依赖水/电的商业全部带走，公告牌清空为 **0 条**；演示3 卸载冰箱店，它的 3 条（1 副作用 + 2 订阅）随插件自动移除，变成 **12 条**。这些数字在下面演示里直接呈现。

---

## 三、演示1：依赖关系 + 加载 + 执行 sell

（旁白）先看加载过程。大家会看到：楼层先挂，咖啡店因为缺 `water`/`power` 停在 `PENDING` 不开业；等供水 / 供电 / 财务挂牌后，**依赖链自动级联激活** coffee → cleaning → bakery → fridge → ac。最后楼外调用方用 `ready(ctx,'sell')` 等服务就绪卖出 5 杯，并打印全楼开业后的公告牌快照（15 条）。

```text
===== 演示1：插件依赖关系 + 加载 + 执行 sell =====
依赖关系：
  water ← power        （供电自己得先通水）
  coffee ← water,power,finance
  cleaning ← coffee    （咖啡店 apply 里注册，随店清退）
  bakery ← sell(←coffee)
  fridge, ac ← power   （演示3 新增的供电依赖租户）
[08:00] 大厦开张，供水/供电/财务部还没挂牌
[08:30] 先挂楼层管理 → 咖啡店入驻，但 inject 缺 water/power → PENDING，不开业
  [秘书处] INFO [floor-manager] 楼层管理挂牌（三楼）
  ✅ floor-manager 开业
[09:00] 依次挂牌供电/供水/财务部（级联触发 coffee→cleaning→bakery→fridge→ac 开业）
  [秘书处] INFO [water] 供水部门挂牌（大厦公用）
  ✅ WaterService 开业
  [秘书处] INFO [power] 通水了，供电部门正式挂牌（大厦公用）
  ✅ PowerService 开业
  [秘书处] INFO [fridge] 冰箱店开业（只依赖供电）
  📋 ▲ 登记   [fridge] 订阅 · power/price-rise
  📋 ▲ 登记   [fridge] 订阅 · power/outage-vote
  📋 ▲ 登记   [fridge] 副作用 · 通电待机
  [秘书处] INFO [fridge] 副作用：冰箱通电待机
  [秘书处] INFO [ac] 空调店开业（只依赖供电）
  📋 ▲ 登记   [ac] 订阅 · power/price-rise
  📋 ▲ 登记   [ac] 订阅 · power/outage-vote
  📋 ▲ 登记   [ac] 副作用 · 通电
  [秘书处] INFO [ac] 副作用：空调通电
  ✅ fridge 开业
  ✅ ac 开业
  [秘书处] INFO [finance] 财务部挂牌（长期账本归这里）
  ✅ FinanceService 开业
  [秘书处] INFO [coffee] 咖啡店开业！供水=自来水 供电=100kW
  [秘书处] INFO [cleaning] 保洁挂牌（瑞迪星自营，随咖啡店退租一并清退）
  📋 ▲ 登记   [cleaning] 订阅 · water/maintenance
  📋 ▲ 登记   [cleaning] 副作用 · 随咖啡店撤场
  [秘书处] INFO [cleaning] 副作用：保洁上岗
  ✅ CleaningService 开业
  📋 ▲ 登记   [coffee] 订阅 · water/maintenance
  📋 ▲ 登记   [coffee] 订阅 · power/price-rise
  📋 ▲ 登记   [coffee] 订阅 · power/outage-vote
  [秘书处] INFO [coffee] 咖啡店叫自家保洁：地板已拖净
  [秘书处] INFO [coffee] 咖啡店借楼层会议室：三楼会议室已预订
  📋 ▲ 登记   [coffee] 副作用 · 招牌灯
  [秘书处] INFO [coffee] 副作用①：门口招牌灯亮起
  📋 ▲ 登记   [coffee] 副作用 · 行业报纸
  [秘书处] INFO [coffee] 副作用②：订阅行业报纸
  ✅ coffee 开业
  [秘书处] INFO [bakery] 面包店开业（三楼兄弟租户）
  [秘书处] INFO [coffee] bakery卖出 2 杯（本班 2 / 全店 2）
  📋 ▲ 登记   [bakery] 订阅 · water/maintenance
  📋 ▲ 登记   [bakery] 副作用 · 灯箱
  [秘书处] INFO [bakery] 副作用：面包店灯箱亮起
  [秘书处] INFO [coffee] main 卖出 5 杯（本班 7 / 全店 7）

  ╔══════════════ 演示1 全楼开业后 · 公告牌（15 条） ══════════════
  ║ [ac] 副作用 · 通电
  ║ [ac] 订阅 · power/price-rise
  ║ [ac] 订阅 · power/outage-vote
  ║ [bakery] 副作用 · 灯箱
  ║ [bakery] 订阅 · water/maintenance
  ║ [cleaning] 副作用 · 随咖啡店撤场
  ║ [cleaning] 订阅 · water/maintenance
  ║ [coffee] 副作用 · 招牌灯
  ║ [coffee] 副作用 · 行业报纸
  ║ [coffee] 订阅 · water/maintenance
  ║ [coffee] 订阅 · power/price-rise
  ║ [coffee] 订阅 · power/outage-vote
  ║ [fridge] 副作用 · 通电待机
  ║ [fridge] 订阅 · power/price-rise
  ║ [fridge] 订阅 · power/outage-vote
  ╚════════════════════════════════════════════════════════
```

这张 15 条快照就是公告牌的价值：哪家挂了什么副作用、谁订阅了哪个频道，一眼看清。

---

## 四、演示2：供水停 → 依赖它的插件停业

（旁白）这个演示要说明**依赖门禁的真实连锁反应**。我们删掉 `WaterService`，看框架怎么自动级联停业。

大家应该看到：咖啡店依赖 `water` 自动停业；又因为 `PowerService.inject=['water']`，**供电也连带退租**，进而把只依赖电力的 `fridge` / `ac` 一起带走。停水后公告牌直接清空（0 条）——所有依赖水 / 电的商业被一并带走。

（开场加载与演示1 完全相同，画面从关键动作 `[14:00]` 切入。）

```text
  ╔══════════════ 停水前 · 公告牌（15 条） ══════════════
  ║ [ac] 副作用 · 通电
  ║ [ac] 订阅 · power/price-rise
  ║ [ac] 订阅 · power/outage-vote
  ║ [bakery] 副作用 · 灯箱
  ║ [bakery] 订阅 · water/maintenance
  ║ [cleaning] 副作用 · 随咖啡店撤场
  ║ [cleaning] 订阅 · water/maintenance
  ║ [coffee] 副作用 · 招牌灯
  ║ [coffee] 副作用 · 行业报纸
  ║ [coffee] 订阅 · water/maintenance
  ║ [coffee] 订阅 · power/price-rise
  ║ [coffee] 订阅 · power/outage-vote
  ║ [fridge] 副作用 · 通电待机
  ║ [fridge] 订阅 · power/price-rise
  ║ [fridge] 订阅 · power/outage-vote
  ╚════════════════════════════════════════════════════════

[14:00] 供水退租 → 依赖 water 的咖啡店（及 cleaning / bakery）自动停业

  ⬇️  WaterService 停业清理
  ⬇️  coffee 停业清理
  ⬇️  PowerService 停业清理
  ⬇️  fridge 停业清理
  ⬇️  ac 停业清理
  [秘书处] INFO [coffee] 咖啡店停业（本班营业账 7 杯作废；财务部总账仍在）
  [秘书处] INFO [coffee] 撤场：报纸已停
  📋 ▼ 注销   [coffee] 副作用 · 行业报纸
  [秘书处] INFO [coffee] 撤场：招牌灯已关
  📋 ▼ 注销   [coffee] 副作用 · 招牌灯
  📋 ▼ 注销   [coffee] 订阅 · power/outage-vote
  📋 ▼ 注销   [coffee] 订阅 · power/price-rise
  📋 ▼ 注销   [coffee] 订阅 · water/maintenance
  ⬇️  CleaningService 停业清理
  [秘书处] INFO [fridge] 冰箱店停业
  [秘书处] INFO [fridge] 撤场：冰箱断电
  📋 ▼ 注销   [fridge] 副作用 · 通电待机
  📋 ▼ 注销   [fridge] 订阅 · power/outage-vote
  📋 ▼ 注销   [fridge] 订阅 · power/price-rise
  [秘书处] INFO [ac] 空调店停业
  [秘书处] INFO [ac] 撤场：空调断电
  📋 ▼ 注销   [ac] 副作用 · 通电
  📋 ▼ 注销   [ac] 订阅 · power/outage-vote
  📋 ▼ 注销   [ac] 订阅 · power/price-rise
  [秘书处] INFO [bakery] 面包店撤场（随三楼一并退）
  📋 ▼ 注销   [bakery] 副作用 · 灯箱
  📋 ▼ 注销   [bakery] 订阅 · water/maintenance
  [秘书处] INFO [cleaning] 保洁撤场（随咖啡店一并退）
  📋 ▼ 注销   [cleaning] 副作用 · 随咖啡店撤场
  📋 ▼ 注销   [cleaning] 订阅 · water/maintenance
[error] coffee shop gone, cannot sell

  ╔══════════════ 停水后 · 公告牌（0 条） ══════════════
  ║ （空）
  ╚════════════════════════════════════════════════════════

[15:00] 新供水挂牌 → 咖啡店重新开业
  [秘书处] INFO [water] 供水部门挂牌（大厦公用）
  ✅ WaterService 开业
  [秘书处] INFO [power] 通水了，供电部门正式挂牌（大厦公用）
  ✅ PowerService 开业
  [秘书处] INFO [coffee] 咖啡店开业！供水=自来水 供电=100kW
  [秘书处] INFO [fridge] 冰箱店开业（只依赖供电）
  📋 ▲ 登记   [fridge] 订阅 · power/price-rise
  📋 ▲ 登记   [fridge] 订阅 · power/outage-vote
  📋 ▲ 登记   [fridge] 副作用 · 通电待机
  [秘书处] INFO [fridge] 副作用：冰箱通电待机
  [秘书处] INFO [ac] 空调店开业（只依赖供电）
  📋 ▲ 登记   [ac] 订阅 · power/price-rise
  📋 ▲ 登记   [ac] 订阅 · power/outage-vote
  📋 ▲ 登记   [ac] 副作用 · 通电
  [秘书处] INFO [ac] 副作用：空调通电
  [秘书处] INFO [cleaning] 保洁挂牌（瑞迪星自营，随咖啡店退租一并清退）
  📋 ▲ 登记   [cleaning] 订阅 · water/maintenance
  📋 ▲ 登记   [cleaning] 副作用 · 随咖啡店撤场
  [秘书处] INFO [cleaning] 副作用：保洁上岗
  ✅ fridge 开业
  ✅ ac 开业
  ✅ CleaningService 开业
  📋 ▲ 登记   [coffee] 订阅 · water/maintenance
  📋 ▲ 登记   [coffee] 订阅 · power/price-rise
  📋 ▲ 登记   [coffee] 订阅 · power/outage-vote
  [秘书处] INFO [coffee] 咖啡店叫自家保洁：地板已拖净
  [秘书处] INFO [coffee] 咖啡店借楼层会议室：三楼会议室已预订
  📋 ▲ 登记   [coffee] 副作用 · 招牌灯
  [秘书处] INFO [coffee] 副作用①：门口招牌灯亮起
  📋 ▲ 登记   [coffee] 副作用 · 行业报纸
  [秘书处] INFO [coffee] 副作用②：订阅行业报纸
  ✅ coffee 开业
  [秘书处] INFO [bakery] 面包店开业（三楼兄弟租户）
  [秘书处] INFO [coffee] bakery卖出 2 杯（本班 2 / 全店 9）
  📋 ▲ 登记   [bakery] 订阅 · water/maintenance
  📋 ▲ 登记   [bakery] 副作用 · 灯箱
  [秘书处] INFO [bakery] 副作用：面包店灯箱亮起
  [秘书处] INFO [coffee] main 卖出 3 杯（本班 5 / 全店 12）
财务账本累计（跨停业保留）= 12
  ✅ bakery 开业
```

注意删的是 `water`，但因为 `power` 依赖 `water`，级联一路带走 `power → fridge/ac`——这是 Cordis 依赖门禁的真实行为，不是 bug。

最后 `[15:00]` 新供水挂牌，咖啡店重新开业，**财务部账本跨停业保留（累计 12）**——这正说明账本是挂在 `finance` 上的全局资源，不随咖啡店退租清零。

---

## 五、演示3：集中事件消息

（旁白）这个演示专门讲「发消息」。涉及发消息，我们逐一介绍每条消息长什么样、对应什么输出。频道集中在 `common.ts` 声明：`water/maintenance`（emit）、`power/price-rise`（waterfall）、`power/outage-vote`（serial）。

发消息前的公告牌快照（15 条）先亮出来，大家能看到 coffee / fridge / ac 各自订阅了哪些频道：

```text
  ╔══════════════ 演示3 发事件前 · 公告牌（15 条） ══════════════
  ║ [ac] 副作用 · 通电
  ║ [ac] 订阅 · power/price-rise
  ║ [ac] 订阅 · power/outage-vote
  ║ [bakery] 副作用 · 灯箱
  ║ [bakery] 订阅 · water/maintenance
  ║ [cleaning] 副作用 · 随咖啡店撤场
  ║ [cleaning] 订阅 · water/maintenance
  ║ [coffee] 副作用 · 招牌灯
  ║ [coffee] 副作用 · 行业报纸
  ║ [coffee] 订阅 · water/maintenance
  ║ [coffee] 订阅 · power/price-rise
  ║ [coffee] 订阅 · power/outage-vote
  ║ [fridge] 副作用 · 通电待机
  ║ [fridge] 订阅 · power/price-rise
  ║ [fridge] 订阅 · power/outage-vote
  ╚════════════════════════════════════════════════════════

--- part1-a：water 发 emit 停水通知（单向广播，发完不管）---
  [秘书处] INFO [cleaning] [保洁] 收到停水通知：今晚18:00 停水 → 暂停拖地
  [秘书处] INFO [coffee] [咖啡店] 收到停水通知：今晚18:00 停水 → 提前蓄水
  [秘书处] INFO [bakery] [面包店] 收到停水通知：今晚18:00 停水 → 暂停和面
emit 已返回（不等待异步监听器）

--- part1-b：同一通知用 parallel 再发一次（并发派发，等全员回执才继续）---
  [秘书处] INFO [cleaning] [保洁] 收到停水通知：今晚18:00 停水 → 暂停拖地
  [秘书处] INFO [coffee] [咖啡店] 收到停水通知：今晚18:00 停水 → 提前蓄水
  [秘书处] INFO [bakery] [面包店] 收到停水通知：今晚18:00 停水 → 暂停和面
  ✅ bakery 开业
parallel 已返回：所有监听器处理完才继续

--- part2：power 发 waterfall 涨价通知（coffee / fridge / ac 逐层包裹）---
  waterfall 合成结果 = 供电科公告：基础电费 +10%；[咖啡店] 每杯转嫁 ¥1；[空调店] 加收 ¥1.5；[冰箱店] 制冷费转嫁 ¥2

--- part3：power 发 serial 停电征求意见（都表态，首个非空即返回）---
  [秘书处] INFO [fridge] [冰箱店] 对 3 楼停电投票：不同意（食材会坏）
  serial 返回首个意见 = 冰箱店：不同意，食材会坏 → power 据此进入下一步（执行停电）

--- 收尾：卸载冰箱店，其事件订阅(副作用)随插件自动移除（无需手动 off）---

  ╔══════════════ 卸载冰箱店前 · 公告牌（15 条） ══════════════
  ║ [ac] 副作用 · 通电
  ║ [ac] 订阅 · power/price-rise
  ║ [ac] 订阅 · power/outage-vote
  ║ [bakery] 副作用 · 灯箱
  ║ [bakery] 订阅 · water/maintenance
  ║ [cleaning] 副作用 · 随咖啡店撤场
  ║ [cleaning] 订阅 · water/maintenance
  ║ [coffee] 副作用 · 招牌灯
  ║ [coffee] 副作用 · 行业报纸
  ║ [coffee] 订阅 · water/maintenance
  ║ [coffee] 订阅 · power/price-rise
  ║ [coffee] 订阅 · power/outage-vote
  ║ [fridge] 副作用 · 通电待机
  ║ [fridge] 订阅 · power/price-rise
  ║ [fridge] 订阅 · power/outage-vote
  ╚════════════════════════════════════════════════════════

  ⬇️  fridge 停业清理
  [秘书处] INFO [fridge] 冰箱店停业
  [秘书处] INFO [fridge] 撤场：冰箱断电
  📋 ▼ 注销   [fridge] 副作用 · 通电待机
  📋 ▼ 注销   [fridge] 订阅 · power/outage-vote
  📋 ▼ 注销   [fridge] 订阅 · power/price-rise

  ╔══════════════ 卸载冰箱店后 · 公告牌（12 条） ══════════════
  ║ [ac] 副作用 · 通电
  ║ [ac] 订阅 · power/price-rise
  ║ [ac] 订阅 · power/outage-vote
  ║ [bakery] 副作用 · 灯箱
  ║ [bakery] 订阅 · water/maintenance
  ║ [cleaning] 副作用 · 随咖啡店撤场
  ║ [cleaning] 订阅 · water/maintenance
  ║ [coffee] 副作用 · 招牌灯
  ║ [coffee] 副作用 · 行业报纸
  ║ [coffee] 订阅 · water/maintenance
  ║ [coffee] 订阅 · power/price-rise
  ║ [coffee] 订阅 · power/outage-vote
  ╚════════════════════════════════════════════════════════
```

收尾卸载冰箱店，它的事件订阅和副作用**随插件自动移除**，无需手动 `off`。公告牌从 15 → 12 条（少了 `fridge` 的 3 条）。

---

## 六、收尾

（旁白）一份样例走完，三块机制就都落地了：依赖驱动激活、provide / dispose 是一等公民资源、事件总线四种语义。而公告牌让我们把「在册 / 注销」变成随时可 `render()` 的快照——这正是故事里那块大堂公告牌，在代码里的样子：插件加载往里加一条、卸载自动移除一条，一眼看清谁在、谁走了。

想继续深挖机制，可对照 `dsh-cordis-core-mech.md`（运行期机制拆解）与 `dsh-cordis-story-only.md`（故事版）。
