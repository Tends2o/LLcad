import { Store } from "./store.js";
import { Principal, authorize } from "../policy/index.js";
import { id } from "../semantic-ir/hash.js";
import { requireThat } from "../semantic-ir/errors.js";

export function faces(store: Store, revision: any, feature: string): any[] {
  const key = revision.geometry?.facts?.[feature]?.cache_key;
  if (!key) return [];
  // A revision names the archives it produced itself; everything it merely
  // reused stands in the cache under the same content key, and both are stored
  // by the hash of their bytes, so the integrity check reads the same either way.
  const stored = (suffix: string, name: string) =>
    revision.geometry?.blobs?.[key + name] ??
    store.get("SELECT blob FROM cache WHERE key=?", key + suffix)?.blob;
  const blob = stored(":topology", ".topology.json");
  if (!blob) return [];
  const data = JSON.parse(store.readBlob(blob).toString());
  requireThat(
    [1, 2, 3].includes(data.version) &&
      data.cache_key === key &&
      (data.version !== 3 || data.brep_sha256 === stored("", ".brep")),
    "INTEGRITY_FAILURE",
    "The face history does not match the revision.",
  );
  return data.faces;
}

function exactFace(store: Store, rev: any, feature: string, faceID: string) {
  const matches = faces(store, rev, feature).filter(
    (f) => f.face_id === faceID,
  );
  requireThat(
    matches.length === 1,
    "AMBIGUOUS_SELECTION",
    "The face is not uniquely available in this revision.",
  );
  return matches[0];
}

function owner(face: any): string {
  const owners = [
    ...new Set<string>(face.origins.map((o: any) => o.feature_id)),
  ];
  requireThat(
    owners.length === 1,
    "AMBIGUOUS_SELECTION",
    "No unique generating feature is recorded for this face. Narrow it down semantically with cad_find and give the feature id.",
  );
  return owners[0];
}

function rebindFace(
  store: Store,
  p: Principal,
  model: string,
  source: any,
  target: any,
  geometryFeature: string,
  faceID: string,
) {
  const original = exactFace(store, source, geometryFeature, faceID);
  requireThat(
    original.origins.length === 1,
    "AMBIGUOUS_SELECTION",
    "The source face has no unique origin.",
  );
  const originKey = original.origins[0].key;
  const matching = (rev: any) =>
    faces(store, rev, geometryFeature).filter((f) =>
      f.origins.some((o: any) => o.key === originKey),
    );
  requireThat(
    matching(source).length === 1,
    "AMBIGUOUS_SELECTION",
    "The source face was already split; automatic reassignment is not allowed.",
  );
  // Check every intervening revision: a temporary split/merge cannot silently
  // disappear and resurrect an old selection at a later revision.
  let cursor = target;
  let result: any;
  for (let depth = 0; depth < 1024; depth++) {
    if (cursor.id === source.id) return result ?? original;
    const matches = matching(cursor);
    requireThat(
      matches.length === 1 && matches[0].origins.length === 1,
      "AMBIGUOUS_SELECTION",
      "The face was removed, split or merged. Please select it again.",
      { revision: cursor.id, candidate_count: matches.length },
    );
    requireThat(
      owner(matches[0]) === owner(original),
      "AMBIGUOUS_SELECTION",
      "The generating feature has changed.",
    );
    result ??= matches[0];
    requireThat(
      cursor.parent,
      "STALE_REVISION",
      "The target revision does not descend from the selection revision.",
    );
    cursor = store.revision(p, model, cursor.parent);
  }
  requireThat(
    false,
    "BUDGET_EXCEEDED",
    "The revision chain is too long for automatic reassignment.",
  );
}

export function resolveSelection(
  store: Store,
  p: Principal,
  args: any,
  rev: any,
) {
  requireThat(
    !args.rebind || (args.selection_handle && args.revision && !args.face_id),
    "INVALID_SCHEMA",
    "Reassignment needs a selection handle and an explicit target revision.",
  );
  let fid = args.feature_id;
  let selectedFace: any = null;
  let geometryFeature: string | undefined;
  if (args.selection_handle) {
    requireThat(
      !args.face_id,
      "INVALID_SCHEMA",
      "Do not give a face id and a selection handle together.",
    );
    const h = store.get(
      "SELECT * FROM selections WHERE id=?",
      args.selection_handle,
    );
    requireThat(h, "AMBIGUOUS_SELECTION", "Selection is not available.");
    authorize(p, "model:read", h);
    store.model(p, h.model);
    requireThat(
      h.model === args.model_id && h.expires > Date.now(),
      "STALE_REVISION",
      "The selection is stale or expired.",
    );
    const target = store.get(
      "SELECT * FROM selection_faces WHERE selection_id=?",
      h.id,
    );
    if (h.revision !== rev.id) {
      requireThat(
        args.rebind && target,
        "STALE_REVISION",
        "The selection is bound to another revision. Reassign the face selection explicitly.",
      );
      const source = store.revision(p, args.model_id, h.revision);
      selectedFace = rebindFace(
        store,
        p,
        args.model_id,
        source,
        rev,
        target.geometry_feature,
        target.face_id,
      );
    } else if (target) {
      selectedFace = exactFace(
        store,
        rev,
        target.geometry_feature,
        target.face_id,
      );
    }
    if (target) geometryFeature = target.geometry_feature;
    requireThat(
      !fid || fid === h.feature,
      "AMBIGUOUS_SELECTION",
      "Contradictory selection.",
    );
    fid = h.feature;
    if (selectedFace)
      requireThat(
        owner(selectedFace) === fid,
        "INTEGRITY_FAILURE",
        "The selection and the face origin do not match.",
      );
  } else if (args.face_id) {
    requireThat(
      fid && args.revision,
      "INVALID_SCHEMA",
      "A face selection needs a feature id and an explicit revision.",
    );
    geometryFeature = fid;
    selectedFace = exactFace(store, rev, fid, args.face_id);
    fid = owner(selectedFace);
  }
  requireThat(
    fid,
    "AMBIGUOUS_SELECTION",
    "A unique feature id or selection is required.",
  );
  const feature = rev.ir.features.find((f: any) => f.id === fid);
  requireThat(
    feature,
    "AMBIGUOUS_SELECTION",
    "The feature is not uniquely available in this revision.",
  );
  return { rev, feature, selectedFace, geometryFeature };
}

export function selectionHandle(
  store: Store,
  p: Principal,
  model: string,
  selection: ReturnType<typeof resolveSelection>,
  anchor: Record<string, unknown> | null = null,
) {
  const handle = id("sel");
  store.run(
    "INSERT INTO selections VALUES(?,?,?,?,?,?,?)",
    handle,
    p.tenant,
    p.user,
    model,
    selection.rev.id,
    selection.feature.id,
    Date.now() + 15 * 60 * 1000,
  );
  if (selection.selectedFace)
    store.run(
      "INSERT INTO selection_faces(selection_id,geometry_feature,face_id,anchor) VALUES(?,?,?,?)",
      handle,
      selection.geometryFeature,
      selection.selectedFace.face_id,
      anchor ? JSON.stringify(anchor) : null,
    );
  return handle;
}
/** Semantic anchor of a viewer hit: revision-bound face, local frame, barycentric point and camera relation. */
export function selectionAnchor(
  args: any,
  selection: ReturnType<typeof resolveSelection>,
  stored: string | null,
) {
  const provided = args.anchor ?? (stored ? JSON.parse(stored) : null);
  if (!provided || !selection.selectedFace) return null;
  const point = provided.point.map(Number),
    normal = provided.normal ? provided.normal.map(Number) : null,
    view = provided.view_direction ? provided.view_direction.map(Number) : null;
  requireThat(
    [...point, ...(normal ?? []), ...(view ?? [])].every(
      (v: number) => Number.isFinite(v) && Math.abs(v) <= 1e6,
    ),
    "INVALID_SCHEMA",
    "Invalid selection anchor.",
  );
  const box = selection.selectedFace.bounds;
  const slack = 1e-6;
  requireThat(
    !box ||
      point.every(
        (v: number, i: number) =>
          v >= box[i] - slack && v <= box[i + 3] + slack,
      ),
    "OUT_OF_SCOPE",
    "The anchor point is not on the selected face.",
  );
  const facing =
    normal && view
      ? normal.reduce(
          (sum: number, n: number, i: number) => sum + n * view[i],
          0,
        ) < 0
      : null;
  return {
    point_mm: point,
    normal,
    barycentric: provided.barycentric ? provided.barycentric.map(Number) : null,
    triangle_index: provided.triangle_index ?? null,
    local_frame: selection.feature.local_frame,
    geometry_feature_id: selection.geometryFeature!,
    face_id: selection.selectedFace.face_id,
    view_relative: {
      facing_camera: facing,
      note: "left/right/inside are camera or topology relative; this anchor records the camera direction explicitly",
    },
  };
}

export function faceSummary(face: any) {
  const { fingerprint, ...summary } = face;
  return { ...summary, unit: "mm", area_unit: "mm2" };
}
