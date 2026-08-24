// 瑞迪星自营保洁：独立插件（Service 子类），由咖啡店在 apply 里注册，随咖啡店退租一并清退。

import { Context, Service } from '@deepseek-ai/cordis'
import { trackEffect, trackEvent } from './common'

declare module '@deepseek-ai/cordis' {
  interface Context {
    cleaning: CleaningService
  }
}

export class CleaningService extends Service {
  constructor(ctx: Context) {
    super(ctx, 'cleaning')
    ctx.logger('cleaning').info('保洁挂牌（瑞迪星自营，随咖啡店退租一并清退）')

    // 订阅供水检修：保洁也关心停水（演示3 part1「各个插件的处理」）
    trackEvent(ctx, 'cleaning', 'water/maintenance', (message) =>
      ctx.logger('cleaning').info('[保洁] 收到停水通知：' + message + ' → 暂停拖地'),
    )

    // 副作用：随咖啡店（乃至三楼整层）退租时执行
    trackEffect(
      ctx,
      'cleaning',
      '随咖啡店撤场',
      () => {
        ctx.logger('cleaning').info('副作用：保洁上岗')
        return () => ctx.logger('cleaning').info('保洁撤场（随咖啡店一并退）')
      },
    )
  }
  clean(): string {
    return '地板已拖净'
  }
}
