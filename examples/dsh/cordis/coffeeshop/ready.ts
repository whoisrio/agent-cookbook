// ready / alive / untilGone：给「插件树之外」的调用方（main 脚本、事件回调等）用的
// 一组服务就绪助手。插件内部靠 inject 门禁；树外的普通代码享受不到，就用这组函数
// 主动查询/等待。底层只用 cordis 的两块原生积木：
//   - ctx.get(name, true)：strict 模式，只认提供方 fiber 已 ACTIVE 的服务；
//   - ctx.on('internal/service', ...)：服务注册/卸载触发的变更事件。

import type { Context } from '@deepseek-ai/cordis'

/**
 * 服务当前是否「可用」：已 provide 且提供方 fiber 处于 ACTIVE（apply 整个跑完）。
 *
 * 这是判断服务死活最可靠的同步入口。注意 root 上下文里直接访问 `ctx.sell` 走的是
 * 非严格查找，在提供者刚被卸载、异步清理还没跑完的短窗口内仍可能返回旧值——
 * alive() 用 strict 模式，不会被这个「尸体还温热」的窗口骗到。
 */
export function alive<K extends string & keyof Context>(ctx: Context, name: K): boolean {
  return ctx.get(name, true) !== undefined
}

/**
 * 等待名为 `name` 的服务可用，resolve 出该服务实例。
 *
 * - 已就绪 → 立即 resolve；
 * - 尚未提供 / 提供方还在 LOADING → 等 `internal/service` 事件，strict 复查通过才 resolve；
 * - 卸载/重载期间事件会多次触发（value 可能还是旧值），所以每次都用 strict get 复查，
 *   不会在「正在下线」的瞬间误 resolve。
 *
 * 若该服务永远不会再被提供，Promise 会一直挂着；需要上限的调用方自行 race 一个超时。
 */
export function ready<K extends string & keyof Context>(
  ctx: Context,
  name: K,
): Promise<Context[K]> {
  const current = ctx.get(name, true)
  if (current !== undefined) return Promise.resolve(current)

  return new Promise<Context[K]>((resolve) => {
    const off = ctx.on('internal/service', (n: string) => {
      if (n !== name) return
      const value = ctx.get(name, true)
      if (value !== undefined) {
        off()
        resolve(value)
      }
    })
  })
}

/**
 * 等待名为 `name` 的服务变为「不可用」（卸载完成或提供方离开 ACTIVE）。
 *
 * - 已不可用 → 立即 resolve；
 * - 仍可用 → 等 `internal/service` 事件，每次 strict 复查，一旦拿不到就 resolve。
 *
 * 用于「楼外代码想确认某个依赖真的退干净了」——例如停水后等咖啡店停业完成。
 * 它只表示「strict 查不到了」，不保证所有异步 disposer 都跑完；
 * 需要等 fiber 彻底销毁的话，await 对应插件的 fiber。
 */
export function untilGone<K extends string & keyof Context>(
  ctx: Context,
  name: K,
): Promise<void> {
  if (ctx.get(name, true) === undefined) return Promise.resolve()

  return new Promise<void>((resolve) => {
    const off = ctx.on('internal/service', (n: string) => {
      if (n !== name) return
      if (ctx.get(name, true) === undefined) {
        off()
        resolve()
      }
    })
  })
}
