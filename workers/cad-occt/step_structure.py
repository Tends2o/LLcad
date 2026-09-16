"""STEP assembly structure through XCAF (Bauplan 3.2, 17.2, 21.5).

A structure-preserving import first probes the product structure: every component
occurrence becomes a node with its prototype label entry, parent occurrence and rigid
placement relative to the parent. The gateway turns the report into frames, assemblies,
parts and one imported feature per leaf; each feature then loads only its prototype shape,
untransformed, and receives its placement through the declared frame hierarchy.
Names are data from the file: they are cleaned, never interpreted.
"""
import math
import numpy as np
from geometry import require, bounds, explore, TopAbs_SOLID, TopAbs_FACE, gp_Trsf
from OCP.XCAFApp import XCAFApp_Application
from OCP.TDocStd import TDocStd_Document
from OCP.TCollection import TCollection_ExtendedString, TCollection_AsciiString
from OCP.XCAFDoc import XCAFDoc_DocumentTool
from OCP.STEPCAFControl import STEPCAFControl_Reader
from OCP.TDF import TDF_LabelSequence, TDF_Label, TDF_Tool
from OCP.TDataStd import TDataStd_Name
from OCP.IFSelect import IFSelect_RetDone

MAX_NODES = 128
MAX_ASSEMBLIES = 64
MAX_DEPTH = 16
_DOCS = {}


def _document(path):
    if path in _DOCS:
        return _DOCS[path]
    app = XCAFApp_Application.GetApplication_s()
    doc = TDocStd_Document(TCollection_ExtendedString('MDTV-XCAF'))
    app.NewDocument(TCollection_ExtendedString('MDTV-XCAF'), doc)
    reader = STEPCAFControl_Reader()
    reader.SetNameMode(True)
    require(reader.ReadFile(path) == IFSelect_RetDone, 'STEP-Datei ungültig.')
    require(reader.Transfer(doc), 'STEP-Produktstruktur konnte nicht übertragen werden.')
    tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    _DOCS[path] = (doc, tool)
    return _DOCS[path]


def _entry(label):
    text = TCollection_AsciiString()
    TDF_Tool.Entry_s(label, text)
    return text.ToCString()


def _name(label):
    attribute = TDataStd_Name()
    if label.FindAttribute(TDataStd_Name.GetID_s(), attribute):
        return ''.join(ch for ch in attribute.Get().ToExtString() if ch.isprintable())[:120].strip()
    return ''


def _axis_angle(trsf):
    """Rigid placement as translation plus axis-angle; mirrored or scaled placements are refused."""
    require(abs(trsf.ScaleFactor() - 1) <= 1e-9 and not trsf.IsNegative(),
            'STEP-Platzierungen mit Skalierung oder Spiegelung werden nicht importiert.', 'OUT_OF_SCOPE')
    R = np.array([[trsf.Value(i, j) for j in (1, 2, 3)] for i in (1, 2, 3)], float)
    t = [trsf.Value(i, 4) for i in (1, 2, 3)]
    cos_angle = max(-1.0, min(1.0, (np.trace(R) - 1) / 2))
    angle = math.acos(cos_angle)
    if angle < 1e-12:
        axis = np.array([0.0, 0.0, 1.0])
        angle = 0.0
    elif math.pi - angle < 1e-9:
        # Symmetric case: axis from the largest diagonal entry of (R + I) / 2.
        M = (R + np.eye(3)) / 2
        column = int(np.argmax(np.diag(M)))
        axis = M[:, column] / math.sqrt(max(M[column, column], 1e-300))
        axis = axis / np.linalg.norm(axis)
        angle = math.pi
    else:
        axis = np.array([R[2, 1] - R[1, 2], R[0, 2] - R[2, 0], R[1, 0] - R[0, 1]]) / (2 * math.sin(angle))
        axis = axis / np.linalg.norm(axis)
    # Verify the reconstruction against the native matrix: honest residual, no silent drift.
    u = axis
    c, s = math.cos(angle), math.sin(angle)
    K = np.array([[0, -u[2], u[1]], [u[2], 0, -u[0]], [-u[1], u[0], 0]])
    rebuilt = np.eye(3) * c + (1 - c) * np.outer(u, u) + K * s
    residual = float(np.abs(rebuilt - R).max())
    require(residual <= 1e-9, 'STEP-Platzierung ist keine reine Drehung.', 'PRECISION_UNSUPPORTED')
    return {'translation': [float(x) for x in t], 'axis': [float(x) for x in axis], 'angle_deg': math.degrees(angle),
            'identity': angle == 0.0 and all(abs(x) < 1e-12 for x in t), 'rotation_residual': residual}


def probe(path):
    """Occurrence tree of a STEP file. Units are millimetres after OCCT's declared-unit conversion."""
    doc, tool = _document(path)
    free = TDF_LabelSequence()
    tool.GetFreeShapes(free)
    require(free.Length() >= 1, 'STEP-Datei enthält keine freien Formen.', 'GEOMETRY_INVALID')
    nodes = []

    def walk(label, parent_path, occurrence, placement, depth):
        require(depth <= MAX_DEPTH, 'STEP-Baugruppe ist zu tief verschachtelt.', 'BUDGET_EXCEEDED')
        require(len(nodes) < MAX_NODES, 'STEP-Baugruppe überschreitet das Strukturbudget von %d Vorkommen.' % MAX_NODES, 'BUDGET_EXCEEDED')
        occurrence_name = _name(occurrence)
        if tool.IsReference_s(label):
            referred = TDF_Label()
            require(tool.GetReferredShape_s(label, referred), 'STEP-Komponente ohne Referenzform.')
            label = referred
        prototype = _entry(label)
        is_assembly = tool.IsAssembly_s(label)
        # Occurrence paths stay unique even when one sub-assembly prototype is placed several times.
        path_entry = (parent_path + '/' if parent_path else '') + _entry(occurrence)
        node = {'entry': path_entry, 'label_entry': _entry(occurrence), 'prototype_entry': prototype, 'name': occurrence_name or _name(label),
                'prototype_name': _name(label), 'kind': 'assembly' if is_assembly else 'part', 'parent_entry': parent_path, 'transform': placement, 'depth': depth}
        if not is_assembly:
            shape = tool.GetShape_s(label)
            require(not shape.IsNull(), 'Leere STEP-Komponente.', 'GEOMETRY_INVALID')
            node.update(local_bounds=bounds(shape), solids=sum(1 for _ in explore(shape, TopAbs_SOLID)), faces=sum(1 for _ in explore(shape, TopAbs_FACE)))
        nodes.append(node)
        if is_assembly:
            components = TDF_LabelSequence()
            tool.GetComponents_s(label, components)
            for i in range(1, components.Length() + 1):
                component = components.Value(i)
                referred = TDF_Label()
                require(tool.GetReferredShape_s(component, referred), 'STEP-Komponente ohne Referenzform.')
                walk(referred, path_entry, component, _axis_angle(tool.GetLocation_s(component).Transformation()), depth + 1)

    for i in range(1, free.Length() + 1):
        label = free.Value(i)
        walk(label, None, label, _axis_angle(gp_Trsf()), 0)
    assemblies = sum(1 for n in nodes if n['kind'] == 'assembly')
    require(assemblies <= MAX_ASSEMBLIES, 'STEP-Baugruppe überschreitet das Baugruppenbudget.', 'BUDGET_EXCEEDED')
    return {'method': 'OCCT_STEPCAFControl_XCAF_occurrence_tree', 'unit': 'mm', 'unit_source': 'STEP_declared_units_converted_by_OCCT',
            'nodes': nodes, 'roots': free.Length(), 'assemblies': assemblies, 'parts': len(nodes) - assemblies,
            'placements': 'rigid_axis_angle_relative_to_parent_occurrence', 'limits': {'occurrences': MAX_NODES, 'assemblies': MAX_ASSEMBLIES, 'depth': MAX_DEPTH}}


def component_shape(path, entry):
    """Untransformed prototype shape of one label entry; placement comes from the declared frame."""
    doc, tool = _document(path)
    label = TDF_Label()
    TDF_Tool.Label_s(doc.GetData(), TCollection_AsciiString(entry), label, False)
    require(not label.IsNull() and tool.IsShape_s(label), 'STEP-Komponente nicht gefunden.', 'GEOMETRY_INVALID')
    if tool.IsReference_s(label):
        referred = TDF_Label()
        require(tool.GetReferredShape_s(label, referred), 'STEP-Komponente ohne Referenzform.')
        label = referred
    require(not tool.IsAssembly_s(label), 'Eine Baugruppe ist kein Teil; ihre Komponenten einzeln importieren.', 'OUT_OF_SCOPE')
    shape = tool.GetShape_s(label)
    require(not shape.IsNull(), 'Leere STEP-Komponente.', 'GEOMETRY_INVALID')
    return shape
