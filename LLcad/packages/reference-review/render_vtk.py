"""Offscreen VTK renderer for LLcad revisions under absolute cameras.

Loads a revision's stored preview tessellation (MFPV preview.bin or preview.json,
millimetres, one mesh per output feature) and renders it on the CPU (EGL/llvmpipe)
in three channels for one absolute camera:

  lit     declared part colours under a fixed neutral light rig, no tone mapping;
          for looking at the model, never for colour gates
  albedo  flat declared sRGB colours, unlit, no anti-aliasing: exactly the hex a
          part carries (what a colour gate compares a photo against)
  ids     one exact 24-bit code per mesh (feature) on black, no anti-aliasing:
          visibility, masks, centroids and part attribution

The absolute camera is the contract shared with reference-review/schema.ts and the
viewer's captureReference:

  {mode:"absolute", projection:"perspective"|"orthographic", position_mm, target_mm,
   up, fov_deg (perspective, vertical), mm_per_px (orthographic), width_px,
   height_px, principal_px?:[cx, cy]}

Image coordinates are continuous pixels, origin at the top-left image corner,
u to the right, v down (pixel i covers [i, i+1)); principal_px defaults to the
image centre (W/2, H/2). Perspective: u = cx + f X/Z, v = cy - f Y/Z with
f = (H/2)/tan(fov/2) in camera coordinates (X right, Y up, Z forward); the up vector
is orthogonalised against the viewing direction like VTK and three.js do.

CLI (used by cad_view): render_vtk.py view SPEC.json -> JSON on stdout."""
import json, math, os, struct, sys, time
from pathlib import Path

import numpy as np

os.environ.setdefault("EGL_LOG_LEVEL", "fatal")
import vtk  # noqa: E402
from vtk.util import numpy_support as ns  # noqa: E402

vtk.vtkObject.GlobalWarningDisplayOff()

MAX_EDGE = 2576  # Opus high-resolution tier: long image edge


# --- preview meshes -------------------------------------------------------------
def load_preview(path):
    """[{feature_id, points (n,3) float64 mm, tris (m,3) int64}] from preview.bin/json."""
    data = Path(path).read_bytes()
    meshes = []
    if data[:4] == b"MFPV":
        length, pad = struct.unpack("<II", data[8:16])
        header = json.loads(data[16:16 + length])
        base = 16 + length + pad
        for m in header["meshes"]:
            n, t = int(m["vertex_count"]), int(m["triangle_count"])
            if not n or not t:
                continue
            pos = np.frombuffer(data, "<f4", n * 3, base + int(m["positions_offset"])).reshape(n, 3).astype(float)
            pos = pos + np.asarray(m.get("origin", [0, 0, 0]), float)
            idx = np.frombuffer(data, "<u4", t * 3, base + int(m["indices_offset"])).reshape(t, 3).astype(np.int64)
            meshes.append({"feature_id": m["feature_id"], "points": pos, "tris": idx})
    else:
        header = json.loads(data)
        for m in header["meshes"]:
            v = np.asarray(m["vertices"], float)
            t = np.asarray(m["triangles"], np.int64)
            if len(v) and len(t):
                meshes.append({"feature_id": m["feature_id"], "points": v, "tris": t})
    if not meshes:
        raise ValueError("preview_without_meshes")
    return meshes


def mesh_bounds(meshes):
    lo = np.min([m["points"].min(0) for m in meshes], 0)
    hi = np.max([m["points"].max(0) for m in meshes], 0)
    return np.concatenate([lo, hi])


# --- colour -----------------------------------------------------------------------
def hex_rgb(h, default=(154, 163, 168)):
    try:
        h = str(h).lstrip("#")
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    except Exception:
        return default


def srgb_to_linear(c):
    c = np.asarray(c, float) / 255.0
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def linear_to_srgb(c):
    c = np.clip(np.asarray(c, float), 0, 1)
    return 255.0 * np.where(c <= 0.0031308, 12.92 * c, 1.055 * c ** (1 / 2.4) - 0.055)


_M = np.array([[0.4124, 0.3576, 0.1805], [0.2126, 0.7152, 0.0722], [0.0193, 0.1192, 0.9505]])


def srgb_to_lab(rgb):
    """sRGB 0..255 (..., 3) -> CIE Lab (D65), vectorised."""
    xyz = srgb_to_linear(rgb) @ _M.T / np.array([0.95047, 1.0, 1.08883])
    f = np.where(xyz > 0.008856, np.cbrt(xyz), 7.787 * xyz + 16 / 116)
    return np.stack([116 * f[..., 1] - 16, 500 * (f[..., 0] - f[..., 1]), 200 * (f[..., 1] - f[..., 2])], -1)


def delta_e00(lab1, lab2):
    """CIEDE2000 (Sharma et al.), vectorised over the leading axes."""
    lab1, lab2 = np.asarray(lab1, float), np.asarray(lab2, float)
    L1, a1, b1 = lab1[..., 0], lab1[..., 1], lab1[..., 2]
    L2, a2, b2 = lab2[..., 0], lab2[..., 1], lab2[..., 2]
    C1, C2 = np.hypot(a1, b1), np.hypot(a2, b2)
    Cb = (C1 + C2) / 2
    G = 0.5 * (1 - np.sqrt(Cb ** 7 / (Cb ** 7 + 25.0 ** 7)))
    a1p, a2p = (1 + G) * a1, (1 + G) * a2
    C1p, C2p = np.hypot(a1p, b1), np.hypot(a2p, b2)
    h1p = np.degrees(np.arctan2(b1, a1p)) % 360
    h2p = np.degrees(np.arctan2(b2, a2p)) % 360
    dL, dC = L2 - L1, C2p - C1p
    dh = h2p - h1p
    dh = np.where(dh > 180, dh - 360, np.where(dh < -180, dh + 360, dh))
    dh = np.where(C1p * C2p == 0, 0.0, dh)
    dH = 2 * np.sqrt(C1p * C2p) * np.sin(np.radians(dh / 2))
    Lb, Cbp = (L1 + L2) / 2, (C1p + C2p) / 2
    hs = h1p + h2p
    hb = np.where(C1p * C2p == 0, hs, np.where(np.abs(h1p - h2p) <= 180, hs / 2, np.where(hs < 360, (hs + 360) / 2, (hs - 360) / 2)))
    T = (1 - 0.17 * np.cos(np.radians(hb - 30)) + 0.24 * np.cos(np.radians(2 * hb))
         + 0.32 * np.cos(np.radians(3 * hb + 6)) - 0.20 * np.cos(np.radians(4 * hb - 63)))
    dth = 30 * np.exp(-(((hb - 275) / 25) ** 2))
    RC = 2 * np.sqrt(Cbp ** 7 / (Cbp ** 7 + 25.0 ** 7))
    SL = 1 + 0.015 * (Lb - 50) ** 2 / np.sqrt(20 + (Lb - 50) ** 2)
    SC, SH = 1 + 0.045 * Cbp, 1 + 0.015 * Cbp * T
    RT = -np.sin(np.radians(2 * dth)) * RC
    return np.sqrt((dL / SL) ** 2 + (dC / SC) ** 2 + (dH / SH) ** 2 + RT * (dC / SC) * (dH / SH))


def hue_family(lab):
    """Coarse colour family: 'neutral-dark|mid|light' or a 30-degree hue sector."""
    L, a, b = float(lab[0]), float(lab[1]), float(lab[2])
    C = math.hypot(a, b)
    if C < 8:
        return "neutral-" + ("dark" if L < 35 else "light" if L > 70 else "mid")
    names = ["red", "orange", "yellow", "yellow-green", "green", "green-cyan", "cyan", "cyan-blue",
             "blue", "violet", "purple", "magenta"]
    return names[int(((math.degrees(math.atan2(b, a)) % 360) + 15) // 30) % 12]


# --- cameras ----------------------------------------------------------------------
def _unit(v):
    v = np.asarray(v, float)
    n = np.linalg.norm(v)
    if n < 1e-12:
        raise ValueError("zero_vector")
    return v / n


class Camera:
    """An absolute camera (see module docstring); `scale` renders it at another size."""

    def __init__(self, c):
        self.c = dict(c)
        self.W, self.H = int(c["width_px"]), int(c["height_px"])
        self.pos = np.asarray(c["position_mm"], float)
        self.tgt = np.asarray(c["target_mm"], float)
        self.ortho = c.get("projection") == "orthographic"
        f = _unit(self.tgt - self.pos)
        up = np.asarray(c.get("up") or [0, 0, 1], float)
        r = np.cross(f, up)
        if np.linalg.norm(r) < 1e-9:  # up parallel to the view: any perpendicular
            r = np.cross(f, [1.0, 0, 0]) if abs(f[0]) < 0.9 else np.cross(f, [0, 1.0, 0])
        r = _unit(r)
        self.R = np.stack([r, np.cross(r, f), f])  # rows: right, up, forward
        pp = c.get("principal_px") or [self.W / 2, self.H / 2]
        self.cx, self.cy = float(pp[0]), float(pp[1])
        self.depth = float(np.linalg.norm(self.tgt - self.pos))
        if self.ortho:
            self.mm_per_px = float(c["mm_per_px"])
            self.k = 1.0 / self.mm_per_px
        else:
            self.fov = float(c["fov_deg"])
            self.k = (self.H / 2) / math.tan(math.radians(self.fov) / 2)
            self.mm_per_px = self.depth / self.k  # at the target distance

    def to_dict(self):
        d = {"mode": "absolute", "projection": "orthographic" if self.ortho else "perspective",
             "position_mm": [round(float(v), 4) for v in self.pos],
             "target_mm": [round(float(v), 4) for v in self.tgt],
             "up": [round(float(v), 6) for v in self.R[1]],
             "width_px": self.W, "height_px": self.H}
        if self.ortho:
            d["mm_per_px"] = round(self.mm_per_px, 7)
        else:
            d["fov_deg"] = round(self.fov, 5)
        if abs(self.cx - self.W / 2) > 1e-6 or abs(self.cy - self.H / 2) > 1e-6:
            d["principal_px"] = [round(self.cx, 3), round(self.cy, 3)]
        return d

    def project(self, P):
        """World points (...,3) -> (uv (...,2), depth (...)) in this camera's pixels."""
        q = (np.asarray(P, float) - self.pos) @ self.R.T
        if self.ortho:
            return np.stack([self.cx + q[..., 0] * self.k, self.cy - q[..., 1] * self.k], -1), q[..., 2]
        z = np.where(np.abs(q[..., 2]) < 1e-9, 1e-9, q[..., 2])
        return np.stack([self.cx + q[..., 0] / z * self.k, self.cy - q[..., 1] / z * self.k], -1), q[..., 2]

    def mm_per_px_at(self, P):
        if self.ortho:
            return self.mm_per_px
        return float(max(1e-6, (np.asarray(P, float) - self.pos) @ self.R[2])) / self.k

    def scaled(self, s):
        """The same frame rendered with s times as many pixels per edge."""
        d = self.to_dict()
        d["width_px"] = max(8, int(round(self.W * s)))
        d["height_px"] = max(8, int(round(self.H * s)))
        sx, sy = d["width_px"] / self.W, d["height_px"] / self.H
        if self.ortho:
            d["mm_per_px"] = self.mm_per_px / sy
        if "principal_px" in d:
            d["principal_px"] = [self.cx * sx, self.cy * sy]
        return Camera(d)


def relative_to_absolute(rel, bounds, W, H):
    """The old bbox-relative camera, resolved exactly as the viewer frames it."""
    lo, hi = np.asarray(bounds[:3], float), np.asarray(bounds[3:], float)
    span = float(max(hi - lo))
    target = np.asarray(rel.get("target") or (lo + hi) / 2, float)
    direction = _unit(rel["direction"])
    df = float(rel.get("distance_factor", 2.5))
    fov = float(rel.get("fov", 28))
    cam = {"mode": "absolute", "projection": rel.get("projection", "perspective"),
           "position_mm": (target + direction * span * df).tolist(), "target_mm": target.tolist(),
           "up": list(rel.get("up") or [0, 0, 1]), "width_px": int(W), "height_px": int(H)}
    if cam["projection"] == "orthographic":
        cam["mm_per_px"] = 2 * span * df * math.tan(math.radians(fov) / 2) / H
    else:
        cam["fov_deg"] = fov
    return cam


PRESETS = {
    "front": ([0, 1, 0], [0, 0, 1]), "back": ([0, -1, 0], [0, 0, 1]),
    "left": ([1, 0, 0], [0, 0, 1]), "right": ([-1, 0, 0], [0, 0, 1]),
    "top": ([0, 0, 1], [0, 1, 0]), "bottom": ([0, 0, -1], [0, 1, 0]),
    "iso": ([1, 1, 0.8], [0, 0, 1]), "iso_back": ([-1, -1, 0.8], [0, 0, 1]),
}


def preset_camera(name, meshes_or_bounds, W, H, projection="perspective", fov=28.0, margin=1.08):
    """A framed absolute camera for a named direction (direction = target -> camera):
    the tessellation's projected extent fills the frame with `margin`."""
    direction, up = PRESETS[name]
    return framed_camera(direction, up, meshes_or_bounds, W, H, projection, fov, margin)


def framed_camera(direction, up, meshes_or_bounds, W, H, projection="perspective", fov=28.0, margin=1.08):
    if isinstance(meshes_or_bounds, list):
        pts = np.concatenate([m["points"][:: max(1, len(m["points"]) // 4000)] for m in meshes_or_bounds])
        b = mesh_bounds(meshes_or_bounds)
    else:
        b = np.asarray(meshes_or_bounds, float)
        lo, hi = b[:3], b[3:]
        pts = np.array([[x, y, z] for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])])
    center = (b[:3] + b[3:]) / 2
    span = float(max(b[3:] - b[:3])) or 1.0
    d = _unit(direction)
    probe = Camera({"mode": "absolute", "projection": "orthographic", "position_mm": (center + d * span * 4).tolist(),
                    "target_mm": center.tolist(), "up": list(up), "mm_per_px": 1.0, "width_px": W, "height_px": H})
    q = (pts - probe.pos) @ probe.R.T
    ext_x, ext_y = np.ptp(q[:, 0]), np.ptp(q[:, 1])
    mid = probe.R[0] * (q[:, 0].min() + ext_x / 2) + probe.R[1] * (q[:, 1].min() + ext_y / 2)
    target = probe.pos + mid + probe.R[2] * float(((center - probe.pos) @ probe.R[2]))
    half_h = margin * max(ext_y / 2, ext_x / 2 * H / W, 1e-3)
    cam = {"mode": "absolute", "projection": projection, "target_mm": target.tolist(), "up": probe.R[1].tolist(),
           "width_px": int(W), "height_px": int(H)}
    if projection == "orthographic":
        cam["position_mm"] = (target + d * span * 3).tolist()
        cam["mm_per_px"] = 2 * half_h / H
    else:
        depth_half = float(np.max(np.abs(q[:, 2] - q[:, 2].mean()))) if len(q) else 0
        dist = half_h / math.tan(math.radians(fov) / 2) + depth_half
        cam["position_mm"] = (target + d * dist).tolist()
        cam["fov_deg"] = fov
    return cam


# --- scene ------------------------------------------------------------------------
def _polydata(points, tris):
    pts = vtk.vtkPoints()
    pts.SetData(ns.numpy_to_vtk(np.ascontiguousarray(points, dtype=np.float64), deep=True))
    ca = vtk.vtkCellArray()
    offsets = np.arange(0, 3 * len(tris) + 1, 3, dtype=np.int64)
    ca.SetData(ns.numpy_to_vtkIdTypeArray(offsets, deep=True),
               ns.numpy_to_vtkIdTypeArray(np.ascontiguousarray(tris, dtype=np.int64).ravel(), deep=True))
    pd = vtk.vtkPolyData()
    pd.SetPoints(pts)
    pd.SetPolys(ca)
    return pd


def id_code(i):
    """24-bit colour of mesh index i (0-based); background is 0."""
    v = i + 1
    return (v & 255, (v >> 8) & 255, (v >> 16) & 255)


class Scene:
    """Meshes of one revision with per-feature appearance, rendered offscreen.

    info: {feature_id: {part, colour (#hex), glass (bool), emissive (bool)}}.
    mirror: None or (axis 0..2, plane coordinate) for the mirror image of the model."""

    def __init__(self, meshes, info=None, mirror=None, lit_background=(250, 250, 250)):
        self.meshes = meshes
        self.info = info or {}
        self.mirror = mirror
        self.lit_background = lit_background
        self.actors, self.polys = [], []
        for m in meshes:
            P = m["points"]
            if mirror is not None:
                P = P.copy()
                ax, c = mirror
                P[:, ax] = 2 * c - P[:, ax]
            pd = _polydata(P, m["tris"][:, ::-1] if mirror is not None else m["tris"])
            self.polys.append(pd)
        self.points = [p if mirror is None else self._mirrored(p) for p in (m["points"] for m in meshes)]
        self.bounds = np.concatenate([np.min([p.min(0) for p in self.points], 0), np.max([p.max(0) for p in self.points], 0)])
        self.ren = vtk.vtkRenderer()
        self.win = vtk.vtkRenderWindow()
        self.win.SetOffScreenRendering(1)
        self.win.SetMultiSamples(0)
        self.win.AddRenderer(self.ren)
        self.win.SetSize(64, 64)
        self.ren.SetUseFXAA(False)
        self._normals = {}
        for i, pd in enumerate(self.polys):
            mapper = vtk.vtkPolyDataMapper()
            mapper.SetInputData(pd)
            mapper.ScalarVisibilityOff()
            actor = vtk.vtkActor()
            actor.SetMapper(mapper)
            self.ren.AddActor(actor)
            self.actors.append(actor)
        self._grab = vtk.vtkWindowToImageFilter()
        self._grab.SetInput(self.win)
        self._grab.ReadFrontBufferOff()
        self._grab.SetInputBufferTypeToRGB()
        self._channel = None
        self._lights = None

    def _mirrored(self, P):
        P = P.copy()
        ax, c = self.mirror
        P[:, ax] = 2 * c - P[:, ax]
        return P

    def feature(self, i):
        return self.meshes[i]["feature_id"]

    def colour(self, i):
        return hex_rgb(self.info.get(self.feature(i), {}).get("colour"))

    def set_camera(self, cam):
        W, H = cam.W, cam.H
        if tuple(self.win.GetSize()) != (W, H):
            self.win.SetSize(W, H)
        vc = self.ren.GetActiveCamera()
        vc.SetPosition(*cam.pos)
        vc.SetFocalPoint(*cam.tgt)
        vc.SetViewUp(*cam.R[1])
        if cam.ortho:
            vc.ParallelProjectionOn()
            vc.SetParallelScale(cam.mm_per_px * H / 2)
        else:
            vc.ParallelProjectionOff()
            vc.SetViewAngle(cam.fov)
        vc.SetWindowCenter(1 - 2 * cam.cx / W, 2 * cam.cy / H - 1)
        # Clipping from the scene bounds along the view axis (not ResetCameraClippingRange,
        # which is fine too but slower on many actors).
        lo, hi = self.bounds[:3], self.bounds[3:]
        corners = np.array([[x, y, z] for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])])
        depth = (corners - cam.pos) @ cam.R[2]
        span = float(np.linalg.norm(hi - lo)) or 1.0
        near = max(float(depth.min()) - 0.02 * span, 1e-3 if not cam.ortho else -1e9)
        far = float(depth.max()) + 0.02 * span
        if cam.ortho:
            near = float(depth.min()) - 0.05 * span
        vc.SetClippingRange(max(near, 1e-4) if not cam.ortho else near, max(far, near + 1e-3))
        self.cam = cam

    def _style(self, channel, classes=None, only=None):
        key = (channel, None if classes is None else id(classes), None if only is None else tuple(sorted(only)))
        if self._channel == key:
            return
        self._channel = key
        lit = channel == "lit"
        self.ren.RemoveAllLights()
        if lit:
            kit = vtk.vtkLightKit()
            kit.SetKeyLightIntensity(0.85)
            kit.SetKeyToHeadRatio(1.6)
            kit.AddLightsToRenderer(self.ren)
            self.ren.SetBackground(*(v / 255 for v in self.lit_background))
            self.ren.SetUseDepthPeeling(1)
            self.ren.SetMaximumNumberOfPeels(6)
            self.win.SetAlphaBitPlanes(1)
        else:
            self.ren.SetBackground(0, 0, 0)
            self.ren.SetUseDepthPeeling(0)
        for i, a in enumerate(self.actors):
            a.SetVisibility(only is None or i in only)
            p = a.GetProperty()
            inf = self.info.get(self.feature(i), {})
            if channel == "ids":
                col = [v / 255 for v in id_code(i)]
            elif channel == "classes":
                col = [v / 255 for v in id_code(int(classes[i]))] if classes is not None else [1, 1, 1]
            else:
                col = [v / 255 for v in self.colour(i)]
            p.SetColor(*col)
            if lit:
                p.LightingOn()
                p.SetInterpolationToPhong()
                p.SetAmbient(0.18 if not inf.get("emissive") else 0.7)
                p.SetDiffuse(0.8 if not inf.get("emissive") else 0.35)
                p.SetSpecular(0.12)
                p.SetSpecularPower(25)
                p.SetOpacity(0.35 if inf.get("glass") else 1.0)
                p.BackfaceCullingOff()
                if i not in self._normals:
                    nf = vtk.vtkPolyDataNormals()
                    nf.SetInputData(self.polys[i])
                    nf.SetFeatureAngle(35)
                    nf.SplittingOn()
                    nf.ConsistencyOn()
                    nf.Update()
                    self._normals[i] = nf.GetOutput()
                a.GetMapper().SetInputData(self._normals[i])
            else:
                p.LightingOff()
                p.SetAmbient(1.0)
                p.SetDiffuse(0.0)
                p.SetSpecular(0.0)
                p.SetOpacity(1.0)
                a.GetMapper().SetInputData(self.polys[i])

    def render(self, channel="albedo", classes=None, only=None):
        """(H, W, 3) uint8 image of the current camera."""
        self._style(channel, classes, only)
        self.win.Render()
        self._grab.Modified()
        self._grab.Update()
        img = self._grab.GetOutput()
        W, H = self.win.GetSize()
        arr = ns.vtk_to_numpy(img.GetPointData().GetScalars()).reshape(H, W, -1)[::-1]
        return np.ascontiguousarray(arr[:, :, :3])

    def ids(self, only=None):
        """(H, W) int32: 0 background, i+1 = mesh index i."""
        a = self.render("ids", only=only).astype(np.int32)
        return a[:, :, 0] | (a[:, :, 1] << 8) | (a[:, :, 2] << 16)

    def classes(self, classes):
        """(H, W) int32 class codes (class c renders as code c; background 0 -> -1)."""
        a = self.render("classes", classes=classes).astype(np.int32)
        v = a[:, :, 0] | (a[:, :, 1] << 8) | (a[:, :, 2] << 16)
        return v - 1

    def mask(self):
        return self.ids() > 0


# --- ids -> part table ------------------------------------------------------------
def part_table(ids, scene, parts, lit=None, erode=2, photo=None):
    """Per part (by `parts` {part_id: {name, colour, glass, emissive}}) in one view:
    visible_px, bbox_px [x0,y0,x1,y1], centroid_px, mean_rgb of the lit render and its
    delta_e00 to the declared colour; photo_rgb/photo_delta_e00 when a registered
    photo (H, W, 3) is given (median of the eroded part mask)."""
    from scipy import ndimage
    feature_part = {scene.feature(i): scene.info.get(scene.feature(i), {}).get("part") for i in range(len(scene.meshes))}
    code_part = np.array([None] + [feature_part[scene.feature(i)] for i in range(len(scene.meshes))], dtype=object)
    rows = []
    pid_mask = {}
    present = np.unique(ids)
    for code in present:
        if code == 0 or code >= len(code_part):
            continue
        pid = code_part[code] or scene.feature(code - 1)
        pid_mask.setdefault(pid, []).append(code)
    for pid, codes in pid_mask.items():
        m = np.isin(ids, codes)
        n = int(m.sum())
        if not n:
            continue
        ys, xs = np.nonzero(m)
        info = parts.get(pid, {})
        row = {"part_id": pid, "visible_px": n,
               "bbox_px": [int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1],
               "centroid_px": [round(float(xs.mean()) + 0.5, 1), round(float(ys.mean()) + 0.5, 1)]}
        core = ndimage.binary_erosion(m, iterations=erode) if n > 60 else m
        if not core.any():
            core = m
        declared_lab = srgb_to_lab(np.array(hex_rgb(info.get("colour")), float))
        if lit is not None:
            mean = lit[core].reshape(-1, 3).mean(0)
            row["mean_rgb"] = [int(round(v)) for v in mean]
            row["delta_e00"] = round(float(delta_e00(declared_lab, srgb_to_lab(mean))), 2)
        if photo is not None:
            px = photo[core].reshape(-1, 3).astype(float)
            med = np.median(px, 0)
            row["photo_rgb"] = [int(round(v)) for v in med]
            row["photo_delta_e00"] = round(float(delta_e00(declared_lab, srgb_to_lab(med))), 2)
        rows.append(row)
    rows.sort(key=lambda r: -r["visible_px"])
    return rows


def id_visual(ids, scene):
    """A false-colour picture of the ids buffer: one distinct colour per part."""
    import colorsys
    parts = {}
    out = np.full(ids.shape + (3,), 255, np.uint8)
    for code in np.unique(ids):
        if code == 0 or code > len(scene.meshes):
            continue
        pid = scene.info.get(scene.feature(code - 1), {}).get("part") or scene.feature(code - 1)
        if pid not in parts:
            k = len(parts)
            r, g, b = colorsys.hsv_to_rgb((k * 0.61803398875) % 1, 0.55 + 0.35 * (k % 2), 0.95 - 0.25 * ((k // 2) % 2))
            parts[pid] = (int(r * 255), int(g * 255), int(b * 255))
        out[ids == code] = parts[pid]
    return out, parts


# --- CLI: cad_view ----------------------------------------------------------------
def _save(arr, path):
    from PIL import Image
    Image.fromarray(arr).save(path, optimize=False, compress_level=3)


def view_main(spec):
    """spec: {preview, info {fid: {part, colour, glass, emissive}}, parts {pid: {...}},
    views [{id, camera (absolute) | preset, projection?, photo?}], width, height, channels,
    out_dir} -> {views: [{id, camera, images {channel: path}, parts [...], seconds}]}"""
    from PIL import Image
    t0 = time.perf_counter()
    meshes = load_preview(spec["preview"])
    scene = Scene(meshes, spec.get("info"))
    load_s = time.perf_counter() - t0
    out_dir = Path(spec["out_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    W, H = int(spec.get("width", 1024)), int(spec.get("height", 1024))
    channels = spec.get("channels") or ["lit"]
    parts = spec.get("parts") or {}
    results = []
    for v in spec["views"]:
        t = time.perf_counter()
        if v.get("camera"):
            cam = Camera(v["camera"])
        else:
            cam = Camera(preset_camera(v.get("preset", "front"), meshes, W, H, v.get("projection", "perspective"),
                                       float(v.get("fov_deg", 28))))
        longest = max(cam.W, cam.H)
        if longest > MAX_EDGE:
            cam = cam.scaled(MAX_EDGE / longest)
        scene.set_camera(cam)
        ids = scene.ids()
        lit = scene.render("lit")
        photo = None
        if v.get("photo"):
            with Image.open(v["photo"]) as im:
                im = im.convert("RGB")
                if im.size != (cam.W, cam.H):
                    im = im.resize((cam.W, cam.H), Image.Resampling.LANCZOS)
                photo = np.asarray(im)
        images = {}
        for ch in channels:
            path = out_dir / f"{v['id']}-{ch}.png"
            if ch == "lit":
                _save(lit, path)
            elif ch == "albedo":
                alb = scene.render("albedo")
                alb[ids == 0] = 255
                _save(alb, path)
            elif ch == "ids":
                _save(id_visual(ids, scene)[0], path)
            images[ch] = str(path)
        rows = part_table(ids, scene, parts, lit=lit, photo=photo)
        results.append({"id": v["id"], "camera": cam.to_dict(), "images": images, "parts": rows,
                        "mm_per_px": round(cam.mm_per_px, 5), "seconds": round(time.perf_counter() - t, 3)})
    return {"views": results, "load_s": round(load_s, 3), "triangles": int(sum(len(m["tris"]) for m in meshes)),
            "seconds": round(time.perf_counter() - t0, 3)}


if __name__ == "__main__":
    action, path = sys.argv[1], sys.argv[2]
    spec = json.loads(Path(path).read_text())
    if action == "view":
        out = view_main(spec)
    else:
        raise SystemExit("unknown action " + action)
    sys.stdout.write(json.dumps(out))
