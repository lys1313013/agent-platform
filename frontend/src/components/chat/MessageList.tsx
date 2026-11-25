import { useCallback, useLayoutEffect, useMemo, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { Button } from 'antd';
import { PlusOutlined } from '@ant-design/icons';
import type { Message, StreamingState } from '@/lib/types';
import BrandLogo from '@/components/BrandLogo';
import ChatMessage from './ChatMessage';
import StreamingMessage from './StreamingMessage';
import { useChatStore } from '@/stores/chatStore';

export interface MessageListProps<T extends Message = Message> {
  messages: T[];
  streaming?: StreamingState;
  streamingMessageId?: string;
  conversationId?: string | null;
  workspaceId?: string | null;
  renderMessage?: (message: T) => ReactNode;
  beforeMessages?: ReactNode;
  afterMessages?: ReactNode;
  /** Agent 或门户脱敏的 PortalAgent —— 仅用 name / description / welcome_message */
  agent?: { name: string; description?: string | null; welcome_message?: string | null } | null;
  onNewChat?: () => void;
  onEditResend?: (content: string) => void;
  /** 覆盖空态欢迎语（宠物弹窗等无独立「新对话」场景） */
  emptyTitle?: string;
  emptySubtitle?: string;
  /** 覆盖空态图标（宠物弹窗用宠物动画代替品牌 Logo） */
  emptyIcon?: ReactNode;
  /** 挂载时滚动到底部（宠物弹窗等场景：打开默认看最新消息） */
  scrollToBottomOnMount?: boolean;
  /** 紧凑模式：窄浮窗场景（宠物对话）下收紧间距与头像尺寸 */
  compact?: boolean;
}

export default function MessageList<T extends Message>({ messages: savedMessages, streaming, streamingMessageId, agent, onNewChat, onEditResend, emptyTitle, emptySubtitle, emptyIcon, scrollToBottomOnMount = true, compact, conversationId, workspaceId: explicitWorkspaceId, renderMessage, beforeMessages, afterMessages }: MessageListProps<T>) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const followBottom = useRef(scrollToBottomOnMount);
  const messages = useMemo(() => streamingMessageId
    ? savedMessages.filter(message => message.id !== streamingMessageId)
    : savedMessages, [savedMessages, streamingMessageId]);
  const firstMessageId = messages[0]?.id;
  const scrollKey = conversationId ?? firstMessageId;
  const lastUserMessageId = useMemo(() => [...messages].reverse().find(message => message.role === 'user')?.id, [messages]);
  // A turn is anchored to its user message, which survives the streaming -> saved handoff.
  const [reasoningChoices, setReasoningChoices] = useState<Record<string, Record<string, boolean>>>({});
  const reasoningVisibility = useCallback((turnId: string | undefined) => {
    const key = JSON.stringify([conversationId, turnId]);
    return {
      values: reasoningChoices[key] ?? {},
      onChange: (id: string, open: boolean) => setReasoningChoices(previous => ({
        ...previous, [key]: { ...previous[key], [id]: open },
      })),
    };
  }, [conversationId, reasoningChoices]);
  const hasContent = messages.length > 0 || streaming?.isStreaming;
  const activeWorkspaceId = useChatStore((state) => (
    state.sessions.find((session) => session.id === state.activeSessionId)?.workspace_id
    ?? state.selectedWorkspaceId
  ));

  const workspaceId = explicitWorkspaceId !== undefined ? explicitWorkspaceId : activeWorkspaceId;
  const history = useMemo(() => {
    let turnId: string | undefined;
    return messages.map(msg => {
      if (renderMessage) return <div key={msg.id}>{renderMessage(msg)}</div>;
      if (msg.role === 'user') turnId = msg.id;
      return <ChatMessage key={msg.id} message={msg} workspaceId={workspaceId}
        onEditResend={onEditResend} compact={compact} reasoningVisibility={reasoningVisibility(turnId ?? msg.id)} />;
    });
  }, [messages, renderMessage, workspaceId, onEditResend, compact, reasoningVisibility]);
  const hasOlder = useChatStore(state => conversationId && !renderMessage && state.hasMoreMessages[conversationId]);
  const loadingOlder = useChatStore(state => !!conversationId && state.loadingOlderMessages[conversationId]);
  const loadOlder = useChatStore(state => state.loadOlderMessages);
  const previousLayout = useRef({ firstId: firstMessageId, height: 0, conversationId });

  // Reset when opening a conversation; sending also resumes following.
  useLayoutEffect(() => {
    followBottom.current = scrollToBottomOnMount;
  }, [scrollKey, scrollToBottomOnMount]);

  useLayoutEffect(() => {
    if (lastUserMessageId) followBottom.current = true;
  }, [lastUserMessageId]);

  useLayoutEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const previous = previousLayout.current;
    const prepended = previous.conversationId === conversationId && previous.firstId !== firstMessageId
      && messages.some(message => message.id === previous.firstId);
    if (prepended) {
      el.scrollTop += el.scrollHeight - previous.height;
    } else if (followBottom.current) el.scrollTop = el.scrollHeight;
    previousLayout.current = { firstId: firstMessageId, height: el.scrollHeight, conversationId };
  }, [messages, streaming, conversationId, firstMessageId]);

  // Images, tool cards and input resizing can change layout after rendering.
  useLayoutEffect(() => {
    const el = scrollRef.current;
    const content = contentRef.current;
    if (!el || !content) return;
    const observer = new ResizeObserver(() => {
      if (followBottom.current) el.scrollTop = el.scrollHeight;
    });
    observer.observe(el);
    observer.observe(content);
    return () => observer.disconnect();
  }, [hasContent]);

  if (messages.length === 0 && !streaming?.isStreaming) {
    const title = emptyTitle ?? (agent ? `欢迎使用 ${agent.name}` : '欢迎使用智能体平台');
    const subtitle = emptySubtitle ?? (agent?.description || '发送消息开始对话');

    return (
      <div className="chat-empty-state flex min-h-0 flex-1 items-center justify-center overflow-y-auto">
        <div className="chat-empty-content flex max-w-md flex-col items-center text-center px-6">
          {/* Logo */}
          <div className={compact ? 'mb-4' : 'mb-6'}>
            {emptyIcon ?? <BrandLogo className="h-16 w-16" />}
          </div>

          {/* Title */}
          <h2 className={`${compact ? 'mb-1 text-base' : 'mb-2 text-xl'} font-bold tracking-tight text-foreground`}>
            {title}
          </h2>
          <p className={`${compact ? 'mb-4' : 'mb-8'} text-sm text-muted-foreground`}>
            {subtitle}
          </p>

          {/* Welcome message bubble */}
          {agent?.welcome_message && (
            <div className="mb-6 max-w-sm rounded-2xl bg-muted/60 px-5 py-3 text-sm leading-relaxed text-foreground/80 shadow-[0_1px_3px_rgba(0,0,0,0.04)]">
              {agent.welcome_message}
            </div>
          )}

          {/* New chat button */}
          {onNewChat && (
            <button
              onClick={onNewChat}
              className="mt-8 flex items-center gap-2 rounded-lg bg-primary px-5 py-2.5 text-sm font-semibold text-primary-foreground transition-colors hover:bg-primary/90"
            >
              <PlusOutlined />
              <span>开始新对话</span>
            </button>
          )}
        </div>
      </div>
    );
  }

  return (
    // data-ui-exclude：text_delta 每 token 改 DOM，快照引擎 MutationObserver 排除本区域（docs/22 §2.2a 规则 8）
    <div
      ref={scrollRef}
      className="chat-message-scroll min-h-0 flex-1 overflow-y-auto"
      onScroll={() => {
        const el = scrollRef.current;
        if (el) followBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 90;
      }}
      data-ui-exclude
    >
      <div ref={contentRef} className={`chat-message-list ${compact ? 'px-3 py-4 space-y-4' : 'max-w-4xl mx-auto px-4 py-6 space-y-6'}`}>
        {beforeMessages}
        {hasOlder && conversationId && <Button type="text" size="small" block loading={loadingOlder}
          onClick={() => void loadOlder(conversationId)}>加载更早消息</Button>}
        {history}
        {afterMessages}
        {streaming?.isStreaming && (
          <StreamingMessage streaming={streaming} workspaceId={workspaceId} compact={compact}
            reasoningVisibility={reasoningVisibility(lastUserMessageId)} />
        )}
      </div>
    </div>
  );
}
