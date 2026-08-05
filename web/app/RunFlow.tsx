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
  const steps = buildAgentSteps(events);
  const multiStep = Boolean(run.agentic ?? (agentSteps.length > 0 || events.some((event) => event.stage_id === "agent.synthesize")));
  const phases = buildPhases([...latest.values()], multiStep, terminal);
  const completedStages = phases.filter((phase) => ["success", "skipped", "warning"].includes(phase.status)).length;
  const toolCallCount = steps.reduce((sum, step) => sum + step.calls.length, 0);
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
      <span>{agentSteps.length ? `${agentSteps.length} 次检索迭代` : toolCallCount ? `${toolCallCount} 次工具检索` : "单轮检索"}</span>
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

    <AgentTrace events={events} running={run.status === "running" || run.status === "queued"} />

    {agentSteps.length > 0 ? <section className="iterationBoard">
      <header><div><span className="eyebrow">Retrieval iterations</span><h4>Agent 将问题拆成 {agentSteps.length} 个可验证子问题</h4></div><small>每次迭代都经过共享 VL 空间与多路召回</small></header>
      <div className="iterationList">{agentSteps.map((stepId, index) => <AgentStep key={stepId} stepId={stepId} index={index} events={events} terminal={terminal} />)}</div>
    </section> : steps.length === 0 && <section className="iterationBoard compact">
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
  { id: "retrieve", label: "检索证据", fallback: "VL + FTS 多路召回与重排", matches: (id) => /^(?:query\.embedding|retrieval\.|agent\.step\.\d+(?:\.|$)|agent\.tool\.)/.test(id) },
  { id: "coverage", label: "覆盖检查", fallback: "判断证据是否足够", optional: "agent", matches: (id) => id === "agent.evidence_gap" },
  { id: "answer", label: "综合回答", fallback: "组织上下文并生成答案", matches: (id) => id === "context.assemble" || id === "generation.answer" || id === "agent.synthesize" },
  { id: "verify", label: "引用校验", fallback: "校验引用与证据跨度", optional: "classic", matches: (id) => id === "citation.validate" },
  { id: "persist", label: "保存轨迹", fallback: "持久化回答和 Trace", matches: (id) => id === "agent.stop" || id === "response.persist" },
  { id: "evaluate", label: "Ragas 复评", fallback: "Judge 评估回答质量", optional: "evaluation", matches: (id) => id.startsWith("evaluation.") },
];

function buildPhases(events: TraceEvent[], multiStep: boolean, terminal: boolean): FlowPhase[] {
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
    const toolLoopControlled = spec.id === "coverage" && !matched.length
      && events.some((event) => event.stage_id === "agent.tool.loop" || event.type.startsWith("agent.tool_call"));
    // Legacy tool-mode runs persisted no ``agent.synthesize`` stage; when the
    // run finished with a successful stop, treat the answer phase as done.
    const legacyDraftAnswer = spec.id === "answer" && !matched.length && terminal
      && events.some((event) => event.stage_id === "agent.stop" && event.status === "success");
    const derived = toolLoopControlled || legacyDraftAnswer;
    return {
      id: spec.id,
      label: spec.label,
      status: derived ? (toolLoopControlled ? "skipped" : "success") : aggregateStatus(matched, terminal),
      summary: meaningful?.summary ?? meaningful?.error ?? (toolLoopControlled
        ? "工具循环自主判断证据充分性"
        : legacyDraftAnswer ? "工具循环直接给出草稿回答" : spec.fallback),
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

function aggregateStatus(events: TraceEvent[], terminal = false): StageStatus {
  if (!events.length) return "pending";
  if (events.some((event) => event.status === "failed")) return "failed";
  if (events.some((event) => event.status === "cancelled")) return "cancelled";
  if (events.some((event) => event.status === "running")) {
    // Append-only event log: a leftover ``running`` event (thinking delta,
    // tool started) never flips once the run itself has reached a terminal
    // state, so demote it to success instead of pinning the phase as live.
    return terminal ? "success" : "running";
  }
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
  const terminal = ["completed", "failed", "cancelled"].includes(runStatus);
  return (!terminal && ordered.find((event) => event.status === "running"))
    ?? ordered.find((event) => event.status === "failed" || event.status === "cancelled")
    ?? (runStatus === "queued" ? undefined : ordered[0]);
}

function agentStepIds(events: TraceEvent[]) {
  return [...new Set(events.flatMap((event) => {
    const match = event.stage_id.match(/^(agent\.step\.\d+)(?:\.|$)/);
    return match ? [match[1]] : [];
  }))].sort((left, right) => Number(left.split(".").at(-1)) - Number(right.split(".").at(-1)));
}

type ToolCall = {
  callId: string;
  name: string;
  argumentsText: string;
  status: StageStatus;
  summary: string | null;
  error: string | null;
  durationMs: number | null;
};

export type AgentStep = {
  /** Thinking deltas accumulated before this round's tool calls. */
  thinking: string;
  /** Tool calls issued after that thinking (parallel calls share one round). */
  calls: ToolCall[];
};

function makeCall(event: TraceEvent, fallbackName: string): ToolCall {
  return {
    callId: String(event.output.call_id ?? event.input.call_id ?? event.event_id),
    name: String(event.output.name ?? event.input.name ?? fallbackName),
    argumentsText: typeof event.input.arguments === "string" ? event.input.arguments : "",
    status: event.status,
    summary: null,
    error: null,
    durationMs: null,
  };
}

function updateCall(call: ToolCall, event: TraceEvent) {
  if (typeof event.input.arguments === "string") call.argumentsText = event.input.arguments;
  if (typeof event.input.name === "string") call.name = event.input.name;
  if (typeof event.output.name === "string") call.name = event.output.name;
  call.status = event.status;
  if (event.summary) call.summary = event.summary;
  if (event.error) call.error = event.error;
  if (event.duration_ms != null) call.durationMs = event.duration_ms;
}

/**
 * Group thinking deltas and tool-call events into execution rounds.
 *
 * A round opens with an assistant thinking segment and closes with the tool
 * calls that segment produced (parallel calls share one round). The event
 * sequence is: thinking… → started → completed → thinking… → started…, so a
 * thinking delta right after calls starts the next round, while consecutive
 * ``started`` events without an intervening delta belong to the same round.
 */
export function buildAgentSteps(events: TraceEvent[]): AgentStep[] {
  const related = events
    .filter((event) =>
      event.type === "agent.thinking.delta"
      || event.type === "agent.tool_call.started"
      || event.type === "agent.tool_call.completed")
    .sort((left, right) => left.seq - right.seq);
  const steps: AgentStep[] = [];
  let thinking = "";
  let calls: ToolCall[] = [];

  const flush = () => {
    if (thinking || calls.length) steps.push({ thinking, calls });
    thinking = "";
    calls = [];
  };

  for (const event of related) {
    if (event.type === "agent.thinking.delta") {
      if (calls.length > 0) flush();
      thinking += String(event.output.text_delta ?? "");
      continue;
    }
    const callId = String(event.output.call_id ?? event.input.call_id ?? event.event_id);
    const fallbackName = event.stage_id.replace(/^agent\.tool\./, "").replace(/\.\d+$/, "");
    if (event.type === "agent.tool_call.started") {
      const existing = [...steps.flatMap((step) => step.calls), ...calls]
        .find((call) => call.callId === callId);
      if (existing) continue;
      calls.push(makeCall(event, fallbackName));
      continue;
    }
    if (event.type === "agent.tool_call.completed") {
      const target = calls.find((call) => call.callId === callId)
        ?? steps.flatMap((step) => step.calls).find((call) => call.callId === callId);
      if (target) updateCall(target, event);
      continue;
    }
  }
  flush();
  return steps;
}

/** Main view: thinking and tool calls as paired execution rounds. */
export function AgentTrace({ events, running }: { events: TraceEvent[]; running: boolean }) {
  const steps = buildAgentSteps(events);
  if (!steps.length) return null;
  const activeIndex = steps.length - 1;
  return <section className="agentTrace">
    <header>
      <div><span className="eyebrow">Agent trace</span><h4>思考与工具调用 · {steps.length} 轮</h4></div>
      <small>每轮 = 模型思考 + 对应工具调用</small>
    </header>
    <div className="agentStepList">
      {steps.map((step, index) => {
        const isActive = running && index === activeIndex;
        const toolNames = step.calls.map((call) => call.name).join(" · ");
        return <article className={`agentStep${isActive ? " active" : ""}`} key={index}>
          <header className="agentStepHead">
            <span className="agentStepIndex">{String(index + 1).padStart(2, "0")}</span>
            <div>
              <b>{step.calls.length ? `${step.calls.length} 个工具调用` : "总结思考"}</b>
              <small>{step.calls.length ? toolNames : (step.thinking.length ? `${step.thinking.length} 字符` : "—")}</small>
            </div>
            {isActive && <span className="agentStepLive">运行中</span>}
          </header>
          {step.thinking && <details className="stepThinking" open={isActive}>
            <summary><span className={`thinkMark ${isActive ? "running" : ""}`}>{isActive ? "◉" : "✓"}</span><div><b>思考</b><small>{isActive ? "正在推理…" : `${step.thinking.length} 字符 · 已折叠`}</small></div>{isActive && <span className="thinkSpinner" />}</summary>
            <div className="thinkBody">{step.thinking}</div>
          </details>}
          {step.calls.length > 0 && <div className="stepCalls">
            {step.calls.map((call, callIndex) => <ToolCallCard key={call.callId} call={call} index={callIndex + 1} />)}
          </div>}
        </article>;
      })}
    </div>
  </section>;
}

/** Compact dock variant: one collapsible row per round.
 *
 * ``running`` must come from the run snapshot status: SSE events are
 * append-only, so a ``started``/``thinking.delta`` event stays ``running``
 * forever even after its completion event arrives. Inferring liveness from
 * event status would keep the trace "executing" long after the run ended.
 */
export function AgentTraceCompact({ events, running }: { events: TraceEvent[]; running?: boolean }) {
  const steps = buildAgentSteps(events);
  if (!steps.length) return null;
  const live = running ?? false;
  return <details className="dockTrace" open={live}>
    <summary>
      <span className={live ? "live" : "done"}>{live ? "◉" : "✓"}</span>
      <b>Agent 执行过程</b>
      <small>{steps.length} 轮 · {live ? "正在执行" : "点击展开"}</small>
    </summary>
    <div className="dockTraceBody">
      {steps.map((step, index) => <article className="dockStep" key={index}>
        <header>
          <b>第 {String(index + 1).padStart(2, "0")} 轮</b>
          <span>{step.calls.length ? `${step.calls.length} 个工具调用` : "思考"}</span>
        </header>
        {step.thinking && <details className="dockStepThinking" open={live && index === steps.length - 1}>
          <summary>思考 · {step.thinking.length} 字符</summary>
          <div>{step.thinking}</div>
        </details>}
        {step.calls.length > 0 && <ul className="dockStepCalls">
          {step.calls.map((call) => <li key={call.callId}>
            <span className={`dockCallState stage-${call.status}`}>{statusIcon(call.status)}</span>
            <div><b>{call.name}</b><small>{call.status === "running" ? "执行中…" : call.summary ?? stageStatusLabel(call.status)}</small></div>
          </li>)}
        </ul>}
      </article>)}
    </div>
  </details>;
}

function ToolCallCard({ call, index }: { call: ToolCall; index: number }) {
  const running = call.status === "running";
  return <article className={`toolCard stage-${call.status}`}>
    <header>
      <span className="toolIndex">{String(index).padStart(2, "0")}</span>
      <span className={`toolState stage-${call.status}`}>{statusIcon(call.status)}</span>
      <div><small>TOOL</small><b>{call.name}</b></div>
      <span className={`toolStatus stage-${call.status}`}>{running ? "执行中" : stageStatusLabel(call.status)}</span>
      {call.durationMs != null && <time>{formatDuration(call.durationMs)}</time>}
    </header>
    <p className="toolSummary">{running ? "工具正在执行，结果将回流给 Agent…" : call.error ?? call.summary ?? "完成"}</p>
    {call.argumentsText && <details className="toolArgs"><summary>参数 <code>{prettyJson(call.argumentsText).length > 60 ? "…" : ""}</code></summary><pre>{prettyJson(call.argumentsText)}</pre></details>}
  </article>;
}

function prettyJson(text: string) {
  try {
    return JSON.stringify(JSON.parse(text), null, 2);
  } catch {
    return text;
  }
}

function AgentStep({ stepId, index, events, terminal = false }: { stepId: string; index: number; events: TraceEvent[]; terminal?: boolean }) {
  const stepEvents = events.filter((event) => event.stage_id === stepId || event.stage_id.startsWith(`${stepId}.`));
  const top = [...stepEvents].reverse().find((event) => event.stage_id === stepId);
  const started = stepEvents.find((event) => event.stage_id === stepId && event.type === "agent.step.started");
  const subquery = String(top?.output.subquery ?? started?.input.subquery ?? `子问题 ${index + 1}`);
  const rawStatus = top?.status ?? aggregateStatus(stepEvents, terminal);
  const status: StageStatus = terminal && rawStatus === "running" ? "success" : rawStatus;
  return <article className={`iterationCard stage-${status}`}>
    <header>
      <span className="iterationIndex">{String(index + 1).padStart(2, "0")}</span>
      <div><small>SUBQUERY</small><b>{subquery}</b><span>{top?.summary ?? pendingSummary("running", status)}</span></div>
      <span className={`iterationState ${status}`}>{statusIcon(status)} {stageStatusLabel(status)}</span>
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
