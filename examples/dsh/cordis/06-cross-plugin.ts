// 示例 06：跨插件监听同一事件
//
// 对应讨论点：不同插件监听同名事件，会被合并进同一条队列，不按插件隔离。
// 顺序由「注册时间」决定（先加载的插件，监听器更靠前=更外层）。
// waterfall 模式下，一个插件可以「包裹」或「短路」另一个插件。

import { Context } from '@deepseek-ai/cordis'

declare module '@deepseek-ai/cordis' {
  interface Events {
    'demo/transform'(input: string, next: () => Promise<string>): Promise<string>
  }
}

// 插件 A：先加载 → 它的监听器更外层
function pluginA(ctx: Context) {
  ctx.on('demo/transform', async (input, next) => {
    const r = await next()
    return `[A]${r}`
  })
}

// 插件 B：后加载 → 它的监听器更内层
function pluginB(ctx: Context) {
  ctx.on('demo/transform', async (input, next) => {
    // 若想「抢到最外层」，可改用 ctx.on(name, fn, { prepend: true })
    const r = await next()
    return `<B>${r}>`
  })
}

async function main() {
  const ctx = new Context()
  await ctx.plugin(pluginA)
  await ctx.plugin(pluginB)
  // 链：A(外层) → B(内层) → 兜底('hello')
  const out = await ctx.waterfall('demo/transform', 'hello', async () => 'hello')
  console.log('结果:', out) // [A]<B>hello>

  // 若调换加载顺序，嵌套会反过来：先把 B 先加载试试
  const ctx2 = new Context()
  await ctx2.plugin(pluginB)
  await ctx2.plugin(pluginA)
  const out2 = await ctx2.waterfall('demo/transform', 'hello', async () => 'hello')
  console.log('调换加载顺序后:', out2) // <B>[A]hello>
}

main()
