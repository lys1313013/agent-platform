import { useCallback, useState, type ReactNode } from 'react';
import { App } from 'antd';
import { webpagesApi, workspacesApi } from '@/lib/api';
import { useWebpagePreviewStore } from '@/stores/webpagePreviewStore';
import { parseArtifactLink, workspaceRelativePath } from './artifacts';
import WorkspaceFilePreview from '@/components/files/WorkspaceFilePreview';

export default function ArtifactLink({ href = '', children, workspaceId }: {
  href?: string; children?: ReactNode; workspaceId?: string | null;
}) {
  const artifact = parseArtifactLink(href);
  const [filePath, setFilePath] = useState<string | null>(null);
  const closeFile = useCallback(() => setFilePath(null), []);
  const { message } = App.useApp();
  if (!artifact) return <a href={href}>{children}</a>;

  return <>
    <a href={href} onClick={async (event) => {
      event.preventDefault();
      if (artifact.kind === 'file') {
        if (!workspaceId) { message.error('当前会话没有可访问的工作区'); return; }
        try {
          const workspaces = await workspacesApi.list();
          const workspace = workspaces.find((item) => item.id === workspaceId);
          if (!workspace) { message.error('当前工作区不存在或无权访问'); return; }
          setFilePath(workspaceRelativePath(artifact.path, workspace.slug));
        } catch {
          message.error('无法读取工作区信息，请重试');
        }
        return;
      }
      const title = event.currentTarget.textContent || '网页';
      const store = useWebpagePreviewStore.getState();
      if (store.panelAvailable) {
        store.openPreview({ pageId: artifact.pageId, title });
      } else {
        // 在用户点击时打开窗口，避免异步请求后被浏览器当作弹窗拦截。
        const popup = window.open('about:blank', '_blank');
        if (popup) popup.opener = null;
        try {
          const { url } = await webpagesApi.getAccess(artifact.pageId);
          if (popup) popup.location.replace(url);
          else message.error('请允许弹出窗口后重试');
        } catch {
          popup?.close();
          message.error('网页加载失败，可能已被删除');
        }
      }
    }}>{children}</a>
    {artifact.kind === 'file' && workspaceId && filePath && <WorkspaceFilePreview
      workspaceId={workspaceId} path={filePath} filename={artifact.filename}
      open={true} onClose={closeFile}
    />}
  </>;
}
