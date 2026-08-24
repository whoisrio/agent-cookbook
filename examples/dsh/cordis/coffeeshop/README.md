# coffeeshop —— 孔太斯大楼 × 星瑞迪咖啡（三段式演示）

`07-coffeeshop.ts` 是单文件版。这个子目录把同一故事**拆成三个演示**，并把插件加载抽成公共地基 `common.ts`，三个演示共用。

## 层级结构

```
大厦根（root Context）
 ├─ 供水处 WaterService        （全楼公用，根上）
 ├─ 供电处 PowerService        （全楼公用，根上；自身 inject:['water']）
 ├─ 财务部 FinanceService      （楼级常设，账本跨停业保留）
 └─ 楼层管理 floorManagerPlugin  ← 这层就是「三楼」
      ├─ 咖啡店 coffeePlugin（inject:['water','power','finance']）
      │    └─ 保洁 CleaningService（瑞迪星自营，随咖啡店清退）
      ├─ 面包店 bakeryPlugin（inject:['sell']）
      ├─ 冰箱店 fridgePlugin（inject:['power']）   ← 演示3 新增
      └─ 空调店 acPlugin（inject:['power']）        ← 演示3 新增
```

## 公共地基：`common.ts`

- `setupConsole(ctx)`：外接秘书处控制台出口。
- `installLifecycleLog(ctx)`：生命周期日志。订阅 `internal/status`（带 `global`），在插件
  - 加载完成（LOADING→ACTIVE）：打印「✅ XX 开业」；
  - 停业开始（ACTIVE→UNLOADING）：打印「⬇️ XX 停业清理」，
  方便看清级联激活与级联退租。公告牌条目的增删由 `trackEffect` / `trackEvent` 负责，不在这里处理。
- `loadBuilding(ctx)`：分阶段加载整栋楼（演示1 的依赖关系 + 演示2/3 复用）。

## 三个演示

| 脚本 | 演示点 |
|---|---|
| `npm run coffee:1` | **依赖关系 + 加载 + sell**：先挂楼层（咖啡店 PENDING）→ 挂牌 water/power/finance 级联开业；`ready()` 等服务就绪后 `sell` |
| `npm run coffee:2` | **供水停 → 依赖供水的插件停业**：`registry.delete(WaterService)` 触发咖啡店（及 cleaning/bakery）自动停业；公告牌同步注销其副作用/订阅；新供水挂牌后复业，账本跨停业保留 |
| `npm run coffee:3` | **集中 event 消息**：① `emit` 停水（发完不管）vs `parallel` 同通知（等全员回执）；② `waterfall` 涨价（coffee/fridge/ac 逐层包裹）；③ `serial` 停电征求意见（首个非空意见即命中）；收尾卸载冰箱店，公告牌呈现事件订阅随插件自动清退 |

## effect / dispose 为什么「显出来」了

原版里 `effect`/`dispose` 只是混在业务日志里的一行 `logger.info`，不跳眼、也无法证明框架在管。
这里换了一张 `ctx` 持有的公告牌（`NoticeBoard`）：
1. **每个 effect 打标签并登记**：`trackEffect(ctx, owner, label, fn)` 在 effect 建立时往公告牌 `add` 一条、
   dispose 时 `remove` 一条——副作用（亮灯、订报）和事件订阅（监听停电/涨价频道）都变成可查状态；
2. **插件卸载自动清账**：插件 unload 时框架 dispose 其全部 effect，`trackEffect`/`trackEvent` 的清理函数
   自动把对应条目从公告牌撤下，无需手动 `off`，证明订阅随插件自动移除。

## 运行

```bash
cd examples/dsh/cordis
npm install            # 已装可跳过
npm run coffee:1       # 演示1
npm run coffee:2       # 演示2
npm run coffee:3       # 演示3
# 或直接：npx tsx coffeeshop/03-events.ts
```
