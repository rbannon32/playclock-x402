import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { canvasLines, renderAnalysis, renderTrendingPreview, safeHref } from "./render.js";

// The renderer only needs these DOM operations; no browser, wallet or network.
function withDom(run) {
  class TestNode {
    constructor() {
      this.children = [];
      this.dataset = {};
      this.textContent = "";
    }
    appendChild(child) { this.children.push(child); }
    setAttribute() {}
  }
  const priorNode = Object.getOwnPropertyDescriptor(globalThis, "Node");
  const priorDocument = Object.getOwnPropertyDescriptor(globalThis, "document");
  globalThis.Node = TestNode;
  globalThis.document = {
    createElement: () => new TestNode(),
    createTextNode: (value) => Object.assign(new TestNode(), { textContent: value }),
  };
  try {
    run();
  } finally {
    for (const [name, prior] of [["Node", priorNode], ["document", priorDocument]]) {
      if (prior) Object.defineProperty(globalThis, name, prior);
      else delete globalThis[name];
    }
  }
}

function renderedText(node) {
  return [node.textContent, ...node.children.map(renderedText)].join(" ");
}

describe("optional response rows", () => {
  for (const field of ["stats_cited", "sources"]) {
    it(`keeps a paid answer visible when ${field} contains null rows`, () => withDom(() => {
      const row = field === "stats_cited"
        ? { stat: "Targets", value: 8, player: "Test Player", source: "nflverse" }
        : { title: "Research source", url: "https://example.com" };
      const result = renderAnalysis("player", {
        verdict: "Keep the valid verdict",
        [field]: [null, row, undefined],
      }, { paid: true, receipt: { success: true, transaction: "test-transaction" } });
      const output = renderedText(result);
      assert.match(output, /Keep the valid verdict/);
      assert.match(output, field === "stats_cited" ? /Targets/ : /Research source/);
      assert.match(output, /Payment settled/);
      assert.match(output, /test-transaction/);
    }));
  }

  it("keeps valid preview players beside null rows", () => withDom(() => {
    const result = renderTrendingPreview({ players: [
      null, { name: "Test Player", trend: "add", trend_count: 3 }, undefined,
    ] });
    assert.match(renderedText(result), /Test Player/);
    assert.equal(result.children.length, 1);
  }));
});

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
