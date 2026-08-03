"use client";

import { PointerEvent as ReactPointerEvent, useEffect, useMemo, useRef, useState } from "react";

type Summary = {
  parser_gold: { counts: Record<string, number>; sample_count: number; gold_ready: boolean };
  qa_review: { counts: Record<string, number>; case_count: number; minimum_reviewed: number; gold_ready: boolean };
  judge_calibration: { double_rated_runs: number; minimum_double_rated_runs: number; distinct_raters: string[]; correlations: Record<string, Record<string, number | null>>; gold_ready: boolean };
  human_gate_ready: boolean;
  identity_notice: string;
};

type ParserSample = {
  sample_id: string;
  source_filename: string;
  page_number: number;
  status: "proposed" | "annotated" | "reviewed";
  tags: string[];
  notes: string;
  elements: Record<string, unknown>[];
  qa_cases: Record<string, unknown>[];
  annotation?: { annotator?: string; reviewer?: string; annotated_at?: string; reviewed_at?: string };
  image_url: string;
};

type QACase = {
  case_id: string;
  question: string;
  reference: string;
  review_status: "candidate" | "reviewed" | "rejected";
  tags: string[];
  source_chunk_ids: string[];
  source_block_ids: string[];
  generator?: Record<string, unknown>;
  evidence_preview?: Array<{ chunk_id: string; document_id: string; page_numbers: number[]; block_ids: string[]; text: string }>;
};

type JudgeRun = {
  run_id: string;
  created_at: string;
  question: string;
  answer: string;
  metrics: Record<string, number | null>;
};

async function jsonRequest<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init);
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(String(payload.detail ?? `HTTP ${response.status}`));
  }
  return response.json() as Promise<T>;
}

function splitIds(value: string) {
  return value.split(/[\n,]/).map((item) => item.trim()).filter(Boolean);
}

export function ReviewWorkbench({ api, onClose, onStatus }: { api: string; onClose: () => void; onStatus: (value: string) => void }) {
  const [tab, setTab] = useState<"parser" | "qa" | "judge">("parser");
  const [actor, setActor] = useState("");
  const [summary, setSummary] = useState<Summary | null>(null);
  const [samples, setSamples] = useState<ParserSample[]>([]);
  const [qaCases, setQaCases] = useState<QACase[]>([]);
  const [runs, setRuns] = useState<JudgeRun[]>([]);
  const [parserIndex, setParserIndex] = useState(0);
  const [qaIndex, setQaIndex] = useState(0);
  const [runIndex, setRunIndex] = useState(0);
  const [elementsText, setElementsText] = useState("[]");
  const [pageQaText, setPageQaText] = useState("[]");
  const [parserNotes, setParserNotes] = useState("");
  const [qaQuestion, setQaQuestion] = useState("");
  const [qaReference, setQaReference] = useState("");
  const [qaTags, setQaTags] = useState("");
  const [qaChunks, setQaChunks] = useState("");
  const [qaBlocks, setQaBlocks] = useState("");
  const [qaNote, setQaNote] = useState("");
  const [scores, setScores] = useState({ faithfulness: "", answer_relevancy: "", citation_correctness: "" });
  const [ratingNotes, setRatingNotes] = useState("");
  const [busy, setBusy] = useState(false);
  const [drawLabel, setDrawLabel] = useState("paragraph");
  const [drawStart, setDrawStart] = useState<{ x: number; y: number } | null>(null);
  const [drawing, setDrawing] = useState<{ x0: number; y0: number; x1: number; y1: number } | null>(null);
  const canvasRef = useRef<HTMLDivElement>(null);

  const sample = samples[parserIndex];
  const qa = qaCases[qaIndex];
  const run = runs[runIndex];
  const draftElements = useMemo(() => {
    try {
      const value = JSON.parse(elementsText);
      return Array.isArray(value) ? value as Array<Record<string, unknown>> : [];
    } catch { return []; }
  }, [elementsText]);

  async function reload() {
    const [nextSummary, nextSamples, nextQa, nextRuns] = await Promise.all([
      jsonRequest<Summary>(`${api}/api/reviews/summary`),
      jsonRequest<ParserSample[]>(`${api}/api/reviews/parser-gold`),
      jsonRequest<QACase[]>(`${api}/api/reviews/qa`),
      jsonRequest<JudgeRun[]>(`${api}/api/reviews/judge/runs`),
    ]);
    setSummary(nextSummary); setSamples(nextSamples); setQaCases(nextQa); setRuns(nextRuns);
  }

  useEffect(() => {
    setActor(localStorage.getItem("scholarrag-review-actor") ?? "");
    void reload().catch((error) => onStatus(`验收工作台加载失败：${error.message}`));
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!sample) return;
    setElementsText(JSON.stringify(sample.elements ?? [], null, 2));
    setPageQaText(JSON.stringify(sample.qa_cases ?? [], null, 2));
    setParserNotes(sample.notes ?? "");
  }, [sample]);

  useEffect(() => {
    if (!qa) return;
    setQaQuestion(qa.question ?? ""); setQaReference(qa.reference ?? "");
    setQaTags((qa.tags ?? []).join(", ")); setQaChunks((qa.source_chunk_ids ?? []).join("\n"));
    setQaBlocks((qa.source_block_ids ?? []).join("\n")); setQaNote("");
  }, [qa]);

  function requireActor() {
    const value = actor.trim();
    if (value.length < 2) throw new Error("请填写真实姓名或组织内可核验的审阅者 ID");
    localStorage.setItem("scholarrag-review-actor", value);
    return value;
  }

  async function act(task: () => Promise<unknown>, success: string) {
    setBusy(true);
    try { await task(); await reload(); onStatus(success); }
    catch (error) { onStatus(error instanceof Error ? error.message : String(error)); }
    finally { setBusy(false); }
  }

  async function annotate() {
    if (!sample) return;
    const elements = JSON.parse(elementsText) as Record<string, unknown>[];
    const qa_cases = JSON.parse(pageQaText) as Record<string, unknown>[];
    if (!Array.isArray(elements) || !Array.isArray(qa_cases)) throw new Error("elements 和 qa_cases 必须是 JSON 数组");
    await jsonRequest(`${api}/api/reviews/parser-gold/${encodeURIComponent(sample.sample_id)}/annotation`, {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ actor: requireActor(), elements, qa_cases, notes: parserNotes }),
    });
  }

  function point(event: ReactPointerEvent<HTMLDivElement>) {
    const bounds = canvasRef.current?.getBoundingClientRect();
    if (!bounds) return { x: 0, y: 0 };
    return {
      x: Math.max(0, Math.min(1000, ((event.clientX - bounds.left) / bounds.width) * 1000)),
      y: Math.max(0, Math.min(1000, ((event.clientY - bounds.top) / bounds.height) * 1000)),
    };
  }

  function beginDraw(event: ReactPointerEvent<HTMLDivElement>) {
    if (sample?.status === "reviewed") return;
    event.currentTarget.setPointerCapture(event.pointerId);
    const start = point(event);
    setDrawStart(start);
    setDrawing({ x0: start.x, y0: start.y, x1: start.x, y1: start.y });
  }

  function moveDraw(event: ReactPointerEvent<HTMLDivElement>) {
    if (!drawStart) return;
    const current = point(event);
    setDrawing({ x0: Math.min(drawStart.x, current.x), y0: Math.min(drawStart.y, current.y), x1: Math.max(drawStart.x, current.x), y1: Math.max(drawStart.y, current.y) });
  }

  function finishDraw(event: ReactPointerEvent<HTMLDivElement>) {
    if (!drawStart) return;
    const current = point(event);
    const bbox = { x0: Math.round(Math.min(drawStart.x, current.x)), y0: Math.round(Math.min(drawStart.y, current.y)), x1: Math.round(Math.max(drawStart.x, current.x)), y1: Math.round(Math.max(drawStart.y, current.y)), coordinate_space: "normalized_0_1000" };
    setDrawStart(null); setDrawing(null);
    if (bbox.x1 - bbox.x0 < 5 || bbox.y1 - bbox.y0 < 5) return;
    const readingOrders = draftElements.map((item) => Number(item.reading_order)).filter(Number.isFinite);
    setElementsText(JSON.stringify([...draftElements, {
      element_id: `e_${Date.now().toString(36)}`,
      label: drawLabel,
      bbox,
      reading_order: readingOrders.length ? Math.max(...readingOrders) + 1 : 0,
      exclude_from_rag: ["header", "footer", "page_number"].includes(drawLabel),
    }], null, 2));
  }

  function removeElement(elementId: string) {
    setElementsText(JSON.stringify(draftElements.filter((item) => item.element_id !== elementId), null, 2));
  }

  async function reviewParser() {
    if (!sample) return;
    await jsonRequest(`${api}/api/reviews/parser-gold/${encodeURIComponent(sample.sample_id)}/review`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ actor: requireActor() }),
    });
  }

  async function decideQa(decision: "reviewed" | "rejected" | "candidate") {
    if (!qa) return;
    await jsonRequest(`${api}/api/reviews/qa/${encodeURIComponent(qa.case_id)}`, {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        actor: requireActor(), decision, question: qaQuestion, reference: qaReference,
        tags: splitIds(qaTags), source_chunk_ids: splitIds(qaChunks), source_block_ids: splitIds(qaBlocks), note: qaNote,
      }),
    });
  }

  async function rate() {
    if (!run) return;
    const parsed = Object.fromEntries(Object.entries(scores).map(([key, value]) => [key, Number(value)]));
    if (Object.values(parsed).some((value) => !Number.isFinite(value) || value < 0 || value > 1)) throw new Error("三项人工评分必须都在 0 到 1 之间");
    await jsonRequest(`${api}/api/reviews/judge/${encodeURIComponent(run.run_id)}/ratings`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ rater: requireActor(), ...parsed, notes: ratingNotes }),
    });
    setScores({ faithfulness: "", answer_relevancy: "", citation_correctness: "" }); setRatingNotes("");
  }

  const gateCards = useMemo(() => summary ? [
    { label: "Parser Gold", value: `${summary.parser_gold.counts.reviewed ?? 0}/${summary.parser_gold.sample_count}`, ready: summary.parser_gold.gold_ready },
    { label: "QA Reviewed", value: `${summary.qa_review.counts.reviewed ?? 0}/${summary.qa_review.case_count}`, ready: summary.qa_review.gold_ready },
    { label: "Judge 双评分", value: `${summary.judge_calibration.double_rated_runs}/${summary.judge_calibration.minimum_double_rated_runs}`, ready: summary.judge_calibration.gold_ready },
  ] : [], [summary]);

  return <div className="reviewBackdrop" onMouseDown={onClose}>
    <section className="acceptancePanel" onMouseDown={(event) => event.stopPropagation()}>
      <header><div><span className="eyebrow">Human acceptance · append-only audit</span><h2>Gold 验收工作台</h2><p>模型不能代签；提交者身份、前后内容哈希和时间都会进入审计日志。</p></div><button onClick={onClose}>×</button></header>
      <div className="acceptanceSummary">
        {gateCards.map((card) => <div className={card.ready ? "gateReady" : "gatePending"} key={card.label}><span>{card.label}</span><b>{card.value}</b><small>{card.ready ? "PASSED" : "PENDING"}</small></div>)}
        <label>当前审阅者<input aria-label="当前审阅者" value={actor} onChange={(event) => setActor(event.target.value)} placeholder="真实姓名或可核验 ID" /></label>
      </div>
      <div className="acceptanceTabs"><button className={tab === "parser" ? "active" : ""} onClick={() => setTab("parser")}>Parser Gold</button><button className={tab === "qa" ? "active" : ""} onClick={() => setTab("qa")}>QA / EasyDataset</button><button className={tab === "judge" ? "active" : ""} onClick={() => setTab("judge")}>Judge 校准</button></div>
      <div className="acceptanceBody">
        {tab === "parser" && (sample ? <div className="parserWorkbench">
          <aside><select aria-label="Parser Gold 样本" value={parserIndex} onChange={(event) => setParserIndex(Number(event.target.value))}>{samples.map((item, index) => <option key={item.sample_id} value={index}>{index + 1}. {item.sample_id} · {item.status}</option>)}</select>
            <div className="drawTools"><label>框选类型<select aria-label="框选元素类型" value={drawLabel} onChange={(event) => setDrawLabel(event.target.value)}>{["title","heading","paragraph","list","formula","formula_number","table","chart","image","caption","reference","footnote","header","footer","page_number","other"].map((label) => <option key={label}>{label}</option>)}</select></label><span>在页面上拖拽建立 normalized bbox</span></div>
            <div ref={canvasRef} className="bboxCanvas" onPointerDown={beginDraw} onPointerMove={moveDraw} onPointerUp={finishDraw}><img src={`${api}${sample.image_url}`} alt={`${sample.sample_id} page`} draggable={false} />
              {draftElements.map((element) => { const bbox = element.bbox as { x0: number; y0: number; x1: number; y1: number } | undefined; return bbox && <button title="删除此元素框" key={String(element.element_id)} style={{ left: `${bbox.x0 / 10}%`, top: `${bbox.y0 / 10}%`, width: `${(bbox.x1 - bbox.x0) / 10}%`, height: `${(bbox.y1 - bbox.y0) / 10}%` }} onPointerDown={(event) => event.stopPropagation()} onClick={() => removeElement(String(element.element_id))}><span>{String(element.reading_order ?? "—")} · {String(element.label)}</span></button>; })}
              {drawing && <i style={{ left: `${drawing.x0 / 10}%`, top: `${drawing.y0 / 10}%`, width: `${(drawing.x1 - drawing.x0) / 10}%`, height: `${(drawing.y1 - drawing.y0) / 10}%` }} />}
            </div><p>{sample.source_filename} · 第 {sample.page_number} 页 · {(sample.tags ?? []).join(" / ")}</p></aside>
          <section><div className="itemState"><b>{sample.sample_id}</b><span className={`decision ${sample.status === "reviewed" ? "accept" : "review"}`}>{sample.status}</span></div><p className="humanChain">标注者：{sample.annotation?.annotator ?? "—"}　复核者：{sample.annotation?.reviewer ?? "—"}</p>
            <label>页面元素 JSON<textarea aria-label="页面元素 JSON" rows={13} value={elementsText} onChange={(event) => setElementsText(event.target.value)} /></label>
            <label>页面 QA JSON<textarea aria-label="页面 QA JSON" rows={5} value={pageQaText} onChange={(event) => setPageQaText(event.target.value)} /></label>
            <label>标注说明<textarea aria-label="Parser 标注说明" rows={3} value={parserNotes} onChange={(event) => setParserNotes(event.target.value)} /></label>
            <div className="reviewActions"><button disabled={busy || sample.status === "reviewed"} onClick={() => void act(annotate, "Parser 标注已保存并记录审计")}>保存标注</button><button className="primary" disabled={busy || sample.status !== "annotated"} onClick={() => void act(reviewParser, "独立复核已签署")}>确认独立复核</button></div>
          </section>
        </div> : <p>没有 Parser Gold 样本。</p>)}
        {tab === "qa" && (qa ? <div className="qaWorkbench"><div className="itemNav"><button onClick={() => setQaIndex(Math.max(0, qaIndex - 1))}>‹</button><select aria-label="QA 候选" value={qaIndex} onChange={(event) => setQaIndex(Number(event.target.value))}>{qaCases.map((item, index) => <option key={item.case_id} value={index}>{index + 1}. {item.review_status} · {item.question.slice(0, 60)}</option>)}</select><button onClick={() => setQaIndex(Math.min(qaCases.length - 1, qaIndex + 1))}>›</button></div><div className="itemState"><b>{qa.case_id}</b><span className={`decision ${qa.review_status === "reviewed" ? "accept" : qa.review_status === "rejected" ? "fallback" : "review"}`}>{qa.review_status}</span></div>
          <label>问题<textarea aria-label="QA 问题" rows={3} value={qaQuestion} onChange={(event) => setQaQuestion(event.target.value)} /></label><label>参考答案<textarea aria-label="QA 参考答案" rows={5} value={qaReference} onChange={(event) => setQaReference(event.target.value)} /></label>
          <section className="evidencePreview"><h3>Active parse 原文证据</h3>{(qa.evidence_preview ?? []).map((evidence) => <article key={evidence.chunk_id}><div><code>{evidence.chunk_id}</code><span>第 {evidence.page_numbers.join(", ")} 页</span>{evidence.page_numbers[0] && <a href={`${api}/api/documents/${encodeURIComponent(evidence.document_id)}/pages/${evidence.page_numbers[0]}.png`} target="_blank">打开原页 ↗</a>}</div><p>{evidence.text}</p><small>{evidence.block_ids.join(" · ")}</small></article>)}{!qa.evidence_preview?.length && <p className="missingEvidence">当前未链接到 active chunk，不能签署为 reviewed。</p>}</section>
          <div className="qaColumns"><label>Tags<input value={qaTags} onChange={(event) => setQaTags(event.target.value)} /></label><label>Source chunk IDs<textarea rows={4} value={qaChunks} onChange={(event) => setQaChunks(event.target.value)} /></label><label>Source block IDs<textarea rows={4} value={qaBlocks} onChange={(event) => setQaBlocks(event.target.value)} /></label></div><label>审阅说明<textarea rows={3} value={qaNote} onChange={(event) => setQaNote(event.target.value)} /></label><div className="reviewActions"><button disabled={busy} onClick={() => void act(() => decideQa("rejected"), "QA 已拒绝并记录理由")}>拒绝</button><button className="primary" disabled={busy || !qaChunks.trim()} onClick={() => void act(() => decideQa("reviewed"), "QA 已人工审阅并链接证据")}>通过并签署</button></div>
        </div> : <p>没有 QA 候选。可先从 EasyDataset 导入。</p>)}
        {tab === "judge" && (run ? <div className="judgeWorkbench"><div className="itemNav"><button onClick={() => setRunIndex(Math.max(0, runIndex - 1))}>‹</button><select aria-label="Judge 评测运行" value={runIndex} onChange={(event) => setRunIndex(Number(event.target.value))}>{runs.map((item, index) => <option key={item.run_id} value={index}>{index + 1}. {item.run_id}</option>)}</select><button onClick={() => setRunIndex(Math.min(runs.length - 1, runIndex + 1))}>›</button></div><h3>{run.question}</h3><div className="blindAnswer"><span>待人工评分回答</span><p>{run.answer}</p></div><div className="scoreGrid">{(["faithfulness", "answer_relevancy", "citation_correctness"] as const).map((name) => <label key={name}>{name.replaceAll("_", " ")}<input aria-label={name} type="number" min="0" max="1" step="0.05" value={scores[name]} onChange={(event) => setScores((current) => ({ ...current, [name]: event.target.value }))} /></label>)}</div><label>评分说明<textarea rows={4} value={ratingNotes} onChange={(event) => setRatingNotes(event.target.value)} /></label><p className="blindNotice">提交前不显示 Judge 分数，避免锚定偏差；同一 run 至少需要两名不同评分者。</p><div className="reviewActions"><button className="primary" disabled={busy} onClick={() => void act(rate, "人工 Judge 评分已写入追加式记录")}>提交盲评</button></div>
          {summary && <details><summary>当前校准统计</summary><pre>{JSON.stringify(summary.judge_calibration, null, 2)}</pre></details>}
        </div> : <p>暂无 Ragas 评测运行；先对完成的 Query Run 执行评测。</p>)}
      </div>
      <footer><span>{summary?.identity_notice}　<a href={`${api}/api/reviews/audit`} target="_blank">查看哈希审计日志 ↗</a></span><b className={summary?.human_gate_ready ? "passed" : "pending"}>{summary?.human_gate_ready ? "ALL HUMAN GATES PASSED" : "HUMAN GATES NOT YET PASSED"}</b></footer>
    </section>
  </div>;
}
