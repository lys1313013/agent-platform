import { workspacesApi } from '@/lib/api';

const TEXT_EXTENSIONS = new Set([
  'txt', 'csv', 'json', 'xml', 'yaml', 'yml', 'toml', 'ini', 'cfg', 'conf',
  'py', 'js', 'ts', 'jsx', 'tsx', 'html', 'css', 'scss', 'less',
  'sh', 'bash', 'zsh', 'fish', 'bat', 'ps1',
  'c', 'cpp', 'h', 'hpp', 'java', 'go', 'rs', 'rb', 'php', 'swift', 'kt',
  'sql', 'r', 'lua', 'pl', 'pm', 'tcl', 'log', 'env', 'gitignore',
  'dockerignore', 'editorconfig', 'makefile', 'cmake', 'dockerfile',
]);

const MIME_TYPES: Record<string, string> = {
  csv: 'text/csv',
  gif: 'image/gif',
  html: 'text/html',
  jpeg: 'image/jpeg',
  jpg: 'image/jpeg',
  json: 'application/json',
  md: 'text/markdown',
  markdown: 'text/markdown',
  pdf: 'application/pdf',
  png: 'image/png',
  svg: 'image/svg+xml',
  txt: 'text/plain',
  webp: 'image/webp',
  xml: 'application/xml',
  yaml: 'application/yaml',
  yml: 'application/yaml',
};

export type FilePreviewKind = 'markdown' | 'text' | 'image' | 'pdf' | 'none';

function extensionOf(filename: string): string {
  const lowerName = filename.toLowerCase();
  const ext = lowerName.split('.').pop() || '';
  return TEXT_EXTENSIONS.has(lowerName) ? lowerName : ext;
}

export function inferFileMimeType(filename: string): string {
  const ext = extensionOf(filename);
  if (MIME_TYPES[ext]) return MIME_TYPES[ext];
  if (TEXT_EXTENSIONS.has(ext)) return 'text/plain';
  return 'application/octet-stream';
}

export function getFilePreviewKind(filename: string, mimeType?: string): FilePreviewKind {
  const ext = extensionOf(filename);
  const mime = mimeType || inferFileMimeType(filename);
  if (mime === 'text/markdown' || ext === 'md' || ext === 'markdown') return 'markdown';
  if (mime === 'application/pdf' || ext === 'pdf') return 'pdf';
  if (mime.startsWith('image/')) return 'image';
  if (mime.startsWith('text/') || TEXT_EXTENSIONS.has(ext)) return 'text';
  return 'none';
}

export function isEditableTextFile(filename: string): boolean {
  const kind = getFilePreviewKind(filename);
  return kind === 'markdown' || kind === 'text';
}

export async function downloadWorkspaceFile(
  workspaceId: string,
  path: string,
  filename: string,
): Promise<void> {
  const blob = await workspacesApi.downloadFile(workspaceId, path);
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}
