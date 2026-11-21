import * as THREE from "three";
import { DecalGeometry } from "three/addons/geometries/DecalGeometry.js";
import { SVGLoader } from "three/addons/loaders/SVGLoader.js";
/* --- Decals ----------------------------------------------------------------
A decal is a print that exists only for the eye: an SVG graphic projected onto
the surface of one output, never part of the B-Rep, never in STEP. The IR names
it with an anchor (a feature, a point on its surface and the surface normal
there), a size in millimetres, a rotation about the normal and a colour. The
graphic is parsed with SVGLoader and painted into a texture — in the decal's
colour when it has one, which is how a single-colour print is described, or in
its own colours otherwise — and DecalGeometry projects it onto the mesh. */

export type DecalSpec = {
  id: string;
  source?: { artifact_id?: string; path?: string };
  anchor: { feature_id?: string; point_mm: number[]; normal: number[] };
  size_mm: number[];
  rotation_deg?: number;
  colour_hex?: string;
  /** SVG text the server resolved for a path source. */
  svg?: string | null;
  /** Why the server could not resolve the source, if it could not. */
  source_error?: string | null;
};
export type DecalTexture = { texture: THREE.CanvasTexture; aspect: number };

const HEX = /^#[0-9a-fA-F]{6}$/;
function paintable(value: unknown) {
  return (
    typeof value === "string" &&
    value !== "none" &&
    value !== "transparent" &&
    !value.startsWith("url(")
  );
}
/** The graphic's own frame: the viewBox if it has one, else its width and
 *  height, else the bounds of what it draws. */
function frameOf(svg: { paths: THREE.ShapePath[]; xml: unknown }) {
  const root = svg.xml as Element | undefined;
  const box = root
    ?.getAttribute?.("viewBox")
    ?.trim()
    .split(/[\s,]+/)
    .map(Number);
  if (
    box &&
    box.length === 4 &&
    box.every(Number.isFinite) &&
    box[2] > 0 &&
    box[3] > 0
  )
    return { x: box[0], y: box[1], w: box[2], h: box[3] };
  const width = parseFloat(root?.getAttribute?.("width") ?? ""),
    height = parseFloat(root?.getAttribute?.("height") ?? "");
  if (width > 0 && height > 0) return { x: 0, y: 0, w: width, h: height };
  const bounds = new THREE.Box2();
  for (const path of svg.paths)
    for (const sub of path.subPaths)
      for (const p of sub.getPoints()) bounds.expandByPoint(p);
  if (bounds.isEmpty()) return { x: 0, y: 0, w: 1, h: 1 };
  const size = bounds.getSize(new THREE.Vector2());
  return {
    x: bounds.min.x,
    y: bounds.min.y,
    w: Math.max(size.x, 1e-6),
    h: Math.max(size.y, 1e-6),
  };
}
/** Paint an SVG into a transparent canvas texture. Fills are drawn as shapes
 *  with their holes (SVGLoader resolves the fill rule), strokes as polylines. */
export function svgTexture(
  text: string,
  colour: string | null | undefined,
  maxEdge = 1024,
): DecalTexture {
  const svg = new SVGLoader().parse(text);
  if (!svg.paths.length) throw new Error("The SVG draws nothing.");
  const frame = frameOf(svg),
    aspect = frame.w / frame.h,
    width = Math.max(8, Math.round(aspect >= 1 ? maxEdge : maxEdge * aspect)),
    height = Math.max(8, Math.round(aspect >= 1 ? maxEdge / aspect : maxEdge)),
    canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;
  const ctx = canvas.getContext("2d")!;
  ctx.setTransform(
    width / frame.w,
    0,
    0,
    height / frame.h,
    (-frame.x * width) / frame.w,
    (-frame.y * height) / frame.h,
  );
  const single = colour && HEX.test(colour) ? colour : null;
  const trace = (points: THREE.Vector2[], close: boolean) => {
    if (!points.length) return;
    ctx.moveTo(points[0].x, points[0].y);
    for (const p of points.slice(1)) ctx.lineTo(p.x, p.y);
    if (close) ctx.closePath();
  };
  for (const path of svg.paths) {
    const style: any = (path as any).userData?.style ?? {};
    const own = `#${path.color.getHexString()}`;
    if (paintable(style.fill)) {
      ctx.fillStyle = single ?? own;
      ctx.globalAlpha =
        Number(style.fillOpacity ?? 1) * Number(style.opacity ?? 1);
      ctx.beginPath();
      for (const shape of SVGLoader.createShapes(path)) {
        trace(shape.getPoints(24), true);
        for (const hole of shape.holes) trace(hole.getPoints(24), true);
      }
      ctx.fill("evenodd");
    }
    if (paintable(style.stroke) && Number(style.strokeWidth ?? 1) > 0) {
      let stroke = style.stroke;
      try {
        stroke = `#${new THREE.Color().setStyle(style.stroke).getHexString()}`;
      } catch {
        /* an unreadable stroke colour keeps the fill colour */
        stroke = own;
      }
      ctx.strokeStyle = single ?? stroke;
      ctx.globalAlpha =
        Number(style.strokeOpacity ?? 1) * Number(style.opacity ?? 1);
      ctx.lineWidth = Number(style.strokeWidth ?? 1);
      ctx.lineJoin = style.strokeLineJoin ?? "miter";
      ctx.lineCap = style.strokeLineCap ?? "butt";
      ctx.beginPath();
      for (const sub of path.subPaths) trace(sub.getPoints(), sub.autoClose);
      ctx.stroke();
    }
  }
  ctx.globalAlpha = 1;
  const texture = new THREE.CanvasTexture(canvas);
  texture.colorSpace = THREE.SRGBColorSpace;
  texture.anisotropy = 4;
  return { texture, aspect };
}
/** The projector's orientation: z along the surface normal, the graphic's up
 *  towards world +Z where that is possible (towards +Y on surfaces facing up or
 *  down), then turned by the decal's rotation about the normal. */
export function decalOrientation(normal: number[], rotationDeg = 0) {
  const z = new THREE.Vector3(...normal).normalize();
  if (!Number.isFinite(z.x) || z.lengthSq() < 0.5)
    throw new Error("The anchor normal is not a direction.");
  const reference =
    Math.abs(z.z) > 0.99
      ? new THREE.Vector3(0, 1, 0)
      : new THREE.Vector3(0, 0, 1);
  const y = reference.addScaledVector(z, -reference.dot(z)).normalize(),
    x = new THREE.Vector3().crossVectors(y, z).normalize();
  const basis = new THREE.Matrix4().makeBasis(x, y, z),
    turn = new THREE.Matrix4().makeRotationZ(
      THREE.MathUtils.degToRad(rotationDeg),
    );
  return new THREE.Euler().setFromRotationMatrix(basis.multiply(turn));
}
/** Drop the triangles the projector hits from behind: walls and inner faces
 *  that lie inside the projection box but face away from the observer. */
function keepFacing(geometry: THREE.BufferGeometry, axis: THREE.Vector3) {
  const position = geometry.getAttribute("position") as THREE.BufferAttribute,
    normal = geometry.getAttribute("normal") as
      THREE.BufferAttribute | undefined,
    uv = geometry.getAttribute("uv") as THREE.BufferAttribute;
  const keep: number[] = [],
    a = new THREE.Vector3(),
    b = new THREE.Vector3(),
    c = new THREE.Vector3(),
    face = new THREE.Vector3();
  for (let i = 0; i + 2 < position.count; i += 3) {
    a.fromBufferAttribute(position, i);
    b.fromBufferAttribute(position, i + 1);
    c.fromBufferAttribute(position, i + 2);
    face.subVectors(c, b).cross(a.clone().sub(b));
    // Winding may not be reliable on imported meshes: fall back to the
    // interpolated normals when the triangle is degenerate.
    if (face.lengthSq() < 1e-18 && normal) {
      face.set(0, 0, 0);
      for (let k = 0; k < 3; k++)
        face.add(new THREE.Vector3().fromBufferAttribute(normal, i + k));
    }
    if (face.normalize().dot(axis) > 0.05) keep.push(i, i + 1, i + 2);
  }
  if (keep.length === position.count) return geometry;
  const out = new THREE.BufferGeometry();
  const pick = (attribute: THREE.BufferAttribute, size: number) => {
    const data = new Float32Array(keep.length * size);
    keep.forEach((index, j) => {
      for (let k = 0; k < size; k++)
        data[j * size + k] = attribute.array[index * size + k] as number;
    });
    return new THREE.Float32BufferAttribute(data, size);
  };
  out.setAttribute("position", pick(position, 3));
  out.setAttribute("uv", pick(uv, 2));
  if (normal) out.setAttribute("normal", pick(normal, 3));
  geometry.dispose();
  return out;
}
/** The triangles of a mesh near a point (mesh coordinates), as a small
 *  non-indexed geometry. DecalGeometry allocates objects for every vertex it
 *  is given; a print covers a few hundred triangles of a part with a hundred
 *  thousand, so only those within reach of the projector are handed over. */
function nearbyTriangles(
  geometry: THREE.BufferGeometry,
  centre: THREE.Vector3,
  radius: number,
) {
  const position = geometry.getAttribute("position") as THREE.BufferAttribute,
    normal = geometry.getAttribute("normal") as
      THREE.BufferAttribute | undefined,
    index = geometry.getIndex(),
    count = index ? index.count : position.count,
    positions: number[] = [],
    normals: number[] = [];
  for (let t = 0; t + 2 < count; t += 3) {
    const ids = [0, 1, 2].map((k) => (index ? index.getX(t + k) : t + k));
    // Distance from the centre to the triangle's bounding box.
    let d2 = 0;
    for (let axis = 0; axis < 3; axis++) {
      let min = Infinity,
        max = -Infinity;
      for (const id of ids) {
        const v = position.getComponent(id, axis);
        if (v < min) min = v;
        if (v > max) max = v;
      }
      const c = centre.getComponent(axis),
        gap = c < min ? min - c : c > max ? c - max : 0;
      d2 += gap * gap;
    }
    if (d2 > radius * radius) continue;
    for (const id of ids) {
      positions.push(position.getX(id), position.getY(id), position.getZ(id));
      if (normal)
        normals.push(normal.getX(id), normal.getY(id), normal.getZ(id));
    }
  }
  const out = new THREE.BufferGeometry();
  out.setAttribute("position", new THREE.Float32BufferAttribute(positions, 3));
  if (normal)
    out.setAttribute("normal", new THREE.Float32BufferAttribute(normals, 3));
  return out;
}
/** Project one decal onto a preview mesh. The mesh is taken at its assembled
 *  place (its origin, not wherever an explosion has moved it), and the result
 *  is expressed in the mesh's own coordinates, so it can ride along as a child
 *  of the mesh. Returns null when the projection covers no surface. */
export function projectDecal(
  target: THREE.Mesh,
  spec: DecalSpec,
  material: THREE.Material,
): THREE.Mesh | null {
  const [w, h] = spec.size_mm;
  if (!(w > 0 && h > 0))
    throw new Error("size_mm must be two positive numbers.");
  const origin = (target.userData.origin as number[] | undefined) ?? [0, 0, 0];
  const orientation = decalOrientation(
      spec.anchor.normal,
      spec.rotation_deg ?? 0,
    ),
    axis = new THREE.Vector3(0, 0, 1).applyEuler(orientation),
    // Deep enough for the sag of a curved surface under the graphic, shallow
    // enough to miss the far side of a thin wall.
    depth = Math.max(0.5, Math.min(Math.max(w, h) * 0.6, 40)),
    point = new THREE.Vector3(...spec.anchor.point_mm);
  const local = point.clone().sub(new THREE.Vector3(...origin)),
    reach = Math.hypot(w, h, depth) / 2 + 1e-3,
    near = nearbyTriangles(target.geometry, local, reach);
  const proxy = new THREE.Mesh(near);
  proxy.position.fromArray(origin);
  proxy.updateMatrixWorld(true);
  let geometry: THREE.BufferGeometry = new DecalGeometry(
    proxy,
    point,
    orientation,
    new THREE.Vector3(w, h, depth),
  );
  near.dispose();
  geometry = keepFacing(geometry, axis);
  if (!geometry.getAttribute("position")?.count) {
    geometry.dispose();
    return null;
  }
  geometry.translate(-origin[0], -origin[1], -origin[2]);
  const mesh = new THREE.Mesh(geometry, material);
  mesh.name = `decal:${spec.id}`;
  mesh.userData.decal = spec.id;
  // Clicks go to the part underneath; a print is not a feature to select.
  mesh.raycast = () => {};
  mesh.renderOrder = 10;
  return mesh;
}
/** The material a decal is drawn with: lit like the part in the lit channels,
 *  flat in the unlit ones, always just in front of the surface it lies on. */
export function decalMaterial(
  texture: THREE.Texture,
  lit: boolean,
  clippingPlanes: THREE.Plane[],
) {
  const common = {
    map: texture,
    transparent: true,
    depthWrite: false,
    polygonOffset: true,
    polygonOffsetFactor: -4,
    polygonOffsetUnits: -8,
    clippingPlanes,
    side: THREE.FrontSide,
  };
  return lit
    ? new THREE.MeshStandardMaterial({
        ...common,
        roughness: 0.55,
        metalness: 0,
      })
    : new THREE.MeshBasicMaterial({ ...common, toneMapped: false });
}
/** The shape of a decal record as the IR carries it; anything else is left out
 *  with a reason rather than drawn wrongly. */
export function decalProblems(spec: any): string | null {
  const nums = (v: unknown, n: number) =>
    Array.isArray(v) && v.length === n && v.every((x) => Number.isFinite(x));
  if (!spec || typeof spec !== "object") return "not an object";
  if (typeof spec.id !== "string" || !spec.id) return "id missing";
  if (!nums(spec.anchor?.point_mm, 3)) return "anchor.point_mm needs 3 numbers";
  if (!nums(spec.anchor?.normal, 3)) return "anchor.normal needs 3 numbers";
  if (!nums(spec.size_mm, 2) || spec.size_mm[0] <= 0 || spec.size_mm[1] <= 0)
    return "size_mm needs 2 positive numbers";
  if (spec.colour_hex != null && !HEX.test(spec.colour_hex))
    return "colour_hex must look like #rrggbb";
  if (!spec.svg && !spec.source?.artifact_id && !spec.source?.path)
    return "source needs artifact_id or path";
  return null;
}
