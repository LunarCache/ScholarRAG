export type StreamEvent = { id: number; event: string; data: unknown };

// Decode before splitting: UTF-8 code points and CRLF may cross transport chunks.
export async function consumeEventStream(
  body: ReadableStream<Uint8Array>,
  after: number,
  signal: AbortSignal,
  receive: (event: StreamEvent) => void,
): Promise<number> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "", frame: string[] = [], cursor = after;
  function dispatch() {
    let id = "", event = "message";
    const data: string[] = [];
    for (const line of frame) {
      if (line.startsWith(":")) continue;
      const split = line.indexOf(":");
      const field = split < 0 ? line : line.slice(0, split);
      const value = split < 0 ? "" : line.slice(split + 1).replace(/^ /, "");
      if (field === "id") id = value;
      if (field === "event") event = value;
      if (field === "data") data.push(value);
    }
    frame = [];
    if (!data.length) return;
    const payload: unknown = JSON.parse(data.join("\n"));
    if (event === "access.revoked") { receive({ id: cursor, event, data: payload }); return; }
    if (!/^\d+$/.test(id) || !Number.isSafeInteger(Number(id))) throw new Error("Invalid event cursor");
    const next = Number(id);
    if (next <= cursor) return;
    receive({ id: next, event, data: payload });
    cursor = next;
  }
  const cancel = () => { void reader.cancel().catch(() => undefined); };
  signal.addEventListener("abort", cancel, { once: true });
  try {
    while (true) {
      if (signal.aborted) throw new DOMException("Aborted", "AbortError");
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      if (buffer.length + frame.reduce((size, line) => size + line.length, 0) > 2_000_000) throw new Error("Event too large");
      let newline: number;
      while ((newline = buffer.indexOf("\n")) >= 0) {
        const line = buffer.slice(0, newline).replace(/\r$/, "");
        buffer = buffer.slice(newline + 1);
        if (!line) dispatch(); else frame.push(line);
      }
    }
    if (signal.aborted) throw new DOMException("Aborted", "AbortError");
    // An incomplete frame is intentionally replayed from the last dispatched ID.
    return cursor;
  } finally {
    signal.removeEventListener("abort", cancel);
    await reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
}
