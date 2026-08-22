// 「楼层」在 cordis 里不是一个内置概念，而是注册结构长出来的：
// 楼层管理本身是一个插件（容器），挂在大厦根上，apply 里再 ctx.plugin 挂本层租户。
// 于是咖啡店的查找链变成：咖啡店 → 楼层管理 → 大厦根——中间多出「三楼」这一层。

import { Context, type Fiber } from '@deepseek-ai/cordis'
import { coffeePlugin } from './coffee'

declare module '@deepseek-ai/cordis' {
  interface Context {
    /** 楼层管理代管的咖啡店 fiber：供楼外（main）等它激活/复业完成 */
    coffee: Fiber
    /** 楼层共享会议室（楼层管理 provide，本层租户沿链可借） */
    meetingRoom: { book(): string }
  }
}

// 面包店：三楼的兄弟租户。它想借咖啡店自营的保洁——按查找链
// （面包店 → 楼层管理 → 根）向上爬，cleaning 挂在咖啡店名下、不在链上，所以借不到。
// 这演示「兄弟层不互通」：自下而上查找只会经过自己的父链，够不到旁支。
const bakeryPlugin = {
  name: 'bakery',
  apply(ctx: Context) {
    ctx.logger.info('面包店开业（三楼兄弟租户）')
    try {
      ctx.cleaning.clean()
    } catch (error) {
      ctx.logger.info('面包店想借咖啡店的保洁 → 被拒：' + (error as Error).message)
    }
    // 登记撤场动作：三楼退租时执行
    ctx.effect(() => () => ctx.logger.info('面包店撤场（随三楼一并退）'))
  },
}

export const floorManagerPlugin = {
  name: 'floor-manager',
  async apply(ctx: Context) {
    ctx.logger.info('楼层管理挂牌（三楼）')

    // 楼层共享服务：挂在楼层管理名下，本层租户沿链向上就能借到（父级 provide 对子树可见）
    ctx.provide('meetingRoom', {
      book() {
        return '三楼会议室已预订'
      },
    })

    // 挂本层租户：咖啡店的 fiber 登记在案（provide 出去），
    // 这样楼外的 main 在供水复业后能 await 它，等咖啡店重新激活完成再卖咖啡。
    const coffeeFiber = ctx.plugin(coffeePlugin)
    ctx.provide('coffee', coffeeFiber)
    await coffeeFiber
    await ctx.plugin(bakeryPlugin)
  },
}
