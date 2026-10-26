import assert from "node:assert/strict";
import test from "node:test";
import { consumeEventStream } from "../lib/workspace-sse.ts";
import { WorkspaceApi } from "../lib/workspace-api.ts";

function bytes(text, size = 1) {
  const encoded = new TextEncoder().encode(text);
  return new ReadableStream({ start(controller) {
    for (let i = 0; i < encoded.length; i += size) controller.enqueue(encoded.slice(i, i + size));
    controller.close();
  }});
}
const signal = () => new AbortController().signal;

test("incremental UTF-8, CRLF, comments and multiline JSON survive arbitrary chunk boundaries", async () => {
  const received = [];
  const cursor = await consumeEventStream(bytes(': heartbeat\r\n\r\nid: 1\r\ndata: {"message":\r\ndata: "引用证据"}\r\n\r\n'), 0, signal(), (event) => received.push(event));
  assert.equal(cursor, 1);
  assert.deepEqual(received, [{ id: 1, event: "message", data: { message: "引用证据" } }]);
});

test("replayed IDs do not duplicate generated content and incomplete frames resume from last dispatched cursor", async () => {
  const received = [];
  const cursor = await consumeEventStream(bytes('id: 4\ndata: {"text":"old"}\n\nid: 5\ndata: {"text":"new"}\n\nid: 6\ndata: {"text":"truncated"}', 8), 4, signal(), (event) => received.push(event));
  assert.equal(cursor, 5);
  assert.deepEqual(received.map((event) => event.data.text), ["new"]);
});

test("revocation without an ID is delivered and never mistaken for an answer event", async () => {
  const received = [];
  const cursor = await consumeEventStream(bytes('event: access.revoked\ndata: {"type":"access.revoked"}\n\n'), 9, signal(), (event) => received.push(event));
  assert.equal(cursor, 9); assert.equal(received[0].event, "access.revoked");
});

test("malformed JSON, missing cursors and oversized frames fail closed", async () => {
  for (const content of ['id: 1\ndata: invalid\n\n', 'data: {}\n\n', `id: 1\ndata: ${"x".repeat(2_000_001)}`]) {
    await assert.rejects(consumeEventStream(bytes(content, 100000), 0, signal(), () => assert.fail("invalid frame emitted")));
  }
});

test("aborting an idle SSE reader cancels its source and releases the connection", async () => {
  const controller = new AbortController(); let canceled = false;
  const stream = new ReadableStream({ cancel() { canceled = true; } });
  const pending = consumeEventStream(stream, 0, controller.signal, () => undefined);
  controller.abort();
  await assert.rejects(pending, (error) => error.name === "AbortError");
  assert.equal(canceled, true);
});

test("authenticated SSE reconnect is a GET of the same run with a cursor, never a new paid query", async () => {
  const calls = [];
  const client = new WorkspaceApi("https://api.example", "synthetic", async (url, init) => {
    calls.push({ url, init });
    return new Response(bytes('id: 8\ndata: {"seq":8,"run_id":"run-test"}\n\n'), { headers: { "Content-Type": "text/event-stream" } });
  });
  const cursor = await client.stream("ws-test", "run-test", 7, signal(), () => undefined);
  assert.equal(cursor, 8);
  assert.equal(calls[0].url, "https://api.example/api/workspaces/ws-test/query-runs/run-test/events");
  assert.equal(calls[0].init.headers.get("Authorization"), "Bearer synthetic");
  assert.equal(calls[0].init.headers.get("Last-Event-ID"), "7");
  assert.ok(!calls[0].init.method || calls[0].init.method === "GET");
});

test("SSE revocation becomes an authorization failure before any private event is accepted", async () => {
  const client = new WorkspaceApi("https://api.example", "synthetic", async () => new Response(bytes('event: access.revoked\ndata: {}\n\n'), { headers: { "Content-Type": "text/event-stream" } }));
  await assert.rejects(client.stream("ws", "run", 1, signal(), () => assert.fail("revocation treated as content")), (error) => error.status === 403);
});
