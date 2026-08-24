// 星瑞迪咖啡：三楼（楼层管理名下）的租户。
// 开业条件 inject：['water','power']（供水 + 供电，两项公用事业）。
// 它挂在三楼名下，所以查找链是：咖啡店 → 楼层管理 → 大厦根。

import { Context } from '@deepseek-ai/cordis'
import { CleaningService } from './cleaning'

// 类型声明跟着提供者走：coffee 只 declare 自己 provide 的 sell。
// water/power/finance 在 services.ts（提供者），cleaning 在 cleaning.ts，
// meetingRoom 在 floor.ts（楼层管理）——都是全局合并的类型，coffee 消费时直接可用。
declare module '@deepseek-ai/cordis' {
  interface Context {
    sell: (cups: number,seller: string) => void
  }
}

export const coffeePlugin = {
  name: 'coffee',
  inject: ['water', 'power', 'finance'] as const,
  async apply(ctx: Context) {
    let shiftNote = 0 // 本班营业账：临时状态，每次开业归零，停业即作废
    ctx.logger.info('咖啡店开业！供水=' + ctx.water.supply() + ' 供电=' + ctx.power.available() + 'kW')

    // 自营保洁：挂在咖啡店名下，随咖啡店退租一并清退（兄弟租户借不到）。
    // 这里的 await 会让 provide('sell') 推迟到下一个微任务，
    // 正是「楼外 client 不能在挂完部门后立刻同步读 sell」的根因——要靠 ready() 等。
    await ctx.plugin(CleaningService)

    const sell = (cups: number,seller: string = 'nobody') => {
      shiftNote += cups
      ctx.get('finance')!.record(cups)
      ctx.logger.info(seller + '卖出 ' + cups + ' 杯（本班 ' + shiftNote + ' / 全店 ' + ctx.get('finance')!.balance() + '）')
    }

    ctx.logger.info('咖啡店叫自家保洁：' + ctx.get('cleaning')!.clean())
    ctx.logger.info('咖啡店借楼层会议室：' + ctx.meetingRoom.book())

    // provide 是同步登记进 reflect.store，但依赖方（面包店、楼外的 ready() 监听者）
    // 要等这个 fiber 翻成 ACTIVE（apply 整个跑完）才会被 internal/service 唤醒。
    ctx.provide('sell', sell)

    return () => {
      ctx.logger.info('咖啡店停业（本班营业账 ' + shiftNote + ' 杯作废；财务部总账仍在）')
    }
  },
}
