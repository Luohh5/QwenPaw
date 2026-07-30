// Multi-agent management types

import type { ModelSlotConfig } from "./provider";

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
  active_model?: ModelSlotConfig | null;
}

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
