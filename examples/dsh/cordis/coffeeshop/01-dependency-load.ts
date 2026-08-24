// 演示1：插件依赖关系 + 各插件加载 + 执行 sell
// 运行：cd examples/dsh/cordis && npx tsx coffeeshop/01-dependency-load.ts

import { Context } from '@deepseek-ai/cordis'
import { setupConsole, installLifecycleLog, loadBuilding, renderBoard } from './common'
import { ready } from './ready'

async function main() {
  const ctx = new Context()
  setupConsole(ctx)
  installLifecycleLog(ctx)

  console.log('===== 演示1：插件依赖关系 + 加载 + 执行 sell =====')
  console.log('依赖关系：')
  console.log('  water ← power        （供电自己得先通水）')
  console.log('  coffee ← water,power,finance')
  console.log('  cleaning ← coffee    （咖啡店 apply 里注册，随店清退）')
  console.log('  bakery ← sell(←coffee)')
  console.log('  fridge, ac ← power   （演示3 新增的供电依赖租户）')

  await loadBuilding(ctx)

  // 楼外 client 用 ready() 等服务就绪（只认 'sell' 这个能力，不管它在谁名下）
  const sell = await ready(ctx, 'sell')
  sell(5, 'main ')

  // 整楼开业后，公告牌一次性快照：服务 provide、副作用、事件订阅都在册
  renderBoard(ctx, '演示1 全楼开业后 · 公告牌')
}

main()
