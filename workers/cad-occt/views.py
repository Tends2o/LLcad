"""Server-side diagnostic views (Bauplan 8.5, 10.2, 19.3): planar sections and hidden-line
orthographic views rendered as SVG with dimension labels, scale bar and resolution note.

Geometry comes from the native B-Rep: sections via BRepAlgoAPI_Section, projections via
OCCT's HLRBRep hidden-line algorithm. The output is a derived diagnostic picture; measured
numbers in the labels come from the same native evaluation, never from pixels. Labels are
XML-escaped model text and are data, not instructions.
"""
import json
import math
from xml.sax.saxutils import escape
import numpy as np
from geometry import require, bounds, explore, TopoDS, TopAbs_EDGE, gp_Pnt, gp_Dir, gp_Ax2
from OCP.gp import gp_Ax3, gp_Pln
from OCP.BRepAlgoAPI import BRepAlgoAPI_Section
from OCP.BRepAdaptor import BRepAdaptor_Curve
from OCP.GCPnts import GCPnts_UniformDeflection
from OCP.HLRBRep import HLRBRep_Algo, HLRBRep_HLRToShape
from OCP.HLRAlgo import HLRAlgo_Projector

MAX_POLYLINES = 20000
MAX_POINTS = 400000


def _polylines(shape, deflection):
    lines = []
    total = 0
    for edge in explore(shape, TopAbs_EDGE):
        curve = BRepAdaptor_Curve(TopoDS.Edge_s(edge))
        try:
            sampler = GCPnts_UniformDeflection(curve, deflection, curve.FirstParameter(), curve.LastParameter(), True)
        except Exception:
            continue
        if not sampler.IsDone() or sampler.NbPoints() < 2:
            continue
        points = []
        for i in range(1, sampler.NbPoints() + 1):
            p = sampler.Value(i)
            points.append((p.X(), p.Y(), p.Z()))
        total += len(points)
        require(len(lines) < MAX_POLYLINES and total <= MAX_POINTS, 'Diagnoseansicht überschreitet das Linienbudget.', 'BUDGET_EXCEEDED')
        lines.append(points)
    return lines


def _frame(normal):
    n = np.asarray(normal, float)
    require(np.linalg.norm(n) > 1e-12, 'Ansichtsnormale darf nicht null sein.')
    n = n / np.linalg.norm(n)
    seed = np.array([0., 0., 1.]) if abs(n[2]) < 0.9 else np.array([1., 0., 0.])
    u = np.cross(n, seed)
    u /= np.linalg.norm(u)
    v = np.cross(n, u)
    return n, u, v


def _project(points, origin, u, v):
    o = np.asarray(origin, float)
    return [(float(np.dot(np.array(p) - o, u)), float(np.dot(np.array(p) - o, v))) for p in points]


def _svg(document, labels, extents, unit_scale, meta):
    """Assemble the SVG: y axis up in model terms, so the document flips it once."""
    (xmin, ymin, xmax, ymax) = extents
    width = max(xmax - xmin, 1e-6)
    height = max(ymax - ymin, 1e-6)
    margin = 0.12 * max(width, height) + 2.0
    scale = unit_scale
    W = (width + 2 * margin) * scale
    H = (height + 2 * margin) * scale + 48
    def X(x):
        return (x - xmin + margin) * scale
    def Y(y):
        return (ymax - y + margin) * scale
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W:.1f}" height="{H:.1f}" viewBox="0 0 {W:.1f} {H:.1f}" font-family="sans-serif" font-size="{max(9, scale * 0.9):.1f}">',
             '<rect width="100%" height="100%" fill="#fbfbf7"/>']
    for kind, lines in document:
        style = {'visible': 'stroke="#1c2a22" stroke-width="1.2" fill="none"',
                 'hidden': 'stroke="#8a948c" stroke-width="0.8" stroke-dasharray="4 3" fill="none"',
                 'section': 'stroke="#b04a2a" stroke-width="1.6" fill="none"'}[kind]
        for line in lines:
            points = ' '.join(f'{X(x):.2f},{Y(y):.2f}' for x, y in line)
            parts.append(f'<polyline points="{points}" {style}/>')
    # dimension lines for the overall extents, measured from native bounds
    dim_y = Y(ymin) + scale * 1.2 + 10
    parts.append(f'<line x1="{X(xmin):.2f}" y1="{dim_y:.2f}" x2="{X(xmax):.2f}" y2="{dim_y:.2f}" stroke="#2f5d8a" stroke-width="1"/>')
    parts.append(f'<text x="{(X(xmin) + X(xmax)) / 2:.2f}" y="{dim_y + 12:.2f}" text-anchor="middle" fill="#2f5d8a">{width:.4g} mm</text>')
    dim_x = X(xmax) + scale * 1.2 + 10
    parts.append(f'<line x1="{dim_x:.2f}" y1="{Y(ymax):.2f}" x2="{dim_x:.2f}" y2="{Y(ymin):.2f}" stroke="#2f5d8a" stroke-width="1"/>')
    parts.append(f'<text x="{dim_x + 4:.2f}" y="{(Y(ymin) + Y(ymax)) / 2:.2f}" fill="#2f5d8a">{height:.4g} mm</text>')
    for label in labels:
        x, y = label['position']
        parts.append(f'<circle cx="{X(x):.2f}" cy="{Y(y):.2f}" r="2.5" fill="#b04a2a"/>')
        parts.append(f'<text x="{X(x) + 5:.2f}" y="{Y(y) - 4:.2f}" fill="#1c2a22">{escape(label["text"])}</text>')
    bar = 10 ** math.floor(math.log10(max(width, 1e-3)))
    parts.append(f'<line x1="{X(xmin):.2f}" y1="{H - 18:.2f}" x2="{X(xmin) + bar * scale:.2f}" y2="{H - 18:.2f}" stroke="#1c2a22" stroke-width="3"/>')
    parts.append(f'<text x="{X(xmin):.2f}" y="{H - 24:.2f}" fill="#1c2a22">Maßstab {bar:g} mm · {escape(meta)}</text>')
    parts.append('</svg>')
    return '\n'.join(parts)


def section_view(targets, origin, normal, deflection, names):
    """Planar section of every target shape; polylines are the exact native section curves, discretised."""
    n, u, v = _frame(normal)
    plane = gp_Pln(gp_Ax3(gp_Pnt(*map(float, origin)), gp_Dir(*map(float, n))))
    lines = []
    labels = []
    for fid, shape in targets:
        section = BRepAlgoAPI_Section(shape, plane, False)
        section.ComputePCurveOn1(False)
        section.Approximation(False)
        section.Build()
        require(section.IsDone(), 'Schnitt konnte nicht berechnet werden.')
        polylines = [_project(line, origin, u, v) for line in _polylines(section.Shape(), deflection)]
        if polylines:
            pts = np.array([p for line in polylines for p in line])
            labels.append({'feature_id': fid, 'text': names.get(fid, fid), 'position': [float(pts[:, 0].mean()), float(pts[:, 1].mean())]})
        lines.extend(polylines)
    require(lines, 'Die Schnittebene trifft keine Geometrie.', 'GEOMETRY_INVALID')
    pts = np.array([p for line in lines for p in line])
    extents = (float(pts[:, 0].min()), float(pts[:, 1].min()), float(pts[:, 0].max()), float(pts[:, 1].max()))
    scale = 800.0 / max(extents[2] - extents[0], extents[3] - extents[1], 1e-6)
    meta = f'Schnitt n=({n[0]:.3g},{n[1]:.3g},{n[2]:.3g}) · Diskretisierung {deflection:g} mm · kein Flächennachweis'
    svg = _svg([('section', lines)], labels, extents, scale, meta)
    report = {'view': 'section', 'origin_mm': list(map(float, origin)), 'normal': n.tolist(), 'axes': {'u': u.tolist(), 'v': v.tolist()},
              'extents_mm': list(extents), 'polylines': len(lines), 'labels': labels, 'discretization_deflection_mm': deflection,
              'pixels_per_mm': scale, 'method': 'OCCT_BRepAlgoAPI_Section_exact_curves_uniformly_discretised', 'certified_surface_bound': None}
    return svg, report


def orthographic_view(targets, direction, deflection, names, hidden=True):
    """Hidden-line orthographic projection; visible edges solid, hidden edges dashed."""
    n, u, v = _frame(direction)
    origin = np.zeros(3)
    algo = HLRBRep_Algo()
    for _, shape in targets:
        algo.Add(shape)
    projector = HLRAlgo_Projector(gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(*map(float, -n)), gp_Dir(*map(float, u))))
    algo.Projector(projector)
    algo.Update()
    algo.Hide()
    extractor = HLRBRep_HLRToShape(algo)
    visible = []
    for getter in (extractor.VCompound, extractor.OutLineVCompound, extractor.Rg1LineVCompound):
        shape = getter()
        if not shape.IsNull():
            visible.extend(_polylines(shape, deflection))
    hidden_lines = []
    if hidden:
        for getter in (extractor.HCompound, extractor.OutLineHCompound):
            shape = getter()
            if not shape.IsNull():
                hidden_lines.extend(_polylines(shape, deflection))
    # HLR output lives in the projector's 2D coordinate system: x along u, y along v, z depth.
    to2d = lambda lines: [[(p[0], p[1]) for p in line] for line in lines]
    visible2, hidden2 = to2d(visible), to2d(hidden_lines)
    require(visible2 or hidden2, 'Die Projektion enthält keine Kanten.', 'GEOMETRY_INVALID')
    labels = []
    for fid, shape in targets:
        b = bounds(shape)
        centre = np.array([(b[i] + b[i + 3]) / 2 for i in range(3)])
        labels.append({'feature_id': fid, 'text': names.get(fid, fid), 'position': [float(np.dot(centre - origin, u)), float(np.dot(centre - origin, v))]})
    pts = np.array([p for line in visible2 + hidden2 for p in line])
    extents = (float(pts[:, 0].min()), float(pts[:, 1].min()), float(pts[:, 0].max()), float(pts[:, 1].max()))
    scale = 800.0 / max(extents[2] - extents[0], extents[3] - extents[1], 1e-6)
    meta = f'Projektion d=({n[0]:.3g},{n[1]:.3g},{n[2]:.3g}) · verdeckte Kanten gestrichelt · Diskretisierung {deflection:g} mm'
    svg = _svg([('hidden', hidden2), ('visible', visible2)], labels, extents, scale, meta)
    report = {'view': 'orthographic', 'direction': n.tolist(), 'axes': {'u': u.tolist(), 'v': v.tolist()}, 'extents_mm': list(extents),
              'visible_polylines': len(visible2), 'hidden_polylines': len(hidden2), 'labels': labels, 'discretization_deflection_mm': deflection,
              'pixels_per_mm': scale, 'method': 'OCCT_HLRBRep_hidden_line_removal', 'certified_surface_bound': None}
    return svg, report
