import { useCallback, useEffect, useState } from "react";
import {
  AlertCircle,
  FileSearch,
  LayoutDashboard,
  Loader2,
  RefreshCw,
  ShieldAlert,
  Split,
  Zap,
  type LucideIcon,
} from "lucide-react";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { api, type Summary } from "@/lib/api";
import Overview from "@/screens/Overview";
import OpsQueue from "@/screens/OpsQueue";
import Chaos from "@/screens/Chaos";
import Reconciliation from "@/screens/Reconciliation";
import Audit from "@/screens/Audit";

// Sidebar nav. Drives the nav list only — each tab's content is declared
// explicitly below because the screens take different props.
const NAV: { value: string; label: string; Icon: LucideIcon }[] = [
  { value: "overview", label: "Overview", Icon: LayoutDashboard },
  { value: "ops", label: "Ops Queue", Icon: ShieldAlert },
  { value: "chaos", label: "Chaos Trigger", Icon: Zap },
  { value: "recon", label: "Reconciliation", Icon: Split },
  { value: "audit", label: "Audit Trail", Icon: FileSearch },
];

export default function App() {
  const [data, setData] = useState<Summary | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setData(await api.summary());
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    // The Tabs root wraps the whole shell so the sidebar list and the main
    // content stay in one Radix context. It renders regardless of load state,
    // so the sidebar is present while metrics are still loading.
    <Tabs
      orientation="vertical"
      defaultValue="overview"
      className="flex min-h-screen bg-muted/30"
    >
      {/* The <aside> stretches to the full document height so its background and
          right border run the whole page; the inner div is what actually sticks. */}
      <aside className="z-50 w-16 shrink-0 border-r bg-background lg:w-60">
        <div className="sticky top-0 flex h-screen flex-col">
        <div className="flex h-16 shrink-0 flex-col items-center justify-center border-b px-3 lg:items-start lg:px-4">
          {/* Below lg the sidebar collapses to icons only, so the wordmark
              collapses to its initial rather than leaving the header empty. */}
          <span className="text-xl font-semibold leading-none lg:hidden">A</span>
          <div className="hidden min-w-0 lg:block">
            <h1 className="truncate text-xl font-semibold leading-tight">Avirata</h1>
            <p className="truncate text-xs text-muted-foreground">
              Silent Mandate Death Recovery
            </p>
          </div>
        </div>

        <nav className="flex-1 overflow-y-auto p-2 lg:p-3">
          <TabsList className="h-auto w-full flex-col items-stretch justify-start gap-1 rounded-none bg-transparent p-0">
            {NAV.map(({ value, label, Icon }) => (
              <TabsTrigger
                key={value}
                value={value}
                title={label}
                className="w-full justify-center gap-2.5 px-2 py-2 lg:justify-start lg:px-3 data-[state=active]:bg-primary/10 data-[state=active]:text-primary data-[state=active]:shadow-none"
              >
                <Icon className="h-4 w-4 shrink-0" />
                <span className="hidden lg:inline">{label}</span>
              </TabsTrigger>
            ))}
          </TabsList>
        </nav>
        </div>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-40 border-b bg-background/95 backdrop-blur supports-[backdrop-filter]:bg-background/75">
          <div className="flex h-16 items-center px-6">
            <div className="ml-auto flex items-center gap-3">
              <Badge variant="warning" className="hidden sm:inline-flex">
                controlled simulation · fixed seed
              </Badge>
              {data && (
                <span className="hidden text-xs text-muted-foreground lg:inline">
                  updated {new Date(data.generated_at).toLocaleTimeString()}
                </span>
              )}
              <Button size="sm" variant="outline" onClick={load} disabled={loading}>
                {loading ? (
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                ) : (
                  <RefreshCw className="h-3.5 w-3.5" />
                )}
                Refresh
              </Button>
            </div>
          </div>
        </header>

        <main className="flex-1 px-6 py-8">
          {error && (
            <Card className="mb-6 border-destructive/30">
              <CardContent className="flex items-start gap-3 p-5">
                <AlertCircle className="mt-0.5 h-4 w-4 shrink-0 text-destructive" />
                <div className="text-sm">
                  <p className="font-medium text-destructive">Could not load metrics</p>
                  <p className="mt-1 text-muted-foreground">{error}</p>
                  <p className="mt-2 text-xs text-muted-foreground">
                    Is uvicorn running, and has{" "}
                    <code className="font-mono">scripts/seed_demo.py</code> been run? Note that a
                    pytest run truncates every table.
                  </p>
                </div>
              </CardContent>
            </Card>
          )}

          {!data && loading && (
            <div className="flex items-center justify-center py-32 text-muted-foreground">
              <Loader2 className="mr-2 h-5 w-5 animate-spin" /> Loading metrics…
            </div>
          )}

          {data && (
            <>
              <TabsContent value="overview" className="mt-0">
                <Overview data={data} />
              </TabsContent>
              <TabsContent value="ops" className="mt-0">
                <OpsQueue data={data} onPromoted={load} />
              </TabsContent>
              <TabsContent value="chaos" className="mt-0">
                <Chaos onDone={load} />
              </TabsContent>
              <TabsContent value="recon" className="mt-0">
                <Reconciliation data={data} />
              </TabsContent>
              <TabsContent value="audit" className="mt-0">
                <Audit data={data} />
              </TabsContent>
            </>
          )}

          <footer className="mt-12 border-t pt-6 text-xs text-muted-foreground">
            Alt-rail execution is a prototype requiring RBI e-mandate / AFA review before production
            use. Figures come from a fixed synthetic seed, not live production data.
          </footer>
        </main>
      </div>
    </Tabs>
  );
}
