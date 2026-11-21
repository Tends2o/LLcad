/** What the "Drawing Export" area shows for the revision on screen, from
 *  GET /api/models/:id/drawings. Pure, so it is tested without a browser. */
export type DrawingRow = {
  drawing_id: string;
  revision: string;
  created: string;
  pdf_artifact_id: string;
  sheets: number;
  render_id: string;
};
export type DrawingRun = {
  annotation_id: string;
  revision: string;
  state: string;
  created: string;
  activity: string | null;
  incomplete_reason: string | null;
  error: string | null;
  drawing_id: string | null;
};
export type DrawingView = {
  kind: "none" | "running" | "current" | "stale" | "failed";
  /** The drawing to offer: of this revision, else the newest (stale). */
  drawing: DrawingRow | null;
  activity: string | null;
  message: string;
};

export function drawingState(data: { drawings: DrawingRow[]; runs: DrawingRun[] }, revision: string | null): DrawingView {
  const drawings = [...(data.drawings ?? [])].sort((a, b) => (a.created < b.created ? 1 : -1));
  const own = drawings.find((d) => d.revision === revision) ?? null;
  const newest = drawings[0] ?? null;
  const run = (data.runs ?? [])[0] ?? null;
  if (run && (run.state === "running" || run.state === "queued"))
    return {
      kind: "running",
      drawing: own ?? newest,
      activity: run.activity,
      message: "Drawing is being created …",
    };
  if (own)
    return {
      kind: "current",
      drawing: own,
      activity: null,
      message: `PDF of this revision · ${own.sheets} sheet${own.sheets === 1 ? "" : "s"}`,
    };
  if (run && run.revision === revision && (run.state === "failed" || run.state === "incomplete"))
    return {
      kind: "failed",
      drawing: newest,
      activity: null,
      message: run.incomplete_reason ?? run.error ?? "The drawing run ended without a PDF.",
    };
  if (newest)
    return {
      kind: "stale",
      drawing: newest,
      activity: null,
      message: `Outdated: drawing of revision ${newest.revision.slice(0, 12)}; the model has changed since.`,
    };
  return { kind: "none", drawing: null, activity: null, message: "" };
}
