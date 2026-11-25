import { Button } from 'antd';
import { useChatStore } from '@/stores/chatStore';

export default function SessionHistoryMore() {
  const hasMore = useChatStore(state => state.hasMoreSessions);
  const busy = useChatStore(state => state.isSessionsLoading);
  const error = useChatStore(state => state.sessionListError);
  const refresh = useChatStore(state => state.refreshSessions);
  const agent = useChatStore(state => state.sessionListAgent);
  const load = useChatStore(state => state.loadMoreSessions);
  if (error) return <Button type="text" size="small" block
    onClick={() => void refresh(agent)}>会话加载失败，点击重试</Button>;
  return hasMore ? <Button type="text" size="small" block loading={busy} onClick={() => void load()}>
    加载更多会话
  </Button> : null;
}
