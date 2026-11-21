#!/usr/bin/env python3
"""LLcad DSL client: compact Python calls -> canonical LLcad IR patch operations.

Generator script (compute everything in Python, submit few large patches):

    import os, sys; sys.path.insert(0, os.path.expanduser("~/.claude/skills/llcad-modelling/scripts"))
    from llcad import Build, Frame
    b = Build("mvp", part="p01-body")
    body = b.box("p01-core", 30, 20, 100, at=(-15, -10, 0))           # box = min corner
    key  = b.rounded_rect("p02-key-sk", 8, 4, 1, at=(0, 0, 100))        # sketch (native or polygon)
    b.extrude("p02-key", key, 1.2, part="p02-key")
    b.part("p01-body", "Grip", colour="#dddddb", material="Plastic satin", outputs=[body])
    b.part("p02-key", "Key", colour="#141416", material="Plastic matte", outputs=["p02-key"])
    b.finish()   # flags: --model ID [--base REV] [--http] [--out DIR] [--lint] [--no-commit] [--gates] [--offline]

CLI: python3 llcad.py {caps|call|job|head|create|lint|submit|schema|structure|gates} ...  (-h for help)

Default submission is MCP-native: patch files are written and one short line tells the agent
what to pass to cad_apply_patch (patch_ref). --http applies, waits, validates and commits via
/api/tools itself. The token is read from a file and never printed. Output is a <=1 KB summary.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from llcad_core import (MAX_OPS, CURVE_OPS, call, capabilities, compact_job, first_error, hint_for,  # noqa: E402
                        idem_key, attempt_key, record_key, is_unknown_tool, lint_operations, llcad_dir, load_json, load_run, num,
                        save_json, server_config, sha, signed_area_xy, track_transaction, wait, ledger_update,
                        ledger_event, failed_checks, run_gates, probe_feature_field)

ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}$")
CORE_FEATURE_KEYS = {"id", "semantic_name", "kind", "owner_part", "local_frame", "purpose", "authoritative_representation",
                     "parameters", "expressions", "parameter_sources", "construction", "depends_on",
                     "protected_relations", "lineage"}
INT_KEYS = {"index", "degree", "multiplicities", "steps", "version", "iterations", "fill_holes_max_edges"}
ANGLE_NAMES = {"angle", "twist", "draft_angle", "angle_deg", "center_angle_deg", "taper"}
SCALAR_NAMES = {"count", "scale", "sections", "scale_end", "u_min", "u_max", "v_min", "v_max", "iterations", "factor"}
# Solids without inputs: may carry a native feature `placement` (curves never do: extrusion stays +Z local).
LEAF_OPS = {"box", "sphere", "cylinder", "cone", "torus", "text", "svg_emblem", "macro", "strip"}
UNIT = {"length": "mm", "angle": "deg", "scalar": "1"}

# Parameter dimensions of the registered operators (fallback when schemas/operator-registry.json is absent).
BUILTIN_DIMS = {
    "point": {"x": "length", "y": "length", "z": "length"}, "line": {}, "arc": {},
    "plane": {"width": "length", "height": "length", "x": "length", "y": "length", "z": "length"},
    "trim_surface": {"u_min": "scalar", "u_max": "scalar", "v_min": "scalar", "v_max": "scalar"},
    "cap": {}, "sew": {"tolerance": "length"}, "regularize": {},
    "thread": {k: "length" for k in ("root_radius", "pitch", "height", "tooth_depth", "tooth_width", "crest_width", "runout")},
    "offset_solid": {"distance": "length"},
    "box": {"width": "length", "depth": "length", "height": "length", "x": "length", "y": "length", "z": "length"},
    "sphere": {"radius": "length", "x": "length", "y": "length", "z": "length"},
    "cylinder": {"radius": "length", "height": "length", "x": "length", "y": "length", "z": "length"},
    "cone": {"radius": "length", "top_radius": "length", "height": "length", "x": "length", "y": "length", "z": "length"},
    "torus": {"major_radius": "length", "minor_radius": "length", "x": "length", "y": "length", "z": "length"},
    "profile": {}, "strip": {"width": "length", "height": "length"},
    "circle": {"radius": "length", "x": "length", "y": "length", "z": "length"},
    "bezier": {}, "bspline": {}, "nurbs_curve": {}, "nurbs_surface": {},
    "extrude": {"height": "length"}, "revolve": {"angle": "angle"}, "loft": {},
    "sweep": {"twist": "angle", "scale_end": "scalar", "sections": "scalar"},
    "union": {}, "intersection": {}, "difference": {},
    "hole": {"radius": "length", "depth": "length", "x": "length", "y": "length", "z": "length"},
    "pocket": {"width": "length", "length": "length", "depth": "length", "x": "length", "y": "length", "z": "length"},
    "groove": {"radius": "length", "width": "length", "depth": "length", "x": "length", "y": "length", "z": "length"},
    "fillet": {"radius": "length"}, "chamfer": {"distance": "length"}, "shell": {"thickness": "length"},
    "transform": {"x": "length", "y": "length", "z": "length", "angle": "angle", "scale": "scalar"},
    "affine_transform": {}, "rotate": {"angle": "angle"},
    "instance": {"x": "length", "y": "length", "z": "length", "angle": "angle"}, "mirror": {},
    "pattern": {"count": "scalar", "dx": "length", "dy": "length", "dz": "length"},
    "circular_pattern": {"count": "scalar", "angle": "angle"}, "assembly": {},
    "field": {}, "imported": {}, "mesh_repair": {"weld_tolerance": "length"},
    "tessellate": {"deflection": "length"}, "extract_isosurface": {},
}


class DSLError(Exception):
    pass


# ============================================================ vector / frame helpers

def v_add(a, b): return (a[0] + b[0], a[1] + b[1], a[2] + b[2])
def v_sub(a, b): return (a[0] - b[0], a[1] - b[1], a[2] - b[2])
def v_mul(a, s): return (a[0] * s, a[1] * s, a[2] * s)
def dot(a, b): return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]
def cross(a, b): return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])
def norm(a): return math.sqrt(dot(a, a))


def unit(a):
    n = norm(a)
    if n < 1e-12:
        raise DSLError("zero-length direction")
    return (a[0] / n, a[1] / n, a[2] / n)


def rot_axis(axis, deg):
    u = unit(axis)
    t = math.radians(deg)
    c, s = math.cos(t), math.sin(t)
    x, y, z = u
    return ((c + x * x * (1 - c), x * y * (1 - c) - z * s, x * z * (1 - c) + y * s),
            (y * x * (1 - c) + z * s, c + y * y * (1 - c), y * z * (1 - c) - x * s),
            (z * x * (1 - c) - y * s, z * y * (1 - c) + x * s, c + z * z * (1 - c)))


def mat_vec(m, v): return tuple(dot(row, v) for row in m)


class Frame:
    """Right-handed placement: local (x, y, z) -> world origin + x*X + y*Y + z*Z.
    z is the sketch normal / extrusion direction; x is the in-plane 'right'."""

    PLANES = {  # name: (x_axis, z_axis)
        "XY": ((1, 0, 0), (0, 0, 1)), "top": ((1, 0, 0), (0, 0, 1)),
        "bottom": ((1, 0, 0), (0, 0, -1)),
        "XZ": ((1, 0, 0), (0, -1, 0)), "front": ((1, 0, 0), (0, -1, 0)),
        "back": ((-1, 0, 0), (0, 1, 0)),
        "YZ": ((0, 1, 0), (1, 0, 0)), "side": ((0, 1, 0), (1, 0, 0)), "right": ((0, 1, 0), (1, 0, 0)),
        "left": ((0, -1, 0), (-1, 0, 0)),
    }

    def __init__(self, origin=(0, 0, 0), x=(1, 0, 0), z=(0, 0, 1)):
        zz = unit(z)
        xx = v_sub(x, v_mul(zz, dot(x, zz)))
        self.z = zz
        self.x = unit(xx)
        self.y = cross(self.z, self.x)
        self.origin = tuple(float(c) for c in origin)

    @classmethod
    def plane(cls, name, origin=(0, 0, 0)):
        if name not in cls.PLANES:
            raise DSLError("unknown plane %r (use %s)" % (name, ", ".join(cls.PLANES)))
        x, z = cls.PLANES[name]
        return cls(origin, x, z)

    @classmethod
    def axis_angle(cls, origin, axis, deg):
        m = rot_axis(axis, deg)
        return cls(origin, (m[0][0], m[1][0], m[2][0]), (m[0][2], m[1][2], m[2][2]))

    def matrix(self):
        """Rows of the 3x3 matrix whose columns are x, y, z (world = M @ local + origin)."""
        return [[self.x[i], self.y[i], self.z[i]] for i in range(3)]

    def point(self, p):
        p = tuple(p) + (0,) * (3 - len(p))
        return v_add(self.origin, v_add(v_mul(self.x, p[0]), v_add(v_mul(self.y, p[1]), v_mul(self.z, p[2]))))

    def dir(self, v):
        return v_add(v_mul(self.x, v[0]), v_add(v_mul(self.y, v[1]), v_mul(self.z, v[2])))

    def then(self, child: "Frame") -> "Frame":
        """child is expressed in this frame -> world frame of the child."""
        return Frame(self.point(child.origin), self.dir(child.x), self.dir(child.z))

    def moved(self, dx=0, dy=0, dz=0, local=True):
        d = self.dir((dx, dy, dz)) if local else (dx, dy, dz)
        return Frame(v_add(self.origin, d), self.x, self.z)

    def rotated(self, axis, deg, local=True):
        a = self.dir(axis) if local else axis
        m = rot_axis(a, deg)
        return Frame(self.origin, mat_vec(m, self.x), mat_vec(m, self.z))

    def axis_angle_deg(self):
        m = self.matrix()
        tr = m[0][0] + m[1][1] + m[2][2]
        c = max(-1.0, min(1.0, (tr - 1) / 2))
        ang = math.acos(c)
        if ang < 1e-12:
            return (0.0, 0.0, 1.0), 0.0
        if abs(math.pi - ang) < 1e-6:
            # R + I = 2 u u^T: take the column with the largest diagonal entry
            j = max(range(3), key=lambda i: m[i][i])
            col = tuple(m[i][j] + (1 if i == j else 0) for i in range(3))
            return unit(col), 180.0
        s = 2 * math.sin(ang)
        ax = ((m[2][1] - m[1][2]) / s, (m[0][2] - m[2][0]) / s, (m[1][0] - m[0][1]) / s)
        return unit(ax), math.degrees(ang)

    def placement(self):
        return {"origin": [num(c) for c in self.origin], "x_axis": [num(c) for c in self.x], "z_axis": [num(c) for c in self.z]}

    def __repr__(self):
        return "Frame(origin=%r, x=%r, z=%r)" % (self.origin, self.x, self.z)


# ============================================================ 2D point helpers (CCW, equal counts)

def ccw(pts):
    """Return (points, flipped): 2D/3D points ordered counter-clockwise seen from +Z."""
    if signed_area_xy(pts) < 0:
        return list(reversed(pts)), True
    return list(pts), False


def arc_pts(cx, cy, r, a0, a1, n):
    """n+1 points from angle a0 to a1 (degrees) inclusive."""
    return [(cx + r * math.cos(math.radians(a0 + (a1 - a0) * i / n)), cy + r * math.sin(math.radians(a0 + (a1 - a0) * i / n))) for i in range(n + 1)]


def rrect_pts(w, h, r, cx=0.0, cy=0.0, segs=8):
    """Rounded rectangle, CCW, 4*(segs+1) points starting at the right edge middle-top corner arc.
    Same w/h/r/segs -> same point count at every loft station."""
    r = max(0.0, min(r, w / 2 - 1e-6, h / 2 - 1e-6))
    hw, hh = w / 2, h / 2
    pts = []
    for (ox, oy, a0) in ((hw - r, hh - r, 0), (-hw + r, hh - r, 90), (-hw + r, -hh + r, 180), (hw - r, -hh + r, 270)):
        if r <= 1e-9:
            pts.extend([(cx + ox, cy + oy)] * 1)
        else:
            pts.extend(arc_pts(cx + ox, cy + oy, r, a0, a0 + 90, segs))
    out = []
    for p in pts:
        if not out or math.dist(out[-1], p) > 1e-9:
            out.append(p)
    if math.dist(out[0], out[-1]) < 1e-9:
        out.pop()
    return out


def slot_pts(length, width, cx=0.0, cy=0.0, segs=12):
    r = width / 2
    a = max(0.0, length / 2 - r)
    return arc_pts(cx + a, cy, r, -90, 90, segs) + arc_pts(cx - a, cy, r, 90, 270, segs)


def ellipse_pts(rx, ry, cx=0.0, cy=0.0, n=48):
    return [(cx + rx * math.cos(2 * math.pi * i / n), cy + ry * math.sin(2 * math.pi * i / n)) for i in range(n)]


def superellipse_pts(a, b, exponent=4.0, n=64, cx=0.0, cy=0.0):
    out = []
    for i in range(n):
        t = 2 * math.pi * i / n
        c, s = math.cos(t), math.sin(t)
        out.append((cx + a * math.copysign(abs(c) ** (2 / exponent), c), cy + b * math.copysign(abs(s) ** (2 / exponent), s)))
    return out


def _local_pos(at):
    if isinstance(at, Frame):
        raise DSLError("sketches take a local (x, y, z) position; place the extruded SOLID with at=Frame")
    return tuple(at) + (0,) * (3 - len(at))


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    return [{k.strip(): (float(v) if v not in (None, "") and re.match(r"^\s*-?[0-9.eE+-]+\s*$", v) else v) for k, v in r.items()} for r in rows]


# ============================================================ schema-driven coercion

class Schema:
    """Field shapes from the deployed LLcad schemas (LLCAD_ROOT/schemas): keeps the DSL in sync with
    new operators and patch ops without code changes."""

    def __init__(self, root=None):
        base = Path(root or server_config()["root"]) / "schemas"
        self.source = str(base)
        self.registry = ((load_json(base / "operator-registry.json") or {}).get("operators") or {})
        self.patch = load_json(base / "parameter-patch-v1.schema.json") or {}
        self.defs = self.patch.get("$defs", {})
        self.ops, self.constructions = {}, {}
        items = ((self.patch.get("properties") or {}).get("operations") or {}).get("items") or {}
        for v in self.variants(items):
            for name in self.const(v, "op") or []:
                self.ops[name] = v
        self.feature = self.resolve(((self.ops.get("add_feature") or {}).get("properties") or {}).get("feature") or {})
        cons = self.resolve((self.feature.get("properties") or {}).get("construction") or {})
        for v in self.variants(cons):
            for name in self.const(v, "operator") or []:
                self.constructions[name] = self.resolve(v)

    def resolve(self, s):
        seen = 0
        while isinstance(s, dict) and "$ref" in s and seen < 32:
            ref = s["$ref"].split("/")[-1]
            s = {**{k: v for k, v in s.items() if k != "$ref"}, **self.defs.get(ref, {})}
            seen += 1
        return s if isinstance(s, dict) else {}

    def variants(self, s):
        s = self.resolve(s)
        return s.get("oneOf") or s.get("anyOf") or [s]

    def const(self, s, key):
        p = self.resolve((self.resolve(s).get("properties") or {}).get(key) or {})
        if "const" in p:
            return [p["const"]]
        return p.get("enum")

    def known_ops(self):
        return set(self.registry) | set(self.constructions) | (set(BUILTIN_DIMS) if not self.registry else set())

    def dims(self, op):
        reg = self.registry.get(op)
        if reg:
            return {k: d.get("dimension", "length") for k, d in (reg.get("params") or {}).items()}
        return BUILTIN_DIMS.get(op) if not self.registry or op in BUILTIN_DIMS else None

    def required_params(self, op):
        reg = self.registry.get(op) or {}
        return {k for k, d in (reg.get("params") or {}).items() if not d.get("optional")}

    def construction_props(self, op):
        v = self.constructions.get(op)
        if v is None:
            return None if self.constructions else {}
        return {k: p for k, p in (v.get("properties") or {}).items() if k != "operator"}

    def construction_required(self, op):
        v = self.constructions.get(op) or {}
        return set(v.get("required") or []) - {"operator"}

    def feature_props(self):
        return self.feature.get("properties") or {}

    def is_quantity(self, s):
        s = self.resolve(s)
        props = s.get("properties") or {}
        return s.get("type") == "object" and set(props) == {"value", "unit"}

    def pick(self, variants, v):
        if isinstance(v, dict):
            for disc in ("op", "operator", "kind"):
                if disc in v:
                    for s in variants:
                        c = self.const(s, disc)
                        if c and v[disc] in c:
                            return s
            for s in variants:
                rs = self.resolve(s)
                if rs.get("type") == "object":
                    props, req = rs.get("properties") or {}, rs.get("required") or []
                    if all(k in v for k in req) and all(k in props for k in v):
                        return s
        want = {list: "array", tuple: "array", str: "string", bool: "boolean", dict: "object"}.get(type(v))
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            for s in variants:
                if self.is_quantity(s) or self.resolve(s).get("type") == "string":
                    return s
        for s in variants:
            if self.resolve(s).get("type") == want:
                return s
        return variants[0] if variants else {}

    def coerce(self, s, v, key=None):
        s = self.resolve(s)
        if v is None:
            return None
        vs = s.get("oneOf") or s.get("anyOf")
        if vs:
            return self.coerce(self.pick(vs, v), v, key)
        if self.is_quantity(s):
            if isinstance(v, dict):
                return {"value": num(v["value"]), "unit": v.get("unit", default_unit(key))}
            return {"value": num(v), "unit": default_unit(key)}
        t = s.get("type")
        if t == "string":
            return num(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else v
        if t == "array" and isinstance(v, (list, tuple)):
            pre = s.get("prefixItems")
            it = s.get("items")
            return [self.coerce(pre[i] if pre and i < len(pre) else (it if isinstance(it, dict) else {}), x, key) for i, x in enumerate(v)]
        if t == "object" and isinstance(v, dict):
            props = s.get("properties") or {}
            addl = s.get("additionalProperties")
            return {k: self.coerce(props.get(k) or (addl if isinstance(addl, dict) else {}), x, k) for k, x in v.items()}
        if t == "integer" and isinstance(v, float) and v.is_integer():
            return int(v)
        if not s:  # unknown shape: IR numbers are decimal strings except a few integer fields
            return generic_coerce(v, key)
        return list(v) if isinstance(v, tuple) else v


def default_unit(key):
    k = (key or "").lower()
    if k in ANGLE_NAMES or k.endswith("_deg") or "angle" in k:
        return "deg"
    if k in SCALAR_NAMES:
        return "1"
    return "mm"


def generic_coerce(v, key=None):
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, (int, float)):
        return int(v) if key in INT_KEYS else num(v)
    if isinstance(v, (list, tuple)):
        return [generic_coerce(x, key) for x in v]
    if isinstance(v, dict):
        return {k: generic_coerce(x, k) for k, x in v.items()}
    return v


# ============================================================ base model state

class Base:
    """What the patch builds on: revision, structure (+hash), existing features and outputs."""

    def __init__(self, model_id=None, revision=None, structure=None, structure_hash=None, features=None, outputs=None, name=None):
        self.model_id, self.revision, self.structure = model_id, revision, structure
        self.structure_hash = structure_hash or sha(structure)
        self.features = features or {}          # id -> owner_part
        self.outputs = list(outputs or [])
        self.name = name

    @classmethod
    def fresh(cls, model_id=None, revision=None):
        return cls(model_id, revision, None, None, {}, [])

    @classmethod
    def fetch(cls, model_id, revision=None, cfg=None):
        cfg = cfg or server_config()
        feats, outputs, offset, head, info = {}, [], 0, None, None
        while True:
            args = {"model_id": model_id, "offset": offset, "limit": 64}
            if revision:
                args["revision"] = revision
            g = call("cad_get_model", args, cfg=cfg)
            if g.get("status") == "failed":
                raise DSLError("cad_get_model failed: %s" % (first_error(g) or {}).get("message"))
            info = info or g
            head = g.get("revision") or head
            for f in g.get("features") or []:
                feats[f["id"]] = f.get("owner_part", "part-main")
            outputs = g.get("outputs") or outputs
            offset += 64
            if offset >= (g.get("feature_count") or 0) or not g.get("features"):
                break
        st = (info or {}).get("structure") or {}
        structure = None
        if st.get("stored_explicitly"):
            structure = fetch_structure(model_id, head, cfg)
            if sha(structure) != st.get("hash"):
                raise DSLError("rebuilt structure hash does not match the server's; pass the structure explicitly")
        return cls(model_id, head, structure, st.get("hash"), feats, outputs, (info or {}).get("name"))


def fetch_structure(model_id, revision, cfg=None):
    out = {"project": None, "frames": [], "assemblies": [], "parts": []}
    for kind, key in (("project", "project"), ("frame", "frames"), ("assembly", "assemblies"), ("part", "parts")):
        offset = 0
        while True:
            r = call("cad_structure", {"model_id": model_id, "revision": revision, "kind": kind, "offset": offset, "limit": 16}, cfg=cfg)
            if r.get("status") == "failed":
                raise DSLError("cad_structure failed: %s" % (first_error(r) or {}).get("message"))
            for e in r.get("entries") or []:
                d = e.get("definition") or {}
                if kind == "project":
                    out["project"] = d
                else:
                    out[key].append(d)
            offset += 16
            if offset >= (r.get("total_matches") or 0):
                break
    return out


# ============================================================ the builder

class Build:
    def __init__(self, name="patch", part=None, prefix=None, frame=None, root=None, native=True, offline=None):
        self.name = re.sub(r"[^A-Za-z0-9_-]", "-", name) or "patch"
        self.default_part = part or "part-main"
        self.prefix = prefix
        self.default_frame = frame or "world"
        self.features, self.fids, self.raw_ops = [], {}, []
        self.parts, self.frames, self.assemblies = {}, {}, {}
        self.project = None
        self.outputs = []
        self.notes = []
        self.external = set()
        self.schema = Schema(root)
        self.native = native
        self._caps = None
        self.offline = offline if offline is not None else ("--offline" in sys.argv or os.environ.get("LLCAD_OFFLINE") == "1")

    # ------------------------------------------------ capability helpers
    def caps(self):
        if self._caps is None and self.offline:
            self._caps = {"reachable": False, "error": "offline"}
        if self._caps is None:
            try:
                c = capabilities()
            except Exception as e:  # pragma: no cover
                c = {"reachable": False, "error": str(e)}
            self._caps = c
        return self._caps

    def server_has_op(self, op):
        c = self.caps()
        if not c.get("reachable"):
            return op in self.schema.known_ops()
        return op in (c.get("operators") or {})

    def has_op(self, op):
        return op in self.schema.known_ops() and self.server_has_op(op)

    def server_accepts(self, field, sample):
        """Feature-level field (placement, direction, plane ...) accepted by the running server?"""
        if self.offline or not self.caps().get("reachable"):
            return True
        return probe_feature_field(field, sample)

    def note(self, text):
        if text not in self.notes:
            self.notes.append(text)

    # ------------------------------------------------ ids
    def _id(self, fid):
        fid = re.sub(r"[^A-Za-z0-9_-]", "-", str(fid)).strip("-_") or "f"
        if self.prefix and not fid.startswith(self.prefix):
            fid = self.prefix + "-" + fid
        if not ID_RE.match(fid):
            raise DSLError("invalid id %r" % fid)
        return fid

    def _ref(self, d):
        d = str(d)
        if d not in self.fids:
            self.external.add(d)
        return d

    def _unique(self, fid):
        base, n = fid, 2
        while fid in self.fids:
            fid = "%s-%d" % (base, n)
            n += 1
        return fid

    # ------------------------------------------------ generic feature
    def _q(self, v, dim):
        if isinstance(v, dict) and "value" in v:
            return {"value": num(v["value"]), "unit": v.get("unit", UNIT.get(dim, "mm"))}
        return {"value": num(v), "unit": UNIT.get(dim, "mm")}

    def feat(self, op, fid, deps=(), name=None, part=None, frame=None, at=None, purpose=None, rep=None, exprs=None, **kw):
        """Any operator. Keyword fields are routed by the deployed schema: registry parameters become
        {value, unit} quantities, construction fields are coerced (numbers -> decimal strings), known
        feature-level fields (placement, plane, direction ...) are passed through."""
        fid = self._id(fid)
        if fid in self.fids:
            raise DSLError("duplicate feature id %s" % fid)
        dims = self.schema.dims(op)
        cprops = self.schema.construction_props(op)
        fprops = self.schema.feature_props()
        if dims is None and cprops is None:
            c = self.caps()
            if c.get("reachable") and op in (c.get("operators") or {}):
                dims = {p: ("angle" if default_unit(p) == "deg" else "scalar" if default_unit(p) == "1" else "length") for p in c["operators"][op]}
            else:
                raise DSLError("operator %r is not known to this LLcad build (schemas: %s)" % (op, self.schema.source))
        params, cons, extra = {}, {"operator": op}, {}
        for k, v in kw.items():
            if v is None:
                continue
            if dims and k in dims:
                params[k] = self._q(v, dims[k])
            elif cprops is not None and k in cprops:
                cons[k] = self.schema.coerce(cprops[k], v, k)
            elif k in fprops and k not in CORE_FEATURE_KEYS:
                extra[k] = self.schema.coerce(fprops[k], v, k)
            elif cprops == {} and not self.schema.constructions:
                cons[k] = generic_coerce(v, k)
            else:
                raise DSLError("%s: unknown field %r (parameters: %s; construction: %s)"
                               % (op, k, sorted(dims or []), sorted(cprops or [])))
        f = {"id": fid, "semantic_name": (name or fid)[:200], "kind": op, "owner_part": part or self.default_part,
             "local_frame": frame or self.default_frame, "parameters": params, "construction": cons,
             "depends_on": [self._ref(d) for d in deps]}
        if purpose:
            f["purpose"] = {"value": str(purpose)[:1000], "status": "user_declared"}
        if rep:
            f["authoritative_representation"] = rep
        if exprs:
            f["expressions"] = exprs
        f.update(extra)
        if at is not None:
            return self._placed(f, at)
        return self._add(f)

    def _add(self, f):
        self.features.append(f)
        self.fids[f["id"]] = f
        return f["id"]

    def _placed(self, f, at):
        if isinstance(at, (tuple, list)):
            x, y, z = (list(at) + [0, 0, 0])[:3]
            if all(k in (self.schema.dims(f["kind"]) or {}) for k in ("x", "y", "z")):
                for k, v in zip("xyz", (x, y, z)):
                    if v:
                        f["parameters"][k] = self._q(v, "length")
                return self._add(f)
            at = Frame((x, y, z))
        if not isinstance(at, Frame):
            raise DSLError("at= must be (x, y, z) or a Frame")
        if f["kind"] in CURVE_OPS:
            raise DSLError("%s: curves/sketches are built in local coordinates (extrusion is +Z local); "
                           "place the SOLID instead, e.g. b.extrude(id, sketch, h, at=frame)" % f["id"])
        if (self.native and "placement" in self.schema.feature_props() and f["kind"] in LEAF_OPS
                and not f["depends_on"] and self.server_has_op(f["kind"])
                and self.server_accepts("placement", dict(f, placement=at.placement()))):
            f["placement"] = at.placement()
            return self._add(f)
        final_id = f["id"]
        local = dict(f, id=self._unique(final_id + "-lc"), semantic_name=(f["semantic_name"] + " (local)")[:200])
        self._add(local)
        m = at.matrix()
        return self._add({"id": final_id, "semantic_name": f["semantic_name"], "kind": "affine_transform",
                          "owner_part": f["owner_part"], "local_frame": f["local_frame"], "parameters": {},
                          "construction": {"operator": "affine_transform", "matrix": [[num(c) for c in row] for row in m],
                                           "translation": [num(c) for c in at.origin]},
                          "depends_on": [local["id"]]})

    def place(self, fid, src, at: Frame, **k):
        """Rigid placement of an existing solid (one affine feature)."""
        m = at.matrix()
        return self.feat("affine_transform", fid, [src], matrix=m, translation=list(at.origin), **k)

    def raw(self, op: dict):
        self.raw_ops.append(op)
        return op

    # ------------------------------------------------ primitives (box = min corner; cylinder/cone/torus = base centre, +Z)
    def box(self, fid, w, d, h, at=(0, 0, 0), **k):
        return self.feat("box", fid, width=w, depth=d, height=h, at=at, **k)

    def cbox(self, fid, w, d, h, center=(0, 0, 0), **k):
        """Box by its centre (converted to the min corner)."""
        cx, cy, cz = center
        return self.box(fid, w, d, h, at=(cx - w / 2, cy - d / 2, cz - h / 2), **k)

    def cyl(self, fid, r, h, at=(0, 0, 0), **k):
        return self.feat("cylinder", fid, radius=r, height=h, at=at, **k)

    def cone(self, fid, r, r_top, h, at=(0, 0, 0), **k):
        return self.feat("cone", fid, radius=r, top_radius=r_top, height=h, at=at, **k)

    def sphere(self, fid, r, at=(0, 0, 0), **k):
        return self.feat("sphere", fid, radius=r, at=at, **k)

    def torus(self, fid, R, r, at=(0, 0, 0), **k):
        return self.feat("torus", fid, major_radius=R, minor_radius=r, at=at, **k)

    # ------------------------------------------------ curves / sketches
    def profile(self, fid, pts, z=0.0, ccw_fix=True, **k):
        """Closed polygon. 2D points get z; XY-planar profiles are made CCW (recorded as a note)."""
        P = [(p[0], p[1], z) if len(p) == 2 else tuple(p) for p in pts]
        if ccw_fix and len({round(p[2], 9) for p in P}) == 1:
            P, flipped = ccw(P)
            if flipped:
                self.note("%s: clockwise points reversed to CCW" % fid)
        return self.feat("profile", fid, points=[list(p) for p in P], **k)

    def polyline_strip(self, fid, paths, width, height, pads=None, z=0.0, **k):
        P = [[(p[0], p[1], z) if len(p) == 2 else tuple(p) for p in path] for path in paths]
        return self.feat("strip", fid, paths=P, pads=pads, width=width, height=height, **k)

    strip = polyline_strip

    def circle(self, fid, r, at=(0, 0, 0), **k):
        return self.feat("circle", fid, radius=r, at=at, **k)

    def nurbs_curve(self, fid, poles, weights=None, degree=3, knots=None, mults=None, **k):
        n = len(poles)
        if knots is None:
            if (n - 1) % degree:
                raise DSLError("piecewise Bezier poles must be 1 + degree*k")
            spans = (n - 1) // degree
            knots = [i / spans for i in range(spans + 1)]
            mults = [degree + 1] + [degree] * (spans - 1) + [degree + 1]
        return self.feat("nurbs_curve", fid, poles=[list(p) for p in poles], weights=weights or [1] * n,
                         basis={"degree": degree, "knots": [num(round(x, 10)) for x in knots], "multiplicities": mults}, **k)

    def _native_try(self, op, candidates: dict, fixed: dict, **k):
        """Emit a new-style operator if the deployed schema has fields for every required input."""
        if not (self.native and self.has_op(op)):
            return None
        dims = self.schema.dims(op) or {}
        cprops = self.schema.construction_props(op) or {}
        known = set(dims) | set(cprops) | (set(self.schema.feature_props()) - CORE_FEATURE_KEYS)
        kw = {}
        for key, value in fixed.items():
            if key in known and value is not None:
                kw[key] = value
        for names, value in candidates.items():
            if value is None:
                continue
            for n in names.split("|"):
                if n in known:
                    kw[n] = value
                    break
        need = (self.schema.required_params(op) | self.schema.construction_required(op)) - set(kw)
        if need:
            self.note("%s native form needs %s; used fallback" % (op, sorted(need)))
            return None
        return kw

    def sketch(self, fid, preset=None, **kw):
        """Native `sketch` operator (arcs, corner radii, presets). Raises if the server lacks it."""
        if not self.has_op("sketch"):
            raise DSLError("sketch operator not available on this server; use rounded_rect/slot/ellipse helpers (polygon fallback)")
        return self.feat("sketch", fid, preset=preset, **kw)

    def _preset_sketch(self, fid, preset: dict, c, **k):
        """Native `sketch` with a preset object {kind, ...} (the deployed schema); None if unavailable.
        A sketch at z != 0 gets a feature placement (curves are never moved with at=)."""
        if not (self.native and self.has_op("sketch")):
            return None
        cprops = self.schema.construction_props("sketch") or {}
        if "preset" not in cprops:
            return None
        pre = dict(preset)
        if c[0] or c[1]:
            pre["center"] = [c[0], c[1]]
        extra = {}
        if c[2]:
            if "placement" not in self.schema.feature_props():
                return None
            extra["placement"] = {"origin": [0, 0, c[2]], "x_axis": [1, 0, 0], "z_axis": [0, 0, 1]}
        return self.feat("sketch", fid, preset=pre, **extra, **k)

    def rounded_rect(self, fid, w, h, r, at=(0, 0, 0), segs=8, **k):
        """Rounded rectangle sketch centred at `at` (x, y, z); native sketch preset if available."""
        c = _local_pos(at)
        got = self._preset_sketch(fid, {"kind": "rounded_rect", "width": w, "height": h, "radius": r}, c, **k)
        if got is not None:
            return got
        return self.profile(fid, rrect_pts(w, h, r, c[0], c[1], segs), z=c[2], **k)

    def slot(self, fid, length, width, at=(0, 0, 0), segs=12, **k):
        c = _local_pos(at)
        got = self._preset_sketch(fid, {"kind": "slot", "length": length, "width": width}, c, **k)
        if got is not None:
            return got
        return self.profile(fid, slot_pts(length, width, c[0], c[1], segs), z=c[2], **k)

    def ellipse(self, fid, rx, ry, at=(0, 0, 0), n=48, **k):
        c = _local_pos(at)
        got = self._preset_sketch(fid, {"kind": "ellipse", "rx": rx, "ry": ry}, c, **k)
        if got is not None:
            return got
        return self.profile(fid, ellipse_pts(rx, ry, c[0], c[1], n), z=c[2], **k)

    # ------------------------------------------------ solids from curves
    def extrude(self, fid, src, h, direction=None, at=None, **k):
        """Extrude along +Z of the local frame. direction '-'/'both' uses the native field when the
        server has it, else is lowered (the sketch must be one this build created at z=const)."""
        if direction in (None, "+"):
            return self.feat("extrude", fid, [src], height=h, at=at, **k)
        fprops, cprops = self.schema.feature_props(), self.schema.construction_props("extrude") or {}
        sample = {"id": "probe-x", "semantic_name": "probe", "kind": "extrude", "parameters": {"height": {"value": "1", "unit": "mm"}},
                  "construction": {"operator": "extrude", **({"direction": direction} if "direction" in cprops else {})},
                  **({"direction": direction} if "direction" in fprops else {}), "depends_on": ["probe-s"]}
        if self.native and ("direction" in fprops or "direction" in cprops) and self.server_accepts("direction", sample):
            return self.feat("extrude", fid, [src], height=h, direction=direction, at=at, **k)
        shift = -h if direction == "-" else -h / 2
        moved = self.feat("transform", self._unique(fid + "-sk"), [src], z=shift, part=k.get("part"), frame=k.get("frame"))
        self.note("%s: extrude direction %r lowered to a %.4g mm sketch shift" % (fid, direction, shift))
        return self.feat("extrude", fid, [moved], height=h, at=at, **k)

    def revolve(self, fid, src, angle=360, **k):
        """Revolve about the local Z axis through the origin (profile in the XZ half-plane x>=0)."""
        return self.feat("revolve", fid, [src], angle=angle, **k)

    def revolve_rz(self, fid, rz_pts, angle=360, **k):
        """Solid of revolution from (r, z) pairs."""
        prof = self.profile(fid + "-sec", [(r, 0, z) for r, z in rz_pts], ccw_fix=False, part=k.get("part"), frame=k.get("frame"))
        return self.revolve(fid, prof, angle, **k)

    def loft(self, fid, sections, ruled=False, **k):
        return self.feat("loft", fid, list(sections), ruled=ruled or None, **k)

    def sweep(self, fid, profile, spine, **k):
        return self.feat("sweep", fid, [profile, spine], **k)

    # ------------------------------------------------ booleans and edits
    def union(self, fid, *deps, **k):
        return self._tree("union", fid, list(deps), **k)

    def cut(self, fid, base, *tools, **k):
        tools = list(tools)
        if len(tools) > 15:
            grp = self.union(fid + "-tools", *tools, part=k.get("part"), frame=k.get("frame"))
            tools = [grp]
        return self.feat("difference", fid, [base] + tools, **k)

    def common(self, fid, *deps, **k):
        return self.feat("intersection", fid, list(deps), **k)

    def _tree(self, op, fid, deps, **k):
        """Stage >16 inputs as a tree (the operator takes 2..16)."""
        if len(deps) == 1:
            return self.feat("assembly", fid, deps, **k)
        n = 0
        while len(deps) > 16:
            nxt = []
            for i in range(0, len(deps), 16):
                chunk = deps[i:i + 16]
                if len(chunk) == 1:
                    nxt.append(chunk[0])
                    continue
                n += 1
                nxt.append(self.feat(op, "%s-t%d" % (fid, n), chunk, part=k.get("part"), frame=k.get("frame")))
            deps = nxt
        return self.feat(op, fid, deps, **k)

    def group(self, fid, *deps, **k):
        """Compound of several solids (assembly operator, 1..128 inputs)."""
        return self.feat("assembly", fid, list(deps), **k)

    def hole(self, fid, base, r, depth, at, **k):
        """Cuts DOWN from `at` z (top face) by depth."""
        x, y, z = at
        return self.feat("hole", fid, [base], radius=r, depth=depth, x=x, y=y, z=z, **k)

    def pocket(self, fid, base, w, l, depth, at, **k):
        x, y, z = at
        return self.feat("pocket", fid, [base], width=w, length=l, depth=depth, x=x, y=y, z=z, **k)

    def groove(self, fid, base, r, width, depth, at, **k):
        x, y, z = at
        return self.feat("groove", fid, [base], radius=r, width=width, depth=depth, x=x, y=y, z=z, **k)

    def fillet(self, fid, src, r, sel="all", between=None, **k):
        if between:
            return self.feat("fillet", fid, [src], radius=r, edge_selector={"between_features": list(between)}, **k)
        return self.feat("fillet", fid, [src], radius=r, edge_selector=sel, **k)

    def chamfer(self, fid, src, d, sel="all", between=None, **k):
        if between:
            return self.feat("chamfer", fid, [src], distance=d, edge_selector={"between_features": list(between)}, **k)
        return self.feat("chamfer", fid, [src], distance=d, edge_selector=sel, **k)

    def shell(self, fid, src, t, opening="top", **k):
        return self.feat("shell", fid, [src], thickness=t, opening=opening, **k)

    def move(self, fid, src, dx=0, dy=0, dz=0, angle=0, scale=None, **k):
        """transform: uniform scale, then rotation about Z THROUGH THE ORIGIN, then translation."""
        return self.feat("transform", fid, [src], x=dx or None, y=dy or None, z=dz or None, angle=angle or None, scale=scale, **k)

    def instance(self, fid, src, dx=0, dy=0, dz=0, angle=0, **k):
        return self.feat("instance", fid, [src], x=dx or None, y=dy or None, z=dz or None, angle=angle or None, **k)

    def rotate(self, fid, src, axis, angle, origin=(0, 0, 0), **k):
        return self.feat("rotate", fid, [src], axis=list(axis), origin=list(origin), angle=angle, **k)

    def affine(self, fid, src, matrix, translation, **k):
        return self.feat("affine_transform", fid, [src], matrix=matrix, translation=list(translation), **k)

    def mirror(self, fid, src, normal=None, point=(0, 0, 0), **k):
        """Mirror across the YZ plane (x -> -x); normal/point need the native mirror plane, else lowered to affine."""
        if normal is None or tuple(normal) in ((1, 0, 0), (-1, 0, 0)) and tuple(point) == (0, 0, 0):
            return self.feat("mirror", fid, [src], **k)
        cprops = self.schema.construction_props("mirror") or {}
        sample = {"id": "probe-m", "semantic_name": "probe", "kind": "mirror", "parameters": {}, "depends_on": ["probe-s"],
                  "construction": {"operator": "mirror", "plane": {"normal": [num(c) for c in normal], "point": [num(c) for c in point]}}}
        if self.native and "plane" in cprops and self.server_accepts("mirror_plane", sample):
            return self.feat("mirror", fid, [src], plane={"normal": list(normal), "point": list(point)}, **k)
        n = unit(normal)
        m = [[(1 if i == j else 0) - 2 * n[i] * n[j] for j in range(3)] for i in range(3)]
        d = 2 * dot(point, n)
        self.note("%s: mirror plane lowered to affine_transform" % fid)
        return self.feat("affine_transform", fid, [src], matrix=m, translation=[d * n[0], d * n[1], d * n[2]], **k)

    def pattern(self, fid, src, count, dx=0, dy=0, dz=0, **k):
        return self.feat("pattern", fid, [src], count=count, dx=dx or None, dy=dy or None, dz=dz or None, **k)

    def cpattern(self, fid, src, count, angle=360, axis=(0, 0, 1), origin=(0, 0, 0), **k):
        """Circular pattern: `angle` is the total span, samples include 0 and exclude the end."""
        return self.feat("circular_pattern", fid, [src], count=count, angle=angle, axis=list(axis), origin=list(origin), **k)

    # ------------------------------------------------ new operators (native when deployed)
    def draft(self, fid, src, angle, **kw):
        return self.feat("draft", fid, [src], angle=angle, **kw)

    def split_by_plane(self, fid, src, **kw):
        return self.feat("split_by_plane", fid, [src], **kw)

    def macro(self, fid, template, params: dict, version=1, **k):
        """Compiler macro (ring, knurl_ring, lens, indicator_row, control_key, usb_c_receptacle).
        Numeric template parameters go to feature.parameters (units from schemas/operator-docs.json
        'macros'), string options to construction.params."""
        docs = (load_json(Path(self.schema.source) / "operator-docs.json") or {}).get("macros") or {}
        spec = docs.get(template) or {}
        pdims = spec.get("parameters") or {}
        nums, opts = {}, {}
        for key, v in (params or {}).items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                dim = (pdims.get(key) or {}).get("dimension") or ("angle" if default_unit(key) == "deg" else "length")
                nums[key] = {"value": num(v), "unit": UNIT.get(dim, "1")}
            else:
                opts[key] = str(v)
        f_id = self.feat("macro", fid, template=template, version=version, params=opts or None, **k)
        self.fids[f_id]["parameters"].update(nums)
        return f_id

    # ------------------------------------------------ text (native `text` or fontTools outlines)
    def text(self, fid, string, cap_height, depth, font="DejaVu Sans", align="center", tracking=0.0, at=None, arc=None, **k):
        """Raised (depth>0) or cutter (depth<0, from z=depth to 0) lettering. Relief >= 0.05 mm.
        arc={radius, center_angle_deg, side:'outside'|'inside'} for ring text. at=Frame places it."""
        if abs(depth) < 0.05:
            self.note("%s: relief %.3g mm < 0.05 mm (z-fighting risk)" % (fid, depth))
        kw = self._native_try("text", {"string|text": string, "font": font, "cap_height|size": cap_height,
                                       "depth|relief": depth, "tracking": tracking or None, "align": align, "arc": arc}, {})
        if kw is not None:
            return self.feat("text", fid, at=at, **kw, **k)
        self.note("%s: text via fontTools outlines (%s)" % (fid, font))
        return self._text_fallback(fid, string, cap_height, depth, font, align, tracking, at, arc, **k)

    def svg_emblem(self, fid, path, depth, width=None, height=None, at=None, element_filter=None, **k):
        """Logo from the original SVG vectors (never redraw brand marks). element_filter e.g. {'fill': '#1f6fd1'}."""
        native_filter = element_filter
        if isinstance(element_filter, dict) and ("fill" in element_filter or "id" in element_filter):
            native_filter = {k: v for k, v in element_filter.items() if k not in ("fill", "id")}
            if "fill" in element_filter:
                native_filter["fills"] = [element_filter["fill"]] if isinstance(element_filter["fill"], str) else list(element_filter["fill"])
            if "id" in element_filter:
                native_filter["ids"] = [element_filter["id"]] if isinstance(element_filter["id"], str) else list(element_filter["id"])
        kw = self._native_try("svg_emblem", {"path|source": str(path), "width_mm|width": width, "height_mm|height": height,
                                             "depth": depth, "element_filter": native_filter}, {})
        if kw is not None:
            return self.feat("svg_emblem", fid, at=at, **kw, **k)
        self.note("%s: svg via fontTools path parser" % fid)
        return self._svg_fallback(fid, path, depth, width, height, at, element_filter, **k)

    # ------------------------------------------------ macro kit (Python expansion, deterministic)
    def ring(self, fid, r_out, r_in, h, z=0.0, at=None, **k):
        pts = [(r_in, 0, z), (r_out, 0, z), (r_out, 0, z + h), (r_in, 0, z + h)]
        sec = self.profile(fid + "-sec", pts, ccw_fix=False, part=k.get("part"), frame=k.get("frame"))
        return self.feat("revolve", fid, [sec], angle=360, at=at, **k)

    def knurl_ring(self, fid, r_out, r_in, h, count=72, groove_r=None, z=0.0, margin=None, at=None, **k):
        """Grooved focusing/grip ring: ring minus `count` axial grooves (one solid, grooves cannot float)."""
        pk = {"part": k.get("part"), "frame": k.get("frame")}
        ring = self.ring(fid + "-blank", r_out, r_in, h, z=z, **pk)
        g = groove_r or max(0.15, math.pi * r_out / count * 0.35)
        m = min(1.0, h * 0.15) if margin is None else margin
        groove = self.cyl(fid + "-groove", g, h - 2 * m, at=(r_out, 0, z + m), **pk)
        grooves = self.cpattern(fid + "-grooves", groove, count, 360, **pk)
        return self.feat("difference", fid, [ring, grooves], at=at, **k)

    def lens(self, fid, r, t_edge, R, z0=0.0, face="+z", seat=None, at=None, **k):
        """Plano-convex lens with its flat face ON z0 (seated). seat={'wall','lip','height','part'} adds a
        ring whose top face is z0 (returns (lens, seat))."""
        if R <= r:
            raise DSLError("lens: curvature radius must exceed lens radius")
        sag = R - math.sqrt(R * R - r * r)
        pk = {"part": k.get("part"), "frame": k.get("frame")}
        total = t_edge + sag
        if face == "+z":
            cyl = self.cyl(fid + "-blank", r, total + 0.01, at=(0, 0, z0), **pk)
            sph = self.sphere(fid + "-sphere", R, at=(0, 0, z0 + total - R), **pk)
        else:
            cyl = self.cyl(fid + "-blank", r, total + 0.01, at=(0, 0, z0 - total - 0.01), **pk)
            sph = self.sphere(fid + "-sphere", R, at=(0, 0, z0 - total + R), **pk)
        lens = self.feat("intersection", fid, [cyl, sph], at=at, **k)
        if not seat:
            return lens
        wall, lip, sh = seat.get("wall", 0.8), seat.get("lip", 0.6), seat.get("height", 1.5)
        zs = z0 - sh if face == "+z" else z0
        s = self.ring(fid + "-seat", r + wall, r - lip, sh, z=zs, at=at, part=seat.get("part", k.get("part")), frame=k.get("frame"))
        return lens, s

    def indicator_row(self, fid, n, pitch, r, h, at=None, parts=None, **k):
        """n LED/indicator windows in a row along local X, centred; returns a list of ids."""
        ids = []
        for i in range(n):
            x = (i - (n - 1) / 2) * pitch
            part = (parts[i] if parts else k.get("part"))
            fr = (at or Frame()).moved(x, 0, 0)
            ids.append(self.cyl("%s-%d" % (fid, i + 1), r, h, at=fr, part=part, frame=k.get("frame")))
        return ids

    def control_key(self, fid, w, h, r, travel=1.5, proud=0.3, seam=0.15, at=None, part=None, **k):
        """Raised key whose cap stays visible: cap top `proud` (>=0.2 mm) above the surface z=0 of `at`;
        returns {'cap': id, 'recess': cutter id to subtract from the housing}."""
        if proud < 0.2:
            self.note("%s: key cap only %.2f mm proud (<0.2 mm may be covered)" % (fid, proud))
        at = at or Frame()
        fk = {"frame": k.get("frame")}
        cap_sk = self.profile(fid + "-cap-sk", rrect_pts(w, h, r), z=-travel, part=part, **fk)
        cap = self.feat("extrude", fid + "-cap", [cap_sk], height=travel + proud, at=at, part=part, **fk)
        rec_sk = self.profile(fid + "-recess-sk", rrect_pts(w + 2 * seam, h + 2 * seam, r + seam), z=-travel - 0.5,
                              part=k.get("housing_part", part), **fk)
        rec = self.feat("extrude", fid + "-recess", [rec_sk], height=travel + 1.5, at=at, part=k.get("housing_part", part), **fk)
        return {"cap": cap, "recess": rec}

    def usb_c(self, fid, at, depth=7.0, part=None, liner_part=None, **k):
        """USB-C receptacle: opening cutter 8.94 x 3.26 mm (face = +Z of `at`) and the tongue.
        returns {'cutter', 'tongue'}."""
        fk = {"frame": k.get("frame")}
        sk = self.profile(fid + "-open-sk", slot_pts(8.94, 3.26), z=-depth, part=part, **fk)
        cutter = self.feat("extrude", fid + "-cutter", [sk], height=depth + 0.5, at=at, part=part, **fk)
        tongue = self.feat("box", fid + "-tongue", width=6.6, depth=0.7, height=max(0.5, depth - 2.0),
                           x=-3.3, y=-0.35, z=-depth, at=at, part=liner_part or part, **fk)
        return {"cutter": cutter, "tongue": tongue}

    def grip_loft_csv(self, fid, csv_path, z="z", w="w", d="d", r=None, r_col=None, segs=8, cx=None, cy=None, ruled=False, **k):
        """Grip body lofted through CSV stations (z, width, depth[, corner radius]); every station gets the
        same point count, so the loft correspondence is controlled."""
        rows = read_csv(csv_path)
        if len(rows) < 2:
            raise DSLError("grip_loft_csv needs >= 2 stations")
        secs = []
        pk = {"part": k.get("part"), "frame": k.get("frame")}
        for i, row in enumerate(rows):
            ww, dd = float(row[w]), float(row[d])
            rr = float(row[r_col]) if r_col else (r if r is not None else 0.25 * min(ww, dd))
            x0 = float(row[cx]) if cx else 0.0
            y0 = float(row[cy]) if cy else 0.0
            secs.append(self.profile("%s-st%02d" % (fid, i), rrect_pts(ww, dd, max(rr, 0.05), x0, y0, segs), z=float(row[z]), **pk))
        return self.loft(fid, secs, ruled=ruled, **k)

    def tilted_head_frame(self, fid, pivot, tilt_deg, axis=(1, 0, 0), parent="world", name=None):
        """Structure frame for a tilted head: build head features with frame=fid (zero extra features)."""
        self.frame(fid, name or "Head frame %g deg" % tilt_deg, origin=pivot, axis=axis, angle=tilt_deg, parent=parent)
        return Frame.axis_angle(pivot, axis, tilt_deg)

    def led_window(self, fid, w, h, r, depth, at, part=None, housing_part=None, **k):
        """Flush window (its own coloured part) plus the recess cutter for the housing."""
        win_sk = self.profile(fid + "-sk", rrect_pts(w, h, r), z=-depth, part=part, frame=k.get("frame"))
        win = self.feat("extrude", fid, [win_sk], height=depth, at=at, part=part, frame=k.get("frame"))
        cut_sk = self.profile(fid + "-cut-sk", rrect_pts(w, h, r), z=-depth, part=housing_part or part, frame=k.get("frame"))
        cut = self.feat("extrude", fid + "-cut", [cut_sk], height=depth + 0.5, at=at, part=housing_part or part, frame=k.get("frame"))
        return {"window": win, "cutter": cut}

    # ------------------------------------------------ structure
    def project_(self, name, pid="project-main", purpose=None):
        self.project = {"id": pid, "semantic_name": name}
        if purpose:
            self.project["purpose"] = {"value": purpose, "status": "user_declared"}

    def frame(self, fid, name=None, origin=(0, 0, 0), axis=(0, 0, 1), angle=0.0, parent="world", placement: Frame | None = None):
        if placement is not None:
            axis, angle = placement.axis_angle_deg()
            origin = placement.origin
        fid = re.sub(r"[^A-Za-z0-9_-]", "-", str(fid)).strip("-_")
        if not ID_RE.match(fid or "") or fid == "world":
            raise DSLError("invalid frame id %r" % fid)
        f = {"id": fid, "semantic_name": name or fid, "parent": parent,
             "translation": [num(c) for c in origin], "axis": [num(round(c, 12)) for c in axis],
             "angle": {"value": num(round(angle, 10)), "unit": "deg"}}
        self.frames[f["id"]] = f
        return f["id"]

    def assembly(self, aid, name, parent=None, frame="world"):
        a = {"id": aid, "semantic_name": name, "local_frame": frame}
        if parent:
            a["parent_assembly"] = parent
        self.assemblies[aid] = a
        return aid

    def part(self, pid, name, outputs=(), colour=None, material=None, assembly=None, frame="world", rep="brep", purpose=None):
        """Part named 'Material #HEX · Name' (the viewer parses the hex colour)."""
        label = name
        if colour:
            if not re.match(r"^#[0-9a-fA-F]{6}$", colour):
                raise DSLError("colour must be #RRGGBB")
            label = "%s %s · %s" % (material or "Colour", colour.lower(), name)
        p = self.parts.get(pid) or {"id": pid, "semantic_name": label, "local_frame": frame,
                                   "authoritative_representation": rep, "outputs": []}
        p["semantic_name"] = label[:200]
        if assembly:
            p["assembly"] = assembly
        if purpose:
            p["purpose"] = {"value": purpose, "status": "user_declared"}
        for o in outputs:
            if o not in p["outputs"]:
                p["outputs"].append(o)
            if o not in self.outputs:
                self.outputs.append(o)
        self.parts[pid] = p
        return pid

    def output(self, *ids):
        for o in ids:
            if o not in self.outputs:
                self.outputs.append(o)

    # ------------------------------------------------ other patch ops
    def set_parameter(self, fid, parameter, expected, value, unit="mm"):
        return self.raw({"op": "set_parameter", "feature_id": fid, "parameter": parameter,
                         "expected": {"value": num(expected), "unit": unit}, "value": {"value": num(value), "unit": unit}})

    def remove_feature(self, fid, expected_hash):
        return self.raw({"op": "remove_feature", "feature_id": fid, "expected_hash": expected_hash})

    def _schema_op(self, op, **fields):
        s = self.schema.ops.get(op)
        if s is None:
            raise DSLError("patch op %r not available in this build (schemas: %s)" % (op, self.schema.source))
        return self.raw(self.schema.coerce(s, {"op": op, **{k: v for k, v in fields.items() if v is not None}}))

    def set_model_settings(self, **settings):
        s = self.schema.ops.get("set_model_settings")
        props = self.schema.resolve(s or {}).get("properties") or {}
        if "settings" in props:
            return self._schema_op("set_model_settings", settings=settings)
        return self._schema_op("set_model_settings", **settings)

    def set_variable(self, name, value, unit="mm", **extra):
        return self._schema_op("set_variable", name=name, value={"value": num(value), "unit": unit}, **extra)

    def prune_unreachable(self):
        return self._schema_op("prune_unreachable")

    def instantiate_fragment(self, source_model, source_revision, root_ids, id_prefix, frame=None):
        return self._schema_op("instantiate_fragment", source_model=source_model, source_revision=source_revision,
                               root_ids=list(root_ids), id_prefix=id_prefix, frame=frame)

    def set_configurations(self, configurations):
        return self._schema_op("set_configurations", configurations=configurations)

    def set_decals(self, decals):
        return self._schema_op("set_decals", decals=decals)

    # ------------------------------------------------ compile, lint, submit
    def merged_structure(self, base: Base):
        if not (self.parts or self.frames or self.assemblies or self.project):
            return None
        if base.structure:
            S = {"project": dict(base.structure.get("project") or {"id": "project-main", "semantic_name": "Model project"}),
                 "frames": [dict(f) for f in base.structure.get("frames", [])],
                 "assemblies": [dict(a) for a in base.structure.get("assemblies", [])],
                 "parts": [dict(p, outputs=list(p.get("outputs", []))) for p in base.structure.get("parts", [])]}
        else:
            S = {"project": {"id": "project-main", "semantic_name": base.name or "Model project"}, "frames": [], "assemblies": [], "parts": []}
            owned = [f for f, o in base.features.items() if o == "part-main"]
            if owned or any(f["owner_part"] == "part-main" for f in self.features):
                S["parts"].append({"id": "part-main", "semantic_name": "part-main", "local_frame": "world",
                                   "authoritative_representation": "brep", "outputs": [o for o in base.outputs if base.features.get(o) == "part-main"]})
        if self.project:
            S["project"] = dict(self.project)

        def upsert(lst, item, merge_outputs=False):
            for i, cur in enumerate(lst):
                if cur["id"] == item["id"]:
                    if merge_outputs:
                        outs = list(cur.get("outputs", []))
                        outs += [o for o in item.get("outputs", []) if o not in outs]
                        lst[i] = {**cur, **{k: v for k, v in item.items() if k != "outputs"}, "outputs": outs}
                    else:
                        lst[i] = {**cur, **item}
                    return
            lst.append(dict(item))
        for f in self.frames.values():
            upsert(S["frames"], f)
        for a in self.assemblies.values():
            upsert(S["assemblies"], a)
        for p in self.parts.values():
            upsert(S["parts"], p, merge_outputs=True)
        return S

    def compile(self, base: Base):
        """Split into <=64-op patches; each patch is self-consistent (structure outputs filtered to the
        features that exist after it; expected hashes chained locally with the server's canonical hash)."""
        final_S = self.merged_structure(base)
        final_outputs = list(base.outputs) + [o for o in self.outputs if o not in base.outputs]
        feats = [{"op": "add_feature", "feature": f} for f in self.features]
        reserve = (1 if final_S is not None else 0) + 1
        chunks, cur = [], []
        cap0 = MAX_OPS - reserve - len(self.raw_ops)
        if cap0 < 1:
            raise DSLError("too many raw ops for one patch")
        for op in feats:
            cap = cap0 if not chunks else MAX_OPS - reserve
            if len(cur) >= cap:
                chunks.append(cur)
                cur = []
            cur.append(op)
        if cur or not chunks:
            chunks.append(cur)
        out, existing = [], set(base.features)
        prev_S, prev_hash, prev_out = base.structure, base.structure_hash, list(base.outputs)
        for k, chunk in enumerate(chunks):
            existing |= {op["feature"]["id"] for op in chunk}
            ops = []
            if final_S is not None:
                S_k = {**final_S, "parts": [dict(p, outputs=[o for o in p.get("outputs", []) if o in existing]) for p in final_S["parts"]]}
                if S_k != prev_S:
                    ops.append({"op": "set_structure", "expected_hash": prev_hash, "structure": S_k})
                    prev_S, prev_hash = S_k, sha(S_k)
            if k == 0:
                ops += self.raw_ops
            ops += chunk
            outs = [o for o in final_outputs if o in existing]
            if outs and outs != prev_out:
                ops.append({"op": "set_outputs", "outputs": outs[:512]})
                prev_out = outs
            if ops:
                out.append(ops)
        return out

    def lint(self, base: Base, chunks=None, cw_policy="error"):
        chunks = chunks if chunks is not None else self.compile(base)
        errors, warnings = [], []
        for i, ops in enumerate(chunks):
            r = lint_operations(ops, cw_policy=cw_policy)
            errors += ["patch %d: %s" % (i + 1, e) for e in r["errors"]]
            warnings += ["patch %d: %s" % (i + 1, w) for w in r["warnings"]]
        S = self.merged_structure(base)
        parts = {p["id"]: p for p in (S or {}).get("parts", [])} if S else ({p["id"]: p for p in (base.structure or {}).get("parts", [])} if base.structure else {"part-main": None})
        frames = {"world"} | {f["id"] for f in (S or base.structure or {"frames": []}).get("frames", [])}
        known = set(self.fids) | set(base.features)
        owner = dict(base.features)
        owner.update({f["id"]: f["owner_part"] for f in self.features})
        for f in self.features:
            if f["owner_part"] not in parts:
                errors.append("feature %s: owner_part %s is not a part (declare b.part(...))" % (f["id"], f["owner_part"]))
            if f["local_frame"] not in frames:
                errors.append("feature %s: unknown local_frame %s" % (f["id"], f["local_frame"]))
        missing = sorted(d for d in self.external if d not in known)
        if missing and base.model_id:
            errors.append("unknown dependencies %s" % missing[:8])
        outs = list(base.outputs) + [o for o in self.outputs if o not in base.outputs]
        for o in self.outputs:
            f = self.fids.get(o)
            if f and f["kind"] in CURVE_OPS:
                errors.append("output %s is a %s, not a solid" % (o, f["kind"]))
        if S:
            reg = {o for p in S["parts"] for o in p.get("outputs", [])}
            for o in outs:
                if o not in reg:
                    errors.append("model output %s is not in any part's outputs" % o)
            for p in S["parts"]:
                for o in p.get("outputs", []):
                    if owner.get(o) not in (None, p["id"]):
                        errors.append("part %s output %s is owned by %s" % (p["id"], o, owner.get(o)))
                if len(p.get("outputs", [])) > 128:
                    errors.append("part %s has > 128 outputs" % p["id"])
        if S:
            clash = sorted(({(S.get("project") or {}).get("id")} | {e["id"] for key in ("frames", "assemblies", "parts") for e in S.get(key, [])}) & known)
            if clash:
                errors.append("structure ids equal feature ids %s: name parts 'pt-...' and features '<prefix>-...'" % clash[:6])
        if len(self.features) + len(base.features) > 4096:
            errors.append("model would exceed 4096 features")
        return {"errors": errors, "warnings": warnings + self.notes}

    def patch_dir(self, model_id=None, out=None):
        if out:
            d = Path(out)
        else:
            allow = [Path(p) for p in (os.environ.get("LLCAD_PATCH_PATHS") or "/root/projects").split(os.pathsep) if p]
            ld = llcad_dir(create=False)
            cwd = Path.cwd().resolve()
            if ld and any(str(ld).startswith(str(a)) for a in allow):
                d = ld / "patches"
            elif any(str(cwd).startswith(str(a)) for a in allow):
                d = cwd / ".llcad" / "patches"
            else:
                d = allow[0] / ".llcad-patches" / (model_id or "scratch")
        d.mkdir(parents=True, exist_ok=True)
        return d.resolve()

    def save(self, base: Base, out=None, chunks=None):
        chunks = chunks if chunks is not None else self.compile(base)
        d = self.patch_dir(base.model_id, out)
        files = []
        for k, ops in enumerate(chunks):
            p = d / ("%s-%02d.json" % (self.name, k + 1))
            save_json(p, {"operations": ops})
            files.append(str(p))
        save_json(d / ("%s.plan.json" % self.name), {"model_id": base.model_id, "base_revision": base.revision, "files": files,
                                                      "ops": [len(c) for c in chunks], "notes": self.notes[:20]})
        return files

    def finish(self, argv=None):
        """CLI for generator scripts. Prints a <=1 KB JSON summary (and the MCP call line)."""
        ap = argparse.ArgumentParser(prog=Path(sys.argv[0]).name, description="LLcad generator")
        ap.add_argument("--model", default=os.environ.get("LLCAD_MODEL") or (load_run() or {}).get("model_id"))
        ap.add_argument("--base", help="base revision (default: head)")
        ap.add_argument("--http", action="store_true", help="apply+validate+commit via /api/tools")
        ap.add_argument("--out", help="patch directory")
        ap.add_argument("--lint", action="store_true", help="lint only")
        ap.add_argument("--no-commit", action="store_true")
        ap.add_argument("--gates", action="store_true", help="run cad_gates after the last commit")
        ap.add_argument("--offline", action="store_true", help="do not contact the server (fresh-model assumptions)")
        ap.add_argument("--force", action="store_true", help="write/submit despite lint errors")
        ap.add_argument("--cw", default=os.environ.get("LLCAD_LINT_CW", "error"), choices=["error", "warn"])
        a = ap.parse_args(argv)
        if a.offline or not a.model:
            base = Base.fresh(a.model, a.base)
        else:
            base = Base.fetch(a.model, a.base)
        chunks = self.compile(base)
        lint = self.lint(base, chunks, cw_policy=a.cw)
        summary = {"model_id": base.model_id, "base": base.revision, "features": len(self.features), "patches": len(chunks),
                   "ops": [len(c) for c in chunks]}
        if lint["errors"]:
            summary["lint_errors"] = lint["errors"][:8]
        if lint["warnings"]:
            summary["warnings"] = [w[:140] for w in lint["warnings"][:6]]
        if a.lint or (lint["errors"] and not a.force):
            return print_summary(summary, exit_code=1 if lint["errors"] else 0)
        if a.http:
            if not base.model_id:
                summary["error"] = "--http needs --model (or run.json model_id)"
                return print_summary(summary, 1)
            res = submit_chunks(base.model_id, base.revision, chunks, self.name, commit=not a.no_commit, gates=a.gates)
            summary.update(res)
            return print_summary(summary, 0 if res.get("ok") else 1)
        files = self.save(base, a.out, chunks)
        summary["files"] = [Path(f).name for f in files]
        summary["dir"] = str(Path(files[0]).parent) if files else None
        summary["next"] = mcp_instruction(base.model_id, base.revision, files[0], self.name, chunks[0]) if files else None
        if len(files) > 1:
            summary["then"] = "repeat for %s-02.. with base_revision = revision from the previous result" % self.name
        return print_summary(summary, 0)


# ============================================================ submission

def mcp_instruction(model_id, base_revision, path, name, ops):
    caps = capabilities()
    pr = (caps or {}).get("probes") or {}
    if not caps.get("reachable"):
        return "server unreachable; later: python3 %s submit %s --model <id> --http" % (Path(__file__).resolve(), path)
    if not pr.get("apply_patch_ref"):
        return "server has no patch_ref: python3 %s submit %s --model %s --http" % (Path(__file__).resolve(), path, model_id or "<id>")
    args = {"model_id": model_id or "<model_id>", "base_revision": base_revision or "<head>",
            "idempotency_key": idem_key(name, {"m": model_id, "b": base_revision, "ops": ops}), "patch_ref": {"path": path}}
    if pr.get("apply_transaction"):
        args.update(validate=True, commit="if_valid")
    if pr.get("apply_wait_ms"):
        args["wait_ms"] = 100000
    return "cad_apply_patch " + json.dumps(args, ensure_ascii=False)


def transact(model_id, base_rev, ops, key, commit=True, caps=None, patch_ref=None):
    """apply -> wait -> validate -> commit. Uses the one-call transaction when the server has it."""
    caps = caps or capabilities()
    pr = caps.get("probes") or {}
    t0 = time.time()
    args = {"model_id": model_id, "base_revision": base_rev, "idempotency_key": key}
    if patch_ref:
        args["patch_ref"] = patch_ref
    else:
        args["operations"] = ops
    res = {"ok": False, "key": key}
    if commit and pr.get("apply_transaction"):
        args.update(validate=True, commit="if_valid")
        if pr.get("apply_wait_ms"):
            args["wait_ms"] = 100000
        r = call("cad_apply_patch", args, timeout=240)
        # A transaction that outlives wait_ms answers "running": the IDENTICAL call resumes it
        # (evaluate -> validate -> commit). Waiting on the evaluate job alone would skip the commit.
        deadline = t0 + float(os.environ.get("LLCAD_TX_BUDGET_S", "900"))
        while r.get("status") in ("queued", "running") and time.time() < deadline:
            if not pr.get("apply_wait_ms"):
                time.sleep(2)
            r = call("cad_apply_patch", args, timeout=240)
        payload = r.get("result") if isinstance(r.get("result"), dict) and r.get("status") in ("succeeded",) else r
        res.update(transaction_id=r.get("transaction_id") or payload.get("transaction_id"),
                   candidate_revision=payload.get("candidate_revision"), revision=payload.get("revision") or r.get("revision"))
        val = payload.get("validation") or {}
        if val:
            res["validation"] = {k: val[k] for k in ("status", "failed_checks") if val.get(k)}
        if res.get("revision"):
            res["ok"] = True
            res["status"] = "committed"
        else:
            e = first_error(r) or first_error(payload) or r.get("error")
            res["status"] = payload.get("status") or r.get("status")
            if e:
                res["error"] = {"code": e.get("code"), "message": str(e.get("message"))[:240], "hint": hint_for(e)}
        res["s"] = round(time.time() - t0, 1)
        return res
    r = call("cad_apply_patch", args, timeout=120)
    if r.get("status") == "failed" or not r.get("job_id"):
        e = first_error(r) or {}
        res.update(status="rejected", error={"code": e.get("code"), "message": str(e.get("message"))[:240],
                                             "details": str(e.get("details"))[:300] if e.get("details") else None, "hint": hint_for(e)})
        return res
    res["transaction_id"] = r.get("transaction_id")
    res["candidate_revision"] = r.get("candidate_revision")
    track_transaction(None, r, "candidate", base_rev)
    job = wait(r["job_id"], 170)
    cj = compact_job(job)
    res["eval_s"] = cj.get("elapsed_s")
    if job.get("status") != "succeeded":
        res.update(status="evaluate_failed", error=cj.get("error") or {"status": job.get("status")})
        track_transaction(None, r, "failed")
        return res
    m = ((job.get("result") or {}).get("measurements") or {})
    res["bounds"] = [round(x, 3) for x in m.get("bounds", [])] or None
    if not commit:
        res.update(ok=True, status="candidate_ready")
        return res
    v = call("cad_validate", {"model_id": model_id, "base_revision": base_rev, "transaction_id": r["transaction_id"],
                              "idempotency_key": key[:110] + "-validate"})
    if v.get("status") == "failed" or not v.get("job_id"):
        e = first_error(v) or {}
        res.update(status="validate_rejected", error={"code": e.get("code"), "message": str(e.get("message"))[:240]})
        return res
    vj = wait(v["job_id"], 170)
    vr = vj.get("result") or {}
    if vj.get("status") != "succeeded" or vr.get("status") != "checks_passed_within_profile" or not vr.get("digest"):
        res.update(status="validation_failed", failed_checks=failed_checks(vr), error=compact_job(vj).get("error"))
        return res
    c = call("cad_commit", {"model_id": model_id, "base_revision": base_rev, "transaction_id": r["transaction_id"],
                            "validation_digest": vr["digest"], "idempotency_key": key[:110] + "-commit"})
    if not c.get("revision"):
        e = first_error(c) or {}
        res.update(status="commit_rejected", error={"code": e.get("code"), "message": str(e.get("message"))[:240], "hint": hint_for(e)})
        return res
    track_transaction(None, c, "committed")
    res.update(ok=True, status="committed", revision=c["revision"], s=round(time.time() - t0, 1))
    return res


def submit_chunks(model_id, base_rev, chunks, name, commit=True, gates=False, patch_refs=None):
    caps = capabilities()
    if not caps.get("reachable"):
        return {"ok": False, "error": caps.get("error")}
    head, steps = base_rev, []
    prev = None
    for k, ops in enumerate(chunks):
        base_key = idem_key("%s-%02d" % (name, k + 1), {"m": model_id, "b": head, "prev": prev, "ops": ops})
        key = attempt_key(base_key)
        last = k == len(chunks) - 1
        r = transact(model_id, head, ops, key, commit=commit or not last, caps=caps,
                     patch_ref=(patch_refs[k] if patch_refs else None))
        record_key(base_key, bool(r.get("ok")))
        step = {"n": k + 1, "ops": len(ops), "status": r.get("status")}
        for f in ("revision", "transaction_id", "eval_s", "s", "error", "failed_checks", "bounds", "validation"):
            if r.get(f):
                step[f] = r[f]
        steps.append(step)
        if not r.get("ok"):
            return {"ok": False, "head": head, "steps": steps}
        prev = key
        head = r.get("revision") or head
    out = {"ok": True, "head": head, "steps": steps}
    if commit:
        ledger_update(None, lambda led: (led.__setitem__("head", head), led.__setitem__("model_id", model_id), ledger_event(led, "dsl committed %s" % head)))
    if gates and commit:
        out["gates"] = run_gates(model_id, head)
    return out


def print_summary(summary, exit_code=0, limit=1024):
    s = json.dumps(summary, ensure_ascii=False, separators=(",", ":"))
    while len(s) > limit:
        for key in ("warnings", "files", "steps", "lint_errors", "then"):
            v = summary.get(key)
            if isinstance(v, list) and len(v) > 1:
                summary[key] = v[:max(1, len(v) // 2)]
                break
        else:
            s = s[:limit - 3] + "..."
            break
        s = json.dumps(summary, ensure_ascii=False, separators=(",", ":"))
    print(s)
    if exit_code:
        sys.exit(exit_code)
    return summary


# ============================================================ text and SVG fallbacks (fontTools)

FONT_FILES = {
    "dejavu sans": "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "dejavu sans bold": "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "dejavu sans condensed": "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed.ttf",
    "liberation sans": "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "liberation sans bold": "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
}


def font_file(name):
    p = FONT_FILES.get(name.lower())
    if not p or not Path(p).exists():
        try:
            p = subprocess.run(["fc-match", "-f", "%{file}", name], capture_output=True, text=True, timeout=10).stdout.strip()
        except Exception:
            p = ""
    if not p or not Path(p).exists():
        raise DSLError("font %r not found" % name)
    if re.search(r"nimbus|urw", p, re.I):
        raise DSLError("font %r resolves to %s (AGPL Nimbus/URW not allowed): use DejaVu, Liberation or Inter" % (name, p))
    return p


def _pen_class():
    from fontTools.pens.basePen import BasePen

    class Pen(BasePen):
        """Collect closed contours as cubic segments [(c1, c2, p)] with a start point."""

        def __init__(self, glyphset=None, xform=None):
            super().__init__(glyphset)
            self.contours, self.start, self.p, self.segs = [], None, None, []
            self.xf = xform or (lambda p: p)

        def _moveTo(self, p):
            if self.segs:
                self._closePath()
            self.start = self.p = p
            self.segs = []

        def _lineTo(self, q):
            p = self.p
            self.segs.append((tuple(p[i] + (q[i] - p[i]) / 3 for i in range(2)), tuple(p[i] + 2 * (q[i] - p[i]) / 3 for i in range(2)), q))
            self.p = q

        def _curveToOne(self, a, b, q):
            self.segs.append((a, b, q))
            self.p = q

        def _qCurveToOne(self, c, q):
            p = self.p
            self._curveToOne(tuple(p[i] + 2 * (c[i] - p[i]) / 3 for i in range(2)), tuple(q[i] + 2 * (c[i] - q[i]) / 3 for i in range(2)), q)

        def _closePath(self):
            if self.p is not None and self.start is not None and math.dist(self.p, self.start) > 1e-9:
                self._lineTo(self.start)
            if self.segs:
                xf = self.xf
                self.contours.append((xf(self.start), [(xf(a), xf(b), xf(c)) for a, b, c in self.segs]))
            self.p, self.segs = None, []

        def _endPath(self):
            self._closePath()
    return Pen


def _contour_poly(contour):
    start, segs = contour
    return [start] + [s[2] for s in segs]


def _point_in_poly(pt, poly):
    x, y = pt
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / ((y2 - y1) or 1e-30) + x1:
            inside = not inside
    return inside


def _ccw_contour(contour):
    start, segs = contour
    poly = _contour_poly(contour)
    if signed_area_xy([(p[0], p[1]) for p in poly]) >= 0:
        return contour
    pts = [start] + [q for s in segs for q in s]  # p0 c1 c2 p1 c1 c2 p2 ...
    pts = pts[::-1]
    return (pts[0], [(pts[i], pts[i + 1], pts[i + 2]) for i in range(1, len(pts) - 1, 3)])


def _nest(contours):
    """Split contours into (outer, [holes]) by nesting depth (odd depth = hole)."""
    polys = [_contour_poly(c) for c in contours]
    depth = []
    for i, c in enumerate(contours):
        probe = polys[i][0]
        depth.append(sum(1 for j, pj in enumerate(polys) if j != i and _point_in_poly(probe, pj)))
    groups = []
    for i, c in enumerate(contours):
        if depth[i] % 2 == 0:
            holes = [contours[j] for j in range(len(contours)) if depth[j] == depth[i] + 1 and _point_in_poly(polys[j][0], polys[i])]
            groups.append((c, holes))
    return groups


def _text_layout(string, cap_height, font, tracking):
    from fontTools.ttLib import TTFont
    tt = TTFont(font_file(font))
    gs, cmap = tt.getGlyphSet(), tt.getBestCmap()
    capu = getattr(tt["OS/2"], "sCapHeight", 0) or 0.7 * tt["head"].unitsPerEm
    s = cap_height / capu
    Pen = _pen_class()
    glyphs, x = [], 0.0
    for ch in string:
        gname = cmap.get(ord(ch))
        if gname is None:
            x += cap_height * 0.5 + tracking
            continue
        g = gs[gname]
        ox = x
        pen = Pen(gs, lambda p, ox=ox: (p[0] * s + ox, p[1] * s))
        g.draw(pen)
        pen._closePath()
        adv = g.width * s
        glyphs.append((ch, pen.contours, ox, adv))
        x += adv + tracking
    return glyphs, max(0.0, x - tracking)


def _extrude_contours(b: Build, fid, contours, depth, z0, **pk):
    """Outer contours extruded, counters subtracted; returns solid id or None."""
    groups = _nest([_ccw_contour(c) for c in contours])
    solids = []
    for gi, (outer, holes) in enumerate(groups):
        o = _curve_solid(b, "%s-o%d" % (fid, gi), _ccw_contour(outer), depth, z0, **pk)
        if holes:
            cutters = [_curve_solid(b, "%s-o%dh%d" % (fid, gi, hi), _ccw_contour(h), depth + 0.02, z0 - 0.01, **pk) for hi, h in enumerate(holes)]
            o = b.cut("%s-g%d" % (fid, gi), o, *cutters, **pk)
        solids.append(o)
    if not solids:
        return None
    return solids[0] if len(solids) == 1 else b.group(fid, *solids, **pk)


def _curve_solid(b: Build, fid, contour, height, z0, **pk):
    start, segs = contour
    if len(segs) > 85:
        raise DSLError("%s: contour with %d segments > 85 (256 poles); simplify the outline" % (fid, len(segs)))
    poles = [(start[0], start[1], z0)] + [(q[0], q[1], z0) for s in segs for q in s]
    c = b.nurbs_curve(fid + "-c", poles, **pk)
    return b.feat("extrude", fid + "-x", [c], height=height, **pk)


def _text_fallback(self: Build, fid, string, cap_height, depth, font, align, tracking, at, arc, **k):
    pk = {"part": k.get("part"), "frame": k.get("frame")}
    glyphs, width = _text_layout(string, cap_height, font, tracking or 0.0)
    h, z0 = abs(depth), (0.0 if depth > 0 else depth)
    frame = at or Frame()
    ids = []
    if arc:
        R = float(arc["radius"])
        centre = math.radians(float(arc.get("center_angle_deg", 90)))
        outside = arc.get("side", "outside") == "outside"
        span = width / R
        for gi, (ch, contours, ox, adv) in enumerate(glyphs):
            if not contours:
                continue
            mid = ox + adv / 2
            shifted = [((s[0] - mid, s[1]), [((a[0] - mid, a[1]), (bb[0] - mid, bb[1]), (c[0] - mid, c[1])) for a, bb, c in segs]) for s, segs in contours]
            solid = _extrude_contours(self, "%s-%02d" % (fid, gi), shifted, h, z0, **pk)
            if outside:
                th = centre + span / 2 - mid / R
                up, xdir = (math.cos(th), math.sin(th), 0), (math.sin(th), -math.cos(th), 0)
            else:
                th = centre - span / 2 + mid / R
                up, xdir = (-math.cos(th), -math.sin(th), 0), (-math.sin(th), math.cos(th), 0)
            gframe = Frame((R * math.cos(th), R * math.sin(th), 0), xdir, cross(xdir, up))
            ids.append(self.place("%s-p%02d" % (fid, gi), solid, frame.then(gframe), **pk))
        return self.group(fid, *ids, **k)
    shift = {"left": 0.0, "center": -width / 2, "right": -width}.get(align, -width / 2)
    for gi, (ch, contours, ox, adv) in enumerate(glyphs):
        if not contours:
            continue
        moved = [((s[0] + shift, s[1]), [((a[0] + shift, a[1]), (bb[0] + shift, bb[1]), (c[0] + shift, c[1])) for a, bb, c in segs]) for s, segs in contours]
        sid = _extrude_contours(self, "%s-%02d" % (fid, gi), moved, h, z0, **pk)
        if sid:
            ids.append(sid)
    if not ids:
        raise DSLError("text %r produced no glyph outlines" % string)
    if at is not None:
        return self.place(fid, self.group(fid + "-lc", *ids, **pk), at, **k)
    return self.group(fid, *ids, **k)


Build._text_fallback = _text_fallback


def _parse_transform(s):
    """SVG transform attribute -> 2x3 affine (a, b, c, d, e, f)."""
    m = (1, 0, 0, 1, 0, 0)

    def mul(p, q):
        a1, b1, c1, d1, e1, f1 = p
        a2, b2, c2, d2, e2, f2 = q
        return (a1 * a2 + c1 * b2, b1 * a2 + d1 * b2, a1 * c2 + c1 * d2, b1 * c2 + d1 * d2, a1 * e2 + c1 * f2 + e1, b1 * e2 + d1 * f2 + f1)
    for name, args in re.findall(r"(\w+)\s*\(([^)]*)\)", s or ""):
        v = [float(x) for x in re.findall(r"-?[\d.]+(?:e-?\d+)?", args)]
        if name == "matrix" and len(v) == 6:
            q = tuple(v)
        elif name == "translate":
            q = (1, 0, 0, 1, v[0], v[1] if len(v) > 1 else 0)
        elif name == "scale":
            q = (v[0], 0, 0, v[1] if len(v) > 1 else v[0], 0, 0)
        elif name == "rotate":
            t = math.radians(v[0])
            q = (math.cos(t), math.sin(t), -math.sin(t), math.cos(t), 0, 0)
            if len(v) == 3:
                q = mul(mul((1, 0, 0, 1, v[1], v[2]), q), (1, 0, 0, 1, -v[1], -v[2]))
        else:
            continue
        m = mul(m, q)
    return m


def svg_shapes(path, element_filter=None):
    """[(fill, contours)] from an SVG (paths, polygons, polylines, rects, circles, ellipses; CSS class fills)."""
    import xml.etree.ElementTree as ET
    from fontTools.svgLib.path import parse_path
    Pen = _pen_class()
    root = ET.parse(path).getroot()
    css = {}
    for st in root.iter():
        if st.tag.split("}")[-1] == "style" and st.text:
            for sel, body in re.findall(r"([^{}]+)\{([^}]*)\}", st.text):
                fill = re.search(r"fill\s*:\s*([^;]+)", body)
                if fill:
                    for cls in re.findall(r"\.([A-Za-z0-9_-]+)", sel):
                        css[cls] = fill.group(1).strip()
    shapes = []

    def fill_of(el, inherited):
        f = el.attrib.get("fill")
        st = re.search(r"fill\s*:\s*([^;]+)", el.attrib.get("style", ""))
        if st:
            f = st.group(1).strip()
        for cls in (el.attrib.get("class") or "").split():
            f = css.get(cls, f)
        return (f or inherited or "#000000").lower()

    def accept(el, fill):
        if not element_filter:
            return True
        if callable(element_filter):
            return element_filter(el, fill)
        for key, val in element_filter.items():
            if key == "fill" and fill != str(val).lower():
                return False
            if key == "class" and val not in (el.attrib.get("class") or "").split():
                return False
            if key == "id" and el.attrib.get("id") != val:
                return False
        return True

    def walk(el, m, fill):
        tag = el.tag.split("}")[-1]
        m2 = m
        if el.attrib.get("transform"):
            a = _parse_transform(el.attrib["transform"])
            a1, b1, c1, d1, e1, f1 = m
            a2, b2, c2, d2, e2, f2 = a
            m2 = (a1 * a2 + c1 * b2, b1 * a2 + d1 * b2, a1 * c2 + c1 * d2, b1 * c2 + d1 * d2, a1 * e2 + c1 * f2 + e1, b1 * e2 + d1 * f2 + f1)
        fl = fill_of(el, fill)
        if fl == "none":
            fl = fill
        xf = (lambda p, M=m2: (M[0] * p[0] + M[2] * p[1] + M[4], M[1] * p[0] + M[3] * p[1] + M[5]))
        pen = None
        if tag in ("path", "polygon", "polyline", "rect", "circle", "ellipse") and accept(el, fl):
            pen = Pen(None, xf)
            if tag == "path":
                parse_path(el.attrib.get("d", ""), pen)
            elif tag in ("polygon", "polyline"):
                xy = [float(v) for v in re.findall(r"-?\d*\.?\d+(?:e-?\d+)?", el.attrib.get("points", ""))]
                pts = list(zip(xy[::2], xy[1::2]))
                if pts:
                    pen.moveTo(pts[0])
                    for p in pts[1:]:
                        pen.lineTo(p)
                    pen.closePath()
            elif tag == "rect":
                x, y = float(el.attrib.get("x", 0)), float(el.attrib.get("y", 0))
                w, h = float(el.attrib.get("width", 0)), float(el.attrib.get("height", 0))
                pen.moveTo((x, y)); pen.lineTo((x + w, y)); pen.lineTo((x + w, y + h)); pen.lineTo((x, y + h)); pen.closePath()
            else:
                cx, cy = float(el.attrib.get("cx", 0)), float(el.attrib.get("cy", 0))
                rx = float(el.attrib.get("rx", el.attrib.get("r", 0)))
                ry = float(el.attrib.get("ry", el.attrib.get("r", 0)))
                kk = 0.5522847498
                pen.moveTo((cx + rx, cy))
                pen.curveTo((cx + rx, cy + kk * ry), (cx + kk * rx, cy + ry), (cx, cy + ry))
                pen.curveTo((cx - kk * rx, cy + ry), (cx - rx, cy + kk * ry), (cx - rx, cy))
                pen.curveTo((cx - rx, cy - kk * ry), (cx - kk * rx, cy - ry), (cx, cy - ry))
                pen.curveTo((cx + kk * rx, cy - ry), (cx + rx, cy - kk * ry), (cx + rx, cy))
                pen.closePath()
            pen._closePath()
            if pen.contours:
                shapes.append((fl, pen.contours))
        for ch in list(el):
            if ch.tag.split("}")[-1] not in ("defs", "clipPath", "mask", "style", "title", "metadata"):
                walk(ch, m2, fl)
    walk(root, (1, 0, 0, 1, 0, 0), None)
    return shapes


def svg_colours(path):
    """Distinct fills of an SVG (to build one coloured part per fill)."""
    return sorted({f for f, _ in svg_shapes(path)})


def _svg_fallback(self: Build, fid, path, depth, width, height, at, element_filter, **k):
    pk = {"part": k.get("part"), "frame": k.get("frame")}
    shapes = svg_shapes(path, element_filter)
    allpts = [p for _, cs in shapes for c in cs for p in _contour_poly(c)]
    if not allpts:
        raise DSLError("svg %s: no shapes matched %r" % (path, element_filter))
    full = [p for _, cs in svg_shapes(path) for c in cs for p in _contour_poly(c)]  # scale from the whole emblem
    x0, x1 = min(p[0] for p in full), max(p[0] for p in full)
    y0, y1 = min(p[1] for p in full), max(p[1] for p in full)
    s = (width / (x1 - x0)) if width else (height / (y1 - y0)) if height else 1.0
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2

    def tf(p):
        return ((p[0] - cx) * s, -(p[1] - cy) * s)  # SVG y is down
    contours = [(tf(st), [(tf(a), tf(bb), tf(c)) for a, bb, c in segs]) for _, cs in shapes for st, segs in cs]
    h, z0 = abs(depth), (0.0 if depth > 0 else depth)
    sid = _extrude_contours(self, fid + ("-lc" if at is not None else "-s"), contours, h, z0, **pk)
    if at is not None:
        return self.place(fid, sid, at, **k)
    return self.group(fid, sid, **k)


Build._svg_fallback = _svg_fallback


# ============================================================ CLI

def _strip(d):
    if isinstance(d, dict):
        return {k: _strip(v) for k, v in d.items() if v not in (None, [], {}, "") and k not in ("trace_id", "result_schema_version")}
    if isinstance(d, list):
        return [_strip(x) for x in d]
    return d


def cmd(argv=None):
    ap = argparse.ArgumentParser(description="LLcad DSL client (compact output, token never printed)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("caps", help="server capabilities + probed optional features")
    s.add_argument("--refresh", action="store_true")
    s = sub.add_parser("call", help="call any tool; compact output")
    s.add_argument("tool")
    s.add_argument("json", nargs="?", default="{}")
    s.add_argument("--max", type=int, default=1500)
    s = sub.add_parser("job", help="wait for a job and print the compact result")
    s.add_argument("job_id")
    s = sub.add_parser("head", help="compact model summary")
    s.add_argument("model")
    s = sub.add_parser("create", help="create a model (and record it in .llcad/run.json if present)")
    s.add_argument("name")
    s.add_argument("--profile", default="precision_cad")
    s.add_argument("--purpose", default="")
    s = sub.add_parser("lint", help="lint patch files")
    s.add_argument("files", nargs="+")
    s.add_argument("--cw", default=os.environ.get("LLCAD_LINT_CW", "error"), choices=["error", "warn"])
    s = sub.add_parser("submit", help="submit patch files (MCP line by default, --http to apply/validate/commit)")
    s.add_argument("files", nargs="+")
    s.add_argument("--model", required=True)
    s.add_argument("--base")
    s.add_argument("--http", action="store_true")
    s.add_argument("--no-commit", action="store_true")
    s.add_argument("--gates", action="store_true")
    s = sub.add_parser("schema", help="compact field list of an operator or patch op from the deployed schemas")
    s.add_argument("name")
    s = sub.add_parser("structure", help="full structure definitions of a model (compact)")
    s.add_argument("model")
    s.add_argument("--rev")
    s = sub.add_parser("gates", help="deterministic gate pack (fallback: model quality)")
    s.add_argument("model")
    s.add_argument("--rev")
    a = ap.parse_args(argv)

    if a.cmd == "caps":
        c = capabilities(refresh=a.refresh)
        if c.get("reachable"):
            new = [o for o in ("text", "svg_emblem", "sketch", "draft", "split_by_plane", "macro") if o in c["operators"]]
            print(json.dumps({"reachable": True, "operators": len(c["operators"]), "new_operators": new,
                              "probes": c["probes"], "implementation_hash": (c.get("implementation_hash") or "")[:12]}))
        else:
            print(json.dumps(c)[:600])
        return
    if a.cmd == "call":
        r = call(a.tool, json.loads(a.json))
        print(json.dumps(_strip(r), ensure_ascii=False)[:a.max])
        return
    if a.cmd == "job":
        print(json.dumps(compact_job(wait(a.job_id)), ensure_ascii=False))
        return
    if a.cmd == "head":
        g = call("cad_get_model", {"model_id": a.model, "limit": 1})
        if g.get("status") == "failed":
            print(json.dumps(_strip(g))[:800])
            sys.exit(1)
        m = g.get("measurements") or {}
        print(json.dumps({"model_id": a.model, "name": g.get("name"), "head": g.get("head_revision"), "quality": g.get("quality"),
                          "features": g.get("feature_count"), "outputs": len(g.get("outputs") or []),
                          "structure_hash": (g.get("structure") or {}).get("hash"), "bounds": [round(x, 2) for x in m.get("bounds", [])],
                          "solids": m.get("solids"), "reference": (g.get("reference_validation") or {}).get("status"),
                          "build": (g.get("build_compatibility") or {}).get("status")}, ensure_ascii=False))
        return
    if a.cmd == "create":
        r = call("cad_create_model", {"name": a.name, "purpose": a.purpose, "profile": a.profile,
                                      "idempotency_key": idem_key("create", {"n": a.name, "t": int(time.time())})})
        out = {k: r.get(k) for k in ("status", "model_id", "revision")}
        if r.get("status") == "failed":
            out["error"] = first_error(r)
        d = llcad_dir()
        if d and r.get("model_id"):
            run = load_json(d / "run.json", {}) or {}
            if not run.get("model_id"):
                run["model_id"] = r["model_id"]
                save_json(d / "run.json", run)
                out["run_json"] = "model_id recorded"
        print(json.dumps(out, ensure_ascii=False))
        return
    if a.cmd == "lint":
        bad = 0
        for f in a.files:
            data = load_json(f)
            ops = data.get("operations") if isinstance(data, dict) else data
            r = lint_operations(ops or [], cw_policy=a.cw)
            bad += len(r["errors"])
            print(json.dumps({"file": Path(f).name, "ops": len(ops or []), "errors": r["errors"][:10], "warnings": r["warnings"][:6]}, ensure_ascii=False))
        sys.exit(1 if bad else 0)
    if a.cmd == "submit":
        chunks = []
        for f in a.files:
            data = load_json(f)
            chunks.append(data.get("operations") if isinstance(data, dict) else data)
        base_rev = a.base or call("cad_get_model", {"model_id": a.model, "limit": 1}).get("head_revision")
        name = re.sub(r"-\d+$", "", Path(a.files[0]).stem)
        if a.http:
            res = submit_chunks(a.model, base_rev, chunks, name, commit=not a.no_commit, gates=a.gates)
            print_summary(res, 0 if res.get("ok") else 1)
        else:
            print_summary({"next": mcp_instruction(a.model, base_rev, str(Path(a.files[0]).resolve()), name, chunks[0]),
                           "patches": len(chunks)})
        return
    if a.cmd == "schema":
        sc = Schema()
        out = {"schemas": sc.source}
        if a.name in sc.constructions or a.name in sc.registry:
            reg = sc.registry.get(a.name) or {}
            out["parameters"] = {k: ("%s%s" % (d.get("dimension"), "" if d.get("optional") else " (required)")) for k, d in (reg.get("params") or {}).items()}
            out["refs"] = reg.get("refs")
            cp = sc.construction_props(a.name) or {}
            out["construction"] = {k: _brief(sc, v) for k, v in cp.items()}
            out["construction_required"] = sorted(sc.construction_required(a.name))
        if a.name in sc.ops:
            s0 = sc.resolve(sc.ops[a.name])
            out["patch_op"] = {k: _brief(sc, v) for k, v in (s0.get("properties") or {}).items()}
            out["required"] = s0.get("required")
        if a.name == "feature":
            out["feature"] = {k: _brief(sc, v) for k, v in sc.feature_props().items()}
        if len(out) == 1:
            out["error"] = "unknown; operators: %s" % ", ".join(sorted(sc.known_ops()))[:900]
        print(json.dumps(out, ensure_ascii=False)[:3000])
        return
    if a.cmd == "structure":
        g = call("cad_get_model", {"model_id": a.model, "limit": 1, **({"revision": a.rev} if a.rev else {})})
        st = fetch_structure(a.model, a.rev or g.get("revision"))
        print(json.dumps({"hash": (g.get("structure") or {}).get("hash"), "structure": st}, ensure_ascii=False)[:6000])
        return
    if a.cmd == "gates":
        rev = a.rev or call("cad_get_model", {"model_id": a.model, "limit": 1}).get("head_revision")
        print(json.dumps(run_gates(a.model, rev), ensure_ascii=False)[:2000])
        return


def _brief(sc: Schema, s, depth=0):
    s = sc.resolve(s)
    if sc.is_quantity(s):
        return "Quantity"
    vs = s.get("oneOf") or s.get("anyOf")
    if vs:
        return " | ".join(str(_brief(sc, v, depth + 1)) for v in vs[:6])
    t = s.get("type")
    if "const" in s:
        return repr(s["const"])
    if "enum" in s:
        return "enum" + str(s["enum"])[:120]
    if t == "array":
        pre = s.get("prefixItems")
        if pre and all(sc.resolve(p).get("type") == "string" for p in pre):
            return "[%d decimal strings]" % len(pre)
        it = s.get("items")
        return "array<%s>" % (_brief(sc, it, depth + 1) if isinstance(it, dict) and depth < 2 else "…")
    if t == "object" and depth < 2:
        props = s.get("properties") or {}
        return "{" + ", ".join("%s%s" % (k, "" if k in (s.get("required") or []) else "?") for k in list(props)[:12]) + "}"
    if t == "string" and s.get("pattern", "").startswith("^-?(0|"):
        return "decimal string"
    return t or "any"


if __name__ == "__main__":
    cmd()
