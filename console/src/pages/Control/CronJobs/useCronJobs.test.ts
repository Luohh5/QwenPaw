import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";

const mockMessage = { success: vi.fn(), error: vi.fn() };

vi.mock("../../../api", () => ({
  default: {
    listCronJobs: vi.fn(),
    createCronJob: vi.fn(),
    replaceCronJob: vi.fn(),
    promoteCronJob: vi.fn(),
    deleteCronJob: vi.fn(),
    triggerCronJob: vi.fn(),
  },
}));
vi.mock("../../../stores/agentStore", () => ({
  useAgentStore: vi.fn(() => ({ selectedAgent: "agent-1" })),
}));
vi.mock("../../../hooks/useAppMessage", () => ({
  useAppMessage: vi.fn(() => ({ message: mockMessage })),
}));
vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (k: string) => k }),
}));
vi.mock("../../../utils/error", () => ({
  parseErrorDetail: vi.fn(),
}));

import api from "../../../api";
import { parseErrorDetail } from "../../../utils/error";
import { useCronJobs } from "./useCronJobs";

type MockJob = { id: string; enabled: boolean; name: string };

const mockJobs: MockJob[] = [
  { id: "j1", enabled: true, name: "Job 1" },
  { id: "j2", enabled: false, name: "Job 2" },
];

describe("useCronJobs", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    (api.listCronJobs as ReturnType<typeof vi.fn>).mockResolvedValue(mockJobs);
    (parseErrorDetail as ReturnType<typeof vi.fn>).mockReturnValue(null);
  });

  // 1. 初始挂载调用 listCronJobs，jobs 被设置，loading 从 true→false
  it("初始挂载调用 listCronJobs，jobs 被设置，loading 最终为 false", async () => {
    const { result } = renderHook(() => useCronJobs());

    await waitFor(() => {
      expect(result.current.loading).toBe(false);
    });

    expect(api.listCronJobs).toHaveBeenCalledTimes(1);
    expect(result.current.jobs).toEqual(mockJobs);
  });

  // 2. createJob 成功：jobs 前置添加新 job，message.success 被调用
  it("createJob 成功：新 job 插入到列表头部，message.success 被调用", async () => {
    const newJob = { id: "j3", enabled: true, name: "Job 3" };
    (api.createCronJob as ReturnType<typeof vi.fn>).mockResolvedValue(newJob);

    const { result } = renderHook(() => useCronJobs());
    await waitFor(() => expect(result.current.loading).toBe(false));

    let returnValue: boolean | undefined;
    await act(async () => {
      returnValue = await result.current.createJob(
        newJob as unknown as Parameters<typeof result.current.createJob>[0],
      );
    });

    expect(returnValue).toBe(true);
    expect(result.current.jobs[0]).toEqual(newJob);
    expect(mockMessage.success).toHaveBeenCalledWith("Created successfully");
  });

  // 3. createJob 失败：message.error 被调用，返回 false
  it("createJob 失败：message.error 被调用，返回 false", async () => {
    (api.createCronJob as ReturnType<typeof vi.fn>).mockRejectedValue(
      new Error("server error"),
    );

    const { result } = renderHook(() => useCronJobs());
    await waitFor(() => expect(result.current.loading).toBe(false));

    let returnValue: boolean | undefined;
    await act(async () => {
      returnValue = await result.current.createJob(
        {} as unknown as Parameters<typeof result.current.createJob>[0],
      );
    });

    expect(returnValue).toBe(false);
    expect(mockMessage.error).toHaveBeenCalled();
  });

  // 4. updateJob 成功：optimistic update 后替换为 API 返回值，message.success
  it("updateJob 成功：乐观更新后用 API 返回值替换，message.success 被调用", async () => {
    const updatedJob = { id: "j1", enabled: true, name: "Job 1 Updated" };
    (api.replaceCronJob as ReturnType<typeof vi.fn>).mockResolvedValue(
      updatedJob,
    );

    const { result } = renderHook(() => useCronJobs());
    await waitFor(() => expect(result.current.loading).toBe(false));

    let returnValue: boolean | undefined;
    await act(async () => {
      returnValue = await result.current.updateJob(
        "j1",
        updatedJob as unknown as Parameters<typeof result.current.updateJob>[1],
      );
    });

    expect(returnValue).toBe(true);
    expect(result.current.jobs.find((j) => j.id === "j1")).toEqual(updatedJob);
    expect(mockMessage.success).toHaveBeenCalledWith("Updated successfully");
  });

  // 5. updateJob 失败：回滚到 original，message.error，返回 false
  it("updateJob 失败：回滚到原始数据，message.error 被调用，返回 false", async () => {
    (api.replaceCronJob as ReturnType<typeof vi.fn>).mockRejectedValue(
      new Error("update failed"),
    );

    const { result } = renderHook(() => useCronJobs());
    await waitFor(() => expect(result.current.loading).toBe(false));

    const originalJob = result.current.jobs.find((j) => j.id === "j1");

    let returnValue: boolean | undefined;
    await act(async () => {
      returnValue = await result.current.updateJob("j1", {
        id: "j1",
        enabled: false,
        name: "Changed",
      } as unknown as Parameters<typeof result.current.updateJob>[1]);
    });

    expect(returnValue).toBe(false);
    expect(result.current.jobs.find((j) => j.id === "j1")).toEqual(originalJob);
    expect(mockMessage.error).toHaveBeenCalled();
  });

  it("待审核任务编辑时保留审核元数据并仅允许覆盖 project_dir", async () => {
    const pendingJob = {
      id: "imported-edit",
      enabled: false,
      name: "Imported original",
      schedule: { type: "cron", cron: "0 9 * * *" },
      task_type: "agent",
      request: {
        input: [{ role: "user", content: "original" }],
        request_context: {
          source: "cron",
          portability_review_required: true,
          project_dir: "/old/project",
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
        },
        audit: "keep",
      },
    };
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
    };
    (api.listCronJobs as ReturnType<typeof vi.fn>).mockResolvedValue([
      pendingJob,
    ]);
    (api.replaceCronJob as ReturnType<typeof vi.fn>).mockImplementation(
      async (_jobId, spec) => spec,
    );

    const { result } = renderHook(() => useCronJobs());
    await waitFor(() => expect(result.current.loading).toBe(false));

    await act(async () => {
      expect(
        await result.current.updateJob(
          pendingJob.id,
          formValues as unknown as Parameters<
            typeof result.current.updateJob
          >[1],
        ),
      ).toBe(true);
    });

    const submitted = (api.replaceCronJob as ReturnType<typeof vi.fn>).mock
      .calls[0][1];
    expect(submitted).toMatchObject({
      id: pendingJob.id,
      name: "Imported edited",
      enabled: false,
      meta: pendingJob.meta,
      dispatch: { meta: pendingJob.dispatch.meta },
      request: {
        input: formValues.request.input,
        request_context: {
          source: "cron",
          portability_review_required: true,
          project_dir: "/new/local-project",
        },
      },
    });
    expect(result.current.jobs[0]).toEqual(submitted);
    expect(result.current.jobs[0].meta?.portability?.requires_review).toBe(
      true,
    );
  });

  // 6. deleteJob 成功：optimistic delete，message.success
  it("deleteJob 成功：乐观删除 job，message.success 被调用", async () => {
    (api.deleteCronJob as ReturnType<typeof vi.fn>).mockResolvedValue(
      undefined,
    );

    const { result } = renderHook(() => useCronJobs());
    await waitFor(() => expect(result.current.loading).toBe(false));

    let returnValue: boolean | undefined;
    await act(async () => {
      returnValue = await result.current.deleteJob("j1");
    });

    expect(returnValue).toBe(true);
    expect(result.current.jobs.find((j) => j.id === "j1")).toBeUndefined();
    expect(mockMessage.success).toHaveBeenCalledWith("Deleted successfully");
  });

  // 7. deleteJob 失败：恢复 original，message.error，返回 false
  it("deleteJob 失败：恢复原始 job，message.error 被调用，返回 false", async () => {
    (api.deleteCronJob as ReturnType<typeof vi.fn>).mockRejectedValue(
      new Error("delete failed"),
    );

    const { result } = renderHook(() => useCronJobs());
    await waitFor(() => expect(result.current.loading).toBe(false));

    let returnValue: boolean | undefined;
    await act(async () => {
      returnValue = await result.current.deleteJob("j1");
    });

    expect(returnValue).toBe(false);
    expect(result.current.jobs.some((j) => j.id === "j1")).toBe(true);
    expect(mockMessage.error).toHaveBeenCalledWith("Failed to delete");
  });

  // 8. toggleEnabled 成功：enabled 翻转，API 调用，message.success
  it("toggleEnabled 成功：enabled 状态翻转，message.success 被调用", async () => {
    const job = mockJobs[0]; // enabled: true
    const toggled = { ...job, enabled: false };
    (api.replaceCronJob as ReturnType<typeof vi.fn>).mockResolvedValue(toggled);

    const { result } = renderHook(() => useCronJobs());
    await waitFor(() => expect(result.current.loading).toBe(false));

    let returnValue: boolean | undefined;
    await act(async () => {
      returnValue = await result.current.toggleEnabled(
        job as unknown as Parameters<typeof result.current.toggleEnabled>[0],
      );
    });

    expect(returnValue).toBe(true);
    expect(api.replaceCronJob).toHaveBeenCalledWith(
      job.id,
      expect.objectContaining({ enabled: false }),
    );
    expect(result.current.jobs.find((j) => j.id === job.id)).toEqual(toggled);
    expect(mockMessage.success).toHaveBeenCalledWith("Disabled");
  });

  // 9. executeNow 成功：triggerCronJob 调用，message.success
  it("executeNow 成功：调用 triggerCronJob，message.success 被调用", async () => {
    (api.triggerCronJob as ReturnType<typeof vi.fn>).mockResolvedValue(
      undefined,
    );

    const { result } = renderHook(() => useCronJobs());
    await waitFor(() => expect(result.current.loading).toBe(false));

    let returnValue: boolean | undefined;
    await act(async () => {
      returnValue = await result.current.executeNow("j1");
    });

    expect(returnValue).toBe(true);
    expect(api.triggerCronJob).toHaveBeenCalledWith("j1");
    expect(mockMessage.success).toHaveBeenCalledWith(
      "Task triggered successfully",
    );
  });

  // 10. executeNow 失败：message.error，返回 false
  it("executeNow 失败：message.error 被调用，返回 false", async () => {
    (api.triggerCronJob as ReturnType<typeof vi.fn>).mockRejectedValue(
      new Error("trigger failed"),
    );

    const { result } = renderHook(() => useCronJobs());
    await waitFor(() => expect(result.current.loading).toBe(false));

    let returnValue: boolean | undefined;
    await act(async () => {
      returnValue = await result.current.executeNow("j1");
    });

    expect(returnValue).toBe(false);
    expect(mockMessage.error).toHaveBeenCalledWith("Failed to execute");
  });

  it("待审核任务不能通过普通启用或立即执行绕过审核", async () => {
    const pendingJob = {
      id: "imported-1",
      enabled: false,
      name: "Imported",
      meta: {
        portability: {
          requires_review: true,
          safety: "disabled_until_explicit_promotion",
        },
      },
    };
    (api.listCronJobs as ReturnType<typeof vi.fn>).mockResolvedValue([
      pendingJob,
    ]);

    const { result } = renderHook(() => useCronJobs());
    await waitFor(() => expect(result.current.loading).toBe(false));

    await act(async () => {
      expect(
        await result.current.toggleEnabled(
          pendingJob as unknown as Parameters<
            typeof result.current.toggleEnabled
          >[0],
        ),
      ).toBe(false);
      expect(await result.current.executeNow(pendingJob.id)).toBe(false);
    });

    expect(api.replaceCronJob).not.toHaveBeenCalled();
    expect(api.triggerCronJob).not.toHaveBeenCalled();
    expect(mockMessage.error).toHaveBeenCalledWith(
      "cronJobs.importReviewBlocked",
    );
  });

  it("promoteImportedJob 成功后用后端结果替换任务且仍保持禁用", async () => {
    const pendingJob = {
      id: "imported-1",
      enabled: false,
      name: "Imported",
      meta: {
        portability: {
          requires_review: true,
          safety: "disabled_until_explicit_promotion",
        },
      },
    };
    const promotedJob = {
      ...pendingJob,
      meta: {
        portability: {
          requires_review: false,
          safety: "reviewed_disabled",
        },
      },
    };
    (api.listCronJobs as ReturnType<typeof vi.fn>).mockResolvedValue([
      pendingJob,
    ]);
    (api.promoteCronJob as ReturnType<typeof vi.fn>).mockResolvedValue(
      promotedJob,
    );

    const { result } = renderHook(() => useCronJobs());
    await waitFor(() => expect(result.current.loading).toBe(false));

    await act(async () => {
      expect(await result.current.promoteImportedJob(pendingJob.id)).toBe(true);
    });

    expect(api.promoteCronJob).toHaveBeenCalledWith(pendingJob.id);
    expect(result.current.jobs[0]).toEqual(promotedJob);
    expect(result.current.jobs[0].enabled).toBe(false);
    expect(mockMessage.success).toHaveBeenCalledWith(
      "cronJobs.importReviewSuccess",
    );
  });

  it("promoteImportedJob 原样展示后端的本地项目目录错误", async () => {
    const pendingJob = {
      id: "remote-imported",
      enabled: false,
      name: "Remote imported",
      meta: { portability: { requires_review: true } },
    };
    const backendDetail =
      "Remote or unverified imported cron jobs require an explicit local project_dir before promotion";
    (api.listCronJobs as ReturnType<typeof vi.fn>).mockResolvedValue([
      pendingJob,
    ]);
    (api.promoteCronJob as ReturnType<typeof vi.fn>).mockRejectedValue(
      new Error("promotion failed"),
    );
    (parseErrorDetail as ReturnType<typeof vi.fn>).mockReturnValue(
      backendDetail,
    );

    const { result } = renderHook(() => useCronJobs());
    await waitFor(() => expect(result.current.loading).toBe(false));

    await act(async () => {
      expect(await result.current.promoteImportedJob(pendingJob.id)).toBe(
        false,
      );
    });

    expect(mockMessage.error).toHaveBeenCalledWith(backendDetail);
    expect(result.current.jobs[0]).toEqual(pendingJob);
  });

  it("远程待审核任务缺少本地目录映射时不调用 promote API", async () => {
    const pendingJob = {
      id: "remote-without-mapping",
      enabled: false,
      name: "Remote imported",
      request: {
        input: [],
        request_context: { project_dir: "   " },
      },
      meta: {
        portability: {
          requires_review: true,
          source_cwd_remote_or_unverified: true,
        },
      },
    };
    (api.listCronJobs as ReturnType<typeof vi.fn>).mockResolvedValue([
      pendingJob,
    ]);

    const { result } = renderHook(() => useCronJobs());
    await waitFor(() => expect(result.current.loading).toBe(false));

    await act(async () => {
      expect(await result.current.promoteImportedJob(pendingJob.id)).toBe(
        false,
      );
    });

    expect(api.promoteCronJob).not.toHaveBeenCalled();
    expect(mockMessage.error).toHaveBeenCalledWith(
      "cronJobs.importReviewProjectDirRequired",
    );
    expect(result.current.promotingJobIds.size).toBe(0);
  });

  describe("错误信息归一化", () => {
    // 11. detail 字符串包含 "schedule.type is cron but cron is empty"
    it("detail 为字符串时，匹配校验规则并返回对应 i18n key", async () => {
      (parseErrorDetail as ReturnType<typeof vi.fn>).mockReturnValue(
        "schedule.type is cron but cron is empty",
      );
      (api.createCronJob as ReturnType<typeof vi.fn>).mockRejectedValue(
        new Error("validation error"),
      );

      const { result } = renderHook(() => useCronJobs());
      await waitFor(() => expect(result.current.loading).toBe(false));

      await act(async () => {
        await result.current.createJob(
          {} as unknown as Parameters<typeof result.current.createJob>[0],
        );
      });

      expect(mockMessage.error).toHaveBeenCalledWith(
        "cronJobs.validation.cronRequired",
      );
    });

    // 12. detail 是 [{msg: "Value error, cron must have 5 fields"}]
    it("detail 为对象数组时，剥去 'Value error,' 前缀后匹配校验规则", async () => {
      (parseErrorDetail as ReturnType<typeof vi.fn>).mockReturnValue([
        { msg: "Value error, cron must have 5 fields" },
      ]);
      (api.createCronJob as ReturnType<typeof vi.fn>).mockRejectedValue(
        new Error("validation error"),
      );

      const { result } = renderHook(() => useCronJobs());
      await waitFor(() => expect(result.current.loading).toBe(false));

      await act(async () => {
        await result.current.createJob(
          {} as unknown as Parameters<typeof result.current.createJob>[0],
        );
      });

      expect(mockMessage.error).toHaveBeenCalledWith(
        "cronJobs.validation.invalidCronExpression",
      );
    });
  });
});
