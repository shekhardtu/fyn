import { describe, expect, it } from "vitest";
import { z } from "zod";

import contractBundle from "@/lib/generated/contracts.json";
import { schemas as generatedSchemas } from "@/lib/generated/contracts.zod";

/**
 * The generated Zod module replaced a runtime `z.fromJSONSchema` conversion of
 * the same bundle. That was worth 164 KB and ~39 ms on every page load, but it
 * is only worth it if the two agree — a validator that is subtly stricter would
 * start rejecting the backend's own responses, and one that is subtly looser
 * would stop catching contract drift.
 *
 * So the old path is reconstructed here, in a test that never ships, and the
 * two are compared on the same inputs. If the emitter and Zod's converter ever
 * disagree, this is where it surfaces.
 */

type Bundle = typeof contractBundle;
const schemaNames = Object.keys(contractBundle.schemas) as Array<keyof Bundle["schemas"]>;

const fromJson = (name: keyof Bundle["schemas"]) =>
  z.fromJSONSchema(contractBundle.schemas[name] as never);

/** Builds a value that should satisfy a schema, resolving $refs against the
 *  schema's own definitions. Deliberately literal-minded: the point is to
 *  produce the same input for both validators, not to be clever. */
function sample(node: Record<string, unknown>, defs: Record<string, never>, depth = 0): unknown {
  if (depth > 6) return null;
  const ref = node.$ref as string | undefined;
  if (ref) return sample(defs[ref.split("/").pop() as string] ?? {}, defs, depth + 1);
  if ("const" in node) return node.const;
  if (Array.isArray(node.enum)) return node.enum[0];
  if (Array.isArray(node.anyOf)) return sample(node.anyOf[0] as Record<string, unknown>, defs, depth + 1);

  switch (node.type) {
    case "string": {
      if (node.format === "uuid") return "6aa484c7-c64c-4a6f-ae11-b031c75b77b5";
      if (node.format === "date-time") return "2026-08-11T09:30:00Z";
      if (node.format === "date") return "2026-08-11";
      if (typeof node.pattern === "string") return null; // patterns need a tailored value
      const min = typeof node.minLength === "number" ? node.minLength : 0;
      const max = typeof node.maxLength === "number" ? node.maxLength : 8;
      return "a".repeat(Math.min(Math.max(min, 3), max));
    }
    case "integer":
    case "number": {
      const floor = typeof node.exclusiveMinimum === "number" ? node.exclusiveMinimum + 1 : (node.minimum as number | undefined) ?? 1;
      const ceiling = typeof node.maximum === "number" ? node.maximum : floor + 1;
      return Math.min(floor, ceiling);
    }
    case "boolean": return true;
    case "null": return null;
    case "array": return node.items ? [sample(node.items as Record<string, unknown>, defs, depth + 1)] : [];
    case "object": {
      const properties = (node.properties ?? {}) as Record<string, Record<string, unknown>>;
      const required = new Set((node.required as string[] | undefined) ?? []);
      const value: Record<string, unknown> = {};
      for (const [key, child] of Object.entries(properties)) {
        if (required.has(key)) value[key] = sample(child, defs, depth + 1);
      }
      return value;
    }
    default: return null;
  }
}

describe("generated Zod contracts match the JSON Schema they replaced", () => {
  it("exports a schema for every name the bundle declares", () => {
    expect(Object.keys(generatedSchemas).sort()).toEqual(schemaNames.slice().sort());
  });

  it.each(schemaNames)("%s agrees on a conforming value", (name) => {
    const schema = contractBundle.schemas[name] as Record<string, unknown>;
    const value = sample(schema, (schema.$defs ?? {}) as Record<string, never>);

    const old = fromJson(name).safeParse(value);
    const next = generatedSchemas[name].safeParse(value);

    expect(next.success).toBe(old.success);
    // Output matters as much as the verdict: defaults must be filled the same
    // way, and unknown keys must survive in both.
    if (old.success && next.success) expect(next.data).toEqual(old.data);
  });

  it.each(schemaNames)("%s agrees on rejecting a wrong-typed value", (name) => {
    for (const wrong of [null, 42, "nonsense", []]) {
      expect(generatedSchemas[name].safeParse(wrong).success).toBe(fromJson(name).safeParse(wrong).success);
    }
  });

  it("keeps unknown keys instead of stripping them, exactly as before", () => {
    const value = { ...(sample(contractBundle.schemas.BootstrapUser as Record<string, unknown>, {}) as object), surprise: "kept" };
    const old = fromJson("BootstrapUser").parse(value);
    const next = generatedSchemas.BootstrapUser.parse(value);
    expect(next).toEqual(old);
    expect((next as Record<string, unknown>).surprise).toBe("kept");
  });

  it("still enforces the string formats", () => {
    expect(generatedSchemas.BootstrapUser.safeParse({ id: "not-a-uuid", name: "H", currency: "INR", timezone: "Asia/Kolkata" }).success).toBe(false);
    expect(generatedSchemas.BootstrapUser.safeParse({ id: "6aa484c7-c64c-4a6f-ae11-b031c75b77b5", name: "H", currency: "INR", timezone: "Asia/Kolkata" }).success).toBe(true);
  });
});
