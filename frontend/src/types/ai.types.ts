import type { Device } from "./device.types";

export interface ProposedDevice {
  role: string;
  template_name: string;
  topology_type: "PHYSICAL" | "CLOUD";
  device: Device | null;
  config?: Record<string, unknown> | null;
}

export interface ProposedEdge {
  source_role: string;
  target_role: string;
  layer: "L1" | "L2" | "L3";
}

export interface AIFileSummary {
  filename: string;
  chars: number;
  truncated: boolean;
}

export interface AIGenerateRequest {
  prompt: string;
  files?: File[];
}

export interface AIStatusResponse {
  enabled: boolean;
  // Conditional-UI signal for the recipe drafting panel (issue #28); the
  // panel is usable only when this AND enabled are both true.
  recipe_authoring?: boolean;
  // True when settings look configured but provider construction still
  // failed (issue #606), e.g. a bad ai_ca_cert path or an SDK/http-client
  // mismatch. enabled is already false in this case, so existing enabled
  // gating needs no change; these fields are additive, for an
  // operator-facing hint only.
  degraded?: boolean;
  // Exception class name only, never present with message text (may carry a
  // base URL or key material). Null when not degraded.
  reason?: string | null;
}

export interface RecipeValidationSection {
  passed: boolean;
  errors: string[];
}

export interface RecipeDryRunMethod {
  action: string;
  passed: boolean;
  success: boolean;
  output: Record<string, unknown> | null;
  error: string | null;
  duration_ms: number | null;
  transcript: Record<string, unknown>[];
}

export interface RecipeValidationReport {
  valid: boolean;
  structural: RecipeValidationSection;
  policy: RecipeValidationSection;
  schema: { present: boolean; schema: Record<string, unknown> | null; error: string | null };
  dry_run: { passed: boolean; methods: RecipeDryRunMethod[]; error: string | null };
}

export interface RecipeDraftResponse {
  draft_id: string;
  valid: boolean;
  attempts: number;
  model: string | null;
  prompt: string;
  hypervisor_type: string | null;
  explanation: string | null;
  driver_py: string;
  driver_metadata: Record<string, unknown>;
  validation: RecipeValidationReport | null;
  package_b64: string;
  created_at: string;
  updated_at: string;
}

export interface AIGenerateResponse {
  purpose: string;
  devices: ProposedDevice[];
  edges: ProposedEdge[];
  notes: string;
  file_summaries: AIFileSummary[];
}

export interface AICommitDevice {
  role: string;
  device_id: string;
  position?: { x: number; y: number };
  config?: Record<string, unknown> | null;
  connection_type?: string | null;
}

export interface AICommitEdge {
  source_role: string;
  target_role: string;
  layer: "L1" | "L2" | "L3";
}

export interface AICommitRequest {
  topology_name: string;
  purpose?: string | null;
  start_time: string;
  end_time: string;
  devices: AICommitDevice[];
  edges: AICommitEdge[];
  apply_configs?: boolean;
}

export interface DeviceConfigResult {
  role: string;
  device_id: string;
  status: "skipped" | "success" | "failed";
  error?: string | null;
  run_id?: string | null;
}

export interface AICommitResponse {
  topology_id: string;
  reservation_id: string;
  config_results: DeviceConfigResult[];
}

export interface SuggestIdentityRequest {
  name: string;
  description?: string | null;
  sections?: Array<Record<string, unknown>> | null;
}

export interface IdentitySuggestion {
  vendor: string;
  model: string;
  part_number: string | null;
  confidence: "low" | "medium" | "high";
  reasoning: string;
}

// Branch 3: one rendered bubble in the chat thread. id is local-only (uuid
// generated at append time) for React keys; the backend's per-message uuids
// are not exposed to the client. The pendingApply piggybacks on the
// assistant message that scheduled it so the confirmation modal can mount
// from per-turn context.
export type ChatRole = "user" | "assistant";

export interface ChatMessage {
  id: string;
  role: ChatRole;
  text: string;
  toolCalls?: ToolCall[];
  toolIterations?: number;
  pendingApply?: PendingApply | null;
  errorText?: string;
}

export interface PendingApply {
  job_id: string;
  version_id: string;
  device_id: string;
  dry_run: boolean;
  scheduled_for: string;
}

export interface ToolCall {
  name: string;
  arguments_summary: string;
  duration_ms: number;
  error: string | null;
}

export interface AssistantResponse {
  answer: string;
  model: string;
  input_tokens: number;
  output_tokens: number;
  stop_reason: string;
  tool_calls: ToolCall[];
  tool_iterations: number;
  conversation_id: string | null;
  pending_apply?: PendingApply | null;
}

export interface CommandLogEntry {
  id: string;
  run_id: string;
  seq: number;
  command: string;
  response: string | null;
  duration_ms: number | null;
  exit_status: string;
  created_at: string;
}
