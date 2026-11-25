import { useEffect, useState } from 'react';
import { App, Button, Drawer, Empty } from 'antd';
import { DownloadOutlined } from '@ant-design/icons';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { downloadWorkspaceFile, getFilePreviewKind, inferFileMimeType } from './workspaceFile';
import { workspacesApi } from '@/lib/api';

export function FilePreviewContent({
  filename,
  mimeType,
  text,
  previewUrl,
}: {
  filename: string;
  mimeType?: string;
  text: string;
  previewUrl: string;
}) {
  const kind = getFilePreviewKind(filename, mimeType);

  if (kind === 'markdown') {
    return (
      <div className="prose prose-sm max-w-none break-words dark:prose-invert">
        <ReactMarkdown remarkPlugins={[remarkGfm]}>{text || '（空文件）'}</ReactMarkdown>
      </div>
    );
  }
  if (kind === 'text') {
    return (
      <pre className="whitespace-pre-wrap break-words rounded bg-muted p-4 text-sm leading-relaxed">
        {text || '（空文件）'}
      </pre>
    );
  }
  if (kind === 'image' && previewUrl) {
    return <img className="mx-auto max-w-full" src={previewUrl} alt={filename} />;
  }
  if (kind === 'pdf' && previewUrl) {
    return <iframe title={filename} sandbox="" className="h-[75vh] w-full border-0" src={previewUrl} />;
  }
  return <Empty description="暂不支持在线预览，请下载后查看" />;
}

interface WorkspaceFilePreviewProps {
  workspaceId: string;
  path: string;
  filename: string;
  mimeType?: string;
  open: boolean;
  onClose: () => void;
}

export default function WorkspaceFilePreview({
  workspaceId,
  path,
  filename,
  mimeType,
  open,
  onClose,
}: WorkspaceFilePreviewProps) {
  const { message } = App.useApp();
  const [loading, setLoading] = useState(false);
  const [text, setText] = useState('');
  const [previewUrl, setPreviewUrl] = useState('');
  const kind = getFilePreviewKind(filename, mimeType);
  const resolvedMimeType = mimeType || inferFileMimeType(filename);

  useEffect(() => {
    if (!open) return undefined;
    let cancelled = false;
    let objectUrl = '';
    setLoading(true);
    setText('');
    setPreviewUrl('');

    if (kind === 'none') {
      setLoading(false);
      return undefined;
    }

    void workspacesApi.downloadFile(workspaceId, path)
      .then(async (blob) => {
        if (cancelled) return;
        if (kind === 'markdown' || kind === 'text') {
          setText(await blob.text());
        } else {
          objectUrl = URL.createObjectURL(new Blob([blob], { type: resolvedMimeType }));
          setPreviewUrl(objectUrl);
        }
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        message.error(error instanceof Error ? error.message : '文件读取失败');
        onClose();
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [kind, message, onClose, open, path, resolvedMimeType, workspaceId]);

  const handleDownload = async () => {
    try {
      await downloadWorkspaceFile(workspaceId, path, filename);
    } catch (error) {
      message.error(error instanceof Error ? error.message : '文件下载失败');
    }
  };

  return (
    <Drawer
      title={filename}
      size="large"
      open={open}
      loading={loading}
      onClose={onClose}
      extra={<Button icon={<DownloadOutlined />} onClick={handleDownload}>下载</Button>}
    >
      <FilePreviewContent
        filename={filename}
        mimeType={resolvedMimeType}
        text={text}
        previewUrl={previewUrl}
      />
    </Drawer>
  );
}
