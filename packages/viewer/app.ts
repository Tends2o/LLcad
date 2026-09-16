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
  width: "Breite",
  depth: "Tiefe",
  height: "Höhe",
  radius: "Radius",
  major_radius: "Ringradius",
  minor_radius: "Rohradius",
  x: "Position X",
  y: "Position Y",
  z: "Position Z",
  count: "Anzahl",
  dx: "Abstand X",
  dy: "Abstand Y",
  dz: "Abstand Z",
  remaining_wall: "Restwand (lokal)",
  volume: "Volumen",
  area: "Oberfläche",
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
    throw new Error("Bitte anmelden.");
  }
  const result = await response.json();
  if (!response.ok || result.status === "failed")
    throw new Error(
      result.errors?.map((e: any) => `${e.code}: ${e.message}`).join("; ") ||
        result.error?.message ||
        "Anfrage fehlgeschlagen.",
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
          : "Job abgebrochen.",
      );
    await new Promise((r) => setTimeout(r, delay));
    delay = Math.min(700, delay + 30);
  }
  throw new Error("Job läuft weiter. Status kann erneut abgerufen werden.");
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
type OverlayMode = "lit" | "unlit" | "normals" | "curvature";
let overlayMode: OverlayMode = "lit",
  lodTarget = 0.02,
  currentMmPerPixel = 0.1,
  lastScalePx = -1;
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
      text("measurement", "Zweiten Oberflächenpunkt auswählen …");
    else
      text(
        "measurement",
        `${measurementPoints[0].distanceTo(measurementPoints[1]).toFixed(4)} mm · Abstand auf der Vorschau`,
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
        "Für eine eindeutige Detailauswahl das Merkmal im Konstruktionsbaum wählen.",
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
      `${data.meshes.reduce((n: number, m: any) => n + m.triangles.length, 0).toLocaleString("de-DE")} Dreiecke${data.clip ? " · Ausschnitt" : ""}`,
    );
    const r = data.resolution;
    text(
      "resolution",
      r
        ? `Vorschau · Auflösung ≤ ${r.absolute_resolution_mm.toPrecision(2)} mm · kleinstes aufgelöstes Merkmal ≈ ${r.minimum_feature_resolved_mm.toPrecision(2)} mm · kein Flächennachweis`
        : `Vorschau · angeforderte Abweichung ${data.meshes[0]?.deflection?.toPrecision(3) ?? "?"} mm · kein Flächennachweis`,
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
        `⌑ Schutzregion (Box im Rahmen ${selected.local_frame}): ${(max[0] - min[0]).toPrecision(3)} × ${(max[1] - min[1]).toPrecision(3)} × ${(max[2] - min[2]).toPrecision(3)} mm`,
      );
    } else if (c.kind === "change_region") {
      if (c.local_frame && c.local_frame !== selected.local_frame) {
        notes.push(
          `◌ Änderungsregion in anderem Rahmen (${c.local_frame}), nicht eingezeichnet`,
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
        `◌ Änderungsregion: Kugel r = ${radius.toPrecision(3)} mm${c.compute_margin ? ` · Rechenrand ${Number(c.compute_margin).toPrecision(3)} mm` : ""}`,
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
    activeCamera === orthoCamera ? "ORTHOGRAFISCH · Z ↑" : "PERSPEKTIVE · Z ↑",
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
  text("measurement", "Zwei Punkte auf der Oberfläche auswählen.");
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
  busy(`Vorschau wird auf ${lodTarget.toPrecision(2)} mm verfeinert …`);
  try {
    await preview(displayRevision, undefined, false, {
      adaptive: true,
      region,
    });
  } finally {
    busy("", false);
  }
  activity(
    "Pixel-LOD",
    `Zielabweichung ${lodTarget.toPrecision(2)} mm aus ${currentMmPerPixel.toPrecision(2)} mm/px${region ? " · Ausschnitt um den Blickpunkt" : ""} · kein Flächennachweis.`,
  );
}
el("lod").onclick = action(lodRender);
el<HTMLSelectElement>("overlay").onchange = action(async () => {
  overlayMode = el<HTMLSelectElement>("overlay").value as OverlayMode;
  const hasCurvature = meshData?.meshes.some((m: any) =>
    m.face_ranges?.some((r: any) => r.curvature_max_per_mm != null),
  );
  if (overlayMode === "curvature" && meshData && !hasCurvature) {
    toast(
      "Der Krümmungskanal zeigt die native Flächenkrümmung der adaptiven Vorschau; sie wird jetzt berechnet.",
    );
    await lodRender();
  } else redraw();
});
el("explode").onclick = () => {
  exploded = !exploded;
  el("explode").classList.toggle("active", exploded);
  mainGroup.children.forEach((m, i) => {
    m.position.x = m.userData.origin[0] + (exploded ? i * 10 : 0);
  });
  if (mainGroup.children.length < 2)
    toast(
      "Dieses Modell besitzt ein Ausgabeobjekt. Mehrere Ausgabeteile lassen sich auseinanderziehen.",
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
  if (!a) throw new Error("Vorschauartefakt fehlt.");
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
  text("validation-summary", "Noch keine ausstehende Änderung.");
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
  else toast("Dieses Teil besitzt noch keine Merkmale.");
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
  text(
    "structure-count",
    `${assemblies.length} Baugruppen · ${parts.length} Teile`,
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
      small.textContent = `${a.part_count} Teile · ${a.definition.local_frame}`;
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
        small.textContent = `${p.definition.authoritative_representation} · ${p.feature_count} Merkmale`;
        button.append(small);
        button.onclick = action(async () => selectPart(p));
        return button;
      }),
  ];
  container.append(...render(undefined, 0));
  if (!container.children.length) container.textContent = "Keine Struktur.";
}
async function openModel(modelID: string, fitView = true) {
  busy("Modell und Geometrie werden geladen …");
  try {
    current = await tool("cad_get_model", { model_id: modelID, limit: 64 });
    displayRevision = current.revision;
    selected = null;
    resetCandidate();
    text("model-title", current.name);
    text("breadcrumb-model", current.name);
    text("model-meta", `${current.feature_count} Merkmale · Millimeter`);
    text(
      "quality",
      current.quality === "checks_passed_within_profile"
        ? "Geprüfte Revision"
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
    activity("Revision geladen", current.revision);
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
      ? `Anker: (${a.point_mm.map((v: number) => v.toFixed(3)).join(", ")}) mm · Fläche ${a.face_id.slice(0, 13)} · Rahmen ${a.local_frame}${a.barycentric ? ` · baryzentrisch (${a.barycentric.map((v: number) => v.toFixed(2)).join(", ")})` : ""}${a.view_relative?.facing_camera != null ? (a.view_relative.facing_camera ? " · der Kamera zugewandt" : " · von der Kamera abgewandt") : ""}`
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
      ? `Fläche: ${selected.selected_face.origins.map((o: any) => o.role).join(", ")} · ${selected.selected_face.area.toLocaleString("de-DE", { maximumFractionDigits: 3 })} mm² · Herkunft geprüft`
      : (selected.purpose?.value ??
          `${selected.construction_summary.operator} · ${selected.local_frame} · stabile Feature-ID`),
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
      Number(value).toLocaleString("de-DE", { maximumFractionDigits: 5 }) +
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
                ? `${labels[c.parameter] ?? c.parameter} geschützt`
                : c.kind === "minimum"
                  ? `Restwand ≥ ${c.target.value} mm`
                  : c.kind === "protected_region"
                    ? "Fernregion geschützt"
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
  busy("Merkmal wird isoliert …");
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
    "Kandidat berechnet. Pflichtprüfungen stehen aus.",
  );
  text("quality", "Kandidat · ungeprüft");
  el("quality").classList.add("preview");
  await preview(current.revision, undefined, true);
  await preview(displayRevision!);
  if (selected) await selectFeature(selected.selected_entities[0]);
  activity(
    "Kandidat bereit",
    "Orange: Kandidat · transparente Überlagerung: Basisrevision.",
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
    toast("Ändere zuerst einen freigegebenen Parameter.");
    return;
  }
  busy("Lokale Änderung wird berechnet …");
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
  busy("Maße, Geometrie und Schutzregeln werden geprüft …");
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
      `${result.check_count ?? result.checks.length} Pflichtprüfungen bestanden. Der Kandidat kann übernommen werden.`,
    );
  } else {
    text(
      "validation-summary",
      "Prüfung fehlgeschlagen: " +
        result.checks.map((c: any) => c.check_id).join(", "),
    );
    el<HTMLButtonElement>("commit").disabled = true;
  }
  busy("", false);
});
el("commit").onclick = action(async () => {
  if (!validation || !candidate) return;
  busy("Geprüfte Revision wird übernommen …");
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
    "Änderung übernommen",
    `Neue Revision ${result.revision.slice(0, 16)} · Nachweise im Modell gespeichert.`,
  );
  toast("Die geprüfte Änderung wurde übernommen.");
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
  busy("Beispielmodell wird konstruiert …");
  const ir = await api("/api/examples/" + name);
  const title =
    {
      housing: "Dichtungsgehäuse",
      organic: "Impliziter Körper",
      assembly: "Stiftreihe",
    }[name] ?? "Beispiel";
  const m = await create(title, "Mathematisches Referenzmodell", ir.profile);
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
    throw new Error("Beispielprüfung fehlgeschlagen.");
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
    toast("Übernimm oder verwirf zuerst den offenen Kandidaten.");
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
    busy("Grundkörper wird konstruiert …");
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
  busy("Export und erneute Geometrieprüfung …");
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
    "Export geprüft",
    "Datei, Maßeinheiten und Roundtrip-Bericht sind verfügbar.",
  );
});
el<HTMLInputElement>("upload").onchange = action(async () => {
  const file = el<HTMLInputElement>("upload").files?.[0];
  if (!file) return;
  if (!current || current.feature_count)
    await create(
      file.name,
      "Importierte Geometrie",
      file.name.toLowerCase().endsWith(".stl")
        ? "render_surface"
        : "precision_cad",
    );
  busy("Datei wird isoliert importiert …");
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
    authMode === "local" ? "● Lokal · privat" : "● OAuth · privat",
  );
  if (authMode === "oauth") {
    el("login-form").querySelector("p")!.textContent =
      "Gib ein gültiges Zugriffstoken deines Identitätsanbieters ein. Das Token bleibt nur für diesen Tab im Arbeitsspeicher.";
  }
  try {
    await initialize();
  } catch {
    showLogin();
  }
})();
