import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { RoomEnvironment } from "three/addons/environments/RoomEnvironment.js";
import { relativePositions } from "../derived-geometry/coordinates.js";
import { drawingState } from "./drawing-state.js";
import {
  planExplosion,
  explosionPosition,
  type ExplosionPart,
  type ExplosionTransform,
  type Point3,
} from "./explosion.js";
import {
  dominantSurfaceNormal,
  explosionViewBasis,
} from "./explosion-orientation.js";
import {
  applyResolvedCamera,
  containWindow,
  describeCamera,
  isAbsoluteCamera,
  resolveCamera,
  type FrameWindow,
  type ResolvedCamera,
} from "./reference-camera.js";
import {
  decalMaterial,
  decalProblems,
  projectDecal,
  svgTexture,
  type DecalSpec,
  type DecalTexture,
} from "./decals.js";
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
/* State of the reference view, the configurations, the prints and the feature
 * proposals. It is declared up here because the drawing code consults it from
 * the first frame on; the code that works with it is at the end of the file. */
type OverlayData = {
  revision: string;
  configurations: any[];
  decals: DecalSpec[];
  unavailable?: boolean;
};
type RefView = {
  ref: any;
  image: ImageBitmap | null;
  /** Set while the camera sits in the reference's registered view. */
  resolved: ResolvedCamera | null;
  /** The part of the reference frame the pane shows (zoom and pan). */
  window: FrameWindow | null;
  overlay: boolean;
  opacity: number;
  source: "photo" | "heatmap" | "gates";
  split: boolean;
  heat: HTMLCanvasElement | null;
  heatStats: {
    iou: number;
    both: number;
    model_only: number;
    photo_only: number;
    pixels: number;
  } | null;
  gates: ImageBitmap | null;
};
let refView: RefView | null = null;
const overlayCache = new Map<string, Promise<OverlayData>>(),
  overlayResolved = new Map<string, OverlayData>();
let chosenConfiguration: string | null = null;
const decalMeshes: THREE.Mesh[] = [];
let proposalBatches: any[] = [];
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
/** Orders that are actually being worked on right now. */
const ACTIVE = ["queued", "running"];
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
  syncHeadWatch();
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
      ? [
          `${building ? "Building" : "Writing the plan"} …`,
          ...progressFigures({
            state: order.state,
            run: { progress: order.progress, commits: order.commits },
          }),
          order.activity,
        ]
          .filter(Boolean)
          .join(" · ")
      : order?.state === "incomplete" && order.kind === "model_build"
        ? `Build stopped early${order.incomplete_reason ? `: ${order.incomplete_reason}` : ""}`
        : summarise(m.purpose) || "No plan yet";
    if (!busyWith && order?.state === "incomplete")
      row.dataset.incomplete = "true";
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
  // The row above is the short summary; the structured plan the intake wrote
  // (parts, details, dimensions, references) is one tap away.
  const full = document.createElement("button");
  full.type = "button";
  full.className = "plan-full";
  full.textContent = "Full plan";
  full.onclick = action(async () => showBuildPlan(model.id));
  box.append(full);
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
  if (current?.model_id === model.id) void loadOrders().catch(() => {});
  toast(
    `“${model.name}” is being built. You can carry on; every commit shows up here.`,
  );
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
    if (order?.state === "incomplete") {
      // Something was committed, but the run did not finish its job: the
      // model shows what there is, the order says what is missing.
      toast(
        `“${m.name}” stopped early${order.incomplete_reason ? `: ${order.incomplete_reason}` : "."}`,
      );
      if (building && current?.model_id === m.id)
        void openModel(m.id, false).catch(() => {});
    }
    if (order?.state === "failed")
      toast(
        building
          ? `“${m.name}” could not be built.`
          : `The plan for “${m.name}” could not be written.`,
      );
    if (current?.model_id === m.id) void loadOrders().catch(() => {});
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
// Glass needs reflected surroundings to read as glass; highlights are lighting,
// never white disks or dots baked into the CAD model. Other channels keep their
// existing lighting so diagnostic colours remain comparable.
const reflectionRoom = new RoomEnvironment();
const reflectionGenerator = new THREE.PMREMGenerator(renderer);
const glassReflection = reflectionGenerator.fromScene(
  reflectionRoom,
  0.03,
).texture;
reflectionRoom.dispose();
reflectionGenerator.dispose();
renderer.domElement.id = "model-canvas";
viewport.prepend(renderer.domElement);
let controls = new OrbitControls<
  THREE.PerspectiveCamera | THREE.OrthographicCamera
>(activeCamera, renderer.domElement);
controls.enableDamping = true;
controls.dampingFactor = 0.08;
/** OrbitControls caches the camera's up axis when constructed. Recreate it
 * after an axis change, also discarding residual motion from a previous drag. */
function renewOrbitControls() {
  const target = controls.target.clone(),
    enabled = controls.enabled;
  controls.dispose();
  controls = new OrbitControls(activeCamera, renderer.domElement);
  controls.target.copy(target);
  controls.enabled = enabled;
  controls.enableDamping = true;
  controls.dampingFactor = 0.08;
  controls.update();
}
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
  get controls() {
    return controls;
  },
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
type ExplodeInfo = ExplosionTransform;
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
/** Set while a reference capture renders: neutral tone mapping instead of ACES. */
let referenceToneMapping = false;
let overlayMode: OverlayMode = "parts",
  lodTarget = 0.02,
  currentMmPerPixel = 0.1,
  lastScalePx = -1;
/** Part colours: parts with the same name share one colour, so identical
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
  [/^Copper layer top/i, "#c9793a"],
  [/^Copper layer bottom/i, "#8d5a2e"],
  [/^Via/i, "#b9bcc0"],
];
function assignGroupColours() {
  groupColours.clear();
  const keys = [...new Set([...partNames.values()].map(groupKey))].sort(
    (a, b) => a.localeCompare(b, "en"),
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
/** Semantic anchor of a viewer hit: world point, native normal, barycentric coordinates, camera. */
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
/** Display channels: lit shading, an unlit diagnostic channel, normals and native per-face curvature. */
function materialFor(
  m: any,
  ghost: boolean,
  geometry: THREE.BufferGeometry,
): THREE.Material {
  // The interactive view keeps ACES; a reference capture uses Khronos PBR
  // Neutral, which leaves declared colours where they are (ACES alone moved
  // a printed logo by ΔE00 ≈ 11.6).
  renderer.toneMapping =
    overlayMode === "original"
      ? referenceToneMapping
        ? THREE.NeutralToneMapping
        : THREE.ACESFilmicToneMapping
      : THREE.NoToneMapping;
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
    const glass = entry?.finish === "clear glass";
    const material = new THREE.MeshPhysicalMaterial({
      ...common,
      color: new THREE.Color(entry?.colour ?? "#9aa3a8"),
      roughness: entry?.roughness ?? 0.55,
      metalness: entry?.metalness ?? 0.1,
      transparent: !glass && opacity < 1,
      opacity: glass ? 1 : opacity,
      transmission: glass ? (entry?.transmission ?? 0.95) : 0,
      ior: glass ? (entry?.ior ?? 1.52) : 1.5,
      thickness: glass ? (entry?.thickness_mm ?? 2) : 0,
      envMap: glassReflection,
      envMapIntensity: glass ? 1.2 : (entry?.metalness ?? 0) > 0.5 ? 0.9 : 0.22,
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
    if (!ghost) prepareExplosion();
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
  // The chosen configuration and the prints belong to every state shown.
  if (!ghost && group === mainGroup) {
    applyConfiguration();
    applyDecals();
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
/** Scale bar from the camera's current millimetres per pixel. */
function updateScaleBar() {
  const h = renderer.domElement.clientHeight || 1,
    w = renderer.domElement.clientWidth || 1;
  // A view window (a registered reference view zoomed in) shows only part of
  // the camera's frame, so each pixel covers that much less.
  const view = activeCamera.view?.enabled ? activeCamera.view : null;
  currentMmPerPixel =
    activeCamera === camera
      ? ((2 *
          camera.position.distanceTo(controls.target) *
          Math.tan(THREE.MathUtils.degToRad(camera.fov / 2))) /
          h) *
        (view ? view.height / view.fullHeight : 1)
      : ((orthoCamera.right - orthoCamera.left) / orthoCamera.zoom / w) *
        (view ? view.width / view.fullWidth : 1);
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
/** Protected and change regions of the selected feature in world coordinates. */
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
  // Framing the model is leaving a registered reference view.
  if (snapped()) leaveReferenceView();
  if (explodeAmount > 0) {
    orientExplosionView();
    dollyToFit();
    return;
  }
  const box = new THREE.Box3().setFromObject(mainGroup);
  if (box.isEmpty()) return;
  const size = box.getSize(new THREE.Vector3()),
    center = box.getCenter(new THREE.Vector3()),
    radius = Math.max(size.length(), 1);
  controls.target.copy(center);
  camera.up.set(0, 0, 1);
  orthoCamera.up.set(0, 0, 1);
  activeCamera.position
    .copy(center)
    .add(new THREE.Vector3(0.9, -1.15, 0.9).multiplyScalar(radius));
  orthoCamera.zoom = 50 / radius;
  orthoCamera.updateProjectionMatrix();
  grid.position.set(center.x, center.y, box.min.z - 0.1);
  renewOrbitControls();
  // The distance above assumes a landscape view; on a portrait screen (a
  // phone) the width is what limits, so the model is framed in camera
  // coordinates instead, keeping the viewing direction.
  if (renderer.domElement.clientWidth < renderer.domElement.clientHeight)
    dollyToFit();
}
const resize = () => {
  // Side by side, the model takes the right half and the photo the left.
  const split = referenceSplit(),
    w = split
      ? Math.max(1, Math.floor(viewport.clientWidth / 2))
      : viewport.clientWidth,
    h = viewport.clientHeight;
  renderer.setSize(w, h);
  if (snapped()) {
    // A registered view keeps its frame; only the window onto it follows the pane.
    applyReferenceWindow(true);
    return;
  }
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
  orthoCamera.left = (-35 * w) / h;
  orthoCamera.right = (35 * w) / h;
  orthoCamera.updateProjectionMatrix();
  drawReferenceOverlay();
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
  if (snapped()) leaveReferenceView();
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
  if (explodeAmount > 0) dollyToFit();
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
  for (const decal of decalMeshes)
    (decal.material as THREE.Material).clippingPlanes = section
      ? [clipping]
      : [];
};
el("measure").onclick = () => {
  measuring = !measuring;
  measurementPoints.length = 0;
  clearGroup(markerGroup);
  el("measure").classList.toggle("active", measuring);
  el("measurement").hidden = !measuring;
  text("measurement", "Select two points on the surface.");
};
/** Pixel-size LOD: the target deflection follows the current screen resolution and,
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
    // Blocked browser storage: the choice lasts for this session only.
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
  // Blocked browser storage: the default channel stays "parts".
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
 * Every semantic part travels along one common axis. Optical instruments use
 * their principal viewing lens normal; the camera presents it left to right.
 */
const EXPLODE_DURATION = 1400;
const reducedMotion = () =>
  window.matchMedia("(prefers-reduced-motion: reduce)").matches;
function prepareExplosion() {
  const meshes = mainGroup.children as THREE.Mesh[];
  const definitions = new Map<string, any>(
    structureEntries.parts.map((part: any) => [part.entity_id, part]),
  );
  const owners = new Map<string, string>(
    (current?.features ?? []).map((feature: any) => [
      feature.id,
      feature.owner_part,
    ]),
  );
  for (const part of structureEntries.parts)
    for (const output of part.definition?.outputs ?? [])
      if (!owners.has(output)) owners.set(output, part.entity_id);
  const inputs: ExplosionPart[] = meshes.map((mesh, index) => {
    const geometry = mesh.geometry as THREE.BufferGeometry;
    if (!geometry.boundingBox) geometry.computeBoundingBox();
    const origin = new THREE.Vector3().fromArray(mesh.userData.origin),
      box = geometry.boundingBox!.clone().translate(origin),
      feature = mesh.userData.feature_id ?? `mesh-${index}`,
      owner = owners.get(feature),
      part = owner ? definitions.get(owner) : undefined;
    return {
      feature,
      owner,
      name: part?.semantic_name,
      assembly: part?.definition?.assembly,
      bounds: [
        box.min.x,
        box.min.y,
        box.min.z,
        box.max.x,
        box.max.y,
        box.max.z,
      ],
    };
  });
  const candidates = inputs
    .map((input, index) => {
      const entry = materialOf(input.feature);
      const label =
        `${input.owner ?? ""} ${input.name ?? ""} ${entry?.name ?? ""}`.toLowerCase();
      const score = /viewing window|magnifier|viewing.*glass|front.*lens/.test(
        label,
      )
        ? 80
        : entry?.finish === "clear glass"
          ? 50
          : /lens/.test(label) && !/wheel|ring|carrier/.test(label)
            ? 20
            : 0;
      const size = Math.hypot(
        ...input.bounds
          .slice(0, 3)
          .map((value, i) => input.bounds[i + 3] - value),
      );
      return { index, score, size, feature: input.feature };
    })
    .filter((candidate) => candidate.score > 0)
    .sort(
      (a, b) =>
        b.score - a.score ||
        b.size - a.size ||
        a.feature.localeCompare(b.feature),
    );
  let axis: Point3 | null = null;
  let axisSource = "model-shortest-extent";
  for (const candidate of candidates) {
    const geometry = meshes[candidate.index].geometry;
    const positions = geometry.getAttribute("position");
    const values = new Float64Array(positions.count * 3);
    for (let i = 0; i < positions.count; i++) {
      values[3 * i] = positions.getX(i);
      values[3 * i + 1] = positions.getY(i);
      values[3 * i + 2] = positions.getZ(i);
    }
    axis = dominantSurfaceNormal(values, geometry.index?.array ?? null);
    if (axis) {
      axisSource = `lens:${candidate.feature}`;
      break;
    }
  }
  if (!axis) {
    const box = new THREE.Box3();
    for (const input of inputs)
      box.union(
        new THREE.Box3(
          new THREE.Vector3(...input.bounds.slice(0, 3)),
          new THREE.Vector3(...input.bounds.slice(3, 6)),
        ),
      );
    const sizes = box.isEmpty()
      ? [0, 0, 0]
      : box.getSize(new THREE.Vector3()).toArray();
    const index = sizes.indexOf(Math.min(...sizes));
    axis = [Number(index === 0), Number(index === 1), Number(index === 2)];
  }
  // Project actual assembled vertices, not the corners of a world AABB. This
  // measures the required clearances along a tilted optical axis as well.
  const rowAxis = explosionViewBasis(axis).up;
  for (let index = 0; index < inputs.length; index++) {
    const positions = meshes[index].geometry.getAttribute("position");
    const origin = meshes[index].userData.origin as number[];
    const originProjection =
      origin[0] * axis[0] + origin[1] * axis[1] + origin[2] * axis[2];
    const originRow =
      origin[0] * rowAxis[0] + origin[1] * rowAxis[1] + origin[2] * rowAxis[2];
    let min = Infinity,
      max = -Infinity,
      rowMin = Infinity,
      rowMax = -Infinity;
    for (let i = 0; i < positions.count; i++) {
      const value =
        positions.getX(i) * axis[0] +
        positions.getY(i) * axis[1] +
        positions.getZ(i) * axis[2] +
        originProjection;
      min = Math.min(min, value);
      max = Math.max(max, value);
      const row =
        positions.getX(i) * rowAxis[0] +
        positions.getY(i) * rowAxis[1] +
        positions.getZ(i) * rowAxis[2] +
        originRow;
      rowMin = Math.min(rowMin, row);
      rowMax = Math.max(rowMax, row);
    }
    inputs[index].projectedBounds = [min, max];
    inputs[index].rowBounds = [rowMin, rowMax];
    meshes[index].userData.explosionProjection = [min, max];
    meshes[index].userData.part = inputs[index].owner;
  }
  const plan = planExplosion(inputs, { axis, gapMm: 30, rowAxis, rowGapMm: 2 });
  mainGroup.userData.explosionProfile = plan.profile;
  mainGroup.userData.explosionAxis = plan.axis;
  mainGroup.userData.explosionAxisSource = axisSource;
  mainGroup.userData.explosionGapMm = plan.gapMm;
  mainGroup.userData.explosionOrder = plan.order;
  mainGroup.userData.explosionLayout = plan.layout;
  mainGroup.userData.explosionRowAxis = rowAxis;
  mainGroup.userData.explosionRowGapMm = 2;
  text(
    "explode-hint",
    "One axis; free rows share space. At 100%: at least 30 mm between parts occupying the same row.",
  );
  for (let index = 0; index < meshes.length; index++)
    meshes[index].userData.explode = plan.features.get(inputs[index].feature);
  applyExplosion(explodeAmount);
}
function orientExplosionView() {
  const axis = mainGroup.userData.explosionAxis as Point3 | undefined;
  if (!axis) return;
  const basis = explosionViewBasis(axis);
  const distance = Math.max(
    activeCamera.position.distanceTo(controls.target),
    1,
  );
  camera.up.fromArray(basis.up);
  orthoCamera.up.fromArray(basis.up);
  activeCamera.position
    .copy(controls.target)
    .addScaledVector(new THREE.Vector3(...basis.direction), distance);
  renewOrbitControls();
}
function applyExplosion(amount: number) {
  for (const mesh of mainGroup.children as THREE.Mesh[]) {
    const origin = mesh.userData.origin as number[] | undefined;
    if (!origin) continue;
    const info = mesh.userData.explode as ExplodeInfo | undefined;
    mesh.position.fromArray(
      info ? explosionPosition(origin, info, amount) : origin,
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
  const right = new THREE.Vector3()
      .crossVectors(activeCamera.up, direction)
      .normalize(),
    up = new THREE.Vector3().crossVectors(direction, right).normalize();
  // Fit the component bounds in camera coordinates. A sphere around a long
  // exploded row wastes most of the viewport height and makes details tiny.
  const extents: THREE.Vector3[] = [];
  for (const mesh of mainGroup.children as THREE.Mesh[]) {
    if (!mesh.geometry.boundingBox) mesh.geometry.computeBoundingBox();
    const bounds = mesh.geometry.boundingBox!;
    for (let i = 0; i < 8; i++) {
      const point = new THREE.Vector3(
        i & 1 ? bounds.max.x : bounds.min.x,
        i & 2 ? bounds.max.y : bounds.min.y,
        i & 4 ? bounds.max.z : bounds.min.z,
      )
        .applyMatrix4(mesh.matrixWorld)
        .sub(centre);
      extents.push(
        new THREE.Vector3(
          point.dot(right),
          point.dot(up),
          point.dot(direction),
        ),
      );
    }
  }
  if (activeCamera === camera) {
    const vertical = (camera.fov * Math.PI) / 180,
      horizontal = 2 * Math.atan(Math.tan(vertical / 2) * camera.aspect),
      distance = Math.max(
        1,
        ...extents.map(
          (point) =>
            point.z +
            1.1 *
              Math.max(
                Math.abs(point.x) / Math.tan(horizontal / 2),
                Math.abs(point.y) / Math.tan(vertical / 2),
              ),
        ),
      );
    camera.position.copy(centre).add(direction.multiplyScalar(distance));
  } else {
    orthoCamera.position.copy(centre).add(direction.multiplyScalar(radius * 4));
    const x = Math.max(1e-3, ...extents.map((point) => Math.abs(point.x))),
      y = Math.max(1e-3, ...extents.map((point) => Math.abs(point.y)));
    orthoCamera.zoom = Math.min(
      (orthoCamera.right - orthoCamera.left) / (2.2 * x),
      (orthoCamera.top - orthoCamera.bottom) / (2.2 * y),
    );
    orthoCamera.updateProjectionMatrix();
  }
  controls.target.copy(centre);
  controls.update();
}
function setExplode(target: number, animate = true) {
  // An exploded view turns the camera; a registered reference view cannot stay.
  if (target > 0 && snapped()) leaveReferenceView();
  if (target > 0 && explodeAmount <= 1e-9) orientExplosionView();
  explodeTo = THREE.MathUtils.clamp(target, 0, 1);
  if (animate && !reducedMotion() && mainGroup.children.length > 1) {
    explodeFrom = explodeAmount;
    explodeStart = performance.now();
    explodeRunning = true;
  } else {
    explodeAmount = explodeTo;
    explodeRunning = false;
    applyExplosion(explodeAmount);
    dollyToFit();
  }
  syncExplodeUI();
}
/** One progress value moves every part together and reverses without jumps. */
function tickExplosion(now: number) {
  if (!explodeRunning) return;
  const progress = THREE.MathUtils.clamp(
    (now - explodeStart) / EXPLODE_DURATION,
    0,
    1,
  );
  explodeAmount = explodeFrom + (explodeTo - explodeFrom) * easeInOut(progress);
  applyExplosion(explodeAmount);
  dollyToFit();
  if (progress >= 1) {
    explodeAmount = explodeTo;
    explodeRunning = false;
    if (explodeAmount > 0.001) retireExplodeHint();
    // Keep the whole assembly in frame without changing the viewing direction.
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
  let result: any;
  try {
    result = await job(
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
  } catch (error) {
    if (
      featureID ||
      lod.adaptive ||
      lod.region ||
      !/^BUILD_MISMATCH:/.test((error as Error).message)
    )
      throw error;
    // The immutable preview was generated by the revision's original build.
    // Discovery validates its model, revision and geometry binding on the server;
    // no current-build geometry is substituted for an older revision.
    result = await api(
      `/api/models/${encodeURIComponent(current.model_id)}/preview?revision=${encodeURIComponent(revision)}`,
    );
    if (result.revision !== revision || result.stored !== true)
      throw new Error("Stored preview revision mismatch.");
  }
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
/** Part and assembly tree from cad_structure. */
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
      overlayCache.clear();
      overlayResolved.clear();
      chosenConfiguration = null;
      proposalBatches = [];
      clearReference();
    }
    // What the parts are made of needs no revision: it is asked for alongside
    // the model rather than after it.
    const colourRequest = api(`/api/models/${modelID}/colours`).then(
      (mask: any) => mask,
      () => null,
    );
    current = await tool("cad_get_model", { model_id: modelID, limit: 64 });
    displayRevision = current.revision;
    void refreshDrawings();
    headRevision = current.revision;
    selected = null;
    resetCandidate();
    text("model-title", current.name);
    text("breadcrumb-model", current.name);
    text("model-meta", `${current.feature_count} features · millimetres`);
    text(
      "quality",
      current.quality === "checks_passed_within_profile"
        ? "Geometry checked"
        : "Draft",
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
          ? "geometry checked"
          : "draft"
      }`,
    );
    el("features").replaceChildren();
    // The geometry is the long pole: ask for it first, put it on screen framed
    // the moment it arrives, and let the feature pages, the structure, the
    // colours and the configurations load alongside it.
    const opened = current.revision;
    const geometry: Promise<Error | boolean> = current.features.length
      ? preview(current.revision).then(
          () => {
            if (fitView && current?.revision === opened) fit();
            return true;
          },
          (error: Error) => error,
        )
      : Promise.resolve(false);
    const structure = structureTree();
    // Configurations and decals arrive when they arrive; nothing waits for them.
    void loadOverlays(modelID, current.revision).catch(() => {});
    // What the parts are made of, for the colouring that shows the real thing.
    const colours = colourRequest.then((mask: any) => {
      if (current?.model_id === modelID) colourMask = mask;
    });
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
      // Geometry may arrive before the complete feature/part pages. Replan once
      // all owners are available, including when restoring cached meshes.
      prepareExplosion();
      applyConfiguration();
      // The first selection happens on its own, after the picture: it is a
      // courtesy, not part of opening, and on a phone the model stays in view
      // instead of the detail sheet sliding up unasked.
      openingModel = true;
      void selectFeature(
        current.features.find((f: any) => f.kind === "groove")?.id ??
          current.features[0].id,
      )
        .catch(() => {})
        .finally(() => {
          openingModel = false;
        });
    } else {
      // An empty model shows nothing, and says nothing about the last one.
      clearGroup(mainGroup);
      meshData = null;
      text("triangle-count", "—");
      text("resolution", "Geometric preview");
      text("detail-name", "Select a feature");
    }
    // Everything else about the model is asked for side by side and shows up
    // as it arrives; none of it holds up the model on screen.
    void models().catch(() => {});
    void loadOrders().catch(() => {});
    void loadHistory().catch(() => {});
    void loadReferenceReview().catch(() => {});
    void loadProposals().catch(() => {});
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
      ? `Anchor: (${a.point_mm.map((v: number) => v.toFixed(3)).join(", ")}) mm · face ${a.face_id.slice(0, 13)} · frame ${a.local_frame}${a.barycentric ? ` · barycentric (${a.barycentric.map((v: number) => v.toFixed(2)).join(", ")})` : ""}${a.view_relative?.facing_camera != null ? (a.view_relative.facing_camera ? " · facing the camera" : " · facing away from the camera") : ""}`
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
  // Proposals committed from the feature batch list are marked as such there.
  if (candidate?.source?.kind === "proposal")
    noteProposalsCommitted(candidate.source);
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
        const picture = await prepareImage(file);
        files.push({
          blob: picture.blob,
          name: file.name,
          mime: picture.mime,
          url: URL.createObjectURL(picture.blob),
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
        ? `image-${++pictures}`
        : `file-${++meshes}`;
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
/** Pictures travel as they are whenever the service can take them: the
 *  original is what an agent crops a logo or a line of print from, and every
 *  re-encoding loses exactly that. The service records their pixel size and
 *  tells the agent when a picture is larger than it reads in one go. Only a
 *  picture it cannot take as it is — another format, or beyond 16384 px or
 *  24 MB — is re-drawn here, at most 2576 px on the long edge (what Opus 5.5
 *  reads at full detail), as JPEG at quality 0.95, or as PNG when it was one. */
const MODEL_IMAGE_EDGE_PX = 2576,
  MAX_ORIGINAL_EDGE_PX = 16384,
  MAX_ORIGINAL_BYTES = 24 * 1024 * 1024,
  JPEG_QUALITY = 0.95;
async function prepareImage(file: File) {
  const bitmap = await createImageBitmap(file);
  try {
    const longest = Math.max(bitmap.width, bitmap.height);
    if (
      (file.type === "image/jpeg" || file.type === "image/png") &&
      longest <= MAX_ORIGINAL_EDGE_PX &&
      file.size <= MAX_ORIGINAL_BYTES
    )
      return { blob: file as Blob, mime: file.type, original: true };
    const mime = file.type === "image/png" ? "image/png" : "image/jpeg";
    return { blob: await shrink(bitmap, mime), mime, original: false };
  } finally {
    bitmap.close();
  }
}
async function shrink(bitmap: ImageBitmap, mime = "image/jpeg") {
  const scale = Math.min(
      1,
      MODEL_IMAGE_EDGE_PX / Math.max(bitmap.width, bitmap.height),
    ),
    canvas = document.createElement("canvas");
  canvas.width = Math.max(1, Math.round(bitmap.width * scale));
  canvas.height = Math.max(1, Math.round(bitmap.height * scale));
  const ctx = canvas.getContext("2d")!;
  ctx.imageSmoothingQuality = "high";
  ctx.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
  return toBlob(canvas, mime);
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
      body: JSON.stringify({ stage: "auto" }),
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
function updateExportScope() {
  const format = el<HTMLSelectElement>("export-format").value,
    selected = el<HTMLSelectElement>("configuration").selectedOptions[0];
  text(
    "export-scope",
    format === "ir"
      ? "JSON contains the complete construction and all configurations."
      : `Export: ${selected?.value ? selected.textContent : "All outputs of the verified revision"}`,
  );
}
el<HTMLSelectElement>("export-format").onchange = () => {
  updateExportScope();
  el("downloads").replaceChildren();
};
updateExportScope();
el("export").onclick = action(async () => {
  if (!current) return;
  const format = el<HTMLSelectElement>("export-format").value,
    configuration = el<HTMLSelectElement>("configuration").value,
    scope = el("export-scope").textContent;
  busy("Exporting and re-checking the geometry …");
  const result = await job(
    await tool("cad_export", {
      model_id: current.model_id,
      revision: current.revision,
      format,
      ...(format !== "ir" && configuration ? { configuration } : {}),
      idempotency_key: key(),
    }),
  );
  el("downloads").replaceChildren();
  const exportedScope = document.createElement("p");
  exportedScope.className = "muted";
  exportedScope.textContent = scope;
  el("downloads").append(exportedScope);
  for (const a of result.artifacts) {
    const link = document.createElement("a");
    link.textContent =
      "↓ " +
      (a.manifest.filename ?? format.toUpperCase()) +
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
/* --- Drawing Export ----------------------------------------------------------
 * The AI agent draws the verified revision (stage "drawing"), looks at every
 * sheet and publishes the PDF; it stays bound to that revision and is shown as
 * outdated once the model has moved on. */
let drawingPoll: ReturnType<typeof setTimeout> | null = null;
function downloadLink(label: string, artifact: string, filename: string) {
  const link = document.createElement("a");
  link.textContent = label;
  link.href = `/api/artifacts/${artifact}`;
  link.download = filename;
  if (bearer)
    link.onclick = async (e) => {
      e.preventDefault();
      const r = await fetch(link.href, { headers: headers() });
      const url = URL.createObjectURL(await r.blob());
      const l = document.createElement("a");
      l.href = url;
      l.download = filename;
      l.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    };
  return link;
}
async function refreshDrawings() {
  if (drawingPoll) clearTimeout(drawingPoll);
  drawingPoll = null;
  const model = current?.model_id,
    revision = current?.revision ?? null;
  const links = el("drawing-export-links"),
    button = el<HTMLButtonElement>("drawing-export-run");
  if (!model) {
    links.replaceChildren();
    text("drawing-export-status", "");
    return;
  }
  let data: any;
  try {
    data = await api(`/api/models/${model}/drawings`);
  } catch {
    return;
  }
  if (current?.model_id !== model) return;
  const state = drawingState(data, revision);
  const area = el("drawing-export");
  area.dataset.state = state.kind;
  button.disabled = state.kind === "running";
  text(
    "drawing-export-status",
    state.kind === "running" && state.activity ? `${state.message} ${state.activity}` : state.message,
  );
  links.replaceChildren();
  if (state.drawing) {
    const d = state.drawing,
      name = String(current?.name ?? "drawing").replace(/[^\w.-]+/g, "_").slice(0, 60);
    links.append(
      downloadLink(
        `PDF ↓ (Rev ${d.revision.slice(0, 12)}, ${d.sheets} sheet${d.sheets === 1 ? "" : "s"})${state.kind === "stale" ? " · outdated" : ""}`,
        d.pdf_artifact_id,
        `${name}-${d.revision.slice(0, 12)}.pdf`,
      ),
    );
  }
  if (state.kind === "running") drawingPoll = setTimeout(() => void refreshDrawings(), 4000);
}
el("drawing-export-run").onclick = action(async () => {
  if (!current) return;
  if (current.quality !== "checks_passed_within_profile")
    throw new Error("Drawing Export needs a verified revision.");
  el<HTMLButtonElement>("drawing-export-run").disabled = true;
  text("drawing-export-status", "Starting the drawing agent …");
  await api(`/api/models/${current.model_id}/drawing`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ revision: current.revision }),
  });
  activity("Drawing Export started", "The agent lays out, dimensions, checks and publishes the PDF.");
  await refreshDrawings();
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
/* Two pens. Red says WHERE: the area that should change, filled as drawn.
 * Blue says WHAT: the new shape or the change itself — a line, a circle, an
 * outline. Every blue stroke is projected into the model and measured in
 * millimetres (on the surface it lies on, or on the view plane through it),
 * then classified, so the agent gets "circle Ø 8.2 mm centred at (x|y|z) on
 * face F" instead of having to read it off a picture. */
type Pen = "red" | "blue";
type Point2 = [number, number];
type Stroke = { pen: Pen; points: Point2[]; sketch?: SketchStroke | null };
type Box = { x: number; y: number; width: number; height: number };
const PEN_COLOUR: Record<Pen, string> = { red: "#e0261c", blue: "#1f5fe0" };
const MARK_COLOUR = PEN_COLOUR.red;
const PEN_HINT: Record<Pen, string> = {
  red: "The view is frozen. Red: draw around what should change.",
  blue: "Blue: sketch the change. Strokes are numbered B1, B2 … — refer to them in your note.",
};
let marking = false,
  pen: Pen = "red",
  strokes: Stroke[] = [],
  drawing: Stroke | null = null;
function markCanvas() {
  return el<HTMLCanvasElement>("markup-canvas");
}
function markWidth() {
  const canvas = markCanvas();
  return Math.max(3, Math.min(canvas.clientWidth, canvas.clientHeight) / 95);
}
/** A sketch line is thinner than an area mark: it is meant to be read exactly. */
function penWidth(stroke: Stroke) {
  return stroke.pen === "blue"
    ? Math.max(2, markWidth() * 0.6)
    : markWidth();
}
function polylineLength(points: Point2[]) {
  let length = 0;
  for (let i = 1; i < points.length; i++)
    length += Math.hypot(
      points[i][0] - points[i - 1][0],
      points[i][1] - points[i - 1][1],
    );
  return length;
}
/** Closed when the pen came back near where it started. */
function strokeClosed(points: Point2[]) {
  if (points.length < 8) return false;
  const length = polylineLength(points),
    gap = Math.hypot(
      points[0][0] - points[points.length - 1][0],
      points[0][1] - points[points.length - 1][1],
    );
  return gap < Math.max(12, length * 0.15);
}
/** The strokes in canvas coordinates; the caller sets the scale it needs.
 *  `fill` fills the red areas and the closed blue shapes (the covered area);
 *  `colour` overrides the pen colours (used for the identifier mask). */
function paintStrokes(
  ctx: CanvasRenderingContext2D,
  style: { colour?: string; width?: number; fill?: boolean; only?: Pen },
) {
  ctx.lineJoin = "round";
  ctx.lineCap = "round";
  for (const stroke of strokes) {
    const points = stroke.points;
    if (points.length < 2 || (style.only && stroke.pen !== style.only))
      continue;
    const colour = style.colour ?? PEN_COLOUR[stroke.pen];
    ctx.strokeStyle = colour;
    ctx.fillStyle = colour;
    ctx.lineWidth = style.width ?? penWidth(stroke);
    ctx.beginPath();
    ctx.moveTo(points[0][0], points[0][1]);
    for (const [x, y] of points.slice(1)) ctx.lineTo(x, y);
    // A circle drawn by hand rarely closes; for the covered area it counts as
    // closed, for the visible line it does not. An open blue line covers only
    // itself.
    if (style.fill && (stroke.pen === "red" || strokeClosed(points))) {
      ctx.closePath();
      ctx.fill();
    }
    ctx.stroke();
  }
}
/** A number next to every blue stroke (B1, B2, …), so the note can say which
 *  stroke it means — the same names the record uses. */
function paintSketchLabels(ctx: CanvasRenderingContext2D) {
  ctx.font = "600 12px system-ui, sans-serif";
  ctx.textBaseline = "middle";
  strokes.forEach((stroke, index) => {
    if (stroke.pen !== "blue" || stroke === drawing || stroke.points.length < 2)
      return;
    const label = blueNumber(index),
      [x, y] = stroke.points.reduce(
        (best, p) => (p[0] > best[0] ? p : best),
        stroke.points[0],
      ),
      width = ctx.measureText(label).width + 12;
    ctx.fillStyle = "#ffffffe6";
    ctx.beginPath();
    ctx.roundRect(x + 8, y - 10, width, 20, 10);
    ctx.fill();
    ctx.fillStyle = PEN_COLOUR.blue;
    ctx.fillText(label, x + 14, y);
  });
}
function blueNumber(index: number) {
  return `B${strokes.slice(0, index + 1).filter((s) => s.pen === "blue").length}`;
}
function redrawMark() {
  const canvas = markCanvas(),
    ratio = Math.min(devicePixelRatio, 2);
  canvas.width = Math.round(canvas.clientWidth * ratio);
  canvas.height = Math.round(canvas.clientHeight * ratio);
  const ctx = canvas.getContext("2d")!;
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx.clearRect(0, 0, canvas.clientWidth, canvas.clientHeight);
  paintStrokes(ctx, {});
  paintSketchLabels(ctx);
  el<HTMLButtonElement>("markup-undo").disabled = !strokes.length;
  el<HTMLButtonElement>("markup-save").disabled = !strokes.length;
}
function choosePen(next: Pen) {
  pen = next;
  el("markup").dataset.pen = next;
  for (const button of document.querySelectorAll<HTMLElement>(
    ".markup-pens .pen",
  ))
    button.setAttribute("aria-checked", String(button.dataset.pen === next));
  text("markup-hint", PEN_HINT[next]);
}
function beginMark() {
  if (marking) return;
  if (!meshData || !mainGroup.children.length) {
    toast("Open a model first.");
    return;
  }
  // A mark is drawn over the model alone; side by side would halve it.
  if (refView?.split) {
    refView.split = false;
    resize();
    renderReferenceControls();
    drawReferenceOverlay();
  }
  marking = true;
  strokes = [];
  document.body.dataset.marking = "true";
  controls.enabled = false;
  runLoop(false);
  el("markup").hidden = false;
  choosePen("red");
  redrawMark();
}
function endMark() {
  marking = false;
  strokes = [];
  drawing = null;
  delete document.body.dataset.marking;
  // In a snapped reference view the orbit stays off: zoom and pan move the frame.
  controls.enabled = !snapped();
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
    drawing = { pen, points: [at(event)] };
    strokes.push(drawing);
    redrawMark();
  });
  canvas.addEventListener("pointermove", (event) => {
    if (!drawing) return;
    const point = at(event),
      last = drawing.points[drawing.points.length - 1];
    if (Math.hypot(point[0] - last[0], point[1] - last[1]) < 1.5) return;
    drawing.points.push(point);
    redrawMark();
  });
  const finish = () => {
    if (drawing && drawing.points.length < 2) strokes.pop();
    else if (drawing?.pen === "blue") {
      // The view is frozen, so the placement taken now stays valid.
      try {
        drawing.sketch = measureSketchStroke(drawing.points);
      } catch {
        drawing.sketch = null;
      }
    }
    drawing = null;
    redrawMark();
  };
  canvas.addEventListener("pointerup", finish);
  canvas.addEventListener("pointercancel", finish);
}
for (const button of document.querySelectorAll<HTMLElement>(
  ".markup-pens .pen",
))
  button.onclick = () => choosePen(button.dataset.pen as Pen);
document.addEventListener("keydown", (event) => {
  if (!marking || event.metaKey || event.ctrlKey || event.altKey) return;
  if ((event.target as HTMLElement)?.closest("input, textarea, select")) return;
  if (el<HTMLDialogElement>("note-dialog").open) return;
  const key = event.key.toLowerCase();
  if (key === "r" || key === "b") {
    choosePen(key === "r" ? "red" : "blue");
    event.preventDefault();
  } else if (key === "z" || key === "backspace") {
    el("markup-undo").click();
    event.preventDefault();
  } else if (key === "escape") endMark();
});
el("markup-undo").onclick = () => {
  strokes.pop();
  text("markup-hint", PEN_HINT[pen]);
  redrawMark();
};
el("markup-cancel").onclick = endMark;
el("mark").onclick = beginMark;
el("fab-mark").onclick = beginMark;
el("markup-save").onclick = () => {
  const bounds = markBounds(),
    sketches = strokes.filter((stroke) => stroke.pen === "blue").length;
  text(
    "note-context",
    `The mark, the parts it covers and a picture of the area are saved with it (${Math.round(bounds.width)} × ${Math.round(bounds.height)} px at ${currentMmPerPixel.toPrecision(2)} mm/px).${
      sketches
        ? ` ${sketches} blue sketch stroke${sketches === 1 ? "" : "s"} (B1${sketches > 1 ? `–B${sketches}` : ""}) go along as 3D paths in mm — say what they mean.`
        : ""
    }`,
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
  const points = strokes.flatMap((stroke) => stroke.points);
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
    // Prints ride on their part as children; drawn with their own texture
    // they would overwrite the identifier colour of the part underneath.
    hidden = [
      grid,
      beforeGroup,
      regionGroup,
      markerGroup,
      ...decalMeshes,
    ].filter((o) => o.visible),
    background = scene.background,
    target = new THREE.WebGLRenderTarget(width, height),
    counts = new Map<number, { pixels: number; x: number; y: number }>(),
    // Inside a registered reference view the canvas already shows a window of
    // the camera's frame; the mark's box is a window inside that window.
    outer = activeCamera.view?.enabled ? { ...activeCamera.view } : null;
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
    if (outer) {
      const kx = outer.width / canvas.clientWidth,
        ky = outer.height / canvas.clientHeight;
      activeCamera.setViewOffset(
        outer.fullWidth,
        outer.fullHeight,
        outer.offsetX + box.x * kx,
        outer.offsetY + box.y * ky,
        box.width * kx,
        box.height * ky,
      );
    } else
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
    if (outer)
      activeCamera.setViewOffset(
        outer.fullWidth,
        outer.fullHeight,
        outer.offsetX,
        outer.offsetY,
        outer.width,
        outer.height,
      );
    else activeCamera.clearViewOffset();
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

/* --- The blue sketch, placed in the model ------------------------------------
 * A blue stroke is not interpreted here — the agent sees the picture and reads
 * the person's words. What the picture cannot give is where the stroke lies in
 * the model: so the stroke is resampled evenly, a few dozen rays find the
 * surface under it, and every sample also gets a point on the plane that faces
 * the camera at the stroke's own depth (the median surface hit, or the orbit
 * centre when it was drawn in free space). The result is the stroke as a 3D
 * path in millimetres, with the features and faces it was drawn on. */
type SketchStroke = { on_model: boolean; [key: string]: unknown };
const SKETCH_RAYS = 32,
  SKETCH_SAMPLES = 96,
  SKETCH_VERTICES = 24;
/** Evenly spaced points along a polyline, ends included. */
function resampleStroke(points: Point2[], count: number): Point2[] {
  const length = polylineLength(points);
  if (points.length < 2 || length === 0 || count < 2) return [points[0]];
  const step = length / (count - 1),
    out: Point2[] = [points[0]];
  let travelled = 0,
    target = step;
  for (let k = 1; k < points.length; k++) {
    const a = points[k - 1],
      b = points[k],
      segment = Math.hypot(b[0] - a[0], b[1] - a[1]);
    while (
      segment > 0 &&
      travelled + segment >= target &&
      out.length < count - 1
    ) {
      const t = (target - travelled) / segment;
      out.push([a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])]);
      target += step;
    }
    travelled += segment;
  }
  out.push(points[points.length - 1]);
  return out;
}
/** Camera axes in world coordinates: where the screen's right, up and depth
 *  point in the model. */
function viewBasis() {
  activeCamera.updateMatrixWorld();
  return {
    forward: activeCamera.getWorldDirection(new THREE.Vector3()),
    right: new THREE.Vector3()
      .setFromMatrixColumn(activeCamera.matrixWorld, 0)
      .normalize(),
    up: new THREE.Vector3()
      .setFromMatrixColumn(activeCamera.matrixWorld, 1)
      .normalize(),
  };
}
function rayThrough(x: number, y: number) {
  const canvas = renderer.domElement;
  raycaster.setFromCamera(
    new THREE.Vector2(
      (x / canvas.clientWidth) * 2 - 1,
      -(y / canvas.clientHeight) * 2 + 1,
    ),
    activeCamera,
  );
  return raycaster.ray.clone();
}
function worldNormal(hit: THREE.Intersection) {
  if (!hit.face) return null;
  return hit.face.normal
    .clone()
    .applyNormalMatrix(
      new THREE.Matrix3().getNormalMatrix(hit.object.matrixWorld),
    )
    .normalize();
}
/** Ramer–Douglas–Peucker: the indices of a short path that stays within
 *  `epsilon` of the drawn one — compression, not interpretation. */
function simplifyPath(
  points: Point2[],
  epsilon: number,
  first = 0,
  last = points.length - 1,
): number[] {
  if (last - first < 2) return first === last ? [first] : [first, last];
  const [ax, ay] = points[first],
    [bx, by] = points[last],
    length = Math.hypot(bx - ax, by - ay);
  let worst = -1,
    index = first;
  for (let i = first + 1; i < last; i++) {
    const [px, py] = points[i],
      distance =
        length < 1e-9
          ? Math.hypot(px - ax, py - ay)
          : Math.abs((bx - ax) * (ay - py) - (ax - px) * (by - ay)) / length;
    if (distance > worst) {
      worst = distance;
      index = i;
    }
  }
  if (worst <= epsilon) return [first, last];
  return [
    ...simplifyPath(points, epsilon, first, index).slice(0, -1),
    ...simplifyPath(points, epsilon, index, last),
  ];
}
/** One blue stroke, placed in the frozen view. */
function measureSketchStroke(points: Point2[]): SketchStroke | null {
  if (points.length < 2) return null;
  const pixels = polylineLength(points),
    samples = resampleStroke(
      points,
      Math.min(SKETCH_SAMPLES, Math.max(2, Math.ceil(pixels / 3) + 1)),
    ),
    rayCount = Math.min(SKETCH_RAYS, samples.length),
    rayIndices = [
      ...new Set(
        Array.from({ length: rayCount }, (_, i) =>
          Math.round((i * (samples.length - 1)) / Math.max(1, rayCount - 1)),
        ),
      ),
    ],
    hits = new Map<number, THREE.Intersection>();
  for (const index of rayIndices) {
    const hit = surfaceAt(...samples[index]);
    if (hit) hits.set(index, hit);
  }
  const { forward, right, up } = viewBasis();
  // The plane the stroke lies on where it leaves the surface: facing the
  // camera, at the depth of the surface it was drawn over.
  const byDepth = [...hits.values()].sort((a, b) => a.distance - b.distance),
    anchor = byDepth.length
      ? byDepth[Math.floor(byDepth.length / 2)].point.clone()
      : controls.target.clone(),
    plane = new THREE.Plane().setFromNormalAndCoplanarPoint(forward, anchor),
    onPlane = samples.map(
      ([x, y]) =>
        rayThrough(x, y).intersectPlane(plane, new THREE.Vector3()) ??
        anchor.clone(),
    ),
    uv: Point2[] = onPlane.map((p) => {
      const d = p.clone().sub(anchor);
      return [d.dot(right), d.dot(up)];
    }),
    length = polylineLength(uv),
    mmPerPx = pixels > 0 ? length / pixels : currentMmPerPixel;

  // What the stroke was drawn on.
  const featureIndex = new Map<string, any>(
      (current?.features ?? []).map((f: any) => [f.id, f]),
    ),
    surfaces = new Map<string, any>();
  for (const hit of hits.values()) {
    const featureId = hit.object.userData.feature_id,
      faceId = faceOf(hit),
      key = `${featureId}|${faceId}`,
      feature = featureIndex.get(featureId),
      entry = surfaces.get(key) ?? {
        feature_id: featureId ?? null,
        semantic_name: feature?.semantic_name ?? null,
        part_id: feature?.owner_part ?? null,
        part_name: partNames.get(feature?.owner_part) ?? null,
        face_id: faceId,
        samples: 0,
        normal: asPoint(worldNormal(hit) ?? forward.clone().negate()),
      };
    entry.samples++;
    surfaces.set(key, entry);
  }
  // Along the surface, when the stroke stayed on it the whole way.
  let surfaceLength: number | null = null;
  if (hits.size >= 2 && hits.size >= rayIndices.length * 0.9) {
    surfaceLength = 0;
    let previous: THREE.Vector3 | null = null;
    for (const index of rayIndices) {
      const point = hits.get(index)?.point;
      if (!point) continue;
      if (previous) surfaceLength += previous.distanceTo(point);
      previous = point;
    }
  }
  // Path corners between the ray samples get a ray of their own, so the path
  // follows the surface instead of jumping to the plane in between.
  const probed = new Map<number, THREE.Vector3 | null>(),
    worldOf = (index: number) => {
      const hit = hits.get(index)?.point;
      if (hit) return hit;
      if (!rayIndices.includes(index) && !probed.has(index))
        probed.set(index, surfaceAt(...samples[index])?.point ?? null);
      return probed.get(index) ?? onPlane[index];
    },
    path = simplifyPath(uv, 2 * mmPerPx);
  // Long wobbly strokes keep an even selection of their corners.
  const kept =
    path.length <= SKETCH_VERTICES
      ? path
      : Array.from(
          { length: SKETCH_VERTICES },
          (_, i) =>
            path[Math.round((i * (path.length - 1)) / (SKETCH_VERTICES - 1))],
        );
  return {
    on_model: hits.size > 0,
    closed: strokeClosed(points),
    length_mm: round(length, 3),
    ...(surfaceLength != null
      ? { length_along_surface_mm: round(surfaceLength, 3) }
      : {}),
    mm_per_px: round(mmPerPx, 5),
    start_world: asPoint(worldOf(0)),
    end_world: asPoint(worldOf(samples.length - 1)),
    // The drawn path, simplified to within two screen pixels: on the surface
    // where a ray found one, otherwise on the plane below.
    path_world: kept.map((index) => asPoint(worldOf(index))),
    plane: {
      origin: asPoint(anchor),
      normal: asPoint(forward.clone().negate()),
      depth_from:
        hits.size > 0 ? "median_surface_hit" : "orbit_centre_no_surface_hit",
    },
    surfaces: [...surfaces.values()]
      .sort((a, b) => b.samples - a.samples)
      .map((s) => ({ ...s, share: round(s.samples / rayIndices.length, 3) })),
    samples: rayIndices.map((index) => {
      const hit = hits.get(index);
      return {
        t: round(index / Math.max(1, samples.length - 1), 3),
        world: asPoint(worldOf(index)),
        on_surface: !!hit,
        ...(hit
          ? {
              feature_id: hit.object.userData.feature_id ?? null,
              face_id: faceOf(hit),
            }
          : {}),
      };
    }),
  };
}
/** Every blue stroke of the mark, numbered B1, B2, … as on the picture. */
function analyseSketch() {
  const blue = strokes.filter((stroke) => stroke.pen === "blue");
  if (!blue.length) return null;
  const { forward, right, up } = viewBasis();
  return {
    meaning:
      "Blue strokes are the person's sketch of the change itself; red strokes only mark the area concerned. The note says what the blue strokes mean. Coordinates are world millimetres: on the surface where a ray hit it, otherwise on the view plane through the stroke's depth.",
    view: {
      towards_camera: asPoint(forward.clone().negate()),
      screen_right: asPoint(right),
      screen_up: asPoint(up),
    },
    strokes: blue.map((stroke, n) => {
      if (stroke.sketch === undefined)
        stroke.sketch = measureSketchStroke(stroke.points);
      return {
        id: `B${n + 1}`,
        stroke_index: strokes.indexOf(stroke),
        ...(stroke.sketch ?? { on_model: false, unmeasured: true }),
      };
    }),
  };
}
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
        stroke.points.map(([x, y]) => [
          round(x / canvas.clientWidth, 5),
          round(y / canvas.clientHeight, 5),
        ]),
      ),
      // Per stroke, in the same order: red = the area, blue = the sketch.
      pens: strokes.map((stroke) => stroke.pen),
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
      type === "image/jpeg" ? JPEG_QUALITY : undefined,
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
      paintStrokes(ctx, {});
      // The labels name the strokes the way the record does (B1, B2, …).
      paintSketchLabels(ctx);
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
  // Capped where the screen itself stops adding detail (twice the device
  // pixels), and never beyond what the agent reads in one go.
  const regionCanvas = cut(crop, 1600),
    viewCanvas = cut(
      { x: 0, y: 0, width: source.clientWidth, height: source.clientHeight },
      MODEL_IMAGE_EDGE_PX,
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
      analysis = { ...analyseMark(box), sketch: analyseSketch() };
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
    void loadReferenceReview().catch(() => {});
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
/* --- Change requests and the agent that carries them out ------------------
 * Every order of the open model is listed here: marks with their change
 * requests, the plan (intake) and the builds. A running order shows what it is
 * doing — its phase, the time against its time box, thinking and output
 * tokens, turns, tool calls and the cost so far — and a finished build shows
 * its report: what it did, what it checked, what it assumed and what is open.
 * -------------------------------------------------------------------------*/
let orderTimer: number | null = null,
  progressTicker: number | null = null,
  shownOrders: any[] = [];
const STATE_LABELS: Record<string, string> = {
  captured: "new",
  queued: "queued",
  running: "running",
  proposed: "proposed",
  succeeded: "done",
  incomplete: "incomplete",
  failed: "failed",
  cancelled: "cancelled",
};
const KIND_LABELS: Record<string, string> = {
  mark: "Change",
  model_purpose: "Plan",
  model_build: "Build",
};
const STAGE_LABELS: Record<string, string> = {
  purpose: "Writing the plan",
  build: "Building",
  execution: "Carrying out the change",
  direct: "Carrying out the change",
  proposal: "Writing a proposal",
};
/** The service reports its one-line activity in English already. */
function englishActivity(line: string | null | undefined) {
  return line || "";
}
const compact = (n: number) =>
  n >= 1e6
    ? `${(n / 1e6).toFixed(1)}M`
    : n >= 1e4
      ? `${Math.round(n / 1e3)}k`
      : n >= 1e3
        ? `${(n / 1e3).toFixed(1)}k`
        : String(n);
function duration(seconds: number) {
  const s = Math.max(0, Math.round(seconds)),
    h = Math.floor(s / 3600),
    m = Math.floor((s % 3600) / 60),
    r = s % 60;
  return h
    ? `${h} h ${String(m).padStart(2, "0")} min`
    : `${m}:${String(r).padStart(2, "0")}`;
}
/** Seconds a run has been going (live while it runs), or took. */
function runSeconds(order: any) {
  const run = order.run ?? {},
    p = run.progress ?? {};
  if (order.state === "running" && run.started)
    return Math.max(
      p.elapsed_s ?? 0,
      (Date.now() - Date.parse(run.started)) / 1000,
    );
  if (typeof p.elapsed_s === "number") return p.elapsed_s;
  return typeof run.duration_ms === "number" ? run.duration_ms / 1000 : null;
}
/** The figures of a run, in the order a person asks for them. */
function progressFigures(order: any) {
  const run = order.run ?? {},
    p = run.progress ?? {},
    running = order.state === "running",
    figures: string[] = [];
  const seconds = runSeconds(order);
  if (seconds != null)
    figures.push(
      `${duration(seconds)}${run.budget_minutes ? ` of ${run.budget_minutes} min` : ""}`,
    );
  if (p.thinking_tokens) figures.push(`${compact(p.thinking_tokens)} thinking`);
  if (p.output_tokens) figures.push(`${compact(p.output_tokens)} output`);
  const turns = p.api_turns ?? run.turns;
  if (turns) figures.push(`${turns} turn${turns === 1 ? "" : "s"}`);
  if (p.tool_calls) figures.push(`${p.tool_calls} tool calls`);
  const cost = running
    ? p.cost_usd_estimate
    : (run.cost_usd ?? p.cost_usd_estimate);
  if (cost)
    figures.push(`$${Number(cost).toFixed(2)}${running ? " est." : ""}`);
  if (run.commits)
    figures.push(`${run.commits} commit${run.commits === 1 ? "" : "s"}`);
  return figures;
}
function orderKind(order: any) {
  return order.kind ?? "mark";
}
function orderPhase(order: any) {
  const run = order.run ?? {};
  if (orderKind(order) === "model_purpose") return STAGE_LABELS.purpose;
  if (orderKind(order) === "model_build") return STAGE_LABELS.build;
  return STAGE_LABELS[run.direct ? "direct" : run.stage] ?? "Working";
}
/** The live card of a running order: phase, what it is doing, its figures and
 *  a bar for the time box it plans against. */
function progressCard(order: any) {
  const run = order.run ?? {},
    card = document.createElement("div");
  card.className = "run-card";
  card.dataset.annotation = order.annotation_id;
  const head = document.createElement("div");
  head.className = "run-head";
  const phase = document.createElement("strong");
  phase.textContent = orderPhase(order);
  const who = document.createElement("span");
  who.textContent = [runModelLabel(run), run.effort, run.fast_mode ? "fast mode" : null]
    .filter(Boolean)
    .join(" · ");
  const stop = document.createElement("button");
  stop.type = "button";
  stop.textContent = "Stop";
  stop.title = "Stop the run. What it has committed stays.";
  stop.onclick = action(async () => {
    await api(`/api/annotations/${order.annotation_id}/cancel`, {
      method: "POST",
    });
    await loadOrders();
    await models();
  });
  head.append(phase, who, stop);
  const doing = document.createElement("p");
  doing.className = "run-activity";
  doing.textContent =
    run.activity ||
    (run.progress?.last_tool ? `last tool: ${run.progress.last_tool}` : "…");
  const figures = document.createElement("div");
  figures.className = "run-figures";
  figures.textContent = progressFigures(order).join(" · ");
  card.append(head, doing, figures);
  const seconds = runSeconds(order);
  if (run.budget_minutes && seconds != null) {
    const bar = document.createElement("div"),
      fill = document.createElement("i"),
      share = seconds / (run.budget_minutes * 60);
    bar.className = "run-bar";
    bar.title = `Time box ${run.budget_minutes} min · hard limit ${run.timeout_minutes ?? "?"} min`;
    fill.style.width = `${Math.min(100, share * 100).toFixed(1)}%`;
    if (share > 1) bar.dataset.over = "true";
    bar.append(fill);
    card.append(bar);
  }
  if (run.head_revision && run.head_revision !== run.base_head) {
    const note = document.createElement("small");
    note.textContent = `Committed so far: ${run.head_revision.slice(0, 16)}${run.head_changed_at ? ` · ${clock(run.head_changed_at)}` : ""}`;
    card.append(note);
  }
  return card;
}
function renderRunProgress() {
  const box = el("run-progress");
  if (!box) return;
  const running = shownOrders.filter((o) => o.state === "running");
  box.hidden = !running.length;
  box.replaceChildren(...running.map(progressCard));
  // The clock of a run moves every second even when the service has nothing
  // new to say (a long think sends few events).
  if (running.length && progressTicker === null)
    progressTicker = window.setInterval(() => {
      for (const card of el("run-progress").querySelectorAll<HTMLElement>(
        ".run-card",
      )) {
        const order = shownOrders.find(
          (o) => o.annotation_id === card.dataset.annotation,
        );
        const figures = card.querySelector(".run-figures");
        if (order && figures)
          figures.textContent = progressFigures(order).join(" · ");
      }
    }, 1000);
  if (!running.length && progressTicker !== null) {
    clearInterval(progressTicker);
    progressTicker = null;
  }
}
/** A list with a heading, for the assumptions and open points of a report. */
function reportList(title: string, items: string[], open = false) {
  const details = document.createElement("details");
  details.className = "report-list";
  details.open = open;
  const summary = document.createElement("summary");
  summary.textContent = `${title} (${items.length})`;
  const list = document.createElement("ul");
  for (const item of items) {
    const li = document.createElement("li");
    li.textContent = item;
    list.append(li);
  }
  details.append(summary, list);
  return details;
}
/** What a finished build or change run reports: summary, what it checked, its
 *  assumptions and open points, and the revisions it committed. */
function reportBlock(order: any) {
  const run = order.run ?? {},
    report = run.report,
    block = document.createElement("div");
  block.className = "order-report";
  const summary = report?.summary || run.summary;
  if (summary) {
    const p = document.createElement("p");
    p.className = "order-answer";
    p.textContent = summary;
    block.append(p);
  }
  if (report?.checked) {
    const p = document.createElement("p");
    p.className = "report-checked";
    p.textContent = `Checked: ${report.checked}`;
    block.append(p);
  }
  if (report?.assumptions?.length)
    block.append(reportList("Assumptions", report.assumptions));
  if (report?.open_points?.length)
    block.append(reportList("Open points", report.open_points, true));
  const commits: any[] = run.committed_revisions ?? [];
  if (commits.length) {
    const row = document.createElement("div");
    row.className = "report-revisions";
    row.append(
      `${commits.length} committed state${commits.length === 1 ? "" : "s"}: `,
    );
    for (const entry of commits.slice(-6)) {
      const revision =
        typeof entry === "string" ? entry : (entry?.revision ?? entry?.id);
      if (!revision) continue;
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "revision-chip";
      chip.textContent = String(revision).slice(0, 12);
      chip.title = "Show this state";
      chip.onclick = action(async () => {
        if (current?.model_id !== order.model_id)
          await openModel(order.model_id);
        await showRevision(revision);
      });
      row.append(chip);
    }
    block.append(row);
  }
  if (run.open_candidates?.length) {
    const p = document.createElement("p");
    p.className = "order-warning";
    p.textContent = `Open candidate left behind: ${run.open_candidates
      .map((t: any) => (typeof t === "string" ? t : t.id))
      .join(", ")}`;
    block.append(p);
  }
  return block;
}
function orderCard(order: any) {
  const kind = orderKind(order),
    run = order.run ?? {},
    card = document.createElement("div");
  card.className = "order";
  card.dataset.state = order.state;
  card.dataset.kind = kind;
  let picture: HTMLElement;
  if (order.region) {
    const image = document.createElement("img");
    image.alt = "";
    image.loading = "lazy";
    image.src = order.region;
    picture = image;
  } else {
    // A plan or a build has no marked area; its kind stands in for the picture.
    picture = document.createElement("div");
    picture.className = "order-symbol";
    picture.textContent =
      kind === "model_build" ? "▣" : kind === "model_purpose" ? "✎" : "◌";
  }
  const body = document.createElement("div"),
    note = document.createElement("div"),
    meta = document.createElement("div"),
    state = document.createElement("span"),
    actions = document.createElement("div");
  note.className = "order-note";
  note.textContent =
    order.note ||
    (kind === "model_build"
      ? "Build the model from its plan"
      : kind === "model_purpose"
        ? "Write the plan from the words and pictures"
        : "");
  meta.className = "order-meta";
  state.className = "order-state";
  state.textContent = STATE_LABELS[order.state] ?? order.state;
  const details = [
    KIND_LABELS[kind] ?? kind,
    new Date(order.created).toLocaleString("en-GB"),
    run.model ? `${runModelLabel(run)} · ${run.effort}` : null,
    run.stage === "proposal" && order.state !== "proposed" ? "proposal" : null,
    run.direct ? "direct" : null,
    order.state === "running"
      ? null
      : progressFigures(order).join(" · ") || null,
    order.images ? `${order.images} attached` : null,
  ].filter(Boolean);
  meta.append(state, document.createTextNode(details.join(" · ")));
  body.append(note, meta);
  if (order.state === "running") {
    const doing = document.createElement("p");
    doing.className = "run-activity";
    doing.textContent = `${orderPhase(order)} · ${run.activity || "…"}`;
    body.append(doing);
  }
  if (order.state === "incomplete" && run.incomplete_reason) {
    const why = document.createElement("p");
    why.className = "order-warning";
    why.textContent = `Stopped early: ${run.incomplete_reason}`;
    body.append(why);
  }
  if (run.error && order.state === "failed") {
    const error = document.createElement("p");
    error.className = "order-answer";
    error.textContent = run.error;
    body.append(error);
  }
  // What the agent answered is the point of the whole exercise, so it is shown
  // in full: a run that changed nothing has its reasons, and they are here.
  if (order.state === "proposed" && run.proposal) {
    const block = document.createElement("p");
    block.className = "order-answer";
    block.textContent = run.proposal;
    body.append(block);
  } else if (order.state !== "running" && (run.report || run.summary))
    body.append(reportBlock(order));
  actions.className = "order-actions";
  const button = (label: string, go: () => Promise<void>, primary = false) => {
    const element = document.createElement("button");
    element.type = "button";
    element.textContent = label;
    if (primary) element.className = "primary";
    element.onclick = action(go);
    return element;
  };
  // "auto" lets the service choose the stage: a plan for a new model, a build
  // from a plan, the accepted proposal, or — by the change mode in the AI
  // settings — a change carried out directly or proposed first.
  const start =
    (stage: "auto" | "proposal", ambiguous?: boolean) => async () => {
      await api(`/api/annotations/${order.annotation_id}/dispatch`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          stage,
          ...(ambiguous === undefined ? {} : { ambiguous }),
        }),
      });
      await loadOrders();
      await models();
    };
  const remove = button("Delete", async () => {
    await api(`/api/annotations/${order.annotation_id}`, { method: "DELETE" });
    await loadOrders();
  });
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
      button("Accept · carry out", start("auto"), true),
      button("Propose again", start("proposal", true)),
      remove,
    );
  else if (kind === "mark")
    actions.append(
      button(
        order.state === "captured" ? "Hand to the AI" : "Try again",
        start("auto"),
        order.state === "captured",
      ),
      button("Proposal first", start("proposal", true)),
      remove,
    );
  else {
    if (kind === "model_purpose" && order.state === "succeeded")
      actions.append(
        button("Show plan", async () => showBuildPlan(order.model_id)),
      );
    if (["failed", "incomplete", "cancelled"].includes(order.state))
      actions.append(
        button(
          kind === "model_build" ? "Carry on" : "Try again",
          start("auto"),
        ),
      );
    actions.append(remove);
  }
  body.append(actions);
  card.append(picture, body);
  return card;
}
async function loadOrders() {
  const list = el("orders");
  if (!list) return;
  const model = current?.model_id ?? null;
  const data = await api(
    "/api/annotations?kind=all" + (model ? `&model=${model}` : ""),
  );
  if ((current?.model_id ?? null) !== model) return;
  shownOrders = data.annotations;
  list.replaceChildren(...shownOrders.map(orderCard));
  if (!shownOrders.length)
    list.textContent = "No orders yet. Mark an area to ask for a change.";
  text("orders-count", shownOrders.length ? String(shownOrders.length) : "");
  renderRunProgress();
  // While something is running the list follows along by itself.
  // A captured order waits for a person; only queued and running ones move.
  const busyOrders = shownOrders.some((a: any) => ACTIVE.includes(a.state));
  if (busyOrders && orderTimer === null)
    orderTimer = window.setInterval(
      () => void loadOrders().catch(() => {}),
      2500,
    );
  if (!busyOrders && orderTimer !== null) {
    clearInterval(orderTimer);
    orderTimer = null;
  }
  syncHeadWatch();
}
/* --- Following a running agent -------------------------------------------
 * While a build or a change runs on the open model, the head is watched with a
 * long poll; every intermediate commit is shown as it lands, so the first
 * shape is on screen minutes into a run, not at its end. A person in the middle
 * of something — a candidate, a mark, an earlier state — is not interrupted:
 * the new state is announced and waits in the history. */
let headWatch: AbortController | null = null;
function agentWritesCurrent() {
  if (!current) return false;
  const row = modelRows.find((m: any) => m.id === current.model_id),
    order = row?.purpose_order;
  if (order && order.kind === "model_build" && ACTIVE.includes(order.state))
    return true;
  return shownOrders.some(
    (o: any) =>
      o.model_id === current.model_id &&
      o.state === "running" &&
      (orderKind(o) === "model_build" || o.run?.stage === "execution"),
  );
}
function syncHeadWatch() {
  const wanted = agentWritesCurrent();
  if (wanted && !headWatch) {
    const control = new AbortController();
    headWatch = control;
    void followHead(current.model_id, control);
  } else if (!wanted && headWatch) {
    headWatch.abort();
    headWatch = null;
  }
}
async function followHead(model: string, control: AbortController) {
  while (!control.signal.aborted && current?.model_id === model) {
    try {
      const since = headRevision ?? current.revision;
      const answer = await api(
        `/api/models/${model}/head?since=${encodeURIComponent(since)}&wait_ms=20000`,
        { signal: control.signal },
      );
      if (control.signal.aborted || current?.model_id !== model) break;
      if (
        answer.changed &&
        answer.head_revision &&
        answer.head_revision !== headRevision
      )
        await headMoved(model, answer.head_revision);
    } catch {
      if (control.signal.aborted) break;
      await new Promise((resolve) => setTimeout(resolve, 3000));
    }
  }
  if (headWatch === control) headWatch = null;
}
async function headMoved(model: string, revision: string) {
  const idle =
    !candidate &&
    !marking &&
    busyCount === 0 &&
    (!shownRevision || shownRevision === headRevision);
  if (idle) {
    await openModel(model, false);
    activity(
      "New state from the agent",
      `${revision.slice(0, 16)} · shown as soon as it was committed`,
    );
    return;
  }
  // Somebody is working: the new state waits in the history and the bar.
  headRevision = revision;
  void loadHistory().catch(() => {});
  syncStates();
  toast("The agent has committed a new state. It is in the history.");
}
/** The model a run used: an agy run names the exact variant it ran. */
function runModelLabel(run: any) {
  return run.agy_model ? `${run.agy_model} (agy)` : run.model;
}
/* --- AI settings -----------------------------------------------------------
 * Every field the service keeps is on the form, so a save sends the whole set
 * and nothing falls back to a default behind the person's back. */
let agentSettings: Record<string, any> = {};
const SETTING_FIELDS: [string, string, "text" | "int" | "money" | "flag"][] = [
  ["agent-model", "model", "text"],
  ["agent-intake-model", "intake_model", "text"],
  ["agent-intake-effort", "intake_effort", "text"],
  ["agent-build-model", "build_model", "text"],
  ["agent-effort", "effort", "text"],
  ["agent-execution-model", "execution_model", "text"],
  ["agent-execution-effort", "execution_effort", "text"],
  ["agent-proposal-model", "proposal_model", "text"],
  ["agent-proposal-effort", "proposal_effort", "text"],
  ["agent-drawing-backend", "drawing_backend", "text"],
  ["agent-drawing-model", "drawing_model", "text"],
  ["agent-drawing-effort", "drawing_effort", "text"],
  ["agent-change-mode", "change_mode", "text"],
  ["agent-permissions", "permission_mode", "text"],
  ["agent-time-box", "budget_minutes", "int"],
  ["agent-timeout", "timeout_minutes", "int"],
  ["agent-budget", "max_budget_usd", "money"],
  ["agent-fast", "fast_mode", "flag"],
  ["agent-skill", "skill", "flag"],
];
function showAgentSettings(settings: Record<string, any>) {
  agentSettings = settings;
  for (const [id, key, kind] of SETTING_FIELDS) {
    const input = document.getElementById(id) as
      HTMLInputElement | HTMLSelectElement | null;
    if (!input) continue;
    const value = settings[key];
    if (kind === "flag") (input as HTMLInputElement).checked = !!value;
    else {
      // A value this page does not list is still shown as what it is.
      if (
        input instanceof HTMLSelectElement &&
        value != null &&
        ![...input.options].some((o) => o.value === String(value))
      ) {
        const option = document.createElement("option");
        option.value = String(value);
        option.textContent = String(value);
        input.append(option);
      }
      input.value = value == null ? "" : String(value);
    }
  }
  const migrated = settings.migrated_from as
    Record<string, unknown> | undefined;
  text(
    "agent-settings-note",
    migrated && Object.keys(migrated).length
      ? `Raised once to the Opus 5.5 defaults; before: ${Object.entries(
          migrated,
        )
          .map(([k, v]) => `${k} ${v}`)
          .join(", ")}.`
      : "",
  );
}
/** Per-stage model selects: "same as AI model", then every model the main
 *  select offers (Claude Code and, when set up, Antigravity). */
const STAGE_MODEL_SELECTS = [
  "agent-intake-model",
  "agent-build-model",
  "agent-execution-model",
  "agent-proposal-model",
  "agent-drawing-model",
];
/** Adds this machine's agy (Antigravity) model families to the model selects.
 *  The stage effort picks the variant (clamped to what a family offers). */
async function loadAgentModels() {
  const main = document.getElementById("agent-model") as HTMLSelectElement | null;
  if (!main) return;
  let agy: any = null;
  try {
    agy = (await api("/api/agent/models"))?.agy ?? null;
  } catch {
    agy = null;
  }
  main.querySelector("optgroup[data-agy]")?.remove();
  if (agy?.models?.length) {
    const group = document.createElement("optgroup");
    group.dataset.agy = "1";
    group.label = agy.available
      ? "Antigravity (agy)"
      : "Antigravity (agy) – not reachable";
    for (const m of agy.models) {
      const option = document.createElement("option");
      option.value = m.id;
      // "agy" first: the sidebar select cuts long labels at the end.
      option.textContent = `agy · ${m.label}`;
      option.title = m.efforts?.length
        ? `efforts: ${m.efforts.join(", ")} (a stage effort it lacks is clamped)`
        : "no effort levels";
      option.disabled = !agy.available;
      group.append(option);
    }
    main.append(group);
  }
  for (const id of STAGE_MODEL_SELECTS) {
    const select = document.getElementById(id) as HTMLSelectElement | null;
    if (!select) continue;
    select.replaceChildren();
    const same = document.createElement("option");
    same.value = "";
    same.textContent = "same as AI model";
    select.append(same);
    for (const child of [...main.children]) select.append(child.cloneNode(true));
  }
}
async function loadAgentSettings() {
  await loadAgentModels();
  showAgentSettings(await api("/api/agent/settings"));
}
const saveAgentSettings = action(async () => {
  const body: Record<string, unknown> = {};
  for (const [id, key, kind] of SETTING_FIELDS) {
    const input = document.getElementById(id) as HTMLInputElement | null;
    if (!input) continue;
    if (kind === "flag") body[key] = input.checked;
    else if (kind === "money") {
      const value = input.value.trim();
      body[key] = value === "" ? null : Number(value);
    } else if (kind === "int") {
      const value = Number(input.value);
      if (!Number.isInteger(value)) {
        showAgentSettings(agentSettings);
        throw new Error("Minutes are whole numbers.");
      }
      body[key] = value;
    } else body[key] = input.value;
  }
  if (Number(body.budget_minutes) > Number(body.timeout_minutes)) {
    showAgentSettings(agentSettings);
    throw new Error("The time box cannot be longer than the hard time limit.");
  }
  try {
    const saved = await api("/api/agent/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    showAgentSettings(saved?.model ? saved : { ...agentSettings, ...body });
  } catch (error) {
    // What the service refused is not left standing on the form.
    showAgentSettings(agentSettings);
    throw error;
  }
});
for (const [id] of SETTING_FIELDS) {
  const input = document.getElementById(id);
  if (input) input.onchange = saveAgentSettings;
}
/** The structured plan of a model (build-plan.md), read in a dialog. */
async function showBuildPlan(modelID: string) {
  let plan: any;
  try {
    plan = await api(`/api/models/${modelID}/build-plan`);
  } catch {
    throw new Error("This model has no structured plan yet.");
  }
  el("build-plan-text").textContent =
    plan.markdown || JSON.stringify(plan.json, null, 2);
  el<HTMLDialogElement>("build-plan-dialog").showModal();
}

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

/* --- Reference captures ----------------------------------------------------
 * Immutable reference-review capture uses this same renderer and material path.
 * Called in an isolated browser, never on the person's working tab.
 *
 * view: either the old bounding-box-relative camera {direction, up, projection,
 * target?, distance_factor, fov, width, height} or an absolute camera
 * {mode:"absolute", projection, position_mm, target_mm, up, fov_deg | mm_per_px,
 * width_px, height_px, principal_px?} (width/height, when given, set the pixel
 * size of the picture; the frame is mapped onto it exactly). Absolute cameras
 * are never scaled to the model's bounds.
 * view.channel: "lit" (default; the model's own materials under Khronos PBR
 * Neutral tone mapping), "albedo" (flat declared colours, unlit, no tone
 * mapping: what a colour gate compares) or "ids" (one flat colour per output on
 * black; result.ids maps the colours back to features).
 * -------------------------------------------------------------------------*/
const CAPTURE_CHANNELS = ["lit", "albedo", "ids"] as const;
function idColour(index: number) {
  const value = index + 1;
  return [
    ((value & 63) * 4 + 2) / 255,
    (((value >> 6) & 63) * 4 + 2) / 255,
    (((value >> 12) & 63) * 4 + 2) / 255,
  ];
}
/** Flat colours for the albedo channel: the declared colour of each part, as
 *  it is written down, without light and without tone mapping. */
function albedoMaterial(mesh: THREE.Mesh) {
  const entry = materialOf(mesh.userData.feature_id),
    source = mesh.material as THREE.Material,
    glass = entry?.finish === "clear glass",
    opacity = glass
      ? Math.max(0.15, 1 - (entry?.transmission ?? 0.95))
      : (entry?.opacity ?? 1);
  return new THREE.MeshBasicMaterial({
    color: new THREE.Color(entry?.colour ?? "#9aa3a8"),
    transparent: opacity < 1,
    opacity,
    toneMapped: false,
    side: source.side,
    polygonOffset: source.polygonOffset,
    polygonOffsetFactor: source.polygonOffsetFactor,
    polygonOffsetUnits: source.polygonOffsetUnits,
  });
}
(window as any).mathforgeViewer.referenceReady = () =>
  !!current && busyCount === 0 && mainGroup.children.length > 0;
(window as any).mathforgeViewer.captureReference = async (view: any) => {
  if (!current || busyCount)
    throw new Error("Viewer not ready for reference capture");
  const channel = (view.channel ?? "lit") as (typeof CAPTURE_CHANNELS)[number];
  if (!CAPTURE_CHANNELS.includes(channel))
    throw new Error(`Unknown capture channel: ${String(view.channel)}`);
  clearReference();
  selected = null;
  section = false;
  wireframe = false;
  overlayMode = "original";
  setExplode(0, false);
  clearGroup(markerGroup);
  clearGroup(regionGroup);
  beforeGroup.visible = false;
  for (const child of mainGroup.children) child.visible = true;
  for (const child of scene.children)
    if (child.type === "GridHelper") child.visible = false;
  referenceToneMapping = true;
  recolour();
  // The loop would refit the depth planes and move the camera through the
  // orbit controls between set-up and picture; a capture renders once, itself.
  runLoop(false);
  const absolute = isAbsoluteCamera(view),
    width = Math.round(view.width ?? view.width_px),
    height = Math.round(view.height ?? view.height_px);
  renderer.setPixelRatio(1);
  renderer.setSize(width, height, false);
  const bounds = new THREE.Box3().setFromObject(mainGroup);
  let described: any;
  camera.clearViewOffset();
  orthoCamera.clearViewOffset();
  if (absolute) {
    const resolved = resolveCamera(view, bounds, { width, height });
    activeCamera = applyResolvedCamera(resolved, camera, orthoCamera, bounds);
    controls.object = activeCamera;
    controls.target.copy(resolved.target);
    described = describeCamera(resolved);
  } else {
    // The bounding-box-relative camera, unchanged: the same numbers still
    // frame the same picture as before.
    const size = bounds.getSize(new THREE.Vector3()),
      span = Math.max(size.x, size.y, size.z);
    const target = view.target
      ? new THREE.Vector3(...view.target)
      : bounds.getCenter(new THREE.Vector3());
    const direction = new THREE.Vector3(...view.direction).normalize();
    const up = new THREE.Vector3(...view.up).normalize();
    if (Math.abs(direction.dot(up)) > 0.999)
      throw new Error("Camera direction and up are parallel");
    activeCamera = view.projection === "orthographic" ? orthoCamera : camera;
    controls.object = activeCamera;
    controls.target.copy(target);
    activeCamera.up.copy(up);
    activeCamera.position
      .copy(target)
      .addScaledVector(direction, span * view.distance_factor);
    camera.aspect = view.width / view.height;
    camera.fov = view.fov;
    const half =
      span *
      view.distance_factor *
      Math.tan(THREE.MathUtils.degToRad(view.fov) / 2);
    orthoCamera.left = (-half * view.width) / view.height;
    orthoCamera.right = -orthoCamera.left;
    orthoCamera.top = half;
    orthoCamera.bottom = -half;
    orthoCamera.zoom = 1;
    activeCamera.near = Math.max(
      0.001,
      span * view.distance_factor - size.length(),
    );
    activeCamera.far = span * (view.distance_factor + 4);
    activeCamera.updateProjectionMatrix();
    controls.update();
    described = {
      mode: "relative",
      projection:
        view.projection === "orthographic" ? "orthographic" : "perspective",
      position_mm: activeCamera.position.toArray(),
      target_mm: target.toArray(),
      up: up.toArray(),
      fov_deg: view.fov,
      width_px: width,
      height_px: height,
    };
  }
  // Channel materials replace the lit ones for this one picture.
  const meshes = mainGroup.children.filter(
      (child): child is THREE.Mesh => child instanceof THREE.Mesh,
    ),
    originals = meshes.map((mesh) => mesh.material as THREE.Material),
    decalOriginals = decalMeshes.map((mesh) => mesh.material as THREE.Material),
    background = scene.background;
  let ids: { index: number; feature_id: string; rgb: number[] }[] | undefined;
  await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
  try {
    if (channel === "albedo") {
      meshes.forEach((mesh) => (mesh.material = albedoMaterial(mesh)));
      decalMeshes.forEach((mesh) => {
        const lit = mesh.material as THREE.MeshStandardMaterial;
        mesh.material = new THREE.MeshBasicMaterial({
          map: lit.map,
          transparent: true,
          depthWrite: false,
          toneMapped: false,
          polygonOffset: true,
          polygonOffsetFactor: lit.polygonOffsetFactor,
          polygonOffsetUnits: lit.polygonOffsetUnits,
        });
      });
    } else if (channel === "ids") {
      ids = [];
      meshes.forEach((mesh, index) => {
        const [r, g, b] = idColour(index),
          source = originals[index];
        // Given in the output colour space, so the picture carries exactly
        // these bytes (the mark coverage pass reads a linear render target).
        mesh.material = new THREE.MeshBasicMaterial({
          color: new THREE.Color().setRGB(r, g, b, THREE.SRGBColorSpace),
          toneMapped: false,
          side: source.side,
          polygonOffset: source.polygonOffset,
          polygonOffsetFactor: source.polygonOffsetFactor,
          polygonOffsetUnits: source.polygonOffsetUnits,
        });
        ids!.push({
          index,
          feature_id: mesh.userData.feature_id,
          rgb: [r, g, b].map((v) => Math.round(v * 255)),
        });
      });
      for (const decal of decalMeshes) decal.visible = false;
      scene.background = new THREE.Color(0x000000);
    }
    renderer.render(scene, activeCamera);
    const png = renderer.domElement.toDataURL("image/png").split(",")[1];
    return {
      revision: displayRevision,
      visible_features: mainGroup.children
        .filter((m) => m.visible)
        .map((m) => m.userData.feature_id),
      png,
      channel,
      tone_mapping: channel === "lit" ? "khronos_pbr_neutral" : "none",
      camera: described,
      ...(ids ? { ids } : {}),
    };
  } finally {
    if (channel !== "lit") {
      meshes.forEach((mesh, index) => {
        (mesh.material as THREE.Material).dispose();
        mesh.material = originals[index];
      });
      decalMeshes.forEach((mesh, index) => {
        if (mesh.material !== decalOriginals[index])
          (mesh.material as THREE.Material).dispose();
        mesh.material = decalOriginals[index];
        mesh.visible = true;
      });
      scene.background = background;
    }
    referenceToneMapping = false;
    runLoop(true);
  }
};

/* --- Reference review -----------------------------------------------------
 * The reference contract's photographs can be laid over the model (onion skin,
 * with an opacity slider), shown beside it with a shared zoom and pan, and — for
 * a reference with a camera — the model can be put exactly into that camera
 * ("snap"). A snapped view keeps its registration: zooming and panning move a
 * window over the reference frame, for the photo and the model alike, instead
 * of moving the camera. "Compare to photo" asks the deterministic gates
 * (cad_gates) for their ranked residuals and, in a snapped view, computes a
 * silhouette difference right here: red where only the model is, blue where
 * only the photo is.
 * -------------------------------------------------------------------------*/
let referenceReport: any = null;
let referenceTimer: ReturnType<typeof setTimeout> | undefined;
let referenceRun: { model: string; id: string } | null = null;
let referenceList: any[] = [],
  referenceListKey: string | null = null,
  referenceChoice: string | null = null,
  lastGates: any = null;
const referenceLabels: Record<string, string> = {
  not_configured: "Reference images not set",
  not_checked: "Reference comparison not run",
  running: "Reference comparison running",
  matches_declared_visible_checks: "Declared visible checks matched",
  needs_correction: "Visible differences found",
  insufficient_evidence: "Some details remain unverified",
  stale: "Recheck needed after a change",
  failed: "Review failed — no approval",
};
async function loadReferenceReview(offset = 0) {
  if (!current) return;
  if (referenceTimer) clearTimeout(referenceTimer);
  const model = current.model_id,
    revision = displayRevision ?? current.revision;
  const run =
    referenceRun && referenceRun.model === model ? referenceRun.id : null;
  const report = await api(
    `/api/models/${model}/reference-review?revision=${encodeURIComponent(revision)}&offset=${offset}${run ? "&run_id=" + encodeURIComponent(run) : ""}`,
  );
  if (
    current?.model_id !== model ||
    (displayRevision ?? current.revision) !== revision
  )
    return;
  referenceReport = report;
  const running = ["queued", "running"].includes(report.state);
  const inspected = report.phase === "inspect" && report.state === "completed";
  text(
    "reference-status",
    running
      ? `Review running · ${report.stage}`
      : inspected
        ? "Quick view ready · draft inspection, no final approval"
        : (referenceLabels[report.reference.status] ?? report.reference.status),
  );
  const cost = report.usage.reduce(
    (sum: number, u: any) => sum + u.cost_usd,
    0,
  );
  text(
    "reference-coverage",
    `${report.reference.matched}/${report.reference.required} required details matched · ${report.reference.mismatched} mismatches · ${report.reference.unknown} unverified${cost ? " · $" + cost.toFixed(4) : ""}`,
  );
  const check = el<HTMLButtonElement>("reference-check");
  check.disabled =
    !report.vision_configured || !report.contract_hash || running;
  el<HTMLButtonElement>("reference-inspect").disabled =
    !report.reference.contract_hash || running;
  check.textContent =
    report.requirements.length || report.reference.required
      ? "Final reference review"
      : "Extract reference details (AI)";
  if (offset === 0) el("reference-findings").replaceChildren();
  const line = (message: string) => {
    const p = document.createElement("p");
    p.textContent = message;
    el("reference-findings").append(p);
    return p;
  };
  if (report.error) line(report.error.code + ": " + report.error.message);
  if (report.checked_summary && report.reference.status === "stale")
    line(
      `Previous review of ${report.checked_revision}: ${report.checked_summary.matched}/${report.checked_summary.required} matched; ${report.checked_summary.mismatched} mismatches; ${report.checked_summary.unknown} unverified.`,
    );
  for (const f of report.findings) {
    const p = line(
      `${f.verdict === "match" ? "✓" : f.verdict === "mismatch" ? "✕" : "?"} ${f.description}`,
    );
    p.className = "reference-finding " + f.verdict;
    for (const o of f.observations) {
      const small = document.createElement("small");
      small.textContent = `${o.model}: ${o.evidence}${o.correction ? " · " + o.correction : ""}`;
      p.append(small);
    }
  }
  for (const f of report.additional_issues)
    line(`✕ Additional ${f.category}: ${f.description}`);
  for (const c of report.native_checks)
    line(
      `${c.status === "passed" ? "✓" : c.status === "failed" ? "✕" : "?"} ${c.id}: ${c.measured ?? "unknown"} ${c.unit} (required ${c.expected})`,
    );
  for (const img of report.images) {
    const link = document.createElement("a");
    link.textContent = `↗ ${img.kind}: ${img.label}`;
    link.href = "/api/artifacts/" + img.artifact_id;
    link.target = "_blank";
    link.rel = "noopener";
    link.className = "reference-image";
    if (bearer)
      link.onclick = async (e) => {
        e.preventDefault();
        const r = await fetch(link.href, { headers: headers() });
        if (!r.ok) return toast("Image unavailable");
        const url = URL.createObjectURL(await r.blob());
        window.open(url, "_blank", "noopener");
        setTimeout(() => URL.revokeObjectURL(url), 60000);
      };
    el("reference-findings").append(link);
  }
  for (const limitation of report.limitations) line("Scope: " + limitation);
  el("reference-more").hidden = report.next_offset === null;
  el("reference-more").onclick = action(() =>
    loadReferenceReview(report.next_offset),
  );
  if (offset === 0) void syncReferences(report).catch(() => {});
  if (running && location.hostname !== "llcad-review.localhost")
    referenceTimer = setTimeout(
      () => void loadReferenceReview().catch(() => {}),
      4000,
    );
}
/** Every reference of the contract (the report pages them three at a time). */
async function syncReferences(report: any) {
  const model = current?.model_id,
    key = `${model}|${report.contract_hash ?? report.reference?.contract_hash ?? ""}`;
  if (!model) return;
  if (key !== referenceListKey) {
    const references = [...(report.references ?? [])];
    let next = report.next_offset,
      offset = references.length;
    while (
      next !== null &&
      references.length === offset &&
      offset > 0 &&
      offset < 12
    ) {
      const page = await api(
        `/api/models/${model}/reference-review?offset=${offset}${report.run_id ? "&run_id=" + encodeURIComponent(report.run_id) : ""}`,
      );
      if (current?.model_id !== model) return;
      if (!page.references?.length) break;
      // A service that ignores the offset answers the first page again.
      if (references.some((r) => r.id === page.references[0].id)) break;
      references.push(...page.references);
      offset += 3;
      next = page.next_offset;
    }
    referenceList = references;
    referenceListKey = key;
    if (!referenceList.some((r) => r.id === referenceChoice))
      referenceChoice =
        referenceList.find((r) => r.registered && r.camera)?.id ??
        referenceList.find((r) => r.camera)?.id ??
        referenceList[0]?.id ??
        null;
  }
  renderReferenceControls();
}
function chosenReference() {
  return referenceList.find((r) => r.id === referenceChoice) ?? null;
}
function cameraLabel(ref: any) {
  if (!ref?.camera) return "no camera";
  const kind = isAbsoluteCamera(ref.camera)
    ? "absolute camera"
    : "relative camera";
  return ref.registered ? `registered · ${kind}` : kind;
}
function renderReferenceControls() {
  const box = el("reference-views");
  box.hidden = !current || !referenceList.length;
  const select = el<HTMLSelectElement>("reference-select");
  select.replaceChildren(
    ...referenceList.map((ref) => {
      const option = document.createElement("option");
      option.value = ref.id;
      option.textContent = `${ref.label} · ${cameraLabel(ref)}`;
      return option;
    }),
  );
  if (referenceChoice) select.value = referenceChoice;
  const ref = chosenReference();
  el<HTMLButtonElement>("reference-snap").disabled = !ref?.camera;
  el<HTMLButtonElement>("reference-overlay-toggle").disabled = !ref;
  el<HTMLButtonElement>("reference-split").disabled = !ref;
  el<HTMLButtonElement>("reference-compare").disabled = !current;
  const pressed = (id: string, on: boolean) => {
    el(id).classList.toggle("active", on);
    el(id).setAttribute("aria-pressed", String(on));
  };
  pressed("reference-snap", snapped());
  pressed("reference-overlay-toggle", !!refView?.overlay);
  pressed("reference-split", !!refView?.split);
  el("reference-snap").textContent = snapped() ? "Leave view" : "Snap to view";
  text(
    "reference-camera",
    ref
      ? ref.camera
        ? `${cameraLabel(ref)}${ref.registered ? "" : " · not registered: framing is approximate"}`
        : "No camera: the photo can be laid over the view, which you turn by hand to meet it."
      : "",
  );
}
const photoCache = new Map<string, Promise<ImageBitmap>>();
function photo(artifactID: string) {
  if (!photoCache.has(artifactID))
    photoCache.set(
      artifactID,
      (async () => {
        const response = await fetch(`/api/artifacts/${artifactID}`, {
          headers: headers(),
        });
        if (!response.ok)
          throw new Error("The reference image is unavailable.");
        return createImageBitmap(await response.blob());
      })(),
    );
  const pending = photoCache.get(artifactID)!;
  pending.catch(() => photoCache.delete(artifactID));
  return pending;
}
function snapped() {
  return !!refView?.resolved;
}
function referenceSplit() {
  return !!refView?.split && !!refView.image;
}
/** The model's extent where it is assembled, whatever an explosion shows. */
function modelBounds() {
  const box = new THREE.Box3();
  for (const mesh of mainGroup.children as THREE.Mesh[]) {
    if (!mesh.visible || !mesh.geometry) continue;
    if (!mesh.geometry.boundingBox) mesh.geometry.computeBoundingBox();
    const origin = (mesh.userData.origin as number[] | undefined) ?? [0, 0, 0];
    box.union(
      mesh.geometry
        .boundingBox!.clone()
        .translate(new THREE.Vector3(...origin)),
    );
  }
  if (box.isEmpty()) box.setFromObject(mainGroup);
  return box;
}
async function ensureRefView() {
  const ref = chosenReference();
  if (!ref) throw new Error("Choose a reference view first.");
  if (refView && refView.ref.id === ref.id) return refView;
  const keep = refView;
  if (keep?.resolved) leaveReferenceView();
  refView = {
    ref,
    image: await photo(ref.artifact_id).catch(() => null),
    resolved: null,
    window: null,
    overlay: keep?.overlay ?? false,
    opacity: keep?.opacity ?? 0.5,
    source: "photo",
    split: keep?.split ?? false,
    heat: null,
    heatStats: null,
    gates: null,
  };
  if (!refView.image)
    toast("The reference image could not be loaded; the camera still works.");
  return refView;
}
async function snapToReference() {
  const view = await ensureRefView(),
    ref = view.ref;
  if (!ref.camera) throw new Error("This reference has no camera yet.");
  if (explodeAmount > 0) setExplode(0, false);
  const bounds = modelBounds();
  view.resolved = resolveCamera(
    ref.camera,
    bounds,
    view.image
      ? { width: view.image.width, height: view.image.height }
      : { width: 1000, height: 1000 },
  );
  view.window = null;
  activeCamera =
    view.resolved.projection === "orthographic" ? orthoCamera : camera;
  controls.object = activeCamera;
  controls.target.copy(view.resolved.target);
  el("ortho").classList.toggle("active", activeCamera === orthoCamera);
  text(
    "view-label",
    `REFERENCE · ${String(ref.label).toUpperCase().slice(0, 40)}`,
  );
  resize();
  renewOrbitControls();
  controls.enabled = false;
  renderReferenceControls();
  drawReferenceOverlay();
  activity(
    "Reference view",
    `${ref.label} · ${cameraLabel(ref)} · wheel and drag zoom and pan the frame; Fit leaves the view.`,
  );
}
/** Apply the snapped camera for the current pane; keeps the zoom and centre of
 *  the window unless a fresh fit is asked for. */
function applyReferenceWindow(refit: boolean) {
  const view = refView;
  if (!view?.resolved) return;
  const canvas = renderer.domElement,
    paneW = Math.max(1, canvas.clientWidth || viewport.clientWidth),
    paneH = Math.max(1, canvas.clientHeight || viewport.clientHeight);
  if (refit || !view.window)
    view.window = containWindow(view.resolved, paneW, paneH);
  else {
    // Same scale and centre, new pane shape.
    const scale = Math.min(paneW / view.window.w, paneH / view.window.h) || 1,
      cx = view.window.x + view.window.w / 2,
      cy = view.window.y + view.window.h / 2,
      w = paneW / scale,
      h = paneH / scale;
    view.window = { x: cx - w / 2, y: cy - h / 2, w, h };
  }
  applyResolvedCamera(
    view.resolved,
    camera,
    orthoCamera,
    modelBounds(),
    view.window,
  );
  drawReferenceOverlay();
}
function leaveReferenceView() {
  const view = refView;
  if (!view?.resolved) return;
  const half = view.resolved.half_height_mm;
  view.resolved = null;
  view.window = null;
  camera.clearViewOffset();
  orthoCamera.clearViewOffset();
  camera.up.set(0, 0, 1);
  orthoCamera.up.set(0, 0, 1);
  camera.zoom = 1;
  orthoCamera.top = 35;
  orthoCamera.bottom = -35;
  if (half > 0) orthoCamera.zoom = 35 / half;
  controls.enabled = !marking;
  resize();
  renewOrbitControls();
  text(
    "view-label",
    activeCamera === orthoCamera ? "ORTHOGRAPHIC · Z ↑" : "PERSPECTIVE · Z ↑",
  );
  renderReferenceControls();
  drawReferenceOverlay();
}
/** Everything the reference view put on screen goes away. */
function clearReference() {
  if (refView?.resolved) leaveReferenceView();
  refView = null;
  referenceList = [];
  referenceListKey = null;
  lastGates = null;
  el("reference-residuals")?.replaceChildren();
  drawReferenceOverlay();
  resize();
}
/** Paint the photo (or a heatmap) where the model is: over it as onion skin,
 *  and in the left pane when side by side. */
function drawReferenceOverlay() {
  const overlay = document.getElementById(
      "reference-overlay",
    ) as HTMLCanvasElement | null,
    pane = document.getElementById(
      "reference-pane",
    ) as HTMLCanvasElement | null,
    bar = document.getElementById("reference-bar");
  if (!overlay || !pane || !bar) return;
  const view = refView,
    split = referenceSplit();
  viewport.dataset.split = String(split);
  const overlayImage =
    view?.source === "heatmap"
      ? view.heat
      : view?.source === "gates"
        ? view.gates
        : view?.image;
  overlay.hidden = !view?.overlay || !overlayImage;
  pane.hidden = !split;
  bar.hidden = !view || (!view.overlay && !split && !view.resolved);
  if (!view) return;
  text("reference-bar-label", view.ref.label);
  el<HTMLInputElement>("reference-opacity").value = String(
    Math.round(view.opacity * 100),
  );
  const source = el<HTMLSelectElement>("reference-source");
  (
    source.querySelector('option[value="heatmap"]') as HTMLOptionElement
  ).disabled = !view.heat;
  (
    source.querySelector('option[value="gates"]') as HTMLOptionElement
  ).disabled = !view.gates;
  source.value = view.source;
  const canvas = renderer.domElement,
    cw = Math.max(1, canvas.clientWidth),
    ch = Math.max(1, canvas.clientHeight),
    ratio = Math.min(devicePixelRatio, 2);
  const paint = (
    target: HTMLCanvasElement,
    image: CanvasImageSource | null | undefined,
    alpha: number,
  ) => {
    if (target.hidden) return;
    target.width = Math.round(cw * ratio);
    target.height = Math.round(ch * ratio);
    const ctx = target.getContext("2d")!;
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    ctx.clearRect(0, 0, cw, ch);
    if (!image) return;
    ctx.globalAlpha = alpha;
    const iw = (image as ImageBitmap).width,
      ih = (image as ImageBitmap).height;
    if (view.resolved && view.window) {
      // The frame (the photo's pixel grid) seen through the window.
      const sx = cw / view.window.w,
        sy = ch / view.window.h;
      ctx.drawImage(
        image,
        -view.window.x * sx,
        -view.window.y * sy,
        view.resolved.width_px * sx,
        view.resolved.height_px * sy,
      );
    } else {
      // Unregistered: the photo is fitted into the view and the model is
      // turned by hand to meet it.
      const scale = Math.min(cw / iw, ch / ih);
      ctx.drawImage(
        image,
        (cw - iw * scale) / 2,
        (ch - ih * scale) / 2,
        iw * scale,
        ih * scale,
      );
    }
  };
  paint(overlay, overlayImage, view.opacity);
  paint(pane, view.image, 1);
}
/* Zoom and pan inside a snapped view: a window over the frame, shared by the
 * model and the photo, so both stay registered and side by side stays synced. */
function referenceWheel(event: WheelEvent) {
  const view = refView;
  if (!view?.resolved || !view.window) return;
  event.preventDefault();
  const rect = (event.currentTarget as HTMLElement).getBoundingClientRect(),
    px = event.clientX - rect.left,
    py = event.clientY - rect.top,
    w = view.window,
    fx = w.x + (px * w.w) / rect.width,
    fy = w.y + (py * w.h) / rect.height,
    limit = Math.max(view.resolved.width_px, view.resolved.height_px),
    nw = THREE.MathUtils.clamp(
      w.w * Math.exp(event.deltaY * 0.0015),
      limit * 0.01,
      limit * 20,
    ),
    nh = (nw * w.h) / w.w;
  view.window = {
    x: fx - (px * nw) / rect.width,
    y: fy - (py * nh) / rect.height,
    w: nw,
    h: nh,
  };
  applyReferenceWindow(false);
}
let panFrom: { x: number; y: number; id: number } | null = null;
function referencePanStart(event: PointerEvent) {
  if (!snapped() || event.button !== 0) return;
  panFrom = { x: event.clientX, y: event.clientY, id: event.pointerId };
}
function referencePanMove(event: PointerEvent) {
  const view = refView;
  if (!panFrom || panFrom.id !== event.pointerId || !view?.window) return;
  const rect = (event.currentTarget as HTMLElement).getBoundingClientRect();
  view.window.x -= ((event.clientX - panFrom.x) * view.window.w) / rect.width;
  view.window.y -= ((event.clientY - panFrom.y) * view.window.h) / rect.height;
  panFrom = { ...panFrom, x: event.clientX, y: event.clientY };
  applyReferenceWindow(false);
}
function referencePanEnd() {
  panFrom = null;
}
for (const target of [renderer.domElement, el("reference-pane")]) {
  target.addEventListener("wheel", referenceWheel, { passive: false });
  target.addEventListener("pointerdown", referencePanStart);
  target.addEventListener("pointermove", referencePanMove);
  target.addEventListener("pointerup", referencePanEnd);
  target.addEventListener("pointercancel", referencePanEnd);
}
/** The model's silhouette in the snapped camera against the photo's foreground,
 *  both at up to 768 px: red where only the model is, blue where only the photo
 *  is, nothing where they agree. The photo's background is taken from its
 *  border, so this assumes a plain backdrop, as product photographs have. */
async function computeHeatmap() {
  const view = refView;
  if (!view?.resolved || !view.image)
    throw new Error("Snap to a reference view with a photo first.");
  const r = view.resolved,
    scale = Math.min(1, 768 / Math.max(r.width_px, r.height_px)),
    w = Math.max(8, Math.round(r.width_px * scale)),
    h = Math.max(8, Math.round(r.height_px * scale));
  const target = new THREE.WebGLRenderTarget(w, h),
    white = new THREE.MeshBasicMaterial({
      color: 0xffffff,
      side: THREE.DoubleSide,
      toneMapped: false,
    }),
    hidden = [
      grid,
      beforeGroup,
      regionGroup,
      markerGroup,
      ...decalMeshes,
    ].filter((o) => o.visible),
    background = scene.background,
    override = scene.overrideMaterial,
    modelPixels = new Uint8Array(w * h * 4);
  try {
    for (const object of hidden) object.visible = false;
    scene.background = new THREE.Color(0x000000);
    scene.overrideMaterial = white;
    const cam = applyResolvedCamera(r, camera, orthoCamera, modelBounds());
    renderer.setRenderTarget(target);
    renderer.render(scene, cam);
    renderer.readRenderTargetPixels(target, 0, 0, w, h, modelPixels);
  } finally {
    renderer.setRenderTarget(null);
    for (const object of hidden) object.visible = true;
    scene.background = background;
    scene.overrideMaterial = override;
    white.dispose();
    target.dispose();
    applyReferenceWindow(false);
  }
  const photoCanvas = document.createElement("canvas");
  photoCanvas.width = w;
  photoCanvas.height = h;
  const pctx = photoCanvas.getContext("2d", { willReadFrequently: true })!;
  pctx.drawImage(view.image, 0, 0, w, h);
  const photoPixels = pctx.getImageData(0, 0, w, h).data;
  // Background colour: the median of the border pixels, channel by channel.
  const border: number[][] = [[], [], []];
  for (let x = 0; x < w; x++)
    for (const y of [0, h - 1])
      for (let c = 0; c < 3; c++)
        border[c].push(photoPixels[(y * w + x) * 4 + c]);
  for (let y = 0; y < h; y++)
    for (const x of [0, w - 1])
      for (let c = 0; c < 3; c++)
        border[c].push(photoPixels[(y * w + x) * 4 + c]);
  const backdrop = border.map((values) => {
    values.sort((a, b) => a - b);
    return values[values.length >> 1];
  });
  const heat = document.createElement("canvas");
  heat.width = w;
  heat.height = h;
  const hctx = heat.getContext("2d")!,
    out = hctx.createImageData(w, h);
  let both = 0,
    modelOnly = 0,
    photoOnly = 0;
  for (let y = 0; y < h; y++)
    for (let x = 0; x < w; x++) {
      const i = (y * w + x) * 4,
        // The render target starts at the bottom row.
        m = modelPixels[((h - 1 - y) * w + x) * 4] > 127,
        dr = photoPixels[i] - backdrop[0],
        dg = photoPixels[i + 1] - backdrop[1],
        db = photoPixels[i + 2] - backdrop[2],
        p = Math.sqrt(dr * dr + dg * dg + db * db) > 38;
      if (m && p) both++;
      else if (m) {
        modelOnly++;
        out.data.set([214, 58, 36, 220], i);
      } else if (p) {
        photoOnly++;
        out.data.set([36, 104, 214, 220], i);
      }
    }
  hctx.putImageData(out, 0, 0);
  const union = both + modelOnly + photoOnly;
  view.heat = heat;
  view.heatStats = {
    iou: union ? both / union : 0,
    both,
    model_only: modelOnly,
    photo_only: photoOnly,
    pixels: w * h,
  };
  return view.heatStats;
}
/** Artifacts in a gates result that look like a difference image for a view. */
function gatesHeatmaps(result: any, refID: string | null) {
  const found: { artifact_id: string; score: number }[] = [];
  const visit = (node: any, depth: number) => {
    if (!node || typeof node !== "object" || depth > 6) return;
    if (Array.isArray(node)) return node.forEach((n) => visit(n, depth + 1));
    if (typeof node.artifact_id === "string") {
      const label = `${node.kind ?? ""} ${node.label ?? ""} ${node.filename ?? ""} ${node.channel ?? ""}`;
      if (/heat|diff|xor|overlay/i.test(label)) {
        const about = `${node.reference_id ?? ""} ${node.view ?? ""} ${node.view_id ?? ""} ${node.id ?? ""}`;
        found.push({
          artifact_id: node.artifact_id,
          score: refID && about.includes(refID) ? 2 : 1,
        });
      }
    }
    for (const value of Object.values(node)) visit(value, depth + 1);
  };
  visit(result, 0);
  return found.sort((a, b) => b.score - a.score).map((f) => f.artifact_id);
}
const SEVERITY_RANK: Record<string, number> = {
  blocker: 0,
  critical: 0,
  fail: 0,
  error: 0,
  high: 1,
  major: 1,
  warn: 2,
  warning: 2,
  medium: 2,
  minor: 3,
  low: 3,
  info: 4,
};
function renderResiduals(gates: any, gatesError: string | null) {
  const box = el("reference-residuals");
  box.replaceChildren();
  const line = (message: string, className = "") => {
    const p = document.createElement("p");
    p.textContent = message;
    if (className) p.className = className;
    box.append(p);
    return p;
  };
  const stats = refView?.heatStats;
  if (stats)
    line(
      `Silhouette in “${refView!.ref.label}” (local): IoU ${stats.iou.toFixed(3)} · model only ${((100 * stats.model_only) / stats.pixels).toFixed(1)} % · photo only ${((100 * stats.photo_only) / stats.pixels).toFixed(1)} % of the frame`,
      "residual-local",
    );
  if (gatesError)
    line(
      /unknown tool|not found/i.test(gatesError)
        ? "cad_gates is not available on this server yet; only the local silhouette comparison ran."
        : `Gates: ${gatesError}`,
      "residual-error",
    );
  if (!gates) return;
  line(
    gates.floor_passed
      ? "Gate floor passed. A floor, not a finish: the residuals below still say what differs."
      : "Gate floor not passed.",
    gates.floor_passed ? "residual-floor passed" : "residual-floor failed",
  );
  const refID = refView?.ref?.id ?? referenceChoice ?? "";
  const residuals: any[] = Array.isArray(gates.residuals)
    ? gates.residuals
    : [];
  for (const [index, r] of residuals.slice(0, 40).entries()) {
    const where =
      typeof r.where === "string"
        ? r.where
        : r.where
          ? JSON.stringify(r.where).slice(0, 160)
          : "";
    const p = line(
      `${index + 1}. ${r.check}: ${r.measured ?? "?"}${r.unit ? " " + r.unit : ""} (target ${r.target ?? "?"}${r.unit ? " " + r.unit : ""})${where ? " · " + where : ""}`,
      "residual",
    );
    p.dataset.severity = String(r.severity ?? "");
    if (refID && where.includes(refID)) p.classList.add("this-view");
    const chip = document.createElement("span");
    chip.className = "severity";
    chip.textContent = String(r.severity ?? "—");
    p.prepend(chip);
  }
  if (residuals.length > 40) line(`… ${residuals.length - 40} more residuals.`);
  if (!residuals.length) line("No residuals reported.");
}
async function compareToPhoto() {
  if (!current) return;
  busy("Comparing with the reference …");
  let gates: any = null,
    gatesError: string | null = null;
  try {
    if (refView?.resolved && refView.image) {
      await computeHeatmap();
      refView.source = "heatmap";
      refView.overlay = true;
    }
    const base = {
      model_id: current.model_id,
      revision: displayRevision ?? current.revision,
      ...(referenceReport?.reference?.contract_hash
        ? { contract_hash: referenceReport.reference.contract_hash }
        : {}),
    };
    try {
      gates = await job(await tool("cad_gates", base));
    } catch (error) {
      const message = (error as Error).message;
      // A build that wants an idempotency key for gate jobs gets one.
      if (/idempotency_key/.test(message))
        gates = await job(
          await tool("cad_gates", { ...base, idempotency_key: key() }),
        );
      else throw error;
    }
  } catch (error) {
    gatesError = (error as Error).message;
  } finally {
    busy("", false);
  }
  lastGates = gates;
  if (gates && refView) {
    const [artifact] = gatesHeatmaps(gates, refView.ref.id);
    if (artifact) refView.gates = await photo(artifact).catch(() => null);
  }
  renderResiduals(gates, gatesError);
  renderReferenceControls();
  drawReferenceOverlay();
  if (!refView?.resolved && !gates)
    toast(
      gatesError && !/unknown tool/i.test(gatesError)
        ? gatesError
        : "Snap to a reference view with a camera for the local silhouette comparison.",
    );
}
el<HTMLSelectElement>("reference-select").onchange = () => {
  referenceChoice = el<HTMLSelectElement>("reference-select").value;
  if (refView && refView.ref.id !== referenceChoice) {
    const keep = { overlay: refView.overlay, split: refView.split };
    if (refView.resolved) leaveReferenceView();
    refView = null;
    if (keep.overlay || keep.split)
      void action(async () => {
        const view = await ensureRefView();
        view.overlay = keep.overlay;
        view.split = keep.split;
        resize();
        renderReferenceControls();
        drawReferenceOverlay();
      })();
  }
  renderReferenceControls();
  drawReferenceOverlay();
};
el("reference-snap").onclick = action(async () => {
  if (snapped()) leaveReferenceView();
  else await snapToReference();
});
el("reference-overlay-toggle").onclick = action(async () => {
  const view = await ensureRefView();
  view.overlay = !view.overlay;
  renderReferenceControls();
  drawReferenceOverlay();
});
el("reference-split").onclick = action(async () => {
  const view = await ensureRefView();
  view.split = !view.split;
  resize();
  renderReferenceControls();
  drawReferenceOverlay();
});
el("reference-compare").onclick = action(compareToPhoto);
el<HTMLInputElement>("reference-opacity").oninput = () => {
  if (!refView) return;
  refView.opacity =
    Number(el<HTMLInputElement>("reference-opacity").value) / 100;
  drawReferenceOverlay();
};
el<HTMLSelectElement>("reference-source").onchange = () => {
  if (!refView) return;
  refView.source = el<HTMLSelectElement>("reference-source")
    .value as RefView["source"];
  refView.overlay = true;
  renderReferenceControls();
  drawReferenceOverlay();
};
el("reference-leave").onclick = () => {
  if (!refView) return;
  if (refView.resolved) leaveReferenceView();
  refView.overlay = false;
  refView.split = false;
  resize();
  renderReferenceControls();
  drawReferenceOverlay();
};
el("reference-check").onclick = action(async () => {
  if (!current || !referenceReport?.contract_hash) return;
  const result = await tool("cad_reference_check", {
    model_id: current.model_id,
    revision: displayRevision ?? current.revision,
    contract_hash: referenceReport.reference.contract_hash,
    phase:
      referenceReport.reference.required || referenceReport.requirements.length
        ? "compare"
        : "inventory",
    idempotency_key: key(),
  });
  referenceRun = { model: current.model_id, id: result.run_id };
  await loadReferenceReview();
});
el("reference-inspect").onclick = action(async () => {
  if (!current || !referenceReport?.reference.contract_hash) return;
  const start = (views: any[]) =>
    tool("cad_reference_check", {
      model_id: current.model_id,
      revision: displayRevision ?? current.revision,
      contract_hash: referenceReport.reference.contract_hash,
      phase: "inspect",
      views,
      idempotency_key: key(),
    });
  const bounds = new THREE.Box3().setFromObject(mainGroup),
    size = bounds.getSize(new THREE.Vector3()),
    span = Math.max(size.x, size.y, size.z);
  const offset = activeCamera.position.clone().sub(controls.target);
  const factor =
    activeCamera === orthoCamera
      ? orthoCamera.top / (span * Math.tan(THREE.MathUtils.degToRad(28) / 2))
      : offset.length() / span;
  const relative = [
    {
      id: "detail",
      label: "Current detail view",
      camera: {
        direction: offset.normalize().toArray(),
        up: activeCamera.up.toArray(),
        target: controls.target.toArray(),
        projection:
          activeCamera === orthoCamera ? "orthographic" : "perspective",
        distance_factor: Math.max(0.15, Math.min(10, factor)),
        fov: activeCamera === orthoCamera ? 28 : camera.fov,
      },
      width: 1000,
      height: 1000,
    },
  ];
  let result: any;
  // In a snapped view with an absolute camera, the capture takes that very
  // camera; a server that does not know absolute cameras yet gets the
  // relative description of the same view.
  const ref = refView?.resolved ? refView.ref : null;
  if (ref && isAbsoluteCamera(ref.camera)) {
    const long = Math.max(ref.camera.width_px, ref.camera.height_px),
      scale = Math.min(1, 2000 / long);
    try {
      result = await start([
        {
          id: `ref-${ref.id}`.slice(0, 80),
          label: `Reference view ${ref.label}`.slice(0, 200),
          camera: ref.camera,
          width: Math.max(128, Math.round(ref.camera.width_px * scale)),
          height: Math.max(128, Math.round(ref.camera.height_px * scale)),
        },
      ]);
    } catch (error) {
      if (!/INVALID_SCHEMA/.test((error as Error).message)) throw error;
    }
  }
  result ??= await start(relative);
  referenceRun = { model: current.model_id, id: result.run_id };
  await loadReferenceReview();
});
el<HTMLInputElement>("reference-upload").onchange = action(async () => {
  const files = Array.from(
    el<HTMLInputElement>("reference-upload").files ?? [],
  );
  if (!current || !files.length) return;
  if (files.length > 12) throw new Error("Select at most 12 reference images.");
  const model = current.model_id;
  const references = [];
  for (const [index, file] of files.entries()) {
    if (file.size > 16 * 1024 * 1024)
      throw new Error("Reference images must be smaller than 16 MiB.");
    // The original goes up as it is: registration and colour checks measure
    // its pixels.
    const a = await api("/api/uploads", {
      method: "POST",
      headers: { "Content-Type": "application/octet-stream" },
      body: file,
    });
    references.push({
      id: "photo-" + index,
      artifact_id: a.artifact_id,
      label: file.name,
      source: "User upload: " + file.name,
    });
  }
  await api(`/api/models/${model}/reference-images`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ references, idempotency_key: key() }),
  });
  referenceRun = null;
  clearReference();
  await loadReferenceReview();
});

/* --- Configurations and decals -------------------------------------------
 * Both come from the revision's IR (GET /api/models/:id/overlays). A
 * configuration is a named set of outputs — a variant, a state, an accessory
 * fitted — and choosing one shows only its outputs; the default configuration
 * is chosen when a model opens. Decals are prints that exist only for the eye
 * and ride on the part they are projected onto.
 * -------------------------------------------------------------------------*/
async function fetchOverlays(
  model: string,
  revision: string,
): Promise<OverlayData> {
  // A model tool that carries them itself saves the request.
  if (
    current?.revision === revision &&
    (Array.isArray(current.configurations) || Array.isArray(current.decals))
  )
    return {
      revision,
      configurations: current.configurations ?? [],
      decals: current.decals ?? [],
    };
  try {
    const data = await api(
      `/api/models/${model}/overlays?revision=${encodeURIComponent(revision)}`,
    );
    return {
      revision,
      configurations: data.configurations ?? [],
      decals: data.decals ?? [],
    };
  } catch {
    // An older service, or a candidate the route does not know: nothing to add.
    return { revision, configurations: [], decals: [], unavailable: true };
  }
}
async function loadOverlays(model: string, revision: string) {
  if (!overlayCache.has(revision))
    overlayCache.set(revision, fetchOverlays(model, revision));
  const data = await overlayCache.get(revision)!;
  if (current?.model_id !== model) return;
  const fresh = !overlayResolved.has(revision);
  overlayResolved.set(revision, data);
  if (chosenConfiguration === null && revision === current.revision)
    chosenConfiguration =
      data.configurations.find((c: any) => c.default)?.id ?? null;
  if (fresh && (shownRevision ?? displayRevision) === revision) {
    applyConfiguration();
    applyDecals();
  }
}
/** The overlays of the state on screen; a candidate the route cannot answer
 *  for shows those of the committed head. */
function overlaysShown(): OverlayData | null {
  const revision = shownRevision ?? displayRevision;
  if (!current || !revision) return null;
  const own = overlayResolved.get(revision);
  if (own && !own.unavailable) return own;
  if (!own && !overlayCache.has(revision))
    void loadOverlays(current.model_id, revision).catch(() => {});
  return overlayResolved.get(current.revision) ?? own ?? null;
}
/** A configuration's outputs as feature ids: parts and assemblies it names
 *  stand for their outputs. */
function configurationFeatures(configuration: any) {
  const features = new Set<string>();
  const partOutputs = (part: any) => {
    for (const output of part.definition?.outputs ?? []) features.add(output);
    for (const feature of current?.features ?? [])
      if (
        feature.owner_part === part.entity_id &&
        (current?.outputs ?? []).includes(feature.id)
      )
        features.add(feature.id);
  };
  const assemblyParts = (assembly: string, depth = 0): void => {
    if (depth > 16) return;
    for (const part of structureEntries.parts)
      if (part.definition?.assembly === assembly) partOutputs(part);
    for (const child of structureEntries.assemblies)
      if (child.definition?.parent_assembly === assembly)
        assemblyParts(child.entity_id, depth + 1);
  };
  for (const id of configuration.outputs ?? []) {
    const part = structureEntries.parts.find((p: any) => p.entity_id === id);
    if (part) {
      partOutputs(part);
      continue;
    }
    if (structureEntries.assemblies.some((a: any) => a.entity_id === id)) {
      assemblyParts(id);
      continue;
    }
    features.add(id);
  }
  return features;
}
function applyConfiguration() {
  const configurations = overlaysShown()?.configurations ?? [],
    select = document.getElementById(
      "configuration",
    ) as HTMLSelectElement | null;
  if (select) {
    select.hidden = !configurations.length;
    const signature = JSON.stringify(
      configurations.map((c: any) => [c.id, c.name]),
    );
    if (select.dataset.signature !== signature) {
      select.dataset.signature = signature;
      const all = document.createElement("option");
      all.value = "";
      all.textContent = "All outputs";
      select.replaceChildren(
        all,
        ...configurations.map((c: any) => {
          const option = document.createElement("option");
          option.value = c.id;
          option.textContent = `${c.name}${c.default ? " (default)" : ""}`;
          return option;
        }),
      );
    }
  }
  const chosen = configurations.find((c: any) => c.id === chosenConfiguration);
  if (select) select.value = chosen ? chosen.id : "";
  updateExportScope();
  const allowed = chosen ? configurationFeatures(chosen) : null;
  const drawn = new Set<string>();
  for (const mesh of mainGroup.children) {
    drawn.add(mesh.userData.feature_id);
    mesh.visible = !allowed || allowed.has(mesh.userData.feature_id);
  }
  if (select) {
    const missing = allowed
      ? [...allowed].filter((id) => !drawn.has(id)).length
      : 0;
    select.title = chosen
      ? `${chosen.name}: ${[...allowed!].filter((id) => drawn.has(id)).length} outputs shown${missing ? ` · ${missing} not in this preview` : ""}${chosen.description ? ` · ${chosen.description}` : ""}`
      : "All outputs of the preview";
  }
}
el<HTMLSelectElement>("configuration").onchange = () => {
  chosenConfiguration = el<HTMLSelectElement>("configuration").value || null;
  applyConfiguration();
  el("downloads").replaceChildren();
  if (refView?.heat) {
    // A heatmap of other outputs would mislead.
    refView.heat = null;
    refView.heatStats = null;
    if (refView.source === "heatmap") refView.source = "photo";
    drawReferenceOverlay();
  }
};
let decalsEnabled = true,
  decalGeneration = 0;
try {
  decalsEnabled = localStorage.getItem("mathforge.decals") !== "off";
} catch {
  // Blocked browser storage: decals start switched on.
}
const decalTextures = new Map<string, Promise<DecalTexture>>();
function clearDecals() {
  for (const mesh of decalMeshes) {
    mesh.parent?.remove(mesh);
    mesh.geometry.dispose();
    (mesh.material as THREE.Material).dispose();
  }
  decalMeshes.length = 0;
}
function decalTexture(spec: DecalSpec) {
  const source = spec.source?.artifact_id ?? spec.source?.path ?? spec.id,
    textKey = `${source}|${spec.colour_hex ?? ""}|${spec.svg ? spec.svg.length : ""}`;
  if (!decalTextures.has(textKey))
    decalTextures.set(
      textKey,
      (async () => {
        let svg = spec.svg ?? null;
        if (!svg && spec.source?.artifact_id) {
          const response = await fetch(
            `/api/artifacts/${spec.source.artifact_id}`,
            {
              headers: headers(),
            },
          );
          if (!response.ok) throw new Error("SVG artifact unavailable");
          svg = await response.text();
        }
        if (!svg) throw new Error(spec.source_error ?? "No SVG for this decal");
        return svgTexture(svg, spec.colour_hex);
      })(),
    );
  const pending = decalTextures.get(textKey)!;
  pending.catch(() => decalTextures.delete(textKey));
  return pending;
}
/** The output a decal is printed on: the one its anchor names, or — when the
 *  anchor names a feature that is not an output of its own — the output
 *  nearest the anchor point, within 5 mm. */
function decalTarget(spec: DecalSpec) {
  const meshes = mainGroup.children as THREE.Mesh[];
  const named = meshes.find(
    (m) => m.userData.feature_id === spec.anchor.feature_id,
  );
  if (named) return named;
  const point = new THREE.Vector3(...spec.anchor.point_mm);
  let best: THREE.Mesh | null = null,
    distance = 5;
  for (const mesh of meshes) {
    if (!mesh.geometry.boundingBox) mesh.geometry.computeBoundingBox();
    const origin = (mesh.userData.origin as number[] | undefined) ?? [0, 0, 0],
      d = mesh.geometry
        .boundingBox!.clone()
        .translate(new THREE.Vector3(...origin))
        .distanceToPoint(point);
    if (d <= distance) {
      distance = d;
      best = mesh;
    }
  }
  return best;
}
function applyDecals() {
  const generation = ++decalGeneration;
  clearDecals();
  const specs = (overlaysShown()?.decals ?? []) as DecalSpec[],
    button = document.getElementById("decals");
  if (button) {
    button.hidden = !specs.length;
    button.classList.toggle("active", decalsEnabled);
    button.setAttribute("aria-pressed", String(decalsEnabled));
  }
  if (
    !decalsEnabled ||
    !specs.length ||
    !mainGroup.children.length ||
    overlayMode === "normals" ||
    overlayMode === "curvature"
  ) {
    if (button)
      button.title = `${specs.length} decal${specs.length === 1 ? "" : "s"} · visual only, never in STEP${decalsEnabled ? "" : " · hidden"}`;
    return;
  }
  const lit = overlayMode !== "unlit",
    problems: string[] = [];
  let placed = 0,
    pending = specs.length;
  const settle = () => {
    if (--pending > 0 || generation !== decalGeneration || !button) return;
    button.title = `${placed} of ${specs.length} decal${specs.length === 1 ? "" : "s"} shown · visual only, never in STEP${problems.length ? ` · not shown: ${problems.join("; ")}` : ""}`;
    if (problems.length) console.warn("Decals not shown:", problems);
  };
  for (const spec of specs) {
    const problem = decalProblems(spec);
    if (problem) {
      problems.push(`${(spec as any)?.id ?? "?"}: ${problem}`);
      settle();
      continue;
    }
    void decalTexture(spec)
      .then((texture) => {
        if (generation !== decalGeneration) return;
        const target = decalTarget(spec);
        if (!target) throw new Error("no output near the anchor");
        const mesh = projectDecal(
          target,
          spec,
          decalMaterial(texture.texture, lit, section ? [clipping] : []),
        );
        if (!mesh) throw new Error("the projection misses the surface");
        target.add(mesh);
        decalMeshes.push(mesh);
        placed++;
      })
      .catch((error: Error) => problems.push(`${spec.id}: ${error.message}`))
      .finally(settle);
  }
}
el("decals").onclick = () => {
  decalsEnabled = !decalsEnabled;
  try {
    localStorage.setItem("mathforge.decals", decalsEnabled ? "on" : "off");
  } catch {
    // Blocked browser storage: the choice lasts for this session only.
  }
  applyDecals();
};

/* --- Feature proposals -----------------------------------------------------
 * Proposals of a feature batch (or of parallel lanes) never change the model
 * by themselves. Each can be looked at as a ghost candidate — the proposal in
 * the candidate colour over the transparent current state — and accepted or
 * rejected; the accepted ones become one candidate that goes through the usual
 * validation and commit.
 * -------------------------------------------------------------------------*/
let proposalTimer: number | null = null;
const proposalArtifacts = new Map<string, Promise<any>>();
function proposalChoices(batch: string): Record<string, string> {
  try {
    return JSON.parse(
      localStorage.getItem("mathforge.proposals." + batch) ?? "{}",
    );
  } catch {
    return {};
  }
}
function setProposalChoice(batch: string, task: string, choice: string | null) {
  const choices = proposalChoices(batch);
  if (choice) choices[task] = choice;
  else delete choices[task];
  try {
    localStorage.setItem(
      "mathforge.proposals." + batch,
      JSON.stringify(choices),
    );
  } catch {
    // Blocked browser storage: the choice lasts until the list is redrawn.
  }
}
function noteProposalsCommitted(source: { batch: string; tasks: string[] }) {
  for (const task of source.tasks)
    setProposalChoice(source.batch, task, "committed");
}
function proposalArtifact(id: string) {
  if (!proposalArtifacts.has(id))
    proposalArtifacts.set(
      id,
      (async () => {
        const response = await fetch(`/api/artifacts/${id}`, {
          headers: headers(),
        });
        if (!response.ok) throw new Error("Proposal unavailable.");
        return response.json();
      })(),
    );
  const pending = proposalArtifacts.get(id)!;
  pending.catch(() => proposalArtifacts.delete(id));
  return pending;
}
async function loadProposals() {
  if (!current) return;
  const model = current.model_id;
  let data: any;
  try {
    data = await api(`/api/models/${model}/feature-batches`);
  } catch {
    data = { batches: [] };
  }
  if (current?.model_id !== model) return;
  proposalBatches = data.batches ?? [];
  renderProposals();
  const running = proposalBatches.some((b) =>
    ["queued", "running"].includes(b.status),
  );
  if (running && proposalTimer === null)
    proposalTimer = window.setInterval(
      () => void loadProposals().catch(() => {}),
      5000,
    );
  if (!running && proposalTimer !== null) {
    clearInterval(proposalTimer);
    proposalTimer = null;
  }
}
async function previewProposals(batch: any, tasks: any[]) {
  if (!current) return;
  if (candidate) {
    if (candidate.source?.kind !== "proposal")
      throw new Error("Commit or discard the open candidate first.");
    await tool("cad_discard", {
      model_id: current.model_id,
      base_revision: candidate.base_revision,
      transaction_id: candidate.transaction_id,
      idempotency_key: key(),
    });
    await openModel(current.model_id, false);
  }
  const operations: any[] = [];
  for (const task of tasks) {
    const artifact = await proposalArtifact(task.proposal_artifact_id);
    operations.push(
      ...(artifact.operations ?? artifact.proposal?.operations ?? []),
    );
  }
  if (!operations.length)
    throw new Error("These proposals carry no operations.");
  if (operations.length > 64)
    throw new Error(
      `${operations.length} operations are more than one patch carries (64); accept fewer proposals at a time.`,
    );
  busy("Computing the proposal as a candidate …");
  try {
    await readyDraft(
      await tool("cad_apply_patch", {
        model_id: current.model_id,
        base_revision: batch.base_revision,
        operations,
        idempotency_key: key(),
      }),
    );
    candidate.source = {
      kind: "proposal",
      batch: batch.batch_id,
      tasks: tasks.map((t) => t.id),
    };
    activity(
      "Proposal as a candidate",
      `${tasks.map((t) => t.title).join(", ")} · candidate colour: proposal · transparent: current state · validate and commit it under Change verification, or discard it.`,
    );
  } finally {
    busy("", false);
  }
  renderProposals();
}
function renderProposals() {
  const box = document.getElementById("proposals");
  if (!box) return;
  const batches = proposalBatches
    .filter(
      (b) =>
        ["queued", "running"].includes(b.status) ||
        b.tasks.some((t: any) => t.proposal_artifact_id),
    )
    .slice(0, 4);
  box.hidden = !batches.length;
  box.replaceChildren();
  if (!batches.length) return;
  const heading = document.createElement("h3");
  heading.className = "proposals-heading";
  heading.textContent = "Feature proposals";
  box.append(heading);
  for (const batch of batches) {
    const stale = batch.base_revision !== current?.revision,
      choices = proposalChoices(batch.batch_id),
      card = document.createElement("div");
    card.className = "proposal-batch";
    card.dataset.status = batch.status;
    const title = document.createElement("div");
    title.className = "proposal-title";
    title.textContent = `${batch.kind === "feature_batch" ? "Feature batch" : batch.kind} · ${batch.status}${stale ? " · made for an earlier state" : ""}`;
    title.title = `${batch.batch_id} · base ${batch.base_revision}`;
    card.append(title);
    if (batch.error) {
      const error = document.createElement("p");
      error.className = "order-warning";
      error.textContent = batch.error;
      card.append(error);
    }
    const accepted: any[] = [];
    for (const task of batch.tasks) {
      const choice = choices[task.id] ?? null,
        row = document.createElement("div");
      row.className = "proposal";
      row.dataset.choice = choice ?? "open";
      const name = document.createElement("strong");
      name.textContent = task.title;
      const detail = document.createElement("small");
      detail.textContent = [
        task.state,
        task.model
          ? `${task.model}${task.effort ? " · " + task.effort : ""}`
          : null,
        task.elapsed_ms ? `${Math.round(task.elapsed_ms / 1000)} s` : null,
        choice,
      ]
        .filter(Boolean)
        .join(" · ");
      row.append(name, detail);
      if (task.summary || task.error) {
        const summary = document.createElement("p");
        summary.textContent = task.error ?? task.summary;
        row.append(summary);
      }
      if (task.proposal_artifact_id && choice !== "committed") {
        if (choice === "accepted") accepted.push(task);
        const actions = document.createElement("div");
        actions.className = "order-actions";
        const make = (
          label: string,
          go: () => Promise<void> | void,
          on = false,
        ) => {
          const b = document.createElement("button");
          b.type = "button";
          b.textContent = label;
          if (on) b.classList.add("active");
          b.disabled = stale && label === "Preview";
          b.onclick = action(async () => {
            await go();
          });
          return b;
        };
        actions.append(
          make("Preview", () => previewProposals(batch, [task])),
          make(
            choice === "accepted" ? "Accepted ✓" : "Accept",
            () => {
              setProposalChoice(
                batch.batch_id,
                task.id,
                choice === "accepted" ? null : "accepted",
              );
              renderProposals();
            },
            choice === "accepted",
          ),
          make(
            choice === "rejected" ? "Rejected ✕" : "Reject",
            () => {
              setProposalChoice(
                batch.batch_id,
                task.id,
                choice === "rejected" ? null : "rejected",
              );
              renderProposals();
            },
            choice === "rejected",
          ),
        );
        row.append(actions);
      }
      card.append(row);
    }
    const apply = document.createElement("button");
    apply.type = "button";
    apply.className = "primary full";
    apply.textContent = `Candidate from accepted (${accepted.length})`;
    apply.disabled = !accepted.length || stale;
    apply.title = stale
      ? "The proposals were made for an earlier state; ask for a new batch."
      : "One candidate from all accepted proposals; validate and commit it as usual.";
    apply.onclick = action(async () => previewProposals(batch, accepted));
    card.append(apply);
    box.append(card);
  }
}
