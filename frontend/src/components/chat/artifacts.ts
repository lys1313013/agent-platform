import type { FileChangeInfo, ToolCallInfo } from '@/lib/types';
import { inferFileMimeType } from '@/components/files/workspaceFile';

export function parseArtifactLink(href: string) {
  const page = /^\/artifacts\/webpages\/([a-f0-9]{32})$/.exec(href);
  if (page) return { kind: 'webpage' as const, pageId: page[1] };
  if (!href.startsWith('/workspace/')) return null;
  try {
    const path = decodeURIComponent(href.slice('/workspace/'.length));
    // Reject control characters in decoded artifact paths, including NUL.
    // eslint-disable-next-line no-control-regex
    const hasUnsafeCharacters = /[\\\u0000-\u001f]/.test(path);
    if (!path || path.split('/').some((part) => !part || part === '.' || part === '..')
      || hasUnsafeCharacters) return null;
    return { kind: 'file' as const, path, filename: path.split('/').pop()! };
  } catch {
    return null;
  }
}

export function workspaceRelativePath(path: string, slug: string) {
  return path.startsWith(`${slug}/`) ? path.slice(slug.length + 1) : path;
}

interface WebpageResult {
  page_id: string;
  title: string;
  url: string;
}

/** Parse the JSON payload returned by the create_webpage tool handler. */
export function parseWebpageResult(preview?: string): WebpageResult | null {
  if (!preview) return null;
  try {
    const data = JSON.parse(preview);
    if (data && typeof data.page_id === 'string' && typeof data.url === 'string') {
      return {
        page_id: data.page_id,
        title: typeof data.title === 'string' ? data.title : '未命名网页',
        url: data.url,
      };
    }
  } catch {
    /* not a webpage result */
  }
  return null;
}

function findFileChangeForPath(
  path: unknown,
  files?: FileChangeInfo[] | null,
): FileChangeInfo | undefined {
  if (typeof path !== 'string') return undefined;
  const normalized = path.replace(/\\/g, '/').replace(/\/$/, '');
  return files?.find((file) => normalized === file.path || normalized.endsWith(`/${file.path}`));
}

/**
 * 新消息优先使用后端记录的文件变更；旧消息则从成功的写入/编辑结果中恢复路径。
 * 旧消息没有创建/修改快照，这里的 action 只用于展示，不作为审计依据。
 */
export function resolveToolFileChange(
  toolCall: ToolCallInfo,
  files: FileChangeInfo[] | null | undefined,
  workspaceId: string | null | undefined,
): FileChangeInfo | undefined {
  const recorded = findFileChangeForPath(toolCall.arguments.path, files);
  if (recorded) return recorded;
  if (
    !workspaceId
    || toolCall.result?.status !== 'ok'
    || !['write_file', 'edit_file'].includes(toolCall.name)
  ) return undefined;

  const preview = toolCall.result.preview || '';
  const resultPath = preview.match(/^Written to (.+)$/m)?.[1]
    || preview.match(/^Edited (.+) successfully$/m)?.[1];
  const argumentPath = typeof toolCall.arguments.path === 'string'
    ? toolCall.arguments.path
    : '';
  const path = (resultPath || argumentPath)
    .trim()
    .replace(/\\/g, '/')
    .replace(/^\/workspace\//, '')
    .replace(/^\.\//, '')
    .replace(/^\/+/, '');
  if (!path || path.split('/').includes('..')) return undefined;

  const filename = path.split('/').pop() || path;
  return {
    action: toolCall.name === 'write_file' ? 'created' : 'modified',
    workspace_id: workspaceId,
    path,
    filename,
    mime_type: inferFileMimeType(filename),
    size: 0,
  };
}
