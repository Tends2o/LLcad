"""Golden-defect fixtures for gate calibration (CADTests-style mutation testing).

Builds a known-bad copy of an LLcad model through the HTTP tool API of a running
instance and copies the model's reference contract onto it, with feature references
remapped, so cad_gates (and the judge bench) can be checked to FAIL on it:

  mirror_x   the whole model mirrored across its X mid-plane (handedness)
  mirror_y   the same across the Y mid-plane (front/back facing)
  shift      --features F.. moved by --delta dx,dy,dz mm (misplaced keys/logo;
             a shift into the housing gives covered controls, away gives floating)
  scale      the whole model scaled uniformly by --factor about its base centre
             (the 240 mm vs 200 mm proportion case)
  recolour   --part P gets colour --hex #rrggbb in its name (wrong marking colour)

Every output is wrapped by one transform feature (mirror / affine_transform) with the
id prefix `gd-`; the original stays in the IR as its input, so the base geometry comes
from the shared feature cache and only the wrappers are evaluated. For whole-model
variants the contract's explicit landmark points move with the model; the registered
cameras (the photos) stay, which is the point of the fixture.

Usage:
  golden_defects.py --base http://127.0.0.1:4391 --token-file DATA/local-token \
      --model MODEL_ID --variant mirror_x [--name NAME] [--features F1,F2 --delta 0,0,8]
      [--factor 1.2] [--part P --hex #ebb13c] [--no-contract] [--data DATA]
Prints {model_id, revision, variant, remapped} as JSON."""
import argparse, copy, json, re, sqlite3, sys, time, urllib.request

PREFIX = "gd-"


class Api:
    def __init__(self, base, token):
        self.base, self.token = base.rstrip("/"), token

    def call(self, tool, body, timeout=400):
        req = urllib.request.Request(f"{self.base}/api/tools/{tool}", data=json.dumps(body).encode(),
                                     headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"})
        out = json.load(urllib.request.urlopen(req, timeout=timeout))
        if out.get("status") == "failed":
            raise SystemExit(f"{tool} failed: {json.dumps(out.get('errors'))[:600]}")
        return out

    def get(self, path):
        req = urllib.request.Request(self.base + path, headers={"Authorization": "Bearer " + self.token})
        return urllib.request.urlopen(req, timeout=120).read()

    def upload(self, data):
        req = urllib.request.Request(self.base + "/api/uploads", data=data,
                                     headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/octet-stream"})
        return json.load(urllib.request.urlopen(req, timeout=120))["artifact_id"]

    def wait(self, job, limit=900):
        t = time.time()
        while time.time() - t < limit:
            j = self.call("cad_job_get", {"job_id": job, "wait_ms": 60000, "detail": "full"})
            if j["status"] in ("succeeded", "failed", "cancelled"):
                return j
        raise SystemExit("timeout " + job)


def dec(v):
    s = f"{float(v):.6f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def wrap_outputs(ir, make, only=None):
    """Replace each output F (or only those in `only`) by a wrapper gd-F built by make(F)."""
    ir = copy.deepcopy(ir)
    ids = {f["id"] for f in ir["features"]}
    remap = {}
    by_id = {f["id"]: f for f in ir["features"]}
    for fid in list(ir["outputs"]):
        if only is not None and fid not in only:
            continue
        src = by_id[fid]
        if src.get("authoritative_representation", "brep") != "brep":
            continue
        new = PREFIX + fid
        if new in ids:
            raise SystemExit(f"id clash {new}")
        w = {"id": new, "semantic_name": (src.get("semantic_name") or fid)[:180], "kind": None,
             "owner_part": src.get("owner_part", "part-main"), "local_frame": "world",
             "authoritative_representation": "brep", "parameters": {}, "expressions": {}, "parameter_sources": {},
             "construction": None, "depends_on": [fid], "protected_relations": []}
        made = make(fid)
        construction, params = made if isinstance(made, tuple) else (made, {})
        w["construction"] = construction
        w["parameters"] = params
        w["kind"] = construction["operator"]
        ir["features"].append(w)
        remap[fid] = new
    ir["outputs"] = [remap.get(f, f) for f in ir["outputs"]]
    for part in (ir.get("structure") or {}).get("parts", []):
        part["outputs"] = [remap.get(f, f) for f in part.get("outputs", [])]
    return ir, remap


def remap_contract(contract, remap, point_map):
    """Contract setup input for the copy: feature ids through remap, explicit landmark
    points and declared directions through point_map (whole-model variants)."""
    def fids(xs):
        return [remap.get(x, x) for x in xs]

    def vec(v):
        o, e = point_map([0.0, 0.0, 0.0]), point_map(list(v))
        return [e[i] - o[i] for i in range(3)]
    refs = []
    for r in contract["references"]:
        r = copy.deepcopy(r)
        for lm in r.get("landmarks") or []:
            if lm.get("feature_id"):
                lm["feature_id"] = remap.get(lm["feature_id"], lm["feature_id"])
            if lm.get("point_mm") and point_map:
                lm["point_mm"] = [round(v, 5) for v in point_map(lm["point_mm"])]
        refs.append(r)
    gates = copy.deepcopy(contract.get("gates") or {})
    for key in ("controls", "sides", "counts", "markings", "must_be_visible", "stations"):
        for item in gates.get(key) or []:
            if item.get("feature_ids"):
                item["feature_ids"] = fids(item["feature_ids"])
    if point_map:
        for ctl in gates.get("controls") or []:
            if ctl.get("direction"):
                ctl["direction"] = vec(ctl["direction"])
    for ax in gates.get("optical_axes") or []:
        if ax.get("allowed_feature_ids"):
            ax["allowed_feature_ids"] = fids(ax["allowed_feature_ids"])
        if point_map:
            o = ax["origin_mm"]
            e = [o[i] + ax["direction"][i] for i in range(3)]
            o2, e2 = point_map(o), point_map(e)
            ax["origin_mm"] = o2
            ax["direction"] = [e2[i] - o2[i] for i in range(3)]
    dims = [dict(d, **({"feature_id": remap.get(d["feature_id"], d["feature_id"])} if d.get("feature_id") else {}))
            for d in contract.get("dimensions") or []]
    contacts = [dict(c, feature_id=remap.get(c["feature_id"], c["feature_id"]),
                     other_feature_id=remap.get(c["other_feature_id"], c["other_feature_id"]))
                for c in contract.get("contacts") or []]
    reqs = []
    for q in contract.get("requirements") or []:
        q = copy.deepcopy(q)
        if q.get("feature_ids"):
            q["feature_ids"] = fids(q["feature_ids"])
        reqs.append(q)
    out = {"references": refs, "requirements": reqs, "dimensions": dims, "contacts": contacts}
    if gates:
        out["gates"] = gates
    for k in ("models", "maximum_cost_usd", "minimum_confidence", "image_pixel_limit", "silhouette_iou_min",
              "edge_f1_min", "scope", "acceptance", "judge", "accepted_assumptions"):
        if contract.get(k) is not None:
            out[k] = contract[k]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--token-file", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--revision")
    ap.add_argument("--variant", required=True, choices=["mirror_x", "mirror_y", "shift", "scale", "recolour"])
    ap.add_argument("--name")
    ap.add_argument("--features", default="")
    ap.add_argument("--delta", default="0,0,0")
    ap.add_argument("--factor", type=float, default=1.2)
    ap.add_argument("--part")
    ap.add_argument("--hex")
    ap.add_argument("--no-contract", action="store_true")
    ap.add_argument("--data", help="instance data dir: read IR, bounds and the full contract (gates block) read-only")
    a = ap.parse_args()
    api = Api(a.base, open(a.token_file).read().strip())
    src = api.call("cad_get_model", {"model_id": a.model, **({"revision": a.revision} if a.revision else {}), "detail": "brief"})
    rev = a.revision or src.get("revision")
    contract = None
    if a.data:
        # read-only: the stored IR, aggregate bounds and the full reference contract
        db = sqlite3.connect(f"file:{a.data}/models.sqlite?mode=ro", uri=True)
        row = db.execute("SELECT ir, geometry FROM revisions WHERE id=? AND model=?", (rev, a.model)).fetchone()
        ir = json.loads(row[0])
        agg = (json.loads(row[1] or "{}").get("aggregate") or {}).get("bounds")
        c = db.execute("SELECT contract FROM reference_contracts WHERE model=?", (a.model,)).fetchone()
        contract = json.loads(c[0]) if c else None
        db.close()
    else:
        exp = api.call("cad_export", {"model_id": a.model, "revision": rev, "format": "ir",
                                      "idempotency_key": f"gd-export-{rev[-24:]}-ir00"})
        art = exp["artifacts"][0]
        ir = json.loads(api.get(f"/api/artifacts/{art['artifact_id']}"))
        ir = ir.get("ir", ir) if "features" not in ir else ir
        agg = (src.get("measurements") or {}).get("bounds")
        if not a.no_contract:
            rep = api.call("cad_reference_report", {"model_id": a.model, "revision": rev})
            if rep.get("contract_hash"):
                # without --data the gates block is not available (the report omits it)
                contract = {"references": rep["references"], "requirements": rep.get("requirements") or [],
                            "dimensions": rep.get("dimensions") or [], "contacts": rep.get("contacts") or [],
                            **{k: v for k, v in (rep.get("configuration") or {}).items() if v is not None}}
    if not agg:
        agg = [0, 0, 0, 0, 0, 0]
    cx, cy = (agg[0] + agg[3]) / 2, (agg[1] + agg[4]) / 2
    point_map = None
    remap = {}
    if a.variant in ("mirror_x", "mirror_y"):
        axis = 0 if a.variant == "mirror_x" else 1
        c = cx if axis == 0 else cy
        normal = ["1", "0", "0"] if axis == 0 else ["0", "1", "0"]
        point = [dec(c), "0", "0"] if axis == 0 else ["0", dec(c), "0"]
        ir, remap = wrap_outputs(ir, lambda f: {"operator": "mirror", "plane": {"normal": normal, "point": point}})

        def point_map(p, axis=axis, c=c):
            q = list(p)
            q[axis] = 2 * c - q[axis]
            return q
    elif a.variant == "shift":
        d = [float(v) for v in a.delta.split(",")]
        only = set(f for f in a.features.split(",") if f)
        if not only:
            raise SystemExit("--features required for shift")
        # rigid `transform` keeps every surface exact (affine_transform may convert B-splines)
        ir, remap = wrap_outputs(ir, lambda f: ({"operator": "transform"},
                                                {k: {"value": dec(v), "unit": "mm"} for k, v in zip("xyz", d)}), only)
    elif a.variant == "scale":
        s = a.factor
        cz = agg[2]
        t = [cx * (1 - s), cy * (1 - s), cz * (1 - s)]
        ir, remap = wrap_outputs(ir, lambda f: ({"operator": "transform"},
                                                {"scale": {"value": dec(s), "unit": "1"},
                                                 **{k: {"value": dec(v), "unit": "mm"} for k, v in zip("xyz", t)}}))

        def point_map(p, s=s):
            return [cx + (p[0] - cx) * s, cy + (p[1] - cy) * s, cz + (p[2] - cz) * s]
    elif a.variant == "recolour":
        if not a.part or not a.hex or not re.fullmatch(r"#[0-9a-fA-F]{6}", a.hex):
            raise SystemExit("--part and --hex #rrggbb required")
        for part in ir["structure"]["parts"]:
            if part["id"] == a.part:
                name = part["semantic_name"]
                part["semantic_name"] = re.sub(r"#[0-9a-fA-F]{6}", a.hex, name, count=1) if re.search(r"#[0-9a-fA-F]{6}", name) else f"{a.hex} {name}"
    name = a.name or f"golden-defect {a.variant} of {src.get('name', a.model)}"[:120]
    created = api.call("cad_create_model", {"name": name, "purpose": f"Golden defect ({a.variant}) of {a.model} for gate calibration.",
                                            "profile": "precision_cad", "idempotency_key": f"gd-create-{a.model[-20:]}-{a.variant}-{int(time.time())}"})
    M, R = created["model_id"], created["revision"]
    up = api.upload(json.dumps(ir).encode())
    imp = api.call("cad_import", {"model_id": M, "base_revision": R, "artifact_id": up, "format": "ir", "source_unit": "mm",
                                  "idempotency_key": f"gd-import-{M[-24:]}"})
    job = api.wait(imp["job_id"])
    if job["status"] != "succeeded":
        raise SystemExit(f"evaluate {job['status']}: {json.dumps(job.get('error'))[:400]}")
    v = api.call("cad_validate", {"model_id": M, "base_revision": R, "transaction_id": imp["transaction_id"],
                                  "idempotency_key": f"gd-validate-{M[-24:]}", "wait_ms": 100000})
    digest = (v.get("result") or {}).get("digest")
    c = api.call("cad_commit", {"model_id": M, "base_revision": R, "transaction_id": imp["transaction_id"],
                                "validation_digest": digest, "idempotency_key": f"gd-commit-{M[-24:]}"})
    head = c["revision"]
    contract_hash = None
    if contract and not a.no_contract:
        body = remap_contract(contract, remap, point_map)
        s = api.call("cad_reference_setup", {"model_id": M, "base_revision": head, "expected_contract_hash": None,
                                             "idempotency_key": f"gd-setup-{M[-24:]}", **body})
        contract_hash = s["contract_hash"]
    print(json.dumps({"model_id": M, "revision": head, "variant": a.variant, "source_model": a.model, "source_revision": rev,
                      "wrapped_outputs": len(remap), "contract_hash": contract_hash}))


if __name__ == "__main__":
    main()
