import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { renderError } from "./flow.js";

function statusNode() {
  return { hidden: true, textContent: "", dataset: {} };
}

describe("renderError after a paid answer", () => {
  it("never says nothing was charged once a paid 2xx is in hand", () => {
    const status = statusNode();
    const original = console.error;
    console.error = () => {};
    try {
      renderError(new TypeError("render blew up"), status, { paid: true, settled: true });
    } finally {
      console.error = original;
    }
    assert.doesNotMatch(status.textContent, /nothing was charged/i);
    assert.match(status.textContent, /payment went through/i);
    assert.match(status.textContent, /do not pay again/i);
    assert.equal(status.dataset.kind, "error");
  });

  it("keeps the not-charged wording when nothing was paid", () => {
    const status = statusNode();
    const original = console.error;
    console.error = () => {};
    try {
      renderError(new TypeError("boom"), status, { paid: false });
    } finally {
      console.error = original;
    }
    assert.match(status.textContent, /Nothing was charged/);
  });

  it("does not claim the payment went through without a successful receipt", () => {
    const status = statusNode();
    const original = console.error;
    console.error = () => {};
    try {
      renderError(new TypeError("render blew up"), status, { paid: true, settled: false });
    } finally {
      console.error = original;
    }
    assert.doesNotMatch(status.textContent, /went through/i);
    assert.doesNotMatch(status.textContent, /nothing was charged/i);
    assert.match(status.textContent, /check your wallet before paying again/i);
  });
});
