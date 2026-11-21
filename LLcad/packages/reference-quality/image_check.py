"""Fixed-camera CAD rasterization and pixel-only checks for registered references.

The checker reads two PNGs and a public view contract, never the CAD IR, fixture
case names or mutation oracle. This is template comparison, not OCR or an LLM.
"""
import hashlib
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def transform(view, points):
    points = np.asarray(points, dtype=float).copy()
    xmin, xmax, ymin, ymax = view["bounds_mm"]
    if not (xmax > xmin and ymax > ymin and view["width"] > 0 and view["height"] > 0):
        raise ValueError("invalid_camera_bounds")
    if not np.isclose(view["width"] / (xmax-xmin), view["height"] / (ymax-ymin)):
        raise ValueError("nonuniform_camera_scale")
    if view.get("direction", "+Z") != "+Z":
        raise ValueError("unsupported_camera_direction")
    points[:, 0] = (points[:, 0] - xmin) * view["width"] / (xmax - xmin)
    points[:, 1] = (ymax - points[:, 1]) * view["height"] / (ymax - ymin)
    return points


def render(mesh_path, contract, output):
    view = contract["view"]
    width, height = view["width"], view["height"]
    canvas = np.full((height, width, 3), 255, dtype=np.uint8)
    depths = np.full((height, width), -np.inf)
    meshes = json.loads(Path(mesh_path).read_text())["meshes"]
    if not meshes:
        raise ValueError("empty_preview")
    for mesh in meshes:
        points = transform(view, mesh["vertices"])
        if not np.isfinite(points).all():
            raise ValueError("nonfinite_mesh")
        color = contract["colors"].get(mesh["feature_id"], [150, 160, 170])
        for indices in mesh["triangles"]:
            tri = points[indices]
            x0, y0, z0 = tri[0]; x1, y1, z1 = tri[1]; x2, y2, z2 = tri[2]
            xl = max(0, int(np.floor(min(x0, x1, x2))))
            xr = min(width - 1, int(np.ceil(max(x0, x1, x2))))
            yt = max(0, int(np.floor(min(y0, y1, y2))))
            yb = min(height - 1, int(np.ceil(max(y0, y1, y2))))
            if xl > xr or yt > yb:
                continue
            den = (y1-y2)*(x0-x2)+(x2-x1)*(y0-y2)
            if abs(den) < 1e-10:
                continue
            yy, xx = np.mgrid[yt:yb+1, xl:xr+1]
            xx = xx + .5; yy = yy + .5
            a = ((y1-y2)*(xx-x2)+(x2-x1)*(yy-y2))/den
            b = ((y2-y0)*(xx-x2)+(x0-x2)*(yy-y2))/den
            c = 1-a-b
            z = a*z0+b*z1+c*z2
            target = depths[yt:yb+1, xl:xr+1]
            mask = (a >= -1e-8) & (b >= -1e-8) & (c >= -1e-8) & (z > target)
            target[mask] = z[mask]
            canvas[yt:yb+1, xl:xr+1][mask] = color
    Image.fromarray(canvas).save(output)


def region_slice(view, region):
    xmin, xmax, ymin, ymax = region
    corners = transform(view, [[xmin, ymax, 0], [xmax, ymin, 0]])
    x0, y0 = np.floor(corners[0, :2]).astype(int)
    x1, y1 = np.ceil(corners[1, :2]).astype(int)
    if not (0 <= x0 < x1 <= view["width"] and 0 <= y0 < y1 <= view["height"]):
        raise ValueError("invalid_region")
    return np.s_[y0:y1, x0:x1]


def check(reference_path, actual_path, contract, diff_path):
    reference = np.array(Image.open(reference_path).convert("RGB"))
    actual = np.array(Image.open(actual_path).convert("RGB"))
    view = contract["view"]
    if reference.shape != (view["height"], view["width"], 3) or actual.shape != reference.shape:
        raise ValueError("camera_or_resolution_mismatch")
    if not contract.get("checks"):
        raise ValueError("missing_checks")
    issues, measurements = [], []
    for rule in contract["checks"]:
        cut = region_slice(view, rule["region_mm"])
        ref, obs = reference[cut], actual[cut]
        metric = rule["metric"]
        limit = rule["limit"]
        if not np.isfinite(limit) or limit < 0:
            raise ValueError("invalid_limit")
        if metric == "relative_width_error":
            rm = np.any(ref < 240, axis=2)
            om = np.any(obs < 240, axis=2)
            rx = np.where(rm)[1]; ox = np.where(om)[1]
            if len(rx) == 0:
                raise ValueError("empty_reference_region:" + rule["id"])
            rw = int(rx.max()-rx.min()+1)
            ow = int(ox.max()-ox.min()+1) if len(ox) else 0
            value = abs(ow-rw)/rw
        elif metric == "foreground_xor_fraction":
            threshold = rule.get("threshold", 240)
            rm = np.all(ref < threshold, axis=2)
            om = np.all(obs < threshold, axis=2)
            if not rm.any():
                raise ValueError("empty_reference_region:" + rule["id"])
            value = float(np.count_nonzero(rm ^ om) / np.count_nonzero(rm))
        elif metric == "changed_pixel_fraction":
            foreground = np.any(ref < 240, axis=2)
            if not foreground.any():
                raise ValueError("empty_reference")
            value = float(np.count_nonzero(np.any(ref != obs, axis=2)) / np.count_nonzero(foreground))
        elif metric == "unexpected_foreground_fraction":
            foreground = np.any(ref < 240, axis=2)
            if not foreground.any():
                raise ValueError("empty_reference")
            # One pixel for rasterization at edges, not a percentage allowance
            # in which a small extra component could disappear.
            allowed = np.array(Image.fromarray(foreground.astype(np.uint8)*255).filter(ImageFilter.MaxFilter(3))) > 0
            unexpected = np.any(obs < 240, axis=2) & ~allowed
            value = float(np.count_nonzero(unexpected) / np.count_nonzero(foreground))
        else:
            raise ValueError("unknown_metric:" + metric)
        item = dict(id=rule["id"], metric=metric, measured=value, limit=limit)
        measurements.append(item)
        if value > limit:
            issues.append(item)
    mismatch = np.any(reference != actual, axis=2)
    diff = (actual.astype(float)*.5 + 127).astype(np.uint8)
    diff[mismatch] = [220, 30, 60]
    Image.fromarray(diff).save(diff_path)
    return dict(complete=True, issues=issues, measurements=measurements,
                reference_sha256=digest(reference_path), actual_sha256=digest(actual_path),
                contract_sha256=hashlib.sha256(json.dumps(contract, sort_keys=True).encode()).hexdigest(),
                scope="registered_front_view_only; no 3D surface bound; label template, not OCR")


if __name__ == "__main__":
    action = sys.argv[1]
    contract = json.loads(Path(sys.argv[2]).read_text())
    if action == "render":
        render(sys.argv[3], contract, sys.argv[4])
    elif action == "check":
        print(json.dumps(check(sys.argv[3], sys.argv[4], contract, sys.argv[5]), allow_nan=False))
    else:
        raise ValueError("unknown_action")
