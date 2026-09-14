import {
  closeSync,
  constants,
  existsSync,
  fsyncSync,
  fstatSync,
  linkSync,
  lstatSync,
  mkdirSync,
  openSync,
  readdirSync,
  readFileSync,
  unlinkSync,
  writeFileSync,
} from "node:fs";
import { dirname, join } from "node:path";
import { bytesHash, id } from "../semantic-ir/hash.js";
import { requireThat } from "../semantic-ir/errors.js";

export function syncDirectory(path: string) {
  const fd = openSync(path, constants.O_RDONLY | constants.O_DIRECTORY);
  try {
    fsyncSync(fd);
  } finally {
    closeSync(fd);
  }
}

export function syncFile(path: string) {
  const fd = openSync(path, constants.O_RDONLY | constants.O_NOFOLLOW);
  try {
    fsyncSync(fd);
  } finally {
    closeSync(fd);
  }
}

export function syncCreatedDirectories(root: string, firstCreated?: string) {
  syncDirectory(root);
  if (firstCreated) {
    const boundary = dirname(firstCreated);
    for (let path = dirname(root); ; path = dirname(path)) {
      syncDirectory(path);
      if (path === boundary) break;
    }
  }
}

/** Called only after acquiring the exclusive Store lock. */
export function prepareBlobStorage(
  root: string,
  firstCreated?: string,
  migrateExisting = false,
) {
  const staging = join(root, ".blob-staging");
  mkdirSync(staging, { recursive: true, mode: 0o700 });
  requireThat(
    lstatSync(staging).isDirectory() &&
      lstatSync(join(root, "blobs")).isDirectory(),
    "INTEGRITY_FAILURE",
    "Blobverzeichnisse müssen echte private Verzeichnisse sein.",
  );
  for (const name of readdirSync(staging)) {
    const path = join(staging, name);
    requireThat(
      /^blob-[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/.test(
        name,
      ) && lstatSync(path).isFile(),
      "INTEGRITY_FAILURE",
      "Unerwarteter Eintrag im privaten Schreibbereich.",
    );
    unlinkSync(path);
  }
  let synchronized = 0;
  if (migrateExisting) {
    for (const name of readdirSync(join(root, "blobs"))) {
      requireThat(
        /^[a-f0-9]{64}$/.test(name),
        "INTEGRITY_FAILURE",
        "Unerwartete vorhandene Blobdatei.",
      );
      verifyExisting(join(root, "blobs", name), name);
      synchronized++;
    }
  }
  syncDirectory(staging);
  syncDirectory(join(root, "blobs"));
  // Persist all newly created ancestor entries, including the first one's name.
  syncCreatedDirectories(root, firstCreated);
  return synchronized;
}

function verifyExisting(path: string, expected: string) {
  const fd = openSync(path, constants.O_RDONLY | constants.O_NOFOLLOW);
  try {
    requireThat(
      fstatSync(fd).isFile() && bytesHash(readFileSync(fd)) === expected,
      "INTEGRITY_FAILURE",
      "Vorhandenes Artefakt stimmt nicht mit seinem Inhaltshash überein.",
    );
    fsyncSync(fd);
  } finally {
    closeSync(fd);
  }
}

/** Publish only complete, synced bytes; the content-addressed name is never overwritten. */
export function durableBlob(root: string, data: Uint8Array | string) {
  const digest = bytesHash(data),
    directory = join(root, "blobs"),
    destination = join(directory, digest);
  if (existsSync(destination)) {
    verifyExisting(destination, digest);
    syncDirectory(directory);
    return digest;
  }
  const staging = join(root, ".blob-staging"),
    temporary = join(staging, id("blob"));
  let fd: number | undefined;
  try {
    fd = openSync(temporary, "wx", 0o600);
    writeFileSync(fd, data);
    fsyncSync(fd);
    closeSync(fd);
    fd = undefined;
    try {
      linkSync(temporary, destination);
    } catch (error: any) {
      if (error.code !== "EEXIST") throw error;
      verifyExisting(destination, digest);
    }
    syncDirectory(directory);
  } finally {
    if (fd !== undefined) closeSync(fd);
    if (existsSync(temporary)) unlinkSync(temporary);
    syncDirectory(staging);
  }
  return digest;
}
