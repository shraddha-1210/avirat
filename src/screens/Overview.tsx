import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  LabelList,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { AlertTriangle, IndianRupee, Timer, TrendingUp, Inbox } from "lucide-react";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { duration, int, rupee, type Summary } from "@/lib/api";
import { cn } from "@/lib/utils";

// Validated categorical pair (blue / orange): CVD ΔE 24.7, normal-vision 33.6.
const TREATMENT = "#2a78d6";
const CONTROL = "#eb6834";
// Ordinal blue ramp for tiers — one hue, monotone lightness, light end clears 2:1.
const TIER = ["#86b6ef", "#2a78d6", "#104281"];

function Stat({
  label,
  value,
  foot,
  icon: Icon,
  tone,
}: {
  label: string;
  value: string;
  foot?: string;
  icon: React.ElementType;
  tone?: "positive";
}) {
  return (
    <Card>
      <CardContent className="p-5">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <p className="text-sm text-muted-foreground">{label}</p>
            <p
              className={cn(
                "mt-2 text-3xl font-semibold tracking-tight",
                tone === "positive" && "text-emerald-600"
              )}
            >
              {value}
            </p>
            {foot && <p className="mt-1.5 text-xs text-muted-foreground">{foot}</p>}
          </div>
          <div className="rounded-md bg-muted p-2 text-muted-foreground">
            <Icon className="h-4 w-4" />
          </div>
        </div>
      </CardContent>
    </Card>
  );
}

function ChartTooltip({ active, payload, label, fmt }: any) {
  if (!active || !payload?.length) return null;
  return (
    <div className="rounded-md border bg-popover px-3 py-2 text-xs shadow-md">
      <p className="mb-1 font-medium text-popover-foreground">{label}</p>
      {payload.map((p: any) => (
        <p key={p.dataKey} className="text-muted-foreground">
          {p.name}: <span className="font-medium text-popover-foreground">{fmt(p.value)}</span>
        </p>
      ))}
    </div>
  );
}

export default function Overview({ data }: { data: Summary }) {
  const { recovered: rec, recovery_rate: rate } = data;
  const openOps = data.ops_queue.filter((o) => o.status === "open").length;

  const mttrData = data.mttr_by_tier.map((t) => ({
    name: `Tier ${t.tier}`,
    hours: t.mttr_seconds == null ? 0 : t.mttr_seconds / 3600,
    measurable: t.mttr_seconds != null,
    resolved: t.resolved_count,
    inFlight: t.in_flight_count,
  }));

  const tierDist = data.mttr_by_tier.map((t) => ({
    name: `Tier ${t.tier}`,
    resolved: t.resolved_count,
    inFlight: t.in_flight_count,
  }));

  const recData = [
    { name: "Treatment", value: rec.treatment_recovered, fill: TREATMENT },
    { name: "Control", value: rec.control_recovered, fill: CONTROL },
  ];

  return (
    <div className="space-y-6">
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Stat
          icon={IndianRupee}
          label="Recovered (treatment − control)"
          value={rec.computable ? rupee(rec.delta) : "not measured"}
          tone={rec.computable && rec.delta > 0 ? "positive" : undefined}
          foot={
            rec.computable
              ? `${rupee(rec.per_mandate_delta)} per mandate · arms ${rec.treatment_mandates}/${rec.control_mandates}`
              : "no arm assignment recorded"
          }
        />
        <Stat
          icon={TrendingUp}
          label={`Within ${rate.sla_hours}h SLA`}
          value={rate.rate == null ? "not measured" : `${(rate.rate * 100).toFixed(1)}%`}
          foot={`${int(rate.resolved_within_sla)} of ${int(rate.actions_total)} dispatched · ${int(rate.actions_in_flight)} in flight`}
        />
        <Stat
          icon={Inbox}
          label="Open Ops escalations"
          value={int(openOps)}
          foot={openOps ? "awaiting human review" : "queue clear"}
        />
        <Stat
          icon={AlertTriangle}
          label="Tier 3 quarantine"
          value={int(data.quarantine.pending_ops_review)}
          foot="unmapped strings pending review"
        />
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-base">
              <IndianRupee className="h-4 w-4 text-muted-foreground" />
              Rupees recovered by arm
            </CardTitle>
            <CardDescription>
              Settled ledger amounts. Refunded collisions excluded.{" "}
              <Badge variant="warning" className="ml-1 align-middle">
                controlled simulation, fixed seed
              </Badge>
            </CardDescription>
          </CardHeader>
          <CardContent>
            {rec.computable ? (
              <ResponsiveContainer width="100%" height={260}>
                <BarChart data={recData} margin={{ top: 20, right: 12, left: 4, bottom: 4 }}>
                  <CartesianGrid strokeDasharray="3 3" vertical={false} stroke="#e1e0d9" />
                  <XAxis dataKey="name" tickLine={false} axisLine={false} fontSize={12} stroke="#898781" />
                  <YAxis
                    tickFormatter={(v) => `₹${(v / 1000).toFixed(0)}k`}
                    tickLine={false}
                    axisLine={false}
                    fontSize={12}
                    stroke="#898781"
                    width={56}
                  />
                  <Tooltip content={<ChartTooltip fmt={rupee} />} cursor={{ fill: "rgba(0,0,0,0.04)" }} />
                  <Bar dataKey="value" name="Recovered" radius={[4, 4, 0, 0]} maxBarSize={92}>
                    {recData.map((d) => (
                      <Cell key={d.name} fill={d.fill} />
                    ))}
                    <LabelList dataKey="value" position="top" formatter={rupee} fontSize={12} fill="#52514e" />
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            ) : (
              <p className="py-12 text-center text-sm text-muted-foreground">{rec.note}</p>
            )}
            <p className="mt-3 text-xs text-muted-foreground">{rec.note}</p>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-base">
              <Timer className="h-4 w-4 text-muted-foreground" />
              MTTR by diagnosis tier
            </CardTitle>
            <CardDescription>
              Dispatch to terminal state. In-flight actions are excluded, never counted as zero.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <ResponsiveContainer width="100%" height={260}>
              <BarChart data={mttrData} margin={{ top: 20, right: 12, left: 4, bottom: 4 }}>
                <CartesianGrid strokeDasharray="3 3" vertical={false} stroke="#e1e0d9" />
                <XAxis dataKey="name" tickLine={false} axisLine={false} fontSize={12} stroke="#898781" />
                <YAxis
                  tickFormatter={(v) => `${v}h`}
                  tickLine={false}
                  axisLine={false}
                  fontSize={12}
                  stroke="#898781"
                  width={44}
                />
                <Tooltip
                  content={<ChartTooltip fmt={(v: number) => `${v.toFixed(1)} h`} />}
                  cursor={{ fill: "rgba(0,0,0,0.04)" }}
                />
                <Bar dataKey="hours" name="MTTR" radius={[4, 4, 0, 0]} maxBarSize={72}>
                  {mttrData.map((d, i) => (
                    <Cell key={d.name} fill={TIER[i % TIER.length]} />
                  ))}
                  <LabelList
                    dataKey="hours"
                    position="top"
                    formatter={(v: number) => `${v.toFixed(1)}h`}
                    fontSize={12}
                    fill="#52514e"
                  />
                </Bar>
              </BarChart>
            </ResponsiveContainer>
            <div className="mt-3 flex flex-wrap gap-x-5 gap-y-1 text-xs text-muted-foreground">
              {mttrData.map((t) => (
                <span key={t.name}>
                  {t.name}: {t.resolved} resolved, {t.inFlight} in flight
                </span>
              ))}
            </div>
          </CardContent>
        </Card>
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Tier distribution</CardTitle>
            <CardDescription>Resolved vs still in flight, per diagnosis tier.</CardDescription>
          </CardHeader>
          <CardContent>
            <ResponsiveContainer width="100%" height={230}>
              <BarChart data={tierDist} margin={{ top: 8, right: 12, left: 4, bottom: 4 }}>
                <CartesianGrid strokeDasharray="3 3" vertical={false} stroke="#e1e0d9" />
                <XAxis dataKey="name" tickLine={false} axisLine={false} fontSize={12} stroke="#898781" />
                <YAxis tickLine={false} axisLine={false} fontSize={12} stroke="#898781" width={36} />
                <Tooltip content={<ChartTooltip fmt={int} />} cursor={{ fill: "rgba(0,0,0,0.04)" }} />
                <Bar dataKey="resolved" name="Resolved" stackId="a" fill={TREATMENT} radius={[0, 0, 0, 0]} maxBarSize={72} />
                <Bar dataKey="inFlight" name="In flight" stackId="a" fill="#c3c2b7" radius={[4, 4, 0, 0]} maxBarSize={72} />
              </BarChart>
            </ResponsiveContainer>
            <div className="mt-3 flex gap-4 text-xs text-muted-foreground">
              <span className="flex items-center gap-1.5">
                <i className="h-2.5 w-2.5 rounded-sm" style={{ background: TREATMENT }} /> Resolved
              </span>
              <span className="flex items-center gap-1.5">
                <i className="h-2.5 w-2.5 rounded-sm bg-[#c3c2b7]" /> In flight
              </span>
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="text-base">Anomaly detection by segment</CardTitle>
            <CardDescription>
              Median Absolute Deviation per (bank, mandate type). Flagged rows highlighted.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Segment</TableHead>
                  <TableHead className="text-right">Checks</TableHead>
                  <TableHead className="text-right">Flags</TableHead>
                  <TableHead className="text-right">Avg MAD</TableHead>
                  <TableHead className="text-right">Threshold</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {data.detection.segments.map((s) => (
                  <TableRow
                    key={s.segment}
                    className={cn(s.flags > 0 && "bg-amber-50/70 hover:bg-amber-50")}
                  >
                    <TableCell className="font-medium">
                      <span className="flex items-center gap-2">
                        {s.flags > 0 && <AlertTriangle className="h-3.5 w-3.5 text-amber-600" />}
                        {s.segment}
                      </span>
                    </TableCell>
                    <TableCell className="text-right tabular-nums">{int(s.checks)}</TableCell>
                    <TableCell className="text-right tabular-nums">
                      {s.flags > 0 ? (
                        <Badge variant="warning">{s.flags}</Badge>
                      ) : (
                        <span className="text-muted-foreground">0</span>
                      )}
                    </TableCell>
                    <TableCell className="text-right tabular-nums">
                      {s.avg_mad == null ? "—" : s.avg_mad.toFixed(3)}
                    </TableCell>
                    <TableCell className="text-right tabular-nums">
                      {s.threshold == null ? "—" : s.threshold.toFixed(3)}
                    </TableCell>
                  </TableRow>
                ))}
                {!data.detection.segments.length && (
                  <TableRow>
                    <TableCell colSpan={5} className="py-8 text-center text-muted-foreground">
                      No detection runs recorded.
                    </TableCell>
                  </TableRow>
                )}
              </TableBody>
            </Table>
            <p className="mt-3 text-xs text-muted-foreground">
              {data.detection.flags_in_window} anomalies flagged in the last {data.detection.window_hours}h ·
              N ≥ 30 gate applies before any MAD is computed.
            </p>
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Actions dispatched</CardTitle>
          <CardDescription>
            One action per (mandate, billing cycle) — enforced by a database unique constraint.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <div className="flex flex-wrap gap-3">
            {data.actions_by_type.map((a) => (
              <div key={a.key} className="rounded-md border px-4 py-3">
                <p className="font-mono text-xs text-muted-foreground">{a.key}</p>
                <p className="mt-1 text-2xl font-semibold tabular-nums">{int(a.count)}</p>
              </div>
            ))}
            {!data.actions_by_type.length && (
              <p className="text-sm text-muted-foreground">
                No actions dispatched yet — run <code className="font-mono">scripts/seed_demo.py</code>.
              </p>
            )}
          </div>
          <p className="mt-4 text-xs text-muted-foreground">
            Ledger:{" "}
            {data.ledger_by_status.length
              ? data.ledger_by_status.map((l) => `${l.count} ${l.key}`).join(" · ")
              : "empty"}{" "}
            · MTTR excludes in-flight work, so {duration(null)} never means instant.
          </p>
        </CardContent>
      </Card>
    </div>
  );
}
