// 星瑞迪咖啡：三楼（楼层管理名下）的租户。
// 开业条件 inject：['water','power','finance']（供水 + 供电 + 财务部）。
// 依赖 power，所以也是演示3「依赖 power 的插件」之一（订阅 power/price-rise、power/outage-vote）。

import { Context } from '@deepseek-ai/cordis'
import { CleaningService } from './cleaning'
import { trackEffect, trackEvent } from './common'

declare module '@deepseek-ai/cordis' {
  interface Context {
    sell: (cups: number, seller?: string) => void
  }
}

export const coffeePlugin = {
  name: 'coffee',
  inject: ['water', 'power', 'finance'] as const,
  async apply(ctx: Context) {
    let shiftNote = 0 // 本班营业账：临时状态，每次开业归零，停业即作废
    ctx.logger.info('咖啡店开业！供水=' + ctx.water.supply() + ' 供电=' + ctx.power.available() + 'kW')

    // 自营保洁：挂在咖啡店名下，随咖啡店退租一并清退
    await ctx.plugin(CleaningService)

    const sell = (cups: number, seller: string = 'nobody') => {
      shiftNote += cups
      ctx.get('finance')!.record(cups)
      ctx.logger.info(seller + '卖出 ' + cups + ' 杯（本班 ' + shiftNote + ' / 全店 ' + ctx.get('finance')!.balance() + '）')
    }

    // —— 订阅：供水检修（emit 单向广播，发完不管）——
    trackEvent(ctx, 'coffee', 'water/maintenance', (message) =>
      ctx.logger.info('[咖啡店] 收到停水通知：' + message + ' → 提前蓄水'),
    )

    // —— 订阅：power 涨价（waterfall，逐层包裹 next）——
    trackEvent(ctx, 'coffee', 'power/price-rise', (note, next) => {
      const r = next()
      return r + '；[咖啡店] 每杯转嫁 ¥1'
    })

    // —— 订阅：power 停电征求意见（serial，首个非空意见即命中）——
    trackEvent(ctx, 'coffee', 'power/outage-vote', (floor) => {
      ctx.logger.info('[咖啡店] 对 ' + floor + ' 楼停电投票：不同意（建议错峰）')
      return '咖啡店：不同意，建议错峰'
    })

    ctx.logger.info('咖啡店叫自家保洁：' + ctx.get('cleaning')!.clean())
    ctx.logger.info('咖啡店借楼层会议室：' + ctx.meetingRoom.book())

    // —— 副作用：经过 trackEffect 登记进公告牌，dispose 时自动撤下 ——
    trackEffect(
      ctx,
      'coffee',
      '招牌灯',
      () => {
        ctx.logger('coffee').info('副作用①：门口招牌灯亮起')
        return () => ctx.logger('coffee').info('撤场：招牌灯已关')
      },
    )
    trackEffect(
      ctx,
      'coffee',
      '行业报纸',
      () => {
        ctx.logger('coffee').info('副作用②：订阅行业报纸')
        return () => ctx.logger('coffee').info('撤场：报纸已停')
      },
    )

    ctx.provide('sell', sell)

    // 主清理（dispose）：本插件业务级收尾——本班账作废（财务部总账仍在）
    return () => {
      ctx.logger.info('咖啡店停业（本班营业账 ' + shiftNote + ' 杯作废；财务部总账仍在）')
    }
  },
}
