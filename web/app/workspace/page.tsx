"use client";

import Link from "next/link";
import { FormEvent, useEffect, useRef, useState } from "react";
import {
  activeJob, IngestionJob, Profile, Workspace, WorkspaceApi, WorkspaceApiError,
  workspaceError, WorkspacePaper,
} from "../../lib/workspace-api";
import { ThemeToggle } from "../ThemeToggle";
import styles from "./workspace.module.css";
import { WorkspaceChat } from "./WorkspaceChat";
import { WorkspacePersonal } from "./WorkspacePersonal";

const API = process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000";
const roleNames = { viewer: "只读成员", member: "成员", admin: "管理员", owner: "所有者" };
const statusNames = { queued: "等待处理", parsing: "解析与索引中", ready: "已完成", failed: "失败", canceled: "已取消" };
const failureNames: Record<string, string> = {
  authorization_revoked: "执行权限已失效", execution_stopped: "执行已停止",
  execution_interrupted: "服务中断，请重新提交", ingestion_failed: "解析或索引失败，请检查文件后重新提交",
};
type Session = { client: WorkspaceApi; profile: Profile; workspaces: Workspace[]; id: number };

export default function WorkspacePage() {
  const [session, setSession] = useState<Session | null>(null);
  const [workspaceId, setWorkspaceId] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const loginInput = useRef<HTMLInputElement>(null);
  const operation = useRef(0);
  const request = useRef<AbortController | null>(null);

  useEffect(() => () => { operation.current += 1; request.current?.abort(); }, []);

  function expire() {
    operation.current += 1;
    request.current?.abort();
    setSession(null);
    setWorkspaceId("");
    setMessage("会话无效或已过期，请重新登录。");
    setBusy(false);
  }

  async function login(event: FormEvent) {
    event.preventDefault();
    const token = loginInput.current?.value.trim();
    if (!token || busy) return;
    if (loginInput.current) loginInput.current.value = "";
    const current = ++operation.current;
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setBusy(true);
    setMessage("");
    try {
      const client = new WorkspaceApi(API, token);
      const [profile, workspaces] = await Promise.all([
        client.profile(controller.signal), client.workspaces(controller.signal),
      ]);
      if (controller.signal.aborted || current !== operation.current) return;
      setSession({ client, profile, workspaces, id: current });
      setWorkspaceId(workspaces[0]?.workspace_id ?? "");
    } catch (error) {
      if (!controller.signal.aborted) setMessage(workspaceError(error));
    } finally {
      if (current === operation.current) setBusy(false);
    }
  }

  async function logout() {
    if (!session) return;
    const client = session.client;
    const current = ++operation.current;
    request.current?.abort();
    setSession(null);
    setWorkspaceId("");
    setBusy(true);
    setMessage("");
    try { await client.logout(); }
    catch (error) {
      if (current === operation.current && !(error instanceof WorkspaceApiError && error.status === 401)) {
        setMessage("已清除本页会话，服务端注销未确认。请联系管理员撤销会话。");
      }
    } finally { if (current === operation.current) setBusy(false); }
  }

  async function reloadWorkspaces() {
    if (!session || busy) return;
    const current = ++operation.current;
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setBusy(true);
    setMessage("");
    try {
      const workspaces = await session.client.workspaces(controller.signal);
      if (controller.signal.aborted || current !== operation.current) return;
      // Remount the scope even for the same ID: discard data from the previous grant snapshot.
      setSession({ ...session, workspaces, id: current });
      setWorkspaceId(workspaces.some((item) => item.workspace_id === workspaceId) ? workspaceId : workspaces[0]?.workspace_id ?? "");
    } catch (error) {
      if (controller.signal.aborted) return;
      if (error instanceof WorkspaceApiError && error.status === 401) expire();
      else setMessage(workspaceError(error));
    } finally { if (current === operation.current) setBusy(false); }
  }

  const workspace = session?.workspaces.find((item) => item.workspace_id === workspaceId);
  return <main className={styles.shell}>
    <header className={styles.header}>
      <Link href="/workspace" className={styles.brandGroup}>
        <span className={styles.brandMark}>S</span>
        <span className={styles.brand}>ScholarRAG</span>
      </Link>
      <span className={styles.subtitle}>工作空间</span>
      <div className={styles.account}>
        <ThemeToggle />
        {session && <>
          <span className={styles.userPill}>
            <span className={styles.userAvatar} aria-hidden="true">{session.profile.display_name.slice(0, 1).toUpperCase()}</span>
            <span>{session.profile.display_name}</span>
          </span>
          <button onClick={() => void logout()}>退出登录</button>
        </>}
      </div>
    </header>
    {!session ? <div className={styles.loginWrapper}>
      <aside className={styles.loginHero} aria-hidden="true">
        <div>
          <p className={styles.eyebrow}>TRACEABLE MULTIMODAL AGENTIC RAG</p>
          <h2>从原始 PDF 到可审计的学术证据链</h2>
          <p className={styles.muted}>结合双解析引擎质量路由、Qwen3-VL 统一多模态检索与细粒度 Claim 语义支持度核验，打造零幻觉的科研工作空间。</p>
          <ul className={styles.featurePillList}>
            <li><span className={styles.featurePillIcon}>01</span><div><b>分屏对照精读与溯源</b><small>点击回答中的引用 [C#] 立即跳转原 PDF 页码，对照核实公式、实验表与上下文。</small></div></li>
            <li><span className={styles.featurePillIcon}>02</span><div><b>多模态 Agentic 自主检索</b><small>支持直接检索、多步规划与工具调用 Agent，完整记录并回放每一步检索轨迹。</small></div></li>
            <li><span className={styles.featurePillIcon}>03</span><div><b>工作空间级租户与权限隔离</b><small>严格保障团队论文库、私有阅读状态、随手笔记与课题集合的并发安全与隐私边界。</small></div></li>
          </ul>
        </div>
        <small className={styles.muted}>ScholarRAG Research Workbench · Enterprise Design System v2.0</small>
      </aside>
      <section className={styles.login}>
        <p className={styles.eyebrow}>YOUR RESEARCH SPACE</p>
        <h1>进入工作空间</h1>
        <p className={styles.muted}>使用管理员签发的会话令牌登录。令牌仅保存在当前页面内存中，刷新页面后需重新登录。</p>
        <form onSubmit={login}>
          <div>
            <label htmlFor="session-token">会话令牌</label>
            <input ref={loginInput} id="session-token" type="password" placeholder="粘贴您的工作空间会话令牌 (Session Token)…" autoComplete="off" spellCheck={false} required disabled={busy} />
          </div>
          <button className={styles.primary} disabled={busy}>{busy ? "正在连接…" : "登录工作空间"}</button>
        </form>
        {message && <p role="alert" className={styles.error}>{message}</p>}
        <p className={styles.muted}>还没有令牌？请联系工作空间管理员。本入口暂不提供自助注册。</p>
      </section>
    </div> : <div className={styles.content}>
      <section className={styles.topControlStrip}>
        <div className={styles.workspaceBar}>
          <div>
            <label htmlFor="workspace-choice">当前工作空间</label>
            <select id="workspace-choice" value={workspaceId} onChange={(event) => setWorkspaceId(event.target.value)} disabled={busy || !session.workspaces.length}>
              {!session.workspaces.length && <option value="">暂无工作空间</option>}
              {session.workspaces.map((item) => <option key={item.workspace_id} value={item.workspace_id}>{item.name}</option>)}
            </select>
          </div>
          {workspace && <span className={styles.badge}>{roleNames[workspace.role]}</span>}
        </div>
        <button disabled={busy} onClick={() => void reloadWorkspaces()}>{busy ? "刷新中…" : "刷新工作空间"}</button>
      </section>
      {message && <p role="alert" className={styles.error}>{message}</p>}
      {!workspace && <section className={styles.panel}><h1>尚未加入工作空间</h1><p className={styles.muted}>请联系管理员添加成员权限，然后刷新工作空间。</p></section>}
      {workspace && !busy && <WorkspaceContent key={`${session.id}:${workspace.workspace_id}:${workspace.role}`} workspace={workspace} client={session.client} onExpired={expire} />}
    </div>}
  </main>;
}

function WorkspaceContent({ workspace, client, onExpired }: { workspace: Workspace; client: WorkspaceApi; onExpired: () => void }) {
  const [papers, setPapers] = useState<WorkspacePaper[]>([]);
  const [view, setView] = useState<"library" | "research" | "reading">("library");
  const [researchScope, setResearchScope] = useState<{ ids: string[]; revision: number } | null>(null);
  const [activeQuery, setActiveQuery] = useState(false);
  const [jobs, setJobs] = useState<IngestionJob[]>([]);
  const [error, setError] = useState("");
  const [syncError, setSyncError] = useState("");
  const [notice, setNotice] = useState("");
  const [loading, setLoading] = useState(true);
  const [blocked, setBlocked] = useState(false);
  const [revision, setRevision] = useState(0);
  const [pending, setPending] = useState<{ file: File; key: string } | null>(null);
  const [uploading, setUploading] = useState(false);
  const [workingJob, setWorkingJob] = useState<string | null>(null);
  const [preview, setPreview] = useState<{ url: string; title: string; documentId: string; page: number } | null>(null);
  const [opening, setOpening] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  const scope = useRef<AbortController | null>(null);
  const previewUrls = useRef(new Set<string>());
  const onExpiredRef = useRef(onExpired);
  onExpiredRef.current = onExpired;

  useEffect(() => {
    const controller = new AbortController();
    scope.current = controller;
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const [documents, tasks] = await client.snapshot(workspace.workspace_id, controller.signal);
        if (controller.signal.aborted) return;
        setPapers(documents);
        setSyncError("");
        setPreview((current) => current && !documents.some((paper) => paper.document_id === current.documentId) ? null : current);
        setJobs(tasks);
        setLoading(false);
        timer = setTimeout(() => void poll(), tasks.some(activeJob) ? 2000 : 10000);
      } catch (reason) {
        if (controller.signal.aborted) return;
        setSyncError(workspaceError(reason));
        setLoading(false);
        if (reason instanceof WorkspaceApiError && reason.status === 401) { onExpiredRef.current(); return; }
        if (reason instanceof WorkspaceApiError && reason.status === 403) {
          controller.abort(); setPapers([]); setJobs([]); setPreview(null); setPending(null); setBlocked(true);
          if (input.current) input.current.value = "";
          return;
        }
        timer = setTimeout(() => void poll(), 10000);
      }
    }
    void poll();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [client, workspace.workspace_id, revision]);

  useEffect(() => {
    const urls = previewUrls.current;
    return () => { for (const url of urls) URL.revokeObjectURL(url); urls.clear(); };
  }, []);
  useEffect(() => () => {
    if (preview) { URL.revokeObjectURL(preview.url); previewUrls.current.delete(preview.url); }
  }, [preview]);

  function failed(reason: unknown) {
    setError(workspaceError(reason));
    if (reason instanceof WorkspaceApiError && reason.status === 401) onExpiredRef.current();
    if (reason instanceof WorkspaceApiError && reason.status === 403) {
      scope.current?.abort(); setPapers([]); setJobs([]); setPreview(null); setPending(null); setBlocked(true);
      if (input.current) input.current.value = "";
    }
  }

  async function upload(event: FormEvent) {
    event.preventDefault();
    const signal = scope.current?.signal;
    if (!pending || !signal || signal.aborted || uploading || blocked) return;
    setUploading(true); setError(""); setNotice("");
    try {
      const job = await client.upload(workspace.workspace_id, pending.file, pending.key, signal);
      if (signal.aborted) return;
      setJobs((current) => [job, ...current.filter((item) => item.job_id !== job.job_id)]);
      setPending(null);
      if (input.current) input.current.value = "";
      setNotice(`已接收本次上传任务：${statusNames[job.status]}。`);
      setRevision((current) => current + 1);
    } catch (reason) { if (!signal.aborted) failed(reason); }
    finally { if (!signal.aborted) setUploading(false); }
  }

  async function cancel(job: IngestionJob) {
    const signal = scope.current?.signal;
    if (!signal || signal.aborted || workingJob) return;
    setWorkingJob(job.job_id); setError("");
    try {
      const updated = await client.cancel(workspace.workspace_id, job.job_id, signal);
      if (!signal.aborted) setJobs((current) => current.map((item) => item.job_id === updated.job_id ? updated : item));
    } catch (reason) { if (!signal.aborted) failed(reason); }
    finally { if (!signal.aborted) setWorkingJob(null); }
  }

  async function openPdf(paper: WorkspacePaper, page = 1, keepCurrentView = false) {
    const signal = scope.current?.signal;
    if (!signal || signal.aborted || opening || blocked) return;
    setOpening(true); setError("");
    try {
      const blob = await client.pdf(workspace.workspace_id, paper.document_id, signal);
      if (!signal.aborted) {
        const url = URL.createObjectURL(blob);
        previewUrls.current.add(url);
        setPreview({ url, title: paper.title || paper.source_filename, documentId: paper.document_id, page });
        if (!keepCurrentView) setView("library");
      }
    } catch (reason) { if (!signal.aborted) failed(reason); }
    finally { if (!signal.aborted) setOpening(false); }
  }

  const activeJobsCount = jobs.filter(activeJob).length;
  const totalChunks = papers.reduce((sum, item) => sum + item.chunk_count, 0);

  return <>
    {syncError && <p role="alert" className={styles.error}>{syncError}</p>}
    {error && <p role="alert" className={styles.error}>{error}</p>}
    {notice && <p role="status" className={styles.notice}>{notice}</p>}

    <div className={styles.topControlStrip}>
      <nav className={styles.featureNav} aria-label="工作空间功能">
        <button aria-pressed={view === "library"} onClick={() => setView("library")}>论文与上传</button>
        <button aria-pressed={view === "research"} onClick={() => setView("research")}>论文问答</button>
        <button aria-pressed={view === "reading"} onClick={() => setView("reading")}>阅读与集合</button>
      </nav>
      <div className={styles.quickStats}>
        <span className={styles.badge}>{papers.length} 篇论文 · {totalChunks} 检索片段</span>
        {activeJobsCount > 0 && <span className={styles.badge} data-state="parsing">{activeJobsCount} 项摄取任务运行中</span>}
        {activeQuery && <span className={styles.badge} data-state="running">问答执行中</span>}
      </div>
    </div>

    <div className={preview ? styles.workbenchSplit : undefined}>
      <div>
        <div hidden={view !== "library"}>
          <div className={styles.columns}>
            <section className={styles.panel}>
              <div className={styles.panelHead}>
                <div><p className={styles.eyebrow}>LIBRARY</p><h1>论文库</h1></div>
                <span className={styles.muted}>{papers.length} 篇可访问论文</span>
              </div>
              {loading ? <p role="status" className={styles.muted}>正在加载论文…</p> : !papers.length && <p className={styles.empty}>{blocked ? "当前内容已清除，请刷新工作空间。" : "还没有可访问的论文。上传后将在这里显示。"}</p>}
              <ul className={styles.paperList}>{papers.map((paper) => <li key={paper.document_id}>
                <div>
                  <h2>{paper.title || paper.source_filename}</h2>
                  <p className={styles.muted}>{paper.chunk_count} 个检索片段 · {paper.source_filename}</p>
                </div>
                <div className={styles.paperRowActions}>
                  <button onClick={() => {
                    setResearchScope((current) => ({ ids: [paper.document_id], revision: (current?.revision ?? 0) + 1 }));
                    setView("research");
                  }} disabled={activeQuery || blocked} title="仅针对该论文发起问答">单篇提问</button>
                  <button className={styles.primary} onClick={() => void openPdf(paper)} disabled={opening || blocked}>查看 PDF</button>
                </div>
              </li>)}</ul>
            </section>

            <section className={styles.panel}>
              <div className={styles.panelHead}>
                <div><p className={styles.eyebrow}>ADD A PAPER</p><h2>上传论文</h2></div>
              </div>
              {workspace.role === "viewer" ? <p className={styles.muted}>你当前拥有只读权限，不能上传论文。</p> : <form onSubmit={upload} className={styles.uploadForm}>
                <div className={styles.dropzoneBox}>
                  <label htmlFor="paper-file">选择 PDF 文件</label>
                  <input ref={input} id="paper-file" type="file" accept="application/pdf,.pdf" disabled={uploading || blocked || loading} onChange={(event) => {
                    const file = event.target.files?.[0];
                    setPending(file ? { file, key: crypto.randomUUID() } : null);
                    setError(""); setNotice("");
                  }} />
                  <p className={styles.muted}>解析与索引在后台运行。断网重试会复用本次请求，避免重复创建任务。供应商调用可能产生费用。</p>
                </div>
                <button className={styles.primary} disabled={!pending || uploading || blocked || loading}>{uploading ? "正在提交…" : "提交解析"}</button>
                {pending && <small className={styles.muted}>已选文件：{pending.file.name} · 保留当前文件可使用同一请求重试；重新选择文件将创建新请求。</small>}
              </form>}
            </section>
          </div>

          <section className={styles.panel}>
            <div className={styles.panelHead}>
              <div><p className={styles.eyebrow}>YOUR INGESTION JOBS</p><h2>我的上传任务</h2></div>
              <span className={styles.muted}>最近 50 项 · 自动更新</span>
            </div>
            {!jobs.length && <p className={styles.empty}>{loading ? "正在加载任务…" : "暂无上传任务。"}</p>}
            <ul className={styles.jobList}>{jobs.map((job) => <li key={job.job_id}>
              <div className={styles.jobDetails}>
                <strong>{job.filename}</strong>
                <code>{job.job_id}</code>
                {job.error_code && <small>{failureNames[job.error_code] ?? "任务未完成"}</small>}
              </div>
              <span className={styles.badge} data-state={job.status}>{statusNames[job.status]}</span>
              {activeJob(job) && <button disabled={Boolean(workingJob) || blocked} onClick={() => void cancel(job)}>{workingJob === job.job_id ? "取消中…" : "取消任务"}</button>}
            </li>)}</ul>
            <p className={styles.muted} style={{ marginTop: 12 }}>取消会停止本地执行，已经提交的供应商解析与费用可能无法撤销。失败任务不会自动重复付费；重新提交请重新选择文件。</p>
          </section>
        </div>

        {!loading && !blocked && <>
          <div hidden={view !== "research"}>
            <WorkspaceChat
              client={client}
              workspaceId={workspace.workspace_id}
              papers={papers}
              onError={failed}
              selectionRequest={researchScope}
              onRunningChange={setActiveQuery}
              onOpenPdf={(id, page) => {
                const paper = papers.find((item) => item.document_id === id);
                if (paper) void openPdf(paper, page, true);
              }}
            />
          </div>
          <div hidden={view !== "reading"}>
            <WorkspacePersonal
              client={client}
              workspace={workspace}
              papers={papers}
              onError={failed}
              activeQuery={activeQuery}
              onUseCollection={(ids) => {
                setResearchScope((current) => ({ ids, revision: (current?.revision ?? 0) + 1 }));
                setView("research");
              }}
            />
          </div>
        </>}
      </div>

      {/* Always-available Split-Screen PDF Preview Stage (side-by-side on wide screens) */}
      {preview && <section className={`${styles.panel} ${styles.pdfSplitStage}`} aria-label="PDF 预览">
        <div className={styles.panelHead}>
          <div>
            <p className={styles.eyebrow}>AUTHENTICATED PDF READER · 第 {preview.page} 页</p>
            <h2>{preview.title}</h2>
          </div>
          <div className={styles.paperRowActions}>
            <a href={preview.url} download="scholarrag-paper.pdf">下载 PDF</a>
            <button onClick={() => setPreview(null)}>关闭预览</button>
          </div>
        </div>
        <iframe className={styles.pdf} src={`${preview.url}#page=${preview.page}`} title={preview.title} />
      </section>}
    </div>
  </>;
}
