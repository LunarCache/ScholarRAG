import assert from "node:assert/strict";
import test from "node:test";
import { activeJob, WorkspaceApi, WorkspaceApiError, workspaceError } from "../lib/workspace-api.ts";
const base = "https://scholar-api.example";
const signal = () => new AbortController().signal;
const json = (data, status = 200) => new Response(JSON.stringify(data), { status, headers: { "Content-Type": "application/json" } });

test("authentication uses headers; requests do not cache, carry cookies or follow redirects", async () => {
  let seen;
  const client = new WorkspaceApi(`${base}/`, "synthetic-session", async (url, init) => {
    seen = { url, init }; return json({ user_id: "user-test", display_name: "Reader" });
  });
  assert.equal((await client.profile(signal())).display_name, "Reader");
  assert.equal(seen.url, `${base}/api/me`);
  assert.equal(seen.init.headers.get("Authorization"), "Bearer synthetic-session");
  assert.equal(seen.init.cache, "no-store");
  assert.equal(seen.init.credentials, "omit");
  assert.equal(seen.init.redirect, "error");
  assert.equal(seen.init.referrerPolicy, "no-referrer");
  assert.ok(!seen.url.includes("synthetic-session"));
});

test("both snapshot reads target the selected workspace", async () => {
  const seen = [];
  const client = new WorkspaceApi(base, "token", async (url) => {
    seen.push(url); return json(url.includes("/documents") ? [{ document_id: "doc-test" }] : [{ job_id: "job-test" }]);
  });
  const [papers, jobs] = await client.snapshot("ws-selected", signal());
  assert.deepEqual(seen.sort(), [`${base}/api/workspaces/ws-selected/documents`, `${base}/api/workspaces/ws-selected/ingestion-jobs?limit=50`]);
  assert.equal(papers[0].document_id, "doc-test"); assert.equal(jobs[0].job_id, "job-test");
});

test("multipart retries preserve key and PDF without overriding the content boundary", async () => {
  const seen = [];
  const client = new WorkspaceApi(base, "token", async (url, init) => {
    seen.push({ url, init }); return json({ job_id: "job-test", status: "queued" });
  });
  const file = new File(["synthetic PDF"], "paper.pdf", { type: "application/pdf" });
  await client.upload("ws-test", file, "same-request", signal());
  await client.upload("ws-test", file, "same-request", signal());
  for (const { url, init } of seen) {
    assert.equal(url, `${base}/api/workspaces/ws-test/uploads`);
    assert.equal(init.headers.get("Idempotency-Key"), "same-request");
    assert.equal(init.headers.get("Content-Type"), null);
    assert.equal(await init.body.get("file").text(), "synthetic PDF");
    assert.equal(init.method, "POST");
  }
});

test("cancellation targets only the selected workspace and job", async () => {
  const client = new WorkspaceApi(base, "token", async (url, init) => {
    assert.equal(url, `${base}/api/workspaces/ws-test/ingestion-jobs/job-test/cancel`);
    assert.equal(init.method, "POST"); return json({ job_id: "job-test", status: "canceled" });
  });
  assert.equal((await client.cancel("ws-test", "job-test", signal())).status, "canceled");
});

test("logout revokes the server session without putting secrets in its URL", async () => {
  const client = new WorkspaceApi(base, "secret-token", async (url, init) => {
    assert.equal(url, `${base}/api/me/session`); assert.equal(init.method, "DELETE");
    return new Response(null, { status: 204 });
  });
  assert.equal((await client.logout()).status, 204);
});

for (const status of [401, 403, 409, 413, 422, 429, 500]) {
  test(`HTTP ${status} never exposes raw provider errors`, async () => {
    const client = new WorkspaceApi(base, "token", async () => json({ detail: "private-path?token=DO-NOT-EXPOSE" }, status));
    await assert.rejects(client.profile(signal()), (error) => {
      assert.ok(error instanceof WorkspaceApiError); assert.equal(error.status, status);
      assert.ok(!workspaceError(error).includes("DO-NOT-EXPOSE")); return true;
    });
  });
}

test("PDF fetch authenticates and returns a correctly typed blob", async () => {
  const client = new WorkspaceApi(base, "pdf-session", async (url, init) => {
    assert.equal(url, `${base}/api/workspaces/ws-test/documents/doc-test/pdf`);
    assert.equal(init.headers.get("Authorization"), "Bearer pdf-session");
    return new Response("%PDF-synthetic", { headers: { "Content-Type": "application/pdf" } });
  });
  const result = await client.pdf("ws-test", "doc-test", signal());
  assert.equal(result.type, "application/pdf"); assert.equal(await result.text(), "%PDF-synthetic");
});

test("HTML cannot become an executable blob preview", async () => {
  const client = new WorkspaceApi(base, "token", async () => new Response("<script>bad()</script>", { headers: { "Content-Type": "text/html" } }));
  await assert.rejects(client.pdf("ws-test", "doc-test", signal()), (error) => error.status === 502);
});

test("aborting the old scope aborts its in-flight request", async () => {
  const controller = new AbortController(); let received;
  const client = new WorkspaceApi(base, "token", async (url, init) => {
    received = init.signal;
    return new Promise((resolve, reject) => init.signal.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")), { once: true }));
  });
  const pending = client.profile(controller.signal); controller.abort();
  await assert.rejects(pending, (error) => error.name === "AbortError"); assert.equal(received.aborted, true);
});

test("arbitrary exception text is never displayed", () => {
  assert.ok(!workspaceError(new Error("token=private")).includes("private"));
});

test("only queued and parsing tasks are active", () => {
  for (const status of ["queued", "parsing", "ready", "failed", "canceled"]) assert.equal(activeJob({ status }), ["queued", "parsing"].includes(status));
});

test("native fetch is called without an invalid client receiver", async () => {
  const client = new WorkspaceApi(base, "token", async function () {
    assert.equal(this, undefined);
    return json({ user_id: "synthetic" });
  });
  assert.equal((await client.profile(signal())).user_id, "synthetic");
});

test("scope cancellation remains active while the response body is being read", async () => {
  const controller = new AbortController();
  let bodyStarted;
  const started = new Promise((resolve) => { bodyStarted = resolve; });
  const client = new WorkspaceApi(base, "token", async (url, init) => {
    const body = new ReadableStream({ start(stream) {
      init.signal.addEventListener("abort", () => stream.error(new DOMException("Aborted", "AbortError")), { once: true });
      bodyStarted();
    }});
    return new Response(body, { headers: { "Content-Type": "application/json" } });
  });
  const pending = client.profile(controller.signal);
  await started; controller.abort();
  await assert.rejects(pending, (error) => error.name === "AbortError");
});

test("personal state sends the expected revision and only user editable fields", async () => {
  const body = { reading_status: "reading", tags: ["retrieval"], note: "private", favorite: true, expected_revision: 3 };
  const client = new WorkspaceApi(base, "token", async (url, init) => {
    assert.equal(url, `${base}/api/workspaces/ws-test/documents/doc-test/personal`);
    assert.equal(init.method, "PUT");
    assert.deepEqual(JSON.parse(init.body), body);
    return json({ ...body, revision: 4 });
  });
  assert.equal((await client.saveState("ws-test", "doc-test", body, signal())).revision, 4);
});

test("collection create and update select different methods and preserve optimistic concurrency", async () => {
  const requests = [];
  const client = new WorkspaceApi(base, "token", async (url, init) => {
    requests.push({ url, method: init.method, body: JSON.parse(init.body) });
    return json({ collection_id: "col-test", revision: requests.length });
  });
  await client.saveCollection("ws-test", { name: "Notes", document_ids: ["doc-test"] }, signal());
  await client.saveCollection("ws-test", { name: "Updated", document_ids: [], expected_revision: 1 }, signal(), "col-test");
  assert.equal(requests[0].method, "POST"); assert.equal(requests[1].method, "PUT");
  assert.equal(requests[1].body.expected_revision, 1);
  assert.equal(requests[1].url, `${base}/api/workspaces/ws-test/collections/col-test`);
});

test("private state and collection deletion accept 204 without trying to parse JSON", async () => {
  const client = new WorkspaceApi(base, "token", async (url, init) => {
    assert.equal(init.method, "DELETE"); return new Response(null, { status: 204 });
  });
  await client.deleteState("ws", "doc", signal());
  await client.deleteCollection("ws", "col", signal());
});

test("multi-turn query submission includes only the selected scope and server-issued parent", async () => {
  const body = { question: "Compare results", document_ids: ["doc-one"], agentic: false, conversation_id: "conv-server", parent_run_id: "run-server" };
  const client = new WorkspaceApi(base, "token", async (url, init) => {
    assert.equal(url, `${base}/api/workspaces/ws/query-runs`);
    assert.equal(init.method, "POST"); assert.deepEqual(JSON.parse(init.body), body);
    return json({ run_id: "new-server-run" });
  });
  assert.equal((await client.createRun("ws", body, signal())).run_id, "new-server-run");
});

test("stopping an answer cancels its query run rather than an ingestion job", async () => {
  const client = new WorkspaceApi(base, "token", async (url, init) => {
    assert.equal(url, `${base}/api/workspaces/ws/query-runs/run-active/cancel`);
    assert.equal(init.method, "POST");
    return json({ run_id: "run-active", status: "cancelled" });
  });
  assert.equal((await client.cancelRun("ws", "run-active", signal())).status, "cancelled");
});

test("evaluation creation preserves idempotency and cannot override parent scope", async () => {
  const seen = [];
  const client = new WorkspaceApi(base, "token", async (url, init) => {
    seen.push({ url, init }); return json({ job_id: "evjob-test", status: "queued" });
  });
  await client.createEvaluation("ws-one", "run-one", "user supplied", "same-request", signal());
  await client.createEvaluation("ws-one", "run-one", "user supplied", "same-request", signal());
  for (const { url, init } of seen) {
    assert.equal(url, `${base}/api/workspaces/ws-one/evaluations`);
    assert.equal(init.method, "POST");
    assert.equal(init.headers.get("Idempotency-Key"), "same-request");
    assert.deepEqual(JSON.parse(init.body), { query_run_id: "run-one", reference: "user supplied" });
    assert.equal(init.headers.get("Authorization"), "Bearer token");
  }
});

test("evaluation reads, history, events and cancellation stay in the selected workspace", async () => {
  const seen = [];
  const client = new WorkspaceApi(base, "token", async (url, init) => {
    seen.push({ url, method: init.method ?? "GET" }); return json({ job_id: "evjob-test", status: "completed" });
  });
  await client.evaluation("ws-two", "evjob-test", signal());
  await client.latestEvaluation("ws-two", "run-test", signal());
  await client.evaluationEvents("ws-two", "evjob-test", signal());
  await client.cancelEvaluation("ws-two", "evjob-test", signal());
  assert.deepEqual(seen, [
    { url: `${base}/api/workspaces/ws-two/evaluations/evjob-test`, method: "GET" },
    { url: `${base}/api/workspaces/ws-two/query-runs/run-test/evaluations/latest`, method: "GET" },
    { url: `${base}/api/workspaces/ws-two/evaluations/evjob-test/event-log`, method: "GET" },
    { url: `${base}/api/workspaces/ws-two/evaluations/evjob-test/cancel`, method: "POST" },
  ]);
});

test("parse review uses scoped URLs and sends conflict revision without an actor", async () => {
  const seen = [];
  const client = new WorkspaceApi(base, "token", async (url, init) => {
    seen.push({ url, init }); return json({ revision: 2 });
  });
  await client.parseReview("ws-a", "doc/a", signal());
  const input = { expected_revision: 1, expected_parse_id: "parse-new", reason: "reviewed", force: false };
  await client.activateParse("ws-a", "doc/a", "parse/old", input, signal());
  assert.equal(seen[0].url, `${base}/api/workspaces/ws-a/documents/doc%2Fa/parse-review`);
  assert.equal(seen[1].url, `${base}/api/workspaces/ws-a/documents/doc%2Fa/parses/parse%2Fold/activate`);
  assert.equal(seen[1].init.method, "POST");
  assert.deepEqual(JSON.parse(seen[1].init.body), input);
  assert.equal(seen[1].init.headers.get("Content-Type"), "application/json");
  assert.equal(seen[1].init.cache, "no-store");
});
