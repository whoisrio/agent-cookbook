// 示例 05：serial 与 bail（命中即停）
//
// 对应讨论点：
//  - serial / bail 都是「按注册顺序逐个跑，遇到第一个命中值就停并返回」。
//  - 区别：serial 会 await 每个监听器（支持异步）；bail 同步（不 await）。
//  - 命中判定：返回 null / false / undefined 算「放行」，其他任何值算「命中/拦截」。

import { Context } from '@deepseek-ai/cordis'

declare module '@deepseek-ai/cordis' {
  interface Events {
    'tool/resolve'(req: string): string | null
  }
}

const ctx = new Context()

// A：先注册（更靠前）
ctx.on('tool/resolve', (req) => {
  console.log('A 看到', req)
  return null // 放行：null 不算命中
})

// B：后注册（更靠内）
ctx.on('tool/resolve', (req) => {
  if (req === 'secret') return 'BLOCKED' // 命中：非空 → 拦截并停
  return null
})

async function main() {
  console.log('--- serial ---')
  console.log('serial(secret) =>', await ctx.serial('tool/resolve', 'secret')) // BLOCKED（B 命中，A 已先跑）
  console.log('serial(normal) =>', await ctx.serial('tool/resolve', 'normal'))  // undefined（都放行）

  // bail 行为一致，但它是同步的（监听器别返回 Promise 才安全）
  console.log('--- bail ---')
  console.log('bail(secret) =>', ctx.bail('tool/resolve', 'secret')) // BLOCKED
  console.log('bail(normal) =>', ctx.bail('tool/resolve', 'normal')) // undefined
}

main()
