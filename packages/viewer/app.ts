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
/** The workspace list: what each model is for, and whether its purpose is
 *  still being written. */
let modelRows: any[] = [];
const orderStates = new Map<string, string>();
const WRITING = ["captured", "queued", "running"];
async function models() {
  const data = await api("/api/models");
  const listed = [...data.models];
  // A model that is open but kept out of the list still names itself here.
  if (current && !listed.some((m: any) => m.id === current.model_id))
    listed.push({
      id: current.model_id,
      name: `${current.name} · hidden`,
      purpose: "",
      purpose_order: null,
    });
  modelRows = listed;
  renderModels();
  announceOrders(listed);
  watchOrders();
  return listed;
}
function renderModels() {
  const list = el("model-list");
  if (!list) return;
  list.replaceChildren();
  for (const m of modelRows) {
    const order = m.purpose_order,
      busyWith = !!order && WRITING.includes(order.state),
      building = busyWith && order.kind === "model_build";
    const row = document.createElement("div");
    row.className = "model-row";
    row.dataset.model = m.id;
    if (busyWith) row.dataset.writing = "true";
    row.classList.toggle("open", current?.model_id === m.id);
    const open = document.createElement("button");
    open.type = "button";
    open.className = "model-open";
    const title = document.createElement("strong");
    title.textContent = m.name;
    const detail = document.createElement("small");
    detail.textContent = busyWith
      ? `${building ? "Building" : "Writing the plan"} … ${order.activity ?? ""}`.trim()
      : summarise(m.purpose) || "No plan yet";
    open.append(title, detail);
    open.onclick = action(async () => {
      await openModel(m.id);
    });
    row.append(open);
    if (busyWith) {
      const stop = document.createElement("button");
      stop.type = "button";
      stop.className = "model-stop";
      stop.textContent = "Cancel";
      stop.title = building
        ? "Stop building. What has been committed so far stays."
        : "Stop writing the plan. The model stays, with the words you typed.";
      stop.onclick = action(async () => {
        await api(`/api/annotations/${order.annotation_id}/cancel`, {
          method: "POST",
        });
        await models();
      });
      row.append(stop);
    } else if (buildable(m)) {
      const again = !!m.features;
      const build = document.createElement("button");
      build.type = "button";
      build.className = "model-stop model-build";
      build.title = again
        ? "Carry on building from what is already committed"
        : "Hand the plan to the AI and let it construct the model";
      arming(build, again ? "Carry on" : "Build", () => buildModel(m));
      build.textContent = again ? "Carry on" : "Build";
      row.append(build);
    }
    list.append(row);
  }
  renderPlan(modelRows.find((m: any) => m.id === current?.model_id));
}
/** A model is worth building when it has a plan and no geometry yet — or when
 *  its last build stopped early, because then there is more to build. A run
 *  commits as it goes, so carrying on picks up where it left off. */
function buildable(m: any) {
  if (!m?.purpose) return false;
  const order = m.purpose_order;
  if (order && WRITING.includes(order.state)) return false;
  // Nothing built yet, or a build that has already run once: a plan is rarely
  // finished in one go, and the run itself says what it left out.
  return !m.features || order?.kind === "model_build";
}
/** The plan of the model on screen, and the button that turns it into one. */
function renderPlan(model: any) {
  const box = el("model-purpose");
  if (!box) return;
  box.replaceChildren();
  box.hidden = !model?.purpose;
  if (!model?.purpose) return;
  const heading = document.createElement("strong");
  heading.className = "plan-heading";
  heading.textContent = "Plan";
  const body = document.createElement("p");
  body.className = "plan-text";
  // The plan is written with **headings**; they are shown as headings, not as
  // asterisks — and as text nodes, never as markup.
  for (const [index, piece] of model.purpose.split(/\*\*/g).entries()) {
    if (!piece) continue;
    if (index % 2) {
      const strong = document.createElement("b");
      strong.textContent = piece;
      body.append(strong);
    } else body.append(document.createTextNode(piece));
  }
  box.append(heading, body);
  if (!buildable(model)) return;
  const again = !!model.features,
    label = again ? "Carry on building" : "Build this model";
  const build = document.createElement("button");
  build.type = "button";
  build.className = "plan-build primary";
  build.title = again
    ? "Carry on from what is already committed"
    : "Hand the plan to the AI and let it construct the model";
  arming(build, label, () => buildModel(model));
  build.textContent = label;
  box.append(build);
}
/** Hand the plan over. The run works through the llcad tools like any other
 *  client and commits what it has built as it goes. */
async function buildModel(model: any) {
  await api(`/api/models/${model.id}/build`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({}),
  });
  await models();
  toast(`“${model.name}” is being built. You can carry on.`);
}
/** The first line of a purpose, without its markup, for a single row. */
function summarise(purpose: string | null) {
  return (
    (purpose ?? "")
      .replace(/\*\*/g, "")
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean)[0]
      ?.slice(0, 140) ?? ""
  );
}
/** An order that was still running and is now done says so, wherever the
 *  person happens to be working. */
function announceOrders(list: any[]) {
  for (const m of list) {
    const order = m.purpose_order,
      state = order ? `${order.kind}:${order.state}` : "none",
      before = orderStates.get(m.id);
    orderStates.set(m.id, state);
    if (before === undefined || before === state) continue;
    if (!WRITING.some((s) => before.endsWith(":" + s))) continue;
    const building = before.startsWith("model_build");
    if (order?.state === "succeeded") {
      toast(
        building
          ? `“${m.name}” has been built.`
          : `The plan for “${m.name}” is ready.`,
      );
      // The geometry is there now; the open model shows it.
      if (building && current?.model_id === m.id)
        void openModel(m.id).catch(() => {});
    }
    if (order?.state === "failed")
      toast(
        building
          ? `“${m.name}” could not be built.`
          : `The plan for “${m.name}” could not be written.`,
      );
  }
}
let orderTimer2: number | null = null;
function watchOrders() {
  const busyWith = modelRows.some(
    (m: any) => m.purpose_order && WRITING.includes(m.purpose_order.state),
  );
  if (busyWith && orderTimer2 === null)
    orderTimer2 = window.setInterval(() => void models().catch(() => {}), 4000);
  if (!busyWith && orderTimer2 !== null) {
    clearInterval(orderTimer2);
    orderTimer2 = null;
  }
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
// Read-only handles for automation and visual tests; the UI never reads them.
(window as any).mathforgeViewer = {
  THREE,
  scene,
  showRevision: (revision: string) => showRevision(revision),
  states: () => [...shownStates.keys()],
  camera,
  orthoCamera,
  controls,
  renderer,
  mainGroup,
  beforeGroup,
};
/* --- Depth ---------------------------------------------------------------
A print, a laser mark or an etched symbol is a sheet a hundredth of a millimetre
thick lying on the part it belongs to: its back face is exactly coplanar with
that part's surface, and its front stands proud by less than a depth buffer can
tell apart — on a phone, which often has only 16 bits, by far less. So the
housing takes over the print, in patches that crawl as the model turns. Two
remedies work together: the planes below are fitted to what is on screen, which
buys back most of the precision, and each sheet is nudged towards the observer
by whole steps of whatever buffer it is drawn into, which settles the question
even when the buffer cannot tell the two surfaces apart at all. */
const DECAL_ASPECT = 40,
  DECAL_SHARE = 500;
const scratchSize = new THREE.Vector3();
/** Thin against its own width, and thin against the whole model: a printed mark,
 *  not a plate or a board that happens to be flat. */
function isDecal(geometry: THREE.BufferGeometry, limit: number) {
  if (!geometry.boundingBox) geometry.computeBoundingBox();
  const size = geometry.boundingBox!.getSize(scratchSize),
    thin = Math.min(size.x, size.y, size.z),
    wide = Math.max(size.x, size.y, size.z);
  return wide > 0 && thin <= limit && thin * DECAL_ASPECT <= wide;
}
/** Stacked prints — a white graphic on a coloured field — are ordered outward:
 *  each sheet is biased towards the observer by a couple of depth-buffer steps
 *  more than the sheet it lies on, so their order survives any buffer. A sheet
 *  also drops its back face, which lies exactly in the surface it rests on and
 *  would otherwise fight its own front. */
function orderDecals(group: THREE.Group, ghost = false) {
  const bounds = new THREE.Box3().setFromObject(group);
  if (bounds.isEmpty()) return;
  const centre = bounds.getCenter(new THREE.Vector3()),
    limit = bounds.getSize(scratchSize).length() / DECAL_SHARE,
    sheets: { mesh: THREE.Mesh; box: THREE.Box3; outward: number }[] = [];
  for (const mesh of group.children) {
    if (!(mesh instanceof THREE.Mesh)) continue;
    mesh.renderOrder = 0;
    const material = mesh.material as THREE.Material;
    material.polygonOffset = false;
    if (!isDecal(mesh.geometry, limit)) continue;
    const box = mesh.geometry.boundingBox!,
      size = box.getSize(scratchSize),
      thin = Math.min(size.x, size.y, size.z),
      axis = size.x === thin ? "x" : size.y === thin ? "y" : "z",
      position = box.getCenter(new THREE.Vector3()).add(mesh.position);
    sheets.push({
      mesh,
      // Sheets that rest on one another share a surface exactly, which rounding
      // turns into a hair's gap; the tolerance keeps them one stack.
      box: box.clone().translate(mesh.position).expandByScalar(limit),
      outward: Math.abs(position[axis] - centre[axis]),
    });
  }
  // Only sheets that actually lie on one another need to be told apart, so the
  // bias stays small: prints elsewhere on the model all count as the first layer.
  const stack = sheets.map((_, i) => i);
  const root = (i: number): number =>
    stack[i] === i ? i : (stack[i] = root(stack[i]));
  for (let i = 0; i < sheets.length; i++)
    for (let j = i + 1; j < sheets.length; j++)
      if (sheets[i].box.intersectsBox(sheets[j].box)) stack[root(j)] = root(i);
  const layers = new Map<number, typeof sheets>();
  for (const [i, sheet] of sheets.entries()) {
    const key = root(i);
    layers.set(key, [...(layers.get(key) ?? []), sheet]);
  }
  for (const group of layers.values()) {
    group.sort((a, b) => a.outward - b.outward);
    group.forEach((sheet, index) => {
      const rank = Math.min(index, 3),
        material = sheet.mesh.material as THREE.Material;
      sheet.mesh.renderOrder = rank + 1;
      material.polygonOffset = true;
      material.polygonOffsetFactor = -1;
      material.polygonOffsetUnits = -2 * (rank + 1);
      if (!ghost && material.side !== THREE.FrontSide) {
        material.side = THREE.FrontSide;
        material.needsUpdate = true;
      }
    });
  }
}
const depthBox = new THREE.Box3(),
  depthCentre = new THREE.Vector3(),
  depthCorner = new THREE.Vector3();
/** Depth resolution follows the near plane. Spread over a whole assembly it is
 *  far too coarse for its details, which is what made prints flicker while the
 *  model moved; fitted to the model and the viewing distance it is not. */
function updateDepthRange() {
  depthBox.makeEmpty();
  for (const group of [mainGroup, beforeGroup])
    if (group.children.length) depthBox.expandByObject(group);
  if (depthBox.isEmpty()) return;
  depthBox.getCenter(depthCentre);
  // The grid reaches past the model and has to stay inside the planes too.
  if (grid.visible) depthBox.expandByObject(grid);
  const eye = activeCamera.position,
    distance = eye.distanceTo(depthCentre);
  let far = 1e-3;
  for (const x of [depthBox.min.x, depthBox.max.x])
    for (const y of [depthBox.min.y, depthBox.max.y])
      for (const z of [depthBox.min.z, depthBox.max.z])
        far = Math.max(far, eye.distanceTo(depthCorner.set(x, y, z)));
  far *= 1.05;
  // Nothing closer than a hundredth of the viewing distance is part of what the
  // observer is looking at, so clipping it costs nothing and buys precision.
  const nearest = depthBox.distanceToPoint(eye) * 0.9,
    near = Math.min(
      Math.max(
        activeCamera === camera ? Math.max(nearest, distance / 100) : nearest,
        1e-4,
      ),
      far / 1.01,
    );
  if (activeCamera.near === near && activeCamera.far === far) return;
  activeCamera.near = near;
  activeCamera.far = far;
  activeCamera.updateProjectionMatrix();
}
const clipping = new THREE.Plane(new THREE.Vector3(0, -1, 0), 0);
let openingModel = false,
  wireframe = false,
  section = false,
  measuring = false,
  meshData: any = null,
  beforeData: any = null;
/** Earlier states of the model that have been on screen. Their meshes stay
 *  built, so going back to one of them is a swap of children rather than a
 *  request: the geometry is already on the graphics card. Nothing is copied
 *  for this — each state is the revision that was committed then. */
const STATE_MEMORY = 4;
const shownStates = new Map<string, { data: any; meshes: THREE.Mesh[] }>();
let shownRevision: string | null = null,
  shownPartial = false,
  compareRevision: string | null = null,
  headRevision: string | null = null,
  historyRows: any[] = [];
/** Exploded view: current, animated and target share of the full explosion. */
let explodeAmount = 0,
  explodeFrom = 0,
  explodeTo = 0,
  explodeStart = 0,
  explodeRunning = false;
type ExplodeInfo = {
  direction: THREE.Vector3;
  distance: number;
  rank: number;
};
type OverlayMode =
  "parts" | "original" | "lit" | "unlit" | "normals" | "curvature";
const OVERLAY_MODES: OverlayMode[] = [
  "parts",
  "original",
  "lit",
  "unlit",
  "normals",
  "curvature",
];
/** What the model is made of: colour, surface and glow per part, as the service
 *  derived it from the names the parts carry. */
let colourMask: any = null;
function materialOf(featureID: string | undefined) {
  const part = current?.features?.find(
    (f: any) => f.id === featureID,
  )?.owner_part;
  return part ? (colourMask?.parts?.[part] ?? null) : null;
}
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
/** Parts and assemblies as the service describes them. A mark reports which of
 *  them it covers, so the whole entry is worth keeping, not only the name. */
let structureEntries: { assemblies: any[]; parts: any[] } = {
  assemblies: [],
  parts: [],
};
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
  if (overlayMode === "original") {
    // In the model's own colours the legend names the materials, not the parts.
    const materials = new Map<string, { colour: string; count: number }>();
    for (const entry of Object.values<any>(colourMask?.parts ?? {})) {
      const key = `${entry.material ?? entry.name} · ${entry.finish}`,
        seen = materials.get(key);
      if (seen) seen.count++;
      else materials.set(key, { colour: entry.colour, count: 1 });
    }
    legend.hidden = materials.size === 0;
    for (const [key, { colour, count }] of [...materials].sort((a, b) =>
      a[0].localeCompare(b[0], "de"),
    )) {
      const row = document.createElement("span");
      row.className = "legend-entry";
      const swatch = document.createElement("i");
      swatch.style.background = colour;
      row.append(swatch, count > 1 ? `${key} (${count} parts)` : key);
      legend.append(row);
    }
    return;
  }
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
    // Meshes of a remembered state are taken off the group but kept alive.
    if (child.userData?.keep) continue;
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
  if (!ghost && overlayMode === "original") {
    // The device as it is: its own colour, its own surface, and a light guide
    // that carries light rather than reflecting it.
    const entry = materialOf(m.feature_id),
      opacity = entry?.opacity ?? 1;
    const material = new THREE.MeshStandardMaterial({
      ...common,
      color: new THREE.Color(entry?.colour ?? "#9aa3a8"),
      roughness: entry?.roughness ?? 0.55,
      metalness: entry?.metalness ?? 0.1,
      transparent: opacity < 1,
      opacity,
    });
    if (entry?.emissive) {
      material.emissive = new THREE.Color(entry.emissive);
      material.emissiveIntensity = entry.emissive_intensity ?? 0.4;
    }
    return material;
  }
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
/** Build the meshes of a preview. This is the expensive half of showing a
 *  state, and the half worth keeping. */
function build(data: any, ghost = false): THREE.Mesh[] {
  return data.meshes.map((m: any) => {
    const geometry = new THREE.BufferGeometry();
    let origin: number[], rounding: number;
    if (m.positions) {
      geometry.setAttribute(
        "position",
        new THREE.BufferAttribute(m.positions, 3),
      );
      geometry.setIndex(new THREE.BufferAttribute(m.indices, 1));
      origin = m.origin;
      rounding = m.coordinate_rounding_mm;
    } else {
      const local = relativePositions(m.vertices),
        indices = new Uint32Array(m.triangles.length * 3);
      for (let i = 0; i < m.triangles.length; i++) {
        indices[3 * i] = m.triangles[i][0];
        indices[3 * i + 1] = m.triangles[i][1];
        indices[3 * i + 2] = m.triangles[i][2];
      }
      geometry.setAttribute(
        "position",
        new THREE.BufferAttribute(local.positions, 3),
      );
      geometry.setIndex(new THREE.BufferAttribute(indices, 1));
      origin = local.origin;
      rounding = local.maxCoordinateError;
    }
    geometry.computeVertexNormals();
    const material = materialFor(m, ghost, geometry);
    const mesh = new THREE.Mesh(geometry, material);
    mesh.position.fromArray(origin);
    mesh.userData.origin = origin;
    mesh.userData.coordinate_rounding_mm = rounding;
    mesh.userData.feature_id = m.feature_id;
    mesh.userData.face_ranges = m.face_ranges;
    return mesh;
  });
}
/** Put built meshes on screen and tell the rest of the view about them. */
function place(
  data: any,
  meshes: THREE.Mesh[],
  group: THREE.Group = mainGroup,
  ghost = false,
) {
  clearGroup(group);
  for (const mesh of meshes) group.add(mesh);
  // A state that has been on screen before keeps its materials, its draw order
  // and the directions its parts fly apart in. Showing it again is then the
  // swap above and nothing else — only the two display switches, which belong
  // to the view rather than to the state, are applied again.
  if (meshes[0]?.userData.dressed) {
    for (const mesh of meshes) {
      const material = mesh.material as THREE.MeshStandardMaterial;
      material.wireframe = wireframe;
      material.clippingPlanes = section ? [clipping] : [];
    }
    if (!ghost) applyExplosion(() => explodeAmount);
  } else {
    orderDecals(group, ghost);
    if (!ghost) prepareExplosion();
    if (!ghost && group === mainGroup)
      for (const mesh of meshes) mesh.userData.dressed = true;
  }
  if (ghost) beforeData = data;
  else {
    meshData = data;
    el("empty-state").hidden = true;
    text(
      "triangle-count",
      `${data.meshes.reduce((n: number, m: any) => n + (m.triangle_count ?? m.triangles.length), 0).toLocaleString("en-US")} triangles${data.clip ? " · excerpt" : ""}`,
    );
    const r = data.resolution;
    text(
      "resolution",
      r
        ? `Preview · resolution ≤ ${r.absolute_resolution_mm.toPrecision(2)} mm · smallest resolved feature ≈ ${r.minimum_feature_resolved_mm.toPrecision(2)} mm · no surface proof`
        : `Preview · requested deviation ${data.meshes[0]?.deflection?.toPrecision(3) ?? "?"} mm · no surface proof`,
    );
  }
  // What is on screen for this state is what will come back for it.
  if (!ghost && group === mainGroup && shownRevision) {
    if (shownPartial) forget(shownRevision);
    else remember(shownRevision, data, meshes);
  }
}
/** Build and show in one step: the usual way in. */
function draw(data: any, group = mainGroup, ghost = false) {
  place(data, build(data, ghost), group, ghost);
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
function frame() {
  controls.update();
  tickExplosion(performance.now());
  updateScaleBar();
  updateDepthRange();
  renderer.render(scene, activeCamera);
}
/** A frozen view has nothing to animate. Stopping the loop leaves the last
 *  image standing, keeps the phone cool and — while a mark is being captured —
 *  gives the encoding of the screenshots a main thread to itself. */
function runLoop(on: boolean) {
  renderer.setAnimationLoop(on ? frame : null);
}
runLoop(true);
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
  syncTrueColour();
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
/** One press for the two colourings anybody wants to compare: the palette that
 *  tells parts apart, and the device as it really looks. */
function setOverlay(mode: OverlayMode) {
  if (overlayMode === mode) return;
  el<HTMLSelectElement>("overlay").value = mode;
  el<HTMLSelectElement>("overlay").dispatchEvent(new Event("change"));
}
function syncTrueColour() {
  const on = overlayMode === "original";
  el("truecolour").classList.toggle("active", on);
  el("truecolour").setAttribute("aria-pressed", String(on));
}
el("truecolour").onclick = () =>
  setOverlay(overlayMode === "original" ? "parts" : "original");
syncTrueColour();
/* --- Exploded view -------------------------------------------------------
 * Follows the rules that Li et al. derive for exploded view diagrams
 * ("Automated Generation of Interactive 3D Exploded View Diagrams",
 * SIGGRAPH 2008) and that CAD viewers implement in practice:
 *   · parts separate outwards from the model centre, so nothing travels
 *     through the middle of the assembly,
 *   · the direction is pulled towards the nearest canonical axis, because a
 *     handful of readable directions is easier to follow than free rays,
 *   · outer parts travel further than inner ones, which keeps the original
 *     order and the layering of the assembly visible,
 *   · offsets stay as small as visibility allows (compactness),
 *   · the amount is directly controllable from 0 to 100 %,
 *   · opening moves the outer parts first and closing the inner ones first,
 *     the order in which an assembly can actually be taken apart,
 *   · the transition is animated with a smooth ease, or skipped entirely when
 *     the visitor asked for reduced motion.
 */
const EXPLODE_DURATION = 780,
  EXPLODE_STAGGER = 0.28,
  EXPLODE_AXIS_BIAS = 0.35,
  EXPLODE_INNER_TRAVEL = 0.05,
  EXPLODE_OUTER_TRAVEL = 0.24,
  // Clearance between two exploded parts, and how far one may travel at most.
  EXPLODE_GAP = 0.012,
  EXPLODE_MAX_TRAVEL = 1.6;
const reducedMotion = () =>
  window.matchMedia("(prefers-reduced-motion: reduce)").matches;
/** Even directions for parts that sit exactly in the centre (golden angle). */
function fallbackDirection(index: number, count: number) {
  const golden = Math.PI * (3 - Math.sqrt(5)),
    z = 1 - (2 * (index + 0.5)) / Math.max(count, 1),
    ring = Math.sqrt(Math.max(0, 1 - z * z)),
    angle = golden * index;
  return new THREE.Vector3(
    Math.cos(angle) * ring,
    Math.sin(angle) * ring,
    z,
  ).normalize();
}
function dominantAxis(direction: THREE.Vector3) {
  const { x, y, z } = direction;
  if (Math.abs(x) >= Math.abs(y) && Math.abs(x) >= Math.abs(z))
    return new THREE.Vector3(Math.sign(x) || 1, 0, 0);
  if (Math.abs(y) >= Math.abs(z))
    return new THREE.Vector3(0, Math.sign(y) || 1, 0);
  return new THREE.Vector3(0, 0, Math.sign(z) || 1);
}
/** Directions and travel per part, measured on the collapsed model. */
function prepareExplosion() {
  const meshes = mainGroup.children as THREE.Mesh[];
  const min = new THREE.Vector3(Infinity, Infinity, Infinity),
    max = new THREE.Vector3(-Infinity, -Infinity, -Infinity),
    centres: THREE.Vector3[] = [];
  for (const mesh of meshes) {
    const geometry = mesh.geometry as THREE.BufferGeometry;
    if (!geometry.boundingBox) geometry.computeBoundingBox();
    const offset = new THREE.Vector3().fromArray(mesh.userData.origin),
      box = geometry.boundingBox!.clone().translate(offset);
    centres.push(box.getCenter(new THREE.Vector3()));
    min.min(box.min);
    max.max(box.max);
  }
  if (!meshes.length || !Number.isFinite(min.x)) return;
  const centre = min.clone().add(max).multiplyScalar(0.5),
    radius = Math.max(min.distanceTo(max) / 2, 1e-6),
    rays = centres.map((c) => c.clone().sub(centre)),
    reach = Math.max(...rays.map((r) => r.length()), 1e-6);
  // The model's own long axis, and how far its parts stand off it.
  const size = max.clone().sub(min),
    axis = new THREE.Vector3(
      Number(size.x >= size.y && size.x >= size.z),
      Number(size.y > size.x && size.y >= size.z),
      Number(size.z > size.x && size.z > size.y),
    ),
    spread =
      Math.max(
        ...rays.map((r) =>
          r.clone().addScaledVector(axis, -r.dot(axis)).length(),
        ),
      ) || radius;
  const parts = meshes.map((mesh, index) => {
    const geometry = mesh.geometry as THREE.BufferGeometry,
      offset = new THREE.Vector3().fromArray(mesh.userData.origin),
      ray = rays[index],
      rank = Math.min(ray.length() / reach, 1),
      // A long device is exploded the way a drawing does it: what sits on the
      // axis travels along it, what sits beside it swings out sideways. Taking
      // the ray from the centre alone would send everything down the same line.
      along = ray.dot(axis),
      lateral = ray.clone().addScaledVector(axis, -along),
      sideways = Math.min(lateral.length() / Math.max(spread, 1e-6), 1),
      radial =
        ray.length() > radius * 1e-3
          ? lateral
              .clone()
              .normalize()
              .multiplyScalar(sideways)
              .addScaledVector(axis, Math.sign(along || 1) * (1 - sideways))
          : fallbackDirection(index, meshes.length);
    return {
      mesh,
      rank,
      box: geometry.boundingBox!.clone().translate(offset),
      direction:
        radial.lengthSq() > 1e-9
          ? radial.normalize()
          : fallbackDirection(index, meshes.length),
      distance:
        radius *
        (EXPLODE_INNER_TRAVEL +
          (EXPLODE_OUTER_TRAVEL - EXPLODE_INNER_TRAVEL) * Math.pow(rank, 0.75)),
    };
  });
  // Travelling outwards is not enough: two parts that start nested, or that lie
  // along the same ray, arrive on top of each other. So each part is placed in
  // turn, innermost first, and pushed a little further until its box is clear of
  // everything already placed. Pushing only ever moves a part outwards, so the
  // order stays intact — outer parts still end up furthest out — and nothing is
  // spread wider than it has to be.
  const gap = radius * EXPLODE_GAP,
    step = radius * 0.02,
    limit = radius * EXPLODE_MAX_TRAVEL,
    placed: THREE.Box3[] = [],
    moved = new THREE.Box3(),
    shift = new THREE.Vector3();
  for (const part of [...parts].sort((a, b) => a.rank - b.rank)) {
    const at = (distance: number) =>
      moved
        .copy(part.box)
        .translate(shift.copy(part.direction).multiplyScalar(distance))
        .expandByScalar(gap);
    at(part.distance);
    for (
      let guard = 0;
      guard < 200 &&
      part.distance < limit &&
      placed.some((other) => other.intersectsBox(moved));
      guard++
    )
      at((part.distance += step));
    placed.push(part.box.clone().translate(shift));
  }
  for (const part of parts)
    part.mesh.userData.explode = {
      direction: part.direction,
      rank: part.rank,
      distance: part.distance,
    } satisfies ExplodeInfo;
  applyExplosion(() => explodeAmount);
}
function applyExplosion(share: (info: ExplodeInfo) => number) {
  for (const mesh of mainGroup.children as THREE.Mesh[]) {
    const origin = mesh.userData.origin as number[] | undefined;
    if (!origin) continue;
    const info = mesh.userData.explode as ExplodeInfo | undefined;
    if (!info) {
      mesh.position.fromArray(origin);
      continue;
    }
    const travel = info.distance * share(info);
    mesh.position.set(
      origin[0] + info.direction.x * travel,
      origin[1] + info.direction.y * travel,
      origin[2] + info.direction.z * travel,
    );
  }
}
const easeInOut = (t: number) =>
  t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
/** Does the whole model still sit inside the frame? Checked on the eight box corners. */
function modelFits(margin = 1.06) {
  const box = new THREE.Box3().setFromObject(mainGroup);
  if (box.isEmpty()) return true;
  activeCamera.updateMatrixWorld();
  for (let i = 0; i < 8; i++) {
    const corner = new THREE.Vector3(
      i & 1 ? box.max.x : box.min.x,
      i & 2 ? box.max.y : box.min.y,
      i & 4 ? box.max.z : box.min.z,
    ).project(activeCamera);
    if (Math.abs(corner.x) > margin || Math.abs(corner.y) > margin)
      return false;
  }
  return true;
}
/** Frame the model from the current direction, keeping the angle the user chose. */
function dollyToFit() {
  const width = renderer.domElement.clientWidth,
    height = renderer.domElement.clientHeight;
  // While a layout change is still settling the viewport can be degenerate,
  // and a near-zero field of view would throw the camera into the distance.
  if (width < 120 || height < 120) return;
  const box = new THREE.Box3().setFromObject(mainGroup);
  if (box.isEmpty()) return;
  const centre = box.getCenter(new THREE.Vector3()),
    radius = Math.max(box.getSize(new THREE.Vector3()).length() / 2, 1e-3),
    direction = activeCamera.position.clone().sub(controls.target);
  if (direction.lengthSq() < 1e-9) direction.set(0.9, -1.15, 0.9);
  direction.normalize();
  if (activeCamera === camera) {
    const vertical = (camera.fov * Math.PI) / 180,
      horizontal = 2 * Math.atan(Math.tan(vertical / 2) * camera.aspect),
      distance = Math.min(
        radius / Math.sin(Math.max(0.05, Math.min(vertical, horizontal) / 2)),
        radius * 12,
      );
    camera.position.copy(centre).add(direction.multiplyScalar(distance * 1.06));
  } else {
    orthoCamera.position.copy(centre).add(direction.multiplyScalar(radius * 4));
    orthoCamera.zoom = 50 / (radius * 1.06);
    orthoCamera.updateProjectionMatrix();
  }
  controls.target.copy(centre);
  controls.update();
}
function setExplode(target: number, animate = true) {
  explodeTo = THREE.MathUtils.clamp(target, 0, 1);
  if (animate && !reducedMotion() && mainGroup.children.length > 1) {
    explodeFrom = explodeAmount;
    explodeStart = performance.now();
    explodeRunning = true;
  } else {
    explodeAmount = explodeTo;
    explodeRunning = false;
    applyExplosion(() => explodeAmount);
  }
  syncExplodeUI();
}
/** One animation step: every part gets its own delay from its position. */
function tickExplosion(now: number) {
  if (!explodeRunning) return;
  const opening = explodeTo > explodeFrom,
    elapsed = now - explodeStart,
    span = EXPLODE_DURATION * (1 - EXPLODE_STAGGER);
  let running = false;
  applyExplosion((info) => {
    const delay =
      (opening ? 1 - info.rank : info.rank) *
      EXPLODE_DURATION *
      EXPLODE_STAGGER;
    const local = THREE.MathUtils.clamp((elapsed - delay) / span, 0, 1);
    if (local < 1) running = true;
    return explodeFrom + (explodeTo - explodeFrom) * easeInOut(local);
  });
  explodeAmount =
    explodeFrom +
    (explodeTo - explodeFrom) *
      easeInOut(THREE.MathUtils.clamp(elapsed / EXPLODE_DURATION, 0, 1));
  if (!running) {
    explodeAmount = explodeTo;
    explodeRunning = false;
    if (explodeAmount > 0.001) retireExplodeHint();
    // Keep the whole assembly in frame once it has spread out, without
    // taking the viewing direction away from the user.
    if (!modelFits()) dollyToFit();
  }
  syncExplodeUI();
}
const EXPLODE_HINT_KEY = "mathforge.explodeHintSeen";
try {
  if (localStorage.getItem(EXPLODE_HINT_KEY)) el("explode-hint").hidden = true;
} catch {
  // without storage the hint simply stays visible
}
/** The hint explains the motion once; after that the space belongs to the model. */
function retireExplodeHint() {
  if (el("explode-hint").hidden) return;
  el("explode-hint").hidden = true;
  try {
    localStorage.setItem(EXPLODE_HINT_KEY, "1");
  } catch {
    // ignored: the hint is a convenience, not state the model depends on
  }
}
function syncExplodeUI() {
  const percent = Math.round(explodeAmount * 100),
    active = explodeAmount > 0.001;
  for (const id of ["explode", "fab-explode"]) {
    el(id).classList.toggle("active", active);
    el(id).setAttribute("aria-pressed", String(active));
  }
  // The floating buttons move up while the slider capsule is on screen.
  document.body.classList.toggle("exploded", active || explodeRunning);
  el("explode-control").hidden = !active && !explodeRunning;
  text("explode-value", `${percent} %`);
  const slider = el<HTMLInputElement>("explode-amount");
  if (document.activeElement !== slider) slider.value = String(percent);
}
el("explode").onclick = () => {
  if (mainGroup.children.length < 2) {
    toast(
      "This model has a single output object; only several output parts can be exploded.",
    );
    return;
  }
  setExplode(explodeTo > 0.001 ? 0 : 1);
};
el<HTMLInputElement>("explode-amount").oninput = (event) => {
  setExplode(Number((event.target as HTMLInputElement).value) / 100, false);
};
async function preview(
  revision: string,
  featureID?: string,
  ghost = false,
  lod: { adaptive: boolean; region?: { center: string[]; radius: string } } = {
    adaptive: false,
  },
) {
  const partial = !!featureID || !!lod.adaptive || !!lod.region;
  if (!ghost) {
    const known = partial ? null : shownStates.get(revision);
    shownRevision = revision;
    shownPartial = partial;
    if (known) {
      // Already built: this is the whole cost of going back to a state.
      place(known.data, known.meshes, mainGroup, false);
      recolour();
      return;
    }
  }
  const data = await previewData(revision, featureID, lod);
  draw(data, ghost ? beforeGroup : mainGroup, ghost);
}
/** Ask the service for a preview and decode it, without putting it on screen. */
async function previewData(
  revision: string,
  featureID?: string,
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
  const binary = result.artifacts.find(
      (a: any) => a.manifest.filename === "preview.bin",
    ),
    json = result.artifacts.find(
      (a: any) => a.manifest.filename === "preview.json",
    );
  if (!binary && !json) throw new Error("Preview artifact missing.");
  return binary
    ? await fetchPreviewBinary(binary.download)
    : await api(json.download);
}
/** The compact preview: a JSON header followed by Float32 positions and Uint32
 *  indices per mesh, used as they are — no number lists to parse. */
async function fetchPreviewBinary(path: string) {
  const response = await fetch(path, { headers: headers() });
  if (response.status === 401) {
    showLogin();
    throw new Error("Please sign in.");
  }
  if (!response.ok) throw new Error("Preview download failed.");
  return decodePreviewBinary(await response.arrayBuffer());
}
function decodePreviewBinary(buffer: ArrayBuffer) {
  const view = new DataView(buffer);
  if (
    String.fromCharCode(...new Uint8Array(buffer, 0, 4)) !== "MFPV" ||
    view.getUint32(4, true) !== 1
  )
    throw new Error("Unknown preview format.");
  const length = view.getUint32(8, true),
    base = 16 + length + view.getUint32(12, true),
    header = JSON.parse(
      new TextDecoder().decode(new Uint8Array(buffer, 16, length)),
    );
  header.meshes = header.meshes.map((m: any) => ({
    ...m,
    positions: new Float32Array(
      buffer,
      base + m.positions_offset,
      m.vertex_count * 3,
    ),
    indices: new Uint32Array(
      buffer,
      base + m.indices_offset,
      m.triangle_count * 3,
    ),
  }));
  return header;
}
/** Give the drawn parts their colours once the structure has arrived. */
function recolour() {
  if (!meshData || (overlayMode !== "parts" && overlayMode !== "original"))
    return;
  const byFeature = new Map<string, any>(
    meshData.meshes.map((m: any) => [m.feature_id, m]),
  );
  for (const mesh of mainGroup.children as THREE.Mesh[]) {
    const m = byFeature.get(mesh.userData.feature_id);
    if (!m) continue;
    (mesh.material as THREE.Material).dispose();
    mesh.material = materialFor(m, false, mesh.geometry);
  }
  orderDecals(mainGroup);
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
/** A list row's text: the title with its detail line underneath. */
function rowText(title: string, detail: string) {
  const text = document.createElement("div");
  text.className = "row-text";
  const name = document.createElement("span");
  name.className = "row-title";
  name.textContent = title;
  const small = document.createElement("small");
  small.textContent = detail;
  text.append(name, small);
  return text;
}
/** Part and assembly tree from cad_structure (Bauplan 19.2). */
async function structureTree() {
  const container = el("structure");
  container.replaceChildren();
  const fetchAll = async (kind: string) => {
    const page = (offset: number) =>
      tool("cad_structure", {
        model_id: current.model_id,
        revision: current.revision,
        kind,
        offset,
        limit: 16,
      });
    const first = await page(0),
      rest: Promise<any>[] = [];
    for (let offset = 16; offset < (first.total_matches ?? 0); offset += 16)
      rest.push(page(offset));
    return [first, ...(await Promise.all(rest))].flatMap((p: any) => p.entries);
  };
  const [assemblies, parts] = await Promise.all([
    fetchAll("assembly"),
    fetchAll("part"),
  ]);
  partNames.clear();
  for (const p of parts) partNames.set(p.entity_id, p.semantic_name);
  structureEntries = { assemblies, parts };
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
      summary.append(
        rowText(
          a.semantic_name,
          `${a.part_count} parts · ${a.definition.local_frame}`,
        ),
      );
      details.append(summary, ...render(a.entity_id, depth + 1));
      return details;
    }),
    ...parts
      .filter((p) => p.definition.assembly === parent)
      .map((p) => {
        const button = document.createElement("button");
        button.className = "part";
        button.dataset.part = p.entity_id;
        const swatch = document.createElement("i");
        swatch.className = "swatch";
        swatch.style.background =
          groupColours.get(groupKey(p.semantic_name)) ?? "#91a58b";
        // "Material · Name": the name leads, the material joins the detail line.
        const [material, ...rest] = p.semantic_name.split(" · ");
        const title = rest.length ? rest.join(" · ") : material,
          detail = [
            rest.length ? material : "",
            p.definition.authoritative_representation,
            `${p.feature_count} features`,
          ]
            .filter(Boolean)
            .join(" · ");
        button.append(swatch, rowText(title, detail));
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
    // Another model has nothing in common with the states kept for this one.
    if (current?.model_id !== modelID) {
      forgetAll();
      compareRevision = null;
      colourMask = null;
    }
    current = await tool("cad_get_model", { model_id: modelID, limit: 64 });
    displayRevision = current.revision;
    headRevision = current.revision;
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
    text("app-title", current.name);
    text(
      "app-subtitle",
      `${current.feature_count} features · ${
        current.quality === "checks_passed_within_profile"
          ? "verified revision"
          : "draft"
      }`,
    );
    el("features").replaceChildren();
    // The geometry is the long pole: ask for it first, and let the feature
    // pages and the structure load alongside it.
    const geometry: Promise<Error | boolean> = current.features.length
      ? preview(current.revision).then(
          () => true,
          (error: Error) => error,
        )
      : Promise.resolve(false);
    const structure = structureTree();
    // What the parts are made of, for the colouring that shows the real thing.
    const colours = api(`/api/models/${modelID}/colours`).then(
      (mask: any) => (colourMask = mask),
      () => (colourMask = null),
    );
    const allFeatures = [...current.features],
      pages: Promise<any>[] = [];
    for (
      let offset = current.next_offset;
      offset !== null && offset < current.feature_count;
      offset += 64
    )
      pages.push(
        tool("cad_get_model", {
          model_id: modelID,
          revision: current.revision,
          offset,
          limit: 64,
        }),
      );
    for (const page of await Promise.all(pages))
      allFeatures.push(...page.features);
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
    await Promise.all([structure, colours]);
    el("detail-body").hidden = true;
    el("empty-state").hidden = !!current.features.length;
    if (current.features.length) {
      const drawn = await geometry;
      if (drawn instanceof Error) throw drawn;
      recolour();
      if (fitView) fit();
      // The first selection happens on its own; on a phone the model stays in
      // view instead of the detail sheet sliding up unasked.
      openingModel = true;
      try {
        await selectFeature(
          current.features.find((f: any) => f.kind === "groove")?.id ??
            current.features[0].id,
        );
      } finally {
        openingModel = false;
      }
    } else {
      // An empty model shows nothing, and says nothing about the last one.
      clearGroup(mainGroup);
      meshData = null;
      text("triangle-count", "—");
      text("resolution", "Geometric preview");
      text("detail-name", "Select a feature");
    }
    await models();
    void loadOrders().catch(() => {});
    void loadHistory().catch(() => {});
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
  revealGroup("detail");
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
  text("validation-summary", "Candidate computed; mandatory checks pending.");
  text("quality", "Candidate · unchecked");
  text("app-subtitle", "Candidate · unchecked");
  el("quality").classList.add("preview");
  await Promise.all([
    preview(current.revision, undefined, true),
    preview(displayRevision!),
  ]);
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
/** What a person hands in with an order: pictures, which are shrunk here —
 *  evidence, not a print, and ten of them still have to travel over a phone
 *  connection — and meshes, which travel as they are because their numbers are
 *  the point. */
type Attachment = {
  blob: Blob;
  name: string;
  mime: string;
  url: string | null;
};
const MAX_ATTACHMENTS = 10,
  MAX_MESH_BYTES = 24 * 1024 * 1024;
function attachmentBox(boxId: string, inputId: string) {
  const files: Attachment[] = [];
  const render = () => {
    const box = el(boxId);
    box.replaceChildren();
    box.hidden = !files.length;
    files.forEach((file, index) => {
      const thumb = document.createElement("button");
      thumb.type = "button";
      thumb.className = file.url ? "thumb" : "thumb mesh";
      thumb.title = `${file.name} — remove`;
      if (file.url) {
        const image = document.createElement("img");
        image.src = file.url;
        image.alt = "";
        thumb.append(image);
      } else {
        const label = document.createElement("small");
        label.textContent =
          file.name.length > 16 ? file.name.slice(0, 14) + "…" : file.name;
        thumb.append(label);
      }
      const cross = document.createElement("span");
      cross.textContent = "✕";
      thumb.append(cross);
      thumb.onclick = () => {
        if (file.url) URL.revokeObjectURL(file.url);
        files.splice(index, 1);
        render();
      };
      box.append(thumb);
    });
  };
  el<HTMLInputElement>(inputId).onchange = action(async () => {
    const input = el<HTMLInputElement>(inputId);
    for (const file of [...(input.files ?? [])]) {
      if (files.length >= MAX_ATTACHMENTS) {
        toast(`${MAX_ATTACHMENTS} attachments are the most one order carries.`);
        break;
      }
      if (/\.stl$/i.test(file.name)) {
        if (file.size > MAX_MESH_BYTES) {
          toast(`${file.name} is larger than 24 MB.`);
          continue;
        }
        files.push({
          blob: file,
          name: file.name,
          mime: "model/stl",
          url: null,
        });
      } else if (file.type.startsWith("image/")) {
        const blob = await shrink(file);
        files.push({
          blob,
          name: file.name,
          mime: "image/jpeg",
          url: URL.createObjectURL(blob),
        });
      } else
        toast(`${file.name}: only pictures and STL meshes can be attached.`);
    }
    input.value = "";
    render();
  });
  return {
    files,
    clear() {
      for (const file of files) if (file.url) URL.revokeObjectURL(file.url);
      files.length = 0;
      render();
    },
  };
}
/** Pictures keep their own numbering, meshes theirs, so the agent can tell from
 *  the name what it is looking at. */
async function uploadAttachments(annotation: string, files: Attachment[]) {
  let pictures = 0,
    meshes = 0;
  await Promise.all(
    files.map((file) => {
      const kind = file.mime.startsWith("image/")
        ? `bild-${++pictures}`
        : `datei-${++meshes}`;
      return api(`/api/annotations/${annotation}/file/${kind}`, {
        method: "POST",
        headers: { "Content-Type": file.mime },
        body: file.blob,
      });
    }),
  );
}
const newFiles = attachmentBox("new-images", "new-image");
const noteFiles = attachmentBox("note-files", "note-file");
async function shrink(file: File) {
  const bitmap = await createImageBitmap(file);
  const scale = Math.min(1, 1600 / Math.max(bitmap.width, bitmap.height)),
    canvas = document.createElement("canvas");
  canvas.width = Math.max(1, Math.round(bitmap.width * scale));
  canvas.height = Math.max(1, Math.round(bitmap.height * scale));
  canvas.getContext("2d")!.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
  bitmap.close();
  return toBlob(canvas, "image/jpeg");
}
el("new-dialog").addEventListener("close", () => newFiles.clear());
el("new-model").onclick = () => el<HTMLDialogElement>("new-dialog").showModal();
document
  .querySelectorAll<HTMLElement>("[data-close]")
  .forEach(
    (b) => (b.onclick = () => el<HTMLDialogElement>(b.dataset.close!).close()),
  );
el("new-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const name = el<HTMLInputElement>("new-name").value.trim(),
    note = el<HTMLTextAreaElement>("new-purpose").value.trim(),
    pictures = [...newFiles.files];
  void action(async () => {
    el<HTMLDialogElement>("new-dialog").close();
    const model = await create(name, note);
    el<HTMLInputElement>("new-name").value = "";
    el<HTMLTextAreaElement>("new-purpose").value = "";
    if (!note && !pictures.length) return;
    // The model is there; the order that describes it is written while the
    // person carries on working.
    const order = await api(`/api/models/${model.model_id}/purpose`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ note }),
    });
    await uploadAttachments(order.annotation_id, pictures);
    await api(`/api/annotations/${order.annotation_id}/dispatch`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ stage: "purpose" }),
    });
    await models();
    toast(
      pictures.length
        ? `“${name}” is in the list. The pictures are being read.`
        : `“${name}” is in the list. Its plan is being written.`,
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
/** Opens the handed-over model, or the first one, exactly once. */
async function initialize(preselect: string | null = null) {
  const list = await models();
  // A handed-over model is opened even when the list leaves it out; only one
  // that cannot be opened falls back to the first of the list.
  if (preselect) {
    try {
      await openModel(preselect);
      return;
    } catch {
      toast("The model handed over could not be opened.");
    }
  }
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
    await initialize(preselect);
  } catch {
    showLogin();
    return;
  }
  await Promise.all([
    loadAgentSettings().catch(() => {}),
    loadOrders().catch(() => {}),
  ]);
})();

/* --- Marking a change -----------------------------------------------------
 * A change request starts as a drawing. The view freezes, the person circles
 * the place they mean, and the viewer works out what lies under the mark —
 * which parts, which features, which faces, and where that is in millimetres —
 * before any of it has to be put into words. What leaves the browser is one
 * record: the stroke, the camera, the covered geometry, a picture of the area
 * and the sentence the person wrote. An agent can be handed the whole thing.
 * -------------------------------------------------------------------------*/
type Stroke = [number, number][];
type Box = { x: number; y: number; width: number; height: number };
const MARK_COLOUR = "#e0261c";
let marking = false,
  strokes: Stroke[] = [],
  drawing: Stroke | null = null;
function markCanvas() {
  return el<HTMLCanvasElement>("markup-canvas");
}
function markWidth() {
  const canvas = markCanvas();
  return Math.max(3, Math.min(canvas.clientWidth, canvas.clientHeight) / 95);
}
/** The strokes in canvas coordinates; the caller sets the scale it needs. */
function paintStrokes(
  ctx: CanvasRenderingContext2D,
  style: { colour: string; width: number; fill?: boolean },
) {
  ctx.lineJoin = "round";
  ctx.lineCap = "round";
  ctx.strokeStyle = style.colour;
  ctx.fillStyle = style.colour;
  ctx.lineWidth = style.width;
  for (const stroke of strokes) {
    if (stroke.length < 2) continue;
    ctx.beginPath();
    ctx.moveTo(stroke[0][0], stroke[0][1]);
    for (const [x, y] of stroke.slice(1)) ctx.lineTo(x, y);
    // A circle drawn by hand rarely closes; for the covered area it counts as
    // closed, for the visible line it does not.
    if (style.fill) {
      ctx.closePath();
      ctx.fill();
    }
    ctx.stroke();
  }
}
function redrawMark() {
  const canvas = markCanvas(),
    ratio = Math.min(devicePixelRatio, 2);
  canvas.width = Math.round(canvas.clientWidth * ratio);
  canvas.height = Math.round(canvas.clientHeight * ratio);
  const ctx = canvas.getContext("2d")!;
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx.clearRect(0, 0, canvas.clientWidth, canvas.clientHeight);
  paintStrokes(ctx, { colour: MARK_COLOUR, width: markWidth() });
  el<HTMLButtonElement>("markup-undo").disabled = !strokes.length;
  el<HTMLButtonElement>("markup-save").disabled = !strokes.length;
}
function beginMark() {
  if (marking) return;
  if (!meshData || !mainGroup.children.length) {
    toast("Open a model first.");
    return;
  }
  marking = true;
  strokes = [];
  document.body.dataset.marking = "true";
  controls.enabled = false;
  runLoop(false);
  el("markup").hidden = false;
  redrawMark();
}
function endMark() {
  marking = false;
  strokes = [];
  drawing = null;
  delete document.body.dataset.marking;
  controls.enabled = true;
  runLoop(true);
  el("markup").hidden = true;
}
{
  const canvas = markCanvas(),
    at = (event: PointerEvent): [number, number] => {
      const box = canvas.getBoundingClientRect();
      return [event.clientX - box.left, event.clientY - box.top];
    };
  canvas.addEventListener("pointerdown", (event) => {
    canvas.setPointerCapture(event.pointerId);
    drawing = [at(event)];
    strokes.push(drawing);
    redrawMark();
  });
  canvas.addEventListener("pointermove", (event) => {
    if (!drawing) return;
    const point = at(event),
      last = drawing[drawing.length - 1];
    if (Math.hypot(point[0] - last[0], point[1] - last[1]) < 1.5) return;
    drawing.push(point);
    redrawMark();
  });
  const finish = () => {
    if (drawing && drawing.length < 2) strokes.pop();
    drawing = null;
    redrawMark();
  };
  canvas.addEventListener("pointerup", finish);
  canvas.addEventListener("pointercancel", finish);
}
el("markup-undo").onclick = () => {
  strokes.pop();
  redrawMark();
};
el("markup-cancel").onclick = endMark;
el("mark").onclick = beginMark;
el("fab-mark").onclick = beginMark;
el("markup-save").onclick = () => {
  const bounds = markBounds();
  text(
    "note-context",
    `The mark, the parts it covers and a picture of the area are saved with it (${Math.round(bounds.width)} × ${Math.round(bounds.height)} px at ${currentMmPerPixel.toPrecision(2)} mm/px).`,
  );
  el<HTMLTextAreaElement>("note-text").value = "";
  el<HTMLDialogElement>("note-dialog").showModal();
  el<HTMLTextAreaElement>("note-text").focus();
};
el<HTMLFormElement>("note-form").onsubmit = (event) => {
  event.preventDefault();
  const note = el<HTMLTextAreaElement>("note-text").value.trim();
  if (!note) return;
  // The attachments are taken before the dialog closes, because closing it
  // gives them back.
  const files = [...noteFiles.files];
  el<HTMLDialogElement>("note-dialog").close();
  void action(async () => saveMark(note, files))();
};
el("note-dialog").addEventListener("close", () => noteFiles.clear());
/** The rectangle the stroke occupies, in canvas pixels, never empty. */
function markBounds(): Box {
  const points = strokes.flat();
  const canvas = markCanvas(),
    xs = points.map((p) => p[0]),
    ys = points.map((p) => p[1]),
    pad = markWidth();
  const x = Math.max(0, Math.min(...xs) - pad),
    y = Math.max(0, Math.min(...ys) - pad),
    right = Math.min(canvas.clientWidth, Math.max(...xs) + pad),
    bottom = Math.min(canvas.clientHeight, Math.max(...ys) + pad);
  return {
    x,
    y,
    width: Math.max(4, right - x),
    height: Math.max(4, bottom - y),
  };
}
/** The marked area as a grid of cells over its own bounding box. */
function markMask(box: Box, width: number, height: number) {
  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;
  const ctx = canvas.getContext("2d")!;
  ctx.setTransform(
    width / box.width,
    0,
    0,
    height / box.height,
    (-box.x * width) / box.width,
    (-box.y * height) / box.height,
  );
  paintStrokes(ctx, {
    colour: "#ffffff",
    width: markWidth(),
    fill: true,
  });
  const data = ctx.getImageData(0, 0, width, height).data,
    mask = new Uint8Array(width * height);
  for (let i = 0; i < mask.length; i++) mask[i] = data[4 * i + 3] > 40 ? 1 : 0;
  return mask;
}
/** Which mesh owns which pixel of the marked area. The scene is drawn once more
 *  into an off-screen buffer, each mesh in a colour that is only an identifier;
 *  reading it back is exact and costs a single frame, where sampling the same
 *  area with rays would cost hundreds. */
function markCoverage(
  box: Box,
  mask: Uint8Array,
  width: number,
  height: number,
) {
  const canvas = renderer.domElement,
    meshes = mainGroup.children.filter(
      (child): child is THREE.Mesh =>
        child instanceof THREE.Mesh && child.visible,
    ),
    originals = meshes.map((mesh) => mesh.material as THREE.Material),
    hidden = [grid, beforeGroup, regionGroup, markerGroup].filter(
      (o) => o.visible,
    ),
    background = scene.background,
    target = new THREE.WebGLRenderTarget(width, height),
    counts = new Map<number, { pixels: number; x: number; y: number }>();
  try {
    meshes.forEach((mesh, index) => {
      const source = originals[index] as any,
        value = index + 1;
      mesh.material = new THREE.MeshBasicMaterial({
        color: new THREE.Color().setRGB(
          ((value & 63) * 4 + 2) / 255,
          (((value >> 6) & 63) * 4 + 2) / 255,
          0,
          THREE.LinearSRGBColorSpace,
        ),
        side: source.side,
        clippingPlanes: source.clippingPlanes ?? [],
        polygonOffset: source.polygonOffset,
        polygonOffsetFactor: source.polygonOffsetFactor,
        polygonOffsetUnits: source.polygonOffsetUnits,
      });
    });
    for (const object of hidden) object.visible = false;
    scene.background = new THREE.Color(0x000000);
    activeCamera.setViewOffset(
      canvas.clientWidth,
      canvas.clientHeight,
      box.x,
      box.y,
      box.width,
      box.height,
    );
    renderer.setRenderTarget(target);
    renderer.render(scene, activeCamera);
    renderer.setRenderTarget(null);
    const pixels = new Uint8Array(width * height * 4);
    renderer.readRenderTargetPixels(target, 0, 0, width, height, pixels);
    for (let row = 0; row < height; row++)
      for (let column = 0; column < width; column++) {
        if (!mask[row * width + column]) continue;
        // The buffer starts at the bottom, the mask at the top.
        const i = ((height - 1 - row) * width + column) * 4,
          r = pixels[i],
          g = pixels[i + 1];
        if (r < 2 || g < 2) continue;
        const index =
          Math.round((r - 2) / 4) + (Math.round((g - 2) / 4) << 6) - 1;
        if (index < 0 || index >= meshes.length) continue;
        const entry = counts.get(index) ?? { pixels: 0, x: 0, y: 0 };
        entry.pixels++;
        entry.x += column;
        entry.y += row;
        counts.set(index, entry);
      }
  } finally {
    meshes.forEach((mesh, index) => {
      (mesh.material as THREE.Material).dispose();
      mesh.material = originals[index];
    });
    for (const object of hidden) object.visible = true;
    scene.background = background;
    activeCamera.clearViewOffset();
    activeCamera.updateProjectionMatrix();
    renderer.setRenderTarget(null);
    target.dispose();
  }
  return { meshes, counts };
}
/** The surface under one point of the canvas, in millimetres. */
function surfaceAt(x: number, y: number) {
  const canvas = renderer.domElement;
  raycaster.setFromCamera(
    new THREE.Vector2(
      (x / canvas.clientWidth) * 2 - 1,
      -(y / canvas.clientHeight) * 2 + 1,
    ),
    activeCamera,
  );
  return (
    raycaster
      .intersectObjects(mainGroup.children, false)
      .find((hit) => !section || clipping.distanceToPoint(hit.point) >= 0) ??
    null
  );
}
function faceOf(hit: THREE.Intersection) {
  return (
    (hit.object.userData.face_ranges ?? []).find(
      (range: any) =>
        hit.faceIndex != null &&
        hit.faceIndex >= range.first_triangle &&
        hit.faceIndex < range.first_triangle + range.triangle_count,
    )?.face_id ?? null
  );
}
const round = (value: number, digits = 4) => Number(value.toFixed(digits));
const asPoint = (v: THREE.Vector3) => [round(v.x), round(v.y), round(v.z)];
/** Everything the mark can say about itself. */
function analyseMark(box: Box) {
  const canvas = renderer.domElement,
    longest = Math.max(box.width, box.height),
    width = Math.max(8, Math.round((box.width / longest) * 192)),
    height = Math.max(8, Math.round((box.height / longest) * 192)),
    mask = markMask(box, width, height),
    covered = mask.reduce((n, cell) => n + cell, 0),
    { meshes, counts } = markCoverage(box, mask, width, height),
    toCanvas = (column: number, row: number): [number, number] => [
      box.x + ((column + 0.5) * box.width) / width,
      box.y + ((row + 0.5) * box.height) / height,
    ];
  const featureIndex = new Map<string, any>(
      (current?.features ?? []).map((f: any) => [f.id, f]),
    ),
    partIndex = new Map<string, any>(
      structureEntries.parts.map((p: any) => [p.entity_id, p]),
    ),
    assemblyIndex = new Map<string, any>(
      structureEntries.assemblies.map((a: any) => [a.entity_id, a]),
    );
  const points: THREE.Vector3[] = [],
    features: any[] = [];
  for (const [index, entry] of [...counts].sort(
    (a, b) => b[1].pixels - a[1].pixels,
  )) {
    const mesh = meshes[index],
      [x, y] = toCanvas(entry.x / entry.pixels, entry.y / entry.pixels),
      hit = surfaceAt(x, y),
      own = hit?.object === mesh ? hit : null,
      feature = featureIndex.get(mesh.userData.feature_id),
      part = partIndex.get(feature?.owner_part ?? ""),
      bounds = new THREE.Box3().setFromObject(mesh);
    if (own) points.push(own.point.clone());
    features.push({
      feature_id: mesh.userData.feature_id,
      semantic_name: feature?.semantic_name ?? null,
      kind: feature?.kind ?? null,
      operator: feature?.operator ?? null,
      representation: feature?.representation ?? null,
      local_frame: feature?.local_frame ?? null,
      part_id: feature?.owner_part ?? null,
      part_name:
        part?.semantic_name ?? partNames.get(feature?.owner_part) ?? null,
      parameters: feature?.parameters ?? null,
      depends_on: feature?.depends_on ?? [],
      coverage: round(entry.pixels / Math.max(1, covered), 5),
      pixels: entry.pixels,
      point: own ? asPoint(own.point) : null,
      normal:
        own?.normal && own.face
          ? asPoint(
              own.face.normal
                .clone()
                .applyNormalMatrix(
                  new THREE.Matrix3().getNormalMatrix(mesh.matrixWorld),
                )
                .normalize(),
            )
          : null,
      face_id: own ? faceOf(own) : null,
      distance_mm: own ? round(own.distance) : null,
      feature_bounds: { min: asPoint(bounds.min), max: asPoint(bounds.max) },
    });
  }
  // A handful of rays spread over the mark bound the region in space; the
  // coverage pass above has already said which parts it belongs to.
  for (let row = 0; row < height; row += Math.max(1, Math.round(height / 6)))
    for (
      let column = 0;
      column < width;
      column += Math.max(1, Math.round(width / 6))
    ) {
      if (!mask[row * width + column]) continue;
      const hit = surfaceAt(...toCanvas(column, row));
      if (hit) points.push(hit.point.clone());
    }
  const world = new THREE.Box3();
  for (const point of points) world.expandByPoint(point);
  const parts = new Map<string, any>();
  for (const feature of features) {
    if (!feature.part_id) continue;
    const part = partIndex.get(feature.part_id),
      assembly = assemblyIndex.get(part?.definition?.assembly ?? ""),
      entry = parts.get(feature.part_id) ?? {
        part_id: feature.part_id,
        name: part?.semantic_name ?? partNames.get(feature.part_id) ?? null,
        assembly_id: part?.definition?.assembly ?? null,
        assembly_name: assembly?.semantic_name ?? null,
        purpose: part?.definition?.purpose?.value ?? null,
        representation: part?.definition?.authoritative_representation ?? null,
        coverage: 0,
        features: [] as string[],
      };
    entry.coverage = round(entry.coverage + feature.coverage, 5);
    entry.features.push(feature.feature_id);
    parts.set(feature.part_id, entry);
  }
  const centre = points.length
      ? world.getCenter(new THREE.Vector3())
      : new THREE.Vector3(),
    direction = activeCamera.position.clone().sub(controls.target).normalize();
  return {
    mark: {
      strokes: strokes.map((stroke) =>
        stroke.map(([x, y]) => [
          round(x / canvas.clientWidth, 5),
          round(y / canvas.clientHeight, 5),
        ]),
      ),
      bounds_px: box,
      bounds_normalised: {
        x: round(box.x / canvas.clientWidth, 5),
        y: round(box.y / canvas.clientHeight, 5),
        width: round(box.width / canvas.clientWidth, 5),
        height: round(box.height / canvas.clientHeight, 5),
      },
      covered_cells: covered,
      grid: { width, height },
    },
    view: {
      projection: activeCamera === camera ? "perspective" : "orthographic",
      position: asPoint(activeCamera.position),
      target: asPoint(controls.target),
      up: asPoint(activeCamera.up),
      direction: asPoint(direction),
      fov: activeCamera === camera ? camera.fov : null,
      zoom: activeCamera === camera ? null : orthoCamera.zoom,
      near: round(activeCamera.near, 5),
      far: round(activeCamera.far, 2),
      distance_mm: round(activeCamera.position.distanceTo(controls.target)),
      mm_per_pixel: round(currentMmPerPixel, 5),
      canvas: {
        width: canvas.clientWidth,
        height: canvas.clientHeight,
        device_pixel_ratio: Math.min(devicePixelRatio, 2),
      },
    },
    world: points.length
      ? {
          centre: asPoint(centre),
          min: asPoint(world.min),
          max: asPoint(world.max),
          radius: round(world.getSize(new THREE.Vector3()).length() / 2),
          samples: points.length,
        }
      : null,
    features,
    parts: [...parts.values()].sort((a, b) => b.coverage - a.coverage),
    scene: {
      overlay: overlayMode,
      wireframe,
      section,
      exploded: round(explodeAmount, 3),
      drawn_features: mainGroup.children.length,
      triangles: (meshData?.meshes ?? []).reduce(
        (n: number, m: any) => n + (m.triangle_count ?? m.triangles.length),
        0,
      ),
    },
  };
}
function toBlob(canvas: HTMLCanvasElement, type: string): Promise<Blob> {
  return new Promise((resolve, reject) =>
    canvas.toBlob(
      (blob) => (blob ? resolve(blob) : reject(new Error("No image."))),
      type,
      type === "image/jpeg" ? 0.88 : undefined,
    ),
  );
}
/** The frozen view with the mark on it: the whole screen for context, and the
 *  marked area close up. Both are cut straight out of the rendered canvas and
 *  capped in size — a picture somebody looks at, not a texture, and encoding a
 *  full phone screen as PNG would cost seconds. */
async function markImages(box: Box) {
  const source = renderer.domElement;
  renderer.render(scene, activeCamera);
  const device = source.width / Math.max(1, source.clientWidth),
    cut = (crop: Box, limit: number) => {
      const zoom = Math.min(
          device * 2,
          limit / Math.max(crop.width, crop.height),
        ),
        canvas = document.createElement("canvas");
      canvas.width = Math.max(1, Math.round(crop.width * zoom));
      canvas.height = Math.max(1, Math.round(crop.height * zoom));
      const ctx = canvas.getContext("2d")!;
      ctx.drawImage(
        source,
        crop.x * device,
        crop.y * device,
        crop.width * device,
        crop.height * device,
        0,
        0,
        canvas.width,
        canvas.height,
      );
      ctx.setTransform(zoom, 0, 0, zoom, -crop.x * zoom, -crop.y * zoom);
      paintStrokes(ctx, { colour: MARK_COLOUR, width: markWidth() });
      return canvas;
    };
  const pad = Math.max(box.width, box.height) * 0.2,
    left = Math.max(0, box.x - pad),
    top = Math.max(0, box.y - pad),
    crop = {
      x: left,
      y: top,
      width: Math.min(source.clientWidth - left, box.width + 2 * pad),
      height: Math.min(source.clientHeight - top, box.height + 2 * pad),
    };
  const regionCanvas = cut(crop, 900),
    viewCanvas = cut(
      { x: 0, y: 0, width: source.clientWidth, height: source.clientHeight },
      1280,
    );
  const regionBlob = await toBlob(regionCanvas, "image/png");
  const viewBlob = await toBlob(viewCanvas, "image/jpeg");
  return {
    region: { blob: regionBlob, type: "image/png" },
    view: { blob: viewBlob, type: "image/jpeg" },
  };
}
async function saveMark(note: string, files: Attachment[] = []) {
  busy("Reading the marked area …");
  try {
    const box = markBounds(),
      analysis = analyseMark(box);
    // The pictures are encoded while the record is already on its way.
    const pictures = markImages(box),
      record = await api("/api/annotations", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          model_id: current.model_id,
          revision: displayRevision ?? current.revision,
          note,
          ...analysis,
        }),
      });
    const images = await pictures;
    await Promise.all(
      (["region", "view"] as const).map(async (kind) => {
        const response = await fetch(
          `/api/annotations/${record.annotation_id}/image/${kind}`,
          {
            method: "POST",
            headers: headers({ "Content-Type": images[kind].type }),
            body: images[kind].blob,
          },
        );
        if (!response.ok) throw new Error("The picture could not be saved.");
      }),
    );
    await uploadAttachments(record.annotation_id, files);
    await loadOrders();
    endMark();
    if (inAppShell()) {
      openPanel("more");
      openSheet("large");
    }
    el("group-orders")?.scrollIntoView({ block: "nearest" });
    toast(
      `Change request saved · ${analysis.parts.length} part${analysis.parts.length === 1 ? "" : "s"} covered`,
    );
  } finally {
    busy("", false);
  }
}

/* --- Earlier states: going back, and forward again ------------------------ */
/** A state that has been drawn stays built until the memory is needed for a
 *  newer one. Releasing it gives up the graphics buffers, but only for meshes
 *  that are no longer on screen. */
function release(entry: { meshes: THREE.Mesh[] }) {
  for (const mesh of entry.meshes) {
    mesh.userData.keep = false;
    if (!mesh.parent) {
      mesh.geometry.dispose();
      (mesh.material as THREE.Material).dispose();
    }
  }
}
function remember(revision: string, data: any, meshes: THREE.Mesh[]) {
  const known = shownStates.get(revision);
  if (known && known.meshes !== meshes) release(known);
  for (const mesh of meshes) mesh.userData.keep = true;
  // Re-inserting makes this the newest entry; the oldest one leaves first.
  shownStates.delete(revision);
  shownStates.set(revision, { data, meshes });
  while (shownStates.size > STATE_MEMORY) {
    const oldest = [...shownStates.keys()].find(
      (r) => r !== revision && r !== shownRevision,
    );
    if (!oldest) break;
    forget(oldest);
  }
}
function forget(revision: string) {
  const entry = shownStates.get(revision);
  if (!entry) return;
  shownStates.delete(revision);
  release(entry);
}
function forgetAll() {
  for (const revision of [...shownStates.keys()]) forget(revision);
  shownRevision = null;
  historyRows = [];
  renderHistory();
}
const RELATIVE = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
function ago(iso: string) {
  const seconds = (Date.parse(iso) - Date.now()) / 1000;
  for (const [unit, size] of [
    ["day", 86400],
    ["hour", 3600],
    ["minute", 60],
  ] as [Intl.RelativeTimeFormatUnit, number][])
    if (Math.abs(seconds) >= size)
      return RELATIVE.format(Math.round(seconds / size), unit);
  return RELATIVE.format(0, "minute");
}
function clock(iso: string) {
  return new Date(iso).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
  });
}
/** What one state says about itself: its size, what the step before it did,
 *  and how much material that cost or saved. */
function stateSummary(row: any) {
  if (!row) return "";
  const parent = historyRows.find((r: any) => r.revision === row.parent),
    counts = row.change?.counts,
    parts = [`${row.features} features`];
  if (counts) {
    const bits = [
      counts.added ? `+${counts.added}` : null,
      counts.removed ? `−${counts.removed}` : null,
      counts.changed ? `~${counts.changed}` : null,
    ].filter(Boolean);
    if (bits.length) parts.push(bits.join(" "));
  }
  if (
    parent &&
    typeof row.volume_mm3 === "number" &&
    typeof parent.volume_mm3 === "number"
  ) {
    const delta = (row.volume_mm3 - parent.volume_mm3) / 1000;
    if (Math.abs(delta) >= 0.005)
      parts.push(`${delta > 0 ? "+" : "−"}${Math.abs(delta).toFixed(2)} cm³`);
  }
  const named =
    row.change?.added?.[0] ??
    row.change?.changed?.[0] ??
    row.change?.removed?.[0];
  if (named) parts.push(named.length > 64 ? named.slice(0, 63) + "…" : named);
  return parts.join(" · ");
}
function badge(label: string, kind = "") {
  const element = document.createElement("span");
  element.className = "state-badge" + (kind ? " " + kind : "");
  element.textContent = label;
  return element;
}
async function loadHistory() {
  if (!current) return;
  const data = await api(`/api/models/${current.model_id}/history?limit=25`);
  historyRows = data.revisions ?? [];
  headRevision = data.head ?? headRevision;
  renderHistory();
  setTimeout(prefetchNeighbour, 2500);
}
function renderHistory() {
  const list = el("history");
  if (!list) return;
  text("history-count", historyRows.length ? String(historyRows.length) : "—");
  list.replaceChildren();
  if (!historyRows.length) {
    const empty = document.createElement("p");
    empty.className = "muted empty-tree";
    empty.textContent = "Every committed state stays available here.";
    list.append(empty);
    return;
  }
  for (const row of historyRows) {
    const item = document.createElement("div");
    item.className = "state-row";
    item.dataset.state = row.state;
    item.dataset.current = String(row.is_head);
    item.dataset.revision = row.revision;
    const mark = document.createElement("span");
    mark.className = "state-mark";
    const open = document.createElement("button");
    open.type = "button";
    open.className = "state-open";
    const title = document.createElement("strong");
    title.textContent = clock(row.created);
    if (row.is_head) title.append(badge("current", "current"));
    else if (row.state === "instant") title.append(badge("instant"));
    else if (row.state === "rebuild_required")
      title.append(badge("rebuild", "cold"));
    const detail = document.createElement("small");
    detail.textContent = `${ago(row.created)} · ${stateSummary(row)}`;
    open.append(title, detail);
    if (row.state === "rebuild_required") {
      open.disabled = true;
      open.title =
        "This state was built with an earlier geometry build. cad_rebuild recomputes it.";
    } else open.onclick = action(() => showRevision(row.revision));
    item.append(mark, open);
    if (!row.is_head && row.state !== "rebuild_required") {
      const adopt = document.createElement("button");
      adopt.type = "button";
      adopt.className = "state-adopt";
      adopt.textContent = "Make current";
      adopt.title = "Continue from this state; nothing is recomputed";
      arming(adopt, "Make current", () => adoptRevision(row.revision));
      item.append(adopt);
    }
    list.append(item);
  }
  syncStates();
}
/** The bar above the model: which state is on screen, the way back, and the
 *  one tap that swaps between two of them. */
function syncStates() {
  for (const item of document.querySelectorAll<HTMLElement>(".state-row"))
    item.classList.toggle("shown", item.dataset.revision === shownRevision);
  const bar = el("time-travel"),
    // A candidate under review is not a state of the history; the bar stays
    // out of the way until a committed state is on screen again.
    known =
      !!shownRevision &&
      (shownRevision === headRevision ||
        historyRows.some((r: any) => r.revision === shownRevision)),
    travelling = known && shownRevision !== headRevision,
    other =
      compareRevision && compareRevision !== shownRevision
        ? compareRevision
        : travelling
          ? headRevision
          : null;
  el("states").classList.toggle("active", travelling);
  el("states").setAttribute("aria-pressed", String(travelling));
  bar.hidden = !current || !known || (!travelling && !other);
  if (bar.hidden) return;
  const row = historyRows.find((r: any) => r.revision === shownRevision),
    target = historyRows.find((r: any) => r.revision === other);
  text("time-travel-kind", travelling ? "Earlier state" : "Current state");
  text(
    "time-travel-time",
    row ? clock(row.created) : travelling ? shownRevision!.slice(4, 12) : "",
  );
  text("time-travel-detail", stateSummary(row));
  const swap = el<HTMLButtonElement>("time-travel-swap");
  swap.hidden = !other;
  const label = swap.querySelector("span");
  if (label) label.textContent = target ? clock(target.created) : "Compare";
  const adopt = el<HTMLButtonElement>("time-travel-adopt");
  // A bar that comes back shows the plain label again, never a half-armed one.
  if (adopt.hidden === travelling) {
    adopt.textContent = "Make current";
    adopt.classList.remove("armed");
  }
  adopt.hidden = !travelling;
}
/** Moving what "current" means is a small click with a large meaning, and on a
 *  phone it sits next to a harmless one. So it asks twice: the button arms
 *  itself for four seconds, and only the second tap carries it out. */
function arming(
  button: HTMLButtonElement,
  label: string,
  run: () => Promise<void>,
) {
  let armed = 0;
  const rest = () => {
    button.textContent = label;
    button.style.minWidth = "";
    button.classList.remove("armed");
  };
  button.onclick = action(async () => {
    const since = Date.now() - armed;
    // A second press is a decision, not a double tap and not a click a tool
    // repeated: it counts from a quarter of a second after the first.
    if (since < 250) return;
    if (since < 4000) {
      armed = 0;
      rest();
      await run();
      return;
    }
    armed = Date.now();
    // The armed button keeps the width it had, so nothing moves under the
    // finger that is about to press again.
    button.style.minWidth = `${Math.round(button.getBoundingClientRect().width)}px`;
    button.textContent = "Sure?";
    button.classList.add("armed");
    setTimeout(() => {
      if (Date.now() - armed >= 4000) rest();
    }, 4200);
  });
}
/** Put a state on screen. One that has been seen before needs no request. */
async function showRevision(revision: string) {
  if (!current || revision === shownRevision) return;
  const previous = shownRevision,
    instant = shownStates.has(revision);
  if (!instant) busy("Loading this state …");
  try {
    await preview(revision);
    displayRevision = revision;
    if (previous && previous !== revision) compareRevision = previous;
    syncStates();
    // On a phone the sheet has done its job once a state is chosen: the model
    // gets the screen back.
    if (inAppShell()) closeSheet();
    activity(
      revision === headRevision ? "Current state" : "Earlier state",
      `${revision.slice(0, 16)}${instant ? " · shown from memory" : ""}`,
    );
  } finally {
    if (!instant) busy("", false);
  }
}
/** Make the state on screen the current one. The construction and its geometry
 *  already exist, so this is a pointer — no rebuild, no copy, and the state it
 *  leaves stays in the list. */
async function adoptRevision(revision: string) {
  if (!current) return;
  busy("Making this state current …");
  try {
    const result = await api(`/api/models/${current.model_id}/head`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ revision }),
    });
    await openModel(current.model_id, false);
    compareRevision = result.previous_revision ?? compareRevision;
    syncStates();
    activity(
      "State restored",
      `${revision.slice(0, 16)} is current again · nothing was recomputed.`,
    );
    toast("This state is the current one again.");
  } finally {
    busy("", false);
  }
}
/** Keep the state before the one on screen ready, so the first step back is as
 *  quick as every one after it. */
function prefetchNeighbour() {
  if (!current || !shownRevision) return;
  const shown = historyRows.find((r: any) => r.revision === shownRevision),
    usable = (r: any) => r && r.state !== "rebuild_required",
    parent = historyRows.find((r: any) => r.revision === shown?.parent),
    target = usable(parent)
      ? parent
      : historyRows.find((r: any) => r.revision !== shownRevision && usable(r));
  if (!target || shownStates.has(target.revision)) return;
  void (async () => {
    try {
      const data = await previewData(target.revision);
      if (!shownStates.has(target.revision))
        remember(target.revision, data, build(data));
    } catch {
      // A state kept ready in advance is a convenience, never a failure.
    }
  })();
}
el("states").onclick = () => {
  if (inAppShell()) openPanel("more");
  else {
    const toggle = document.querySelector<HTMLButtonElement>(
      '[aria-controls="group-versions"]',
    );
    if (toggle) {
      setGroup(toggle, true);
      toggle.scrollIntoView({ block: "nearest", behavior: "smooth" });
    }
  }
  void loadHistory().catch(() => {});
};
el("time-travel-swap").onclick = action(async () => {
  const other =
    compareRevision && compareRevision !== shownRevision
      ? compareRevision
      : headRevision;
  if (other) await showRevision(other);
});
arming(el<HTMLButtonElement>("time-travel-adopt"), "Make current", async () => {
  if (shownRevision) await adoptRevision(shownRevision);
});
el("time-travel-exit").onclick = action(async () => {
  if (headRevision && shownRevision !== headRevision)
    await showRevision(headRevision);
  else {
    compareRevision = null;
    syncStates();
  }
});
// One key for the comparison: press it again and the other state is back.
document.addEventListener("keydown", (event) => {
  if (event.key !== "v" || event.metaKey || event.ctrlKey || event.altKey)
    return;
  if ((event.target as HTMLElement)?.closest("input, textarea, select")) return;
  if (el("time-travel").hidden || el("time-travel-swap").hidden) return;
  el("time-travel-swap").click();
});
/* --- Change requests and the agent that carries them out ------------------ */
let orderTimer: number | null = null;
function orderCard(order: any) {
  const card = document.createElement("div");
  card.className = "order";
  card.dataset.state = order.state;
  const image = document.createElement("img");
  image.alt = "";
  image.loading = "lazy";
  if (order.region) image.src = order.region;
  const body = document.createElement("div"),
    note = document.createElement("div"),
    meta = document.createElement("div"),
    state = document.createElement("span"),
    actions = document.createElement("div");
  note.className = "order-note";
  note.textContent = order.note;
  meta.className = "order-meta";
  state.className = "order-state";
  state.textContent = order.state;
  const run = order.run ?? {},
    when = new Date(order.created).toLocaleString(),
    details = [
      when,
      run.model ? `${run.model} · ${run.effort}` : null,
      run.stage === "proposal" && order.state !== "proposed"
        ? "proposal"
        : null,
      order.state === "running" ? (run.activity ?? "…") : null,
      run.cost_usd ? `$${run.cost_usd.toFixed(2)}` : null,
      run.turns ? `${run.turns} steps` : null,
      order.images ? `${order.images} attached` : null,
    ].filter(Boolean);
  meta.append(state, document.createTextNode(details.join(" · ")));
  // What the agent answered is the point of the whole exercise, so it is shown
  // in full: a run that changed nothing has its reasons, and they are here.
  const answer = [
    run.error,
    order.state === "proposed" ? run.proposal : run.summary,
  ]
    .filter(Boolean)
    .join("\n\n");
  if (answer) {
    const block = document.createElement("p");
    block.className = "order-answer";
    block.textContent = answer;
    body.append(note, meta, block);
  }
  actions.className = "order-actions";
  const button = (label: string, run: () => Promise<void>, primary = false) => {
    const element = document.createElement("button");
    element.type = "button";
    element.textContent = label;
    if (primary) element.className = "primary";
    element.onclick = action(run);
    return element;
  };
  // A change is never carried out unasked: the agent proposes, a person accepts.
  const start = (stage: "proposal" | "execution") => async () => {
    await api(`/api/annotations/${order.annotation_id}/dispatch`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ stage }),
    });
    await loadOrders();
  };
  if (order.state === "running")
    actions.append(
      button("Stop", async () => {
        await api(`/api/annotations/${order.annotation_id}/cancel`, {
          method: "POST",
        });
        await loadOrders();
      }),
    );
  else if (order.state === "proposed")
    actions.append(
      button("Accept · carry out", start("execution"), true),
      button("Propose again", start("proposal")),
      button("Delete", async () => {
        await api(`/api/annotations/${order.annotation_id}`, {
          method: "DELETE",
        });
        await loadOrders();
      }),
    );
  else
    actions.append(
      button(
        order.state === "captured" ? "Ask AI for a proposal" : "Try again",
        start("proposal"),
        order.state === "captured",
      ),
      button("Delete", async () => {
        await api(`/api/annotations/${order.annotation_id}`, {
          method: "DELETE",
        });
        await loadOrders();
      }),
    );
  if (!body.children.length) body.append(note, meta);
  body.append(actions);
  card.append(image, body);
  return card;
}
async function loadOrders() {
  const list = el("orders");
  if (!list) return;
  const data = await api(
    "/api/annotations" + (current ? `?model=${current.model_id}` : ""),
  );
  list.replaceChildren(...data.annotations.map(orderCard));
  if (!data.annotations.length)
    list.textContent = "No change requests yet. Mark an area to create one.";
  text(
    "orders-count",
    data.annotations.length ? String(data.annotations.length) : "",
  );
  // While something is running the list follows along by itself.
  const busyOrders = data.annotations.some((a: any) => a.state === "running");
  if (busyOrders && orderTimer === null)
    orderTimer = window.setInterval(() => void loadOrders(), 2500);
  if (!busyOrders && orderTimer !== null) {
    clearInterval(orderTimer);
    orderTimer = null;
  }
}
async function loadAgentSettings() {
  const settings = await api("/api/agent/settings");
  el<HTMLSelectElement>("agent-model").value = settings.model;
  el<HTMLSelectElement>("agent-effort").value = settings.effort;
  el<HTMLSelectElement>("agent-proposal-effort").value =
    settings.proposal_effort;
  el<HTMLSelectElement>("agent-permissions").value = settings.permission_mode;
  el<HTMLInputElement>("agent-budget").value =
    settings.max_budget_usd == null ? "" : String(settings.max_budget_usd);
}
const saveAgentSettings = action(async () => {
  const budget = el<HTMLInputElement>("agent-budget").value.trim();
  await api("/api/agent/settings", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      model: el<HTMLSelectElement>("agent-model").value,
      effort: el<HTMLSelectElement>("agent-effort").value,
      proposal_effort: el<HTMLSelectElement>("agent-proposal-effort").value,
      permission_mode: el<HTMLSelectElement>("agent-permissions").value,
      max_budget_usd: budget === "" ? null : Number(budget),
    }),
  });
});
for (const id of [
  "agent-model",
  "agent-effort",
  "agent-proposal-effort",
  "agent-permissions",
  "agent-budget",
])
  el(id).onchange = saveAgentSettings;

/* --- Phone app shell ------------------------------------------------------
 * On a phone the page becomes an app: the model fills the screen, three round
 * buttons carry the frequent actions, and every panel arrives in a sheet —
 * from the tab bar for the four main sections, from the two top buttons for
 * models and the workshop menu. The presentation follows iOS: a grabber, two
 * detents, drag to dismiss, a dimmed backdrop only at the tall detent so the
 * model can still be turned behind a half-height sheet. On the desktop the
 * very same panels sit back in their sidebars.
 */
type Layout = "desktop" | "mobile";
type Detent = "medium" | "large";
const LAYOUT_KEY = "mathforge.layout",
  // A phone whose browser lays the page out at 980 px ("desktop site") still
  // has a coarse pointer; a tablet in landscape keeps the desktop page.
  PHONE_QUERY = window.matchMedia(
    "(max-width: 860px), ((pointer: coarse) and (max-width: 1000px))",
  ),
  PANELS: Record<string, { title: string; groups: string[]; tab?: boolean }> = {
    model: { title: "Model", groups: [], tab: true },
    structure: { title: "Parts", groups: ["structure"], tab: true },
    features: { title: "Construction", groups: ["features"], tab: true },
    detail: { title: "Detail", groups: ["detail", "change"], tab: true },
    models: { title: "Models", groups: ["workspace", "examples"] },
    view: { title: "View", groups: ["view"] },
    more: {
      title: "Workshop",
      groups: ["versions", "orders", "system", "export"],
    },
  };
let activePanel = "model",
  toolbarAnchor: Element | null = null;
const tabButtons = () =>
  Array.from(document.querySelectorAll<HTMLButtonElement>(".tab"));
const inAppShell = () => document.body.dataset.layout === "mobile";
function groupToggles() {
  return Array.from(
    document.querySelectorAll<HTMLButtonElement>(".group-toggle"),
  );
}
function setGroup(button: HTMLButtonElement, open: boolean) {
  button.setAttribute("aria-expanded", String(open));
  const body = document.getElementById(
    button.getAttribute("aria-controls") ?? "",
  );
  if (body) body.hidden = !open;
}
for (const button of groupToggles())
  button.onclick = () =>
    setGroup(button, button.getAttribute("aria-expanded") !== "true");

/** Move the panels, the view toolbar and the header actions into the sheet;
 *  the nodes keep their handlers and state. */
function enterAppShell() {
  const body = el("sheet-body");
  for (const group of document.querySelectorAll<HTMLElement>(
    "aside .panel-group",
  ))
    body.append(group);
  const toolbar = document.querySelector<HTMLElement>(".view-toolbar");
  if (toolbar) {
    toolbarAnchor = toolbar.nextElementSibling;
    el("group-view").append(toolbar);
    // The display channel becomes a labelled row of the list.
    const row = document.createElement("label"),
      caption = document.createElement("span");
    row.className = "select-row";
    row.htmlFor = "overlay";
    caption.textContent = "Display";
    row.append(caption, el("overlay"));
    toolbar.append(row);
  }
  const actions = document.querySelector<HTMLElement>(".header-actions");
  if (actions) el("group-system").append(actions);
  el("app-shell").hidden = false;
  closeSheet();
}
/** Put everything back where the desktop page keeps it. */
function leaveAppShell() {
  closeSheet();
  const groups = Array.from(
    document.querySelectorAll<HTMLElement>(
      '#sheet-body .panel-group[data-home="left"], #sheet-body .panel-group[data-home="right"]',
    ),
  ).sort((a, b) => Number(a.dataset.order) - Number(b.dataset.order));
  for (const group of groups)
    el(group.dataset.home === "right" ? "panel-right" : "panel-left").append(
      group,
    );
  const toolbar = document.querySelector<HTMLElement>(
    "#group-view .view-toolbar",
  );
  if (toolbar) {
    toolbar.append(el("overlay"));
    toolbar.querySelector(".select-row")?.remove();
    if (toolbarAnchor?.parentElement === el("viewport"))
      toolbarAnchor.before(toolbar);
    else el("viewport").append(toolbar);
  }
  const actions = document.querySelector<HTMLElement>(
    "#group-system .header-actions",
  );
  if (actions) document.querySelector("header")?.append(actions);
  el("app-shell").hidden = true;
  // Every panel is visible again in its sidebar, whatever the last sheet showed.
  for (const group of document.querySelectorAll<HTMLElement>(".panel-group"))
    group.hidden = false;
  for (const button of groupToggles()) setGroup(button, true);
}
function openSheet(detent: Detent = "medium") {
  el("app-shell").dataset.open = "true";
  el("app-shell").dataset.detent = detent;
  el("app-sheet").dataset.detent = detent;
  el("app-sheet").style.transform = "";
}
function closeSheet() {
  el("app-shell").dataset.open = "false";
  el("app-sheet").style.transform = "";
  activePanel = "model";
  for (const button of tabButtons())
    button.setAttribute(
      "aria-selected",
      String(button.dataset.tab === "model"),
    );
}
function openPanel(name: string) {
  const spec = PANELS[name] ?? PANELS.model;
  if (!spec.groups.length) {
    closeSheet();
    return;
  }
  activePanel = name;
  // A sheet from the top buttons is modal over the sections: no tab is lit.
  for (const button of tabButtons())
    button.setAttribute(
      "aria-selected",
      String(!!spec.tab && button.dataset.tab === name),
    );
  el("sheet-body").dataset.single = String(spec.groups.length === 1);
  for (const group of document.querySelectorAll<HTMLElement>(
    "#sheet-body .panel-group",
  )) {
    const shown = spec.groups.includes(group.dataset.group ?? ""),
      toggle = group.querySelector<HTMLButtonElement>(".group-toggle");
    group.hidden = !shown;
    if (shown && toggle) setGroup(toggle, true);
  }
  text("sheet-title", spec.title);
  el("sheet-body").scrollTop = 0;
  openSheet("medium");
}
for (const button of tabButtons())
  button.onclick = () => {
    const tab = button.dataset.tab ?? "model";
    // Tapping the open tab again gives the model the whole screen back.
    if (tab === activePanel && tab !== "model") closeSheet();
    else openPanel(tab);
  };
el("top-models").onclick = () => openPanel("models");
el("top-more").onclick = () => openPanel("more");
el("fab-view").onclick = () => openPanel("view");
el("fab-fit").onclick = fit;
el("fab-explode").onclick = () => el("explode").click();
el("sheet-close").onclick = closeSheet;
el("sheet-backdrop").onclick = closeSheet;
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && el("app-shell").dataset.open === "true")
    closeSheet();
});
/* Drag the grabber: down dismisses, up switches to the tall detent. */
let dragFrom = 0,
  dragBy = 0;
function beginDrag(event: PointerEvent) {
  if (el("app-shell").dataset.open !== "true") return;
  // A tap on "Done" is a tap, not the start of a drag: capturing the pointer
  // here would swallow the button's click.
  if ((event.target as HTMLElement).closest("button")) return;
  dragFrom = event.clientY;
  dragBy = 0;
  el("app-shell").dataset.dragging = "true";
  (event.currentTarget as HTMLElement).setPointerCapture(event.pointerId);
}
function moveDrag(event: PointerEvent) {
  if (el("app-shell").dataset.dragging !== "true") return;
  dragBy = event.clientY - dragFrom;
  // Pulling up resists, the way a sheet behaves at its top detent.
  const shift = dragBy < 0 ? Math.max(dragBy / 3, -70) : dragBy;
  el("app-sheet").style.transform = `translateY(${shift}px)`;
}
function endDrag() {
  const shell = el("app-shell");
  if (shell.dataset.dragging !== "true") return;
  shell.dataset.dragging = "false";
  const sheet = el("app-sheet"),
    height = sheet.getBoundingClientRect().height;
  sheet.style.transform = "";
  if (dragBy > height * 0.28) closeSheet();
  else if (dragBy < -40) openSheet("large");
  else if (dragBy > 60 && sheet.dataset.detent === "large") openSheet("medium");
}
for (const handle of [
  el("sheet-grabber"),
  ...Array.from(document.querySelectorAll<HTMLElement>(".sheet-header")),
]) {
  handle.addEventListener("pointerdown", beginDrag);
  handle.addEventListener("pointermove", moveDrag);
  handle.addEventListener("pointerup", endDrag);
  handle.addEventListener("pointercancel", endDrag);
}

function applyLayout(mode: Layout) {
  const previous = document.body.dataset.layout;
  document.body.dataset.layout = mode;
  const toggle = el<HTMLButtonElement>("layout-toggle");
  toggle.setAttribute("aria-pressed", String(mode === "mobile"));
  const caption = mode === "mobile" ? "Desktop view" : "Phone view",
    label = toggle.querySelector(".layout-toggle-text");
  if (label) label.textContent = caption;
  toggle.setAttribute("aria-label", `Switch to the ${caption.toLowerCase()}`);
  toggle.title = `Switch to the ${caption.toLowerCase()}`;
  if (mode === "mobile" && previous !== "mobile") enterAppShell();
  if (mode === "desktop" && previous === "mobile") leaveAppShell();
  resize();
  // The viewport changes shape with the layout; frame the model for the new
  // aspect ratio, which corrects a view that became too wide as well as one
  // that became too tight.
  requestAnimationFrame(() => {
    resize();
    if (meshData) dollyToFit();
  });
}
/** Bring a panel up once the app has something to show in it. */
function revealGroup(name: string) {
  if (!inAppShell() || openingModel) return;
  const panel = Object.keys(PANELS).find((key) =>
    PANELS[key].groups.includes(name),
  );
  if (panel) openPanel(panel);
}
function storedLayout(): Layout | null {
  try {
    const value = localStorage.getItem(LAYOUT_KEY);
    return value === "mobile" || value === "desktop" ? value : null;
  } catch {
    return null; // blocked browser storage: fall back to the screen width
  }
}
el("layout-toggle").onclick = () => {
  const next: Layout = inAppShell() ? "desktop" : "mobile";
  try {
    localStorage.setItem(LAYOUT_KEY, next);
  } catch {
    /* without storage the choice simply lasts for this visit */
  }
  applyLayout(next);
};
PHONE_QUERY.addEventListener("change", (event) => {
  if (!storedLayout()) applyLayout(event.matches ? "mobile" : "desktop");
});
applyLayout(storedLayout() ?? (PHONE_QUERY.matches ? "mobile" : "desktop"));
