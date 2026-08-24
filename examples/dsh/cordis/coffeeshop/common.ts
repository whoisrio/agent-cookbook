// common.ts —— 三个演示的公共地基
//
// 核心思路：让 effect / dispose 变成一张 ctx 持有的「大楼公告牌（NoticeBoard）」真实对象的可查状态：
//   - 副作用 effect → 公告牌登记一条「副作用」条目；dispose → 注销
//   - 事件订阅 on   → 公告牌登记一条「订阅」条目；退订（含插件卸载自动退订）→ 注销
//
// 增删都发生在 effect 自身生命周期内（trackEffect / trackEvent 包裹），
// 业务动作（亮灯、订报）仍由你自己的 logger 打印——公告牌只负责「可查状态」。
// 插件 unload 时框架自动 dispose 其 effect，条目随之注销，无需手动操作。

import { Context } from '@deepseek-ai/cordis'
import { WaterService, PowerService, FinanceService } from './services'
import { floorManagerPlugin } from './floor'

// 演示3 用到的事件频道
declare module '@deepseek-ai/cordis' {
  interface Events {
    // emit：供水检修单向广播（发完不管）
    'water/maintenance'(message: string): void
    // waterfall：供电涨价通知，依赖 power 的插件逐层包裹 next()
    'power/price-rise'(note: string, next: () => string): string
    // serial：供电停电征求意见，首个非空意见即命中
    'power/outage-vote'(floor: number): string | null
  }
  interface Context {
    /** 大楼公告牌：挂在根上下文，任何插件沿链可读 */
    board: NoticeBoard
  }
}

export type BoardKind = 'effect' | 'event'

const KIND_CN: Record<BoardKind, string> = {
  effect: '副作用',
  event: '订阅',
}

// fiber 生命周期状态（本地常量，避免跨模块 const enum 在 tsx 运行期问题）
const S = { PENDING: 0, LOADING: 1, ACTIVE: 2, FAILED: 3, DISPOSED: 4, UNLOADING: 5 } as const

/** 大楼公告牌：一张真实可查的对象，按 所有者 / 种类 / 标签 记录当前在册条目。 */
export class NoticeBoard {
  private entries = new Map<string, { owner: string; kind: BoardKind; label: string }>()

  private key(owner: string, kind: BoardKind, label: string) {
    return owner + '\x00' + kind + '\x00' + label
  }

  /** 登记一条；重复登记忽略（幂等）。 */
  add(owner: string, kind: BoardKind, label: string) {
    const k = this.key(owner, kind, label)
    if (this.entries.has(k)) return
    this.entries.set(k, { owner, kind, label })
    console.log('  📋 ▲ 登记   [' + owner + '] ' + KIND_CN[kind] + ' · ' + label)
  }

  /** 注销一条；不存在则忽略（幂等）。 */
  remove(owner: string, kind: BoardKind, label: string) {
    const k = this.key(owner, kind, label)
    if (!this.entries.delete(k)) return
    console.log('  📋 ▼ 注销   [' + owner + '] ' + KIND_CN[kind] + ' · ' + label)
  }

  list() {
    return [...this.entries.values()]
  }

  /** 打印整张公告牌当前快照。 */
  render(title = '大楼公告牌') {
    const items = this.list().sort((a, b) =>
      a.owner === b.owner ? a.kind.localeCompare(b.kind) : a.owner.localeCompare(b.owner),
    )
    console.log('\n  ╔══════════════ ' + title + '（' + items.length + ' 条） ══════════════')
    if (!items.length) {
      console.log('  ║ （空）')
    } else {
      for (const it of items) {
        console.log('  ║ [' + it.owner + '] ' + KIND_CN[it.kind] + ' · ' + it.label)
      }
    }
    console.log('  ╚════════════════════════════════════════════════════════\n')
  }
}

/**
 * 副作用登记：登记/注销都发生在 effect 自身生命周期里——effect 建立时 add，dispose 时 remove。
 * 业务动作（亮灯/订报）仍在你传入的 fn 里由自己的 logger 打印——公告牌只负责「可查状态」。
 */
export function trackEffect(ctx: Context, owner: string, label: string, fn: () => void | (() => void)) {
  return ctx.effect(
    () => {
      ctx.board.add(owner, 'effect', label)
      const dispose = fn()
      return () => {
        if (typeof dispose === 'function') dispose()
        ctx.board.remove(owner, 'effect', label)
      }
    },
    owner + ': ' + label,
  )
}

/** 事件订阅登记：订阅建立时写公告牌，退订（含插件卸载自动退订）时撤下——同样在 effect 生命周期内。 */
export function trackEvent(
  ctx: Context,
  owner: string,
  eventName: string,
  handler: (...args: any[]) => any,
  options?: { global?: boolean },
) {
  return ctx.effect(
    () => {
      ctx.board.add(owner, 'event', eventName)
      const off = ctx.on(eventName, handler, options)
      return () => {
        off()
        ctx.board.remove(owner, 'event', eventName)
      }
    },
    owner + ': 订阅 ' + eventName,
  )
}

/** 外接秘书处控制台出口（默认只写楼内 ring buffer，不外接看不到）。 */
export function setupConsole(ctx: Context) {
  ctx.logger.exporter({
    export(message) {
      const tag = message.type.toUpperCase().padEnd(4)
      console.log('  [秘书处] ' + tag + ' [' + message.name + '] ' + message.args.join(' '))
    },
  })
}

/**
 * 生命周期日志：打印插件开业 / 停业标题，方便演示时看清级联激活与级联退租。
 * 公告牌条目的增删不在这里处理——由 trackEffect / trackEvent 的 effect 生命周期负责。
 */
export function installLifecycleLog(ctx: Context) {
  ctx.on(
    'internal/status',
    (fiber: any, old: number) => {
      if (fiber.name === 'root') return
      if (old === S.LOADING && fiber.state === S.ACTIVE) {
        console.log('\n  ✅ ' + fiber.name + ' 开业')
      }
      if (old === S.ACTIVE && fiber.state === S.UNLOADING) {
        console.log('\n  ⬇️  ' + fiber.name + ' 停业清理')
      }
    },
    { global: true },
  )
}

/** 打印一次公告牌快照（demo 用来做前后对比）。 */
export function renderBoard(ctx: Context, title?: string) {
  const board = ctx.get('board') as NoticeBoard | undefined
  if (board) board.render(title)
}

/** 分阶段加载整栋楼：先挂楼层（咖啡店 PENDING），再挂牌供水/供电/财务（级联开业）。 */
export async function loadBuilding(ctx: Context) {
  console.log('[08:00] 大厦开张，供水/供电/财务部还没挂牌')
  ctx.provide('board', new NoticeBoard())
  console.log('[08:30] 先挂楼层管理 → 咖啡店入驻，但 inject 缺 water/power → PENDING，不开业')
  await ctx.plugin(floorManagerPlugin)

  console.log('[09:00] 依次挂牌供电/供水/财务部（级联触发 coffee→cleaning→bakery→fridge→ac 开业）')
  await ctx.plugin(PowerService)
  await ctx.plugin(WaterService)
  await ctx.plugin(FinanceService)
}
