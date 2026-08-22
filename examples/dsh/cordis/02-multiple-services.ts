// 示例 02：一个插件提供多个服务
//
// 对应讨论点：框架允许「一个插件提供多个服务」——在 apply 里多次 ctx.provide 即可。
// 只是 harness 的约定偏好「一插件一服务」，但能力本身不受限。

import { Context } from '@deepseek-ai/cordis'

declare module '@deepseek-ai/cordis' {
  interface Context {
    calc: { add(a: number, b: number): number }
    logger: { info(msg: string): void }
  }
}

// 一个函数式插件，一次性把两个能力挂到 ctx 上
function toolbox(ctx: Context) {
  // 服务 1
  ctx.provide('calc', {
    add: (a: number, b: number) => a + b,
  })
  // 服务 2（同一个插件提供的第二个服务）
  ctx.provide('logger', {
    info: (msg: string) => console.log('[log]', msg),
  })
}

async function main() {
  const ctx = new Context()
  await ctx.plugin(toolbox)

  console.log('calc.add(2, 3) =', ctx.calc.add(2, 3)) // 5
  ctx.logger.info('这俩服务来自同一个插件') // [log] 这俩服务来自同一个插件
}

main()
