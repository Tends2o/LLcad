"""Write a small STEP assembly fixture (nested assembly, two instances of one prototype, rotation).

Usage: .venv/bin/python scripts/assembly-fixture.py <output.step>
"""
import math, sys
from OCP.XCAFApp import XCAFApp_Application
from OCP.TDocStd import TDocStd_Document
from OCP.TCollection import TCollection_ExtendedString
from OCP.XCAFDoc import XCAFDoc_DocumentTool
from OCP.STEPCAFControl import STEPCAFControl_Writer
from OCP.TDataStd import TDataStd_Name
from OCP.gp import gp_Trsf, gp_Vec, gp_Ax1, gp_Pnt, gp_Dir
from OCP.TopLoc import TopLoc_Location
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
from OCP.IFSelect import IFSelect_RetDone
from OCP.Message import Message, Message_Gravity


def main(path):
    Message.DefaultMessenger_s().Printers().Clear()
    app = XCAFApp_Application.GetApplication_s()
    doc = TDocStd_Document(TCollection_ExtendedString('MDTV-XCAF'))
    app.NewDocument(TCollection_ExtendedString('MDTV-XCAF'), doc)
    tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())

    def named(label, name):
        TDataStd_Name.Set_s(label, TCollection_ExtendedString(name))
        return label

    def location(dx=0, dy=0, dz=0, axis=None, angle_deg=0):
        t = gp_Trsf()
        if axis is not None and angle_deg:
            t.SetRotation(gp_Ax1(gp_Pnt(0, 0, 0), gp_Dir(*axis)), math.radians(angle_deg))
        move = gp_Trsf()
        move.SetTranslation(gp_Vec(dx, dy, dz))
        return TopLoc_Location(move.Multiplied(t))

    root = named(tool.NewShape(), 'Baugruppe')
    sub = named(tool.NewShape(), 'Unterbaugruppe')
    box = named(tool.AddShape(BRepPrimAPI_MakeBox(10, 10, 10).Shape(), False), 'Quader')
    cylinder = named(tool.AddShape(BRepPrimAPI_MakeCylinder(3, 12).Shape(), False), 'Zylinder')
    pin = named(tool.AddShape(BRepPrimAPI_MakeCylinder(1, 4).Shape(), False), 'Stift')
    named(tool.AddComponent(root, box, location()), 'Quader an Ort')
    named(tool.AddComponent(root, cylinder, location(20, 0, 0, axis=(1, 0, 0), angle_deg=90)), 'Zylinder gedreht')
    named(tool.AddComponent(root, sub, location(0, 30, 0)), 'Unterbaugruppe versetzt')
    named(tool.AddComponent(sub, pin, location(0, 0, 0)), 'Stift 1')
    named(tool.AddComponent(sub, pin, location(5, 0, 0)), 'Stift 2')
    tool.UpdateAssemblies()
    writer = STEPCAFControl_Writer()
    writer.SetNameMode(True)
    assert writer.Transfer(doc), 'transfer failed'
    assert writer.Write(path) == IFSelect_RetDone, 'write failed'


if __name__ == '__main__':
    main(sys.argv[1])
