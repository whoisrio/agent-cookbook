/**
 * Baby Agent — createReactAgent 示例。
 */

import { readFileSync } from "fs";
import { resolve, dirname } from "path";
import { fileURLToPath } from "url";
import { ChatOpenAI } from "@langchain/openai";
import { createReactAgent } from "@langchain/langgraph/prebuilt";
import { BaseMessage } from "@langchain/core/messages";
import { allTools } from "./tools.js";

// ---------------------------------------------------------------------------
// .env 配置（从仓库根目录读取，与 Python 版共用）
// ---------------------------------------------------------------------------

const __dirname = dirname(fileURLToPath(import.meta.url));
// baby_agent_ts/src/ → 上三级 = agent-cookbook/（repo 根）
const REPO_ROOT = resolve(__dirname, "../../..");

function loadEnv(): Record<string, string> {
  try {
    const content = readFileSync(resolve(REPO_ROOT, ".env"), "utf-8");
    const env: Record<string, string> = {};
    for (const line of content.split("\n")) {
      const trimmed = line.trim();
      if (!trimmed || trimmed.startsWith("#")) continue;
      const idx = trimmed.indexOf("=");
      if (idx === -1) continue;
      env[trimmed.slice(0, idx)] = trimmed.slice(idx + 1);
    }
    return env;
  } catch {
    return {};
  }
}

const _env = loadEnv();

// 注入到 process.env（不覆盖已有值）
for (const [k, v] of Object.entries(_env)) {
  if (!process.env[k]) process.env[k] = v;
}

// ---------------------------------------------------------------------------
// 自定义 State 类型
// ---------------------------------------------------------------------------

export interface BabyAgentState {
  messages: BaseMessage[];
  steering_queue: BaseMessage[];
  follow_up_queue: BaseMessage[];
}

// ---------------------------------------------------------------------------
// 创建 Agent
// ---------------------------------------------------------------------------

export function createBabyAgent(modelName?: string) {
  const model = new ChatOpenAI({
    model: modelName || _env.OPENAI_MODEL || "gpt-4o-mini",
    apiKey: _env.OPENAI_API_KEY,
    configuration: { baseURL: _env.OPENAI_API_BASE },
    temperature: 0.7,
  });

  const agent = createReactAgent({
    llm: model,
    tools: allTools,
    prompt:
      "You are an expert coding assistant. You help with software engineering tasks: " +
      "reading, searching, editing, and running code. Be concise. " +
      "Do not guess — read files before editing, verify before claiming.",
  });

  return agent;
}
