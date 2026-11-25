import type { ObsTraceDetail } from '@/lib/api';

type Fields = Record<string, unknown>;

export interface TraceNode {
  key: string;
  name: string;
  kind: 'agent' | 'round' | 'llm' | 'tool' | 'unknown';
  start: number | null;
  end: number | null;
  failed: boolean;
  data: Fields;
  children?: TraceNode[];
}

function interval(data: Fields) {
  const end = typeof data.created_at === 'string' ? Date.parse(data.created_at) : NaN;
  const duration = typeof data.duration_ms === 'number' && data.duration_ms >= 0 ? data.duration_ms : null;
  return {
    end: Number.isFinite(end) ? end : null,
    start: Number.isFinite(end) && duration !== null ? end - duration : null,
  };
}

const compareStart = (a: TraceNode, b: TraceNode) => (a.start ?? Infinity) - (b.start ?? Infinity);

function callNode(data: Fields, kind: 'llm' | 'tool', index: number): TraceNode {
  return {
    key: typeof data.span_id === 'string' ? data.span_id : `${kind}-${index}`,
    kind,
    data,
    ...interval(data),
    name: String(kind === 'llm' ? data.model ?? 'LLM' : data.tool_name ?? '工具'),
    failed: data.final_status === 'failed' || data.is_error === true,
  };
}

function group(key: string, name: string, kind: 'round' | 'unknown', children: TraceNode[]): TraceNode {
  if (kind === 'round') children.sort(compareStart);
  const starts = children.flatMap(node => node.start === null ? [] : [node.start]);
  const ends = children.flatMap(node => node.end === null ? [] : [node.end]);
  return {
    key, name, kind, children, data: {},
    start: kind === 'round' && starts.length ? Math.min(...starts) : null,
    end: kind === 'round' && ends.length ? Math.max(...ends) : null,
    failed: children.some(node => node.failed),
  };
}

function agentRounds(key: string, calls: TraceNode[]) {
  if (calls.some(node => typeof node.data.iteration === 'number')) {
    const byIteration = new Map<number, TraceNode[]>();
    const unmatched: TraceNode[] = [];
    for (const call of calls) {
      const iteration = call.data.iteration;
      if (typeof iteration !== 'number') {
        unmatched.push(call);
        continue;
      }
      const siblings = byIteration.get(iteration) ?? [];
      siblings.push(call);
      byIteration.set(iteration, siblings);
    }
    const rounds = [...byIteration].sort(([a], [b]) => a - b).map(([iteration, children]) =>
      group(`${key}-round-${iteration}`, `第 ${iteration} 轮`, 'round', children));
    if (unmatched.length) rounds.push(group(`${key}-unmatched`, '未关联调用', 'unknown', unmatched));
    return { rounds, inferred: false };
  }

  // Historical rows only have timing. Associate a tool with the latest
  // model call that completed before it started.
  const llms = calls.filter(call => call.kind === 'llm').sort(compareStart);
  const batches = llms.map(llm => [llm]);
  const unmatched: TraceNode[] = [];
  for (const tool of calls.filter(call => call.kind === 'tool')) {
    let parent = -1;
    if (tool.start !== null) {
      for (let i = llms.length - 1; i >= 0; i -= 1) {
        const end = llms[i].end;
        if (end !== null && end <= tool.start) {
          parent = i;
          break;
        }
      }
    }
    if (parent === -1) unmatched.push(tool);
    else batches[parent].push(tool);
  }
  const rounds = batches.map((batch, index) =>
    group(`${key}-round-${index + 1}`, `第 ${index + 1} 轮`, 'round', batch));
  if (unmatched.length) rounds.push(group(`${key}-unmatched`, '未关联工具', 'unknown', unmatched));
  return { rounds, inferred: calls.length > 0 };
}

export function flattenTrace(node: TraceNode): TraceNode[] {
  return [node, ...(node.children ?? []).flatMap(flattenTrace)];
}

export function buildTraceTree(detail: ObsTraceDetail) {
  const rootKey = String(detail.trace.trace_id ?? 'agent');
  const callsByAgent = new Map<string, TraceNode[]>();
  const toolsBySpan = new Map<string, TraceNode>();
  function addCall(data: Fields, kind: 'llm' | 'tool', index: number) {
    const agentKey = String(data.trace_id ?? rootKey);
    const call = callNode(data, kind, index);
    const calls = callsByAgent.get(agentKey) ?? [];
    calls.push(call);
    callsByAgent.set(agentKey, calls);
    if (kind === 'tool' && typeof data.span_id === 'string') toolsBySpan.set(data.span_id, call);
  }
  detail.llm_calls.forEach((data, index) => addCall(data, 'llm', index));
  detail.tool_calls.forEach((data, index) => addCall(data, 'tool', index));

  let inferred = false;
  function agent(data: Fields, key: string, isChild: boolean): TraceNode {
    const { rounds, inferred: agentInferred } = agentRounds(key, callsByAgent.get(key) ?? []);
    inferred ||= agentInferred;
    const fallback = isChild ? `子 Agent ${String(data.agent_id ?? key).slice(0, 8)}` : 'Agent 执行';
    return {
      key, name: String(data.agent_name ?? fallback), kind: 'agent', data, ...interval(data),
      failed: ['error', 'timeout', 'interrupted'].includes(String(data.status)),
      children: rounds.length ? rounds : undefined,
    };
  }

  const root = agent(detail.trace, rootKey, false);
  const orphans: TraceNode[] = [];
  for (const data of detail.child_traces ?? []) {
    const child = agent(data, String(data.trace_id), true);
    const parent = toolsBySpan.get(String(data.parent_tool_span_id));
    if (parent) (parent.children ??= []).push(child);
    else orphans.push(child);
  }
  if (orphans.length) (root.children ??= []).push(group(`${rootKey}-orphaned`, '未关联的子 Agent', 'unknown', orphans));

  const nodes = flattenTrace(root);
  const starts = nodes.flatMap(node => node.start === null ? [] : [node.start]);
  const ends = nodes.flatMap(node => node.end === null ? [] : [node.end]);
  const origin = starts.length ? Math.min(...starts) : 0;
  const duration = Math.max(1, ends.length ? Math.max(...ends) - origin : 1);
  return { root, origin, duration, inferred };
}
