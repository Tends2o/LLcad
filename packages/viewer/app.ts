import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { relativePositions } from "../derived-geometry/coordinates.js";
const el = <T extends HTMLElement = HTMLElement>(id: string) =>
  document.getElementById(id) as T;
const text = (id: string, value: string) => {
  el(id).textContent = value;
};
const key = () => crypto.randomUUID();
const labels: Record<string, string> = {
  width: "Width",
  depth: "Depth",
  height: "Height",
  radius: "Radius",
  major_radius: "Major radius",
  minor_radius: "Minor radius",
  x: "Position X",
  y: "Position Y",
  z: "Position Z",
  count: "Count",
  dx: "Spacing X",
  dy: "Spacing Y",
  dz: "Spacing Z",
  remaining_wall: "Remaining wall (local)",
  volume: "Volume",
  area: "Surface area",
};
let current: any = null,
  selected: any = null,
  candidate: any = null,
  validation: any = null,
  displayRevision: string | null = null,
  busyCount = 0;
let authMode = "local",
  bearer = "";
function headers(extra: Record<string, string> = {}) {
  return {
    "X-MathForge-Client": "viewer",
    ...(bearer ? { Authorization: `Bearer ${bearer}` } : {}),
    ...extra,
  };
}
async function api(path: string, options: RequestInit = {}) {
  const response = await fetch(path, {
    ...options,
    headers: headers((options.headers as Record<string, string>) ?? {}),
  });
  if (response.status === 401) {
    showLogin();
    throw new Error("Please sign in.");
  }
  const result = await response.json();
  if (!response.ok || result.status === "failed")
    throw new Error(
      result.errors?.map((e: any) => `${e.code}: ${e.message}`).join("; ") ||
        result.error?.message ||
        "Request failed.",
    );
  return result;
}
async function tool(name: string, args: any) {
  return api("/api/tools/" + name, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(args),
  });
}
function toast(message: string) {
  text("toast", message);
  el("toast").hidden = false;
  setTimeout(() => {
    el("toast").hidden = true;
  }, 7000);
}
function busy(message: string, on = true) {
  busyCount = Math.max(0, busyCount + (on ? 1 : -1));
  text("busy-text", message);
  el("busy").hidden = busyCount === 0;
}
function action(fn: () => Promise<void>) {
  return async () => {
    try {
      await fn();
    } catch (error) {
      toast((error as Error).message);
      busyCount = 0;
      el("busy").hidden = true;
    }
  };
}
async function job(response: any) {
  if (!response.job_id) return response;
  let delay = 100;
  for (let i = 0; i < 250; i++) {
    const result = await tool("cad_job_get", { job_id: response.job_id });
    if (result.status === "succeeded") return result.result;
    if (["failed", "cancelled"].includes(result.status))
      throw new Error(
        result.error
          ? `${result.error.code}: ${result.error.message}`
          : "Job cancelled.",
      );
    await new Promise((r) => setTimeout(r, delay));
    delay = Math.min(700, delay + 30);
  }
  throw new Error("The job is still running; poll its status again.");
}
function activity(title: string, detail: string) {
  text("activity-title", title);
  text("activity-detail", detail);
}
async function models() {
  const data = await api("/api/models");
  const select = el<HTMLSelectElement>("model-select");
  select.replaceChildren();
  for (const m of data.models) {
    const o = document.createElement("option");
    o.value = m.id;
    o.textContent = m.name;
    select.append(o);
  }
  if (current) select.value = current.model_id;
  return data.models;
}
function showLogin() {
  if (!el<HTMLDialogElement>("login-dialog").open)
    el<HTMLDialogElement>("login-dialog").showModal();
}
el("login-dialog").addEventListener("cancel", (event) =>
  event.preventDefault(),
);
el("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const token = el<HTMLInputElement>("token").value;
    if (authMode === "oauth") {
      bearer = token;
      await api("/api/models");
    } else
      await api("/api/session", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token }),
      });
    el<HTMLInputElement>("token").value = "";
    el<HTMLDialogElement>("login-dialog").close();
    await initialize();
  } catch (error) {
    text("login-error", (error as Error).message);
  }
});
el("logout").onclick = action(async () => {
  await api("/api/session", { method: "DELETE" });
  bearer = "";
  location.reload();
});

const viewport = el("viewport"),
  scene = new THREE.Scene();
scene.background = new THREE.Color("#f1f4ec");
const camera = new THREE.PerspectiveCamera(38, 1, 0.01, 100000);
camera.up.set(0, 0, 1);
camera.position.set(65, -75, 65);
const orthoCamera = new THREE.OrthographicCamera(
  -35,
  35,
  35,
  -35,
  0.01,
  100000,
);
orthoCamera.up.set(0, 0, 1);
let activeCamera: THREE.PerspectiveCamera | THREE.OrthographicCamera = camera;
const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false });
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
renderer.localClippingEnabled = true;
renderer.setClearColor("#f1f4ec");
renderer.outputColorSpace = THREE.SRGBColorSpace;
viewport.prepend(renderer.domElement);
const controls = new OrbitControls<
  THREE.PerspectiveCamera | THREE.OrthographicCamera
>(activeCamera, renderer.domElement);
controls.enableDamping = true;
controls.dampingFactor = 0.08;
scene.add(new THREE.HemisphereLight(0xffffff, 0x738068, 2.4));
const light = new THREE.DirectionalLight(0xffffff, 3.1);
light.position.set(30, -35, 70);
scene.add(light);
const fill = new THREE.DirectionalLight(0xdde5d1, 1.4);
fill.position.set(-50, 30, 20);
scene.add(fill);
let grid = new THREE.GridHelper(160, 40, 0xd0d9c5, 0xe4e9dd);
grid.rotation.x = Math.PI / 2;
grid.position.z = -0.1;
scene.add(grid);
const mainGroup = new THREE.Group(),
  beforeGroup = new THREE.Group();
scene.add(mainGroup, beforeGroup);
const clipping = new THREE.Plane(new THREE.Vector3(0, -1, 0), 0);
let wireframe = false,
  section = false,
  measuring = false,
  exploded = false,
  meshData: any = null,
  beforeData: any = null;
type OverlayMode = "parts" | "lit" | "unlit" | "normals" | "curvature";
const OVERLAY_MODES: OverlayMode[] = [
  "parts",
  "lit",
  "unlit",
  "normals",
  "curvature",
];
let overlayMode: OverlayMode = "parts",
  lodTarget = 0.02,
  currentMmPerPixel = 0.1,
  lastScalePx = -1;
/** Part colours (Bauplan 19.3): parts with the same name share one colour, so identical
 *  components read alike. A role after " · " (e.g. "Resistor 0603 · gate") is ignored for
 *  grouping. Colours come from a fixed palette by name hash, resolved collision-free per model. */
const PART_PALETTE = [
  "#5b8def",
  "#e08a3c",
  "#5aa469",
  "#d15b5b",
  "#8b6bc4",
  "#3fb0a8",
  "#b28457",
  "#e07aa8",
  "#9aa53a",
  "#4f7fa8",
  "#d9a93a",
  "#6d6d6d",
  "#c2c95a",
  "#a85a4a",
  "#3ea3d1",
  "#c9a27e",
  "#7a9bd6",
  "#f0a26b",
  "#8cc48f",
  "#e28b8b",
  "#b19ad9",
  "#79cbc5",
  "#cba98a",
  "#f0a3c5",
  "#bfc86a",
  "#7fa0c6",
  "#efc76b",
  "#9a9a9a",
  "#dade8a",
  "#c88a7d",
  "#7cc0e0",
  "#ddc3a5",
];
const partNames = new Map<string, string>();
const groupColours = new Map<string, string>();
const groupKey = (name: string) => name.split(" · ")[0].trim() || name;
/** Material colours by name prefix: copper layers and plated vias of a PCB read as metal
 *  instead of taking an arbitrary palette slot. */
const FIXED_COLOURS: [RegExp, string][] = [
  [/^Kupferlage Oberseite/i, "#c9793a"],
  [/^Kupferlage Unterseite/i, "#8d5a2e"],
  [/^Durchkontaktierung/i, "#b9bcc0"],
];
function assignGroupColours() {
  groupColours.clear();
  const keys = [...new Set([...partNames.values()].map(groupKey))].sort(
    (a, b) => a.localeCompare(b, "de"),
  );
  const used = new Set<number>();
  for (const k of keys) {
    const fixed = FIXED_COLOURS.find(([re]) => re.test(k));
    if (fixed) {
      groupColours.set(k, fixed[1]);
      continue;
    }
    let hash = 7;
    for (const ch of k) hash = (hash * 31 + ch.charCodeAt(0)) >>> 0;
    let i = hash % PART_PALETTE.length;
    while (used.has(i) && used.size < PART_PALETTE.length)
      i = (i + 1) % PART_PALETTE.length;
    used.add(i);
    groupColours.set(k, PART_PALETTE[i]);
  }
}
function partColour(featureID: string | undefined): string {
  const part = current?.features?.find(
    (f: any) => f.id === featureID,
  )?.owner_part;
  const name = part ? partNames.get(part) : undefined;
  return (name && groupColours.get(groupKey(name))) || "#91a58b";
}
function renderLegend() {
  const legend = el("legend");
  legend.replaceChildren();
  legend.hidden = overlayMode !== "parts" || groupColours.size === 0;
  const counts = new Map<string, number>();
  for (const name of partNames.values())
    counts.set(groupKey(name), (counts.get(groupKey(name)) ?? 0) + 1);
  for (const [k, colour] of groupColours) {
    const row = document.createElement("span");
    row.className = "legend-entry";
    const swatch = document.createElement("i");
    swatch.style.background = colour;
    const n = counts.get(k) ?? 1;
    row.append(swatch, n > 1 ? `${k} (${n} parts)` : k);
    legend.append(row);
  }
}
const regionGroup = new THREE.Group(),
  markerGroup = new THREE.Group();
scene.add(regionGroup, markerGroup);
const measurementPoints: THREE.Vector3[] = [];
/** Semantic anchor of a viewer hit (Bauplan 8.4): world point, native normal, barycentric coordinates, camera. */
function anchorFor(hit: THREE.Intersection) {
  const mesh = hit.object as THREE.Mesh;
  const geometry = mesh.geometry as THREE.BufferGeometry;
  const index = geometry.getIndex();
  const decimal = (v: number) => v.toFixed(6);
  const anchor: Record<string, unknown> = {
    point: hit.point.toArray().map(decimal),
    view_direction: activeCamera
      .getWorldDirection(new THREE.Vector3())
      .toArray()
      .map(decimal),
  };
  if (hit.face) anchor.normal = hit.face.normal.toArray().map(decimal);
  if (index && hit.faceIndex != null) {
    const corner = (k: number) =>
      new THREE.Vector3()
        .fromBufferAttribute(
          geometry.attributes.position as THREE.BufferAttribute,
          index.getX(hit.faceIndex! * 3 + k),
        )
        .add(mesh.position);
    const bary = THREE.Triangle.getBarycoord(
      hit.point,
      corner(0),
      corner(1),
      corner(2),
      new THREE.Vector3(),
    );
    if (bary)
      anchor.barycentric = bary
        .toArray()
        .map((v) => Math.min(1, Math.max(0, v)).toFixed(6));
    anchor.triangle_index = hit.faceIndex;
  }
  return anchor;
}
function drawMarkers() {
  clearGroup(markerGroup);
  if (!measurementPoints.length) return;
  const origin = measurementPoints[0];
  const radius = Math.max(currentMmPerPixel * 3, 1e-4);
  for (const point of measurementPoints) {
    const marker = new THREE.Mesh(
      new THREE.SphereGeometry(radius, 12, 8),
      new THREE.MeshBasicMaterial({ color: 0xd55c37, depthTest: false }),
    );
    marker.position.copy(point);
    markerGroup.add(marker);
  }
  if (measurementPoints.length === 2) {
    const geometry = new THREE.BufferGeometry().setFromPoints([
      new THREE.Vector3(),
      measurementPoints[1].clone().sub(origin),
    ]);
    const line = new THREE.Line(
      geometry,
      new THREE.LineBasicMaterial({ color: 0xd55c37, depthTest: false }),
    );
    line.position.copy(origin);
    markerGroup.add(line);
  }
}
const raycaster = new THREE.Raycaster();
let down = [0, 0];
renderer.domElement.addEventListener("pointerdown", (e) => {
  down = [e.clientX, e.clientY];
});
renderer.domElement.addEventListener("pointerup", (event) => {
  if (Math.hypot(event.clientX - down[0], event.clientY - down[1]) > 4) return;
  const box = renderer.domElement.getBoundingClientRect();
  raycaster.setFromCamera(
    new THREE.Vector2(
      ((event.clientX - box.left) / box.width) * 2 - 1,
      (-(event.clientY - box.top) / box.height) * 2 + 1,
    ),
    activeCamera,
  );
  const hit = raycaster
    .intersectObjects(mainGroup.children)
    .find((h) => !section || clipping.distanceToPoint(h.point) >= 0);
  if (!hit) return;
  if (measuring) {
    measurementPoints.push(hit.point.clone());
    if (measurementPoints.length > 2) measurementPoints.shift();
    drawMarkers();
    el("measurement").hidden = false;
    if (measurementPoints.length === 1)
      text("measurement", "Select the second surface point …");
    else
      text(
        "measurement",
        `${measurementPoints[0].distanceTo(measurementPoints[1]).toFixed(4)} mm · distance on the preview`,
      );
  } else if (hit.object.userData.feature_id) {
    const face = hit.object.userData.face_ranges?.find(
      (range: any) =>
        hit.faceIndex != null &&
        hit.faceIndex >= range.first_triangle &&
        hit.faceIndex < range.first_triangle + range.triangle_count,
    );
    if (face) {
      const anchor = anchorFor(hit);
      void action(async () =>
        selectFeature(hit.object.userData.feature_id, face.face_id, anchor),
      )();
      return;
    }
    const feature = current?.features.find(
      (f: any) => f.id === hit.object.userData.feature_id,
    );
    if (feature?.depends_on.length)
      toast(
        "For an unambiguous selection pick the feature in the construction tree.",
      );
    else
      void action(async () => selectFeature(hit.object.userData.feature_id))();
  }
});
function clearGroup(group: THREE.Group) {
  for (const child of [...group.children]) {
    group.remove(child);
    if (
      child instanceof THREE.Mesh ||
      child instanceof THREE.Line ||
      child instanceof THREE.LineSegments
    ) {
      child.geometry.dispose();
      (child.material as THREE.Material).dispose();
    }
  }
}
/** Display channels (Bauplan 19.3): lit shading, an unlit diagnostic channel, normals and native per-face curvature. */
function materialFor(
  m: any,
  ghost: boolean,
  geometry: THREE.BufferGeometry,
): THREE.Material {
  const common = {
    side: THREE.DoubleSide,
    transparent: ghost,
    opacity: ghost ? 0.24 : 1,
    wireframe,
    clippingPlanes: section ? [clipping] : [],
  };
  if (!ghost && overlayMode === "parts")
    return new THREE.MeshStandardMaterial({
      color: new THREE.Color(partColour(m.feature_id)),
      roughness: 0.48,
      metalness: 0.18,
      ...common,
    });
  if (ghost || overlayMode === "lit")
    return new THREE.MeshStandardMaterial({
      color: ghost ? 0x769281 : candidate ? 0xd38a63 : 0x91a58b,
      roughness: 0.48,
      metalness: 0.18,
      ...common,
    });
  if (overlayMode === "normals") return new THREE.MeshNormalMaterial(common);
  if (overlayMode === "curvature") {
    // Native maximum principal curvature per face from the adaptive preview; faces without it stay grey.
    const ranges: any[] = (m.face_ranges ?? []).filter(
      (r: any) => r.curvature_max_per_mm != null,
    );
    const colors = new Float32Array(
      geometry.attributes.position.count * 3,
    ).fill(0.72);
    const index = geometry.getIndex()!;
    const max = Math.max(
      1e-9,
      ...ranges.map((r: any) => r.curvature_max_per_mm),
    );
    for (const r of ranges) {
      const c = new THREE.Color().setHSL(
        0.62 - 0.62 * (r.curvature_max_per_mm / max),
        0.65,
        0.48,
      );
      for (
        let tri = r.first_triangle;
        tri < r.first_triangle + r.triangle_count;
        tri++
      )
        for (let k = 0; k < 3; k++) {
          const v = index.getX(tri * 3 + k);
          colors[v * 3] = c.r;
          colors[v * 3 + 1] = c.g;
          colors[v * 3 + 2] = c.b;
        }
    }
    geometry.setAttribute("color", new THREE.BufferAttribute(colors, 3));
    return new THREE.MeshBasicMaterial({ vertexColors: true, ...common });
  }
  return new THREE.MeshBasicMaterial({
    color: candidate ? 0xd38a63 : 0x91a58b,
    ...common,
  });
}
function draw(data: any, group = mainGroup, ghost = false) {
  clearGroup(group);
  for (const m of data.meshes) {
    const local = relativePositions(m.vertices);
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute(
      "position",
      new THREE.BufferAttribute(local.positions, 3),
    );
    geometry.setIndex(m.triangles.flat());
    geometry.computeVertexNormals();
    const material = materialFor(m, ghost, geometry);
    const mesh = new THREE.Mesh(geometry, material);
    mesh.position.fromArray(local.origin);
    mesh.userData.origin = local.origin;
    mesh.userData.coordinate_rounding_mm = local.maxCoordinateError;
    mesh.userData.feature_id = m.feature_id;
    mesh.userData.face_ranges = m.face_ranges;
    group.add(mesh);
    if (!ghost && exploded) mesh.position.x += (group.children.length - 1) * 10;
  }
  if (ghost) beforeData = data;
  else {
    meshData = data;
    el("empty-state").hidden = true;
    text(
      "triangle-count",
      `${data.meshes.reduce((n: number, m: any) => n + m.triangles.length, 0).toLocaleString("en-US")} triangles${data.clip ? " · excerpt" : ""}`,
    );
    const r = data.resolution;
    text(
      "resolution",
      r
        ? `Preview · resolution ≤ ${r.absolute_resolution_mm.toPrecision(2)} mm · smallest resolved feature ≈ ${r.minimum_feature_resolved_mm.toPrecision(2)} mm · no surface proof`
        : `Preview · requested deviation ${data.meshes[0]?.deflection?.toPrecision(3) ?? "?"} mm · no surface proof`,
    );
  }
}
function redraw() {
  if (meshData) draw(meshData);
  if (beforeData && candidate) draw(beforeData, beforeGroup, true);
}
/** Scale bar from the camera's current millimetres per pixel (Bauplan 19.2). */
function updateScaleBar() {
  const h = renderer.domElement.clientHeight || 1,
    w = renderer.domElement.clientWidth || 1;
  currentMmPerPixel =
    activeCamera === camera
      ? (2 *
          camera.position.distanceTo(controls.target) *
          Math.tan(THREE.MathUtils.degToRad(camera.fov / 2))) /
        h
      : (orthoCamera.right - orthoCamera.left) / orthoCamera.zoom / w;
  if (!Number.isFinite(currentMmPerPixel) || currentMmPerPixel <= 0) return;
  const target = 90 * currentMmPerPixel,
    decade = 10 ** Math.floor(Math.log10(target));
  const length = [1, 2, 5, 10]
    .map((k) => k * decade)
    .reduce(
      (best, k) => (Math.abs(k - target) < Math.abs(best - target) ? k : best),
      decade,
    );
  const px = length / currentMmPerPixel;
  if (Math.abs(px - lastScalePx) < 0.5) return;
  lastScalePx = px;
  el("scale-bar-line").style.width = px.toFixed(1) + "px";
  text(
    "scale-bar-text",
    `${Number(length.toPrecision(3))} mm · ${currentMmPerPixel.toPrecision(2)} mm/px`,
  );
}
/** Protected and change regions of the selected feature in world coordinates (Bauplan 19.2). */
function drawRegions() {
  clearGroup(regionGroup);
  el("regions").replaceChildren();
  if (!selected) return;
  const placement = selected.frame_to_world;
  const toWorld = (p: number[]) => {
    const r = placement.rotation,
      t = placement.translation;
    return new THREE.Vector3(
      r[0][0] * p[0] + r[0][1] * p[1] + r[0][2] * p[2] + t[0],
      r[1][0] * p[0] + r[1][1] * p[1] + r[1][2] * p[2] + t[1],
      r[2][0] * p[0] + r[2][1] * p[1] + r[2][2] * p[2] + t[2],
    );
  };
  const notes: string[] = [];
  for (const c of selected.protected_constraints) {
    if (c.kind === "protected_region") {
      const min = c.min.map(Number),
        max = c.max.map(Number);
      const corners = [0, 1, 2, 3, 4, 5, 6, 7].map((i) =>
        toWorld([
          i & 1 ? max[0] : min[0],
          i & 2 ? max[1] : min[1],
          i & 4 ? max[2] : min[2],
        ]),
      );
      const center = corners
        .reduce((a, b) => a.add(b), new THREE.Vector3())
        .multiplyScalar(1 / 8);
      const edges = [
        0, 1, 1, 3, 3, 2, 2, 0, 4, 5, 5, 7, 7, 6, 6, 4, 0, 4, 1, 5, 2, 6, 3, 7,
      ];
      const geometry = new THREE.BufferGeometry().setFromPoints(
        edges.map((i) => corners[i].clone().sub(center)),
      );
      const lines = new THREE.LineSegments(
        geometry,
        new THREE.LineBasicMaterial({ color: 0x386957 }),
      );
      lines.position.copy(center);
      regionGroup.add(lines);
      notes.push(
        `⌑ Protected region (box in frame ${selected.local_frame}): ${(max[0] - min[0]).toPrecision(3)} × ${(max[1] - min[1]).toPrecision(3)} × ${(max[2] - min[2]).toPrecision(3)} mm`,
      );
    } else if (c.kind === "change_region") {
      if (c.local_frame && c.local_frame !== selected.local_frame) {
        notes.push(
          `◌ Change region in another frame (${c.local_frame}), not drawn`,
        );
        continue;
      }
      const radius = Number(c.radius);
      const sphere = new THREE.Mesh(
        new THREE.SphereGeometry(radius, 24, 16),
        new THREE.MeshBasicMaterial({
          color: 0xd55c37,
          wireframe: true,
          transparent: true,
          opacity: 0.35,
        }),
      );
      sphere.position.copy(toWorld(c.center.map(Number)));
      regionGroup.add(sphere);
      notes.push(
        `◌ Change region: sphere r = ${radius.toPrecision(3)} mm${c.compute_margin ? ` · compute margin ${Number(c.compute_margin).toPrecision(3)} mm` : ""}`,
      );
    }
  }
  el("regions").textContent = notes.join(" · ");
}
function fit() {
  const box = new THREE.Box3().setFromObject(mainGroup);
  if (box.isEmpty()) return;
  const size = box.getSize(new THREE.Vector3()),
    center = box.getCenter(new THREE.Vector3()),
    radius = Math.max(size.length(), 1);
  controls.target.copy(center);
  activeCamera.position
    .copy(center)
    .add(new THREE.Vector3(0.9, -1.15, 0.9).multiplyScalar(radius));
  activeCamera.near = Math.max(radius / 10000, 1e-5);
  activeCamera.far = radius * 100;
  activeCamera.updateProjectionMatrix();
  orthoCamera.zoom = 50 / radius;
  orthoCamera.updateProjectionMatrix();
  grid.position.set(center.x, center.y, box.min.z - 0.1);
  controls.update();
}
const resize = () => {
  const w = viewport.clientWidth,
    h = viewport.clientHeight;
  renderer.setSize(w, h);
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
  orthoCamera.left = (-35 * w) / h;
  orthoCamera.right = (35 * w) / h;
  orthoCamera.updateProjectionMatrix();
};
new ResizeObserver(resize).observe(viewport);
renderer.setAnimationLoop(() => {
  controls.update();
  updateScaleBar();
  renderer.render(scene, activeCamera);
});
el("fit").onclick = fit;
el("ortho").onclick = () => {
  const next = activeCamera === camera ? orthoCamera : camera;
  next.position.copy(activeCamera.position);
  next.quaternion.copy(activeCamera.quaternion);
  activeCamera = next;
  controls.object = activeCamera;
  el("ortho").classList.toggle("active", activeCamera === orthoCamera);
  text(
    "view-label",
    activeCamera === orthoCamera ? "ORTHOGRAPHIC · Z ↑" : "PERSPECTIVE · Z ↑",
  );
  resize();
};
el("wireframe").onclick = () => {
  wireframe = !wireframe;
  el("wireframe").classList.toggle("active", wireframe);
  for (const group of [mainGroup, beforeGroup])
    for (const m of group.children)
      (
        m as THREE.Mesh<THREE.BufferGeometry, THREE.MeshStandardMaterial>
      ).material.wireframe = wireframe;
};
el("section").onclick = () => {
  section = !section;
  el("section").classList.toggle("active", section);
  const center = new THREE.Box3()
    .setFromObject(mainGroup)
    .getCenter(new THREE.Vector3());
  clipping.constant = center.y;
  for (const group of [mainGroup, beforeGroup])
    for (const m of group.children)
      (
        m as THREE.Mesh<THREE.BufferGeometry, THREE.MeshStandardMaterial>
      ).material.clippingPlanes = section ? [clipping] : [];
};
el("measure").onclick = () => {
  measuring = !measuring;
  measurementPoints.length = 0;
  clearGroup(markerGroup);
  el("measure").classList.toggle("active", measuring);
  el("measurement").hidden = !measuring;
  text("measurement", "Select two points on the surface.");
};
/** Pixel-size LOD (Bauplan 19.4): the target deflection follows the current screen resolution and,
 *  for models larger than the view, only the region around the view target is refined. */
async function lodRender() {
  if (!current || !displayRevision) return;
  lodTarget = Math.min(0.2, Math.max(0.005, currentMmPerPixel / 2));
  const visible =
    activeCamera === camera
      ? camera.position.distanceTo(controls.target) *
        Math.tan(THREE.MathUtils.degToRad(camera.fov / 2)) *
        1.6
      : ((orthoCamera.right - orthoCamera.left) / orthoCamera.zoom) * 0.8;
  const box = new THREE.Box3().setFromObject(mainGroup);
  const region =
    !box.isEmpty() && box.getSize(new THREE.Vector3()).length() > visible * 2
      ? {
          center: controls.target.toArray().map((v) => v.toFixed(3)),
          radius: Math.max(visible, 1e-3).toFixed(3),
        }
      : undefined;
  busy(`Refining the preview to ${lodTarget.toPrecision(2)} mm …`);
  try {
    await preview(displayRevision, undefined, false, {
      adaptive: true,
      region,
    });
  } finally {
    busy("", false);
  }
  activity(
    "Pixel LOD",
    `Target deviation ${lodTarget.toPrecision(2)} mm from ${currentMmPerPixel.toPrecision(2)} mm/px${region ? " · excerpt around the view centre" : ""} · no surface proof.`,
  );
}
el("lod").onclick = action(lodRender);
el<HTMLSelectElement>("overlay").onchange = action(async () => {
  overlayMode = el<HTMLSelectElement>("overlay").value as OverlayMode;
  try {
    localStorage.setItem("mathforge.overlay", overlayMode);
  } catch {
    // Browserspeicher blockiert: die Wahl gilt nur für diese Sitzung.
  }
  renderLegend();
  const hasCurvature = meshData?.meshes.some((m: any) =>
    m.face_ranges?.some((r: any) => r.curvature_max_per_mm != null),
  );
  if (overlayMode === "curvature" && meshData && !hasCurvature) {
    toast(
      "The curvature channel shows the native face curvature of the adaptive preview; it is being computed now.",
    );
    await lodRender();
  } else redraw();
});
try {
  const stored = localStorage.getItem(
    "mathforge.overlay",
  ) as OverlayMode | null;
  if (stored && OVERLAY_MODES.includes(stored)) overlayMode = stored;
} catch {
  // Browserspeicher blockiert: Standardkanal bleibt "Bauteile".
}
el<HTMLSelectElement>("overlay").value = overlayMode;
el("explode").onclick = () => {
  exploded = !exploded;
  el("explode").classList.toggle("active", exploded);
  mainGroup.children.forEach((m, i) => {
    m.position.x = m.userData.origin[0] + (exploded ? i * 10 : 0);
  });
  if (mainGroup.children.length < 2)
    toast(
      "This model has a single output object; only several output parts can be exploded.",
    );
};
async function preview(
  revision: string,
  featureID?: string,
  ghost = false,
  lod: { adaptive: boolean; region?: { center: string[]; radius: string } } = {
    adaptive: false,
  },
) {
  const result = await job(
    await tool("cad_render", {
      model_id: current.model_id,
      revision,
      idempotency_key: key(),
      feature_id: featureID,
      deflection: { value: "0.02", unit: "mm" },
      ...(lod.adaptive
        ? {
            adaptive: {
              target_error: { value: lodTarget.toFixed(4), unit: "mm" },
              feature_factor: "0.5",
            },
          }
        : {}),
      ...(lod.region ? { region: lod.region } : {}),
    }),
  );
  const a = result.artifacts.find(
    (a: any) => a.manifest.filename === "preview.json",
  );
  if (!a) throw new Error("Preview artifact missing.");
  const data = await api(a.download);
  draw(data, ghost ? beforeGroup : mainGroup, ghost);
}
function resetCandidate() {
  candidate = null;
  validation = null;
  el<HTMLButtonElement>("validate").disabled = true;
  el<HTMLButtonElement>("commit").disabled = true;
  el("discard").hidden = true;
  for (const step of ["candidate", "validation", "commit"])
    el("step-" + step).classList.remove("done");
  text("validation-summary", "No pending change.");
  clearGroup(beforeGroup);
  beforeData = null;
}
async function selectPart(part: any) {
  document
    .querySelectorAll(".structure .part")
    .forEach((b) =>
      b.classList.toggle(
        "selected",
        (b as HTMLElement).dataset.part === part.entity_id,
      ),
    );
  document
    .querySelectorAll<HTMLElement>(".feature")
    .forEach(
      (b) => (b.hidden = !!b.dataset.part && b.dataset.part !== part.entity_id),
    );
  const first =
    part.definition.outputs?.[0] ??
    current?.features.find((f: any) => f.owner_part === part.entity_id)?.id;
  if (first) await selectFeature(first);
  else toast("This part has no features yet.");
}
/** Part and assembly tree from cad_structure (Bauplan 19.2). */
async function structureTree() {
  const container = el("structure");
  container.replaceChildren();
  const fetchAll = async (kind: string) => {
    const entries: any[] = [];
    let offset: number | null = 0;
    while (offset !== null) {
      const page = await tool("cad_structure", {
        model_id: current.model_id,
        revision: current.revision,
        kind,
        offset,
        limit: 16,
      });
      entries.push(...page.entries);
      offset = page.next_offset;
    }
    return entries;
  };
  const [assemblies, parts] = await Promise.all([
    fetchAll("assembly"),
    fetchAll("part"),
  ]);
  partNames.clear();
  for (const p of parts) partNames.set(p.entity_id, p.semantic_name);
  assignGroupColours();
  renderLegend();
  text(
    "structure-count",
    `${assemblies.length} assemblies · ${parts.length} parts`,
  );
  const children = new Map<string | undefined, any[]>();
  for (const a of assemblies) {
    const parent = a.definition.parent_assembly;
    children.set(parent, [...(children.get(parent) ?? []), a]);
  }
  const render = (parent: string | undefined, depth: number): HTMLElement[] => [
    ...(children.get(parent) ?? []).map((a) => {
      const details = document.createElement("details");
      details.open = depth < 2;
      const summary = document.createElement("summary");
      summary.textContent = a.semantic_name;
      const small = document.createElement("small");
      small.textContent = `${a.part_count} parts · ${a.definition.local_frame}`;
      summary.append(small);
      details.append(summary, ...render(a.entity_id, depth + 1));
      return details;
    }),
    ...parts
      .filter((p) => p.definition.assembly === parent)
      .map((p) => {
        const button = document.createElement("button");
        button.className = "part";
        button.dataset.part = p.entity_id;
        button.textContent = p.semantic_name;
        const small = document.createElement("small");
        small.textContent = `${p.definition.authoritative_representation} · ${p.feature_count} features`;
        button.append(small);
        button.onclick = action(async () => selectPart(p));
        return button;
      }),
  ];
  container.append(...render(undefined, 0));
  if (!container.children.length) container.textContent = "No structure.";
}
async function openModel(modelID: string, fitView = true) {
  busy("Loading model and geometry …");
  try {
    current = await tool("cad_get_model", { model_id: modelID, limit: 64 });
    displayRevision = current.revision;
    selected = null;
    resetCandidate();
    text("model-title", current.name);
    text("breadcrumb-model", current.name);
    text("model-meta", `${current.feature_count} features · millimetres`);
    text(
      "quality",
      current.quality === "checks_passed_within_profile"
        ? "Verified revision"
        : "Entwurf",
    );
    el("quality").classList.toggle(
      "preview",
      current.quality !== "checks_passed_within_profile",
    );
    text("revision", current.revision.slice(0, 12));
    text("feature-count", String(current.feature_count));
    el("features").replaceChildren();
    let allFeatures = [...current.features];
    let offset = current.next_offset;
    while (offset !== null) {
      const page = await tool("cad_get_model", {
        model_id: modelID,
        revision: current.revision,
        offset,
        limit: 64,
      });
      allFeatures.push(...page.features);
      offset = page.next_offset;
    }
    current.features = allFeatures;
    for (const f of allFeatures) {
      const button = document.createElement("button");
      button.className = "feature";
      button.dataset.feature = f.id;
      const symbol = document.createElement("span");
      symbol.className = "feature-symbol";
      symbol.textContent =
        f.kind === "groove"
          ? "◎"
          : f.kind === "hole"
            ? "⊙"
            : f.kind === "field"
              ? "∿"
              : "◇";
      button.dataset.part = f.owner_part;
      const content = document.createElement("div");
      content.textContent = f.semantic_name;
      const small = document.createElement("small");
      small.textContent = f.operator + " · " + f.representation;
      content.append(small);
      button.append(symbol, content);
      button.onclick = action(async () => selectFeature(f.id));
      el("features").append(button);
    }
    clearGroup(regionGroup);
    clearGroup(markerGroup);
    await structureTree();
    el("detail-body").hidden = true;
    el("empty-state").hidden = !!current.features.length;
    if (current.features.length) {
      await preview(current.revision);
      if (fitView) fit();
      await selectFeature(
        current.features.find((f: any) => f.kind === "groove")?.id ??
          current.features[0].id,
      );
    } else clearGroup(mainGroup);
    await models();
    activity("Revision loaded", current.revision);
  } finally {
    busy("", false);
  }
}
async function selectFeature(
  featureID: string,
  faceID?: string,
  anchor?: Record<string, unknown>,
) {
  if (!current) return;
  selected = await tool("cad_inspect", {
    model_id: current.model_id,
    revision: displayRevision ?? current.revision,
    feature_id: featureID,
    ...(faceID ? { face_id: faceID } : {}),
    ...(anchor ? { anchor } : {}),
  });
  const a = selected.selection_anchor;
  text(
    "anchor",
    a
      ? `Anchor: (${a.point_mm.map((v: number) => v.toFixed(3)).join(", ")}) mm · face ${a.face_id.slice(0, 13)} · frame ${a.local_frame}${a.barycentric ? ` · baryzentrisch (${a.barycentric.map((v: number) => v.toFixed(2)).join(", ")})` : ""}${a.view_relative?.facing_camera != null ? (a.view_relative.facing_camera ? " · facing the camera" : " · facing away from the camera") : ""}`
      : "",
  );
  featureID = selected.selected_entities[0];
  document
    .querySelectorAll(".feature")
    .forEach((b) =>
      b.classList.toggle(
        "selected",
        (b as HTMLElement).dataset.feature === featureID,
      ),
    );
  text(
    "detail-name",
    current.features.find((f: any) => f.id === featureID)?.semantic_name ??
      featureID,
  );
  text(
    "detail-purpose",
    selected.selected_face
      ? `Face: ${selected.selected_face.origins.map((o: any) => o.role).join(", ")} · ${selected.selected_face.area.toLocaleString("en-US", { maximumFractionDigits: 3 })} mm² · provenance verified`
      : (selected.purpose?.value ??
          `${selected.construction_summary.operator} · ${selected.local_frame} · stable feature ID`),
  );
  el("detail-body").hidden = false;
  el("parameters").replaceChildren();
  for (const [name, q] of Object.entries<any>(selected.parameters)) {
    const label = document.createElement("label");
    label.className = "parameter";
    const caption = document.createElement("span");
    caption.textContent = labels[name] ?? name;
    const input = document.createElement("input");
    input.value = q.value;
    input.dataset.parameter = name;
    input.setAttribute("aria-label", labels[name] ?? name);
    input.pattern = "-?(0|[1-9][0-9]*)(\\.[0-9]+)?";
    input.disabled =
      !!selected.expressions?.[name] ||
      selected.protected_constraints.some(
        (c: any) => c.kind === "protected_parameter" && c.parameter === name,
      ) ||
      !!candidate;
    const unit = document.createElement("span");
    unit.textContent = q.unit;
    label.append(caption, input, unit);
    el("parameters").append(label);
  }
  el("measurements").replaceChildren();
  const measurements = { ...selected.known_facts?.dimensions };
  if (selected.known_facts?.volume != null)
    measurements.volume = selected.known_facts.volume;
  for (const [name, value] of Object.entries<any>(measurements)) {
    const row = document.createElement("div");
    const caption = document.createElement("span");
    caption.textContent = labels[name] ?? name;
    const v = document.createElement("strong");
    v.textContent =
      Number(value).toLocaleString("en-US", { maximumFractionDigits: 5 }) +
      (name === "volume" ? " mm³" : " mm");
    row.append(caption, v);
    el("measurements").append(row);
  }
  text(
    "protections",
    selected.protected_constraints.length
      ? "⌑ " +
          selected.protected_constraints
            .map((c: any) =>
              c.kind === "protected_parameter"
                ? `${labels[c.parameter] ?? c.parameter} protected`
                : c.kind === "minimum"
                  ? `Remaining wall ≥ ${c.target.value} mm`
                  : c.kind === "protected_region"
                    ? "Remote region protected"
                    : c.id,
            )
            .join(" · ")
      : "",
  );
  drawRegions();
}
el<HTMLSelectElement>("model-select").onchange = action(async () =>
  openModel(el<HTMLSelectElement>("model-select").value),
);
el("isolate").onclick = action(async () => {
  if (!selected) return;
  busy("Isolating the feature …");
  await preview(displayRevision!, selected.selected_entities[0]);
  fit();
  busy("", false);
});
async function readyDraft(draft: any) {
  candidate = { ...draft, base_revision: current.revision };
  validation = null;
  const result = await job(draft);
  candidate.candidate_revision = result.candidate_revision;
  displayRevision = result.candidate_revision;
  el("step-candidate").classList.add("done");
  el<HTMLButtonElement>("validate").disabled = false;
  el("discard").hidden = false;
  text(
    "validation-summary",
    "Candidate computed; mandatory checks pending.",
  );
  text("quality", "Candidate · unchecked");
  el("quality").classList.add("preview");
  await preview(current.revision, undefined, true);
  await preview(displayRevision!);
  if (selected) await selectFeature(selected.selected_entities[0]);
  activity(
    "Candidate ready",
    "Orange: candidate · transparent overlay: base revision.",
  );
}
el("stage-edit").onclick = action(async () => {
  if (!selected || candidate) return;
  const operations = [];
  for (const input of el("parameters").querySelectorAll<HTMLInputElement>(
    "input",
  )) {
    const name = input.dataset.parameter!;
    if (input.value !== selected.parameters[name].value)
      operations.push({
        op: "set_parameter",
        feature_id: selected.selected_entities[0],
        parameter: name,
        expected: selected.parameters[name],
        value: {
          value: input.value.replace(",", "."),
          unit: selected.parameters[name].unit,
        },
      });
  }
  if (!operations.length) {
    toast("Change an editable parameter first.");
    return;
  }
  busy("Computing the local change …");
  const binding = {
    model_id: current.model_id,
    base_revision: current.revision,
    operations,
    selection_handle: selected.selection_handle,
  };
  await tool("cad_plan_edit", { ...binding, idempotency_key: key() });
  await readyDraft(
    await tool("cad_apply_patch", { ...binding, idempotency_key: key() }),
  );
  busy("", false);
});
el("validate").onclick = action(async () => {
  if (!candidate) return;
  busy("Checking dimensions, geometry and protections …");
  const result = await job(
    await tool("cad_validate", {
      model_id: current.model_id,
      base_revision: candidate.base_revision,
      transaction_id: candidate.transaction_id,
      idempotency_key: key(),
    }),
  );
  validation = result;
  if (result.status === "checks_passed_within_profile") {
    el("step-validation").classList.add("done");
    el<HTMLButtonElement>("commit").disabled = false;
    text(
      "validation-summary",
      `${result.check_count ?? result.checks.length} mandatory checks passed; the candidate can be committed.`,
    );
  } else {
    text(
      "validation-summary",
      "Validation failed: " +
        result.checks.map((c: any) => c.check_id).join(", "),
    );
    el<HTMLButtonElement>("commit").disabled = true;
  }
  busy("", false);
});
el("commit").onclick = action(async () => {
  if (!validation || !candidate) return;
  busy("Committing the verified revision …");
  const result = await tool("cad_commit", {
    model_id: current.model_id,
    base_revision: candidate.base_revision,
    transaction_id: candidate.transaction_id,
    validation_digest: validation.digest,
    idempotency_key: key(),
  });
  await openModel(current.model_id, false);
  el("step-commit").classList.add("done");
  activity(
    "Change committed",
    `New revision ${result.revision.slice(0, 16)} · evidence stored in the model.`,
  );
  toast("The verified change has been committed.");
  busy("", false);
});
el("discard").onclick = action(async () => {
  if (!candidate) return;
  await tool("cad_discard", {
    model_id: current.model_id,
    base_revision: candidate.base_revision,
    transaction_id: candidate.transaction_id,
    idempotency_key: key(),
  });
  await openModel(current.model_id, false);
});
async function create(name: string, purpose = "", profile = "precision_cad") {
  const m = await tool("cad_create_model", {
    name,
    purpose,
    profile,
    idempotency_key: key(),
  });
  await openModel(m.model_id);
  return m;
}
el("new-model").onclick = () => el<HTMLDialogElement>("new-dialog").showModal();
document
  .querySelectorAll<HTMLElement>("[data-close]")
  .forEach(
    (b) => (b.onclick = () => el<HTMLDialogElement>(b.dataset.close!).close()),
  );
el("new-form").addEventListener("submit", (event) => {
  event.preventDefault();
  void action(async () => {
    el<HTMLDialogElement>("new-dialog").close();
    await create(
      el<HTMLInputElement>("new-name").value,
      el<HTMLInputElement>("new-purpose").value,
    );
  })();
});
async function loadExample(name: string) {
  busy("Building the example model …");
  const ir = await api("/api/examples/" + name);
  const title =
    {
      housing: "Seal housing",
      organic: "Implicit body",
      assembly: "Pin row",
    }[name] ?? "Example";
  const m = await create(title, "Mathematical reference model", ir.profile);
  const uploaded = await api("/api/uploads", {
    method: "POST",
    headers: { "Content-Type": "application/octet-stream" },
    body: JSON.stringify(ir),
  });
  const draft = await tool("cad_import", {
    model_id: m.model_id,
    base_revision: m.revision,
    artifact_id: uploaded.artifact_id,
    format: "ir",
    source_unit: "mm",
    idempotency_key: key(),
  });
  await job(draft);
  const v = await job(
    await tool("cad_validate", {
      model_id: m.model_id,
      base_revision: m.revision,
      transaction_id: draft.transaction_id,
      idempotency_key: key(),
    }),
  );
  if (v.status !== "checks_passed_within_profile")
    throw new Error("Example validation failed.");
  await tool("cad_commit", {
    model_id: m.model_id,
    base_revision: m.revision,
    transaction_id: draft.transaction_id,
    validation_digest: v.digest,
    idempotency_key: key(),
  });
  await openModel(m.model_id);
  busy("", false);
}
document
  .querySelectorAll<HTMLElement>("[data-example]")
  .forEach(
    (b) => (b.onclick = action(async () => loadExample(b.dataset.example!))),
  );
el("start-demo").onclick = action(async () => loadExample("housing"));
const primitiveParameters: Record<string, Record<string, string>> = {
  box: { width: "20", depth: "20", height: "10" },
  sphere: { radius: "10" },
  cylinder: { radius: "5", height: "20" },
  torus: { major_radius: "10", minor_radius: "2" },
};
function primitiveForm() {
  const params =
    primitiveParameters[el<HTMLSelectElement>("primitive-type").value];
  el("primitive-parameters").replaceChildren();
  for (const [name, value] of Object.entries(params)) {
    const label = document.createElement("label");
    label.textContent = (labels[name] ?? name) + " (mm)";
    const input = document.createElement("input");
    input.dataset.parameter = name;
    input.value = value;
    input.required = true;
    el("primitive-parameters").append(label, input);
  }
}
el("add-feature").onclick = () => {
  if (!current) {
    el<HTMLDialogElement>("new-dialog").showModal();
    return;
  }
  if (candidate) {
    toast("Commit or discard the open candidate first.");
    return;
  }
  primitiveForm();
  el<HTMLDialogElement>("primitive-dialog").showModal();
};
el<HTMLSelectElement>("primitive-type").onchange = primitiveForm;
el("primitive-form").addEventListener("submit", (event) => {
  event.preventDefault();
  void action(async () => {
    const operator = el<HTMLSelectElement>("primitive-type").value,
      parameters: Record<string, unknown> = {};
    el("primitive-parameters")
      .querySelectorAll<HTMLInputElement>("input")
      .forEach(
        (i) =>
          (parameters[i.dataset.parameter!] = {
            value: i.value.replace(",", "."),
            unit: "mm",
          }),
      );
    const fid = "feat-" + key();
    el<HTMLDialogElement>("primitive-dialog").close();
    busy("Building the primitive …");
    await readyDraft(
      await tool("cad_apply_patch", {
        model_id: current.model_id,
        base_revision: current.revision,
        idempotency_key: key(),
        operations: [
          {
            op: "add_feature",
            feature: {
              id: fid,
              kind: operator,
              semantic_name:
                el<HTMLSelectElement>("primitive-type").selectedOptions[0]
                  .textContent,
              parameters,
              construction: { operator },
            },
          },
          { op: "set_outputs", outputs: [...current.outputs, fid] },
        ],
      }),
    );
    fit();
    busy("", false);
  })();
});
el("export").onclick = action(async () => {
  if (!current) return;
  busy("Exporting and re-checking the geometry …");
  const result = await job(
    await tool("cad_export", {
      model_id: current.model_id,
      revision: current.revision,
      format: el<HTMLSelectElement>("export-format").value,
      idempotency_key: key(),
    }),
  );
  el("downloads").replaceChildren();
  for (const a of result.artifacts) {
    const link = document.createElement("a");
    link.textContent =
      "↓ " +
      (a.manifest.filename ??
        el<HTMLSelectElement>("export-format").value.toUpperCase()) +
      " · " +
      (a.size / 1024).toFixed(1) +
      " KB";
    link.href = a.download;
    link.download = a.manifest.filename ?? "mathforge-export";
    if (bearer)
      link.onclick = async (e) => {
        e.preventDefault();
        const r = await fetch(a.download, { headers: headers() });
        const url = URL.createObjectURL(await r.blob());
        const l = document.createElement("a");
        l.href = url;
        l.download = link.download;
        l.click();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
      };
    el("downloads").append(link);
  }
  busy("", false);
  activity(
    "Export verified",
    "File, units and round-trip report are available.",
  );
});
el<HTMLInputElement>("upload").onchange = action(async () => {
  const file = el<HTMLInputElement>("upload").files?.[0];
  if (!file) return;
  if (!current || current.feature_count)
    await create(
      file.name,
      "Imported geometry",
      file.name.toLowerCase().endsWith(".stl")
        ? "render_surface"
        : "precision_cad",
    );
  busy("Importing the file in isolation …");
  const uploaded = await api("/api/uploads", {
    method: "POST",
    headers: { "Content-Type": "application/octet-stream" },
    body: file,
  });
  const format = file.name.endsWith(".json")
    ? "ir"
    : file.name.endsWith(".stl")
      ? "stl"
      : "step";
  await readyDraft(
    await tool("cad_import", {
      model_id: current.model_id,
      base_revision: current.revision,
      artifact_id: uploaded.artifact_id,
      format,
      source_unit: el<HTMLSelectElement>("import-unit").value,
      idempotency_key: key(),
    }),
  );
  fit();
  busy("", false);
});
async function initialize() {
  const list = await models();
  if (list.length) await openModel(list[0].id);
}
void action(async () => {
  const config = await api("/api/config");
  authMode = config.auth_mode;
  text(
    "connection",
    authMode === "local" ? "● Local · private" : "● OAuth · private",
  );
  if (authMode === "oauth") {
    el("login-form").querySelector("p")!.textContent =
      "Enter a valid access token from your identity provider. It stays in memory for this tab only.";
  }
  // cad_viewer_open hands over a single-use login code and optionally a model in the
  // URL fragment; the fragment never reaches the server log and is removed at once.
  const handover = new URLSearchParams(location.hash.replace(/^#/, ""));
  const code = handover.get("code"),
    preselect = handover.get("model");
  if (code || preselect)
    history.replaceState(null, "", location.pathname + location.search);
  if (code && authMode === "local") {
    try {
      await api("/api/session", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code }),
      });
    } catch {
      /* an expired code falls back to the login dialog below */
    }
  }
  try {
    await initialize();
  } catch {
    showLogin();
    return;
  }
  if (preselect)
    try {
      await openModel(preselect);
    } catch {
      /* unknown or foreign model: keep the first model open */
    }
})();
