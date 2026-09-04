import { useEffect, useMemo, useState } from "react";
import {
  ArrowDown,
  ArrowRight,
  ArrowUp,
  ArrowUpDown,
  CheckCircle2,
  Clock,
  Loader2,
  ShieldAlert,
  Sparkles,
} from "lucide-react";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  api,
  duration,
  type PromoteResponse,
  type QuarantineCase,
  type SafeHoldCase,
  type Summary,
} from "@/lib/api";
import { cn } from "@/lib/utils";

type Row = {
  key: string;
  kind: "SAFE_HOLD" | "MANUAL_REVIEW" | "QUARANTINE" | "ESCALATION";
  ref: string;
  detail: string;
  cause: string | null;
  tier: number | null;
  risk: number | null;
  age: number;
  sla: number | null;
  raw?: string;
  reason: string;
};

type SortKey = "kind" | "ref" | "age" | "sla" | "risk";

function slaBadge(sla: number | null) {
  if (sla == null) return <span className="text-muted-foreground">—</span>;
  if (sla < 0)
    return (
      <Badge variant="destructive">
        <Clock className="h-3 w-3" /> {duration(sla)} over
      </Badge>
    );
  if (sla < 4 * 3600)
    return (
      <Badge variant="warning">
        <Clock className="h-3 w-3" /> {duration(sla)} left
      </Badge>
    );
  return <span className="tabular-nums text-muted-foreground">{duration(sla)} left</span>;
}

const kindVariant = {
  SAFE_HOLD: "info",
  MANUAL_REVIEW: "warning",
  QUARANTINE: "destructive",
  ESCALATION: "warning",
} as const;

export default function OpsQueue({ data, onPromoted }: { data: Summary; onPromoted: () => void }) {
  const [sort, setSort] = useState<{ key: SortKey; dir: 1 | -1 }>({ key: "age", dir: -1 });
  const [promote, setPromote] = useState<Row | null>(null);
  const [chosen, setChosen] = useState<string>("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<PromoteResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  // The ontology is served by the backend, so the UI can never drift from the
  // set the validator actually enforces.
  const [ontology, setOntology] = useState<string[]>([]);
  const [ruleCount, setRuleCount] = useState<number | null>(null);
  const [promoted, setPromoted] = useState<Record<string, string>>({});

  useEffect(() => {
    api
      .rules()
      .then((r) => {
        // 'unknown' is in the ontology but is rejected for promotion: a Tier 1
        // rule resolves with confidence 1.0, so it would bypass quarantine.
        setOntology(r.ontology.filter((c) => c !== "unknown"));
        setRuleCount(r.rules_count);
      })
      .catch(() => setOntology([]));
  }, []);

  const rows: Row[] = useMemo(() => {
    const hold = data.safe_hold_cases.map(
      (c: SafeHoldCase): Row => ({
        key: `a${c.id}`,
        kind: c.action_type === "SAFE_HOLD" ? "SAFE_HOLD" : "MANUAL_REVIEW",
        ref: `${c.mandate_id} · ${c.billing_cycle}`,
        detail: c.cause ?? "undiagnosed",
        cause: c.cause,
        tier: c.diagnosis_tier,
        risk: c.risk_score,
        age: c.age_seconds,
        sla: c.sla_remaining_seconds,
        reason: c.reason,
      })
    );
    const quar = data.quarantine_cases.map(
      (q: QuarantineCase): Row => ({
        key: `q${q.id}`,
        kind: "QUARANTINE",
        ref: q.event_id,
        detail: q.raw_input,
        cause: null,
        tier: q.tier_attempted,
        risk: null,
        age: (Date.now() - new Date(q.created_at).getTime()) / 1000,
        sla: null,
        raw: q.raw_input,
        reason: q.reason,
      })
    );
    const esc = data.ops_queue.map((o): Row => ({
      key: `o${o.id}`,
      kind: "ESCALATION",
      ref: `${o.mandate_id} · ${o.billing_cycle}`,
      detail: o.source_layer,
      cause: null,
      tier: null,
      risk: null,
      age: o.age_seconds,
      sla: o.sla_remaining_seconds,
      reason: o.reason,
    }));
    return [...esc, ...hold, ...quar];
  }, [data]);

  const sorted = useMemo(() => {
    const v = (r: Row) =>
      sort.key === "age" ? r.age
      : sort.key === "sla" ? (r.sla ?? Number.MAX_SAFE_INTEGER)
      : sort.key === "risk" ? (r.risk ?? -1)
      : sort.key === "kind" ? r.kind
      : r.ref;
    return [...rows].sort((a, b) => {
      const x = v(a), y = v(b);
      return (x > y ? 1 : x < y ? -1 : 0) * sort.dir;
    });
  }, [rows, sort]);

  function openPromote(r: Row) {
    setPromote(r);
    setChosen(ontology[0] ?? "");
    setResult(null);
    setError(null);
  }

  async function submit() {
    if (!promote?.raw || !chosen) return;
    setBusy(true);
    setError(null);
    try {
      const res = await api.promote(promote.raw, chosen);
      setResult(res);
      setRuleCount(res.rules_count);
      setPromoted((p) => ({ ...p, [res.added.raw_input]: res.added.target_cause }));
      onPromoted();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  const Th = ({ k, children, className }: { k: SortKey; children: React.ReactNode; className?: string }) => (
    <TableHead className={className}>
      <button
        className="inline-flex items-center gap-1 uppercase tracking-wide hover:text-foreground"
        onClick={() => setSort((s) => ({ key: k, dir: s.key === k ? ((-s.dir) as 1 | -1) : 1 }))}
      >
        {children}
        {sort.key !== k ? (
          <ArrowUpDown className="h-3 w-3 opacity-40" />
        ) : sort.dir > 0 ? (
          <ArrowUp className="h-3 w-3" />
        ) : (
          <ArrowDown className="h-3 w-3" />
        )}
      </button>
    </TableHead>
  );

  const breached = rows.filter((r) => r.sla != null && r.sla < 0).length;

  return (
    <div className="space-y-6">
      <div className="grid gap-4 sm:grid-cols-4">
        <Card>
          <CardContent className="p-5">
            <p className="text-sm text-muted-foreground">Cases awaiting review</p>
            <p className="mt-2 text-3xl font-semibold tabular-nums">{rows.length}</p>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="p-5">
            <p className="text-sm text-muted-foreground">SLA breached</p>
            <p className={cn("mt-2 text-3xl font-semibold tabular-nums", breached > 0 && "text-destructive")}>
              {breached}
            </p>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="p-5">
            <p className="text-sm text-muted-foreground">Quarantined strings</p>
            <p className="mt-2 text-3xl font-semibold tabular-nums">{data.quarantine_cases.length}</p>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="p-5">
            <p className="text-sm text-muted-foreground">Tier 1 rules</p>
            <p className="mt-2 text-3xl font-semibold tabular-nums">{ruleCount ?? "—"}</p>
            {Object.keys(promoted).length > 0 && (
              <p className="mt-1.5 text-xs text-emerald-600">
                +{Object.keys(promoted).length} promoted this session
              </p>
            )}
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-base">
            <ShieldAlert className="h-4 w-4 text-muted-foreground" />
            Ops queue
          </CardTitle>
          <CardDescription>
            Cases the pipeline deliberately did not act on automatically. Approving a quarantined
            string writes a Tier 1 rule, so the next occurrence resolves instantly with no LLM call.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow>
                <Th k="kind">Type</Th>
                <Th k="ref">Reference</Th>
                <TableHead>Detail</TableHead>
                <Th k="risk" className="text-right">Risk</Th>
                <Th k="age" className="text-right">Age</Th>
                <Th k="sla" className="text-right">SLA</Th>
                <TableHead className="text-right">Action</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {sorted.map((r) => {
                const done = r.raw ? promoted[r.raw.trim().toUpperCase()] : undefined;
                return (
                  <TableRow key={r.key} className={cn(r.sla != null && r.sla < 0 && "bg-destructive/5")}>
                    <TableCell>
                      <Badge variant={kindVariant[r.kind]}>{r.kind.replace("_", " ")}</Badge>
                    </TableCell>
                    <TableCell className="font-mono text-xs">{r.ref}</TableCell>
                    <TableCell
                      className="max-w-[280px] truncate font-mono text-xs text-muted-foreground"
                      title={r.reason}
                    >
                      {r.detail}
                    </TableCell>
                    <TableCell className="text-right tabular-nums">
                      {r.risk == null ? "—" : r.risk.toFixed(3)}
                    </TableCell>
                    <TableCell className="text-right tabular-nums text-muted-foreground">
                      {duration(r.age)}
                    </TableCell>
                    <TableCell className="text-right">{slaBadge(r.sla)}</TableCell>
                    <TableCell className="text-right">
                      {r.kind !== "QUARANTINE" ? (
                        <span className="text-xs text-muted-foreground">—</span>
                      ) : done ? (
                        <Badge variant="success">
                          <CheckCircle2 className="h-3 w-3" /> → {done}
                        </Badge>
                      ) : (
                        <Button size="sm" variant="outline" onClick={() => openPromote(r)}>
                          <CheckCircle2 className="h-3.5 w-3.5" />
                          Approve
                        </Button>
                      )}
                    </TableCell>
                  </TableRow>
                );
              })}
              {!sorted.length && (
                <TableRow>
                  <TableCell colSpan={7} className="py-10 text-center text-muted-foreground">
                    Queue is clear — nothing awaiting review.
                  </TableCell>
                </TableRow>
              )}
            </TableBody>
          </Table>
        </CardContent>
      </Card>

      <Dialog open={!!promote} onOpenChange={(o) => !o && setPromote(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              {result ? "Rule promoted" : "Promote to Tier 1 rule"}
            </DialogTitle>
            <DialogDescription>
              {result
                ? "This string now resolves instantly at Tier 1, with no LLM call."
                : "Maps this string into the fixed ontology so future occurrences resolve instantly."}
            </DialogDescription>
          </DialogHeader>

          {result ? (
            <div className="space-y-4">
              <div className="flex items-center gap-3 rounded-md border border-emerald-200 bg-emerald-50 px-4 py-3">
                <CheckCircle2 className="h-5 w-5 shrink-0 text-emerald-600" />
                <div className="min-w-0 text-sm">
                  <p className="font-mono font-medium text-emerald-900">
                    {result.added.raw_input}
                    <ArrowRight className="mx-1.5 inline h-3.5 w-3.5" />
                    {result.added.target_cause}
                  </p>
                  <p className="mt-0.5 text-xs text-emerald-800">
                    {result.rules_count} Tier 1 rules now active
                    {result.replaced && ` · replaced "${result.replaced}"`}
                  </p>
                </div>
              </div>
              <div className="rounded-md border bg-muted/50 px-4 py-3 text-xs text-muted-foreground">
                <p className="flex items-center gap-1.5 font-medium text-foreground">
                  <Sparkles className="h-3.5 w-3.5" /> Close the loop
                </p>
                <p className="mt-1">
                  Open <strong>Chaos Trigger</strong> and inject{" "}
                  <code className="rounded bg-background px-1 py-0.5 font-mono">
                    {result.added.raw_input}
                  </code>
                  . It previously quarantined at Tier 3; it will now resolve at Tier 1 without
                  touching the LLM.
                </p>
              </div>
              <p className="text-xs text-muted-foreground">{result.note}</p>
            </div>
          ) : (
            <div className="space-y-4">
              <div>
                <p className="text-xs font-medium text-muted-foreground">Raw input</p>
                <p className="mt-1 break-all rounded-md bg-muted px-3 py-2 font-mono text-sm">
                  {promote?.raw}
                </p>
              </div>
              <div>
                <p className="mb-2 text-xs font-medium text-muted-foreground">
                  Map to cause {ontology.length === 0 && "(loading ontology…)"}
                </p>
                <div className="flex flex-wrap gap-2">
                  {ontology.map((c) => (
                    <Button
                      key={c}
                      size="sm"
                      variant={chosen === c ? "default" : "outline"}
                      onClick={() => setChosen(c)}
                      disabled={busy}
                    >
                      {c}
                    </Button>
                  ))}
                </div>
              </div>
              <div>
                <p className="text-xs font-medium text-muted-foreground">Resulting rule</p>
                <pre className="mt-1 overflow-x-auto rounded-md bg-muted px-3 py-2 font-mono text-xs">
{`TIER1_RULES["${(promote?.raw ?? "").trim().toUpperCase()}"] = "${chosen || "…"}"`}
                </pre>
              </div>
              {error && (
                <p className="rounded-md border border-destructive/20 bg-destructive/5 px-3 py-2 text-xs text-destructive">
                  {error}
                </p>
              )}
              <p className="text-xs text-muted-foreground">
                In-memory for the demo — a server restart reverts it.
              </p>
            </div>
          )}

          <DialogFooter>
            {result ? (
              <Button onClick={() => setPromote(null)}>Done</Button>
            ) : (
              <>
                <Button variant="outline" onClick={() => setPromote(null)} disabled={busy}>
                  Cancel
                </Button>
                <Button onClick={submit} disabled={busy || !chosen}>
                  {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <CheckCircle2 className="h-4 w-4" />}
                  {busy ? "Promoting…" : "Approve & promote"}
                </Button>
              </>
            )}
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
