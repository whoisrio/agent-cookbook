// 冰箱店：三楼租户，只依赖供电（inject:['power']）。
// 演示3 里是「依赖 power 的插件」之一：订阅 power/price-rise（waterfall）与 power/outage-vote（serial）。

import { Context } from '@deepseek-ai/cordis'
import { trackEffect, trackEvent } from './common'

declare module '@deepseek-ai/cordis' {
  interface Context {
    fridge: (x: number) => string
  }
}

export const fridgePlugin = {
  name: 'fridge',
  inject: ['power'] as const,
  apply(ctx: Context) {
    ctx.logger.info('冰箱店开业（只依赖供电）')

    // waterfall：涨价通知，包裹 next() 叠加自己的转嫁说明
    trackEvent(ctx, 'fridge', 'power/price-rise', (note, next) => {
      const r = next()
      return r + '；[冰箱店] 制冷费转嫁 ¥2'
    })

    // serial：停电征求意见，返回非空意见（首个即命中）
    trackEvent(ctx, 'fridge', 'power/outage-vote', (floor) => {
      ctx.logger('fridge').info('[冰箱店] 对 ' + floor + ' 楼停电投票：不同意（食材会坏）')
      return '冰箱店：不同意，食材会坏'
    })

    trackEffect(
      ctx,
      'fridge',
      '通电待机',
      () => {
        ctx.logger('fridge').info('副作用：冰箱通电待机')
        return () => ctx.logger('fridge').info('撤场：冰箱断电')
      },
    )

    return () => ctx.logger('fridge').info('冰箱店停业')
  },
}
