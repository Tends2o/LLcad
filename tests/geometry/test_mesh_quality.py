import unittest, sys, math, tempfile, copy, subprocess
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'workers/cad-occt'))
import mesh_quality as mq
from geometry import GeometryError

def cube(size=1, origin=(0,0,0), inward=False):
    vertices=[[origin[0]+x*size,origin[1]+y*size,origin[2]+z*size] for x,y,z in
              [(0,0,0),(1,0,0),(1,1,0),(0,1,0),(0,0,1),(1,0,1),(1,1,1),(0,1,1)]]
    triangles=[[0,2,1],[0,3,2],[4,5,6],[4,6,7],[0,1,5],[0,5,4],[1,2,6],[1,6,5],[2,3,7],[2,7,6],[3,0,4],[3,4,7]]
    return {'vertices':vertices,'triangles':[t[::-1] if inward else t for t in triangles]}

def ascii_stl(mesh):
    lines=['solid test']
    for t in mesh['triangles']:
        lines+=['facet normal 0 0 0','outer loop']+['vertex '+' '.join(map(str,mesh['vertices'][i])) for i in t]+['endloop','endfacet']
    return '\n'.join(lines+['endsolid test'])+'\n'

class MeshQualityTests(unittest.TestCase):
    def test_cube_area_volume_topology_and_quality(self):
        q=mq.inspect_mesh(cube());self.assertTrue(q['watertight_solid']);n=q['native'];top=q['topology']
        for key,value in [('signed_volume_interval_mm3',1),('area_interval_mm2',6)]:
            self.assertLessEqual(n[key][0],value);self.assertGreaterEqual(n[key][1],value)
        self.assertAlmostEqual(n['triangle_quality_min'],math.sqrt(3)/2)
        self.assertEqual((top['vertices'],top['edges'],top['triangles'],top['euler_characteristic']),(8,18,12,2))
        self.assertEqual(top['component_examples'][0]['genus_if_closed_orientable'],0)
        self.assertEqual(q['manufacturing_status'],'not_certified');self.assertIsNone(q['source_surface_error_bound_mm'])

    def test_tetrahedron_analytic_measurements(self):
        m={'vertices':[[0,0,0],[1,0,0],[0,1,0],[0,0,1]],'triangles':[[0,2,1],[0,1,3],[0,3,2],[1,2,3]]}
        q=mq.inspect_mesh(m);self.assertTrue(q['watertight_solid'])
        self.assertAlmostEqual(sum(q['native']['signed_volume_interval_mm3'])/2,1/6)
        self.assertAlmostEqual(sum(q['native']['area_interval_mm2'])/2,(3+math.sqrt(3))/2)
        self.assertAlmostEqual(q['native']['triangle_quality_max'],1)

    def test_cavities_islands_disconnected_solids_and_reversed_outer(self):
        for mesh,valid,volume,solids in [
            (cube(inward=True),False,None,0),
            (mq.combine([cube(3),cube(1,(1,1,1),True)]),True,26,1),
            (mq.combine([cube(3),cube(1,(1,1,1))]),False,None,0),
            (mq.combine([cube(5),cube(3,(1,1,1),True),cube(1,(2,2,2))]),True,99,2),
            (mq.combine([cube(),cube(1,(2,0,0))]),True,2,2),
        ]:
            with self.subTest(valid=valid,volume=volume):
                q=mq.inspect_mesh(mesh);self.assertEqual(q['watertight_solid'],valid)
                facts=mq.mesh_facts(mesh,{'cache_key':'test'},q);self.assertEqual(facts['solids'],solids)
                if volume is None:self.assertIsNone(facts['volume'])
                else:self.assertAlmostEqual(facts['volume'],volume)

    def test_open_mesh_and_inconsistent_winding(self):
        m=cube();m['triangles'].pop();q=mq.inspect_mesh(m)
        self.assertFalse(q['watertight_solid']);self.assertEqual(q['topology']['boundary_edges'],3)
        self.assertTrue(mq.mesh_facts(m,{'cache_key':'x'},q)['valid'])
        m=cube();m['triangles'][0].reverse();q=mq.inspect_mesh(m)
        self.assertFalse(q['checks']['consistent_orientation']);self.assertFalse(q['watertight_solid'])

    def test_duplicate_degenerate_and_nonmanifold_edge(self):
        m=cube();m['triangles'].append(m['triangles'][0]);q=mq.inspect_mesh(m)
        self.assertFalse(q['checks']['unique_triangles']);self.assertEqual(q['topology']['nonmanifold_edges'],3)
        for vertices,triangles in [([[0,0,0],[1,0,0],[2,0,0]],[[0,1,2]]),([[0,0,0],[1,0,0]],[[0,1,0]])]:
            q=mq.inspect_mesh({'vertices':vertices,'triangles':triangles});self.assertFalse(q['checks']['nondegenerate'])

    def test_bowtie_vertex_is_not_a_manifold_despite_closed_edges(self):
        m=mq.combine([cube(),cube(1,(1,1,1))])
        m['triangles']=[[6 if i==8 else i for i in t] for t in m['triangles']]
        # Remove the now unreferenced duplicate corner and compact its indices.
        m['vertices'].pop(8);m['triangles']=[[i-1 if i>8 else i for i in t] for t in m['triangles']]
        q=mq.inspect_mesh(m);self.assertEqual(q['topology']['boundary_edges'],0)
        self.assertEqual(q['topology']['nonmanifold_edges'],0);self.assertEqual(q['topology']['nonmanifold_vertices'],1)
        self.assertFalse(q['watertight_solid'])

    def test_exact_contacts_crossings_coplanar_overlap_and_small_gap(self):
        for offset,valid in [((.5,.5,.5),False),((1,0,0),False),((1,1,1),False),((1+2**-40,0,0),True)]:
            q=mq.inspect_mesh(mq.combine([cube(),cube(1,offset)]));self.assertEqual(q['watertight_solid'],valid)
            self.assertEqual(q['checks']['no_self_intersections'],valid)
        m={'vertices':[[0,0,0],[2,0,0],[0,2,0],[.5,.5,0],[3,.5,0],[.5,3,0]],'triangles':[[0,1,2],[3,4,5]]}
        self.assertFalse(mq.inspect_mesh(m)['checks']['no_self_intersections'])

    def test_large_translation_keeps_small_binary64_geometry(self):
        q=mq.inspect_mesh(cube(2**-10,(1e9,1e9,1e9)));self.assertTrue(q['watertight_solid'])
        self.assertAlmostEqual(sum(q['native']['signed_volume_interval_mm3'])/2,2**-30,places=20)

    def test_stl_exact_indexing_units_and_export_bound(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'a.stl';p.write_text(ascii_stl(cube(.1)))
            m=mq.read_stl(p,'m');self.assertEqual(len(m['vertices']),8)
            self.assertEqual(m['vertices'][1],[100.,100.,0.]);self.assertFalse(m['source_conversion']['proximity_welding'])
            self.assertEqual(m['source_conversion']['coordinate_error_bound_mm'],0)
            restored,q,r=mq.export_stl(m,Path(tmp)/'out.stl',1e-6)
            self.assertTrue(q['watertight_solid']);self.assertEqual(r['certified_surface_bound'],0)
            self.assertEqual(mq.digest(restored),q['geometry_hash'])
            p.write_text(ascii_stl(cube(.1)));m=mq.read_stl(p,'mm')
            self.assertGreater(m['source_conversion']['coordinate_error_bound_mm'],0)
            _,_,r=mq.export_stl(m,Path(tmp)/'rounded.stl',1e-6)
            self.assertGreater(r['certified_surface_bound'],0)

    def test_export_rejects_precision_loss_and_detects_float32_collapse(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'a.stl'
            with self.assertRaises(GeometryError) as error:mq.export_stl(cube(.1),p,1e-12)
            self.assertEqual(error.exception.code,'PRECISION_UNSUPPORTED')
            _,q,_=mq.export_stl(cube(.001,(1e9,1e9,1e9)),p,1)
            self.assertFalse(q['watertight_solid']);self.assertFalse(q['checks']['nondegenerate'])

    def test_bad_data_and_malformed_ascii_normals(self):
        for m in [{'vertices':[[math.nan,0,0]],'triangles':[[0,0,0]]},{'vertices':[[0,0,0]],'triangles':[[0,0,1]]}]:
            with self.assertRaises(GeometryError):mq.inspect_mesh(m)
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'bad.stl';p.write_text(ascii_stl(cube()).replace('normal 0 0 0','normal wat 0 0',1))
            with self.assertRaises(GeometryError) as error:mq.read_stl(p,'mm')
            self.assertEqual(error.exception.code,'INVALID_SCHEMA')
        process=subprocess.run([str(mq.HERE/'meshcheck')],input=b'3 1\n0 0 0\n1 0 0\n0 1 0\n0 1 2\nEXTRA',capture_output=True,timeout=10)
        self.assertEqual(process.returncode,2);self.assertEqual(process.stderr.strip(),b'INVALID_SCHEMA')

    def test_overlap_budget_and_corresponding_triangle_bound(self):
        m={'vertices':[[0,0,0],[1,0,0],[0,1,0]],'triangles':[[0,1,2]]*2100}
        with self.assertRaises(GeometryError) as error:mq.inspect_mesh(m)
        self.assertEqual(error.exception.code,'BUDGET_EXCEEDED')
        a=cube();b=copy.deepcopy(a);b['vertices'][0]=[.25,.25,.25]
        bound=mq.correspondence_bound(a,b);self.assertGreaterEqual(bound,math.sqrt(3)/4)
        self.assertLess(bound,math.sqrt(3)/4+1e-15)
        b['triangles'][0].reverse()
        with self.assertRaises(GeometryError):mq.correspondence_bound(a,b)

if __name__=='__main__':unittest.main()
