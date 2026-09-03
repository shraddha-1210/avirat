import { ArrowRight, ShieldCheck, Split } from "lucide-react";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { duration, int, rupee, type LedgerPath, type Race, type Summary } from "@/lib/api";
import { cn } from "@/lib/utils";

const STATUS_VARIANT: Record<string, "success" | "warning" | "destructive" | "secondary"> = {
  settled: "success",
  auto_refunded: "warning",
  expired_escalated: "destructive",
  closed_superseded: "secondary",
  pending: "secondary",
};

function PathCard({
  name,
  path,
  outcome,
}: {
  name: string;
  path: LedgerPath | undefined;
  outcome: "won" | "refunded" | "other";
}) {
  if (!path) {
    return (
      <div className="rounded-md border border-dashed p-4 text-sm text-muted-foreground">
        {name} — no hold opened
      </div>
    );
  }
  return (
    <div
      className={cn(
        "rounded-md border p-4",
        outcome === "won" && "border-emerald-200 bg-emerald-50/50",
        outcome === "refunded" && "border-amber-200 bg-amber-50/50"
      )}
    >
      <div className="flex items-center justify-between gap-2">
        <span className="font-mono text-xs font-medium">{name}</span>
        <Badge variant={STATUS_VARIANT[path.status] ?? "secondary"}>{path.status}</Badge>
      </div>
      <p className="mt-2 text-lg font-semibold tabular-nums">{rupee(path.amount)}</p>
      <dl className="mt-2 space-y-0.5 text-xs text-muted-foreground">
        <div className="flex justify-between gap-3">
          <dt>opened</dt>
          <dd className="font-mono">{new Date(path.opened_at).toLocaleTimeString()}</dd>
        </div>
        <div className="flex justify-between gap-3">
          <dt>resolved</dt>
          <dd className="font-mono">
            {path.resolved_at ? new Date(path.resolved_at).toLocaleTimeString() : "—"}
          </dd>
        </div>
      </dl>
    </div>
  );
}

export default function Reconciliation({ data }: { data: Summary }) {
  const races = data.reconciliation_races;
  const refunded = data.ledger_by_status.find((l) => l.key === "auto_refunded")?.count ?? 0;
  const settled = data.ledger_by_status.find((l) => l.key === "settled")?.count ?? 0;

  return (
    <div className="space-y-6">
      <div className="grid gap-4 sm:grid-cols-3">
        <Card>
          <CardContent className="p-5">
            <p className="text-sm text-muted-foreground">Keys where both rails raced</p>
            <p className="mt-2 text-3xl font-semibold tabular-nums">{int(races.length)}</p>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="p-5">
            <p className="text-sm text-muted-foreground">Collisions auto-refunded</p>
            <p className="mt-2 text-3xl font-semibold tabular-nums text-amber-600">{int(refunded)}</p>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="p-5">
            <p className="text-sm text-muted-foreground">Settled (at most one per key)</p>
            <p className="mt-2 text-3xl font-semibold tabular-nums text-emerald-600">{int(settled)}</p>
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardContent className="flex flex-wrap items-center gap-3 p-4 text-sm">
          <ShieldCheck className="h-4 w-4 shrink-0 text-emerald-600" />
          <span>
            <strong className="font-semibold">Exactly one settled row per key</strong> is enforced by a
            partial unique index —{" "}
            <code className="rounded bg-muted px-1.5 py-0.5 font-mono text-xs">
              UNIQUE (mandate_id, billing_cycle) WHERE status = 'settled'
            </code>{" "}
            — not by application logic. The second arrival hits the index and is refunded.
          </span>
        </CardContent>
      </Card>

      {!races.length && (
        <Card>
          <CardContent className="py-12 text-center text-sm text-muted-foreground">
            No key has had both rails open a hold yet. Run{" "}
            <code className="font-mono">scripts/seed_demo.py</code> — it injects collisions
            deliberately — or fire an ALT_RAIL from the Chaos tab and then settle the mandate path.
          </CardContent>
        </Card>
      )}

      <div className="grid gap-4 lg:grid-cols-2">
        {races.map((r: Race) => (
          <Card key={`${r.mandate_id}-${r.billing_cycle}`}>
            <CardHeader className="pb-4">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <CardTitle className="font-mono text-sm">
                  {r.mandate_id} · {r.billing_cycle}
                </CardTitle>
                {r.collision ? (
                  <Badge variant="warning">
                    <Split className="h-3 w-3" /> collision
                  </Badge>
                ) : (
                  <Badge variant="secondary">no collision</Badge>
                )}
              </div>
              <CardDescription>
                {r.winner ? (
                  <>
                    <span className="font-mono">{r.winner}</span> settled first
                    {r.refunded && (
                      <>
                        {" "}
                        · <span className="font-mono">{r.refunded}</span> auto-refunded
                      </>
                    )}
                    {r.gap_seconds != null && <> · {duration(r.gap_seconds)} apart</>}
                  </>
                ) : (
                  "neither path settled"
                )}
              </CardDescription>
            </CardHeader>
            <CardContent>
              <div className="grid items-stretch gap-3 sm:grid-cols-[1fr_auto_1fr]">
                <PathCard
                  name="mandate"
                  path={r.paths["mandate"]}
                  outcome={
                    r.winner === "mandate" ? "won" : r.refunded === "mandate" ? "refunded" : "other"
                  }
                />
                <div className="flex items-center justify-center">
                  <ArrowRight className="h-4 w-4 rotate-90 text-muted-foreground sm:rotate-0" />
                </div>
                <PathCard
                  name="alt_rail"
                  path={r.paths["alt_rail"]}
                  outcome={
                    r.winner === "alt_rail" ? "won" : r.refunded === "alt_rail" ? "refunded" : "other"
                  }
                />
              </div>
            </CardContent>
          </Card>
        ))}
      </div>
    </div>
  );
}
