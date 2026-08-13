import { tool } from "@langchain/core/tools";
import { z } from "zod";
import { exec } from "child_process";
import { readFile as fsReadFile, writeFile as fsWriteFile } from "fs/promises";

/**
 * Execute a shell command and return its output.
 */
export const runCommand = tool(
  async (input: { command: string }): Promise<string> => {
    return new Promise((resolve) => {
      exec(input.command, { timeout: 30_000 }, (err, stdout, stderr) => {
        if (err) {
          resolve(`Error: ${stderr || err.message}`);
          return;
        }
        resolve(stdout || "(no output)");
      });
    });
  },
  {
    name: "run_command",
    description: "Execute a shell command and return its output.",
    schema: z.object({
      command: z.string().describe("The shell command to run (e.g. 'ls -la', 'cat file.txt')"),
    }),
  }
);

/**
 * Read a file and return its contents.
 */
export const readFile = tool(
  async (input: { path: string }): Promise<string> => {
    try {
      return await fsReadFile(input.path, "utf-8");
    } catch (err: unknown) {
      return `Error: ${(err as Error).message}`;
    }
  },
  {
    name: "read_file",
    description: "Read a file and return its contents.",
    schema: z.object({
      path: z.string().describe("Path to the file to read"),
    }),
  }
);

/**
 * Write content to a file.
 */
export const writeFile = tool(
  async (input: { path: string; content: string }): Promise<string> => {
    try {
      await fsWriteFile(input.path, input.content, "utf-8");
      return `Wrote ${input.content.length} bytes to ${input.path}`;
    } catch (err: unknown) {
      return `Error: ${(err as Error).message}`;
    }
  },
  {
    name: "write_file",
    description: "Write content to a file.",
    schema: z.object({
      path: z.string().describe("Path to the file to write"),
      content: z.string().describe("Content to write to the file"),
    }),
  }
);

export const allTools = [runCommand, readFile, writeFile];
