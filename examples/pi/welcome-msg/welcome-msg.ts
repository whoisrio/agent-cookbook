import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

// pi agent extension —— agent 启动时打印一条欢迎语
// 激活方式（二选一）：
//   1) 快速测试： pi -e ./welcome-msg.ts
//   2) 落为项目本地扩展： 把本文件放到 .pi/extensions/welcome-msg.ts（项目根下）
export default function (pi: ExtensionAPI) {
  pi.on("session_start", async (event, ctx) => {
    // 只在 agent 真正“启动”时打招呼；reload / resume / fork 不打扰
    //if (event.reason !== "startup" && event.reason !== "new") return;

    const message = "大哥，又来玩啦";
    console.log(message); // 字面打印到终端
    ctx.ui.notify(message, "info"); // TUI 里弹一条通知，保证看得见
  });
}
