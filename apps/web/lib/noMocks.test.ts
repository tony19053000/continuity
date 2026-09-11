/**
 * `04_FRONTEND_SPEC.md` §3 acceptance: no mock fixture is imported by
 * production code, and no component contains a hardcoded provider, change,
 * workflow, test count, or status.
 *
 * Repository-wide and structural, so it holds for screens nobody has written
 * yet. A demo that renders convincing fake data is the single easiest way to
 * make this product look finished while being useless, and this is what stops
 * it landing by accident.
 */

import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

const ROOT = join(__dirname, "..");
const SOURCE_DIRS = ["app", "components", "lib"];

function walk(dir: string): string[] {
  const found: string[] = [];
  for (const entry of readdirSync(dir)) {
    if (entry === "node_modules" || entry.startsWith(".")) continue;
    const path = join(dir, entry);
    if (statSync(path).isDirectory()) {
      found.push(...walk(path));
    } else if (/\.tsx?$/.test(entry)) {
      found.push(path);
    }
  }
  return found;
}

const ALL = SOURCE_DIRS.flatMap((dir) => walk(join(ROOT, dir)));
const PRODUCTION = ALL.filter((path) => !/\.test\.tsx?$/.test(path));

describe("production code", () => {
  it("has files to check", () => {
    // Guards every test below against passing by finding nothing.
    expect(PRODUCTION.length).toBeGreaterThan(8);
  });

  it("imports no test fixture, mock, or stub", () => {
    const offenders = PRODUCTION.filter((path) => {
      const source = readFileSync(path, "utf8");
      return /from\s+["'][^"']*(mock|fixture|stub|sample-data|demo-data)/i.test(
        source,
      );
    });

    expect(offenders).toEqual([]);
  });

  it("never imports a testing library", () => {
    const offenders = PRODUCTION.filter((path) =>
      /from\s+["'](vitest|@testing-library|msw)/.test(
        readFileSync(path, "utf8"),
      ),
    );

    expect(offenders).toEqual([]);
  });

  it("hardcodes no provider name", () => {
    // Real provider names a demo would reach for. A screen showing "Stripe"
    // when the backend never mentioned Stripe is fabricated data.
    const providers =
      /\b(stripe|twilio|sendgrid|shopify|plaid|acmepay|paypal|braintree)\b/i;

    const offenders = PRODUCTION.filter((path) =>
      providers.test(readFileSync(path, "utf8")),
    );

    expect(offenders).toEqual([]);
  });

  it("hardcodes no counts, versions, or percentages a screen should read", () => {
    // The numbers from the spec's worked example. Seeing one of these in a
    // component means it is rendering an illustration rather than data.
    const fabricated = /["'>\s](23 Integration Points|7 Providers|91%|1 Active Change)/;

    const offenders = PRODUCTION.filter((path) =>
      fabricated.test(readFileSync(path, "utf8")),
    );

    expect(offenders).toEqual([]);
  });

  it("declares no literal array of displayable records", () => {
    // A component holding `const changes = [{...}]` is a mock by another name.
    const inlineData =
      /const\s+(projects|changes|runs|integrations|findings|approvals|events)\s*(:[^=]+)?=\s*\[\s*\{/;

    const offenders = PRODUCTION.filter((path) =>
      inlineData.test(readFileSync(path, "utf8")),
    );

    expect(offenders).toEqual([]);
  });
});

describe("the rules are enforceable", () => {
  it("detects a planted mock import", () => {
    // A structural test that has quietly stopped matching looks exactly like a
    // passing one, so this feeds it something it must reject.
    const planted = `import { projects } from "../fixtures/mock-projects";`;

    expect(
      /from\s+["'][^"']*(mock|fixture|stub|sample-data|demo-data)/i.test(planted),
    ).toBe(true);
  });

  it("detects planted inline data", () => {
    const planted = `const changes = [{ id: "1", resource: "POST /v1/charges" }];`;

    expect(
      /const\s+(projects|changes|runs|integrations|findings|approvals|events)\s*(:[^=]+)?=\s*\[\s*\{/.test(
        planted,
      ),
    ).toBe(true);
  });
});
