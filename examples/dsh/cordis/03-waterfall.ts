// 示例 03：waterfall 事件（环绕中间件 + 否决）
//
// 对应讨论点：waterfall 让监听器形成「外层包裹内层」的链；
// 调用 next() 执行下游并把返回值传回本层；不调用 next() 直接返回即「否决/短路」。
//
// 事件定义只描述「事件名 + 参数类型」，派发方式(waterfall/emit/serial...)由发送方选定。

import { Context } from '@deepseek-ai/cordis'

declare module '@deepseek-ai/cordis' {
  interface Events {
    'demo/transform'(input: string, next: () => Promise<string>): Promise<string>
  }
}

const ctx = new Context()

// 监听器 1（先注册 → 外层）：包裹下游结果
ctx.on('demo/transform', async (input, next) => {
  const downstream = await next()
  return downstream.toUpperCase()
})


// 监听器 2（后注册 → 内层）：命中 blocked 时直接返回，不调 next → 短路
ctx.on('demo/transform', async (input, next) => {
  if (input.includes('blocked')) return '** blocked **'
  return 'null'//next()
})

ctx.on('demo/transform', async (input, next) => {
  
  return 'only me';
})

async function main() {
  // 第二参数 'blocked words' 是事件的 input；最后一个函数是最内层兜底逻辑
  console.log(await ctx.emit('demo/transform', 'hello', async () => 'hello'))
  // → HELLO（L1 把 'hello' 大写）

  console.log(await ctx.waterfall('demo/transform', 'blocked words', async () => 'blocked words'))
  // → ** BLOCKED **（L2 短路，最内层兜底没机会执行；L1 的外层大写仍生效）
}

main()
