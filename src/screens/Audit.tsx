import { useState } from "react";
import { FileSearch, Loader2, Search } from "lucide-react";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { api, rupee, type AuditTrace, type Summary } from "@/lib/api";

const TIER_VARIANT = { 1: "success", 2: "info", 3: "destructive" } as const;

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div>
      <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
        {title}
      </h3>
      {children}
    </div>
  );
}

export default function Audit({ data }: { data: Summary }) {
  // Seed the inputs from a real key so the screen is usable without typing.
  const seedCase = data.safe_hold_cases[0] ?? data.ops_queue[0];
  const [mandateId, setMandateId] = useState(seedCase?.mandate_id ?? "");
  const [cycle, setCycle] = useState(seedCase?.billing_cycle ?? "");
  const [trace, setTrace] = useState<AuditTrace | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function search() {
    if (!mandateId.trim() || !cycle.trim()) return;
    setBusy(true);
    setError(null);
    try {
      setTrace(await api.audit(mandateId.trim(), cycle.trim()));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setTrace(null);
    } finally {
      setBusy(false);
    }
  }

  const suggestions = [
    ...data.reconciliation_races.slice(0, 3).map((r) => ({ m: r.mandate_id, c: r.billing_cycle })),
    ...data.ops_queue.slice(0, 3).map((o) => ({ m: o.mandate_id, c: o.billing_cycle })),
  ].slice(0, 5);

  return (
    <div className="space-y-6">
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-base">
            <FileSearch className="h-4 w-4 text-muted-foreground" />
            Decision trace
          </CardTitle>
          <CardDescription>
            Why was this customer charged this way? Every layer that touched the key contributes its
            own recorded explanation — nothing is reconstructed after the fact.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="flex flex-wrap gap-2">
            <Input
              value={mandateId}
              onChange={(e) => setMandateId(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && search()}
              placeholder="mandate_id (e.g. MND-00042)"
              className="w-full font-mono text-xs sm:w-64"
            />
            <Input
              value={cycle}
              onChange={(e) => setCycle(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && search()}
              placeholder="billing_cycle (YYYY-MM)"
              className="w-full font-mono text-xs sm:w-44"
            />
            <Button onClick={search} disabled={busy || !mandateId.trim() || !cycle.trim()}>
              {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <Search className="h-4 w-4" />}
              Search
            </Button>
          </div>
          {suggestions.length > 0 && (
            <div className="flex flex-wrap items-center gap-1.5">
              <span className="text-xs text-muted-foreground">Try:</span>
              {suggestions.map((s) => (
                <Button
                  key={`${s.m}-${s.c}`}
                  size="sm"
                  variant="outline"
                  className="font-mono text-[11px]"
                  onClick={() => {
                    setMandateId(s.m);
                    setCycle(s.c);
                  }}
                >
                  {s.m} · {s.c}
                </Button>
              ))}
            </div>
          )}
          {error && (
            <p className="rounded-md border border-destructive/20 bg-destructive/5 px-3 py-2 text-xs text-destructive">
              {error}
            </p>
          )}
        </CardContent>
      </Card>

      {trace && !trace.found && (
        <Card>
          <CardContent className="py-10 text-center text-sm text-muted-foreground">
            Nothing recorded for{" "}
            <span className="font-mono">
              {trace.mandate_id} · {trace.billing_cycle}
            </span>
            . That is a real answer, not an error — this key was never touched.
          </CardContent>
        </Card>
      )}

      {trace?.found && (
        <div className="space-y-4">
          <Card>
            <CardContent className="space-y-6 p-6">
              <Section title={`Decline events (${trace.events.length})`}>
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Event</TableHead>
                      <TableHead>Raw code</TableHead>
                      <TableHead>Segment</TableHead>
                      <TableHead className="text-right">Amount</TableHead>
                      <TableHead>Arm</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {trace.events.map((e) => (
                      <TableRow key={e.event_id}>
                        <TableCell className="font-mono text-xs">{e.event_id}</TableCell>
                        <TableCell className="font-mono text-xs">{e.raw_error_code}</TableCell>
                        <TableCell className="text-xs text-muted-foreground">{e.segment}</TableCell>
                        <TableCell className="text-right tabular-nums">{rupee(e.amount)}</TableCell>
                        <TableCell>
                          {e.arm ? <Badge variant="secondary">{e.arm}</Badge> : "—"}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </Section>

              <Section title={`Diagnoses (${trace.diagnoses.length})`}>
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Event</TableHead>
                      <TableHead>Tier</TableHead>
                      <TableHead>Cause</TableHead>
                      <TableHead className="text-right">Confidence</TableHead>
                      <TableHead>Sanitized input</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {trace.diagnoses.map((d, i) => (
                      <TableRow key={`${d.event_id}-${i}`}>
                        <TableCell className="font-mono text-xs">{d.event_id}</TableCell>
                        <TableCell>
                          <Badge variant={TIER_VARIANT[d.tier as 1 | 2 | 3] ?? "secondary"}>
                            Tier {d.tier}
                          </Badge>
                        </TableCell>
                        <TableCell className="font-mono text-xs">{d.cause ?? "—"}</TableCell>
                        <TableCell className="text-right tabular-nums">
                          {d.confidence == null ? "—" : d.confidence.toFixed(2)}
                        </TableCell>
                        <TableCell className="max-w-[220px] truncate font-mono text-xs text-muted-foreground">
                          {d.sanitized_input ?? "—"}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
                <p className="mt-2 text-xs text-muted-foreground">
                  A key can carry several events in one cycle. Pair each diagnosis to its event by{" "}
                  <code className="font-mono">event_id</code>, not row order.
                </p>
              </Section>

              <Section title="Action">
                {trace.action ? (
                  <div className="rounded-md border p-4">
                    <div className="flex flex-wrap items-center gap-2">
                      <Badge>{trace.action.action_type}</Badge>
                      <Badge variant="secondary">{trace.action.status}</Badge>
                      <span className="text-xs text-muted-foreground">
                        dispatched {new Date(trace.action.created_at).toLocaleString()}
                        {trace.action.resolved_at &&
                          ` · resolved ${new Date(trace.action.resolved_at).toLocaleString()}`}
                      </span>
                    </div>
                    <pre className="mt-3 overflow-x-auto rounded bg-muted px-3 py-2 font-mono text-[11px] leading-relaxed">
{JSON.stringify(trace.action.params, null, 2)}
                    </pre>
                  </div>
                ) : (
                  <p className="text-sm text-muted-foreground">No action was dispatched.</p>
                )}
              </Section>

              <Section title={`Reconciliation (${trace.reconciliation.length})`}>
                {trace.reconciliation.length ? (
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>Path</TableHead>
                        <TableHead>Status</TableHead>
                        <TableHead className="text-right">Amount</TableHead>
                        <TableHead>Opened</TableHead>
                        <TableHead>Resolved</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {trace.reconciliation.map((r) => (
                        <TableRow key={r.path}>
                          <TableCell className="font-mono text-xs">{r.path}</TableCell>
                          <TableCell>
                            <Badge variant={r.status === "settled" ? "success" : "warning"}>
                              {r.status}
                            </Badge>
                          </TableCell>
                          <TableCell className="text-right tabular-nums">{rupee(r.amount)}</TableCell>
                          <TableCell className="text-xs text-muted-foreground">
                            {new Date(r.opened_at).toLocaleString()}
                          </TableCell>
                          <TableCell className="text-xs text-muted-foreground">
                            {r.resolved_at ? new Date(r.resolved_at).toLocaleString() : "—"}
                          </TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                ) : (
                  <p className="text-sm text-muted-foreground">No settlement hold was opened.</p>
                )}
              </Section>

              {trace.escalations.length > 0 && (
                <Section title={`Escalations (${trace.escalations.length})`}>
                  <div className="space-y-2">
                    {trace.escalations.map((e, i) => (
                      <div key={i} className="flex flex-wrap items-center gap-2 rounded-md border p-3">
                        <Badge variant="warning">{e.reason}</Badge>
                        <span className="font-mono text-xs text-muted-foreground">
                          {e.source_layer}
                        </span>
                        <span className="text-xs text-muted-foreground">
                          {new Date(e.at).toLocaleString()}
                        </span>
                      </div>
                    ))}
                  </div>
                </Section>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="pb-3">
              <CardTitle className="text-sm">Raw response</CardTitle>
              <CardDescription className="font-mono text-xs">
                GET /api/audit/decision/{trace.mandate_id}/{trace.billing_cycle}
              </CardDescription>
            </CardHeader>
            <CardContent>
              <pre className="max-h-96 overflow-auto rounded bg-muted px-3 py-2 font-mono text-[11px] leading-relaxed">
{JSON.stringify(trace, null, 2)}
              </pre>
            </CardContent>
          </Card>
        </div>
      )}
    </div>
  );
}
