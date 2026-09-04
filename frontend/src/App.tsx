import { useCallback, useEffect, useState } from "react";
import {
  Activity,
  AlertCircle,
  FileSearch,
  LayoutDashboard,
  Loader2,
  RefreshCw,
  ShieldAlert,
  Split,
  Zap,
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
    <div className="min-h-screen bg-muted/30">
      <header className="sticky top-0 z-40 border-b bg-background/95 backdrop-blur supports-[backdrop-filter]:bg-background/75">
        <div className="mx-auto flex max-w-[1400px] flex-wrap items-center gap-4 px-6 py-4">
          <div className="flex items-center gap-2.5">
            <div className="flex h-8 w-8 items-center justify-center rounded-md bg-primary text-primary-foreground">
              <Activity className="h-4 w-4" />
            </div>
            <div>
              <h1 className="text-sm font-semibold leading-tight">Avirata</h1>
              <p className="text-xs text-muted-foreground">Silent Mandate Death Recovery</p>
            </div>
          </div>

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

      <main className="mx-auto max-w-[1400px] px-6 py-8">
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
          <Tabs defaultValue="overview">
            <TabsList className="flex h-auto w-full flex-wrap justify-start sm:w-auto">
              <TabsTrigger value="overview">
                <LayoutDashboard className="h-4 w-4" /> Overview
              </TabsTrigger>
              <TabsTrigger value="ops">
                <ShieldAlert className="h-4 w-4" /> Ops Queue
              </TabsTrigger>
              <TabsTrigger value="chaos">
                <Zap className="h-4 w-4" /> Chaos Trigger
              </TabsTrigger>
              <TabsTrigger value="recon">
                <Split className="h-4 w-4" /> Reconciliation
              </TabsTrigger>
              <TabsTrigger value="audit">
                <FileSearch className="h-4 w-4" /> Audit Trail
              </TabsTrigger>
            </TabsList>

            <TabsContent value="overview">
              <Overview data={data} />
            </TabsContent>
            <TabsContent value="ops">
              <OpsQueue data={data} onPromoted={load} />
            </TabsContent>
            <TabsContent value="chaos">
              <Chaos onDone={load} />
            </TabsContent>
            <TabsContent value="recon">
              <Reconciliation data={data} />
            </TabsContent>
            <TabsContent value="audit">
              <Audit data={data} />
            </TabsContent>
          </Tabs>
        )}

        <footer className="mt-12 border-t pt-6 text-xs text-muted-foreground">
          Alt-rail execution is a prototype requiring RBI e-mandate / AFA review before production
          use. Figures come from a fixed synthetic seed, not live production data.
        </footer>
      </main>
    </div>
  );
}
