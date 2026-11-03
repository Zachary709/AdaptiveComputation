export class ApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

export async function api<T>(
  path: string,
  options: { method?: string; body?: unknown } = {},
): Promise<T> {
  const headers: Record<string, string> = {};
  if (options.body !== undefined) {
    headers["Content-Type"] = "application/json";
  }
  const response = await fetch(path, {
    method: options.method ?? "GET",
    headers,
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
  });
  if (!response.ok) {
    const text = await response.text();
    throw new ApiError(response.status, text || response.statusText);
  }
  if (response.status === 204) {
    return undefined as T;
  }
  return (await response.json()) as T;
}

export async function streamSnapshots(
  onSnapshot: (snapshotId: number | null) => void,
  signal: AbortSignal,
): Promise<void> {
  const response = await fetch("/api/v1/events", { signal });
  if (!response.ok || response.body === null) {
    throw new ApiError(response.status, "cannot open the event stream");
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) {
      return;
    }
    buffer += decoder.decode(value, { stream: true });
    const blocks = buffer.split("\n\n");
    buffer = blocks.pop() ?? "";
    for (const block of blocks) {
      const dataLine = block
        .split("\n")
        .find((line) => line.startsWith("data:"));
      if (dataLine === undefined) {
        continue;
      }
      const payload = JSON.parse(dataLine.slice(5).trim()) as { snapshot_id: number | null };
      onSnapshot(payload.snapshot_id);
    }
  }
}
