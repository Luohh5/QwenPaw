import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import type { CronJobSpecOutput } from "../../../api/types";

// ---- Hoisted mocks ----

const mockApi = vi.hoisted(() => ({
  listCronJobs: vi.fn(),
  createCronJob: vi.fn(),
  replaceCronJob: vi.fn(),
  promoteCronJob: vi.fn(),
  deleteCronJob: vi.fn(),
  triggerCronJob: vi.fn(),
}));

const mockMessage = vi.hoisted(() => ({
  success: vi.fn(),
  error: vi.fn(),
  warning: vi.fn(),
  info: vi.fn(),
}));

vi.mock("../../../stores/agentStore", () => ({
  useAgentStore: () => ({ selectedAgent: "agent-1" }),
}));

vi.mock("../../../hooks/useAppMessage", () => ({
  useAppMessage: () => ({ message: mockMessage }),
}));

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string) => key,
    i18n: { language: "en" },
  }),
}));

vi.mock("../../../api", () => ({
  default: mockApi,
}));

import { useCronJobs } from "./useCronJobs";

const mockCronJobs: CronJobSpecOutput[] = [
  {
    id: "job-1",
    name: "Daily Report",
    enabled: true,
    schedule: { type: "cron", cron: "0 9 * * *" },
    task_type: "text",
    text: "Generate daily report",
    dispatch: {
      type: "channel",
      target: { user_id: "u1", session_id: "s1" },
    },
  },
  {
    id: "job-2",
    name: "Weekly Cleanup",
    enabled: false,
    schedule: { type: "cron", cron: "0 0 * * 0" },
    task_type: "text",
    text: "Clean up old data",
    dispatch: {
      type: "channel",
      target: { user_id: "u1", session_id: "s1" },
    },
  },
];

function pendingImportedJob(remote = false): CronJobSpecOutput {
  return {
    id: remote ? "remote-imported" : "imported-1",
    enabled: false,
    name: "Imported",
    schedule: { type: "cron", cron: "0 9 * * *" },
    task_type: "agent",
    request: {
      input: [{ role: "user", content: "original" }],
      request_context: {
        source: "cron",
        portability_review_required: true,
        project_dir: remote ? "" : "/old/project",
      },
    },
    dispatch: {
      type: "channel",
      target: { user_id: "system", session_id: "isolated" },
      meta: { portability: { source: "qoder" }, audit: "keep" },
    },
    meta: {
      portability: {
        requires_review: true,
        safety: "disabled_until_explicit_promotion",
        source: "qoder",
        source_cwd_remote_or_unverified: remote,
      },
      audit: "keep",
    },
  };
}

describe("useCronJobs (#2250 + A#80724854 编辑/批量操作)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockApi.listCronJobs.mockResolvedValue([...mockCronJobs]);
  });

  describe("编辑功能 (#2250)", () => {
    it("updateJob 成功后用 API 返回值更新列表", async () => {
      const updatedJob = { ...mockCronJobs[0], name: "Updated Report" };
      mockApi.replaceCronJob.mockResolvedValue(updatedJob);

      const { result } = renderHook(() => useCronJobs());

      await vi.waitFor(() => {
        expect(result.current.jobs).toHaveLength(2);
      });

      let success = false;
      await act(async () => {
        success = await result.current.updateJob("job-1", {
          ...mockCronJobs[0],
          name: "Updated Report",
        } as CronJobSpecOutput);
      });

      expect(success).toBe(true);
      expect(mockApi.replaceCronJob).toHaveBeenCalledWith(
        "job-1",
        expect.objectContaining({ name: "Updated Report" }),
      );
      expect(result.current.jobs.find((j) => j.id === "job-1")!.name).toBe(
        "Updated Report",
      );
      expect(mockMessage.success).toHaveBeenCalledWith("Updated successfully");
    });

    it("updateJob 失败后回滚到原始数据（乐观更新回滚）", async () => {
      mockApi.replaceCronJob.mockRejectedValue(
        new Error('Network error - {"detail":"save failed"}'),
      );

      const { result } = renderHook(() => useCronJobs());

      await vi.waitFor(() => {
        expect(result.current.jobs).toHaveLength(2);
      });

      const originalName = result.current.jobs.find(
        (j) => j.id === "job-1",
      )!.name;

      let success = false;
      await act(async () => {
        success = await result.current.updateJob("job-1", {
          ...mockCronJobs[0],
          name: "Will Fail",
        } as CronJobSpecOutput);
      });

      expect(success).toBe(false);
      expect(result.current.jobs.find((j) => j.id === "job-1")!.name).toBe(
        originalName,
      );
      expect(mockMessage.error).toHaveBeenCalled();
    });

    it("toggleEnabled 乐观更新 enabled 状态", async () => {
      const toggledJob = { ...mockCronJobs[0], enabled: false };
      mockApi.replaceCronJob.mockResolvedValue(toggledJob);

      const { result } = renderHook(() => useCronJobs());

      await vi.waitFor(() => {
        expect(result.current.jobs).toHaveLength(2);
      });

      expect(result.current.jobs.find((j) => j.id === "job-1")!.enabled).toBe(
        true,
      );

      await act(async () => {
        await result.current.toggleEnabled(mockCronJobs[0]);
      });

      expect(result.current.jobs.find((j) => j.id === "job-1")!.enabled).toBe(
        false,
      );
    });

    it("toggleEnabled 失败后回滚 enabled 状态", async () => {
      mockApi.replaceCronJob.mockRejectedValue(new Error("Server error"));

      const { result } = renderHook(() => useCronJobs());

      await vi.waitFor(() => {
        expect(result.current.jobs).toHaveLength(2);
      });

      await act(async () => {
        await result.current.toggleEnabled(mockCronJobs[0]);
      });

      expect(result.current.jobs.find((j) => j.id === "job-1")!.enabled).toBe(
        true,
      );
      expect(mockMessage.error).toHaveBeenCalledWith("Operation failed");
    });
  });

  describe("批量操作后状态更新 (A#80724854)", () => {
    it("deleteJob 成功后从列表中移除", async () => {
      mockApi.deleteCronJob.mockResolvedValue(undefined);

      const { result } = renderHook(() => useCronJobs());

      await vi.waitFor(() => {
        expect(result.current.jobs).toHaveLength(2);
      });

      let success = false;
      await act(async () => {
        success = await result.current.deleteJob("job-1");
      });

      expect(success).toBe(true);
      expect(result.current.jobs).toHaveLength(1);
      expect(result.current.jobs[0].id).toBe("job-2");
      expect(mockMessage.success).toHaveBeenCalledWith("Deleted successfully");
    });

    it("deleteJob 失败后恢复已删除的 job", async () => {
      mockApi.deleteCronJob.mockRejectedValue(new Error("Delete failed"));

      const { result } = renderHook(() => useCronJobs());

      await vi.waitFor(() => {
        expect(result.current.jobs).toHaveLength(2);
      });

      let success = false;
      await act(async () => {
        success = await result.current.deleteJob("job-1");
      });

      expect(success).toBe(false);
      expect(result.current.jobs).toHaveLength(2);
      expect(mockMessage.error).toHaveBeenCalledWith("Failed to delete");
    });

    it("createJob 成功后新 job 插入列表头部", async () => {
      const newJob: CronJobSpecOutput = {
        id: "job-3",
        name: "New Job",
        enabled: true,
        schedule: { type: "cron", cron: "0 12 * * *" },
        task_type: "text",
        text: "New task",
        dispatch: {
          type: "channel",
          target: { user_id: "u1", session_id: "s1" },
        },
      };
      mockApi.createCronJob.mockResolvedValue(newJob);

      const { result } = renderHook(() => useCronJobs());

      await vi.waitFor(() => {
        expect(result.current.jobs).toHaveLength(2);
      });

      let success = false;
      await act(async () => {
        success = await result.current.createJob(newJob);
      });

      expect(success).toBe(true);
      expect(result.current.jobs).toHaveLength(3);
      expect(result.current.jobs[0].id).toBe("job-3");
      expect(mockMessage.success).toHaveBeenCalledWith("Created successfully");
    });

    it("executeNow 触发成功后不改变 job 列表", async () => {
      mockApi.triggerCronJob.mockResolvedValue(undefined);

      const { result } = renderHook(() => useCronJobs());

      await vi.waitFor(() => {
        expect(result.current.jobs).toHaveLength(2);
      });

      let success = false;
      await act(async () => {
        success = await result.current.executeNow("job-1");
      });

      expect(success).toBe(true);
      expect(result.current.jobs).toHaveLength(2);
      expect(mockMessage.success).toHaveBeenCalledWith(
        "Task triggered successfully",
      );
    });
  });
});

describe("useCronJobs imported-job review gate", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("preserves review metadata and only overrides project_dir when edited", async () => {
    const pendingJob = pendingImportedJob();
    const formValues = {
      ...pendingJob,
      name: "Imported edited",
      enabled: true,
      meta: {
        portability: {
          requires_review: false,
          safety: "reviewed_disabled",
        },
      },
      dispatch: {
        ...pendingJob.dispatch,
        meta: { audit: "erase" },
      },
      request: {
        input: [{ role: "user", content: "edited" }],
        request_context: {
          project_dir: "  /new/local-project  ",
          source: "tampered",
          portability_review_required: false,
        },
      },
    } as CronJobSpecOutput;
    mockApi.listCronJobs.mockResolvedValue([pendingJob]);
    mockApi.replaceCronJob.mockImplementation(async (_jobId, spec) => spec);

    const { result } = renderHook(() => useCronJobs());
    await vi.waitFor(() => expect(result.current.jobs).toHaveLength(1));

    await act(async () => {
      expect(await result.current.updateJob(pendingJob.id, formValues)).toBe(
        true,
      );
    });

    const submitted = mockApi.replaceCronJob.mock.calls[0][1];
    expect(submitted).toMatchObject({
      id: pendingJob.id,
      name: "Imported edited",
      enabled: false,
      meta: pendingJob.meta,
      dispatch: { meta: pendingJob.dispatch.meta },
      request: {
        input: formValues.request?.input,
        request_context: {
          source: "cron",
          portability_review_required: true,
          project_dir: "/new/local-project",
        },
      },
    });
  });

  it("blocks normal enable and run operations until promotion", async () => {
    const pendingJob = pendingImportedJob();
    mockApi.listCronJobs.mockResolvedValue([pendingJob]);

    const { result } = renderHook(() => useCronJobs());
    await vi.waitFor(() => expect(result.current.jobs).toHaveLength(1));

    await act(async () => {
      expect(await result.current.toggleEnabled(pendingJob)).toBe(false);
      expect(await result.current.executeNow(pendingJob.id)).toBe(false);
    });

    expect(mockApi.replaceCronJob).not.toHaveBeenCalled();
    expect(mockApi.triggerCronJob).not.toHaveBeenCalled();
    expect(mockMessage.error).toHaveBeenCalledWith(
      "cronJobs.importReviewBlocked",
    );
  });

  it("uses the server result after explicit promotion and keeps it disabled", async () => {
    const pendingJob = pendingImportedJob();
    const promotedJob: CronJobSpecOutput = {
      ...pendingJob,
      meta: {
        portability: {
          requires_review: false,
          safety: "reviewed_disabled",
        },
      },
    };
    mockApi.listCronJobs.mockResolvedValue([pendingJob]);
    mockApi.promoteCronJob.mockResolvedValue(promotedJob);

    const { result } = renderHook(() => useCronJobs());
    await vi.waitFor(() => expect(result.current.jobs).toHaveLength(1));

    await act(async () => {
      expect(await result.current.promoteImportedJob(pendingJob.id)).toBe(true);
    });

    expect(mockApi.promoteCronJob).toHaveBeenCalledWith(pendingJob.id);
    expect(result.current.jobs[0]).toEqual(promotedJob);
    expect(result.current.jobs[0].enabled).toBe(false);
  });

  it("does not promote a remote source without a local project mapping", async () => {
    const pendingJob = pendingImportedJob(true);
    mockApi.listCronJobs.mockResolvedValue([pendingJob]);

    const { result } = renderHook(() => useCronJobs());
    await vi.waitFor(() => expect(result.current.jobs).toHaveLength(1));

    await act(async () => {
      expect(await result.current.promoteImportedJob(pendingJob.id)).toBe(
        false,
      );
    });

    expect(mockApi.promoteCronJob).not.toHaveBeenCalled();
    expect(mockMessage.error).toHaveBeenCalledWith(
      "cronJobs.importReviewProjectDirRequired",
    );
    expect(result.current.promotingJobIds.size).toBe(0);
  });
});
