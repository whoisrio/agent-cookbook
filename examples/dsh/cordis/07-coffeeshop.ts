// 示例 07：孔太斯大楼与星瑞迪咖啡（逐拍还原 docs/dsh-extension.md §1.1）
//
// 角色对照（故事 → cordis 真名）：
//   孔太斯大楼         → Context（new Context()）
//   星瑞迪咖啡（插件）  → 对象式插件 ctx.plugin({ name:'coffee', inject:[...] })
//   开店条件           → inject：['water','power']（供水 + 供电，两项公用事业）
//   供水处 / 供电处     → Service 子类：牌子挂在 this 上（super(ctx,'key') 即挂牌）
//   财务部             → 楼级常设 Service：挂在根上，咖啡店不靠它开业，但账目交到它手里
//   招商引资办 + 名册   → Registry（ctx.plugin 的受理窗口 + Runtime 集合）
//   楼管               → reflect（Proxy handler：接线 + notify 挨个重查）
//   店长               → Fiber（每份入驻协议一份；开业条件写在协议上）
//   秘书处             → logger（开盘即驻；默认只写楼内 ring buffer，要到控制台得外接 exporter）
//   广播系统           → emit / parallel / serial·bail / waterfall

import { Context, Service } from '@deepseek-ai/cordis'

// ---- 类型扩充：服务挂到 ctx 上 + 广播频道（只描述频道名与参数）----
declare module '@deepseek-ai/cordis' {
  interface Context {
    water: WaterService
    power: PowerService
    finance: FinanceService
    sell: (cups: number) => void
  }
  interface Events {
    // emit：单向广播，发完不管（供水部门通知今晚停水）
    'water/maintenance'(message: string): void
    // parallel：广播 + 等全员回执（大楼发涨价公告，要求每家确认）
    'notice/price-hike'(percent: number): void
    // serial / bail：首个应答者拍板（巡逻员发现某层灯还亮着，第一个上报即处理）
    'security/light-on'(floor: number): string | null
    // waterfall：层层流转单（临时增容用电申请，管理处可否决/加管理费，供电科兜底）
    'power/request'(req: { floor: number; kw: number }, next: () => string): string
  }
}

// ---- 两个「公用事业部门 / 服务」：构造时 super(ctx,'key') 即挂牌 ----
class WaterService extends Service {
  constructor(ctx: Context) {
    super(ctx, 'water')
    ctx.logger('water').info('供水部门挂牌')
  }
  supply(): string {
    return '自来水'
  }
}

class PowerService extends Service {
  constructor(ctx: Context) {
    super(ctx, 'power')
    ctx.logger('power').info('供电部门挂牌')
  }
  available(): number {
    return 100 // 可用负荷 kW
  }
}

// 财务部：楼级常设部门（挂在根上），保管长期账本。
// 它不是咖啡店的开业条件（inject 里没有它），咖啡店只是「借用」根上提供的它——
// 这样停水导致咖啡店停业时，账目仍在，复业后继续累计（演示"长期状态不随门店停业清零"）。
class FinanceService extends Service {
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

// ---- 主角：星瑞迪咖啡（一个对象式插件，写明开业条件）----
const coffeePlugin = {
  name: 'coffee',
  inject: ['water', 'power'] as const,
  apply(ctx: Context) {
    // 开业流程：每次依赖就绪都会「重新走一遍」——本地临时状态在此归零
    let shiftNote = 0 // 本班营业账：临时状态，停业即作废
    ctx.logger.info('咖啡店开业！供水=' + ctx.water.supply() + ' 供电=' + ctx.power.available() + 'kW')

    // 订阅广播：供水部门的停水通知（emit，发完不管，订阅方各自应对）
    ctx.on('water/maintenance', (msg) => {
      ctx.logger.info('收到大楼广播：' + msg + ' → 准备提前歇业')
    })

    // 协议附件登记撤场处理（LIFO：后登记的先执行）
    // 忘登记的副作用（裸 setTimeout 等）退租时物业不管——这是大楼最常见的漏水点
    ctx.effect(() => () => ctx.logger.info('撤场：摘下门口的画'))
    ctx.effect(() => () => ctx.logger.info('撤场：停掉订阅的报纸'))

    // 卖一杯：本地记一笔，长期账交给财务部（根上提供的服务）。
    // 咖啡店不把财务部写进 inject（它不是开业条件），所以用 ctx.get('finance')
    // 这个"不要求 inject 的读取"接口直接拿根上的财务部——这正是它设计用途。
    // 注意：ctx 是 Proxy，不能随意赋值属性，必须用 provide 才能挂出去
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

async function main() {
  const ctx = new Context()

  // 秘书处：外接一根控制台出口（默认只写楼内 ring buffer，容量 1000，不外接看不到）
  ctx.logger.exporter({
    export(message) {
      const tag = message.type.toUpperCase().padEnd(4)
      console.log('  [秘书处] ' + tag + ' [' + message.name + '] ' + message.args.join(' '))
    },
  })

  // 招商登记咖啡店（楼外加载方调用 ctx.plugin；店内自己不调）
  const coffee = ctx.plugin(coffeePlugin)
  console.log('[08:00] coffee 已登记，但供水/供电还没挂牌 → 只能在门口等（PENDING）')

  // 10:00 供水、供电、财务部挂牌 → 楼管 notify → 依赖齐了 coffee 自动开业
  // （财务部虽不是开业条件，但先挂好，咖啡店卖一杯时用 ctx.get 借到它）
  await ctx.plugin(WaterService)
  await ctx.plugin(PowerService)
  await ctx.plugin(FinanceService)
  await coffee

  // —— 广播系统：emit（单向，发完不管）——
  // 供水部门作为发起方，向 water/maintenance 频道全网广播
  ctx.emit('water/maintenance', '今晚18:00 停水')

  ctx.sell(3)

  // —— 广播系统：parallel（广播 + 等全员回执）——
  // 两家租户订阅涨价公告频道，各自回执
  ctx.on('notice/price-hike', (pct) => ctx.logger('tenant-a').info('tenant-a 收到涨价 ' + pct + '% 回执'))
  ctx.on('notice/price-hike', (pct) => ctx.logger('tenant-b').info('tenant-b 收到涨价 ' + pct + '% 回执'))
  await ctx.parallel('notice/price-hike', 10) // 等两家都处理完才返回

  // —— 广播系统：serial / bail（首个应答者拍板）——
  // 巡逻员发现某层灯还亮着，第一个上报即处理，其余免谈
  ctx.on('security/light-on', (floor) => {
    if (floor === 3) return '巡逻员甲上报：3 楼灯亮，已处理' // 首位拍板 → 短路
    return null
  })
  ctx.on('security/light-on', (floor) => '巡逻员乙上报：' + floor + ' 楼灯亮') // 不会被调到
  const report = await ctx.serial('security/light-on', 3)
  console.log('  serial 结果（首位拍板）= ' + report)

  // —— 广播系统：waterfall（层层流转单）——
  // 临时增容用电申请：楼层管理处审批（可否决 / 加管理费）→ 供电科兜底
  ctx.on('power/request', (req, next) => {
    if (req.kw > 50) return '管理处否决：超容'
    const r = next()
    return r + ' +管理费10元'
  })
  const decision = ctx.waterfall(
    'power/request',
    { floor: 3, kw: 30 },
    (req) => '供电科批准 ' + req.floor + ' 楼 ' + req.kw + 'kW',
  )
  console.log('  waterfall 结果 = ' + decision)

  // 下午两点：供水退租（摘牌）→ 楼管 notify → coffee 自动停业（财务账本仍在）
  console.log('[14:00] 供水退租')
  ctx.registry.delete(WaterService)

  // 下午三点：新供水挂牌 → coffee 重新走一遍开业流程
  console.log('[15:00] 新供水挂牌')
  await ctx.plugin(WaterService)
  await coffee
  ctx.sell(2)

  // 财务部账本跨停业保留
  console.log('财务账本累计（跨停业保留）= ' + ctx.finance.balance()) // 5

  // 体面退租：撤掉供水 → coffee 再次停业，协议附件 LIFO 清理
  ctx.registry.delete(WaterService)
}

main()
