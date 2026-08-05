"use client";

import { FormEvent, ReactNode, useCallback, useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import rehypeKatex from "rehype-katex";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import { AgentTraceCompact, EvidencePointer, QueryRun, RunFlow, TraceEvent } from "./RunFlow";
import { ReviewWorkbench } from "./ReviewWorkbench";

const API = process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000";

type Paper = {
  document_id: string;
  parse_id: string;
  title: string | null;
  source_filename: string;
  provider: string;
  provider_model: string;
  chunk_count: number;
  page_count?: number;
  quality?: { score?: number; decision?: string };
  routing?: { recommended_action?: string; activation_reason?: string };
};

type ParseVersion = {
  document_id: string;
  parse_id: string;
  provider: string;
  provider_model: string;
  status: string;
  active: boolean;
  can_activate: boolean;
  created_at: string | null;
  page_count: number | null;
  block_count: number | null;
  chunk_count: number | null;
  quality: { score?: number; decision?: string; critical_failures?: string[] } | null;
  routing: Record<string, unknown> | null;
};

type ParseRoute = {
  route_id?: string;
  created_at?: string;
  recommended_action?: string;
  selected_parse_id?: string | null;
  reason?: string;
  execution?: {
    actual_selected_parse_id?: string | null;
    deviated_from_recommendation?: boolean;
    fallback_error?: string | null;
  };
  comparison?: { overall_similarity?: number; disagreement_flags?: string[] } | null;
};

type Citation = {
  citation_id: string;
  document_id: string;
  chunk_id: string;
  page_number: number;
  quote: string;
  score: number | null;
  bbox: { x0: number; y0: number; x1: number; y1: number } | null;
};

type Answer = {
  answer: string;
  citations: Citation[];
  trace_id: string;
  retrieval_debug: Record<string, unknown>;
  claims: Array<{
    claim_id: string;
    text: string;
    supported: boolean;
    citation_ids: string[];
    evidence_spans: Array<{
      citation_id: string;
      document_id?: string;
      page_number: number;
      bbox: Citation["bbox"];
      quote: string;
    }>;
  }>;
};

type QueryRunSnapshot = QueryRun & {
  answer: Answer | null;
};

type ChatTurn = {
  run_id: string;
  question: string;
  answer: Answer | null;
  status: QueryRun["status"];
};

type Evaluation = {
  run_id: string;
  metrics: Record<string, { value: number | string | boolean | null; reason?: string }>;
  artifact_path: string;
};

type EvaluationJob = {
  job_id: string;
  status: "queued" | "running" | "completed" | "failed" | "cancelled";
  query_run_id: string | null;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
  event_count: number;
  result: Evaluation | null;
  error: string | null;
};

type CockpitView = "overview" | "library" | "reader" | "runs" | "evaluation";

export default function Home() {
  const [activeView, setActiveView] = useState<CockpitView>("overview");
  const [papers, setPapers] = useState<Paper[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [activePaper, setActivePaper] = useState<string | null>(null);
  const [page, setPage] = useState(1);
  const [highlight, setHighlight] = useState<Citation["bbox"]>(null);
  const [question, setQuestion] = useState("");
  // tool = 工具调用型 Agent（默认）；multi = 多步规划受控 Agent；rag = 单轮检索。
  const [answerMode, setAnswerMode] = useState<"tool" | "multi" | "rag">("tool");
  const [conversationId, setConversationId] = useState(() => crypto.randomUUID());
  const [turns, setTurns] = useState<ChatTurn[]>([]);
  const [lastQuestion, setLastQuestion] = useState("");
  const [answer, setAnswer] = useState<Answer | null>(null);
  const [streamedAnswer, setStreamedAnswer] = useState("");
  const [queryRun, setQueryRun] = useState<QueryRunSnapshot | null>(null);
  const [runEvents, setRunEvents] = useState<TraceEvent[]>([]);
  const [evaluationEvents, setEvaluationEvents] = useState<TraceEvent[]>([]);
  const [evaluationJob, setEvaluationJob] = useState<EvaluationJob | null>(null);
  const [evaluation, setEvaluation] = useState<Evaluation | null>(null);
  const [busy, setBusy] = useState(false);
  const [evaluationBusy, setEvaluationBusy] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [reviewOpen, setReviewOpen] = useState(false);
  const [versions, setVersions] = useState<ParseVersion[]>([]);
  const [routes, setRoutes] = useState<ParseRoute[]>([]);
  const [reviewTarget, setReviewTarget] = useState<string | null>(null);
  const [reviewReason, setReviewReason] = useState("");
  const [reviewForce, setReviewForce] = useState(false);
  const [reviewBusy, setReviewBusy] = useState(false);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [historyRuns, setHistoryRuns] = useState<QueryRunSnapshot[]>([]);
  const [historyBusy, setHistoryBusy] = useState(false);
  const [dashboardOpen, setDashboardOpen] = useState(false);
  const [dashboard, setDashboard] = useState<Record<string, unknown> | null>(null);
  const [acceptanceOpen, setAcceptanceOpen] = useState(false);
  const [personal, setPersonal] = useState({ reading_status: "unread", tags: [] as string[], note: "", favorite: false });
  const [status, setStatus] = useState("准备就绪");
  const fileRef = useRef<HTMLInputElement>(null);
  const conversationRef = useRef<HTMLDivElement>(null);
  const eventSourceRef = useRef<EventSource | null>(null);
  const evaluationEventSourceRef = useRef<EventSource | null>(null);

  useEffect(() => {
    conversationRef.current?.scrollTo({ top: conversationRef.current.scrollHeight });
  }, [turns, answer, streamedAnswer, queryRun?.status]);

  const refresh = useCallback(async () => {
    const response = await fetch(`${API}/api/documents`);
    if (!response.ok) throw new Error("无法读取论文库");
    const data: Paper[] = await response.json();
    setPapers(data);
    if (!activePaper && data[0]) {
      setActivePaper(data[0].document_id);
      setSelected([data[0].document_id]);
    }
  }, [activePaper]);

  useEffect(() => {
    refresh().catch((error) => setStatus(error.message));
  }, [refresh]);

  useEffect(() => () => {
    eventSourceRef.current?.close();
    evaluationEventSourceRef.current?.close();
  }, []);

  useEffect(() => {
    if (!activePaper) return;
    fetch(`${API}/api/documents/${activePaper}/personal`).then((response) => response.json())
      .then((value) => setPersonal({
        reading_status: value.reading_status ?? "unread",
        tags: Array.isArray(value.tags) ? value.tags : [],
        note: value.note ?? "",
        favorite: Boolean(value.favorite),
      })).catch(() => undefined);
  }, [activePaper]);

  // Rehydrate the chat thread (question bubbles + answers) when a conversation
  // is opened, started fresh, or when the current run finishes.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const response = await fetch(`${API}/api/query-runs?limit=100`);
        if (!response.ok) return;
        const runs: QueryRunSnapshot[] = await response.json();
        const own = runs
          .filter((run) => run.conversation_id === conversationId)
          .sort((left, right) => left.created_at.localeCompare(right.created_at))
          .map((run) => ({ run_id: run.run_id, question: run.question, answer: run.answer, status: run.status }));
        const hasCurrent = own.some((turn) => turn.run_id === queryRun?.run_id);
        const merged = queryRun && !hasCurrent
          ? [...own, { run_id: queryRun.run_id, question: queryRun.question, answer: queryRun.answer ?? null, status: queryRun.status }]
          : own;
        if (!cancelled) setTurns(merged);
      } catch { /* chat thread is best-effort; trace views stay authoritative */ }
    })();
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [conversationId, queryRun?.run_id, queryRun?.status, answer]);

  const loadReview = useCallback(async (documentId: string) => {
    const [versionsResponse, routesResponse] = await Promise.all([
      fetch(`${API}/api/documents/${documentId}/parses`),
      fetch(`${API}/api/documents/${documentId}/routes`),
    ]);
    if (!versionsResponse.ok) throw new Error("无法读取解析版本");
    if (!routesResponse.ok) throw new Error("无法读取解析路由");
    setVersions(await versionsResponse.json());
    setRoutes(await routesResponse.json());
  }, []);

  const active = useMemo(
    () => papers.find((paper) => paper.document_id === activePaper) ?? null,
    [papers, activePaper],
  );

  async function upload(file: File) {
    setUploading(true);
    setStatus(`正在上传 ${file.name}`);
    try {
      const form = new FormData();
      form.append("file", file);
      const response = await fetch(`${API}/api/documents`, { method: "POST", body: form });
      if (!response.ok) throw new Error(await response.text());
      const { job_id } = await response.json();
      for (; ;) {
        await new Promise((resolve) => setTimeout(resolve, 1800));
        const jobResponse = await fetch(`${API}/api/jobs/${job_id}`);
        const job = await jobResponse.json();
        setStatus(job.status === "parsing" ? "MinerU 解析与索引中…" : job.status);
        if (job.status === "ready") {
          await refresh();
          setActivePaper(job.document_id);
          setSelected((current) => [...new Set([...current, job.document_id])]);
          setStatus("解析完成，已可检索");
          break;
        }
        if (job.status === "failed") throw new Error(job.error ?? "解析失败");
      }
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "上传失败");
    } finally {
      setUploading(false);
      if (fileRef.current) fileRef.current.value = "";
    }
  }

  async function ask(event: FormEvent) {
    event.preventDefault();
    const prompt = question.trim();
    if (!prompt || busy) return;
    setBusy(true);
    setAnswer(null);
    setStreamedAnswer("");
    setQueryRun(null);
    setRunEvents([]);
    setEvaluationEvents([]);
    setEvaluationJob(null);
    setEvaluation(null);
    setEvaluationBusy(false);
    setLastQuestion(prompt);
    setActiveView("runs");
    setStatus("正在检索、重排并生成…");
    try {
      eventSourceRef.current?.close();
      evaluationEventSourceRef.current?.close();
      const response = await fetch(`${API}/api/query-runs`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          question: prompt,
          document_ids: selected.length ? selected : null,
          agentic: answerMode !== "rag",
          tool_mode: answerMode === "tool",
          conversation_id: conversationId,
          parent_run_id: queryRun?.run_id ?? null,
        }),
      });
      if (!response.ok) throw new Error(await response.text());
      const run: QueryRunSnapshot = await response.json();
      setQueryRun(run);
      setQuestion(""); // 提交成功，清空输入框便于下一次提问
      setStatus("已建立执行轨迹，正在确定检索范围…");
      watchQueryRun(run.run_id);
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "问答失败");
      setBusy(false);
    }
  }

  function watchQueryRun(runId: string) {
    const source = new EventSource(`${API}/api/query-runs/${encodeURIComponent(runId)}/events`);
    eventSourceRef.current = source;
    source.onmessage = (message) => {
      const event: TraceEvent = JSON.parse(message.data);
      setRunEvents((current) => current.some((item) => item.seq === event.seq)
        ? current
        : [...current, event].sort((left, right) => left.seq - right.seq));
      if (event.type === "run.started") {
        setQueryRun((current) => current ? { ...current, status: "running" } : current);
      }
      if (event.type === "generation.delta" && typeof event.output.text_delta === "string") {
        setStreamedAnswer((current) => current + event.output.text_delta);
      }
      if (["run.completed", "run.failed", "run.cancelled"].includes(event.type)) {
        source.close();
        if (eventSourceRef.current === source) eventSourceRef.current = null;
        void finishQueryRun(runId);
      } else if (event.status === "running") {
        setStatus(`${event.label}…`);
      }
    };
    source.onerror = () => {
      if (eventSourceRef.current === source) setStatus("执行轨迹连接中断，正在自动重连…");
    };
  }

  async function finishQueryRun(runId: string) {
    try {
      const [response, eventsResponse] = await Promise.all([
        fetch(`${API}/api/query-runs/${encodeURIComponent(runId)}`),
        fetch(`${API}/api/query-runs/${encodeURIComponent(runId)}/event-log`),
      ]);
      if (!response.ok) throw new Error(await response.text());
      const run: QueryRunSnapshot = await response.json();
      if (eventsResponse.ok) {
        const events: TraceEvent[] = await eventsResponse.json();
        setRunEvents(events.filter((event) => !event.stage_id.startsWith("evaluation.")));
        setEvaluationEvents(events.filter((event) => event.stage_id.startsWith("evaluation.")));
      }
      setQueryRun(run);
      if (run.answer) {
        setAnswer(run.answer);
        setStreamedAnswer("");
      }
      setStatus(run.status === "completed" ? "回答完成，完整执行轨迹已保存" :
        run.status === "cancelled" ? "问答已取消" : run.error ?? "问答失败");
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "无法读取问答结果");
    } finally {
      setBusy(false);
    }
  }

  async function cancelQueryRun() {
    if (!queryRun || !["queued", "running"].includes(queryRun.status)) return;
    setStatus("正在取消问答…");
    try {
      const response = await fetch(
        `${API}/api/query-runs/${encodeURIComponent(queryRun.run_id)}/cancel`,
        { method: "POST" },
      );
      if (!response.ok) throw new Error(await response.text());
      const run: QueryRunSnapshot = await response.json();
      setQueryRun(run);
      if (["cancelled", "failed", "completed"].includes(run.status)) {
        eventSourceRef.current?.close();
        eventSourceRef.current = null;
        await finishQueryRun(run.run_id);
      }
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "取消失败");
    }
  }

  async function openRunHistory() {
    setHistoryOpen(true);
    setHistoryBusy(true);
    try {
      const response = await fetch(`${API}/api/query-runs?limit=50`);
      if (!response.ok) throw new Error(await response.text());
      setHistoryRuns(await response.json());
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "无法读取历史轨迹");
    } finally {
      setHistoryBusy(false);
    }
  }

  async function restoreQueryRun(runId: string) {
    setHistoryBusy(true);
    try {
      eventSourceRef.current?.close();
      evaluationEventSourceRef.current?.close();
      const [runResponse, eventsResponse, evaluationResponse] = await Promise.all([
        fetch(`${API}/api/query-runs/${encodeURIComponent(runId)}`),
        fetch(`${API}/api/query-runs/${encodeURIComponent(runId)}/event-log`),
        fetch(`${API}/api/query-runs/${encodeURIComponent(runId)}/evaluations/latest`),
      ]);
      if (!runResponse.ok || !eventsResponse.ok) throw new Error("无法恢复该问答轨迹");
      const run: QueryRunSnapshot = await runResponse.json();
      const events: TraceEvent[] = await eventsResponse.json();
      const restoredEvaluationJob: EvaluationJob | null = evaluationResponse.ok
        ? await evaluationResponse.json()
        : null;
      setQueryRun(run);
      setRunEvents(events.filter((event) => !event.stage_id.startsWith("evaluation.")));
      setEvaluationEvents(events.filter((event) => event.stage_id.startsWith("evaluation.")));
      setEvaluationJob(restoredEvaluationJob);
      setEvaluation(restoredEvaluationJob?.result ?? null);
      if (run.conversation_id) setConversationId(run.conversation_id);
      setAnswer(run.answer);
      setStreamedAnswer(run.answer ? "" : events
        .filter((event) => event.type === "generation.delta" && typeof event.output.text_delta === "string")
        .map((event) => String(event.output.text_delta)).join(""));
      setHistoryOpen(false);
      setActiveView("runs");
      const running = ["queued", "running"].includes(run.status);
      setBusy(running);
      if (running) watchQueryRun(run.run_id);
      const evaluationRunning = restoredEvaluationJob != null
        && ["queued", "running"].includes(restoredEvaluationJob.status);
      setEvaluationBusy(evaluationRunning);
      if (evaluationRunning) watchEvaluationJob(restoredEvaluationJob.job_id);
      setStatus(evaluationRunning ? "已恢复运行中的 Ragas 评测" :
        running ? "已恢复运行中的问答轨迹" : "已打开历史问答轨迹");
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "无法恢复问答轨迹");
    } finally {
      setHistoryBusy(false);
    }
  }

  async function runEvaluation() {
    if (!lastQuestion || busy || evaluationBusy) return;
    setEvaluationBusy(true);
    setEvaluation(null);
    setEvaluationEvents([]);
    setStatus("正在创建后台 Ragas 评测任务…");
    try {
      const response = await fetch(`${API}/api/evaluations`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          question: lastQuestion,
          document_ids: selected.length ? selected : null,
          query_run_id: queryRun?.run_id ?? null,
        }),
      });
      if (!response.ok) throw new Error(await response.text());
      const job: EvaluationJob = await response.json();
      setEvaluationJob(job);
      if (job.result) setEvaluation(job.result);
      if (["queued", "running"].includes(job.status)) {
        setStatus("Ragas 后台评测已启动，可继续浏览论文…");
        watchEvaluationJob(job.job_id);
      } else {
        setEvaluationBusy(false);
        setStatus(job.status === "completed" ? "评估完成并已保存" : job.error ?? "评估失败");
      }
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "评估失败");
      setEvaluationBusy(false);
    }
  }

  function watchEvaluationJob(jobId: string) {
    evaluationEventSourceRef.current?.close();
    const source = new EventSource(
      `${API}/api/evaluations/${encodeURIComponent(jobId)}/events`,
    );
    evaluationEventSourceRef.current = source;
    let terminalSeen = false;
    source.onmessage = (message) => {
      const event: TraceEvent = JSON.parse(message.data);
      setEvaluationEvents((current) => current.some((item) => item.event_id === event.event_id)
        ? current
        : [...current, event].sort((left, right) => left.seq - right.seq));
      if (["evaluation.completed", "evaluation.failed", "evaluation.cancelled"].includes(event.type)) {
        terminalSeen = true;
        source.close();
        if (evaluationEventSourceRef.current === source) evaluationEventSourceRef.current = null;
        void finishEvaluationJob(jobId);
      } else if (event.status === "running") {
        setStatus(`${event.label}…`);
      }
    };
    source.onerror = () => {
      if (!terminalSeen && evaluationEventSourceRef.current === source) {
        setStatus("Ragas 轨迹连接中断，正在自动重连…");
      }
    };
  }

  async function finishEvaluationJob(jobId: string) {
    try {
      const response = await fetch(`${API}/api/evaluations/${encodeURIComponent(jobId)}`);
      if (!response.ok) throw new Error(await response.text());
      const job: EvaluationJob = await response.json();
      setEvaluationJob(job);
      setEvaluation(job.result);
      setStatus(job.status === "completed" ? "Ragas 评估完成并已保存" :
        job.status === "cancelled" ? "Ragas 评估已取消" : job.error ?? "Ragas 评估失败");
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "无法读取 Ragas 评估结果");
    } finally {
      setEvaluationBusy(false);
    }
  }

  async function cancelEvaluation() {
    if (!evaluationJob || !["queued", "running"].includes(evaluationJob.status)) return;
    setStatus("正在取消 Ragas 评估…");
    try {
      const response = await fetch(
        `${API}/api/evaluations/${encodeURIComponent(evaluationJob.job_id)}/cancel`,
        { method: "POST" },
      );
      if (!response.ok) throw new Error(await response.text());
      const job: EvaluationJob = await response.json();
      setEvaluationJob(job);
      setEvaluation(job.result);
      evaluationEventSourceRef.current?.close();
      evaluationEventSourceRef.current = null;
      setEvaluationBusy(false);
      setStatus("Ragas 评估已取消，问答结果不受影响");
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "取消 Ragas 评估失败");
    }
  }

  function openCitation(citation: Citation) {
    setActivePaper(citation.document_id);
    setPage(citation.page_number);
    setHighlight(citation.bbox);
    setActiveView("reader");
  }

  function openEvidence(evidence: EvidencePointer) {
    setActivePaper(evidence.document_id);
    setPage(evidence.page_number);
    setHighlight(evidence.bbox);
    setActiveView("reader");
  }

  async function openReview() {
    if (!activePaper) return;
    setReviewOpen(true);
    setReviewTarget(null);
    setReviewReason("");
    setReviewForce(false);
    try {
      await loadReview(activePaper);
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "无法读取解析版本");
    }
  }

  async function activateParse(parseId: string) {
    if (!activePaper || reviewReason.trim().length < 3 || reviewBusy) return;
    setReviewBusy(true);
    setStatus("正在切换解析版本并记录复核证据…");
    try {
      const response = await fetch(
        `${API}/api/documents/${activePaper}/parses/${encodeURIComponent(parseId)}/activate`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ reason: reviewReason.trim(), force: reviewForce }),
        },
      );
      if (!response.ok) {
        const payload = await response.json().catch(() => null);
        throw new Error(payload?.detail ?? "解析版本切换失败");
      }
      await Promise.all([refresh(), loadReview(activePaper)]);
      setReviewTarget(null);
      setReviewReason("");
      setReviewForce(false);
      setPage(1);
      setHighlight(null);
      setStatus("解析版本已激活，复核记录已保存");
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "解析版本切换失败");
    } finally {
      setReviewBusy(false);
    }
  }

  async function loadDashboard() {
    setDashboard(null);
    const response = await fetch(`${API}/api/dashboard`);
    if (!response.ok) throw new Error("无法读取质量看板");
    setDashboard(await response.json());
  }

  async function openDashboard() {
    setDashboardOpen(true);
    await loadDashboard();
  }

  async function savePersonal() {
    if (!activePaper) return;
    const response = await fetch(`${API}/api/documents/${activePaper}/personal`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(personal),
    });
    if (!response.ok) throw new Error("无法保存阅读记录");
    setStatus("笔记、标签与阅读状态已保存");
  }

  const viewMeta: Record<CockpitView, { eyebrow: string; title: string; description: string }> = {
    overview: { eyebrow: "Workspace", title: "Cockpit", description: "论文、检索与评估系统的实时总览" },
    library: { eyebrow: "Knowledge", title: "论文库", description: "管理论文范围、阅读状态与个人笔记" },
    reader: { eyebrow: "Evidence", title: "论文精读", description: "查看原始页面并定位回答引用" },
    runs: { eyebrow: "Runtime", title: "运行轨迹", description: "观察 Agent 从问题到证据与回答的完整流程" },
    evaluation: { eyebrow: "Quality", title: "评估中心", description: "汇总 Ragas 指标、延迟、费用与回归状态" },
  };
  const totalChunks = papers.reduce((sum, paper) => sum + paper.chunk_count, 0);
  const currentView = viewMeta[activeView];

  return (
    <main className="shell">
      <header className="topbar">
        <div className="brand">
          <span className="mark">S</span>
          <div><strong>ScholarRAG</strong><small>RESEARCH COCKPIT</small></div>
        </div>
        <div className="status"><span className={busy || uploading ? "pulse" : "dot"} />{status}</div>
        <button className="upload" onClick={() => fileRef.current?.click()} disabled={uploading}>
          {uploading ? "处理中…" : "+ 上传论文"}
        </button>
        <input ref={fileRef} type="file" accept="application/pdf" hidden onChange={(event) => {
          const file = event.target.files?.[0]; if (file) void upload(file);
        }} />
      </header>

      <section className="workspace">
        <aside className="cockpitNav">
          <nav>
            <NavGroup label="Workspace">
              <NavButton active={activeView === "overview"} icon="◫" label="Cockpit" onClick={() => setActiveView("overview")} />
              <NavButton active={activeView === "library"} icon="▤" label="论文库" badge={String(papers.length)} onClick={() => setActiveView("library")} />
              <NavButton active={activeView === "reader"} icon="◧" label="论文精读" onClick={() => setActiveView("reader")} />
            </NavGroup>
            <NavGroup label="Agent">
              <NavButton active={activeView === "runs"} icon="⌁" label="运行轨迹" running={busy} onClick={() => setActiveView("runs")} />
              <NavButton active={activeView === "evaluation"} icon="◇" label="Ragas 评估" running={evaluationBusy} onClick={() => {
                setActiveView("evaluation");
                if (!dashboard) void loadDashboard().catch((error) => setStatus(error.message));
              }} />
              <NavButton icon="↺" label="历史记录" onClick={() => void openRunHistory()} />
            </NavGroup>
            <NavGroup label="System">
              <NavButton icon="⎚" label="解析与路由" onClick={() => activePaper ? void openReview() : setStatus("请先选择一篇论文")} />
              <NavButton icon="✓" label="验收工作台" onClick={() => setAcceptanceOpen(true)} />
              <NavButton icon="↗" label="独立质量看板" onClick={() => void openDashboard().catch((error) => setStatus(error.message))} />
            </NavGroup>
          </nav>
          <div className="navContext">
            <span>ACTIVE SCOPE</span>
            <b>{selected.length || papers.length} 篇论文</b>
            <small>{active?.title || "尚未选择当前论文"}</small>
          </div>
        </aside>

        <section className="cockpitMain">
          <header className="cockpitHead">
            <div><span className="eyebrow">{currentView.eyebrow}</span><h1>{currentView.title}</h1><p>{currentView.description}</p></div>
            <div className="cockpitHeadActions">
              {activeView === "reader" && active && <button onClick={() => void openReview()}>解析版本</button>}
              {activeView === "runs" && <button onClick={() => void openRunHistory()}>历史轨迹</button>}
              {activeView === "evaluation" && <button onClick={() => void loadDashboard().catch((error) => setStatus(error.message))}>刷新数据</button>}
              <span className="systemLive"><i /> LIVE</span>
            </div>
          </header>

          <div className="cockpitViewport">
            {activeView === "overview" && <div className="overviewView">
              <div className="overviewStats">
                <article><span>PAPERS</span><b>{papers.length}</b><small>{selected.length || papers.length} in query scope</small></article>
                <article><span>CHUNKS</span><b>{totalChunks.toLocaleString()}</b><small>text + visual index</small></article>
                <article><span>ACTIVE RUN</span><b className={busy ? "liveValue" : ""}>{busy ? "RUNNING" : queryRun?.status?.toUpperCase() ?? "IDLE"}</b><small>{runEvents.length} trace events</small></article>
                <article><span>RAGAS</span><b>{evaluation ? "READY" : evaluationBusy ? "RUNNING" : "—"}</b><small>{evaluation ? `${Object.keys(evaluation.metrics).length} metrics` : "run-level evaluation"}</small></article>
              </div>
              <section className="architectureCard">
                <header><div><span className="eyebrow">Live architecture</span><h2>从论文到可追溯回答</h2></div><span>API pipeline</span></header>
                <div className="architectureMap">
                  <ArchitectureLane number="01" title="Ingest" detail="PDF → structured blocks" nodes={["MinerU / Paddle", "Layout + OCR", "Artifact store"]} />
                  <ArchitectureLane number="02" title="Index" detail="dual multimodal index" nodes={["Qwen3-VL Embed", "Text + Image", "Vector / FTS"]} />
                  <ArchitectureLane number="03" title="Retrieve" detail="hybrid evidence search" nodes={["Query planning", "VL Reranker", "Evidence spans"]} active={busy} />
                  <ArchitectureLane number="04" title="Answer" detail="grounded generation" nodes={["Qwen3.6-27B", "Citations", "Trace persist"]} active={busy} />
                  <ArchitectureLane number="05" title="Evaluate" detail="automatic RAG quality" nodes={["Ragas", "Qwen3.6 Judge", "Regression"]} active={evaluationBusy} />
                </div>
              </section>
              <div className="overviewBottom">
                <section><header><b>当前论文</b><button onClick={() => setActiveView("library")}>管理范围 →</button></header>{active ? <div className="activePaperSummary"><span>{personal.favorite ? "★" : "S"}</span><div><b>{active.title || active.source_filename}</b><small>{active.provider_model} · {active.page_count ?? "?"} pages · {active.chunk_count} chunks</small></div><button onClick={() => setActiveView("reader")}>打开</button></div> : <p>上传或选择一篇论文开始。</p>}</section>
                <section><header><b>最近运行</b><button onClick={() => setActiveView("runs")}>查看 Trace →</button></header>{queryRun ? <div className="recentRun"><span className={`runState ${queryRun.status}`}>{queryRun.status}</span><div><b>{queryRun.question}</b><small>{queryRun.event_count} events · {queryRun.trace_id.slice(0, 12)}</small></div></div> : <p>发送第一个问题后，这里会显示运行状态。</p>}</section>
              </div>
            </div>}

            {activeView === "library" && <div className="libraryView">
              <section className="libraryCatalog">
                <div className="libraryToolbar"><div><b>{papers.length} 篇论文</b><span>勾选多篇进行跨论文问答</span></div><button onClick={() => setSelected(papers.map((paper) => paper.document_id))}>全选</button><button onClick={() => setSelected([])}>清空范围</button></div>
                <div className="paperGrid">{papers.map((paper) => <article key={paper.document_id} className={activePaper === paper.document_id ? "paperTile active" : "paperTile"} onClick={() => { setActivePaper(paper.document_id); setPage(1); setHighlight(null); }}>
                  <label onClick={(event) => event.stopPropagation()}><input type="checkbox" checked={selected.includes(paper.document_id)} onChange={(event) => setSelected((current) => event.target.checked ? [...new Set([...current, paper.document_id])] : current.filter((id) => id !== paper.document_id))} /><span>问答范围</span></label>
                  <div className="paperGlyph">PDF</div><h3>{paper.title || paper.source_filename}</h3><p>{paper.provider_model}</p><footer><span>{paper.page_count ?? "?"} pages</span><span>{paper.chunk_count} chunks</span></footer>
                  <button onClick={(event) => { event.stopPropagation(); setActivePaper(paper.document_id); setActiveView("reader"); setPage(1); }}>精读 →</button>
                </article>)}</div>
                {!papers.length && <div className="empty">上传第一篇 PDF 开始构建论文库。</div>}
              </section>
              {active && <aside className="personalCard libraryNotes"><div><b>个人阅读记录</b><button onClick={() => setPersonal((value) => ({ ...value, favorite: !value.favorite }))}>{personal.favorite ? "★" : "☆"}</button></div><small>{active.title || active.source_filename}</small>
                <select value={personal.reading_status} onChange={(event) => setPersonal((value) => ({ ...value, reading_status: event.target.value }))}><option value="unread">未读</option><option value="reading">阅读中</option><option value="finished">已完成</option></select>
                <input value={personal.tags.join(", ")} onChange={(event) => setPersonal((value) => ({ ...value, tags: event.target.value.split(",").map((item) => item.trim()).filter(Boolean) }))} placeholder="标签，以逗号分隔" />
                <textarea rows={8} value={personal.note} onChange={(event) => setPersonal((value) => ({ ...value, note: event.target.value }))} placeholder="论文笔记…" />
                <button onClick={() => void savePersonal().catch((error) => setStatus(error.message))}>保存记录</button></aside>}
            </div>}

            {activeView === "reader" && <section className="reader cockpitReader">
              <div className="panelHead">
                <div><span className="eyebrow">SOURCE PDF</span><h2>{active?.title ?? "尚未选择论文"}</h2></div>
                {active && <div className="readerActions"><select value={active.document_id} onChange={(event) => { setActivePaper(event.target.value); setPage(1); setHighlight(null); }}>{papers.map((paper) => <option key={paper.document_id} value={paper.document_id}>{paper.title || paper.source_filename}</option>)}</select><div className="pageControls">
                  <button onClick={() => { setPage(Math.max(1, page - 1)); setHighlight(null); }}>‹</button><span className="pageChip">第 {page} / {active.page_count ?? "?"} 页</span><button onClick={() => { setPage(Math.min(active.page_count ?? page + 1, page + 1)); setHighlight(null); }}>›</button>
                </div></div>}
              </div>
              {active ? <div className="pageStage"><div className="pageImage"><img alt={`第 ${page} 页`} src={`${API}/api/documents/${active.document_id}/pages/${page}.png`} />{highlight && <span className="bbox" style={{ left: `${highlight.x0 / 10}%`, top: `${highlight.y0 / 10}%`, width: `${(highlight.x1 - highlight.x0) / 10}%`, height: `${(highlight.y1 - highlight.y0) / 10}%` }} />}</div><a className="originalPdf" href={`${API}/api/documents/${active.document_id}/pdf`} target="_blank">打开原始 PDF ↗</a></div> : <div className="readerEmpty"><button onClick={() => setActiveView("library")}>先从论文库选择论文</button></div>}
            </section>}

            {activeView === "runs" && <div className="runsView">{queryRun ? <RunFlow run={queryRun} events={[...runEvents, ...evaluationEvents]} onCancel={() => void cancelQueryRun()} onEvidenceSelect={openEvidence} /> : <div className="viewEmpty"><span>⌁</span><h2>尚无运行轨迹</h2><p>在右侧 Chat Dock 提问后，检索、重排、生成和持久化过程会实时显示在这里。</p><button onClick={() => setQuestion("这篇论文的核心贡献、方法和主要实验结论是什么？")}>准备精读问题</button></div>}</div>}

            {activeView === "evaluation" && <div className="evaluationView">{!dashboard ? <div className="viewEmpty"><span className={evaluationBusy ? "pulse" : ""}>◇</span><h2>正在聚合评估数据</h2><p>读取持久化 Query Run、Ragas artifact、组件延迟与成本。</p></div> : <Dashboard value={dashboard} />}{evaluation && <section className="currentEvaluation"><header><b>当前问题的 Ragas 结果</b><code>{evaluation.run_id}</code></header><div className="metrics">{Object.entries(evaluation.metrics).map(([name, score]) => <div key={name}><span>{name.replaceAll("_", " ")}</span><b>{typeof score.value === "number" ? score.value.toFixed(3) : String(score.value)}</b></div>)}</div></section>}</div>}
          </div>
        </section>

        <section className="assistantPanel">
          <div className="panelHead"><div><span className="eyebrow">Persistent Chat Dock</span><h2>论文助手</h2></div><div className="scopeActions"><button title="新建会话" onClick={() => { setConversationId(crypto.randomUUID()); setTurns([]); setQueryRun(null); setAnswer(null); setRunEvents([]); setEvaluationEvents([]); setEvaluationJob(null); setEvaluation(null); setEvaluationBusy(false); evaluationEventSourceRef.current?.close(); setStatus("已新建会话"); }}>＋</button><button title="历史轨迹" onClick={() => void openRunHistory()}>↺</button><span className="scope">{selected.length || papers.length} 篇</span></div></div>
          <div className="conversation" ref={conversationRef}>
            {!turns.length && !answer && !busy && !queryRun && <div className="welcome"><span>⌁</span><h3>从证据出发，而不是凭印象回答</h3>
              <p>询问方法、公式、实验对比或跨论文差异。每条引用都能回到原页。</p>
              <button onClick={() => setQuestion("这篇论文的核心贡献、方法和主要实验结论是什么？")}>生成精读提纲</button></div>}
            {turns.map((turn) => {
              const isCurrent = queryRun?.run_id === turn.run_id;
              const live = isCurrent && ["queued", "running"].includes(queryRun!.status);
              return <div className="chatTurn" key={turn.run_id}>
                <div className="chatQuestion">{turn.question}</div>
                {turn.status !== "completed" && !live && <span className={`turnState ${turn.status}`}>{turn.status}</span>}
                {isCurrent && queryRun && <button className="chatRunPeek" onClick={() => setActiveView("runs")}><span className={`runState ${queryRun.status}`}>{queryRun.status}</span><div><b>运行轨迹</b><small>{runEvents.length + evaluationEvents.length} events · 点击在中央查看</small></div><span>→</span></button>}
                {isCurrent && <AgentTraceCompact events={runEvents} running={live} />}
                {isCurrent && !answer && !turn.answer && streamedAnswer && <div className="answerText streamingAnswer"><ReactMarkdown remarkPlugins={[remarkGfm, remarkMath]} rehypePlugins={[rehypeKatex]}>{streamedAnswer}</ReactMarkdown><span className="streamCursor" /></div>}
                {(() => {
                  const turnAnswer = isCurrent ? (answer ?? turn.answer) : turn.answer;
                  if (!turnAnswer || live) return null;
                  return <>
                    <div className="answerText"><ReactMarkdown remarkPlugins={[remarkGfm, remarkMath]} rehypePlugins={[rehypeKatex]}>{turnAnswer.answer}</ReactMarkdown></div>
                    <div className="citations"><div className="subhead">引用证据</div>
                      {turnAnswer.citations.map((citation) => <button key={citation.citation_id} onClick={() => openCitation(citation)}>
                        <b>{citation.citation_id}</b><span><strong>第 {citation.page_number} 页</strong>{plainQuote(citation.quote)}</span>
                        <em>{citation.score == null ? "" : citation.score.toFixed(3)}</em></button>)}
                    </div>
                    {turnAnswer.claims?.length > 0 && <details className="claimEvidence"><summary>Claim → evidence span · {turnAnswer.claims.length}</summary>
                      {turnAnswer.claims.map((claim) => <article key={claim.claim_id}><div><b>{claim.claim_id}</b><span className={claim.supported ? "supported" : "unsupported"}>{claim.supported ? "已定位" : "缺少证据"}</span></div><p>{claim.text}</p>
                        {claim.evidence_spans.map((span) => <button key={`${claim.claim_id}-${span.citation_id}`} onClick={() => {
                          const citation = turnAnswer.citations.find((item) => item.citation_id === span.citation_id);
                          if (citation) openCitation({ ...citation, page_number: span.page_number, bbox: span.bbox });
                        }}><b>{span.citation_id} · 第 {span.page_number} 页</b><span>{plainQuote(span.quote)}</span></button>)}</article>)}</details>}
                    {isCurrent && <>
                      <details className="trace"><summary>查看完整检索轨迹 <code>{turnAnswer.trace_id.slice(0, 12)}</code></summary>
                        <pre>{JSON.stringify(turnAnswer.retrieval_debug, null, 2)}</pre></details>
                      <div className="evaluationActions">
                        <button className="evaluate" onClick={() => void runEvaluation()} disabled={busy || evaluationBusy}>{evaluationBusy ? "Ragas 评测运行中…" : evaluation ? "重新用 Ragas 复评" : "用 Ragas 复评本问题"}</button>
                        {evaluationBusy && <button className="cancelEvaluation" onClick={() => void cancelEvaluation()}>取消评测</button>}
                      </div>
                      {evaluationJob && <div className={`evaluationJob evaluation-${evaluationJob.status}`}><span>{evaluationJob.status}</span><code>{evaluationJob.job_id.slice(0, 18)}</code>{evaluationJob.error && <small>{evaluationJob.error}</small>}</div>}
                      {evaluation && <div className="metrics">{Object.entries(evaluation.metrics).map(([name, score]) =>
                        <div key={name}><span>{name.replaceAll("_", " ")}</span><b>{typeof score.value === "number" ? score.value.toFixed(3) : String(score.value)}</b></div>)}</div>}
                    </>}
                    <a className="exportReport" href={`${API}/api/query-runs/${encodeURIComponent(turn.run_id)}/export`}>导出证据报告.md</a>
                  </>;
                })()}
              </div>;
            })}
            {busy && !queryRun && <div className="thinking"><i /><i /><i /><span>正在建立执行轨迹</span></div>}
          </div>
          <form className="composer" onSubmit={ask}>
            <textarea value={question} onChange={(event) => setQuestion(event.target.value)} onKeyDown={(event) => {
              // Ignore Enter while the IME is composing (e.g. 拼音选词),
              // otherwise confirming a candidate submits the question.
              if (event.nativeEvent.isComposing || event.keyCode === 229) return;
              if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                event.currentTarget.form?.requestSubmit();
              }
            }}
              placeholder="询问论文中的方法、公式、实验或局限…" rows={3} />
            <div><span className="modeSwitch" role="radiogroup" aria-label="回答模式">
              {([["tool", "工具 Agent", "模型自主调用 list_papers / get_paper / retrieve / read_chunk"], ["multi", "多步规划", "受控 Planner 拆解子问题并迭代检索"], ["rag", "单轮检索", "一次混合检索直接生成，最快"]] as const).map(([value, label, tip]) =>
                <button type="button" key={value} title={tip} role="radio" aria-checked={answerMode === value} className={answerMode === value ? "on" : ""} onClick={() => setAnswerMode(value)}>{label}</button>)}
            </span><span>Enter 提问 · Shift+Enter 换行</span><button type="submit" disabled={busy || !question.trim()}>发送</button></div>
          </form>
        </section>
      </section>
      {historyOpen && <div className="reviewBackdrop" onMouseDown={() => setHistoryOpen(false)}>
        <section className="historyPanel" onMouseDown={(event) => event.stopPropagation()}>
          <header><div><span className="eyebrow">Persisted query runs</span><h2>历史问答轨迹</h2><p>重新打开回答、组件事件和候选证据。</p></div><button onClick={() => setHistoryOpen(false)}>×</button></header>
          <div className="historyBody">{historyBusy && <p>正在读取…</p>}{!historyBusy && historyRuns.length === 0 && <p>暂无历史问答。</p>}
            {historyRuns.map((run) => <button className="historyRun" key={run.run_id} onClick={() => void restoreQueryRun(run.run_id)}>
              <span className={`runState ${run.status}`}>{run.status}</span><div><b>{run.question}</b><small>{new Date(run.created_at).toLocaleString()} · {run.event_count} events</small></div><code>{run.trace_id.slice(0, 12)}</code>
            </button>)}</div>
        </section>
      </div>}
      {dashboardOpen && <div className="reviewBackdrop" onMouseDown={() => setDashboardOpen(false)}><section className="historyPanel dashboardPanel" onMouseDown={(event) => event.stopPropagation()}>
        <header><div><span className="eyebrow">Quality · latency · cost</span><h2>质量与回归看板</h2><p>由持久化 parse、Query Run 与评测 artifact 聚合。</p></div><button onClick={() => setDashboardOpen(false)}>×</button></header>
        <div className="dashboardBody">{!dashboard ? <p>正在聚合 artifacts…</p> : <Dashboard value={dashboard} />}</div>
      </section></div>}
      {acceptanceOpen && <ReviewWorkbench api={API} onClose={() => setAcceptanceOpen(false)} onStatus={setStatus} />}
      {reviewOpen && <div className="reviewBackdrop" onMouseDown={() => setReviewOpen(false)}>
        <section className="reviewPanel" onMouseDown={(event) => event.stopPropagation()}>
          <header><div><span className="eyebrow">Parse provenance</span><h2>解析版本与人工复核</h2>
            <p>{active?.title ?? active?.source_filename}</p></div><button onClick={() => setReviewOpen(false)}>×</button></header>
          <div className="reviewBody">
            <div className="reviewNotice">只有被激活的 parse 会进入检索。切换到尚未索引的候选可能产生 embedding 费用；所有操作都会保存不可变复核记录。</div>
            <div className="versionList">{versions.map((version) => {
              const quality = version.quality;
              const isTarget = reviewTarget === version.parse_id;
              return <article key={version.parse_id} className={version.active ? "version activeVersion" : "version"}>
                <div className="versionTop"><div><b>{version.provider_model}</b><code>{version.parse_id}</code></div>
                  <span className={`decision ${quality?.decision ?? "unknown"}`}>{version.active ? "ACTIVE" : quality?.decision ?? version.status}</span></div>
                <div className="versionMeta"><span>{version.page_count ?? "?"} 页</span><span>{version.block_count ?? "?"} blocks</span><span>{version.chunk_count ?? "?"} chunks</span>
                  <span>质量 {typeof quality?.score === "number" ? quality.score.toFixed(4) : "—"}</span><span>{version.status}</span></div>
                {!version.active && version.can_activate && !isTarget && <button className="selectVersion" onClick={() => { setReviewTarget(version.parse_id); setReviewReason(""); setReviewForce(false); }}>复核并激活</button>}
                {isTarget && <div className="reviewForm"><textarea rows={3} value={reviewReason} onChange={(event) => setReviewReason(event.target.value)} placeholder="说明选择该解析版本的依据，例如公式、表格和阅读顺序抽查结果…" />
                  {quality?.decision === "fallback" && <label><input type="checkbox" checked={reviewForce} onChange={(event) => setReviewForce(event.target.checked)} />我已人工检查，确认覆盖质量门禁</label>}
                  <div><button onClick={() => setReviewTarget(null)}>取消</button><button className="confirmReview" disabled={reviewBusy || reviewReason.trim().length < 3 || (quality?.decision === "fallback" && !reviewForce)} onClick={() => void activateParse(version.parse_id)}>{reviewBusy ? "处理中…" : "确认激活并记录"}</button></div></div>}
              </article>;
            })}</div>
            <div className="routeList"><h3>路由审计记录</h3>{routes.length === 0 && <p>该论文暂无自动路由记录。</p>}
              {routes.map((route, index) => <details key={route.route_id ?? index}><summary><span>{route.recommended_action ?? "unknown"}</span><code>{route.route_id?.slice(0, 20)}</code></summary>
                <p>{route.reason}</p><pre>{JSON.stringify({ selected: route.selected_parse_id, execution: route.execution, comparison: route.comparison }, null, 2)}</pre></details>)}</div>
          </div>
        </section>
      </div>}
    </main>
  );
}

function NavGroup({ label, children }: { label: string; children: ReactNode }) {
  return <section className="navGroup"><h2>{label}</h2><div>{children}</div></section>;
}

function NavButton({ active = false, icon, label, badge, running = false, onClick }: {
  active?: boolean;
  icon: string;
  label: string;
  badge?: string;
  running?: boolean;
  onClick: () => void;
}) {
  return <button className={active ? "navButton active" : "navButton"} onClick={onClick}>
    <span className="navIcon">{icon}</span><span>{label}</span>
    {running ? <i className="navRunning" /> : badge ? <em>{badge}</em> : null}
  </button>;
}

function ArchitectureLane({ number, title, detail, nodes, active = false }: {
  number: string;
  title: string;
  detail: string;
  nodes: string[];
  active?: boolean;
}) {
  return <div className={active ? "architectureLane active" : "architectureLane"}>
    <div className="laneLabel"><span>{number}</span><div><b>{title}</b><small>{detail}</small></div></div>
    <div className="laneNodes">{nodes.map((node, index) => <div key={node}><span>{node}</span>{index < nodes.length - 1 && <i>→</i>}</div>)}</div>
  </div>;
}

function plainQuote(value: string) {
  return value
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/<[^>]+>/g, " ")
    .replace(/\s+/g, " ");
}

function Dashboard({ value }: { value: Record<string, unknown> }) {
  const query = value.query_runs as Record<string, unknown>;
  const evaluation = value.evaluation as Record<string, unknown>;
  const parsing = value.parsing as Record<string, unknown>;
  const latency = value.latency_ms as Record<string, { p50: number; p95: number; count: number }>;
  const metrics = evaluation.metric_means as Record<string, number>;
  const queryStatuses = query.status_counts as Record<string, number> | undefined;
  const parseDecisions = parsing.decision_counts as Record<string, number> | undefined;
  const stageFailures = value.stage_failure_counts as Record<string, number> | undefined;
  const versions = value.versions as Record<string, unknown> | undefined;
  const latencyRows = Object.entries(latency ?? {}).sort(([, left], [, right]) => right.p95 - left.p95);
  const failureRows = Object.entries(stageFailures ?? {}).sort(([, left], [, right]) => right - left);
  return <>
    <div className="dashboardCards">
      <div><span>Query runs</span><b>{String(query.count)}</b><small>{statusSummary(queryStatuses)}</small></div>
      <div><span>Eval runs</span><b>{String(evaluation.run_count)}</b><small>{Object.keys(metrics ?? {}).length} metrics</small></div>
      <div><span>Parses</span><b>{String(parsing.parse_count)}</b><small>{statusSummary(parseDecisions)}</small></div>
      <div><span>估算费用 CNY</span><b>{Number(query.estimated_cost_cny ?? 0).toFixed(4)}</b><small>基于已记录 usage</small></div>
    </div>

    <div className="dashboardHealth">
      <section>
        <header><span>运行状态</span><small>{String(query.count)} runs</small></header>
        <div className="statusBadges">{Object.entries(queryStatuses ?? {}).map(([status, count]) => <span className={`statusBadge ${status}`} key={status}><i />{status}<b>{count}</b></span>)}</div>
      </section>
      <section>
        <header><span>解析质量</span><small>mean {Number(parsing.mean_quality_score ?? 0).toFixed(3)}</small></header>
        <div className="statusBadges">{Object.entries(parseDecisions ?? {}).map(([status, count]) => <span className={`statusBadge ${status}`} key={status}><i />{status}<b>{count}</b></span>)}</div>
      </section>
      <section>
        <header><span>组件异常</span><small>{failureRows.reduce((sum, [, count]) => sum + count, 0)} events</small></header>
        {failureRows.length ? <div className="failureList">{failureRows.slice(0, 4).map(([stage, count]) => <span key={stage}><code title={stage}>{stage}</code><b>{count}</b></span>)}</div> : <p className="healthyMessage">没有记录到组件失败</p>}
      </section>
    </div>

    <h3>评测指标</h3>
    {Object.keys(metrics ?? {}).length ? <div className="metrics">{Object.entries(metrics).map(([name, score]) => <div key={name}><span>{name.replaceAll("_", " ")}</span><b>{score.toFixed(3)}</b><small>n={Number((evaluation.metric_samples as Record<string, number> | undefined)?.[name] ?? 0)}</small></div>)}</div> : <p className="dashboardEmpty">尚无 Ragas 评测结果</p>}

    <div className="dashboardSectionTitle"><h3>组件延迟</h3><span>按 P95 从慢到快排列</span></div>
    <div className="latencyTable">
      <div className="latencyHead"><span>Stage</span><span>P50</span><span>P95</span><span>Samples</span></div>
      {latencyRows.map(([stage, item]) => <div key={stage}><code title={stage}>{stage}</code><span>{formatLatency(item.p50)}</span><strong>{formatLatency(item.p95)}</strong><small>{item.count}</small></div>)}
    </div>

    <div className="modelStrip"><span>Embedding <b>{String(versions?.embedding_model ?? "—")}</b></span><span>Reranker <b>{String(versions?.rerank_model ?? "—")}</b></span><span>Answer / Judge <b>{String(versions?.chat_model ?? "—")}</b></span></div>

    <details className="rawSnapshot">
      <summary><span><b>原始聚合数据</b><small>用于调试、导出和字段核对</small></span><code>JSON · schema {String(value.schema_version ?? "—")}</code></summary>
      <pre>{JSON.stringify(value, null, 2)}</pre>
    </details>
  </>;
}

function statusSummary(values?: Record<string, number>) {
  const rows = Object.entries(values ?? {});
  return rows.length ? rows.map(([name, count]) => `${count} ${name}`).join(" · ") : "暂无分类数据";
}

function formatLatency(value: number) {
  if (value >= 1000) return `${(value / 1000).toFixed(value >= 10_000 ? 1 : 2)}s`;
  return `${value.toFixed(value >= 100 ? 0 : 1)}ms`;
}
