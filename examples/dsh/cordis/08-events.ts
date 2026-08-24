// 示例 08：消息广播通知（events）——「发通知」的那套机制
//
// 对应讨论点：
//  - cordis 里「通知」有两套独立机制：reflect.notify 管依赖级联（见 §3.1），
//    events 管消息广播（本节）。两者不要混为一谈。
//  - events 是挂到 ctx 上的发布/订阅总线：发送方 emit / parallel / serial / bail / waterfall，
//    接收方 on / once 订阅「频道」（事件名）。
//  - emit 是单向广播、发完不管（不同步等待监听器，也不收返回值）；
//    parallel 是并发派发并 await 所有监听器（大楼要求每家都回执了才继续）。
//  - 通过 ctx.on 注册的监听器归「当前 fiber」所有，fiber 卸载时自动移除——
//    所以插件在 apply 里订阅即可随插件一起清理，无需手动 off。

import { Context } from '@deepseek-ai/cordis'

declare module '@deepseek-ai/cordis' {
  interface Events {
    // 供水检修频道：发送方广播一条通知，订阅方各自应对
    'water/maintenance'(message: string): void
    // 涨价公告：大楼要求每家都回执（用 parallel 等全员）
    'price/notice'(message: string): void
  }
}

const ctx = new Context()

// —— 接收方：两家租户订阅「供水检修」频道（同步监听器，按注册顺序触发）——
ctx.on('water/maintenance', (msg) => {
  console.log('[咖啡店] 收到供水通知：' + msg + ' → 提前蓄水')
})

ctx.on('water/maintenance', (msg) => {
  console.log('[面包店] 收到供水通知：' + msg + ' → 暂停和面')
})

// 异步监听器：emit 不会等它，它的日志要等一个微任务才打出
ctx.on('water/maintenance', async (msg) => {
  await Promise.resolve()
  console.log('[异步租户] 慢半拍才看到：' + msg)
})

// —— 接收方：两家租户订阅「涨价公告」频道，用于 parallel 演示 ——
let ack = 0
ctx.on('price/notice', (msg) => {
  ack++
  console.log('[租户 ' + ack + '] 收到涨价公告：' + msg + ' → 已回执')
})
ctx.on('price/notice', (msg) => {
  ack++
  console.log('[租户 ' + ack + '] 收到涨价公告：' + msg + ' → 已回执')
})

async function main() {
  // 发送方：供水部门单向广播，发完不管
  console.log('--- emit：发完不管（不等待监听器）---')
  ctx.emit('water/maintenance', '今晚 18:00 停水')
  console.log('emit 调用已返回，不会等待上面的异步监听器')

  // 发送方：大楼要求「必须每家都回执了才继续」→ parallel 并发派发并 await 所有监听器
  console.log('\n--- parallel：等全员回执 ---')
  await ctx.parallel('price/notice', '下月水费 +10%')
  console.log('parallel 已返回：每家都处理完才继续')
}

main()
