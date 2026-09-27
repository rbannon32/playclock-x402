import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { endpointForQuestion, labelForEndpoint } from "./question-router.js";

describe("conversational endpoint routing", () => {
  const cases = [
    ["Start Bijan or Achane this week?", "matchup"],
    ["Audit my Sleeper roster", "roster"],
    ["Who should I pick up from waivers?", "waivers"],
    ["Give me three breakout sleepers", "sleepers"],
    ["Show me the draft board by ADP", "draft_board"],
    ["What is trending across the market?", "trending"],
    ["Is Bijan Robinson healthy enough to play?", "player"],
    // Plural product nouns must not fall through to the one endpoint that
    // demands a player name.
    ["Top sleepers for week 2", "sleepers"],
    ["Best waivers this week?", "waivers"],
    ["Best adds this week", "waivers"],
    // The two endpoints that had no rule at all.
    ["Give me this week's briefing", "report"],
    ["Team report for my league", "team_report"],
    // "Sleeper" the app, not the weekly sleepers board.
    ["Grade my Sleeper team", "team_report"],
    ["What is my Sleeper username", "roster"],
    // Boundary and ordering.
    ["Bijan? or Achane", "matchup"],
    ["Should I draft Bijan or Achane?", "draft_board"],
    ["Which sleepers should I start?", "sleepers"],
    // A textarea newline must not defeat the " or " rule.
    ["Bijan\nor Achane?", "matchup"],
  ];

  for (const [question, endpoint] of cases) {
    it(`routes “${question.replace(/\n/g, "\\n")}” to ${endpoint}`, () => {
      assert.equal(endpointForQuestion(question), endpoint);
    });
  }

  it("never routes a Sleeper account question to the sleepers board", () => {
    assert.notEqual(endpointForQuestion("What is my Sleeper username"), "sleepers");
  });

  it("labels every endpoint it can route to", () => {
    const routed = new Set(cases.map(([question]) => endpointForQuestion(question)));
    for (const key of routed) {
      assert.notEqual(labelForEndpoint(key), key);
      if (key !== "player") assert.notEqual(labelForEndpoint(key), "player deep dive");
    }
  });
});
