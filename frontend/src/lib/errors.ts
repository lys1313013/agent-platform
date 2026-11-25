/** Normalize exceptions from fetch, form callbacks and integrations for display. */
export function errorMessage(error: unknown, fallback = '未知错误'): string {
  if (typeof error === 'string' && error) return error;
  if (typeof error === 'object' && error !== null && 'message' in error
    && typeof error.message === 'string' && error.message) return error.message;
  return fallback;
}

export function isFormValidationError(error: unknown): boolean {
  return typeof error === 'object' && error !== null && 'errorFields' in error
    && Array.isArray(error.errorFields);
}
