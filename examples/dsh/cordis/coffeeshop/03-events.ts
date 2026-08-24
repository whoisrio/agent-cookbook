// 演示3：集中 event 消息（emit / parallel / waterfall / serial）
// 运行：cd examples/dsh/cordis && npx tsx coffeeshop/03-events.ts

import { Context } from '@deepseek-ai/cordis'
import { setupConsole, installLifecycleLog, loadBuilding, renderBoard } from './common'
import { ready } from './ready'
import { fridgePlugin } from './fridge'

/** 等一拍，让 cordis 异步 dispose 跑完（公告牌的注销发生在 dispose 过程中）。 */
const settle = () => new Promise((r) => setTimeout(r, 60))

async function main() {
  const ctx = new Context()
  setupConsole(ctx)
  installLifecycleLog(ctx)

  console.log('===== 演示3：集中 event 消息 =====')
  await loadBuilding(ctx)
  const sell = await ready(ctx, 'sell')
  sell(3, 'main ')

  // 发事件前先拍一张公告牌：可见 coffee/cleaning/bakery/fridge/ac 各自订阅了哪些频道
  renderBoard(ctx, '演示3 发事件前 · 公告牌')

  // —— part1：emit（发完不管）vs parallel（等全员回执）——
  console.log('\n--- part1-a：water 发 emit 停水通知（单向广播，发完不管）---')
  ctx.emit('water/maintenance', '今晚18:00 停水')
  console.log('emit 已返回（不等待异步监听器）')

  console.log('\n--- part1-b：同一通知用 parallel 再发一次（并发派发，等全员回执才继续）---')
  await ctx.parallel('water/maintenance', '今晚18:00 停水')
  console.log('parallel 已返回：所有监听器处理完才继续')

  // —— part2：waterfall 涨价（依赖 power 的插件逐层包裹）——
  console.log('\n--- part2：power 发 waterfall 涨价通知（coffee / fridge / ac 逐层包裹）---')
  const rise = ctx.waterfall(
    'power/price-rise',
    '基础电费 +10%',
    (note) => '供电科公告：' + note,
  )
  console.log('  waterfall 合成结果 = ' + rise)

  // —— part3：serial 停电征求意见（首个非空意见即命中）——
  console.log('\n--- part3：power 发 serial 停电征求意见（都表态，首个非空即返回）---')
  const vote = await ctx.serial('power/outage-vote', 3)
  console.log('  serial 返回首个意见 = ' + vote + ' → power 据此进入下一步（执行停电）')

  // —— 收尾：卸载一个 power 依赖插件，其事件订阅（副作用）随插件自动清退 ——
  console.log('\n--- 收尾：卸载冰箱店，其事件订阅(副作用)随插件自动移除（无需手动 off）---')
  renderBoard(ctx, '卸载冰箱店前 · 公告牌')
  ctx.registry.delete(fridgePlugin)
  await settle()
  renderBoard(ctx, '卸载冰箱店后 · 公告牌')
}

main()
