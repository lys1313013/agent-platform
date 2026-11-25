import { beforeEach, describe, expect, it } from 'vitest';
import { tasksForWorkspace, usePetStore } from './petStore';
import { useChatStore } from './chatStore';
import type { Session, UserPet } from '@/lib/types';

const mockPet = { id: 'pet-1', package: { row_mapping: {} } } as unknown as UserPet;

/** 构造一条渠道任务事件（形状与后端 task_registry._publish 一致） */
function started(label: string, sessionId: string, chatKey: string, startedAt = 0, workspaceId = 'ws-a') {
  return {
    type: 'pet_task_started',
    task: {
      session_id: sessionId,
      label,
      tool: null,
      source: 'feishu',
      chat_key: chatKey,
      agent_id: 'agent-1',
      workspace_id: workspaceId,
      started_at: startedAt,
    },
  };
}

function finished(sessionId: string) {
  return { type: 'pet_task_finished', session_id: sessionId };
}

beforeEach(() => {
  usePetStore.setState({ activePet: mockPet, tasks: {} });
  useChatStore.setState({ sessions: [], selectedWorkspaceId: 'ws-a' });
});

describe('pet task workspace switching', () => {
  it('preserves and updates a local task while another workspace is selected', () => {
    const s = usePetStore.getState();
    s.startTask('local-a', 'Local task');
    useChatStore.getState().setSelectedWorkspace('ws-b');
    expect(tasksForWorkspace(usePetStore.getState().tasks, 'ws-b')).toEqual({});
    s.reportEvent('tool_call', { sessionId: 'local-a', tool: 'lookup' });
    s.applyRemoteTaskEvent({ type: 'pet_task_snapshot', tasks: [] }, 'ws-b');
    useChatStore.getState().setSelectedWorkspace('ws-a');
    expect(tasksForWorkspace(usePetStore.getState().tasks, 'ws-a')['local-a']?.tool).toBe('lookup');
  });

  it('uses the existing session workspace rather than the current selector', () => {
    useChatStore.setState({
      selectedWorkspaceId: 'ws-b',
      sessions: [{ id: 'local-a', workspace_id: 'ws-a' } as Session],
    });
    usePetStore.getState().startTask('local-a', 'Existing conversation');
    expect(tasksForWorkspace(usePetStore.getState().tasks, 'ws-b')).toEqual({});
    expect(tasksForWorkspace(usePetStore.getState().tasks, 'ws-a')).toHaveProperty('local-a');
  });

  it('uses the pet dialog workspace for a session absent from the sidebar', () => {
    useChatStore.setState({ selectedWorkspaceId: 'ws-b' });
    usePetStore.getState().startTask('pet-a', 'Pet task', 'agent-1', 'ws-a');
    expect(tasksForWorkspace(usePetStore.getState().tasks, 'ws-a')).toHaveProperty('pet-a');
  });

  it('does not finish another workspace task when applying a scoped snapshot', () => {
    const s = usePetStore.getState();
    s.applyRemoteTaskEvent(started('A', 'remote-a', 'K-a'));
    s.applyRemoteTaskEvent(started('B', 'remote-b', 'K-b', 0, 'ws-b'));
    s.applyRemoteTaskEvent({ type: 'pet_task_snapshot', tasks: [] }, 'ws-b');
    const tasks = usePetStore.getState().tasks;
    expect(tasks['remote-b']?.status).toBe('done');
    expect(tasks['remote-a']?.status).toBeUndefined();
    expect(tasksForWorkspace(tasks, 'ws-a')).toHaveProperty('remote-a');
  });
});

describe('pet 渠道任务替换逻辑（回归：飞书同会话新消息卡在旧 label）', () => {
  it('同 session 新消息到达替换为最新 label 并回到进行中', () => {
    const s = usePetStore.getState();
    s.applyRemoteTaskEvent(started('你好1', 's1', 'K'));
    s.applyRemoteTaskEvent(finished('s1'));
    // 此时应显示「你好1」done（打勾停留）
    expect(usePetStore.getState().tasks.s1?.label).toBe('你好1');
    expect(usePetStore.getState().tasks.s1?.status).toBe('done');

    // 再发「你好2」（飞书同会话不 /new，复用 session_id）
    s.applyRemoteTaskEvent(started('你好2', 's1', 'K'));

    const t = usePetStore.getState().tasks.s1;
    expect(t?.label).toBe('你好2');
    expect(t?.status).toBeUndefined();
    expect(t?.doneAt).toBeUndefined();
  });

  it('/new 换 session 后同 chat_key 旧残留被新任务替换', () => {
    const s = usePetStore.getState();
    s.applyRemoteTaskEvent(started('旧消息', 's1', 'K'));
    s.applyRemoteTaskEvent(finished('s1'));

    s.applyRemoteTaskEvent(started('新消息', 's2', 'K'));

    const tasks = usePetStore.getState().tasks;
    expect(Object.keys(tasks)).toEqual(['s2']);
    expect(tasks.s2?.label).toBe('新消息');
  });

  it('不同 chat_key 的任务互不替换', () => {
    const s = usePetStore.getState();
    s.applyRemoteTaskEvent(started('对话A', 's1', 'K1'));
    s.applyRemoteTaskEvent(started('对话B', 's2', 'K2'));

    const tasks = usePetStore.getState().tasks;
    expect(Object.keys(tasks).sort()).toEqual(['s1', 's2']);
    expect(tasks.s1?.label).toBe('对话A');
    expect(tasks.s2?.label).toBe('对话B');
  });

  it('新消息到达清除旧的工具名', () => {
    const s = usePetStore.getState();
    s.applyRemoteTaskEvent(started('你好1', 's1', 'K'));
    usePetStore.setState((st) => ({
      tasks: { ...st.tasks, s1: { ...st.tasks.s1!, tool: 'web_search' } },
    }));

    s.applyRemoteTaskEvent(started('你好2', 's1', 'K'));
    expect(usePetStore.getState().tasks.s1?.tool).toBeNull();
  });

  it('pet_task_snapshot 全量同步同样替换为最新 label', () => {
    const s = usePetStore.getState();
    s.applyRemoteTaskEvent(started('你好1', 's1', 'K'));

    s.applyRemoteTaskEvent({
      type: 'pet_task_snapshot',
      tasks: [started('你好2', 's1', 'K', 2).task],
    });

    expect(usePetStore.getState().tasks.s1?.label).toBe('你好2');
  });
});
