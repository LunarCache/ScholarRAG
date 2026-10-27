import { consumeEventStream } from "./workspace-sse.ts";
import type { StreamEvent } from "./workspace-sse.ts";

export type Profile = { user_id: string; display_name: string };
export type Workspace = {
  workspace_id: string;
  name: string;
  role: "viewer" | "member" | "admin" | "owner";
};
export type WorkspacePaper = {
  document_id: string;
  title: string | null;
  source_filename: string;
  chunk_count: number;
};
export type IngestionJob = {
  job_id: string;
  filename: string;
  status: "queued" | "parsing" | "ready" | "failed" | "canceled";
  error_code: string | null;
  document_id: string | null;
  created_at: string;
};

export type PaperState = {
  document_id: string; reading_status: "unread" | "reading" | "finished";
  tags: string[]; note: string; favorite: boolean; revision: number;
};
export type PaperStateInput = Omit<PaperState, "document_id" | "revision"> & { expected_revision: number };
export type PaperCollection = {
  collection_id: string; name: string; document_ids: string[];
  unavailable_document_count: number; revision: number;
};
export type QueryAnswer = {
  answer: string;
  citations: { citation_id: string; document_id: string; page_number: number; quote: string; chunk_id: string }[];
  claims: { claim_id: string; text: string; evidence_located?: boolean; support_status?: string }[];
};
export type WorkspaceRun = {
  run_id: string; trace_id: string; question: string; document_ids: string[];
  status: "queued" | "running" | "completed" | "failed" | "cancelled";
  conversation_id: string; parent_run_id: string | null; created_at: string;
  answer: QueryAnswer | null; error: string | null;
};
export type RunEvent = {
  seq: number; run_id: string; type: string; label: string; stage_id: string;
  status: string; output: Record<string, unknown>;
};

export type EvaluationJob = {
  job_id: string; workspace_id: string; owner_id: string; query_run_id: string;
  status: "queued" | "running" | "completed" | "failed" | "cancelled";
  created_at: string; event_count: number; error: string | null;
  case: { question: string; reference: string | null; review_status: string };
  result: { metrics: Record<string, { value: number | boolean | string | null; reason?: string | null }> } | null;
};

export class WorkspaceApiError extends Error {
  status: number;
  constructor(status: number) {
    const messages: Record<number, string> = {
      401: "会话无效或已过期，请重新登录。",
      403: "访问权限已变化，请刷新工作空间后重试。",
      404: "内容不可用，请确认 API 已启用工作空间模式。",
      409: "请求冲突或队列已满。请检查任务列表后重试。",
      413: "PDF 超出服务器允许的文件大小或页数。",
      415: "仅支持 PDF 文件。",
      422: "文件或请求格式不符合要求，请检查后重试。",
      429: "请求过于频繁，请稍后重试。",
    };
    super(messages[status] ?? "服务暂时不可用，请稍后重试。");
    this.status = status;
  }
}

// This client never persists the bearer or puts it into a URL, cookie or log.
export class WorkspaceApi {
  #base: string;
  #token: string;
  #fetch: typeof fetch;

  constructor(base: string, token: string, fetcher: typeof fetch = fetch) {
    this.#base = base.replace(/\/$/, "");
    this.#token = token;
    this.#fetch = fetcher;
  }

  async #perform<T>(path: string, init: RequestInit, consume: (response: Response) => Promise<T>, timeoutMs = 30000): Promise<T> {
    if (!path.startsWith("/api/") || path.includes("..")) throw new Error("Invalid API path");
    const headers = new Headers(init.headers);
    headers.set("Authorization", `Bearer ${this.#token}`);
    const controller = new AbortController();
    const cancel = () => controller.abort();
    if (init.signal?.aborted) cancel();
    init.signal?.addEventListener("abort", cancel, { once: true });
    const timer = setTimeout(cancel, timeoutMs);
    try {
      // Native browser fetch must not receive this client as its WebIDL receiver.
      const fetcher = this.#fetch;
      const response = await fetcher(`${this.#base}${path}`, {
        ...init, headers, signal: controller.signal, cache: "no-store", credentials: "omit", redirect: "error",
        referrerPolicy: "no-referrer",
      });
      if (!response.ok) throw new WorkspaceApiError(response.status);
      return await consume(response);
    } finally {
      clearTimeout(timer);
      init.signal?.removeEventListener("abort", cancel);
    }
  }

  async json<T>(path: string, init: RequestInit = {}, timeoutMs = 30000): Promise<T> {
    return this.#perform(path, init, (response) => response.json() as Promise<T>, timeoutMs);
  }

  profile(signal: AbortSignal) { return this.json<Profile>("/api/me", { signal }); }
  workspaces(signal: AbortSignal) { return this.json<Workspace[]>("/api/workspaces", { signal }); }
  logout() { return this.#perform("/api/me/session", { method: "DELETE" }, async (response) => response); }

  path(workspaceId: string, suffix: string) {
    return `/api/workspaces/${encodeURIComponent(workspaceId)}${suffix}`;
  }

  snapshot(workspaceId: string, signal: AbortSignal) {
    return Promise.all([
      this.json<WorkspacePaper[]>(this.path(workspaceId, "/documents"), { signal }),
      this.json<IngestionJob[]>(this.path(workspaceId, "/ingestion-jobs?limit=50"), { signal }),
    ]);
  }

  upload(workspaceId: string, file: File, key: string, signal: AbortSignal) {
    const body = new FormData();
    body.append("file", file);
    return this.json<IngestionJob>(this.path(workspaceId, "/uploads"), {
      method: "POST", headers: { "Idempotency-Key": key }, body, signal,
    }, 300000);
  }

  cancel(workspaceId: string, jobId: string, signal: AbortSignal) {
    return this.json<IngestionJob>(this.path(workspaceId, `/ingestion-jobs/${encodeURIComponent(jobId)}/cancel`), {
      method: "POST", signal,
    });
  }

  createRun(ws: string, body: { question: string; document_ids: string[]; agentic: boolean; tool_mode?: boolean; conversation_id?: string; parent_run_id?: string }, signal: AbortSignal) {
    return this.json<WorkspaceRun>(this.path(ws, "/query-runs"), { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), signal });
  }
  runs(ws: string, signal: AbortSignal) { return this.json<WorkspaceRun[]>(this.path(ws, "/query-runs?limit=50"), { signal }); }
  run(ws: string, id: string, signal: AbortSignal) { return this.json<WorkspaceRun>(this.path(ws, `/query-runs/${encodeURIComponent(id)}`), { signal }); }
  runEvents(ws: string, id: string, signal: AbortSignal) { return this.json<RunEvent[]>(this.path(ws, `/query-runs/${encodeURIComponent(id)}/event-log`), { signal }); }
  conversation(ws: string, id: string, signal: AbortSignal) { return this.json<WorkspaceRun[]>(this.path(ws, `/conversations/${encodeURIComponent(id)}?limit=100`), { signal }); }
  cancelRun(ws: string, id: string, signal: AbortSignal) { return this.json<WorkspaceRun>(this.path(ws, `/query-runs/${encodeURIComponent(id)}/cancel`), { method: "POST", signal }); }
  stream(ws: string, id: string, after: number, signal: AbortSignal, receive: (event: StreamEvent) => void) {
    return this.#perform(this.path(ws, `/query-runs/${encodeURIComponent(id)}/events`), {
      headers: { Accept: "text/event-stream", "Last-Event-ID": String(after) }, signal,
    }, async (response) => {
      if (!response.body || !response.headers.get("Content-Type")?.startsWith("text/event-stream")) throw new WorkspaceApiError(502);
      return consumeEventStream(response.body, after, signal, (event) => {
        if (event.event === "access.revoked") throw new WorkspaceApiError(403);
        receive(event);
      });
    }, 120000);
  }
  createEvaluation(ws: string, runId: string, reference: string | null, key: string, signal: AbortSignal) {
    return this.json<EvaluationJob>(this.path(ws, "/evaluations"), {
      method: "POST", headers: { "Content-Type": "application/json", "Idempotency-Key": key },
      body: JSON.stringify({ query_run_id: runId, reference }), signal,
    });
  }
  evaluation(ws: string, id: string, signal: AbortSignal) {
    return this.json<EvaluationJob>(this.path(ws, `/evaluations/${encodeURIComponent(id)}`), { signal });
  }
  latestEvaluation(ws: string, runId: string, signal: AbortSignal) {
    return this.json<EvaluationJob>(this.path(ws, `/query-runs/${encodeURIComponent(runId)}/evaluations/latest`), { signal });
  }
  cancelEvaluation(ws: string, id: string, signal: AbortSignal) {
    return this.json<EvaluationJob>(this.path(ws, `/evaluations/${encodeURIComponent(id)}/cancel`), { method: "POST", signal });
  }
  evaluationEvents(ws: string, id: string, signal: AbortSignal) {
    return this.json<RunEvent[]>(this.path(ws, `/evaluations/${encodeURIComponent(id)}/event-log`), { signal });
  }
  exportRun(ws: string, id: string, signal: AbortSignal) {
    return this.#perform(this.path(ws, `/query-runs/${encodeURIComponent(id)}/export`), { signal }, (response) => response.blob());
  }
  state(ws: string, doc: string, signal: AbortSignal) { return this.json<PaperState>(this.path(ws, `/documents/${encodeURIComponent(doc)}/personal`), { signal }); }
  saveState(ws: string, doc: string, body: PaperStateInput, signal: AbortSignal) {
    return this.json<PaperState>(this.path(ws, `/documents/${encodeURIComponent(doc)}/personal`), { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), signal });
  }
  deleteState(ws: string, doc: string, signal: AbortSignal) {
    return this.#perform(this.path(ws, `/documents/${encodeURIComponent(doc)}/personal`), { method: "DELETE", signal }, async () => undefined);
  }
  collections(ws: string, signal: AbortSignal) { return this.json<PaperCollection[]>(this.path(ws, "/collections?limit=200"), { signal }); }
  saveCollection(ws: string, body: { name: string; document_ids: string[]; expected_revision?: number }, signal: AbortSignal, id?: string) {
    return this.json<PaperCollection>(this.path(ws, `/collections${id ? `/${encodeURIComponent(id)}` : ""}`), { method: id ? "PUT" : "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), signal });
  }
  deleteCollection(ws: string, id: string, signal: AbortSignal) {
    return this.#perform(this.path(ws, `/collections/${encodeURIComponent(id)}`), { method: "DELETE", signal }, async () => undefined);
  }

  async pdf(workspaceId: string, documentId: string, signal: AbortSignal) {
    return this.#perform(this.path(workspaceId, `/documents/${encodeURIComponent(documentId)}/pdf`), { signal }, async (response) => {
      if (response.headers.get("Content-Type")?.split(";")[0].trim().toLowerCase() !== "application/pdf") {
        throw new WorkspaceApiError(502);
      }
      return response.blob();
    }, 120000);
  }

}

export function workspaceError(error: unknown) {
  return error instanceof WorkspaceApiError ? error.message : "网络连接失败，请检查连接后重试。";
}

export function activeJob(job: IngestionJob) {
  return job.status === "queued" || job.status === "parsing";
}
