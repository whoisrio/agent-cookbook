// 演示2：供水停 → 依赖供水的插件停业（生命周期看板呈现 effect / dispose）
// 运行：cd examples/dsh/cordis && npx tsx coffeeshop/02-water-shutdown.ts

import { Context } from '@deepseek-ai/cordis'
import { setupConsole, installLifecycleLog, loadBuilding, renderBoard } from './common'
import { ready } from './ready'
import { WaterService } from './services'

/** 等一拍，让 cordis 异步 dispose 跑完（公告牌的注销发生在 dispose 过程中）。 */
const settle = () => new Promise((r) => setTimeout(r, 60))

async function main() {
  const ctx = new Context()
  setupConsole(ctx)
  installLifecycleLog(ctx)

  console.log('===== 演示2：供水停 → 依赖供水的插件停业 =====')
  await loadBuilding(ctx)

  const sell = await ready(ctx, 'sell')
  sell(5, 'main ')

  // 停水前快照
  renderBoard(ctx, '停水前 · 公告牌')

  // —— 供水退租：依赖 water 的咖啡店自动停业；公告牌的服务/副作用/订阅随之注销 ——
  console.log('\n[14:00] 供水退租 → 依赖 water 的咖啡店（及 cleaning / bakery）自动停业')
  ctx.registry.delete(WaterService)
  await settle()

  // 不能复用上面的 sell 闭包——它是上一轮 apply 的残留，停水后 strict get 当场是 undefined
  try {
    const sellAfterShutdown = ctx.get('sell', true)
    if (!sellAfterShutdown) throw new Error('coffee shop gone, cannot sell')
    sellAfterShutdown(2, 'main ')
  } catch (error) {
    console.error('[error] ' + (error as Error).message)
  }

  // 停水后快照：water / floor-manager(含 coffee·cleaning·bakery) 整条链已注销
  renderBoard(ctx, '停水后 · 公告牌')

  // —— 新供水挂牌：咖啡店重新走一遍开业流程 ——
  console.log('\n[15:00] 新供水挂牌 → 咖啡店重新开业')
  await ctx.plugin(WaterService)
  const reopenedSell = await ready(ctx, 'sell')
  reopenedSell(3, 'main ')
  console.log('财务账本累计（跨停业保留）= ' + ctx.finance.balance())
}

main()
