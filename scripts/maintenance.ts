import { Store } from "../packages/model-service/store.js";
import {
  backupStore,
  restoreStore,
  garbageCollect,
  eraseTenant,
  applyRetention,
} from "../packages/model-service/maintenance.js";
import { requireThat } from "../packages/semantic-ir/errors.js";
const [command, source, target, flag] = process.argv.slice(2);
requireThat(
  source &&
    ["backup", "restore", "gc", "retention", "erase-tenant"].includes(command),
  "INVALID_SCHEMA",
  "Aufruf: tsx scripts/maintenance.ts backup DATA NEUES_ZIEL | restore BACKUP NEUES_ZIEL | gc DATA | retention DATA | erase-tenant DATA TENANT [--apply]",
);
if (command === "restore") {
  requireThat(target, "INVALID_SCHEMA", "Ziel fehlt.");
  console.log(restoreStore(source, target));
} else {
  const store = new Store(source);
  try {
    if (command === "backup") {
      requireThat(target, "INVALID_SCHEMA", "Ziel fehlt.");
      const result = await backupStore(store, target);
      console.log({ status: "backed_up", blobs: result.blobs.length });
    } else if (command === "erase-tenant") {
      requireThat(
        target && (!flag || flag === "--apply"),
        "INVALID_SCHEMA",
        "Mandant und optional --apply erforderlich.",
      );
      console.log(eraseTenant(store, target, flag === "--apply"));
    } else if (command === "retention")
      console.log({ ...applyRetention(store), ...garbageCollect(store) });
    else console.log(garbageCollect(store));
  } finally {
    store.close();
  }
}
