# DSH · Cordis 总结

一句话：**插件的生死由依赖决定，善后由框架兜底。**

1. **依赖声明（inject）管一切。** 齐了开业，缺了等待，依赖消失自动撤场，不用自己判断。
2. **reflect 只传声，fiber 自己翻牌。** 所以依赖链会级联倒下——删供水，供电、冰箱店全跟着停，这是设计。
3. **effect 成对登记。** 建立时登记，撤场时按 LIFO 自动清理，不用手动 off。
4. **apply 里的状态是临时的。** 要跨停业保留的东西（总账），挂到长命资源上。
5. **五种事件分派由发送方定。** DSH 里：状态广播走 emit、提示词拼装走 waterfall、hook 检查走 serial、输入处理走 bail，parallel 基本不用。
6. **对外服务三件套。** declare 类型 + provide 值 + inject 依赖，缺一不可。
7. **时空都收敛，而且是上面整套机制的结果。** 依赖声明式、reflect 只通知"谁变了"、notify 命中过滤、epoch 缓存状态——改一处只波及依赖链，不做全量重算；emit 发完即走。空间上 effect 成对登记、撤场自动回收。
8. **核心没展开的其他能力（源码里真实存在）。** 插件可运行时加载/卸载（`ctx.plugin` / `registry.delete`），配置有 schema 校验（`Plugin.Config`）且可热更新（`fiber.update` 走 `internal/update` waterfall，源码注释明说这是给 HMR 留的挂点，HMR 本体在外层 loader）；服务可按作用域隔离（`extend` / `isolate` / `intercept`，同一服务在不同 scope 可有不同实现）；`@Inject` 装饰器能把方法调用延迟到依赖就绪再执行。它们和时空收敛是同一套"局部性"原则的延伸——只重载变动的、只回收该回收的，但本质是动态性能力，不是复杂度控制。这几篇都没展开，可另篇深挖。

对应到咱们的story里，大楼=Context、招商办=registry、楼管=reflect、店长=fiber、公告牌=effect/dispose、广播=events。
