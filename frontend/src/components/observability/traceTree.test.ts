import { describe, expect, it } from 'vitest';
import { buildTraceTree } from './traceTree';
const at = (ms: number) => new Date(1700000000000 + ms).toISOString();
describe('trace waterfall', () => {
  it('restores start times from completion logs and keeps parallel tools in the same round', () => {
    const { root, origin, duration } = buildTraceTree({ trace: { created_at: at(1000), duration_ms: 1000 }, llm_calls: [
      { model: 'a', created_at: at(200), duration_ms: 200 },
      { model: 'a', created_at: at(900), duration_ms: 200 },
    ], tool_calls: [
      { tool_name: 'one', created_at: at(600), duration_ms: 300, call_order: 1 },
      { tool_name: 'two', created_at: at(650), duration_ms: 350, call_order: 1, is_error: true },
    ] });
    expect(origin).toBe(Date.parse(at(0)));
    expect(duration).toBe(1000);
    expect(root.children![0].children!.map(n => n.name)).toEqual(['a', 'one', 'two']);
    expect(root.children![0].failed).toBe(true);
    expect(root.children![0].end).toBe(Date.parse(at(650)));
    expect(root.children![1].children).toHaveLength(1);
  });
  it('keeps tools without timing or a preceding model unassociated', () => {
    const { root } = buildTraceTree({ trace: {}, llm_calls: [], tool_calls: [{ tool_name: 'unknown' }, { tool_name: 'early', created_at: at(100), duration_ms: 100 }] });
    expect(root.children![0].kind).toBe('unknown');
    expect(root.children![0].children).toHaveLength(2);
    expect(root.children![0].children![0].start).toBeNull();
  });
  it('handles empty and zero-duration traces without invalid axis values', () => {
    const tree = buildTraceTree({ trace: { created_at: at(0), duration_ms: 0 }, llm_calls: [], tool_calls: [] });
    expect(tree.duration).toBe(1);
    expect(tree.root.children).toBeUndefined();
  });

  it('uses recorded rounds and attaches each delegated agent to its exact tool call', () => {
    const { root, inferred } = buildTraceTree({
      trace: { trace_id: 'root' },
      child_traces: [
        { trace_id: 'one', agent_name: 'First', parent_tool_span_id: 'first-tool' },
        { trace_id: 'two', agent_name: 'Second', parent_tool_span_id: 'second-tool' },
      ],
      llm_calls: [
        { trace_id: 'root', model: 'failed attempt', iteration: 1, final_status: 'failed' },
        { trace_id: 'root', model: 'successful retry', iteration: 1 },
        { trace_id: 'root', model: 'next round', iteration: 2 },
        { trace_id: 'one', model: 'child model', iteration: 1 },
      ],
      tool_calls: [
        { trace_id: 'root', tool_name: 'delegate_task', span_id: 'second-tool', iteration: 1 },
        { trace_id: 'root', tool_name: 'delegate_task', span_id: 'first-tool', iteration: 1 },
      ],
    });
    expect(inferred).toBe(false);
    expect(root.children?.map(node => node.name)).toEqual(['第 1 轮', '第 2 轮']);
    const calls = root.children?.[0].children ?? [];
    expect(calls.filter(node => node.kind === 'llm')).toHaveLength(2);
    const first = calls.find(node => node.key === 'first-tool')?.children?.[0];
    expect(first?.name).toBe('First');
    expect(first?.children?.[0].children?.[0].name).toBe('child model');
    expect(calls.find(node => node.key === 'second-tool')?.children?.[0].name).toBe('Second');
  });
});
