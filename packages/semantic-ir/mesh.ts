import { z } from "zod";
const Digest = z.string().regex(/^[a-f0-9]{64}$/);
const count = z.int().nonnegative();
const pair = z.tuple([count, count]);
const interval = z.tuple([z.number(), z.number()]).refine(([a, b]) => a <= b);
export const MeshQuality = z.strictObject({
  schema_version: z.literal("1"),
  geometry_hash: Digest,
  engine: z.literal("CGAL-6.0.1/EPECK"),
  domain: z.literal("entire_indexed_mesh_at_supplied_binary64_coordinates"),
  guarantee: z.literal("exact_predicates_for_declared_mesh"),
  topology: z.strictObject({
    vertices: count,
    edges: count,
    triangles: count,
    surface_components: count,
    boundary_edges: count,
    nonmanifold_edges: count,
    inconsistently_oriented_edges: count,
    nonmanifold_vertices: count,
    isolated_vertices: count,
    duplicate_triangles: count,
    closed_vertex_manifold: z.boolean(),
    consistent_orientation: z.boolean(),
    euler_characteristic: z.int(),
    component_examples: z
      .array(
        z.strictObject({
          first_triangle: count,
          vertices: count,
          edges: count,
          triangles: count,
          euler_characteristic: z.int(),
          genus_if_closed_orientable: count.nullable(),
        }),
      )
      .max(16),
    examples: z.strictObject({
      boundary_edges: z.array(pair).max(16),
      nonmanifold_edges: z.array(pair).max(16),
      nonmanifold_vertices: z.array(count).max(16),
      inconsistently_oriented_edges: z.array(pair).max(16),
    }),
  }),
  native: z.strictObject({
    schema_version: z.literal("1"),
    engine: z.literal("CGAL-6.0.1/EPECK"),
    source_hash: Digest,
    degenerate_triangles: count,
    aabb_candidate_pairs: count.max(2000000),
    self_intersections_found: z.boolean(),
    intersection_examples: z.array(pair).max(16),
    volume_checked: z.boolean(),
    nested_orientation_valid: z.boolean(),
    bounded_solids: count.max(64),
    signed_volume_interval_mm3: interval,
    area_interval_mm2: interval,
    triangle_quality_min: z.number().min(0).max(1),
    triangle_quality_max: z.number().min(0).max(1),
    components: z
      .array(
        z.strictObject({
          first_triangle: count,
          nesting_depth: count,
          outward: z.boolean(),
        }),
      )
      .max(64),
  }),
  checks: z.strictObject({
    nondegenerate: z.boolean(),
    unique_triangles: z.boolean(),
    closed_vertex_manifold: z.boolean(),
    consistent_orientation: z.boolean(),
    no_self_intersections: z.boolean(),
    nested_shell_orientation: z.boolean(),
  }),
  watertight_solid: z.boolean(),
  manufacturing_status: z.literal("not_certified"),
  source_surface_error_bound_mm: z.null(),
});
