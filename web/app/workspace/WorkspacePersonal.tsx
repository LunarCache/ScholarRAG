"use client";

import { FormEvent, useEffect, useRef, useState } from "react";
import { PaperCollection, PaperState, Workspace, WorkspaceApi, WorkspaceApiError, WorkspacePaper, workspaceError } from "../../lib/workspace-api";
import styles from "./workspace.module.css";

type Common = { client: WorkspaceApi; workspace: Workspace; papers: WorkspacePaper[]; onError: (reason: unknown) => void; activeQuery: boolean; onUseCollection: (ids: string[]) => void };
const fatal = (reason: unknown) => reason instanceof WorkspaceApiError && [401, 403].includes(reason.status);

export function WorkspacePersonal(props: Common) {
  const [documentId, setDocumentId] = useState(props.papers[0]?.document_id ?? "");
  const [dirty, setDirty] = useState(false);
  const [switchTo, setSwitchTo] = useState<string | null>(null);
  const current = props.papers.find((paper) => paper.document_id === documentId);
  useEffect(() => {
    if (!props.papers.some((paper) => paper.document_id === documentId)) {
      setDocumentId(props.papers[0]?.document_id ?? ""); setDirty(false); setSwitchTo(null);
    }
  }, [props.papers, documentId]);
  return <section className={styles.panel} aria-label="个人阅读与集合">
    <div className={styles.panelHead}>
      <div>
        <p className={styles.eyebrow}>PRIVATE READING STATE & COLLECTIONS</p>
        <h2>我的阅读空间</h2>
      </div>
      <span className={styles.muted}>笔记、标签、收藏和集合仅自己可见。切换工作空间会清除本页未保存草稿。</span>
    </div>
    <div className={styles.researchColumns}>
      <div>
        <label htmlFor="personal-document">编辑论文阅读记录</label>
        <select id="personal-document" value={current?.document_id ?? ""} onChange={(event) => {
          if (dirty) setSwitchTo(event.target.value); else setDocumentId(event.target.value);
        }} disabled={!props.papers.length}>
          {!props.papers.length && <option value="">暂无可访问论文</option>}
          {props.papers.map((paper) => <option key={paper.document_id} value={paper.document_id}>{paper.title || paper.source_filename}</option>)}
        </select>
        {switchTo && <p role="alert" className={styles.error}>有未保存草稿。<button onClick={() => { setDocumentId(switchTo); setSwitchTo(null); setDirty(false); }}>放弃草稿并切换论文</button><button onClick={() => setSwitchTo(null)}>继续编辑</button></p>}
        {current && <ReadingEditor key={current.document_id} {...props} documentId={current.document_id} onDirty={setDirty} />}
      </div>
      <CollectionEditor {...props} />
    </div>
  </section>;
}

function ReadingEditor({ client, workspace, documentId, onError, onDirty }: Common & { documentId: string; onDirty: (value: boolean) => void }) {
  const [record, setRecord] = useState<PaperState | null>(null);
  const [tags, setTags] = useState("");
  const [busy, setBusy] = useState(true);
  const [message, setMessage] = useState("");
  const [conflict, setConflict] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const scope = useRef<AbortController | null>(null);
  const errorRef = useRef(onError); errorRef.current = onError;
  const writable = workspace.role !== "viewer";

  function fail(reason: unknown) {
    if (reason instanceof WorkspaceApiError && reason.status === 409) { setConflict(true); setMessage("记录已被其他窗口修改。草稿已保留，请放弃草稿并加载最新版本后再编辑。"); }
    else setMessage(workspaceError(reason));
    if (fatal(reason)) errorRef.current(reason);
  }
  async function reload(signal: AbortSignal) {
    setBusy(true);
    try {
      const data = await client.state(workspace.workspace_id, documentId, signal);
      if (signal.aborted) return;
      setRecord(data); setTags(data.tags.join(", ")); setConflict(false); setMessage(""); onDirty(false); setDeleting(false);
    } catch (reason) { if (!signal.aborted) fail(reason); }
    finally { if (!signal.aborted) setBusy(false); }
  }
  useEffect(() => {
    const controller = new AbortController(); scope.current = controller;
    void reload(controller.signal);
    return () => controller.abort();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [client, workspace.workspace_id, documentId]);

  async function save(event: FormEvent) {
    event.preventDefault();
    const signal = scope.current?.signal;
    if (!record || !signal || signal.aborted || busy || conflict) return;
    const parsedTags = [...new Set(tags.split(/[,，]/).map((tag) => tag.trim()).filter(Boolean))];
    if (parsedTags.length > 64 || parsedTags.some((tag) => tag.length > 64)) { setMessage("最多 64 个标签，每个标签最多 64 字。"); return; }
    setBusy(true); setMessage("");
    try {
      const result = await client.saveState(workspace.workspace_id, documentId, {
        reading_status: record.reading_status, tags: parsedTags, note: record.note, favorite: record.favorite, expected_revision: record.revision,
      }, signal);
      if (!signal.aborted) { setRecord(result); setTags(result.tags.join(", ")); onDirty(false); setMessage("阅读记录已保存。"); }
    } catch (reason) { if (!signal.aborted) fail(reason); }
    finally { if (!signal.aborted) setBusy(false); }
  }
  async function remove() {
    const signal = scope.current?.signal;
    if (!signal || signal.aborted || busy) return;
    setBusy(true);
    try { await client.deleteState(workspace.workspace_id, documentId, signal); if (!signal.aborted) { await reload(signal); setMessage("个人阅读记录已清空，论文未删除。"); } }
    catch (reason) { if (!signal.aborted) fail(reason); }
    finally { if (!signal.aborted) setBusy(false); }
  }
  function change(update: Partial<PaperState>) { setRecord((current) => current ? { ...current, ...update } : null); onDirty(true); setMessage(""); }
  return <form className={styles.editor} onSubmit={save}>
    {message && <p role="status" className={conflict ? styles.error : styles.notice}>{message}</p>}
    {!record ? <p className={styles.muted}>{busy ? "加载阅读记录…" : "记录暂不可用。"}</p> : <>
      <div>
        <label htmlFor="reading-status">阅读状态</label>
        <select id="reading-status" disabled={!writable || busy} value={record.reading_status} onChange={(event) => change({ reading_status: event.target.value as PaperState["reading_status"] })}>
          <option value="unread">未读</option>
          <option value="reading">阅读中</option>
          <option value="finished">已读</option>
        </select>
      </div>
      <label className={styles.checkLabel}><input type="checkbox" checked={record.favorite} disabled={!writable || busy} onChange={(event) => change({ favorite: event.target.checked })} />收藏这篇论文</label>
      <div>
        <label htmlFor="paper-tags">标签（逗号分隔）</label>
        <input id="paper-tags" placeholder="例如：RAG, Agent, Multimodal, Baseline" value={tags} maxLength={4224} disabled={!writable || busy} onChange={(event) => { setTags(event.target.value); onDirty(true); }} />
      </div>
      <div>
        <label htmlFor="paper-note">私有笔记</label>
        <textarea id="paper-note" placeholder="记录论文的核心假设、实验设置、公式推导或局限性…" value={record.note} maxLength={20000} rows={6} readOnly={!writable} disabled={busy} onChange={(event) => change({ note: event.target.value })} />
      </div>
      <small className={styles.muted}>修订 {record.revision} · {record.note.length}/20000 字</small>
      <div className={styles.editorFooter}>
        {writable ? <button className={styles.primary} disabled={busy || conflict}>保存阅读记录</button> : <p className={styles.muted}>当前角色不能编辑，可以清空自己先前保存的记录。</p>}
        <button type="button" disabled={busy} onClick={() => { const signal = scope.current?.signal; if (signal) void reload(signal); }}>放弃草稿并加载最新记录</button>
        {record.revision > 0 && <button type="button" className={styles.dangerBtn} disabled={busy || conflict} onClick={() => setDeleting(true)}>清空我的阅读记录</button>}
      </div>
      {deleting && <div role="alert" className={styles.error}><p>只清空自己的笔记、标签、收藏与阅读状态。</p><div><button type="button" className={styles.dangerBtn} disabled={busy} onClick={() => void remove()}>确认清空记录</button><button type="button" onClick={() => setDeleting(false)}>保留记录</button></div></div>}
    </>}
  </form>;
}

function CollectionEditor({ client, workspace, papers, onError, activeQuery, onUseCollection }: Common) {
  const [collections, setCollections] = useState<PaperCollection[]>([]);
  const [editing, setEditing] = useState<PaperCollection | null>(null);
  const [name, setName] = useState("");
  const [documents, setDocuments] = useState<string[]>([]);
  const [dropUnavailable, setDropUnavailable] = useState(false);
  const [busy, setBusy] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [conflict, setConflict] = useState(false);
  const [message, setMessage] = useState("");
  const [deleteId, setDeleteId] = useState<string | null>(null);
  const scope = useRef<AbortController | null>(null);
  const errorRef = useRef(onError); errorRef.current = onError;
  const writable = workspace.role !== "viewer";
  function fail(reason: unknown) {
    if (reason instanceof WorkspaceApiError && reason.status === 409) { setConflict(true); setMessage("集合版本冲突，草稿已保留；请放弃修改后加载最新集合。"); }
    else setMessage(workspaceError(reason));
    if (fatal(reason)) errorRef.current(reason);
  }
  async function reload(signal: AbortSignal) {
    const items = await client.collections(workspace.workspace_id, signal);
    if (!signal.aborted) setCollections(items);
    return items;
  }
  useEffect(() => {
    const controller = new AbortController(); scope.current = controller;
    void reload(controller.signal).catch((reason) => { if (!controller.signal.aborted) fail(reason); });
    return () => controller.abort();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [client, workspace.workspace_id]);

  function edit(item: PaperCollection | null) {
    setEditing(item); setName(item?.name ?? ""); setDocuments(item?.document_ids ?? []); setDirty(false); setConflict(false); setMessage(""); setDeleteId(null); setDropUnavailable(false);
  }
  async function save(event: FormEvent) {
    event.preventDefault();
    const signal = scope.current?.signal;
    if (!signal || signal.aborted || busy || conflict || !name.trim()) return;
    setBusy(true); setMessage("");
    try {
      const result = await client.saveCollection(workspace.workspace_id, {
        name: name.trim(), document_ids: documents, ...(editing ? { expected_revision: editing.revision } : {}),
      }, signal, editing?.collection_id);
      if (!signal.aborted) { edit(result); setCollections((items) => [result, ...items.filter((item) => item.collection_id !== result.collection_id)]); setMessage("集合已保存。"); await reload(signal); }
    } catch (reason) { if (!signal.aborted) fail(reason); }
    finally { if (!signal.aborted) setBusy(false); }
  }
  async function remove(id: string) {
    const signal = scope.current?.signal;
    if (!signal || signal.aborted || busy) return;
    setBusy(true);
    try {
      await client.deleteCollection(workspace.workspace_id, id, signal);
      if (!signal.aborted) { edit(null); setCollections((items) => items.filter((item) => item.collection_id !== id)); setMessage("集合已删除，论文和阅读记录未删除。"); await reload(signal); }
    } catch (reason) { if (!signal.aborted) fail(reason); }
    finally { if (!signal.aborted) setBusy(false); }
  }
  const invalidScope = documents.some((id) => !papers.some((paper) => paper.document_id === id));
  return <div>
    <div className={styles.panelHead}>
      <h3>我的论文集合</h3>
      <button disabled={busy || dirty || !writable} onClick={() => edit(null)}>新建集合</button>
    </div>
    {message && <p role="status" className={conflict ? styles.error : styles.notice}>{message}</p>}
    <ul className={styles.collectionList}>{collections.map((item) => <li key={item.collection_id}>
      <span>{item.name} · {item.document_ids.length} 篇{item.unavailable_document_count > 0 && ` · ${item.unavailable_document_count} 篇已不可访问`}</span>
      <button disabled={busy || dirty} onClick={() => edit(item)}>打开集合</button>
      <button className={styles.primary} disabled={busy || activeQuery || !item.document_ids.length} onClick={() => onUseCollection(item.document_ids)}>基于集合提问</button>
    </li>)}</ul>
    {!collections.length && <p className={styles.muted}>暂无私人集合。</p>}
    {!writable && <p className={styles.muted}>当前角色可查看或删除自己的已有集合，不能创建或修改。</p>}
    <form className={styles.editor} onSubmit={save}>
      <div>
        <label htmlFor="collection-name">{editing ? "集合名称" : "新集合名称"}</label>
        <input id="collection-name" placeholder="例如：2026 多模态检索综述" maxLength={120} value={name} disabled={!writable || busy} onChange={(event) => { setName(event.target.value); setDirty(true); }} />
      </div>
      <fieldset className={styles.scope}>
        <legend>集合中的论文</legend>
        {papers.map((paper) => <label key={paper.document_id}><input type="checkbox" checked={documents.includes(paper.document_id)} disabled={!writable || busy} onChange={(event) => { setDocuments((items) => event.target.checked ? [...items, paper.document_id] : items.filter((id) => id !== paper.document_id)); setDirty(true); }} />{paper.title || paper.source_filename}</label>)}
      </fieldset>
      {Boolean(editing?.unavailable_document_count) && <label className={styles.checkLabel}><input type="checkbox" checked={dropUnavailable} disabled={!writable || busy} onChange={(event) => setDropUnavailable(event.target.checked)} />确认保存时移除 {editing?.unavailable_document_count} 个已不可访问的论文引用</label>}
      {invalidScope && <p role="alert" className={styles.error}>论文权限已变化，请重新加载集合后编辑。</p>}
      <div className={styles.editorFooter}>
        {writable && <button className={styles.primary} disabled={busy || conflict || !name.trim() || invalidScope || Boolean(editing?.unavailable_document_count && !dropUnavailable)}>保存集合</button>}
        <button type="button" disabled={busy} onClick={async () => {
          const signal = scope.current?.signal; if (!signal || signal.aborted) return;
          setBusy(true);
          try { const items = await reload(signal); if (!signal.aborted) edit(items.find((item) => item.collection_id === editing?.collection_id) ?? null); }
          catch (reason) { if (!signal.aborted) fail(reason); }
          finally { if (!signal.aborted) setBusy(false); }
        }}>放弃修改并刷新集合</button>
        {editing && <button type="button" className={styles.dangerBtn} disabled={busy || dirty || conflict} onClick={() => setDeleteId(editing.collection_id)}>删除当前集合</button>}
      </div>
      {deleteId && <div role="alert" className={styles.error}><p>删除集合不会删除论文或笔记。</p><div><button type="button" className={styles.dangerBtn} disabled={busy} onClick={() => void remove(deleteId)}>确认删除集合</button><button type="button" onClick={() => setDeleteId(null)}>保留集合</button></div></div>}
    </form>
  </div>;
}
