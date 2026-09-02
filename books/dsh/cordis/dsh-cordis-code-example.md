# DSH · Cordis 代码样例：孔太斯大楼咖啡店

> 可跑示例：`examples/dsh/cordis/coffeeshop/`
> ```bash
> npx tsx coffeeshop/01-dependency-load.ts   # 演示1：依赖驱动激活 + 执行 sell
> npx tsx coffeeshop/02-water-shutdown.ts    # 演示2：供水停 → 依赖它的插件停业
> npx tsx coffeeshop/03-events.ts            # 演示3：集中事件消息 emit/parallel/waterfall/serial
> ```

---

## 这个样例有什么

咱们快速走读一下，样例模拟一栋楼里开咖啡店，角色分两层：

- 大楼级公用服务（Service 插件）：供水 `WaterService`、供电 `PowerService`、财务 `FinanceService`。供电自己依赖供水（`PowerService.inject=['water']`）。
- 业务租户（普通插件）：咖啡店 `coffeePlugin`（依赖 `water/power/finance`）、自营保洁 `CleaningService`（挂在咖啡店名下，随其退租）、面包店 `bakeryPlugin`（依赖咖啡店提供的 `sell`）、冰箱店 `fridgePlugin` 与空调店 `acPlugin`（都只依赖 `power`）。

依赖关系：

```
power   ← water
coffee  ← water, power, finance
cleaning ← coffee
bakery  ← sell(←coffee)
fridge, ac ← power
```

Cordis 在依赖齐了才让插件开业（`ACTIVE`），缺一个就 `PENDING`；依赖消失则自动撤场。样例中特意引入只依赖 `power` 的冰箱店 / 空调店，是为了在演示3用「多个商家」模拟供电部门一条涨价 / 停电通知时，各家怎么各自处理。

---

## 关键代码：effect 与事件订阅

样例在根 `Context` 上挂了一张 `NoticeBoard`（公告牌），把「插件在册 / 注销」变成可 `render()` 的快照——副作用登记一条、事件订阅登记一条；插件卸载时自动撤下。下面两行包裹函数就是实现核心：

```ts
// common.ts —— 副作用登记：登记/注销都发生在 effect 自身生命周期里
export function trackEffect(ctx, owner, label, fn) {
  return ctx.effect(() => {
    ctx.board.add(owner, 'effect', label)   // effect 建立 → 登记
    const dispose = fn()
    return () => {
      if (typeof dispose === 'function') dispose()
      ctx.board.remove(owner, 'effect', label) // effect 清理 → 注销
    }
  }, owner + ': ' + label)
}

// common.ts —— 事件订阅登记：订阅建立时写公告牌，退订时撤下
// 关键：trackEvent 内部就是 ctx.on(eventName, handler) —— 原生的订阅原语
export function trackEvent(ctx, owner, eventName, handler, options?) {
  return ctx.effect(() => {
    ctx.board.add(owner, 'event', eventName)
    const off = ctx.on(eventName, handler, options)  // ← 真正的订阅
    return () => { off(); ctx.board.remove(owner, 'event', eventName) }
  }, owner + ': 订阅 ' + eventName)
}
```

两个函数都把「登记 / 注销」放进 `ctx.effect` 自己的生命周期里：建立时 `add`、插件卸载框架 `dispose` 时 `remove`，**无需手动 `off`**。

插件里就这么用（以咖啡店为例，订阅三个频道、登记两个副作用）：

```ts
// coffee.ts —— 咖啡店订阅三个频道（water/maintenance 用 emit，另两个见演示3）
trackEvent(ctx, 'coffee', 'water/maintenance', (message) =>
  ctx.logger.info('[咖啡店] 收到停水通知：' + message + ' → 提前蓄水'))

trackEvent(ctx, 'coffee', 'power/price-rise', (note, next) => {
  const r = next()
  return r + '；[咖啡店] 每杯转嫁 ¥1'
})

trackEvent(ctx, 'coffee', 'power/outage-vote', (floor) => {
  ctx.logger.info('[咖啡店] 对 ' + floor + ' 楼停电投票：不同意（建议错峰）')
  return '咖啡店：不同意，建议错峰'
})

// 副作用：经过 trackEffect 登记进公告牌，dispose 时自动撤下
trackEffect(ctx, 'coffee', '招牌灯', () => {
  ctx.logger('coffee').info('副作用①：门口招牌灯亮起')
  return () => ctx.logger('coffee').info('撤场：招牌灯已关')
})
```

`effect`（副作用登记）与 `ctx.on`（事件订阅）的底层机制，分别写在 `dsh-cordis-core-mech.md` 的 `## effect & dispose` 与 `## events`，这里只看「样例里怎么写」。

---

## 演示1：依赖驱动激活

**展现什么**：Cordis 的依赖门禁——楼层先挂，咖啡店因缺 `water/power` 停在 `PENDING` 不开业；等供水 / 供电 / 财务挂牌后，依赖链自动级联激活 `coffee → cleaning → bakery → fridge → ac`。最后楼外用 `ready(ctx,'sell')` 等服务就绪卖出，并打印全楼开业后的公告牌（**15 条**）。

```text
===== 演示1：插件依赖关系 + 加载 + 执行 sell =====
[08:00] 大厦开张，供水/供电/财务部还没挂牌
[08:30] 先挂楼层管理 → 咖啡店入驻，但 inject 缺 water/power → PENDING，不开业
  ✅ floor-manager 开业
[09:00] 依次挂牌供电/供水/财务部（级联触发 coffee→cleaning→bakery→fridge→ac 开业）
  ✅ WaterService 开业
  ✅ PowerService 开业
  ✅ fridge 开业   ✅ ac 开业
  ✅ FinanceService 开业
  ✅ CleaningService 开业
  ✅ coffee 开业
  ✅ bakery 开业
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

这张快照就是公告牌的价值：哪家挂了什么副作用、谁订阅了哪个频道，一眼看清。

---

## 演示2：停水级联停业

**展现什么**：依赖门禁的连锁反应——删掉 `WaterService`，因为 `PowerService.inject=['water']`，供电也连带退租，进而把只依赖电力的 `fridge` / `ac` 一起带走。
停水后公告牌清空（**0 条**）；新供水挂牌后咖啡店重开，**财务部账本跨停业保留（累计 12）**——账本是挂在 `finance` 上的全局资源，不随咖啡店退租清零。

```text
  ╔══════════════ 停水前 · 公告牌（15 条） ══════════════
  ║ （同演示1 全楼开业后的 15 条）
  ╚════════════════════════════════════════════════════════

[14:00] 供水退租 → 依赖 water 的咖啡店（及 cleaning / bakery）自动停业

  ⬇️  WaterService 停业清理
  ⬇️  coffee 停业清理
  ⬇️  PowerService 停业清理
  ⬇️  fridge 停业清理
  ⬇️  ac 停业清理
  [秘书处] INFO [coffee] 咖啡店停业（本班营业账 7 杯作废；财务部总账仍在）
  📋 ▼ 注销   [coffee] 副作用 · 行业报纸
  📋 ▼ 注销   [coffee] 副作用 · 招牌灯
  📋 ▼ 注销   [coffee] 订阅 · power/outage-vote
  📋 ▼ 注销   [coffee] 订阅 · power/price-rise
  📋 ▼ 注销   [coffee] 订阅 · water/maintenance
  📋 ▼ 注销   [fridge] 副作用 · 通电待机
  📋 ▼ 注销   [fridge] 订阅 · power/outage-vote
  📋 ▼ 注销   [fridge] 订阅 · power/price-rise
  📋 ▼ 注销   [ac] 副作用 · 通电
  📋 ▼ 注销   [ac] 订阅 · power/outage-vote
  📋 ▼ 注销   [ac] 订阅 · power/price-rise
  📋 ▼ 注销   [bakery] 副作用 · 灯箱
  📋 ▼ 注销   [bakery] 订阅 · water/maintenance
  📋 ▼ 注销   [cleaning] 副作用 · 随咖啡店撤场
  📋 ▼ 注销   [cleaning] 订阅 · water/maintenance
[error] coffee shop gone, cannot sell

  ╔══════════════ 停水后 · 公告牌（0 条） ══════════════
  ║ （空）
  ╚════════════════════════════════════════════════════════

[15:00] 新供水挂牌 → 咖啡店重新开业
  ✅ WaterService 开业   ✅ PowerService 开业
  ✅ fridge 开业   ✅ ac 开业
  ✅ CleaningService 开业   ✅ coffee 开业   ✅ bakery 开业
  [秘书处] INFO [coffee] main 卖出 3 杯（本班 5 / 全店 12）
财务账本累计（跨停业保留）= 12
```

注意删的是 `water`，但级联一路带走 `power → fridge/ac`——这是 Cordis 依赖门禁的真实行为，不是 bug。每条 `📋 ▼ 注销` 都是框架 `dispose` 插件 effect 时自动触发，对应上面 `trackEffect` / `trackEvent` 里写的清理函数。

---

## 演示3：四种事件派发

**展现什么**：同一栋楼里，供水 / 供电如何用四种分派模式发通知——`emit` 单向广播、`parallel` 等全员回执、`waterfall` 逐层转包、`serial` 首个非空即停。频道在 `common.ts` 声明：`water/maintenance`（emit）、`power/price-rise`（waterfall）、`power/outage-vote`（serial）。

发送方代码（节选自 `03-events.ts`）：

```ts
// part1：emit 发完不管 vs parallel 等全员回执
ctx.emit('water/maintenance', '今晚18:00 停水')
await ctx.parallel('water/maintenance', '今晚18:00 停水')

// part2：waterfall 涨价，初始值逐层被依赖 power 的插件包裹
ctx.waterfall('power/price-rise', '基础电费 +10%', (note) => '供电科公告：' + note)

// part3：serial 征求意见，首个非空意见即命中
await ctx.serial('power/outage-vote', 3)
```

执行结果（订阅方就是上面「关键代码」里咖啡店 / 冰箱店的那几段 `trackEvent`）：

```text
  ╔══════════════ 演示3 发事件前 · 公告牌（15 条） ══════════════
  ║ （同演示1 的 15 条：coffee/cleaning/bakery/fridge/ac 各自订阅的频道）
  ╚════════════════════════════════════════════════════════

--- part1-a：emit 停水通知（单向广播，发完不管）---
  [秘书处] INFO [cleaning] [保洁] 收到停水通知：今晚18:00 停水 → 暂停拖地
  [秘书处] INFO [coffee] [咖啡店] 收到停水通知：今晚18:00 停水 → 提前蓄水
  [秘书处] INFO [bakery] [面包店] 收到停水通知：今晚18:00 停水 → 暂停和面
emit 已返回（不等待异步监听器）

--- part1-b：同一通知用 parallel（并发派发，等全员回执才继续）---
  （三家同样收到；parallel 已返回：所有监听器处理完才继续）

--- part2：waterfall 涨价（coffee / fridge / ac 逐层包裹）---
  waterfall 合成结果 = 供电科公告：基础电费 +10%；[咖啡店] 每杯转嫁 ¥1；[空调店] 加收 ¥1.5；[冰箱店] 制冷费转嫁 ¥2

--- part3：serial 停电征求意见（首个非空即返回）---
  [秘书处] INFO [fridge] [冰箱店] 对 3 楼停电投票：不同意（食材会坏）
  serial 返回首个意见 = 冰箱店：不同意，食材会坏 → power 据此进入下一步（执行停电）

--- 收尾：卸载冰箱店，其事件订阅(副作用)随插件自动移除（无需手动 off）---
  ╔══════════════ 卸载冰箱店前 · 公告牌（15 条） ══════════════
  ║ （15 条，含 fridge 的 3 条）
  ╚════════════════════════════════════════════════════════
  ⬇️  fridge 停业清理
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

卸载冰箱店后，它的事件订阅和副作用**随插件自动移除**，无需手动 `off`，公告牌从 15 → 12 条（少 `fridge` 的 3 条）。

---

## 收尾

一份样例走完，三块东西落地了：依赖驱动激活、effect / dispose 是一等公民资源、事件总线四种语义。想继续深挖机制，看 `dsh-cordis-core-mech.md`（运行期机制拆解）与 `dsh-cordis-story-only.md`（故事版）。
