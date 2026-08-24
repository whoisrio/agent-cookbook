// 入口：盖楼 → 挂全楼公用部门（供水/供电/财务）→ 挂三楼楼层管理 → 跑一天。
// 运行：cd examples/cordis && npx tsx coffeeshop/main.ts

import { Context } from '@deepseek-ai/cordis'
import { WaterService, PowerService, FinanceService } from './services'
import { floorManagerPlugin } from './floor'
import { ready } from './ready'

async function main() {
  const ctx = new Context()

  // 秘书处：外接一根控制台出口（默认只写楼内 ring buffer，容量 1000，不外接看不到）
  ctx.logger.exporter({
    export(message) {
      const tag = message.type.toUpperCase().padEnd(4)
      console.log('  [秘书处] ' + tag + ' [' + message.name + '] ' + message.args.join(' '))
    },
  })

  // —— 大厦开张：先挂三楼楼层管理（咖啡店随之注册，但此时供水/供电还没挂牌）——
  console.log('[08:00] 大厦开张，供水/供电/财务部还没挂牌')
  console.log('[08:30] 先挂楼层管理 → 咖啡店入驻，但 inject 缺 water/power → PENDING，不开业')
  await ctx.plugin(floorManagerPlugin)

  // —— 挂全楼公用部门：最后一个部门（finance）挂牌后，
  //    water→power→coffee→bakery 的依赖级联被触发，但级联是异步跨微任务的，
  //    await ctx.plugin(FinanceService) 只等 finance 自己激活，不保证咖啡店已经开业。——
  console.log('[09:00] 依次挂牌供电/供水/财务部')
  await ctx.plugin(PowerService)
  await ctx.plugin(WaterService)
  await ctx.plugin(FinanceService)

  // main 跑在 root fiber 里，没有 inject 门禁，是「楼外 client」。
  // 用 ready() 按服务名等咖啡店的能力就绪——只认 'sell' 这个能力，
  // 不关心它在三楼名下、也不需要持有咖啡店的 fiber。
  const sell = await ready(ctx, 'sell')
  sell(5,'main ')

  // —— 广播系统：emit（单向，发完不管）——
  // 供水部门作为发起方，向 water/maintenance 频道全网广播
  ctx.emit('water/maintenance', '今晚18:00 停水')

  // —— 停业 / 复业：供水依赖驱动 ——
  console.log('[14:00] 供水退租')
  ctx.registry.delete(WaterService)

  // 注意两点：
  // 1) 不能复用上面第36行 const sell 那个闭包——它是上一轮 apply 的残留，闭包里只
  //    引用了根上的 finance，停水后照样能调、照样记账。cordis 管的是「通过 ctx 查找
  //    服务」这条路径，管不了已经拿到手的函数引用，所以这里必须重新从 ctx 解析。
  // 2) 这里是「查当下在不在」，不能用 await ready()——ready 是「等未来可用」，水刚删、
  //    还没挂回来时它会一直挂着（最初版不挂，但会 resolve 出正在卸载的旧闭包，更危险）。
  //    strict get 看的是提供方 fiber 状态，delete 同步返回时 fiber 已离开 ACTIVE，
  //    当场就是 undefined。
  try {
    const sellAfterShutdown = ctx.get('sell', true)
    if (!sellAfterShutdown) throw new Error('coffee shop gone, cannot sell')
    sellAfterShutdown(2, 'main ')
  } catch (error) {
    console.error('[error] ' + (error as Error).message)
  }
  console.log('[15:00] 新供水挂牌 → 咖啡店重新走一遍开业流程')
  await ctx.plugin(WaterService)
  // 复业后 sell 是新实例，同样用 ready() 等它重新 provide。
  const reopenedSell = await ready(ctx, 'sell')
  reopenedSell(3,'main ')
  console.log('财务账本累计（跨停业保留）= ' + ctx.finance.balance())

  // —— 楼层退租：三楼整层级联清退（咖啡店 / 面包店 / 保洁 一并撤场）——
  console.log('[18:00] 楼层管理退租 → 三楼整层清退')
  ctx.registry.delete(floorManagerPlugin)
}

main()
