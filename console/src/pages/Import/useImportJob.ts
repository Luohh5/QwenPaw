import { useCallback, useEffect, useRef, useState } from "react";
import { portabilityImportApi } from "../../api/modules/import";
import type {
  ImportJobEvent,
  ImportJobSnapshot,
  ImportSelection,
  ImportSource,
  ImportSourceProbe,
} from "../../api/types/import";
import { useAgentStore } from "../../stores/agentStore";

const terminal = new Set([
  "completed",
  "completed_with_issues",
  "failed",
  "interrupted",
]);
const ACTIVE_JOB = "qwenpaw.portability.activeImport";
type ActiveJob = [string, string];

function loadActiveJob(): ActiveJob | null {
  try {
    return JSON.parse(
      sessionStorage.getItem(ACTIVE_JOB) ?? "null",
    ) as ActiveJob | null;
  } catch {
    return null;
  }
}

function saveActiveJob(value: ActiveJob | null) {
  try {
    if (value) sessionStorage.setItem(ACTIVE_JOB, JSON.stringify(value));
    else sessionStorage.removeItem(ACTIVE_JOB);
  } catch {
    /* Storage is only a navigation recovery hint. */
  }
}

export function useImportJob() {
  const { selectedAgent } = useAgentStore();
  const [sources, setSources] = useState<ImportSourceProbe[]>([]);
  const [job, setJob] = useState<ImportJobSnapshot | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const pinnedAgent = useRef("");
  const latest = useRef<ImportJobSnapshot | null>(null);
  const controller = useRef<AbortController | null>(null);

  const accept = useCallback((event: ImportJobEvent) => {
    if (event.seq <= (latest.current?.seq ?? -1)) return;
    latest.current = event.snapshot;
    setJob(event.snapshot);
  }, []);

  const watch = useCallback(
    async (agentId: string, jobId: string, signal: AbortSignal) => {
      while (!signal.aborted && !terminal.has(latest.current?.state ?? "")) {
        try {
          await portabilityImportApi.streamEvents(
            agentId,
            jobId,
            latest.current?.seq ?? 0,
            accept,
            signal,
          );
          if (terminal.has(latest.current?.state ?? "")) return;
          const snapshot = await portabilityImportApi.snapshot(agentId, jobId);
          accept({ seq: snapshot.seq, snapshot });
        } catch (reason) {
          if (signal.aborted) return;
          setError(reason instanceof Error ? reason.message : String(reason));
        }
        await new Promise((resolve) => setTimeout(resolve, 750));
      }
    },
    [accept],
  );

  const detect = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const result = await portabilityImportApi.sources(selectedAgent);
      setSources(result);
      return result;
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
      throw reason;
    } finally {
      setLoading(false);
    }
  }, [selectedAgent]);

  const scan = useCallback(
    async (selected: ImportSource[]) => {
      controller.current?.abort();
      const abort = new AbortController();
      controller.current = abort;
      pinnedAgent.current = selectedAgent;
      setLoading(true);
      setError("");
      try {
        const created = await portabilityImportApi.create(
          selectedAgent,
          selected,
        );
        saveActiveJob([selectedAgent, created.job_id]);
        latest.current = created;
        setJob(created);
        void watch(selectedAgent, created.job_id, abort.signal);
        return created;
      } catch (reason) {
        setError(reason instanceof Error ? reason.message : String(reason));
        throw reason;
      } finally {
        setLoading(false);
      }
    },
    [selectedAgent, watch],
  );

  const start = useCallback(
    async (selections: Partial<Record<ImportSource, ImportSelection>>) => {
      if (!latest.current || !pinnedAgent.current) {
        throw new Error("Import job has not been created");
      }
      setLoading(true);
      setError("");
      try {
        const started = await portabilityImportApi.start(
          pinnedAgent.current,
          latest.current.job_id,
          selections,
        );
        latest.current = started;
        setJob(started);
        return started;
      } catch (reason) {
        setError(reason instanceof Error ? reason.message : String(reason));
        throw reason;
      } finally {
        setLoading(false);
      }
    },
    [],
  );

  const reset = useCallback(() => {
    controller.current?.abort();
    controller.current = null;
    pinnedAgent.current = "";
    latest.current = null;
    saveActiveJob(null);
    setJob(null);
    setError("");
  }, []);

  useEffect(() => {
    const active = loadActiveJob();
    if (active) {
      const [agentId, jobId] = active;
      const abort = new AbortController();
      controller.current = abort;
      void portabilityImportApi
        .snapshot(agentId, jobId)
        .then((snapshot) => {
          if (abort.signal.aborted) return;
          pinnedAgent.current = agentId;
          latest.current = snapshot;
          setJob(snapshot);
          if (!terminal.has(snapshot.state)) {
            void watch(agentId, jobId, abort.signal);
          }
        })
        .catch((reason) => {
          if (!abort.signal.aborted) {
            saveActiveJob(null);
            setError(reason instanceof Error ? reason.message : String(reason));
          }
        });
    }
    return () => controller.current?.abort();
  }, [watch]);
  return { sources, job, loading, error, detect, scan, start, reset };
}
