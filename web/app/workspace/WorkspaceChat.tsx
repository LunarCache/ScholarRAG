"use client";

import { FormEvent, useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { RunEvent, WorkspaceApi, WorkspaceApiError, WorkspacePaper, WorkspaceRun, workspaceError } from "../../lib/workspace-api";
import styles from "./workspace.module.css";

const terminal = (run: WorkspaceRun) => !["queued", "running"].includes(run.status);
const labels = { queued: "排队中", running: "回答中", completed: "已完成", failed: "失败", cancelled: "已取消" };
const sameScope = (a: string[], b: string[]) => [...a].sort().join("\n") === [...b].sort().join("\n");

export function WorkspaceChat({ client, workspaceId, papers, onError, onOpenPdf, selectionRequest, onRunningChange }: {
  client: WorkspaceApi; workspaceId: string; papers: WorkspacePaper[];
  selectionRequest: { ids: string[]; revision: number } | null; onRunningChange: (value: boolean) => void;
  onError: (error: unknown) => void; onOpenPdf: (id: string, page: number) => void;
}) {
  const [selected, setSelected] = useState(papers.map((paper) => paper.document_id));
  const [question, setQuestion] = useState("");
  const [mode, setMode] = useState("rag");
  const [history, setHistory] = useState<WorkspaceRun[]>([]);
  const [conversation, setConversation] = useState<WorkspaceRun[]>([]);
  const [run, setRun] = useState<WorkspaceRun | null>(null);
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [streamed, setStreamed] = useState("");
  const [error, setError] = useState("");
  const [connection, setConnection] = useState("");
  const [busy, setBusy] = useState(false);
  const [reconnect, setReconnect] = useState(0);
  const [report, setReport] = useState<string | null>(null);
  const scope = useRef<AbortController | null>(null);
  const activeView = useRef(0);
  const historyVersion = useRef(0);
  const cursor = useRef(0);
  const reportUrls = useRef(new Set<string>());
  const onErrorRef = useRef(onError); onErrorRef.current = onError;
  const runRef = useRef(run); runRef.current = run;

  function failure(reason: unknown) {
    setError(workspaceError(reason));
    if (reason instanceof WorkspaceApiError && [401, 403].includes(reason.status)) onErrorRef.current(reason);
  }

  useEffect(() => {
    const controller = new AbortController(); scope.current = controller;
    const version = ++historyVersion.current;
    client.runs(workspaceId, controller.signal).then((items) => {
      if (!controller.signal.aborted && version === historyVersion.current) setHistory(items);
    }).catch((reason) => { if (!controller.signal.aborted) failure(reason); });
    const urls = reportUrls.current;
    return () => { controller.abort(); activeView.current += 1; for (const url of urls) URL.revokeObjectURL(url); urls.clear(); };
  }, [client, workspaceId]);

  useEffect(() => {
    const allowed = new Set(papers.map((paper) => paper.document_id));
    setSelected((current) => current.filter((id) => allowed.has(id)));
    if (runRef.current && !runRef.current.document_ids.every((id) => allowed.has(id))) {
      onErrorRef.current(new WorkspaceApiError(403));
    }
  }, [papers]);

  useEffect(() => () => { if (report) { URL.revokeObjectURL(report); reportUrls.current.delete(report); } }, [report]);

  const isTerminal = !run || terminal(run);
  useEffect(() => { onRunningChange(!isTerminal || busy); }, [isTerminal, busy, onRunningChange]);
  useEffect(() => {
    if (!selectionRequest) return;
    if (runRef.current && !terminal(runRef.current)) { setError("请先停止当前问答，再使用集合切换范围。"); return; }
    setSelected(selectionRequest.ids.filter((id) => papers.some((paper) => paper.document_id === id)));
    activeView.current += 1; setRun(null); setConversation([]); setEvents([]); setStreamed(""); setReport(null); setError(""); setConnection("");
  }, [selectionRequest]);

  useEffect(() => {
    if (!run || isTerminal) return;
    const controller = new AbortController();
    const runId = run.run_id;
    let retry = 0;
    async function watch() {
      while (!controller.signal.aborted) {
        try {
          setConnection(retry ? "正在恢复连接…" : "实时连接中");
          cursor.current = await client.stream(workspaceId, runId, cursor.current, controller.signal, (frame) => {
            if (controller.signal.aborted) return;
            const item = frame.data as RunEvent;
            if (item.run_id !== runId || item.seq !== frame.id) throw new Error("Invalid run event");
            if (frame.id <= cursor.current) return;
            cursor.current = frame.id;
            setEvents((current) => [...current, item].slice(-500));
            if (item.type === "run.started") setRun((current) => current?.run_id === runId ? { ...current, status: "running" } : current);
            if (item.type === "generation.delta" && typeof item.output?.text_delta === "string") {
              setStreamed((current) => (current + item.output.text_delta).slice(0, 200000));
            }
            setConnection("正在接收实时事件");
          });
          const current = await client.run(workspaceId, runId, controller.signal);
          if (controller.signal.aborted) return;
          setRun(current);
          historyVersion.current += 1;
          setHistory((items) => [current, ...items.filter((item) => item.run_id !== current.run_id)]);
          if (terminal(current)) { setConnection("已同步持久化结果"); return; }
          retry = 0;
        } catch (reason) {
          if (controller.signal.aborted) return;
          if (reason instanceof WorkspaceApiError && [401, 403].includes(reason.status)) { failure(reason); return; }
          retry += 1;
          if (retry >= 5) { setConnection("连接中断，点击重新连接以恢复同一任务；不会重新发起模型调用。"); return; }
          setConnection("连接中断，正在恢复同一任务…");
        }
        await new Promise<void>((resolve) => {
          const done = () => { clearTimeout(timer); controller.signal.removeEventListener("abort", done); resolve(); };
          const timer = setTimeout(done, Math.min(1000 * 2 ** retry, 10000));
          controller.signal.addEventListener("abort", done, { once: true });
        });
      }
    }
    void watch();
    return () => controller.abort();
  }, [client, workspaceId, run?.run_id, isTerminal, reconnect]);

  async function loadRun(item: WorkspaceRun) {
    const signal = scope.current?.signal;
    if (!signal || signal.aborted || busy) return;
    const view = ++activeView.current;
    setBusy(true); setError(""); setReport(null); setConnection("正在恢复持久化记录…");
    // Stop the old stream immediately while the next view is being authorized.
    setRun(null); setEvents([]); setStreamed(""); setConversation([]);
    try {
      const [loaded, log] = await Promise.all([client.run(workspaceId, item.run_id, signal), client.runEvents(workspaceId, item.run_id, signal)]);
      const turns = await client.conversation(workspaceId, loaded.conversation_id, signal);
      if (signal.aborted || view !== activeView.current) return;
      cursor.current = log.at(-1)?.seq ?? 0;
      setEvents(log.slice(-500));
      setStreamed(log.filter((event) => event.type === "generation.delta").map((event) => event.output.text_delta ?? "").join("").slice(0, 200000));
      setConversation(turns.sort((a, b) => a.created_at.localeCompare(b.created_at)));
      setSelected(loaded.document_ids); setRun(loaded); setConnection(terminal(loaded) ? "已恢复持久化结果" : "正在恢复实时连接…");
      historyVersion.current += 1; setHistory((items) => items.map((item) => item.run_id === loaded.run_id ? loaded : item));
    } catch (reason) { if (!signal.aborted && view === activeView.current) failure(reason); }
    finally { if (!signal.aborted && view === activeView.current) setBusy(false); }
  }

  async function ask(event: FormEvent) {
    event.preventDefault();
    const signal = scope.current?.signal;
    if (!signal || signal.aborted || busy || !question.trim() || !selected.length || (run && !terminal(run))) return;
    setBusy(true); setError(""); setReport(null);
    const parent = run?.status === "completed" && sameScope(run.document_ids, selected) ? run : null;
    const body = {
      question: question.trim(), document_ids: selected, agentic: mode !== "rag", tool_mode: mode === "tool",
      ...(parent ? { conversation_id: parent.conversation_id, parent_run_id: parent.run_id } : {}),
    };
    try {
      const created = await client.createRun(workspaceId, body, signal);
      if (signal.aborted) return;
      if (parent) setConversation((items) => [...items.filter((item) => item.run_id !== parent.run_id), parent]);
      else setConversation([]);
      cursor.current = 0; setEvents([]); setStreamed(""); setRun(created); setQuestion("");
      historyVersion.current += 1;
      setHistory((items) => [created, ...items].slice(0, 50));
    } catch (reason) {
      if (!signal.aborted) { failure(reason); if (!(reason instanceof WorkspaceApiError)) setError(`${workspaceError(reason)} 提交状态未确认，请先刷新历史记录，避免重复创建付费任务。`); }
    } finally { if (!signal.aborted) setBusy(false); }
  }

  async function refreshHistory() {
    const signal = scope.current?.signal;
    if (!signal || signal.aborted) return;
    const version = ++historyVersion.current;
    try { const items = await client.runs(workspaceId, signal); if (!signal.aborted && version === historyVersion.current) { setHistory(items); setError(""); } }
    catch (reason) { if (!signal.aborted) failure(reason); }
  }
  async function cancelRun() {
    const signal = scope.current?.signal;
    if (!run || !signal || signal.aborted || busy) return;
    setBusy(true);
    try {
      const result = await client.cancelRun(workspaceId, run.run_id, signal);
      if (!signal.aborted) { setRun(result); historyVersion.current += 1; setHistory((items) => items.map((item) => item.run_id === result.run_id ? result : item)); }
    } catch (reason) { if (!signal.aborted) failure(reason); }
    finally { if (!signal.aborted) setBusy(false); }
  }
  async function prepareReport() {
    const signal = scope.current?.signal; const id = run?.run_id;
    if (!id || !signal || signal.aborted || busy) return;
    setBusy(true);
    try { const blob = await client.exportRun(workspaceId, id, signal); if (!signal.aborted && runRef.current?.run_id === id) { const url = URL.createObjectURL(blob); reportUrls.current.add(url); setReport(url); } }
    catch (reason) { if (!signal.aborted) failure(reason); }
    finally { if (!signal.aborted) setBusy(false); }
  }

  return <section className={styles.panel} aria-label="工作空间问答">
    <div className={styles.panelHead}><h2>论文问答</h2><button disabled={busy || Boolean(run && !terminal(run))} onClick={() => { activeView.current += 1; setRun(null); setEvents([]); setStreamed(""); setConversation([]); setReport(null); setError(""); setConnection(""); }}>新建会话</button></div>
    {error && <p role="alert" className={styles.error}>{error}</p>}
    <div className={styles.researchColumns}>
      <div>
        <fieldset className={styles.scope}><legend>检索范围（{selected.length} 篇）</legend>
          <button type="button" disabled={busy || Boolean(run && !terminal(run))} onClick={() => setSelected(papers.map((paper) => paper.document_id))}>全选</button>
          <button type="button" disabled={busy || Boolean(run && !terminal(run))} onClick={() => setSelected([])}>清空</button>
          {papers.map((paper) => <label key={paper.document_id}><input type="checkbox" checked={selected.includes(paper.document_id)} disabled={busy || Boolean(run && !terminal(run))} onChange={(event) => setSelected((items) => event.target.checked ? [...items, paper.document_id] : items.filter((id) => id !== paper.document_id))} />{paper.title || paper.source_filename}</label>)}
        </fieldset>
        <form onSubmit={ask} className={styles.editor}>
          <label htmlFor="research-question">研究问题</label>
          <textarea id="research-question" disabled={busy} value={question} maxLength={8000} rows={3} onChange={(event) => setQuestion(event.target.value)} placeholder="请基于所选论文回答，并给出原文引用。" />
          <label htmlFor="answer-mode">回答方式</label><select id="answer-mode" disabled={busy || Boolean(run && !terminal(run))} value={mode} onChange={(event) => setMode(event.target.value)}><option value="rag">直接检索问答</option><option value="tool">工具 Agent</option><option value="multi">多步规划</option></select>
          <button className={styles.primary} disabled={busy || !selected.length || !question.trim() || Boolean(run && !terminal(run))}>发送问题</button>
          {!selected.length && <small>请选择至少一篇论文。</small>}
          {run && terminal(run) && !sameScope(run.document_ids, selected) && <small>范围变化后会创建新会话。</small>}
        </form>
        {conversation.filter((item) => item.run_id !== run?.run_id).map((item) => <details key={item.run_id}><summary>{item.question}</summary><ReactMarkdown remarkPlugins={[remarkGfm]} components={{ img: () => null }}>{item.answer?.answer ?? labels[item.status]}</ReactMarkdown></details>)}
        {run && <article className={styles.answer}>
          <div className={styles.panelHead}><h3>{run.question}</h3><span className={styles.badge}>{labels[run.status]}</span></div>
          <ReactMarkdown remarkPlugins={[remarkGfm]} components={{ img: () => null }}>{run.answer?.answer || streamed || "正在检索与生成…"}</ReactMarkdown>
          {run.error && <p role="alert">{run.status === "cancelled" ? "本次问答已取消。" : "本次问答未完成，请检查权限或稍后重试。"}</p>}
          <details><summary>证据支持状态（{run.answer?.claims.length ?? 0} 项）</summary>{(run.answer?.claims ?? []).map((claim) => <p key={claim.claim_id} className={styles.muted}>{claim.text} · {claim.support_status === "supported" ? "已验证支持" : claim.support_status === "contradicted" ? "与原文矛盾" : claim.support_status === "insufficient" ? "支持证据不足" : claim.evidence_located ? "已定位原文，语义支持未验证" : "尚未定位证据"}</p>)}</details>
          <ul className={styles.citations}>{run.answer?.citations.map((citation) => <li key={citation.citation_id}><button onClick={() => onOpenPdf(citation.document_id, citation.page_number)}>{citation.citation_id} · 第 {citation.page_number} 页</button><blockquote>{citation.quote}</blockquote></li>)}</ul>
          <p role="status" className={styles.muted}>{connection}</p>
          {!terminal(run) && <button disabled={busy} onClick={() => void cancelRun()}>停止回答</button>}
          {!terminal(run) && <button onClick={() => setReconnect((value) => value + 1)}>重新连接</button>}
          {run.answer && <button disabled={busy} onClick={() => void prepareReport()}>准备导出证据报告</button>}
          {report && <a href={report} download={`scholarrag-${run.run_id}.md`}>下载证据报告.md</a>}
          <details><summary>执行轨迹（最近 {events.length} 项）</summary><ol>{events.map((item) => <li key={item.seq}>{item.seq}. {item.label} · {item.status}</li>)}</ol></details>
        </article>}
      </div>
      <aside className={styles.history}><div className={styles.panelHead}><h3>我的问答历史</h3><button disabled={busy} onClick={() => void refreshHistory()}>刷新历史</button></div>
        {!history.length && <p className={styles.muted}>尚无问答记录。</p>}
        {history.map((item) => <button key={item.run_id} disabled={busy} aria-current={run?.run_id === item.run_id ? "true" : undefined} onClick={() => void loadRun(item)}>{item.question}<small>{labels[item.status]} · {new Date(item.created_at).toLocaleString()}</small></button>)}
      </aside>
    </div>
  </section>;
}
