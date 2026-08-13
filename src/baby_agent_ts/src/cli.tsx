#!/usr/bin/env node
/**
 * Baby Agent CLI — Ink TUI 入口。
 *
 * 用法:
 *   npx tsx src/cli.tsx              # 默认模型
 *   npx tsx src/cli.tsx --model gpt-4o-mini  # 指定模型
 */

import React from "react";
import { render } from "ink";
import { App } from "./app.js";

const args = process.argv.slice(2);
let model: string | undefined;
const modelIdx = args.indexOf("--model");
if (modelIdx !== -1 && args[modelIdx + 1]) {
  model = args[modelIdx + 1];
}

render(<App model={model} />);
