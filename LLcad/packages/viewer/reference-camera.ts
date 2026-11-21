import * as THREE from "three";
/* --- Reference cameras -----------------------------------------------------
Two camera forms reach the viewer. The old one is relative to the model's
bounding box (direction, distance factor, field of view): the same numbers frame
a different picture whenever the model's envelope changes. The absolute one is a
real camera in millimetres for one image of width_px × height_px pixels:

  {mode:"absolute", projection:"perspective"|"orthographic", position_mm, target_mm,
   up, fov_deg? (perspective, vertical), mm_per_px? (orthographic), width_px,
   height_px, principal_px?: [cx, cy]}

It is the contract shared with the reference-review schema and the VTK renderer,
so a registered photo, a capture and the interactive overlay agree to the pixel.
Both forms are resolved here into one description, and applied to a three.js
camera through a view window over the full reference frame: the window is how
the interactive overlay zooms and pans without leaving the registration, and a
principal point off the image centre is a shift of that window. */

export type AbsoluteCamera = {
  mode: "absolute";
  projection?: "perspective" | "orthographic";
  position_mm: number[];
  target_mm: number[];
  up?: number[];
  fov_deg?: number;
  mm_per_px?: number;
  width_px: number;
  height_px: number;
  principal_px?: number[];
};
export type RelativeCamera = {
  direction: number[];
  up?: number[];
  projection?: "perspective" | "orthographic";
  target?: number[];
  distance_factor?: number;
  fov?: number;
};
export type ResolvedCamera = {
  mode: "absolute" | "relative";
  projection: "perspective" | "orthographic";
  position: THREE.Vector3;
  target: THREE.Vector3;
  up: THREE.Vector3;
  /** Vertical field of view in degrees (perspective). */
  fov_deg: number;
  /** Half the frame height in millimetres (orthographic). */
  half_height_mm: number;
  width_px: number;
  height_px: number;
  principal_px: [number, number];
};
/** A part of the reference frame, in frame pixels. */
export type FrameWindow = { x: number; y: number; w: number; h: number };

const finite = (v: unknown): v is number =>
  typeof v === "number" && Number.isFinite(v);
const vector = (v: unknown, n = 3) =>
  Array.isArray(v) && v.length === n && v.every(finite);

export function isAbsoluteCamera(camera: any): camera is AbsoluteCamera {
  return camera?.mode === "absolute";
}
/** The problems of an absolute camera, in words; empty when it can be used. */
export function absoluteCameraProblems(camera: any): string[] {
  const problems: string[] = [];
  if (!vector(camera?.position_mm))
    problems.push("position_mm needs 3 numbers");
  if (!vector(camera?.target_mm)) problems.push("target_mm needs 3 numbers");
  if (camera?.up !== undefined && !vector(camera.up))
    problems.push("up needs 3 numbers");
  if (!(Number.isInteger(camera?.width_px) && camera.width_px > 0))
    problems.push("width_px must be a positive integer");
  if (!(Number.isInteger(camera?.height_px) && camera.height_px > 0))
    problems.push("height_px must be a positive integer");
  const ortho = camera?.projection === "orthographic";
  if (ortho && !(finite(camera?.mm_per_px) && camera.mm_per_px > 0))
    problems.push("an orthographic camera needs mm_per_px > 0");
  if (
    !ortho &&
    !(finite(camera?.fov_deg) && camera.fov_deg > 0.1 && camera.fov_deg < 179)
  )
    problems.push("a perspective camera needs fov_deg in (0.1, 179)");
  if (camera?.principal_px !== undefined && !vector(camera.principal_px, 2))
    problems.push("principal_px needs 2 numbers");
  if (
    vector(camera?.position_mm) &&
    vector(camera?.target_mm) &&
    Math.hypot(
      camera.position_mm[0] - camera.target_mm[0],
      camera.position_mm[1] - camera.target_mm[1],
      camera.position_mm[2] - camera.target_mm[2],
    ) < 1e-9
  )
    problems.push("position_mm and target_mm coincide");
  return problems;
}
/** One description for both camera forms. A relative camera needs the model's
 *  bounds (that is its weakness) and the frame it is drawn into; an absolute
 *  camera brings its own frame. */
export function resolveCamera(
  camera: any,
  bounds: THREE.Box3,
  frame: { width: number; height: number },
): ResolvedCamera {
  if (isAbsoluteCamera(camera)) {
    const problems = absoluteCameraProblems(camera);
    if (problems.length)
      throw new Error("Absolute camera unusable: " + problems.join("; "));
    const position = new THREE.Vector3(...camera.position_mm),
      target = new THREE.Vector3(...camera.target_mm),
      up = new THREE.Vector3(...(camera.up ?? [0, 0, 1])).normalize(),
      direction = position.clone().sub(target).normalize();
    if (Math.abs(direction.dot(up)) > 0.999)
      throw new Error("Camera direction and up are parallel");
    const orthographic = camera.projection === "orthographic";
    return {
      mode: "absolute",
      projection: orthographic ? "orthographic" : "perspective",
      position,
      target,
      up,
      fov_deg: orthographic ? 0 : camera.fov_deg!,
      half_height_mm: orthographic
        ? (camera.mm_per_px! * camera.height_px) / 2
        : 0,
      width_px: camera.width_px,
      height_px: camera.height_px,
      principal_px: camera.principal_px
        ? [camera.principal_px[0], camera.principal_px[1]]
        : [camera.width_px / 2, camera.height_px / 2],
    };
  }
  // The bounding-box-relative form, exactly as the capture has always read it.
  const size = bounds.getSize(new THREE.Vector3()),
    span = Math.max(size.x, size.y, size.z, 1e-6),
    target = camera?.target
      ? new THREE.Vector3(...camera.target)
      : bounds.getCenter(new THREE.Vector3()),
    direction = new THREE.Vector3(
      ...(camera?.direction ?? [1, -1, 1]),
    ).normalize(),
    up = new THREE.Vector3(...(camera?.up ?? [0, 0, 1])).normalize();
  if (Math.abs(direction.dot(up)) > 0.999)
    throw new Error("Camera direction and up are parallel");
  const factor = camera?.distance_factor ?? 2.5,
    fov = camera?.fov ?? 28;
  return {
    mode: "relative",
    projection:
      camera?.projection === "orthographic" ? "orthographic" : "perspective",
    position: target.clone().addScaledVector(direction, span * factor),
    target,
    up,
    fov_deg: fov,
    half_height_mm: span * factor * Math.tan(THREE.MathUtils.degToRad(fov) / 2),
    width_px: frame.width,
    height_px: frame.height,
    principal_px: [frame.width / 2, frame.height / 2],
  };
}
/** The whole frame, fitted into a pane of the given size (letterboxed). */
export function containWindow(
  resolved: ResolvedCamera,
  paneWidth: number,
  paneHeight: number,
): FrameWindow {
  const scale = Math.min(
    paneWidth / resolved.width_px,
    paneHeight / resolved.height_px,
  );
  const w = paneWidth / scale,
    h = paneHeight / scale;
  return {
    x: (resolved.width_px - w) / 2,
    y: (resolved.height_px - h) / 2,
    w,
    h,
  };
}
/** Near and far planes that hold the whole model from this camera. */
export function depthRange(
  camera: THREE.Camera,
  bounds: THREE.Box3,
  orthographic: boolean,
) {
  const eye = camera.position,
    forward = camera.getWorldDirection(new THREE.Vector3());
  let nearest = Infinity,
    farthest = -Infinity;
  for (let i = 0; i < 8; i++) {
    const corner = new THREE.Vector3(
      i & 1 ? bounds.max.x : bounds.min.x,
      i & 2 ? bounds.max.y : bounds.min.y,
      i & 4 ? bounds.max.z : bounds.min.z,
    );
    const depth = corner.sub(eye).dot(forward);
    nearest = Math.min(nearest, depth);
    farthest = Math.max(farthest, depth);
  }
  if (!Number.isFinite(nearest)) return { near: 0.01, far: 1e5 };
  const margin = Math.max(1e-3, (farthest - nearest) * 0.02);
  if (orthographic) return { near: nearest - margin, far: farthest + margin };
  const far = Math.max(farthest + margin, 1e-2);
  return { near: Math.max(nearest - margin, far / 1e5, 1e-4), far };
}
/** Put a resolved camera on a three.js camera. The window selects the part of
 *  the reference frame shown (the whole frame when omitted); the principal
 *  point shifts it. Returns the camera that was set up. */
export function applyResolvedCamera(
  resolved: ResolvedCamera,
  perspective: THREE.PerspectiveCamera,
  orthographic: THREE.OrthographicCamera,
  bounds: THREE.Box3,
  window?: FrameWindow,
) {
  const W = resolved.width_px,
    H = resolved.height_px,
    view = window ?? { x: 0, y: 0, w: W, h: H },
    shiftX = W / 2 - resolved.principal_px[0],
    shiftY = H / 2 - resolved.principal_px[1];
  const cam =
    resolved.projection === "orthographic" ? orthographic : perspective;
  cam.up.copy(resolved.up);
  cam.position.copy(resolved.position);
  cam.lookAt(resolved.target);
  cam.updateMatrixWorld(true);
  if (cam === perspective) {
    perspective.fov = resolved.fov_deg;
    perspective.aspect = W / H;
    perspective.zoom = 1;
  } else {
    const half = resolved.half_height_mm;
    orthographic.left = (-half * W) / H;
    orthographic.right = (half * W) / H;
    orthographic.top = half;
    orthographic.bottom = -half;
    orthographic.zoom = 1;
  }
  const whole = !window && Math.abs(shiftX) < 1e-9 && Math.abs(shiftY) < 1e-9;
  if (whole) cam.clearViewOffset();
  else
    cam.setViewOffset(W, H, view.x + shiftX, view.y + shiftY, view.w, view.h);
  const { near, far } = depthRange(cam, bounds, cam === orthographic);
  cam.near = near;
  cam.far = far;
  cam.updateProjectionMatrix();
  return cam;
}
/** A JSON-friendly account of a resolved camera, returned with captures so a
 *  caller can see exactly what was rendered. */
export function describeCamera(resolved: ResolvedCamera) {
  const r = (v: number) => Number(v.toFixed(6));
  return {
    mode: resolved.mode,
    projection: resolved.projection,
    position_mm: resolved.position.toArray().map(r),
    target_mm: resolved.target.toArray().map(r),
    up: resolved.up.toArray().map(r),
    ...(resolved.projection === "perspective"
      ? { fov_deg: r(resolved.fov_deg) }
      : { mm_per_px: r((2 * resolved.half_height_mm) / resolved.height_px) }),
    width_px: resolved.width_px,
    height_px: resolved.height_px,
    principal_px: resolved.principal_px.map(r),
  };
}
