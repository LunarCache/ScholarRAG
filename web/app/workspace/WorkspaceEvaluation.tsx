"use client";

import { useEffect, useRef, useState } from "react";
import { EvaluationJob, RunEvent, WorkspaceApi, WorkspaceApiError, workspaceError } from "../../lib/workspace-api";
import styles from "./workspace.module.css";

const active = (job: EvaluationJob | null) => job?.status === "queued" || job?.status === "running";
const statusLabel = { queued: "评测排队中", running: "评测中", completed: "评测完成", failed: "评测失败", cancelled: "评测已取消" };
const names: Record<string, string> = { citation_coverage: "引用覆盖", faithfulness: "忠实度", answer_relevancy: "答案相关性", context_precision: "上下文精确度", context_recall: "上下文召回", factual_correctness: "事实正确性", citation_validity: "引用有效率", claim_citation_completeness: "结论引用完整率", claim_evidence_span_rate: "结论原文定位率", visual_images_included: "生成图片数", visual_images_skipped: "跳过图片数" };

export function WorkspaceEvaluation({ client, workspaceId, runId, onError }: {
  client: WorkspaceApi; workspaceId: string; runId: string; onError: (error: unknown) => void;
}) {
  const [job, setJob] = useState<EvaluationJob | null>(null);
  const [reference, setReference] = useState("");
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(true);
  const [refresh, setRefresh] = useState(0);
  const scope = useRef<AbortController | null>(null);
  const activeJobId = useRef<string | null>(null);
  const request = useRef<{ reference: string; key: string } | null>(null);
  const onErrorRef = useRef(onError); onErrorRef.current = onError;

  function fail(reason: unknown) {
    setError(workspaceError(reason));
    if (reason instanceof WorkspaceApiError && [401, 403].includes(reason.status)) {
      activeJobId.current = null;
      setJob(null); setEvents([]); setReference(""); request.current = null;
      onErrorRef.current(reason);
    }
  }

  useEffect(() => {
    const controller = new AbortController(); scope.current = controller;
    let changed = false;
    setBusy(true);
    client.latestEvaluation(workspaceId, runId, controller.signal).then(async (latest) => {
      if (controller.signal.aborted || changed) return;
      activeJobId.current = latest.job_id;
      setJob(latest);
      const log = await client.evaluationEvents(workspaceId, latest.job_id, controller.signal);
      if (!controller.signal.aborted) setEvents(log.slice(-100));
    }).catch((reason) => {
      if (!controller.signal.aborted && !(reason instanceof WorkspaceApiError && reason.status === 404)) fail(reason);
    }).finally(() => { if (!controller.signal.aborted) setBusy(false); });
    return () => { changed = true; controller.abort(); };
  }, [client, workspaceId, runId]);

  const running = active(job);
  useEffect(() => {
    if (!job || !running) return;
    const controller = new AbortController(); const id = job.job_id;
    async function poll() {
      while (!controller.signal.aborted) {
        try {
          const latest = await client.evaluation(workspaceId, id, controller.signal);
          const log = await client.evaluationEvents(workspaceId, id, controller.signal);
          if (controller.signal.aborted || activeJobId.current !== id) return;
          setJob((current) => current?.job_id === id && current.event_count <= latest.event_count ? latest : current);
          setEvents((current) => (current.at(-1)?.seq ?? 0) > (log.at(-1)?.seq ?? 0) ? current : log.slice(-100));
          if (!active(latest)) return;
        } catch (reason) {
          if (!controller.signal.aborted) fail(reason);
          return;
        }
        await new Promise<void>((resolve) => {
          const done = () => { clearTimeout(timer); controller.signal.removeEventListener("abort", done); resolve(); };
          const timer = setTimeout(done, 1000);
          controller.signal.addEventListener("abort", done, { once: true });
        });
      }
    }
    void poll();
    return () => controller.abort();
  }, [client, workspaceId, job?.job_id, running, refresh]);

  async function start() {
    const signal = scope.current?.signal;
    if (!signal || signal.aborted || busy || running) return;
    if (!request.current || request.current.reference !== reference) {
      request.current = { reference, key: crypto.randomUUID() };
    }
    setBusy(true); setError("");
    try {
      const created = await client.createEvaluation(workspaceId, runId, reference || null, request.current.key, signal);
      if (signal.aborted) return;
      activeJobId.current = created.job_id;
      setJob(created); setEvents([]); request.current = null;
    } catch (reason) { if (!signal.aborted) fail(reason); }
    finally { if (!signal.aborted) setBusy(false); }
  }

  async function stop() {
    const signal = scope.current?.signal;
    if (!job || !signal || signal.aborted || busy) return;
    setBusy(true); setError("");
    try {
      const cancelled = await client.cancelEvaluation(workspaceId, job.job_id, signal);
      if (!signal.aborted) { setJob(cancelled); setRefresh((value) => value + 1); }
    } catch (reason) { if (!signal.aborted) fail(reason); }
    finally { if (!signal.aborted) setBusy(false); }
  }

  return <section aria-label="回答评测" className={styles.evaluation}>
    <div className={styles.panelHead}><h3>回答评测</h3>{job && <span role="status">{statusLabel[job.status]}</span>}</div>
    <p className={styles.muted}>评测会调用 Judge，可能产生费用。自动指标仅供参考；填写的参考答案不等同于人工 Gold。</p>
    <label>参考答案（可选）<textarea value={reference} maxLength={50000} disabled={busy || running} onChange={(event) => setReference(event.target.value)} rows={3} placeholder="可留空；填写后增加与参考答案相关的指标。" /></label>
    <div>
      <button disabled={busy || running} onClick={() => void start()}>{job ? "重新评测" : "开始评测"}</button>
      {running && <button className={styles.dangerBtn} disabled={busy} onClick={() => void stop()}>停止评测</button>}
      {running && <button disabled={busy} onClick={() => { setError(""); setRefresh((value) => value + 1); }}>刷新评测</button>}
    </div>
    {error && <p role="alert" className={styles.error}>{error}</p>}
    {job?.status === "failed" && <p className={styles.muted}>评测未完成；中断调用不会自动重试。可检查执行轨迹后显式重新评测。</p>}
    {job?.result && <div className={styles.evaluationResults}><p className={styles.muted}>以下指标基于保存的问答和本次参考答案；不会改写原问答。</p><details><summary>本次参考答案</summary><p>{job.case.reference || "未提供参考答案"}</p></details><table><thead><tr><th>指标</th><th>结果</th></tr></thead><tbody>
      {Object.entries(job.result.metrics).map(([name, metric]) => <tr key={name}><td>{names[name] ?? name}</td><td>{metric.value === null ? "无结果" : typeof metric.value === "number" ? (name.startsWith("visual_images_") ? String(metric.value) : metric.value.toFixed(3)) : String(metric.value)}{metric.reason && <small>{metric.reason}</small>}</td></tr>)}
    </tbody></table></div>}
    <details><summary>评测轨迹（最近 {events.length} 项）</summary><ol>{events.map((event) => <li key={event.seq}>{event.seq}. {event.label} · {event.status}</li>)}</ol></details>
  </section>;
}
