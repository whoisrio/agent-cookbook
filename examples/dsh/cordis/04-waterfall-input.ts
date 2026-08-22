// 示例 04：waterfall 中 input 全程恒定
//
// 对应讨论点：next() 不带参数，所有监听器拿到的 input 都是发送事件时传入的原始值。
// waterfall 只能「包裹返回值」，无法「改写下游的 input」——它不是可改写 request 的中间件。
// （源码：next 闭包里 cb(...args)，args 是分发时固定的 [input, next]，next() 不接收参数。）

import { Context } from '@deepseek-ai/cordis'

declare module '@deepseek-ai/cordis' {
  interface Events {
    'demo/echo'(input: string, next: () => Promise<string>): Promise<string>
  }
}

const ctx = new Context()

ctx.on('demo/echo', async (input, next) => {
  // 外层监听器：即便想“改 input”也没有通道——next 不接受参数
  console.log('外层监听器看到的 input =', input)
  const r = await next()
  return r
})

ctx.on('demo/echo', async (input, next) => {
  // 内层监听器：拿到的仍是同一个原始 input
  console.log('内层监听器看到的 input =', input)
  const r = await next()
  return r
})

async function main() {
  const result = await ctx.waterfall('demo/echo', 'ORIGINAL', async (input) => {
    console.log('最内层兜底看到的 input =', input)
    return input
  })
  console.log('最终结果 =', result) // ORIGINAL（没人能改写 input）
}

main()
