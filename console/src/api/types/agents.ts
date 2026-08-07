// Multi-agent management types

import type { ModelSlotConfig } from "./provider";
import type { HarnessCapabilities } from "../modules/harness";

export type AgentStartupStatus =
  | "disabled"
  | "pending"
  | "starting"
  | "running"
  | "failed";

export interface AgentSummary {
  id: string;
  name: string;
  description: string;
  workspace_dir: string;
  enabled: boolean;
  pinned?: boolean;
  startup_status?: AgentStartupStatus;
  backend: AgentBackend;
  backend_capabilities?: Partial<HarnessCapabilities>;
  backend_model?: string | null;
  backend_reasoning_effort?: string | null;
  active_model?: ModelSlotConfig | null;
}

export type AgentBackend = string;

export interface AgentListResponse {
  agents: AgentSummary[];
}

export interface ReorderAgentsResponse {
  success: boolean;
  agent_ids: string[];
}

export interface AgentMailCredential {
  name: string;
  domain: string;
  // "" for whitelisted domains; enterprise provider id
  // (tencent_exmail / aliyun_qiye / netease_qiye) for custom domains.
  provider?: string;
  auth_code: string;
  password: string;
  phone_number: string;
}

export interface AgentMailPushRule {
  // "subject" is a legacy alias of "content" (kept for old configs)
  field: "from" | "subject" | "content" | "keyword"; // default "from"
  contains: string;
  action: "mark_read" | "move" | "notify" | "wake_agent"; // default "notify"
  param: string;
}

export interface AgentMailPushConfig {
  mode: "off" | "rules_only" | "rules_then_agent" | "agent_all"; // default "off"
  rules: AgentMailPushRule[];
  poll_interval_seconds?: number; // default 120
  access_control_enabled?: boolean; // default true
}

export interface AgentMailConfig {
  is_new_account: boolean;
  credential: AgentMailCredential;
  push?: AgentMailPushConfig | null;
}

export interface AgentProfileConfig {
  id: string;
  name: string;
  description?: string;
  workspace_dir?: string;
  backend?: AgentBackend;
  backend_settings?: {
    binary?: string;
    model?: string;
    reasoning_effort?: string;
    [key: string]: unknown;
  };
  approval_level?: string;
  active_model?: ModelSlotConfig | null;
  channels?: unknown;
  mcp?: unknown;
  heartbeat?: unknown;
  running?: unknown;
  llm_routing?: unknown;
  system_prompt_files?: string[];
  tools?: unknown;
  security?: unknown;
  mail?: AgentMailConfig | null;
}

export interface CreateAgentRequest {
  id?: string;
  name: string;
  description?: string;
  workspace_dir?: string;
  language?: string;
  skill_names?: string[];
  active_model?: ModelSlotConfig | null;
  mail?: AgentMailConfig | null;
  backend?: AgentBackend;
  backend_settings?: {
    binary?: string;
    model?: string;
    reasoning_effort?: string;
    [key: string]: unknown;
  };
}

export interface CopyAgentRequest {
  name?: string;
  copy_agent_json?: true;
  copy_md_files?: boolean;
  copy_skills?: boolean;
  copy_jobs?: boolean;
}

export interface AgentProfileRef {
  id: string;
  workspace_dir: string;
  enabled?: boolean;
  pinned?: boolean;
}
