# cordis 概念示例

基于 `@deepseek-ai/cordis@4.0.1`，覆盖我们聊过的所有讨论点。每个文件独立可运行。

## 运行

```bash
cd examples/cordis
npm install          # 已安装可跳过
npm run 01           # 运行单个
npm run all          # 运行全部
# 或直接：node_modules/.bin/tsx 03-waterfall.ts
```

## 文件与对应讨论点

| 文件 | 对应点 |
|---|---|
| `01-service-inject.ts` | 插件 vs 服务；`super(ctx,'key')` 发布服务；`inject` 按**服务名**（非插件对象）消费 |
| `02-multiple-services.ts` | 一个插件用 `ctx.provide` 提供多个服务（框架允许，harness 约定偏好一插件一服务） |
| `03-waterfall.ts` | waterfall 环绕中间件 + 否决：`next()` 包裹返回值；不调 `next()` 直接返回即短路 |
| `04-waterfall-input.ts` | 验证 `next()` 不带参，所有监听器拿到的 `input` 都是发送时的原始值，只能包裹返回值 |
| `05-serial-bail.ts` | `serial`（异步命中即停）与 `bail`（同步命中即停）的 `isBailed` 语义 |
| `06-cross-plugin.ts` | 跨插件监听同名事件合并进同一队列，顺序由加载顺序决定（先加载更外层） |
| `07-coffeeshop.ts` | 实例化 `docs/dsh-extension.md` §1.1「咖啡店」故事：Service/provide 挂牌、inject 开业条件、依赖缺失 pending、移除依赖自动停业并重开、长期账本跨停业保留 |
| `coffeeshop/` | 多层级版（子目录，见其 README）：楼层管理容器插件挂根、apply 里挂咖啡店与面包店；咖啡店再挂自营保洁。演示嵌套注册长出中间层、父级服务对子树可见、兄弟层不互通、楼层级联清退 |

## 关键结论

- **服务是契约/实例，插件是装载它的单元**：`super(ctx, key)` 只「发布」，挂在 `ctx.<key>`；`ctx.plugin(Service)` 才「挂载」并触发实例化。
- **`inject` 指向服务名**（如 `greeter`、`llm`），不是插件对象——依赖通过契约解耦。
- **同一事件名跨插件共享一条队列**，waterfall/serial/bail 下插件互相包裹/拦截；想确定性控制顺序用 `ctx.on(name, fn, { prepend: true })`。
- **waterfall 是环绕中间件**：监听器必须调 `next()` 往下传，不调即打回（veto 短路）；能包裹下游返回值，但 `next()` 不带参数，无法改写下游看到的 `input`。
