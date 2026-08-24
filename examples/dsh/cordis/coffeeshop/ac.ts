// 空调店：三楼租户，只依赖供电（inject:['power']）。
// 演示3 里是「依赖 power 的插件」之一：订阅 power/price-rise（waterfall）与 power/outage-vote（serial）。

import { Context } from '@deepseek-ai/cordis'
import { trackEffect, trackEvent } from './common'

declare module '@deepseek-ai/cordis' {
  interface Context {
    ac: (x: number) => string
  }
}

export const acPlugin = {
  name: 'ac',
  inject: ['power'] as const,
  apply(ctx: Context) {
    ctx.logger.info('空调店开业（只依赖供电）')

    // waterfall：涨价通知，包裹 next() 叠加自己的转嫁说明
    trackEvent(ctx, 'ac', 'power/price-rise', (note, next) => {
      const r = next()
      return r + '；[空调店] 加收 ¥1.5'
    })

    // serial：停电征求意见，返回非空意见（首个即命中）
    trackEvent(ctx, 'ac', 'power/outage-vote', (floor) => {
      ctx.logger('ac').info('[空调店] 对 ' + floor + ' 楼停电投票：不同意（机房过热）')
      return '空调店：不同意，机房过热'
    })

    trackEffect(
      ctx,
      'ac',
      '通电',
      () => {
        ctx.logger('ac').info('副作用：空调通电')
        return () => ctx.logger('ac').info('撤场：空调断电')
      },
    )

    return () => ctx.logger('ac').info('空调店停业')
  },
}
