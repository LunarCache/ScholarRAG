"use client";

import { FormEvent, useEffect, useRef, useState } from "react";
import { ParseReviewSnapshot, Workspace, WorkspaceApi, WorkspaceApiError, WorkspacePaper, workspaceError } from "../../lib/workspace-api";
import styles from "./workspace.module.css";

type Props = { client: WorkspaceApi; workspace: Workspace; papers: WorkspacePaper[]; onError: (error: unknown) => void; onChanged: () => void };

export function WorkspaceParseReview(props: Props) {
  const [selected, setSelected] = useState("");
  const paper = props.papers.find((item) => item.document_id === selected) ?? props.papers[0];
  return <section className={styles.panel}>
    <h2>解析版本审核</h2>
    <p className={styles.muted}>查看候选质量与审核记录。所有者和管理员可切换已完成索引的版本；未索引候选暂不能激活。切换论文会清除未提交的理由草稿。</p>
    <label htmlFor="review-paper">选择论文</label>
    <select id="review-paper" value={paper?.document_id ?? ""} disabled={!paper} onChange={(event) => setSelected(event.target.value)}>
      {!paper && <option value="">暂无可访问论文</option>}
      {props.papers.map((item) => <option key={item.document_id} value={item.document_id}>{item.title || item.source_filename}</option>)}
    </select>
    {paper && <ReviewDocument key={`${props.workspace.workspace_id}:${paper.document_id}`} {...props} documentId={paper.document_id} />}
  </section>;
}

function ReviewDocument({ client, workspace, documentId, onError, onChanged }: Props & { documentId: string }) {
  const [data, setData] = useState<ParseReviewSnapshot | null>(null);
  const [target, setTarget] = useState("");
  const [reason, setReason] = useState("");
  const [force, setForce] = useState(false);
  const [busy, setBusy] = useState(false);
  const [conflict, setConflict] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const controller = useRef<AbortController | null>(null);
  const errorRef = useRef(onError); errorRef.current = onError;
  const editable = workspace.role === "owner" || workspace.role === "admin";

  useEffect(() => {
    const request = new AbortController(); controller.current = request;
    client.parseReview(workspace.workspace_id, documentId, request.signal).then((value) => {
      if (!request.signal.aborted) setData(value);
    }).catch((failure) => {
      if (!request.signal.aborted) { setError(workspaceError(failure)); if (failure instanceof WorkspaceApiError && (failure.status === 401 || failure.status === 403)) errorRef.current(failure); }
    });
    return () => request.abort();
  }, [client, workspace.workspace_id, documentId]);

  async function refresh() {
    const signal = controller.current?.signal;
    if (!signal || busy) return;
    setBusy(true); setError("");
    try {
      const snapshot = await client.parseReview(workspace.workspace_id, documentId, signal);
      if (!signal.aborted) {
        setData(snapshot); setConflict(false); setTarget(""); setForce(false);
        setMessage("已刷新版本；审核理由草稿已保留，请重新选择目标版本。");
      }
    } catch (failure) {
      if (!signal.aborted) { setError(workspaceError(failure)); if (failure instanceof WorkspaceApiError && (failure.status === 401 || failure.status === 403)) errorRef.current(failure); }
    } finally { if (!signal.aborted) setBusy(false); }
  }

  async function activate(event: FormEvent) {
    event.preventDefault();
    const signal = controller.current?.signal;
    if (!data || !signal || busy || conflict || !target || reason.trim().length < 3) return;
    setBusy(true); setError(""); setMessage("");
    try {
      await client.activateParse(workspace.workspace_id, documentId, target, {
        expected_revision: data.revision, expected_parse_id: data.active_parse_id, reason: reason.trim(), force,
      }, signal);
      if (signal.aborted) return;
      setData(null); setTarget(""); setForce(false); setReason(""); onChanged();
      const snapshot = await client.parseReview(workspace.workspace_id, documentId, signal);
      if (!signal.aborted) { setData(snapshot); setMessage("解析版本已激活，审核记录已保存。"); }
    } catch (failure) {
      if (signal.aborted) return;
      // An interrupted POST may already have committed. Require an explicit
      // reload before any retry, including an unknown transport outcome.
      setConflict(true);
      setError(failure instanceof WorkspaceApiError && failure.status === 409
        ? "版本已变化或候选尚不可激活。理由草稿已保留，请刷新后检查。"
        : "激活结果未确认，请刷新审核记录后再操作。");
      if (failure instanceof WorkspaceApiError && (failure.status === 401 || failure.status === 403)) errorRef.current(failure);
    } finally { if (!signal.aborted) setBusy(false); }
  }

  return <div>
    <button disabled={busy} onClick={() => void refresh()}>刷新审核记录</button>
    {error && <p role="alert" className={styles.error}>{error}</p>}
    {message && <p role="status" className={styles.notice}>{message}</p>}
    {!data ? <p className={styles.muted}>版本尚未加载；可刷新重试。</p> : <>
      <p>当前版本：<code>{data.active_parse_id}</code> · 修订 {data.revision}</p>
      <ul className={styles.jobList}>{data.versions.map((version) => <li key={version.parse_id}>
        <div><strong>{version.provider} · {version.provider_model}</strong><p><code>{version.parse_id}</code></p>
          <small>{version.active ? "正在使用" : version.requires_indexing ? "待索引，暂不可激活" : version.status} · 质量 {version.quality_decision ?? "未评估"} / {String(version.quality_score ?? "—")} · {version.page_count ?? "—"} 页</small>
        </div>
      </li>)}</ul>
      {!editable ? <p className={styles.muted}>当前角色可查看，激活需要所有者或管理员权限。</p> : <form onSubmit={activate} className={styles.uploadForm}>
        <label htmlFor="review-target">激活目标</label>
        <select id="review-target" value={target} disabled={busy || conflict} onChange={(event) => { setTarget(event.target.value); setForce(false); }}>
          <option value="">请选择已索引版本</option>
          {data.versions.filter((version) => version.can_activate && !version.active).map((version) => <option key={version.parse_id} value={version.parse_id}>{version.provider} · {version.parse_id}</option>)}
        </select>
        <label htmlFor="review-reason">审核理由（至少 3 个字符）</label>
        <textarea id="review-reason" value={reason} maxLength={2000} disabled={busy} onChange={(event) => setReason(event.target.value)} />
        <label><input type="checkbox" checked={force} disabled={busy || conflict} onChange={(event) => setForce(event.target.checked)} />已人工检查，允许覆盖 fallback 质量门禁</label>
        <button type="submit" disabled={busy || conflict || !target || reason.trim().length < 3}>{busy ? "处理中…" : "确认激活此版本"}</button>
      </form>}
      <h3>最近审核记录</h3>
      {!data.history.length && <p className={styles.muted}>暂无工作空间审核记录。</p>}
      <ul className={styles.jobList}>{data.history.map((item) => <li key={item.review_id}><div>
        <strong>修订 {item.revision} · {item.parse_id}</strong><p>{item.reason}</p>
        <small>操作者 {item.actor_id} · {item.created_at}{item.force ? " · 已确认覆盖质量门禁" : ""}</small>
      </div></li>)}</ul>
    </>}
  </div>;
}
