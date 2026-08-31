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
const ACTIVE_JOBS = "qwenpaw.portability.activeImports";
const LEGACY_ACTIVE_JOB = "qwenpaw.portability.activeImport";
const RECONNECT_INITIAL_MS = 750;
const RECONNECT_MAX_MS = 15_000;

function activeJobs(): Record<string, string> {
  try {
    const saved = sessionStorage.getItem(ACTIVE_JOBS);
    if (saved) {
      const value = JSON.parse(saved);
      if (value && typeof value === "object" && !Array.isArray(value)) {
        return Object.fromEntries(
          Object.entries(value).filter(
            ([agentId, jobId]) =>
              typeof agentId === "string" && typeof jobId === "string",
          ),
        ) as Record<string, string>;
      }
    }
    const legacy = JSON.parse(
      sessionStorage.getItem(LEGACY_ACTIVE_JOB) ?? "null",
    );
    return Array.isArray(legacy) &&
      legacy.length === 2 &&
      legacy.every((item) => typeof item === "string")
      ? { [legacy[0]]: legacy[1] }
      : {};
  } catch {
    return {};
  }
}

function saveActiveJob(agentId: string, jobId = "") {
  try {
    const jobs = activeJobs();
    if (jobId) jobs[agentId] = jobId;
    else delete jobs[agentId];
    sessionStorage.setItem(ACTIVE_JOBS, JSON.stringify(jobs));
    sessionStorage.removeItem(LEGACY_ACTIVE_JOB);
  } catch {
    /* Storage is only a navigation recovery hint. */
  }
}

const message = (reason: unknown) =>
  reason instanceof Error ? reason.message : String(reason);

export function useImportJob() {
  const { selectedAgent } = useAgentStore();
  const [sources, setSources] = useState<ImportSourceProbe[]>([]);
  const [job, setJob] = useState<ImportJobSnapshot | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [streamError, setStreamError] = useState("");
  const selectedRef = useRef(selectedAgent);
  const mounted = useRef(true);
  const latest = useRef<ImportJobSnapshot | null>(null);
  const view = useRef({ agentId: selectedAgent, jobId: "" });
  const controller = useRef<AbortController | null>(null);
  selectedRef.current = selectedAgent;

  const isCurrent = useCallback(
    (agentId: string, jobId: string) =>
      mounted.current &&
      selectedRef.current === agentId &&
      view.current.agentId === agentId &&
      view.current.jobId === jobId,
    [],
  );

  const accept = useCallback(
    (agentId: string, jobId: string, event: ImportJobEvent) => {
      if (
        !isCurrent(agentId, jobId) ||
        event.snapshot.job_id !== jobId ||
        event.seq <= (latest.current?.seq ?? -1)
      ) {
        return;
      }
      latest.current = event.snapshot;
      setJob(event.snapshot);
    },
    [isCurrent],
  );

  const watch = useCallback(
    async (agentId: string, jobId: string, signal: AbortSignal) => {
      let delay = RECONNECT_INITIAL_MS;
      while (
        !signal.aborted &&
        isCurrent(agentId, jobId) &&
        !terminal.has(latest.current?.state ?? "")
      ) {
        try {
          await portabilityImportApi.streamEvents(
            agentId,
            jobId,
            latest.current?.seq ?? 0,
            (event) => accept(agentId, jobId, event),
            signal,
            () => {
              delay = RECONNECT_INITIAL_MS;
              if (isCurrent(agentId, jobId)) setStreamError("");
            },
          );
          if (signal.aborted || !isCurrent(agentId, jobId)) return;
          if (terminal.has(latest.current?.state ?? "")) return;
          const snapshot = await portabilityImportApi.snapshot(agentId, jobId);
          accept(agentId, jobId, { seq: snapshot.seq, snapshot });
        } catch (reason) {
          if (signal.aborted || !isCurrent(agentId, jobId)) return;
          setStreamError(message(reason));
        }
        await new Promise((resolve) => setTimeout(resolve, delay));
        delay = Math.min(delay * 2, RECONNECT_MAX_MS);
      }
    },
    [accept, isCurrent],
  );

  const show = useCallback(
    (snapshot: ImportJobSnapshot) => {
      controller.current?.abort();
      const abort = new AbortController();
      controller.current = abort;
      view.current = { agentId: snapshot.agent_id, jobId: snapshot.job_id };
      latest.current = snapshot;
      setStreamError("");
      setJob(snapshot);
      if (!terminal.has(snapshot.state)) {
        void watch(snapshot.agent_id, snapshot.job_id, abort.signal);
      }
    },
    [watch],
  );

  const detect = useCallback(async () => {
    const agentId = selectedAgent;
    const idle = !view.current.jobId;
    if (idle) {
      setLoading(true);
      setError("");
    }
    try {
      const result = await portabilityImportApi.sources(agentId);
      if (mounted.current && selectedRef.current === agentId) {
        setSources(result);
      }
      return result;
    } catch (reason) {
      if (
        mounted.current &&
        idle &&
        selectedRef.current === agentId &&
        !view.current.jobId
      ) {
        setError(message(reason));
      }
      throw reason;
    } finally {
      if (
        mounted.current &&
        idle &&
        selectedRef.current === agentId &&
        !view.current.jobId
      ) {
        setLoading(false);
      }
    }
  }, [selectedAgent]);

  const scan = useCallback(
    async (selected: ImportSource[]) => {
      const agentId = selectedAgent;
      setLoading(true);
      setError("");
      try {
        const created = await portabilityImportApi.create(agentId, selected);
        saveActiveJob(agentId, created.job_id);
        if (isCurrent(agentId, "")) {
          show(created);
        }
        return created;
      } catch (reason) {
        if (mounted.current && selectedRef.current === agentId) {
          setError(message(reason));
        }
        throw reason;
      } finally {
        if (mounted.current && selectedRef.current === agentId) {
          setLoading(false);
        }
      }
    },
    [isCurrent, selectedAgent, show],
  );

  const start = useCallback(
    async (selections: Partial<Record<ImportSource, ImportSelection>>) => {
      const snapshot = latest.current;
      if (!snapshot || !isCurrent(selectedAgent, snapshot.job_id)) {
        throw new Error("Import job has not been created");
      }
      setLoading(true);
      setError("");
      try {
        const started = await portabilityImportApi.start(
          selectedAgent,
          snapshot.job_id,
          selections,
        );
        accept(selectedAgent, snapshot.job_id, {
          seq: started.seq,
          snapshot: started,
        });
        return started;
      } catch (reason) {
        if (isCurrent(selectedAgent, snapshot.job_id))
          setError(message(reason));
        throw reason;
      } finally {
        if (isCurrent(selectedAgent, snapshot.job_id)) setLoading(false);
      }
    },
    [accept, isCurrent, selectedAgent],
  );

  const retry = useCallback(
    async (selections: Partial<Record<ImportSource, ImportSelection>>) => {
      const snapshot = latest.current;
      if (!snapshot || !isCurrent(selectedAgent, snapshot.job_id)) {
        throw new Error("Import job has not been created");
      }
      setLoading(true);
      setError("");
      try {
        const retried = await portabilityImportApi.retry(
          selectedAgent,
          snapshot.job_id,
          selections,
        );
        saveActiveJob(selectedAgent, retried.job_id);
        if (isCurrent(selectedAgent, snapshot.job_id)) show(retried);
        return retried;
      } catch (reason) {
        if (isCurrent(selectedAgent, snapshot.job_id))
          setError(message(reason));
        throw reason;
      } finally {
        if (mounted.current && selectedRef.current === selectedAgent) {
          setLoading(false);
        }
      }
    },
    [isCurrent, selectedAgent, show],
  );

  const cancel = useCallback(async () => {
    const snapshot = latest.current;
    if (!snapshot || !isCurrent(selectedAgent, snapshot.job_id)) {
      throw new Error("Import job has not been created");
    }
    setLoading(true);
    setError("");
    try {
      const interrupted = await portabilityImportApi.cancel(
        selectedAgent,
        snapshot.job_id,
      );
      accept(selectedAgent, snapshot.job_id, {
        seq: interrupted.seq,
        snapshot: interrupted,
      });
      return interrupted;
    } catch (reason) {
      if (isCurrent(selectedAgent, snapshot.job_id)) setError(message(reason));
      throw reason;
    } finally {
      if (isCurrent(selectedAgent, snapshot.job_id)) setLoading(false);
    }
  }, [accept, isCurrent, selectedAgent]);

  const reset = useCallback(() => {
    const agentId = selectedAgent;
    controller.current?.abort();
    controller.current = null;
    view.current = { agentId, jobId: "" };
    latest.current = null;
    saveActiveJob(agentId);
    setJob(null);
    setError("");
    setStreamError("");
  }, [selectedAgent]);

  useEffect(() => {
    controller.current?.abort();
    controller.current = null;
    latest.current = null;
    setJob(null);
    setError("");
    setStreamError("");
    const jobId = activeJobs()[selectedAgent];
    view.current = { agentId: selectedAgent, jobId: jobId ?? "" };
    if (!jobId) {
      setLoading(false);
      return undefined;
    }
    saveActiveJob(selectedAgent, jobId);
    const abort = new AbortController();
    controller.current = abort;
    setLoading(true);
    void portabilityImportApi
      .snapshot(selectedAgent, jobId)
      .then((snapshot) => {
        if (!isCurrent(selectedAgent, jobId) || abort.signal.aborted) return;
        latest.current = snapshot;
        setJob(snapshot);
        if (!terminal.has(snapshot.state)) {
          void watch(selectedAgent, jobId, abort.signal);
        }
      })
      .catch((reason) => {
        if (!isCurrent(selectedAgent, jobId) || abort.signal.aborted) return;
        view.current = { agentId: selectedAgent, jobId: "" };
        latest.current = null;
        saveActiveJob(selectedAgent);
        setError(message(reason));
        setLoading(false);
      })
      .finally(() => {
        if (isCurrent(selectedAgent, jobId) && !abort.signal.aborted) {
          setLoading(false);
        }
      });
    return () => {
      controller.current?.abort();
      controller.current = null;
    };
  }, [isCurrent, selectedAgent, watch]);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      controller.current?.abort();
    };
  }, []);

  return {
    sources,
    job,
    selectedAgent,
    loading,
    error: error || streamError,
    detect,
    scan,
    start,
    retry,
    cancel,
    reset,
  };
}
