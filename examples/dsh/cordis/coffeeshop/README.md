# coffeeshop —— 多层级的孔太斯大楼演示

07-coffeeshop.ts 是单文件版（咖啡店直接挂根上，只有两级）。这个子目录演示**多层级**：用嵌套注册长出"楼层"。

## 层级结构

```
大厦根（root Context）
 ├─ 供水处 WaterService      （全楼公用，根上）
 ├─ 供电处 PowerService      （全楼公用，根上）
 ├─ 财务部 FinanceService    （楼级常设，账本跨停业保留）
 └─ 楼层管理 floorManagerPlugin  ← 这层就是"三楼"
      ├─ 咖啡店 coffeePlugin（inject: ['water','power']，挂三楼名下）
      │    └─ 保洁 CleaningService（瑞迪星自营，咖啡店 apply 里注册，随店清退）
      └─ 面包店 bakeryPlugin（三楼兄弟租户）
```

## 每个文件

- `services.ts`：全楼公用部门（供水/供电/财务）
- `cleaning.ts`：瑞迪星自营保洁（独立插件，由咖啡店注册）
- `coffee.ts`：星瑞迪咖啡（挂三楼；apply 里注册保洁、借楼层会议室、订阅广播、记财务账）
- `floor.ts`：楼层管理（容器插件，apply 里挂本层租户 + provide 楼层共享会议室）+ 面包店
- `main.ts`：入口，按一天时间线跑

## 演示点

1. **楼层 = 容器插件**：楼层管理挂根上，apply 里 `ctx.plugin(coffeePlugin)`，咖啡店查找链变成 咖啡店 → 楼层管理 → 根。
2. **父级服务对子树可见**：咖啡店不 inject、直接 `ctx.meetingRoom` 借到楼层的共享会议室（沿链向上命中楼层管理）。
3. **兄弟层不互通**：面包店 `ctx.cleaning` 报错（自下而上查找够不到旁支）。
4. **自营保洁独立注册**：CleaningService 是独立插件，挂咖啡店名下，随咖啡店停业一并清退。
5. **依赖驱动启停 + 财务账本跨停业保留**：供水退租 → 咖啡店自动停业；新供水挂牌 → 自动复业，账本累计不丢。
6. **楼层级联清退**：楼层管理退租 → 三楼整层（咖啡店/面包店/保洁）一并撤场。

## 运行

```bash
cd examples/cordis
npx tsx coffeeshop/main.ts
```
