import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { canvasLines, safeHref } from "./render.js";

/** A canvas measuring context stand-in: every glyph is exactly 10px wide. */
const context = { measureText: (value) => ({ width: String(value).length * 10 }) };

describe("share-card line wrapping", () => {
  it("keeps every word when the verdict fits inside the line budget", () => {
    assert.deepEqual(canvasLines(context, "aaaa bbbb cccc dddd eeee ffff", 100, 3), [
      "aaaa bbbb",
      "cccc dddd",
      "eeee ffff",
    ]);
  });

  it("ellipsises only the last kept line when the verdict overflows", () => {
    assert.deepEqual(canvasLines(context, "aaaa bbbb cccc dddd eeee ffff gggg", 100, 3), [
      "aaaa bbbb",
      "cccc dddd",
      "eeee ffff…",
    ]);
  });

  it("trims the last line until the ellipsis itself fits", () => {
    assert.deepEqual(canvasLines(context, "aaaa bbbb cccc dddd eeee ffff gggg", 95, 3), [
      "aaaa bbbb",
      "cccc dddd",
      "eeee fff…",
    ]);
  });

  it("returns nothing for an empty verdict", () => {
    assert.deepEqual(canvasLines(context, "", 100, 3), []);
  });
});

describe("safeHref", () => {
  it("keeps http(s) links and drops every other scheme", () => {
    assert.equal(safeHref("https://example.com/a"), "https://example.com/a");
    assert.equal(safeHref("http://example.com"), "http://example.com");
    assert.equal(safeHref("javascript:alert(1)"), null);
    assert.equal(safeHref(" JavaScript:alert(1)"), null);
    assert.equal(safeHref("data:text/html,<b>x</b>"), null);
    assert.equal(safeHref("not a url"), null);
    assert.equal(safeHref(null), null);
  });
});
