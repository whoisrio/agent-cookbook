// 入口：盖楼 → 挂全楼公用部门（供水/供电/财务）→ 挂三楼楼层管理 → 跑一天。
// 运行：cd examples/cordis && npx tsx coffeeshop/main.ts

import { Context } from '@deepseek-ai/cordis'
import { WaterService, PowerService, FinanceService } from './services'
import { floorManagerPlugin } from './floor'

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

  // —— 挂全楼公用部门：此时咖啡店依赖齐了，自动从 PENDING 翻成 ACTIVE 开业 ——
  // 结构：根 ─ 供水/供电/财务
  //        └─ 楼层管理（三楼）─ 咖啡店 ─ 保洁（瑞迪星自营）
  //                        └─ 面包店
  console.log('[09:00] 供水/供电/财务部挂牌 → 咖啡店自动从 PENDING 开业')
  await ctx.plugin(WaterService)
  await ctx.plugin(PowerService)
  await ctx.plugin(FinanceService)

  // —— 广播系统：emit（单向，发完不管）——
  // 供水部门作为发起方，向 water/maintenance 频道全网广播
  ctx.emit('water/maintenance', '今晚18:00 停水')

  // 卖 3 杯（咖啡店现已开业、sell 已挂上）
  ctx.sell(3)

  // —— 停业 / 复业：供水依赖驱动 ——
  console.log('[14:00] 供水退租 → 咖啡店自动停业，自营保洁随咖啡店一并撤场')
  ctx.registry.delete(WaterService)

  console.log('[15:00] 新供水挂牌 → 咖啡店重新走一遍开业流程')
  await ctx.plugin(WaterService)
  // 等级联（water→power→coffee）走完、sell 重新挂上再加杯。
  // 注意：咖啡店是异步 reload 的，不能直接 ctx.sell(2)——要等 'sell' 真正 provide 的事件。
  await new Promise<void>((resolve) => {
    ctx.on('internal/service', (name: string) => {
      if (name === 'sell') resolve()
    })
  })
  ctx.sell(2)

  console.log('财务账本累计（跨停业保留）= ' + ctx.finance.balance())

  // —— 楼层退租：三楼整层级联清退（咖啡店 / 面包店 / 保洁 一并撤场）——
  console.log('[18:00] 楼层管理退租 → 三楼整层清退')
  ctx.registry.delete(floorManagerPlugin)
}

main()
