// OpenCode memory harness for m3m.
// OpenCode loads local plugins as ESM modules from ~/.config/opencode/plugins.
//
// Injection happens pre-turn, matching the push-core/pull-tail pattern used by
// Zep, Letta and Claude Code: memory must exist in the system prompt before
// the model's first generated token of a turn.
// - chat.message stashes the latest user text and invalidates the cached
//   system context once per user turn.
// - experimental.chat.system.transform injects the m3m context block into the
//   system prompt on every LLM request, rebuilt at most once per turn (with
//   prompt-relevant archival retrieval when a user text is stashed).
// - session.idle captures the transcript after the session goes idle.
import { spawn } from "node:child_process";

const M3M_CONTEXT_MARKER = "<m3m-persistent-memory>";
const M3M_TIMEOUT_MS = 10000;
const lastSavedTranscript = new Map();
const sessionContext = new Map();
const sessionPrompt = new Map();

function killTree(child) {
  // m3m may have spawned its own provider CLI; signal the whole group so no
  // descendant survives as an orphan.
  if (!child.pid || child.exitCode !== null || child.signalCode !== null) return;
  try {
    process.kill(-child.pid, "SIGTERM");
  } catch {
    try { child.kill("SIGTERM"); } catch {}
  }
  setTimeout(() => {
    if (child.exitCode !== null || child.signalCode !== null) return;
    try { process.kill(-child.pid, "SIGKILL"); } catch {
      try { child.kill("SIGKILL"); } catch {}
    }
  }, 2000).unref();
}

function runM3m(args, input, timeoutMs = M3M_TIMEOUT_MS) {
  return new Promise((resolve, reject) => {
    const child = spawn("m3m", args, { stdio: ["pipe", "pipe", "pipe"], detached: true });
    let stdout = "";
    let stderr = "";
    let settled = false;
    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      killTree(child);
      reject(new Error(`m3m ${args[0]} timed out after ${timeoutMs}ms and its process tree was terminated`));
    }, timeoutMs);
    child.stdout.on("data", (chunk) => { stdout += chunk; });
    child.stderr.on("data", (chunk) => { stderr += chunk; });
    child.on("error", (error) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      reject(error);
    });
    child.on("close", (code) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      if (code === 0) resolve(stdout);
      else reject(new Error(stderr.trim() || `m3m exited with code ${code}`));
    });
    child.stdin.end(input);
  });
}

function transcriptFromMessages(response) {
  const messages = Array.isArray(response) ? response : response?.data;
  if (!Array.isArray(messages)) return "";

  return messages.flatMap(({ info, parts = [] }) => {
    if (info?.role !== "user" && info?.role !== "assistant") return [];
    const text = parts
      .filter((part) => part?.type === "text" && typeof part.text === "string")
      .map((part) => part.text)
      .join("\n")
      .trim();
    if (!text || text.startsWith(M3M_CONTEXT_MARKER)) return [];
    return [`${info.role.toUpperCase()}: ${text}`];
  }).join("\n\n");
}

function textFromParts(parts) {
  return (Array.isArray(parts) ? parts : [])
    .filter((part) => part?.type === "text" && typeof part.text === "string" && !part.text.startsWith(M3M_CONTEXT_MARKER))
    .map((part) => part.text)
    .join("\n")
    .trim();
}

export const M3mMemoryPlugin = async ({ client }) => ({
  "chat.message": async (input, output) => {
    // New user turn: stash the text for relevance retrieval and force the
    // system context to rebuild on the next LLM request of this turn.
    const sessionID = input?.sessionID;
    if (!sessionID) return;
    const text = textFromParts(output?.parts);
    if (text) sessionPrompt.set(sessionID, text);
    sessionContext.delete(sessionID);
  },

  "experimental.chat.system.transform": async (input, output) => {
    const sessionID = input?.sessionID;
    if (!sessionID || !Array.isArray(output?.system)) return;
    let context = sessionContext.get(sessionID);
    if (context === undefined) {
      const prompt = sessionPrompt.get(sessionID);
      try {
        // Per-turn injection runs on every LLM request: skip usage writes to
        // avoid rewriting memory files under hook bursts. SessionStart hooks
        // and manual `m3m context` keep the reinforcement signal.
        context = (await runM3m(prompt ? ["context", "--prompt", prompt, "--no-record-usage"] : ["context", "--no-record-usage"])).trim();
        sessionContext.set(sessionID, context);
      } catch (error) {
        await client.app.log({ body: { service: "m3m", level: "warn", message: `context injection failed: ${error.message}` } });
        return;
      }
    }
    if (!context) return;
    output.system.push(`${M3M_CONTEXT_MARKER}\n${context}`);
  },

  event: async ({ event }) => {
    if (event.type === "session.idle") {
      const sessionID = event.properties.sessionID;
      try {
        const transcript = transcriptFromMessages(await client.session.messages({ path: { id: sessionID } }));
        if (!transcript || lastSavedTranscript.get(sessionID) === transcript) return;
        await runM3m(["save", "--stdin", "--session-end", "--quiet"], transcript);
        lastSavedTranscript.set(sessionID, transcript);
      } catch (error) {
        await client.app.log({ body: { service: "m3m", level: "warn", message: `memory capture failed: ${error.message}` } });
      }
      return;
    }

    if (event.type === "session.deleted") {
      const sessionID = event.properties.info.id;
      lastSavedTranscript.delete(sessionID);
      sessionContext.delete(sessionID);
      sessionPrompt.delete(sessionID);
    }
  },
});
