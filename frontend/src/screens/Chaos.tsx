import { useState } from "react";
import {
  Activity,
  BadgeCheck,
  Ban,
  CheckCircle2,
  CircleDashed,
  Loader2,
  Radar,
  Scale,
  Stethoscope,
  Zap,
} from "lucide-react";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { api, rupee } from "@/lib/api";
import { cn } from "@/lib/utils";

type StageState = "idle" | "running" | "done" | "skipped" | "error";

interface Stage {
  id: string;
  label: string;
  hint: string;
  icon: React.ElementType;
  state: StageState;
  summary?: string;
  detail?: Record<string, unknown>;
}

const BLANK: Stage[] = [
  { id: "detect", label: "Detect", hint: "MAD anomaly, N ≥ 30 gate", icon: Radar, state: "idle" },
  { id: "diagnose", label: "Diagnose", hint: "Tier 1 rules → Tier 2 LLM → Tier 3 quarantine", icon: Stethoscope, state: "idle" },
  { id: "policy", label: "Policy", hint: "Risk scorecard → action → idempotent dispatch", icon: Scale, state: "idle" },
  { id: "reconcile", label: "Reconcile", hint: "Settlement hold, collision auto-refund", icon: Activity, state: "idle" },
];

const PRESETS = [
  "BANK_NOT_AVAILABLE",
  "MANDATE_REVOKED_BY_PAYER",
  "AUTH_TIMEOUT",
  "XZ-991",
  "<script>alert(1)</script>'; DROP TABLE mandates;--",
];

function StageIcon({ state, icon: Icon }: { state: StageState; icon: React.ElementType }) {
  if (state === "running") return <Loader2 className="h-4 w-4 animate-spin text-primary" />;
  if (state === "done") return <CheckCircle2 className="h-4 w-4 text-emerald-600" />;
  if (state === "skipped") return <Ban className="h-4 w-4 text-muted-foreground" />;
  if (state === "error") return <Ban className="h-4 w-4 text-destructive" />;
  return <Icon className="h-4 w-4 text-muted-foreground" />;
}

export default function Chaos({ onDone }: { onDone: () => void }) {
  const [reason, setReason] = useState("BANK_NOT_AVAILABLE");
  const [amount, setAmount] = useState("4800");
  const [busy, setBusy] = useState(false);
  const [stages, setStages] = useState<Stage[]>(BLANK);
  const [error, setError] = useState<string | null>(null);

  const set = (id: string, patch: Partial<Stage>) =>
    setStages((s) => s.map((st) => (st.id === id ? { ...st, ...patch } : st)));

  async function inject() {
    setBusy(true);
    setError(null);
    setStages(BLANK.map((s) => ({ ...s })));

    const stamp = Date.now();
    const mandateId = `MND-CHAOS-${String(stamp).slice(-6)}`;
    const eventId = `EVT-CHAOS-${stamp}`;
    const cycle = new Date().toISOString().slice(0, 7);
    const amt = Math.max(1, parseInt(amount || "0", 10) || 1);

    try {
      // 1 — Detect (POST /api/events/ingest)
      set("detect", { state: "running" });
      const ing = await api.ingest({
        event_id: eventId,
        mandate_id: mandateId,
        customer_id: `CUST-CHAOS-${String(stamp).slice(-6)}`,
        bank: "ICICI",
        mandate_type: "UPI_AUTOPAY",
        event_ts: new Date().toISOString(),
        billing_cycle: cycle,
        amount: amt,
        mandate_reliability: 0.55,
        raw_error_code: reason,
        arm: "treatment",
      });
      set("detect", {
        state: "done",
        summary: `${ing.detection.status} · N=${ing.detection.sample_size}${
          ing.detection.mad != null ? ` · MAD ${ing.detection.mad.toFixed(3)}` : ""
        }`,
        detail: ing.detection as unknown as Record<string, unknown>,
      });

      // 2+3 — Diagnose and Policy both come from POST /api/events/recover
      set("diagnose", { state: "running" });
      const rec = await api.recover({
        event_id: eventId,
        mandate_id: mandateId,
        billing_cycle: cycle,
        raw_error_code: reason,
        amount: amt,
        mandate_reliability: 0.55,
        days_to_next_cycle: 1,
      });
      set("diagnose", {
        state: "done",
        summary: `Tier ${rec.diagnosis.tier} · ${rec.diagnosis.cause ?? "unmapped"}${
          rec.diagnosis.confidence != null ? ` · conf ${rec.diagnosis.confidence.toFixed(2)}` : ""
        }`,
        detail: {
          sanitized_input: rec.diagnosis.sanitized_input,
          status: rec.diagnosis.status,
          llm_model: rec.diagnosis.llm_model,
          reason: rec.diagnosis.reason,
        },
      });

      set("policy", {
        state: "done",
        summary: `${rec.decision.action} · risk ${rec.decision.risk.score.toFixed(3)}${
          rec.fired ? "" : " · not fired (idempotent guard)"
        }`,
        detail: {
          action: rec.decision.action,
          risk_score: rec.decision.risk.score,
          expected_loss: rec.decision.risk.expected_loss,
          alt_rail_cost: rec.decision.risk.alt_rail_cost,
          cost_benefit_passed: rec.decision.risk.cost_benefit_passed,
          reason: rec.decision.reason,
        },
      });

      // 4 — Reconcile: only actions that actually collect money open a hold.
      const collects = ["RETRY", "NUDGE_BALANCE", "ALT_RAIL"].includes(rec.decision.action);
      if (!collects || !rec.fired) {
        set("reconcile", {
          state: "skipped",
          summary: rec.quarantined
            ? "no money action on a quarantined event"
            : `${rec.decision.action} collects nothing — no hold opened`,
        });
      } else {
        set("reconcile", { state: "running" });
        const path = rec.decision.action === "ALT_RAIL" ? "alt_rail" : "mandate";
        const settle = await api.settle({
          mandate_id: mandateId,
          billing_cycle: cycle,
          path,
          amount: amt,
        });
        set("reconcile", {
          state: "done",
          summary: `${path} → ${settle.status}${
            settle.collided_with ? ` (collided with ${settle.collided_with})` : ""
          }`,
          detail: settle as unknown as Record<string, unknown>,
        });
      }
      onDone();
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setError(msg);
      setStages((s) => s.map((st) => (st.state === "running" ? { ...st, state: "error" } : st)));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="grid gap-4 lg:grid-cols-[380px_1fr]">
      <Card className="h-fit">
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-base">
            <Zap className="h-4 w-4 text-muted-foreground" />
            Inject a decline
          </CardTitle>
          <CardDescription>
            Pushes one synthetic event through the real pipeline. Hostile strings are sanitized
            before they reach the LLM.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div>
            <label className="mb-1.5 block text-xs font-medium text-muted-foreground">
              Raw decline reason
            </label>
            <Input
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              placeholder="e.g. BANK_NOT_AVAILABLE"
              className="font-mono text-xs"
            />
          </div>
          <div>
            <label className="mb-1.5 block text-xs font-medium text-muted-foreground">
              Amount (₹)
            </label>
            <Input
              value={amount}
              onChange={(e) => setAmount(e.target.value.replace(/[^0-9]/g, ""))}
              inputMode="numeric"
              className="tabular-nums"
            />
            <p className="mt-1.5 text-xs text-muted-foreground">
              Alt-rail needs expected loss ({rupee(Math.round((parseInt(amount, 10) || 0) * 0.4))})
              to clear its ₹1,600 cost.
            </p>
          </div>
          <div>
            <p className="mb-2 text-xs font-medium text-muted-foreground">Presets</p>
            <div className="flex flex-wrap gap-1.5">
              {PRESETS.map((p) => (
                <Button
                  key={p}
                  size="sm"
                  variant="outline"
                  className="max-w-full truncate font-mono text-[11px]"
                  onClick={() => setReason(p)}
                  title={p}
                >
                  {p.length > 24 ? p.slice(0, 24) + "…" : p}
                </Button>
              ))}
            </div>
          </div>
          <Button className="w-full" onClick={inject} disabled={busy || !reason.trim()}>
            {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <Zap className="h-4 w-4" />}
            {busy ? "Injecting…" : "Inject event"}
          </Button>
          {error && (
            <p className="rounded-md border border-destructive/20 bg-destructive/5 px-3 py-2 text-xs text-destructive">
              {error}
            </p>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-base">
            <Activity className="h-4 w-4 text-muted-foreground" />
            Live pipeline trace
          </CardTitle>
          <CardDescription>
            Each stage lights up as its endpoint returns — Detect, Diagnose and Policy, then
            Reconcile.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <ol className="relative space-y-3">
            {stages.map((s, i) => (
              <li key={s.id} className="relative flex gap-3">
                {i < stages.length - 1 && (
                  <span
                    className={cn(
                      "absolute left-[15px] top-9 h-[calc(100%-12px)] w-px",
                      s.state === "done" ? "bg-emerald-300" : "bg-border"
                    )}
                  />
                )}
                <div
                  className={cn(
                    "z-10 flex h-8 w-8 shrink-0 items-center justify-center rounded-full border bg-background",
                    s.state === "done" && "border-emerald-300 bg-emerald-50",
                    s.state === "running" && "border-primary",
                    s.state === "error" && "border-destructive bg-destructive/5"
                  )}
                >
                  <StageIcon state={s.state} icon={s.icon} />
                </div>
                <div
                  className={cn(
                    "min-w-0 flex-1 rounded-md border px-4 py-3 transition-colors",
                    s.state === "idle" && "opacity-55",
                    s.state === "done" && "border-emerald-200 bg-emerald-50/40",
                    s.state === "error" && "border-destructive/30 bg-destructive/5"
                  )}
                >
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-medium">{s.label}</span>
                    {s.state === "skipped" && <Badge variant="secondary">skipped</Badge>}
                    {s.state === "done" && (
                      <Badge variant="success">
                        <BadgeCheck className="h-3 w-3" /> ok
                      </Badge>
                    )}
                    {s.state === "idle" && <CircleDashed className="h-3.5 w-3.5 text-muted-foreground" />}
                  </div>
                  <p className="mt-0.5 text-xs text-muted-foreground">{s.hint}</p>
                  {s.summary && (
                    <p className="mt-2 break-words font-mono text-xs text-foreground">{s.summary}</p>
                  )}
                  {s.detail && (
                    <pre className="mt-2 max-h-44 overflow-auto rounded bg-muted px-3 py-2 font-mono text-[11px] leading-relaxed text-muted-foreground">
{JSON.stringify(s.detail, null, 2)}
                    </pre>
                  )}
                </div>
              </li>
            ))}
          </ol>
        </CardContent>
      </Card>
    </div>
  );
}
