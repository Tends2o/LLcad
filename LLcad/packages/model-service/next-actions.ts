/**
 * Concrete next steps for a failed call (recommended_next_actions), so an agent repairs a
 * request in one retry instead of several guesses. Derived from the error code, its
 * details and the caller's own arguments; never throws and never changes the answer's
 * code or message. ERROR_GUIDE is also the source of the llcad://pitfalls resource.
 */
import { safeError } from "../semantic-ir/errors.js";
import { hash } from "../semantic-ir/hash.js";
import { compileStructure } from "../compiler/structure.js";
import type { Principal } from "../policy/index.js";

export const ERROR_GUIDE: Record<string, { cause: string; fix: string }> = {
  INVALID_SCHEMA: {
    cause:
      "The input does not match the tool schema, or a structural rule of the model (part outputs, unique ids, frames, input counts).",
    fix: "Read details.issues (path, expected/received, allowed_keys, options, limits), fix every issue and resend once. Fields and one valid example per operator: llcad://operators.",
  },
  STALE_REVISION: {
    cause:
      "base_revision is no longer the head, or an expected value/hash of an edit operation no longer matches the model.",
    fix: "Re-read with cad_context {feature_ids} (parameter values, construction/feature/context hashes, part output hashes) or cad_get_model {detail: brief} (head_revision); resend on the current head with a new idempotency_key.",
  },
  IDEMPOTENCY_CONFLICT: {
    cause: "The idempotency_key was already used with different arguments.",
    fix: "Use a new idempotency_key for every changed request; the same key with identical arguments replays (and resumes) the earlier call.",
  },
  AMBIGUOUS_SELECTION: {
    cause: "Several faces or features match, or a selector matches nothing.",
    fix: "cad_inspect {sections: [faces]} or cad_find, then pass face_id with an explicit revision (or a narrower edge selector / between_features).",
  },
  BUILD_MISMATCH: {
    cause: "The revision or candidate was built with another registry/build.",
    fix: "cad_rebuild {mode: plan} then {mode: candidate} with target_registry_hash from cad_capabilities; candidates of an old build are re-applied.",
  },
  VALIDATION_REQUIRED: {
    cause: "Commit or export needs a passing validation of exactly this candidate / a committed revision.",
    fix: "cad_apply_patch {validate: true, commit: 'if_valid'} does apply, validate and commit in one call; otherwise cad_validate and cad_commit with its digest.",
  },
  BUDGET_EXCEEDED: {
    cause:
      "A request, answer or job budget was exceeded (1 MiB request, 32 KB answer, 16 open jobs, job time).",
    fix: "Wait (wait_ms) or cancel open jobs, read narrower (limit/offset, detail brief, cad_context), split very large patches. A build that ran out of time continues automatically while it makes progress.",
  },
  KERNEL_FAILURE: {
    cause: "The geometry kernel failed on a feature or the worker ended early.",
    fix: "Simplify the failing feature (error details, cad_job_get warnings): blend single bodies before booleans, avoid near-tangent cuts (>=0.5 mm off or exactly on), split huge glyph/strip patches, use text/svg_emblem operators.",
  },
  GEOMETRY_INVALID: {
    cause: "The result is not a valid solid, or an input is degenerate.",
    fix: "Check profile orientation and planarity, equal section point counts for lofts, that a cut tool reaches material; llcad://pitfalls.",
  },
  PRECISION_UNSUPPORTED: {
    cause:
      "Kernel tolerances exceed the model tolerance, or a value lies outside the precision contract.",
    fix: "Blend before booleans and keep cuts clear of tangency; for photo reconstructions select the explicit preset set_model_settings {profile_preset: visual_reconstruction} (0.01 mm).",
  },
  OUT_OF_SCOPE: {
    cause: "The operation lies outside the registered contract or the access scope.",
    fix: "Follow the message: e.g. one part per representation, instantiate_fragment for other models, a grant for shared projects.",
  },
  CONSTRAINT_CONFLICT: {
    cause:
      "State conflict: feature id exists, candidate not ready, protected feature/constraint touched, repair budget or committed revision.",
    fix: "Use fresh ids, wait for the candidate (wait_ms), keep protected features unchanged, start a new patch on the head.",
  },
  ACCESS_DENIED: {
    cause: "No access to the model, job, artifact or path (patch_ref/svg paths must be inside the allowed folders).",
    fix: "cad_access {mode: inspect}; patch_ref paths must be absolute *.json files inside LLCAD_PATCH_PATHS (default /root/projects, <data>/uploads, <data>/annotations).",
  },
  UNIT_MISMATCH: {
    cause: "A quantity has a unit of the wrong dimension.",
    fix: "Lengths mm | m | um, angles deg | rad, scalars '1'.",
  },
  NEEDS_APPROVAL: {
    cause: "The change needs a separately confirmed grant.",
    fix: "cad_access {mode: propose}; the owner confirms it outside the tool.",
  },
  CANCELLED: {
    cause: "The job or candidate was cancelled or discarded.",
    fix: "Apply the patch again with a new idempotency_key.",
  },
  INTERNAL_ERROR: {
    cause: "Unexpected server error.",
    fix: "Retry once with a new idempotency_key; if it repeats, reduce the request and report it.",
  },
};

type Service = {
  store: {
    revision(p: Principal, model: string, revision?: string): any;
  };
};

const json = (value: unknown) => JSON.stringify(value);

export function nextActions(
  service: Service,
  p: Principal,
  tool: string,
  args: any,
  error: unknown,
): string[] {
  try {
    const e =
      error && typeof error === "object" && "code" in (error as any) && !(error instanceof Error)
        ? (error as any)
        : safeError(error);
    const details = e.details ?? {};
    const out: string[] = [];
    const operations: any[] = Array.isArray(args?.operations) ? args.operations : [];
    if (e.code === "INVALID_SCHEMA" && Array.isArray(details.unregistered_outputs)) {
      // Every model output must be one part's output: say which part, with the hash.
      let head: any = null;
      try {
        head = compileStructure(
          service.store.revision(p, args.model_id, args.base_revision).ir,
        ).structure;
      } catch {
        head = null;
      }
      const byPart = new Map<string, string[]>();
      details.unregistered_outputs.forEach((fid: string, i: number) => {
        const added = operations.find(
          (op) => op?.op === "add_feature" && op.feature?.id === fid,
        );
        const part =
          details.owner_parts?.[i] ?? added?.feature?.owner_part ?? "part-main";
        byPart.set(part, [...(byPart.get(part) ?? []), fid]);
      });
      for (const [part, ids] of byPart) {
        const existing = head?.parts?.find((x: any) => x.id === part);
        out.push(
          existing && head
            ? `Add to the same patch: ${json({ op: "set_part_outputs", part_id: part, expected_hash: hash(existing.outputs), outputs: [...new Set([...existing.outputs, ...ids])] })}`
            : `Add to the same patch: ${json({ op: "add_part", part: { id: part, semantic_name: part, outputs: ids } })} (or list the ids in that part's outputs of your set_structure)`,
        );
      }
      out.push(
        "Every id in set_outputs must be in exactly one part's outputs, and that feature's owner_part must be the part.",
      );
    } else if (e.code === "INVALID_SCHEMA" && details.part_id) {
      out.push(
        `Part ${details.part_id} lists outputs it does not own (${(details.foreign_or_missing_outputs ?? []).join(", ")}): set owner_part of those features to ${details.part_id} (set_feature_context) or move them to their own part's outputs.`,
      );
    } else if (e.code === "INVALID_SCHEMA" && Array.isArray(details.issues)) {
      const issues = details.issues as any[];
      out.push(
        `Fix all ${details.issue_count ?? issues.length} listed schema issues (details.issues) and resend once; field lists and one valid example per operator: llcad://operators.`,
      );
      for (const issue of issues.slice(0, 4)) {
        const at = (issue.path ?? []).join(".") || "(root)";
        if (issue.code === "unrecognized_keys")
          out.push(
            `${at}: remove ${json(issue.keys)}` +
              (issue.allowed_keys ? `; allowed keys: ${issue.allowed_keys.join(", ")}` : ""),
          );
        else if (issue.options && issue.discriminator)
          out.push(
            issue.did_you_mean
              ? `${at}: ${issue.discriminator} "${issue.did_you_mean}"? (${issue.message}; options in details.issues)`
              : `${at}: ${issue.discriminator} must be one of ${issue.options.slice(0, 64).join(", ")}`,
          );
        else if (issue.code === "invalid_value" && issue.options)
          out.push(`${at}: one of ${issue.options.join(", ")}`);
        else if (issue.code === "invalid_type")
          out.push(`${at}: expected ${issue.expected}, received ${issue.received}`);
        else if (issue.code === "too_big" || issue.code === "too_small")
          out.push(
            `${at}: ${issue.code === "too_big" ? "at most " + issue.maximum : "at least " + issue.minimum} (${issue.origin ?? "value"})` +
              (issue.received_length !== undefined ? `, got ${issue.received_length}` : "") +
              (at === "operations" && issue.code === "too_big"
                ? "; split the patch into several calls (each on the new head)"
                : ""),
          );
        else if (issue.hint) out.push(`${at}: ${issue.hint}`);
      }
    } else if (e.code === "STALE_REVISION") {
      if (details.current_revision)
        out.push(
          `The head moved to ${details.current_revision}: re-read what the patch expects on that revision (cad_context) and resend with base_revision ${details.current_revision} and a new idempotency_key.`,
        );
      const ids = [
        ...new Set(
          operations.flatMap((op) => (typeof op?.feature_id === "string" ? [op.feature_id] : [])),
        ),
      ].slice(0, 32);
      if (ids.length && args?.model_id)
        out.push(
          `cad_context ${json({ model_id: args.model_id, feature_ids: ids, depth: 0 })} returns the current parameter values (expected) and construction_hash/feature_hash/context_hash (expected_hash).`,
        );
      if (details.current_hash)
        out.push(`Current hash for this operation: ${details.current_hash}.`);
      if (operations.some((op) => op?.op === "set_structure"))
        out.push("set_structure needs structure_hash of the head (cad_get_model detail brief); prefer add_part / add_frame / set_part_outputs deltas.");
    } else if (e.code === "BUILD_MISMATCH" && details.target_registry_hash && args?.model_id)
      out.push(
        `cad_rebuild ${json({ model_id: args.model_id, base_revision: "<head>", target_registry_hash: details.target_registry_hash, mode: "plan" })}, then mode candidate, validate and commit.`,
      );
    else if (e.code === "IDEMPOTENCY_CONFLICT")
      out.push("Resend with a new idempotency_key (the old one belongs to different arguments).");
    else if (e.code === "AMBIGUOUS_SELECTION" && args?.feature_id)
      out.push(
        `cad_inspect ${json({ model_id: args.model_id, feature_id: args.feature_id, revision: args.revision ?? "<revision>", sections: ["faces"] })} and pass the chosen face_id with that revision.`,
      );
    else if (e.code === "VALIDATION_REQUIRED" && tool === "cad_commit")
      out.push("Validate first: cad_validate {wait_ms: 30000} and commit with its result.digest, or use cad_apply_patch {validate: true, commit: 'if_valid'}.");
    const guide = ERROR_GUIDE[e.code];
    // The general advice only where nothing specific could be said.
    if (guide && !out.length) out.push(guide.fix);
    return out.slice(0, 8);
  } catch {
    return [];
  }
}
