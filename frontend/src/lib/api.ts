/**
 * Typed client for the existing FastAPI surface. No endpoints were added for
 * the UI; these mirror what the backend already serves.
 */

export interface TierMTTR {
  tier: number;
  resolved_count: number;
  in_flight_count: number;
  mttr_seconds: number | null;
  note: string;
}

export interface Recovered {
  treatment_recovered: number;
  control_recovered: number;
  delta: number;
  treatment_mandates: number;
  control_mandates: number;
  per_mandate_delta: number | null;
  computable: boolean;
  note: string;
}

export interface RecoveryRate {
  sla_hours: number;
  actions_total: number;
  actions_resolved: number;
  actions_in_flight: number;
  resolved_within_sla: number;
  rate: number | null;
  note: string;
}

export interface Segment {
  segment: string;
  checks: number;
  flags: number;
  avg_mad: number | null;
  threshold: number | null;
}

export interface OpsCase {
  id: number;
  mandate_id: string;
  billing_cycle: string;
  reason: string;
  source_layer: string;
  status: string;
  created_at: string;
  age_seconds: number;
  sla_remaining_seconds: number;
}

export interface SafeHoldCase {
  id: number;
  mandate_id: string;
  billing_cycle: string;
  action_type: string;
  status: string;
  cause: string | null;
  diagnosis_tier: number | null;
  risk_score: number | null;
  reason: string;
  created_at: string;
  age_seconds: number;
  sla_remaining_seconds: number;
}

export interface QuarantineCase {
  id: number;
  event_id: string;
  raw_input: string;
  tier_attempted: number;
  reason: string;
  status: string;
  created_at: string;
}

export interface LedgerPath {
  status: string;
  amount: number;
  opened_at: string;
  resolved_at: string | null;
}

export interface Race {
  mandate_id: string;
  billing_cycle: string;
  paths: Record<string, LedgerPath>;
  winner: string | null;
  refunded: string | null;
  collision: boolean;
  gap_seconds: number | null;
}

export interface Summary {
  mttr_by_tier: TierMTTR[];
  recovered: Recovered;
  recovery_rate: RecoveryRate;
  escalations_by_type: { source_layer: string; reason: string; status: string; count: number }[];
  quarantine: { total: number; pending_ops_review: number; samples: QuarantineCase[] };
  detection: { window_hours: number; flags_in_window: number; segments: Segment[] };
  actions_by_type: { key: string; count: number }[];
  ledger_by_status: { key: string; count: number }[];
  ops_queue: OpsCase[];
  safe_hold_cases: SafeHoldCase[];
  quarantine_cases: QuarantineCase[];
  reconciliation_races: Race[];
  generated_at: string;
  disclaimer: string;
}

export interface DiagnosisTrace {
  event_id: string;
  tier: number;
  cause: string | null;
  confidence: number | null;
  status: string;
  sanitized_input: string | null;
  llm_model: string | null;
  at: string;
}

export interface AuditTrace {
  mandate_id: string;
  billing_cycle: string;
  found: boolean;
  events: {
    event_id: string;
    event_ts: string;
    segment: string;
    amount: number;
    raw_error_code: string;
    arm: string | null;
  }[];
  diagnoses: DiagnosisTrace[];
  action: {
    action_type: string;
    status: string;
    params: Record<string, unknown>;
    created_at: string;
    resolved_at: string | null;
  } | null;
  reconciliation: {
    path: string;
    status: string;
    amount: number;
    opened_at: string;
    resolved_at: string | null;
  }[];
  escalations: { reason: string; source_layer: string; status: string; at: string }[];
}

export interface DetectionResult {
  status: string;
  is_anomaly: boolean;
  sample_size: number;
  median: number | null;
  mad: number | null;
  threshold: number | null;
  deviation: number | null;
  observed_value: number;
  reason?: string;
}

export interface IngestResponse {
  event_id: string;
  duplicate: boolean;
  segment: string;
  detection_id: number;
  detection: DetectionResult;
}

export interface RecoverResponse {
  event_id: string;
  mandate_id: string;
  billing_cycle: string;
  diagnosis: {
    cause: string | null;
    tier: number;
    status: string;
    confidence: number | null;
    raw_input: string;
    sanitized_input: string | null;
    llm_model: string | null;
    reason: string;
  };
  decision: {
    action: string;
    cause: string | null;
    diagnosis_tier: number;
    max_retries: number;
    reason: string;
    risk: {
      score: number;
      urgency: number;
      unreliability: number;
      amount_tier: number;
      cost_benefit: number;
      expected_loss: number;
      alt_rail_cost: number;
      cost_benefit_passed: boolean;
    };
  };
  fired: boolean;
  action_id: number | null;
  quarantined: boolean;
  comms: Record<string, unknown> | null;
  reason: string;
}

export interface SettlementResponse {
  path: string;
  status: string;
  collided_with: string | null;
  refunded_amount: number | null;
  within_hold_window: boolean;
  seconds_since_open: number | null;
  reason: string;
}

export interface PromoteResponse {
  ok: boolean;
  rules_count: number;
  added: { raw_input: string; target_cause: string };
  replaced: string | null;
  note: string;
}

export interface RulesResponse {
  rules: Record<string, string>;
  rules_count: number;
  ontology: string[];
}

async function req<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers || {}) },
  });
  if (!res.ok) {
    const body = await res.text().catch(() => "");
    let detail = body;
    try {
      const parsed = JSON.parse(body);
      if (typeof parsed?.detail === "string") detail = parsed.detail;
    } catch {
      /* not JSON — fall back to the raw body */
    }
    throw new Error(detail ? detail.slice(0, 400) : `${res.status} ${res.statusText}`);
  }
  return res.json() as Promise<T>;
}

export type GeminiHealth = {
  state: "CLOSED" | "OPEN" | "HALF_OPEN";
  failure_count_last_60s: number;
  last_failure_at: string | null;
  next_test_at: string | null;
  retries_attempted: number;
  retries_succeeded: number;
};

export const api = {
  summary: () => req<Summary>("/api/dashboard/summary"),

  geminiHealth: () => req<GeminiHealth>("/api/health/gemini"),

  audit: (mandateId: string, billingCycle: string) =>
    req<AuditTrace>(
      `/api/audit/decision/${encodeURIComponent(mandateId)}/${encodeURIComponent(billingCycle)}`
    ),

  ingest: (body: Record<string, unknown>) =>
    req<IngestResponse>("/api/events/ingest", { method: "POST", body: JSON.stringify(body) }),

  recover: (body: Record<string, unknown>) =>
    req<RecoverResponse>("/api/events/recover", { method: "POST", body: JSON.stringify(body) }),

  promote: (raw_input: string, target_cause: string) =>
    req<PromoteResponse>("/api/ontology/promote", {
      method: "POST",
      body: JSON.stringify({ raw_input, target_cause }),
    }),

  rules: () => req<RulesResponse>("/api/ontology/rules"),

  settle: (body: Record<string, unknown>) =>
    req<SettlementResponse>("/api/webhooks/settlement", {
      method: "POST",
      body: JSON.stringify(body),
    }),
};

// ---------------------------------------------------------------------------
// formatting
// ---------------------------------------------------------------------------
export const rupee = (n: number | null | undefined) =>
  n == null ? "—" : "₹" + Math.round(n).toLocaleString("en-IN");

export const int = (n: number | null | undefined) =>
  n == null ? "—" : n.toLocaleString("en-IN");

export function duration(seconds: number | null | undefined): string {
  if (seconds == null) return "—";
  const s = Math.abs(seconds);
  if (s < 90) return `${s.toFixed(0)}s`;
  if (s < 5400) return `${(s / 60).toFixed(0)}m`;
  if (s < 172800) return `${(s / 3600).toFixed(1)}h`;
  return `${(s / 86400).toFixed(1)}d`;
}
