export function sourceIds(metadata: Record<string, unknown>): string[] {
  const raw = metadata.source_session;
  return [...new Set((Array.isArray(raw) ? raw : raw ? [raw] : [])
    .filter((value): value is string => typeof value === 'string'))];
}
