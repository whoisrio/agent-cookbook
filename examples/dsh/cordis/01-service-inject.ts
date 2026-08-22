// 示例 01：插件与服务 / 基于「服务名」的 inject
//
// 对应讨论点：
//  - 服务(Services)是挂在 ctx 上的能力；插件(Plugin)是把能力装上 ctx 的执行单元。
//  - inject 声明的是「我需要哪些服务（按 key 名）」，不是「我需要哪个插件对象」。
//  - 一个典型 Service 写法是 `export default class extends Service`，用 super(ctx, key) 发布到 ctx。

import { Context, Service } from '@deepseek-ai/cordis'

// 把 GreeterService 合并进 Context 的类型，获得类型推导
declare module '@deepseek-ai/cordis' {
  interface Context {
    greeter: GreeterService
  }
}

// 提供者：一个插件（Service 子类）把自身发布到 ctx.greeter
export default class GreeterService extends Service {
  constructor(ctx: Context) {
    super(ctx, 'greeter') // 这一步才把实例挂到 ctx.greeter；光写 class 不会注册
  }

  greet(who: string): string {
    return `Hello, ${who}!`
  }
}

// 消费者：通过「服务名」注入，而非引用 GreeterService 这个类/插件对象
function consumer(ctx: Context) {
  // inject 接收的是服务名数组；cordis 等服务就绪后再执行回调
  ctx.inject(['greeter'], (ctx) => {
    // 这里拿到的 ctx 上已经有 greeter
    console.log('[消费者] 调用 greeter 服务：', ctx.greeter.greet('cordis'))
  })
}

async function main() {
  const ctx = new Context()
  await ctx.plugin(GreeterService) // 挂载提供者 → ctx.greeter 出现
  await ctx.plugin(consumer)        // 挂载消费者 → 依赖的 greeter 已就绪，回调执行
}

main()
