import test from "node:test";
import assert from "node:assert/strict";
import * as THREE from "three";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";
import { relativePositions } from "../../packages/derived-geometry/coordinates.js";
import { packGLB } from "../../packages/model-service/glb.js";

const origin = [900000, -800000, 700000];
const vertices = [
  [0, 0, 0],
  [0.02, 0, 0],
  [0, 0.02, 0],
  [0, 0, 0.02],
].map((p) => p.map((v, i) => v + origin[i]));
const triangles = [
  [0, 2, 1],
  [0, 1, 3],
  [1, 2, 3],
  [2, 0, 3],
];

test("local Float32 mesh storage preserves a 20-micrometre detail at large world coordinates", () => {
  assert.equal(Math.fround(vertices[0][0]), Math.fround(vertices[1][0]));
  const before = structuredClone(vertices),
    local = relativePositions(vertices);
  assert.deepEqual(vertices, before);
  assert.ok(local.maxCoordinateError < 1e-8);
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute(
    "position",
    new THREE.BufferAttribute(local.positions, 3),
  );
  geometry.setIndex(triangles.flat());
  const mesh = new THREE.Mesh(
    geometry,
    new THREE.MeshBasicMaterial({ side: THREE.DoubleSide }),
  );
  mesh.position.fromArray(local.origin);
  mesh.updateMatrixWorld(true);
  const box = new THREE.Box3().setFromObject(mesh);
  assert.ok(Math.abs(box.max.x - box.min.x - 0.02) < 1e-8);
  const ray = new THREE.Raycaster(
    new THREE.Vector3(origin[0] + 0.002, origin[1] + 0.002, origin[2] + 1),
    new THREE.Vector3(0, 0, -1),
  );
  const hits = ray.intersectObject(mesh);
  assert.ok(hits.length);
  assert.ok(Math.abs(hits[0].point.z - origin[2] - 0.016) < 1e-8);
  const camera = new THREE.PerspectiveCamera(38, 1, 0.0001, 10);
  camera.position.set(origin[0] + 0.01, origin[1] + 0.01, origin[2] + 1);
  camera.lookAt(new THREE.Vector3().fromArray(origin));
  camera.updateMatrixWorld(true);
  const modelView = new THREE.Matrix4().multiplyMatrices(
    camera.matrixWorldInverse,
    mesh.matrixWorld,
  );
  const gpuMatrix = new THREE.Matrix4().fromArray(
    Array.from(new Float32Array(modelView.elements)),
  );
  const a = new THREE.Vector3()
    .fromBufferAttribute(geometry.getAttribute("position"), 0)
    .applyMatrix4(gpuMatrix);
  const b = new THREE.Vector3()
    .fromBufferAttribute(geometry.getAttribute("position"), 1)
    .applyMatrix4(gpuMatrix);
  assert.ok(Math.abs(a.distanceTo(b) - 0.02) < 1e-8);
  geometry.dispose();
  mesh.material.dispose();
});

test("GLB actual bytes and independent loader preserve translated mesh detail, axes and indices", async () => {
  const preview = {
    meshes: [
      { feature_id: "positive-part", vertices, triangles },
      {
        feature_id: "negative-part",
        vertices: vertices.map((p) => p.map((x) => -x)),
        triangles: triangles.map((t) => [t[0], t[2], t[1]]),
      },
    ],
  };
  const { buffer, report } = packGLB(preview);
  assert.equal(report.vertices_checked, 8);
  assert.equal(report.indices_checked, 24);
  assert.ok(report.max_coordinate_error_mm < 1e-8);
  const document = await new GLTFLoader().parseAsync(
    Uint8Array.from(buffer).buffer,
    "",
  );
  document.scene.updateMatrixWorld(true);
  let checked = 0;
  document.scene.traverse((object) => {
    if (!(object instanceof THREE.Mesh)) return;
    const source = preview.meshes.find((m) => m.feature_id === object.name)!;
    assert.ok(source);
    const position = object.geometry.getAttribute("position");
    for (let i = 0; i < position.count; i++) {
      const p = new THREE.Vector3()
        .fromBufferAttribute(position, i)
        .applyMatrix4(object.matrixWorld);
      const worldMM = [p.x * 1000, -p.z * 1000, p.y * 1000];
      worldMM.forEach((x, j) =>
        assert.ok(Math.abs(x - source.vertices[i][j]) < 1e-8),
      );
      checked++;
    }
    assert.deepEqual(
      Array.from(object.geometry.getIndex()!.array),
      source.triangles.flat(),
    );
    object.geometry.dispose();
  });
  assert.equal(checked, 8);
});

test("relative mesh positions reject invalid numeric domains", () => {
  for (const points of [
    [],
    [[0, 0]],
    [[Infinity, 0, 0]],
    [[NaN, 0, 0]],
    [
      [1e100, 0, 0],
      [-1e100, 0, 0],
    ],
  ])
    assert.throws(() => relativePositions(points));
  assert.throws(() => relativePositions(vertices, 0));
});
