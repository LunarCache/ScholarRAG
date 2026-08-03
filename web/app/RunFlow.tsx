type RunStatus = "queued" | "running" | "completed" | "failed" | "cancelled";
type StageStatus = "pending" | "running" | "success" | "skipped" | "warning" | "failed" | "cancelled";

export type QueryRun = {
  run_id: string;
  trace_id: string;
  status: RunStatus;
  question: string;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
  event_count: number;
  error: string | null;
  conversation_id?: string | null;
  parent_run_id?: string | null;
  agentic?: boolean;
};

export type TraceEvent = {
  event_id: string;
  seq: number;
  run_id: string;
  type: string;
  stage_id: string;
  parent_stage_id: string | null;
  label: string;
  status: StageStatus;
  timestamp: string;
  duration_ms: number | null;
  component: string | null;
  model: string | null;
  summary: string | null;
  input: Record<string, unknown>;
  output: Record<string, unknown>;
  trace: Record<string, unknown>;
  error: string | null;
};

export type EvidencePointer = {
  chunk_id: string;
  document_id: string;
  page_number: number;
  bbox: { x0: number; y0: number; x1: number; y1: number } | null;
};

export function RunFlow({
  run,
  events,
  onCancel,
  onEvidenceSelect,
}: {
  run: QueryRun;
  events: TraceEvent[];
  onCancel: () => void;
  onEvidenceSelect: (evidence: EvidencePointer) => void;
}) {
  const latest = latestEvents(events);
  const terminal = ["completed", "failed", "cancelled"].includes(run.status);
  const agentSteps = agentStepIds(events);
  const multiStep = Boolean(run.agentic ?? (agentSteps.length > 0 || events.some((event) => event.stage_id === "agent.synthesize")));
  const phases = buildPhases([...latest.values()], multiStep);
  const completedStages = phases.filter((phase) => ["success", "skipped", "warning"].includes(phase.status)).length;
  const elapsed = elapsedMs(run);
  const candidates = candidateMatrix(events);
  const evaluationEvents = events.filter((event) => event.stage_id.startsWith("evaluation."));
  const latestEvaluationEvents = new Map<string, TraceEvent>();
  for (const event of evaluationEvents) latestEvaluationEvents.set(event.stage_id, event);
  const focus = focusEvent([...latest.values()], run.status);
  const emphasizedFocus = focus && ["running", "failed", "cancelled"].includes(focus.status) ? focus : undefined;
  const plan = latest.get("agent.plan");

  return <section className={`runFlow run-${run.status}`}>
    <header className="runFlowHead">
      <div><span className="eyebrow">Live execution trace</span><h3>{run.question}</h3></div>
      {!terminal && <button onClick={onCancel}>取消</button>}
    </header>
    <div className="runSummary">
      <span className={`runState ${run.status}`}>{statusLabel(run.status)}</span>
      <span>{Math.min(completedStages, phases.length)}/{phases.length} 阶段</span>
      <span>{formatDuration(elapsed)}</span>
      <span>{agentSteps.length ? `${agentSteps.length} 次检索迭代` : "单轮检索"}</span>
      <code>{run.trace_id.slice(0, 12)}</code>
    </div>

    <div className="flowRail" aria-label="运行主流程">
      {phases.map((phase, index) => <div className="flowRailItem" key={phase.id}>
        <article className={`flowNode stage-${phase.status}${emphasizedFocus && phase.events.includes(emphasizedFocus) ? " current" : ""}`}>
          <span className="flowNodeMark">{statusIcon(phase.status)}</span>
          <div><small>{String(index + 1).padStart(2, "0")}</small><b>{phase.label}</b><span>{phase.summary}</span></div>
          {phase.duration > 0 && <time>{formatDuration(phase.duration)}</time>}
        </article>
        {index < phases.length - 1 && <span className={`flowConnector ${phase.status}`}>→</span>}
      </div>)}
    </div>

    <section className="phaseCards" aria-label="各阶段执行详情">
      {phases.map((phase, index) => <article className={`phaseCard stage-${phase.status}${emphasizedFocus && phase.events.includes(emphasizedFocus) ? " current" : ""}`} key={phase.id}>
        <header>
          <span className="phaseCardIcon">{statusIcon(phase.status)}</span>
          <div><small>PHASE {String(index + 1).padStart(2, "0")}</small><h4>{phase.label}</h4></div>
          {phase.duration > 0 && <time>{formatDuration(phase.duration)}</time>}
        </header>
        <p>{phase.summary}</p>
        <footer><span>{stageStatusLabel(phase.status)} · {phase.events.length} stages</span>{phaseTags(phase).map((tag) => <code key={tag}>{tag}</code>)}</footer>
      </article>)}
    </section>

    {plan && <section className="planBrief">
      <div><span>ROUTE</span><b>{String(plan.output.intent ?? "general")} · {String(plan.output.difficulty ?? "standard")}</b><small>{String(plan.output.reason ?? plan.summary ?? "已生成检索计划")}</small></div>
      <div className="planTools">{Array.isArray(plan.output.tools) && plan.output.tools.map((tool) => <code key={String(tool)}>{String(tool)}</code>)}</div>
    </section>}

    {agentSteps.length > 0 ? <section className="iterationBoard">
      <header><div><span className="eyebrow">Retrieval iterations</span><h4>Agent 将问题拆成 {agentSteps.length} 个可验证子问题</h4></div><small>每次迭代都经过共享 VL 空间与多路召回</small></header>
      <div className="iterationList">{agentSteps.map((stepId, index) => <AgentStep key={stepId} stepId={stepId} index={index} events={events} />)}</div>
    </section> : <section className="iterationBoard compact">
      <header><div><span className="eyebrow">Retrieval path</span><h4>单轮混合检索</h4></div><small>Dense、FTS 与 Visual 并行召回</small></header>
      <RetrievalPath events={events} prefix="" />
    </section>}

    {candidates.length > 0 && <details className="evidenceMatrix">
      <summary>候选证据排名矩阵 · {candidates.length} chunks</summary>
      <div><table><thead><tr><th>Chunk / 证据</th><th>Dense</th><th>FTS</th><th>Visual</th><th>RRF</th><th>Rerank</th></tr></thead>
        <tbody>{candidates.map((candidate) => <tr key={candidate.chunk_id}>
          <td><button disabled={!candidate.document_id || !candidate.page_number} onClick={() => candidate.document_id && candidate.page_number && onEvidenceSelect({
            chunk_id: candidate.chunk_id,
            document_id: candidate.document_id,
            page_number: candidate.page_number,
            bbox: candidate.bbox,
          })}><b>{candidate.section_path?.join(" › ") || candidate.chunk_id.slice(0, 18)}</b><span>{candidate.text_preview || candidate.chunk_id}</span></button></td>
          <td>{rankCell(candidate.dense)}</td><td>{rankCell(candidate.lexical)}</td><td>{rankCell(candidate.visual)}</td><td>{rankCell(candidate.fused)}</td><td>{rankCell(candidate.reranked)}</td>
        </tr>)}</tbody></table></div>
    </details>}
    {evaluationEvents.length > 0 && <details className="evaluationTrace" open={evaluationEvents.some((event) => event.status === "running")}>
      <summary>Ragas 评估子轨迹 · {latestEvaluationEvents.size} stages</summary>
      <div className="stageList">{[...latestEvaluationEvents.values()].map((event) => <article className={`stageRow stage-${event.status}`} key={event.stage_id}>
        <span className="stageMark">{statusIcon(event.status)}</span>
        <div className="stageMain"><strong>{event.label}</strong><small>{event.summary ?? (event.status === "running" ? "Judge 正在推理…" : "等待评测状态")}</small></div>
        {event.model && <span className="stageModel">{shortModel(event.model)}</span>}
        <time>{event.duration_ms == null ? "" : formatDuration(event.duration_ms)}</time>
      </article>)}</div>
    </details>}
    {run.error && <div className="runError">{run.error}</div>}
    <details className="runDetails">
      <summary>查看 {events.length} 条完整事件</summary>
      <div className="eventList">{events.map((event) => <article key={event.event_id}>
        <div><b>#{event.seq} {event.label}</b><span>{event.type}</span></div>
        <p>{event.summary ?? event.error ?? "状态更新"}</p>
        <div className="eventMeta">
          {event.component && <code>{event.component}</code>}
          {event.model && <code>{event.model}</code>}
          {event.duration_ms != null && <code>{formatDuration(event.duration_ms)}</code>}
          {typeof event.trace.provider_trace_id === "string" && <code>provider {event.trace.provider_trace_id.slice(0, 14)}</code>}
        </div>
        {(Object.keys(event.input).length > 0 || Object.keys(event.output).length > 0) &&
          <details><summary>输入/输出元数据</summary><pre>{JSON.stringify({ input: event.input, output: event.output }, null, 2)}</pre></details>}
      </article>)}</div>
    </details>
  </section>;
}

type FlowPhase = {
  id: string;
  label: string;
  status: StageStatus;
  summary: string;
  duration: number;
  events: TraceEvent[];
};

type PhaseSpec = {
  id: string;
  label: string;
  fallback: string;
  optional?: "agent" | "classic" | "evaluation";
  matches: (stageId: string) => boolean;
};

const PHASE_SPECS: PhaseSpec[] = [
  { id: "intake", label: "理解问题", fallback: "识别问题与论文范围", matches: (id) => id === "request" || id === "scope.resolve" || id === "agent.classify" },
  { id: "plan", label: "生成计划", fallback: "拆解可验证子问题", optional: "agent", matches: (id) => id === "agent.plan" },
  { id: "retrieve", label: "检索证据", fallback: "VL + FTS 多路召回与重排", matches: (id) => /^(?:query\.embedding|retrieval\.|agent\.step\.\d+(?:\.|$))/.test(id) },
  { id: "coverage", label: "覆盖检查", fallback: "判断证据是否足够", optional: "agent", matches: (id) => id === "agent.evidence_gap" },
  { id: "answer", label: "综合回答", fallback: "组织上下文并生成答案", matches: (id) => id === "context.assemble" || id === "generation.answer" || id === "agent.synthesize" },
  { id: "verify", label: "引用校验", fallback: "校验引用与证据跨度", optional: "classic", matches: (id) => id === "citation.validate" },
  { id: "persist", label: "保存轨迹", fallback: "持久化回答和 Trace", matches: (id) => id === "agent.stop" || id === "response.persist" },
  { id: "evaluate", label: "Ragas 复评", fallback: "Judge 评估回答质量", optional: "evaluation", matches: (id) => id.startsWith("evaluation.") },
];

function buildPhases(events: TraceEvent[], multiStep: boolean): FlowPhase[] {
  return PHASE_SPECS.filter((spec) => {
    const hasEvents = events.some((event) => spec.matches(event.stage_id));
    if (spec.optional === "agent") return multiStep || hasEvents;
    if (spec.optional === "classic") return !multiStep || hasEvents;
    if (spec.optional === "evaluation") return hasEvents;
    return true;
  }).map((spec) => {
    const matched = events.filter((event) => spec.matches(event.stage_id));
    const meaningful = [...matched].sort((left, right) => right.seq - left.seq).find((event) => event.summary || event.error);
    const duration = matched.reduce((sum, event) => {
      if (/^agent\.step\.\d+$/.test(event.stage_id)) return sum;
      return sum + (event.duration_ms ?? 0);
    }, 0);
    return {
      id: spec.id,
      label: spec.label,
      status: aggregateStatus(matched),
      summary: meaningful?.summary ?? meaningful?.error ?? spec.fallback,
      duration,
      events: matched,
    };
  });
}

function phaseTags(phase: FlowPhase) {
  return [...new Set(phase.events.flatMap((event) => [
    event.component,
    event.model ? shortModel(event.model) : null,
  ]).filter((value): value is string => Boolean(value)))].slice(0, 3);
}

function aggregateStatus(events: TraceEvent[]): StageStatus {
  if (!events.length) return "pending";
  if (events.some((event) => event.status === "failed")) return "failed";
  if (events.some((event) => event.status === "cancelled")) return "cancelled";
  if (events.some((event) => event.status === "running")) return "running";
  if (events.some((event) => event.status === "warning")) return "warning";
  if (events.every((event) => event.status === "skipped")) return "skipped";
  return "success";
}

function latestEvents(events: TraceEvent[]) {
  const latest = new Map<string, TraceEvent>();
  for (const event of events) latest.set(event.stage_id, event);
  return latest;
}

function focusEvent(events: TraceEvent[], runStatus: RunStatus) {
  const ordered = [...events].sort((left, right) => right.seq - left.seq);
  return ordered.find((event) => event.status === "running")
    ?? ordered.find((event) => event.status === "failed" || event.status === "cancelled")
    ?? (runStatus === "queued" ? undefined : ordered[0]);
}

function agentStepIds(events: TraceEvent[]) {
  return [...new Set(events.flatMap((event) => {
    const match = event.stage_id.match(/^(agent\.step\.\d+)(?:\.|$)/);
    return match ? [match[1]] : [];
  }))].sort((left, right) => Number(left.split(".").at(-1)) - Number(right.split(".").at(-1)));
}

function AgentStep({ stepId, index, events }: { stepId: string; index: number; events: TraceEvent[] }) {
  const stepEvents = events.filter((event) => event.stage_id === stepId || event.stage_id.startsWith(`${stepId}.`));
  const top = [...stepEvents].reverse().find((event) => event.stage_id === stepId);
  const started = stepEvents.find((event) => event.stage_id === stepId && event.type === "agent.step.started");
  const subquery = String(top?.output.subquery ?? started?.input.subquery ?? `子问题 ${index + 1}`);
  return <article className={`iterationCard stage-${top?.status ?? aggregateStatus(stepEvents)}`}>
    <header>
      <span className="iterationIndex">{String(index + 1).padStart(2, "0")}</span>
      <div><small>SUBQUERY</small><b>{subquery}</b><span>{top?.summary ?? pendingSummary("running", top?.status ?? "running")}</span></div>
      <span className={`iterationState ${top?.status ?? "running"}`}>{statusIcon(top?.status ?? "running")} {stageStatusLabel(top?.status ?? "running")}</span>
      {top?.duration_ms != null && <time>{formatDuration(top.duration_ms)}</time>}
    </header>
    <RetrievalPath events={stepEvents} prefix={`${stepId}.`} />
  </article>;
}

function RetrievalPath({ events, prefix }: { events: TraceEvent[]; prefix: string }) {
  const latest = latestEvents(events);
  const event = (suffix: string) => latest.get(`${prefix}${suffix}`);
  return <div className="retrievalPath">
    <MiniStage label="Scope" event={event("scope.resolve")} />
    <span className="miniArrow">→</span>
    <MiniStage label="VL Embed" event={event("query.embedding.vl") ?? event("query.embedding.text")} />
    <span className="miniArrow">→</span>
    <div className="parallelRecall">
      <small>PARALLEL RECALL</small>
      <MiniStage label="Dense" event={event("retrieval.dense")} />
      <MiniStage label="FTS" event={event("retrieval.lexical")} />
      <MiniStage label="Visual" event={event("retrieval.visual")} />
    </div>
    <span className="miniArrow">→</span>
    <MiniStage label="RRF" event={event("retrieval.fusion")} />
    <span className="miniArrow">→</span>
    <MiniStage label="VL Rerank" event={event("retrieval.rerank")} />
  </div>;
}

function MiniStage({ label, event }: { label: string; event?: TraceEvent }) {
  const status = event?.status ?? "pending";
  return <div className={`miniStage stage-${status}`} title={event?.summary ?? label}>
    <span>{statusIcon(status)}</span><div><b>{label}</b><small>{event?.summary ?? pendingSummary("running", status)}</small></div>
    {event?.duration_ms != null && <time>{formatDuration(event.duration_ms)}</time>}
  </div>;
}

function stageStatusLabel(status: StageStatus) {
  return { pending: "等待", running: "运行中", success: "完成", skipped: "跳过", warning: "警告", failed: "失败", cancelled: "取消" }[status];
}

type CandidateValue = { rank: number; score: number; winning_modality?: "text" | "image" };
type CandidateRow = {
  chunk_id: string;
  document_id?: string;
  page_number?: number;
  bbox: EvidencePointer["bbox"];
  section_path?: string[];
  text_preview?: string;
  dense?: CandidateValue;
  lexical?: CandidateValue;
  visual?: CandidateValue;
  fused?: CandidateValue;
  reranked?: CandidateValue;
};

function candidateMatrix(events: TraceEvent[]) {
  const stageToColumn = {
    "retrieval.dense": "dense",
    "retrieval.lexical": "lexical",
    "retrieval.visual": "visual",
    "retrieval.fusion": "fused",
    "retrieval.rerank": "reranked",
  } as const;
  const rows = new Map<string, CandidateRow>();
  for (const event of events) {
    const stage = Object.keys(stageToColumn).find((candidate) => event.stage_id === candidate || event.stage_id.endsWith(`.${candidate}`));
    const column = stage ? stageToColumn[stage as keyof typeof stageToColumn] : undefined;
    const values = event.output.top_candidates;
    if (!column || !Array.isArray(values)) continue;
    for (const value of values) {
      if (!isCandidate(value)) continue;
      const row = rows.get(value.chunk_id) ?? { chunk_id: value.chunk_id, bbox: null };
      row[column] = {
        rank: value.rank,
        score: value.score,
        winning_modality: value.winning_modality === "image" || value.winning_modality === "text"
          ? value.winning_modality
          : undefined,
      };
      if (typeof value.document_id === "string") row.document_id = value.document_id;
      if (typeof value.page_number === "number") row.page_number = value.page_number;
      if (isBbox(value.bbox)) row.bbox = value.bbox;
      if (Array.isArray(value.section_path)) row.section_path = value.section_path.filter((item): item is string => typeof item === "string");
      if (typeof value.text_preview === "string") row.text_preview = value.text_preview;
      rows.set(value.chunk_id, row);
    }
  }
  return [...rows.values()].sort((left, right) =>
    (left.reranked?.rank ?? left.fused?.rank ?? 999) - (right.reranked?.rank ?? right.fused?.rank ?? 999));
}

function isCandidate(value: unknown): value is Record<string, unknown> & { chunk_id: string; rank: number; score: number } {
  if (!value || typeof value !== "object") return false;
  const candidate = value as Record<string, unknown>;
  return typeof candidate.chunk_id === "string" && typeof candidate.rank === "number" && typeof candidate.score === "number";
}

function isBbox(value: unknown): value is EvidencePointer["bbox"] & object {
  if (!value || typeof value !== "object") return false;
  const bbox = value as Record<string, unknown>;
  return ["x0", "y0", "x1", "y1"].every((key) => typeof bbox[key] === "number");
}

function rankCell(value?: CandidateValue) {
  const modality = value?.winning_modality === "image" ? " · 图" : value?.winning_modality === "text" ? " · 文" : "";
  return value ? <span title={`score ${value.score.toFixed(4)}${modality}`}>#{value.rank}<small>{value.score.toFixed(3)}{modality}</small></span> : "—";
}

function elapsedMs(run: QueryRun) {
  const start = new Date(run.started_at ?? run.created_at).getTime();
  const end = run.completed_at ? new Date(run.completed_at).getTime() : Date.now();
  return Math.max(0, end - start);
}

function statusLabel(status: RunStatus) {
  return {
    queued: "排队中",
    running: "运行中",
    completed: "已完成",
    failed: "失败",
    cancelled: "已取消",
  }[status];
}

function statusIcon(status: StageStatus) {
  return {
    pending: "○",
    running: "◉",
    success: "✓",
    skipped: "–",
    warning: "!",
    failed: "×",
    cancelled: "×",
  }[status];
}

function pendingSummary(runStatus: RunStatus, stageStatus: StageStatus) {
  if (stageStatus === "running") return "正在运行";
  return runStatus === "failed" || runStatus === "cancelled" ? "未执行" : "等待上游阶段";
}

function shortModel(model: string) {
  return model.includes("/") ? model.split("/").at(-1) : model;
}

function formatDuration(value: number) {
  if (value < 1000) return `${Math.round(value)} ms`;
  return `${(value / 1000).toFixed(value < 10000 ? 1 : 0)} s`;
}
