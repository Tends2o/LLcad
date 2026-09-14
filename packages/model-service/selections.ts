import { Store } from "./store.js";
import { Principal, authorize } from "../policy/index.js";
import { id } from "../semantic-ir/hash.js";
import { requireThat } from "../semantic-ir/errors.js";

export function faces(store: Store, revision: any, feature: string): any[] {
  const key = revision.geometry?.facts?.[feature]?.cache_key;
  const blob = revision.geometry?.blobs?.[key + ".topology.json"];
  if (!blob) return [];
  const data = JSON.parse(store.readBlob(blob).toString());
  requireThat(
    [1, 2, 3].includes(data.version) &&
      data.cache_key === key &&
      (data.version !== 3 ||
        data.brep_sha256 === revision.geometry.blobs[key + ".brep"]),
    "INTEGRITY_FAILURE",
    "Flächenhistorie passt nicht zur Revision.",
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
    "Fläche ist in dieser Revision nicht eindeutig verfügbar.",
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
    "Für diese Fläche ist kein eindeutiges erzeugendes Merkmal belegt. Mit cad_find semantisch eingrenzen und die Feature-ID angeben.",
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
    "Die Ausgangsfläche hat keine eindeutige Herkunft.",
  );
  const originKey = original.origins[0].key;
  const matching = (rev: any) =>
    faces(store, rev, geometryFeature).filter((f) =>
      f.origins.some((o: any) => o.key === originKey),
    );
  requireThat(
    matching(source).length === 1,
    "AMBIGUOUS_SELECTION",
    "Die Ausgangsfläche war bereits geteilt; eine automatische Neuzuordnung ist nicht zulässig.",
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
      "Die Fläche wurde entfernt, geteilt oder zusammengeführt. Bitte neu auswählen.",
      { revision: cursor.id, candidate_count: matches.length },
    );
    requireThat(
      owner(matches[0]) === owner(original),
      "AMBIGUOUS_SELECTION",
      "Das erzeugende Merkmal hat sich geändert.",
    );
    result ??= matches[0];
    requireThat(
      cursor.parent,
      "STALE_REVISION",
      "Die Zielrevision stammt nicht von der Auswahlrevision ab.",
    );
    cursor = store.revision(p, model, cursor.parent);
  }
  requireThat(
    false,
    "BUDGET_EXCEEDED",
    "Die Revisionskette ist für eine automatische Neuzuordnung zu lang.",
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
    "Neuzuordnung benötigt ein Auswahlhandle und eine ausdrückliche Zielrevision.",
  );
  let fid = args.feature_id;
  let selectedFace: any = null;
  let geometryFeature: string | undefined;
  if (args.selection_handle) {
    requireThat(
      !args.face_id,
      "INVALID_SCHEMA",
      "Flächen-ID und Auswahlhandle nicht gemeinsam angeben.",
    );
    const h = store.get(
      "SELECT * FROM selections WHERE id=?",
      args.selection_handle,
    );
    requireThat(h, "AMBIGUOUS_SELECTION", "Auswahl ist nicht verfügbar.");
    authorize(p, "model:read", h);
    store.model(p, h.model);
    requireThat(
      h.model === args.model_id && h.expires > Date.now(),
      "STALE_REVISION",
      "Auswahl ist veraltet oder abgelaufen.",
    );
    const target = store.get(
      "SELECT * FROM selection_faces WHERE selection_id=?",
      h.id,
    );
    if (h.revision !== rev.id) {
      requireThat(
        args.rebind && target,
        "STALE_REVISION",
        "Auswahl ist an eine andere Revision gebunden. Flächenauswahl ausdrücklich neu zuordnen.",
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
      "Widersprüchliche Auswahl.",
    );
    fid = h.feature;
    if (selectedFace)
      requireThat(
        owner(selectedFace) === fid,
        "INTEGRITY_FAILURE",
        "Auswahl und Flächenherkunft stimmen nicht überein.",
      );
  } else if (args.face_id) {
    requireThat(
      fid && args.revision,
      "INVALID_SCHEMA",
      "Flächenauswahl benötigt Feature-ID und ausdrückliche Revision.",
    );
    geometryFeature = fid;
    selectedFace = exactFace(store, rev, fid, args.face_id);
    fid = owner(selectedFace);
  }
  requireThat(
    fid,
    "AMBIGUOUS_SELECTION",
    "Eine eindeutige Feature-ID oder Auswahl ist erforderlich.",
  );
  const feature = rev.ir.features.find((f: any) => f.id === fid);
  requireThat(
    feature,
    "AMBIGUOUS_SELECTION",
    "Feature ist in dieser Revision nicht eindeutig verfügbar.",
  );
  return { rev, feature, selectedFace, geometryFeature };
}

export function selectionHandle(
  store: Store,
  p: Principal,
  model: string,
  selection: ReturnType<typeof resolveSelection>,
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
      "INSERT INTO selection_faces VALUES(?,?,?)",
      handle,
      selection.geometryFeature,
      selection.selectedFace.face_id,
    );
  return handle;
}

export function faceSummary(face: any) {
  const { fingerprint, ...summary } = face;
  return { ...summary, unit: "mm", area_unit: "mm2" };
}
