// 全楼公用事业部门：挂在「大厦」根上，任何楼层、任何店铺都能借到。
// 角色：供水处 / 供电处 / 财务部（Service 子类，构造时 super(ctx,'key') 即挂牌）。

import { Context, Service } from '@deepseek-ai/cordis'

// 类型声明跟着提供者走：供水/供电/财务由本文件提供，就在这里声明
// （declare module 全局合并，消费方无需重复声明）
declare module '@deepseek-ai/cordis' {
  interface Context {
    water: WaterService
    power: PowerService
    finance: FinanceService
  }
  interface Events {
    // 供水部门发起的广播频道
    'water/maintenance'(message: string): void
  }
}

export class WaterService extends Service {
  constructor(ctx: Context) {
    super(ctx, 'water')
    ctx.logger('water').info('供水部门挂牌（大厦公用）')
  }
  supply(): string {
    return '自来水'
  }
}

export class PowerService extends Service {
  constructor(ctx: Context) {
    super(ctx, 'power')
    ctx.logger('power').info('供电部门挂牌（大厦公用）')
  }
  available(): number {
    return 100 // 可用负荷 kW
  }
}

// 财务部：楼级常设部门（挂在根上），账本跨停业保留。
// 咖啡店不靠它开业（inject 里没有它），用 ctx.get 借它记账。
export class FinanceService extends Service {
  private totalSold = 0 // 注意：Service 子类不能用字段初始化器，必须在构造函数里赋值
  constructor(ctx: Context) {
    super(ctx, 'finance')
    ctx.logger('finance').info('财务部挂牌（长期账本归这里）')
  }
  record(n: number) {
    this.totalSold += n
  }
  balance() {
    return this.totalSold
  }
}
