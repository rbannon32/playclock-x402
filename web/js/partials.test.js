import assert from "node:assert/strict";
import { afterEach, describe, it } from "node:test";

import { resolveApiLinks } from "./partials.js";

/** Minimal stand-in for the browser globals `config.js` reads. */
function browser(hostname) {
  globalThis.window = {
    location: { hostname, search: "" },
    localStorage: {
      getItem: () => null,
      removeItem: () => {},
      setItem: () => {},
    },
  };
}

/** A fake root exposing only what `resolveApiLinks` touches. */
function page(paths) {
  const anchors = paths.map((path) => ({ dataset: { apiHref: path }, href: path }));
  return { anchors, querySelectorAll: () => anchors };
}

afterEach(() => {
  delete globalThis.window;
});

describe("API links on the docs page", () => {
  it("sends the discovery links to the API host, not this site", () => {
    // These four are served by the api service. Left same-origin they 404 on
    // playclock.xyz — which is what shipped, on the page written for agents.
    browser("playclock.xyz");
    const root = page(["/docs", "/openapi.json", "/llms.txt", "/v1/catalog"]);

    resolveApiLinks(root);

    assert.deepEqual(
      root.anchors.map((a) => a.href),
      [
        "https://api.playclock.xyz/docs",
        "https://api.playclock.xyz/openapi.json",
        "https://api.playclock.xyz/llms.txt",
        "https://api.playclock.xyz/v1/catalog",
      ],
    );
  });

  it("leaves them same-origin locally, where one server serves both", () => {
    browser("localhost");
    const root = page(["/openapi.json", "/v1/catalog"]);

    resolveApiLinks(root);

    assert.deepEqual(root.anchors.map((a) => a.href), ["/openapi.json", "/v1/catalog"]);
  });

  it("touches nothing on a page with no API links", () => {
    browser("playclock.xyz");
    const root = page([]);

    resolveApiLinks(root);

    assert.deepEqual(root.anchors, []);
  });
});
