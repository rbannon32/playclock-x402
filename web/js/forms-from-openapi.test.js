/**
 * Deriving form fields from the API's own schemas.
 *
 * The point of this module is that a new paid endpoint arrives in the UI with
 * a working form and the bounds the server will actually enforce, instead of
 * being invisible until someone hand-writes one. The draft board and draft
 * report shipped and could not be bought by a human, which is the bug these
 * tests exist to keep fixed.
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { formSpecFor, humanize } from "./forms-from-openapi.js";

const DOC = {
  paths: {
    "/v1/trending": {
      get: {
        parameters: [
          {
            name: "week",
            in: "query",
            required: false,
            schema: { anyOf: [{ type: "integer" }, { type: "null" }] },
            description: "NFL week.",
          },
          {
            name: "limit",
            in: "query",
            required: true,
            schema: { type: "integer", minimum: 1, maximum: 50, default: 25 },
          },
          { name: "internal", in: "header", required: false, schema: { type: "string" } },
        ],
      },
    },
    "/v1/draft-report": {
      post: {
        requestBody: {
          content: { "application/json": { schema: { $ref: "#/components/schemas/DraftReportRequest" } } },
        },
      },
    },
    "/v1/roster": {
      post: {
        requestBody: {
          content: { "application/json": { schema: { $ref: "#/components/schemas/RosterRequest" } } },
        },
      },
    },
  },
  components: {
    schemas: {
      DraftReportRequest: {
        type: "object",
        properties: {
          draft_id: { anyOf: [{ type: "string" }, { type: "null" }], description: "Sleeper draft id." },
          draft_slot: { anyOf: [{ type: "integer", minimum: 1 }, { type: "null" }] },
          sleeper_username: { type: "string" },
        },
        required: ["sleeper_username"],
      },
      RosterRequest: {
        type: "object",
        properties: {
          league_id: { type: "string" },
          roster: { type: "array", items: { type: "object" } },
        },
        required: [],
      },
    },
  },
};

describe("humanize", () => {
  it("turns a key into a label nobody had to write", () => {
    assert.equal(humanize("draft_slot"), "Draft slot");
    assert.equal(humanize("sleeper_username"), "Sleeper username");
    assert.equal(humanize("week"), "Week");
  });
});

describe("formSpecFor — query endpoints", () => {
  const spec = formSpecFor(DOC, { path: "/v1/trending", method: "GET" });

  it("derives the query parameters", () => {
    assert.equal(spec.in, "query");
    assert.deepEqual(
      spec.fields.map((f) => f.name),
      ["week", "limit"],
    );
  });

  it("carries the bounds the server will actually enforce", () => {
    // A hand-written copy of these is free to drift from the Pydantic model.
    const limit = spec.fields.find((f) => f.name === "limit");
    assert.equal(limit.min, 1);
    assert.equal(limit.max, 50);
    assert.equal(limit.placeholder, "25");
    assert.equal(limit.required, true);
  });

  it("unwraps FastAPI's optional anyOf", () => {
    const week = spec.fields.find((f) => f.name === "week");
    assert.equal(week.type, "number");
    assert.equal(week.required, false);
    assert.equal(week.hint, "NFL week.");
  });

  it("ignores parameters that are not query", () => {
    assert.ok(!spec.fields.some((f) => f.name === "internal"));
  });
});

describe("formSpecFor — body endpoints", () => {
  it("derives a body form from the request schema", () => {
    const spec = formSpecFor(DOC, { path: "/v1/draft-report", method: "POST" });
    assert.equal(spec.in, "body");
    assert.deepEqual(
      spec.fields.map((f) => f.name),
      ["draft_id", "draft_slot", "sleeper_username"],
    );
    assert.equal(spec.fields.find((f) => f.name === "sleeper_username").required, true);
  });

  it("skips fields no person should be asked for", () => {
    const spec = formSpecFor(DOC, { path: "/v1/roster", method: "POST" });
    assert.ok(!spec || !spec.fields.some((f) => f.name === "league_id"));
  });

  it("refuses rather than rendering a form that would submit nothing", () => {
    // RosterRequest is a league id we hide plus an array needing a real widget,
    // so there is no honest single-input form. Saying so beats an empty POST.
    assert.equal(formSpecFor(DOC, { path: "/v1/roster", method: "POST" }), null);
  });

  it("returns null for a path the document does not describe", () => {
    assert.equal(formSpecFor(DOC, { path: "/v1/nope", method: "GET" }), null);
  });
});
