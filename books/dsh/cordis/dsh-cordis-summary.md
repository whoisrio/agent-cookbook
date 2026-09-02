# DSH · Cordis 总结

好，如上内容就是Cordis的核心工作原理，一句话：**插件的生死由依赖决定，善后由框架兜底。**

1. **依赖声明（inject）管一切。** 齐了开业，缺了等待，依赖消失自动撤场，不用自己判断。
2. **reflect 只传声，fiber 自己翻牌。** 所以依赖链会级联倒下——被依赖的服务停止，依赖他的全都停服。
3. **effect 成对登记。** 建立时登记，撤场时按 LIFO 自动清理，不用手动 off。
4. **apply 里的状态是临时的。** 要跨停业保留的东西（总账），挂到长命资源上。
5. **五种事件分派由发送方定。** 不关心返回的广播走 emit(通知)，需要下游级联处理的走 waterfall、只需要下游任意一个订阅者应答走serial/bail，需要下游同步处理并等待所有应答走parallel。
6. **对外服务三件套。** declare 类型 + provide 值 + inject 依赖，缺一不可。
7. **副作用** 空间上 effect 成对登记、撤场自动回收。

对应到咱们的story里，大楼=Context、招商办=registry、楼管=reflect、店长=fiber、公告牌=effect/dispose、广播=events。
Cordis本身还提供了插件的加载，热更新(HMR)，schema校验等等机制，所有的这些能力加起来,才能支撑everything is plugin。 
DeepSeek Harness打造的是一个能给自己长出手脚的平台，短短两周，社区已经贡献了成千上万的插件了。
不过，DeepSeek Harnes本身还是rc版本，高度可扩展和安全稳定可靠同样都很重要。
没有完美的agent，永远有更适合你场景的agent，你觉得呢? 
