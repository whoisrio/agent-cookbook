/**
 * Baby Agent TUI — Ink 组件，底部输入 + 逐字 stream 输出。
 */

import React, { useState, useCallback, useRef, useEffect } from "react";
import { Box, Text, useInput, useApp, useStdin } from "ink";
import { AIMessageChunk, HumanMessage } from "@langchain/core/messages";
import { createBabyAgent } from "./agent.js";

// ---------------------------------------------------------------------------
// 类型
// ---------------------------------------------------------------------------

interface ChatEntry {
  role: "user" | "agent" | "system";
  text: string;
}

// ---------------------------------------------------------------------------
// App 组件
// ---------------------------------------------------------------------------

export function App({ model }: { model?: string }) {
  const [entries, setEntries] = useState<ChatEntry[]>([
    { role: "system", text: "等待输入... 输入 /help 查看命令" },
  ]);
  const [input, setInput] = useState("");
  const [streaming, setStreaming] = useState(false);
  const [streamBuf, setStreamBuf] = useState("");
  const agentRef = useRef(createBabyAgent(model));
  const streamingRef = useRef(false);
  const streamBufRef = useRef("");
  const entriesRef = useRef(entries);
  entriesRef.current = entries;

  // ── 输入处理 ──────────────────────────────────────────────────

  const handleSubmit = useCallback(async () => {
    const text = input.trim();
    setInput("");
    if (!text) return;

    // 特殊命令
    if (text === "/quit") {
      process.exit(0);
    }

    if (text === "/help") {
      setEntries((prev) => [
        ...prev,
        {
          role: "system",
          text:
            "命令:\n" +
            "  /steering <消息>  — 紧急插队\n" +
            "  /followup <消息>  — 任务追加\n" +
            "  /quit             — 退出",
        },
      ]);
      return;
    }

    // 用户消息
    setEntries((prev) => [...prev, { role: "user", text }]);
    setStreaming(true);
    streamingRef.current = true;
    streamBufRef.current = "";
    setStreamBuf("");

    // 调用 agent stream
    try {
      const agent = agentRef.current;
      const stream = await agent.stream(
        { messages: [new HumanMessage(text)] },
        { streamMode: "messages" }
      );

      let currentMsgId: string | undefined;

      for await (const [msg, _metadata] of stream) {
        if (!(msg instanceof AIMessageChunk)) continue;
        if (typeof msg.content !== "string" || !msg.content) continue;

        // 跳过 thinking
        if ((msg as any).reasoning_content) continue;
        if (msg.additional_kwargs?.reasoning_content) continue;

        // 新消息 → 归档上一条
        if (msg.id !== currentMsgId) {
          if (streamBufRef.current) {
            const buf = streamBufRef.current;
            setEntries((prev) => [...prev, { role: "agent", text: buf }]);
          }
          currentMsgId = msg.id;
          streamBufRef.current = "";
        }

        streamBufRef.current += msg.content;
        setStreamBuf(streamBufRef.current);
      }

      // 最后一条
      if (streamBufRef.current) {
        const buf = streamBufRef.current;
        setEntries((prev) => [...prev, { role: "agent", text: buf }]);
      }
    } catch (err) {
      setEntries((prev) => [
        ...prev,
        { role: "system", text: `Error: ${(err as Error).message}` },
      ]);
    }

    setStreaming(false);
    setStreamBuf("");
    streamingRef.current = false;
    streamBufRef.current = "";
  }, [input]);

  // ── 键盘输入 ─────────────────────────────────────────────────

  useInput((ch, key) => {
    if (streaming) return; // stream 期间不接受输入

    if (key.return) {
      handleSubmit();
      return;
    }

    if (key.backspace || key.delete) {
      setInput((prev) => prev.slice(0, -1));
      return;
    }

    if (ch && !key.ctrl && !key.meta) {
      setInput((prev) => prev + ch);
    }
  });

  // ── 渲染 ─────────────────────────────────────────────────────

  return (
    <Box flexDirection="column" height="100%">
      {/* 历史记录 + 当前 stream */}
      <Box flexDirection="column" flexGrow={1} paddingX={1}>
        {entries.map((e, i) => (
          <Box key={i} marginBottom={1}>
            {e.role === "user" && (
              <Text>
                <Text bold color="cyan">
                  🧑 You:{" "}
                </Text>
                {e.text}
              </Text>
            )}
            {e.role === "agent" && (
              <Text>
                <Text bold color="green">
                  🤖 Agent:{" "}
                </Text>
                {e.text}
              </Text>
            )}
            {e.role === "system" && <Text dimColor>{e.text}</Text>}
          </Box>
        ))}

        {/* 当前 stream（逐字更新） */}
        {streaming && streamBuf && (
          <Box marginBottom={1}>
            <Text>
              <Text bold color="green">
                🤖 Agent:{" "}
              </Text>
              {streamBuf}
            </Text>
          </Box>
        )}
      </Box>

      {/* 底部输入框 */}
      <Box
        borderStyle="single"
        borderColor="cyan"
        paddingX={1}
        marginBottom={1}
      >
        <Text>
          🧑{" "}
          <Text color={streaming ? "gray" : "white"}>
            {streaming ? "等待回复中..." : input}
            {!streaming && <Text color="gray">█</Text>}
          </Text>
        </Text>
      </Box>
    </Box>
  );
}
