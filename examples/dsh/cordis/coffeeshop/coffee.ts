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
    sell: (cups: number) => void
  }
}

export const coffeePlugin = {
  name: 'coffee',
  inject: ['water', 'power'] as const,
  async apply(ctx: Context) {
    // 开业流程：每次依赖就绪都会「重新走一遍」——本班营业账在此归零
    let shiftNote = 0
    ctx.logger.info('咖啡店开业！供水=' + ctx.water.supply() + ' 供电=' + ctx.power.available() + 'kW')

    // ① 瑞迪星自营保洁：独立插件，注册在咖啡店名下（子插件）。
    //    注意：查找链只向上、够不到自己挂的「孩子」，所以咖啡店自己要用保洁，
    //    得用 ctx.get 直查总账（跟借财务部同款写法）。
    await ctx.plugin(CleaningService)
    ctx.logger.info('咖啡店叫自家保洁：' + ctx.get('cleaning')!.clean())

    // ② 借楼层的共享会议室：楼层管理 provide 的服务，沿链向上就能命中，不用 inject。
    ctx.logger.info('咖啡店借楼层会议室：' + ctx.meetingRoom.book())

    // 订阅广播：供水部门的停水通知（emit，发完不管，订阅方各自应对）
    ctx.on('water/maintenance', (msg) => {
      ctx.logger.info('收到大楼广播：' + msg + ' → 准备提前歇业')
    })

    // 协议附件登记撤场处理（LIFO：后登记的先执行）
    ctx.effect(() => () => ctx.logger.info('撤场：摘下门口的画'))
    ctx.effect(() => () => ctx.logger.info('撤场：停掉订阅的报纸'))

    // 卖一杯：本地记一笔，长期账交给财务部（ctx.get 借根上常驻的财务部）
    const sell = (cups: number) => {
      shiftNote += cups
      ctx.get('finance')!.record(cups)
      ctx.logger.info('卖出 ' + cups + ' 杯（本班 ' + shiftNote + ' / 全店 ' + ctx.get('finance')!.balance() + '）')
    }
    ctx.provide('sell', sell)

    // 退租清理：依赖消失时这段被调用，从最后一项往回执行
    return () => {
      ctx.logger.info('咖啡店停业（本班营业账 ' + shiftNote + ' 杯作废；财务部总账仍在）')
    }
  },
}
