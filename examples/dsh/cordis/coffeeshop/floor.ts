// 楼层管理（容器插件）+ 本层租户：咖啡店、面包店、冰箱店、空调店。
// 楼层管理挂根上，apply 里再 ctx.plugin 挂本层租户，于是咖啡店查找链变成 咖啡店 → 楼层 → 根。

import { Context } from '@deepseek-ai/cordis'
import { coffeePlugin } from './coffee'
import { fridgePlugin } from './fridge'
import { acPlugin } from './ac'
import { trackEffect, trackEvent } from './common'

declare module '@deepseek-ai/cordis' {
  interface Context {
    /** 楼层共享会议室（楼层管理 provide，本层租户沿链可借） */
    meetingRoom: { book(): string }
  }
}

// 面包店：三楼兄弟租户，inject:['sell']（依赖咖啡店）。
const bakeryPlugin = {
  name: 'bakery',
  inject: ['sell'] as const,
  apply(ctx: Context) {
    ctx.logger.info('面包店开业（三楼兄弟租户）')
    ctx.sell(2, 'bakery')

    // 订阅供水检修（演示3 part1）
    trackEvent(ctx, 'bakery', 'water/maintenance', (message) =>
      ctx.logger('bakery').info('[面包店] 收到停水通知：' + message + ' → 暂停和面'),
    )

    // 副作用
    trackEffect(
      ctx,
      'bakery',
      '灯箱',
      () => {
        ctx.logger('bakery').info('副作用：面包店灯箱亮起')
        return () => ctx.logger('bakery').info('面包店撤场（随三楼一并退）')
      },
    )
  },
}

export const floorManagerPlugin = {
  name: 'floor-manager',
  apply(ctx: Context) {
    ctx.logger.info('楼层管理挂牌（三楼）')

    // 楼层共享服务：挂在楼层管理名下，本层租户沿链向上就能借到
    ctx.provide('meetingRoom', {
      book() {
        return '三楼会议室已预订'
      },
    })

    // 挂本层租户（依赖门禁自动决定加载顺序，无需 await）
    ctx.plugin(coffeePlugin)
    ctx.plugin(bakeryPlugin)
    ctx.plugin(fridgePlugin)
    ctx.plugin(acPlugin)
  },
}
