import { buildAuthHeaders } from "../authHeaders";
import { getApiUrl } from "../config";
import { request } from "../request";
import type {
  ImportJobEvent,
  ImportJobSnapshot,
  ImportSelection,
  ImportSource,
  ImportSourceProbe,
} from "../types/import";

const base = (agentId: string) =>
  `/agents/${encodeURIComponent(agentId)}/portability/imports`;

export const portabilityImportApi = {
  sources: (agentId: string) =>
    request<ImportSourceProbe[]>(`${base(agentId)}/sources`),

  create: (agentId: string, sources: ImportSource[]) =>
    request<ImportJobSnapshot>(`${base(agentId)}/jobs`, {
      method: "POST",
      body: JSON.stringify({ sources }),
    }),

  snapshot: (agentId: string, jobId: string) =>
    request<ImportJobSnapshot>(
      `${base(agentId)}/jobs/${encodeURIComponent(jobId)}`,
    ),

  start: (
    agentId: string,
    jobId: string,
    selections: Partial<Record<ImportSource, ImportSelection>>,
  ) =>
    request<ImportJobSnapshot>(
      `${base(agentId)}/jobs/${encodeURIComponent(jobId)}/start`,
      {
        method: "POST",
        body: JSON.stringify({ selections }),
      },
    ),

  streamEvents: async (
    agentId: string,
    jobId: string,
    after: number,
    onEvent: (event: ImportJobEvent) => void,
    signal: AbortSignal,
  ): Promise<void> => {
    const path = `${base(agentId)}/jobs/${encodeURIComponent(jobId)}/events`;
    const response = await fetch(getApiUrl(`${path}?after=${after}`), {
      headers: buildAuthHeaders(),
      signal,
    });
    if (!response.ok || !response.body) {
      throw new Error(`Import event stream failed: ${response.status}`);
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (!signal.aborted) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop() ?? "";
      for (const line of lines) {
        if (!line.startsWith("data:")) continue;
        const payload = JSON.parse(line.slice(5).trim()) as
          | ImportJobEvent
          | { error: string };
        if ("error" in payload) throw new Error(payload.error);
        onEvent(payload);
      }
    }
  },
};
