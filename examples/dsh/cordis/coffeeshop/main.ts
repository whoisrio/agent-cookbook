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

  // —— 大厦开张：挂全楼公用部门 ——
  console.log('[08:00] 大厦开张，供水/供电/财务部还没挂牌')
  await ctx.plugin(WaterService)
  await ctx.plugin(PowerService)
  await ctx.plugin(FinanceService)

  // —— 挂三楼楼层管理：apply 里挂咖啡店 + 面包店，咖啡店又挂自营保洁 ——
  // 结构：根 ─ 供水/供电/财务
  //        └─ 楼层管理（三楼）─ 咖啡店 ─ 保洁（瑞迪星自营）
  //                        └─ 面包店
  console.log('[09:00] 三楼挂楼层管理 → 咖啡店、面包店、自营保洁随之入驻')
  await ctx.plugin(floorManagerPlugin)

  // —— 广播系统：emit（单向，发完不管）——
  // 供水部门作为发起方，向 water/maintenance 频道全网广播
  ctx.emit('water/maintenance', '今晚18:00 停水')

  // 卖 3 杯（咖啡店挂在三楼，但 sell 由它 provide，根上直查总账能取到）
  ctx.sell(3)

  // —— 停业 / 复业：供水依赖驱动 ——
  console.log('[14:00] 供水退租 → 咖啡店自动停业，自营保洁随咖啡店一并撤场')
  ctx.registry.delete(WaterService)

  console.log('[15:00] 新供水挂牌 → 咖啡店重新走一遍开业流程')
  await ctx.plugin(WaterService)
  await ctx.get('coffee') // 等咖啡店重新激活完成（sell 重新挂上）再卖
  ctx.sell(2)

  console.log('财务账本累计（跨停业保留）= ' + ctx.finance.balance())

  // —— 楼层退租：三楼整层级联清退（咖啡店 / 面包店 / 保洁 一并撤场）——
  console.log('[18:00] 楼层管理退租 → 三楼整层清退')
  ctx.registry.delete(floorManagerPlugin)
}

main()
