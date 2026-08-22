// 瑞迪星自营保洁：一个「独立插件」（Service 子类）。
// 它不会自己跑去登记——由星瑞迪咖啡在 apply 里 ctx.plugin(CleaningService) 注册，
// 注册位置决定它是谁的：挂在咖啡店名下 = 品牌自营、随咖啡店退租一并清退。
// 如果注册在根上，就是全楼共享保洁。

import { Context, Service } from '@deepseek-ai/cordis'

// 类型声明跟着提供者走：cleaning 由本文件提供，就在这里声明
declare module '@deepseek-ai/cordis' {
  interface Context {
    cleaning: CleaningService
  }
}

export class CleaningService extends Service {
  constructor(ctx: Context) {
    super(ctx, 'cleaning')
    ctx.logger('cleaning').info('保洁挂牌（瑞迪星自营，随咖啡店退租一并清退）')
    // 登记撤场动作：随咖啡店（乃至三楼整层）退租时执行
    ctx.effect(() => () => ctx.logger('cleaning').info('保洁撤场（随咖啡店一并退）'))
  }
  clean(): string {
    return '地板已拖净'
  }
}
