/**
 * Map conversational landing-page copy onto the existing typed API surface.
 * This is intentionally only routing: the selected endpoint's structured form
 * remains the authority before a quote is requested or a payment is signed.
 *
 * The rules are ordered, and the order is load-bearing:
 *
 *  - `team report` and `grade my … team` outrank the roster audit, so "grade my
 *    team" is not read as "my team".
 *  - the draft rules outrank the start/sit rules, so "should I draft A or B"
 *    is a draft question rather than a lineup question.
 *  - the board rules outrank start/sit, so "which sleepers should I start" is
 *    still the sleepers board.
 *  - `report` reaches its own rule only after `team report` and `draft report`
 *    have already claimed theirs.
 *
 * "Sleeper" is also a brand name. Only the plural product noun (and explicit
 * "sleeper pick/candidate") routes to the weekly sleepers board; "Sleeper"
 * followed by an account word is the app, and belongs to the roster rules.
 */
const RULES = [
  [/\bteam report\b|\bgrade\s+(?:my|our)\b[^.?!]*\bteam\b/, "team_report"],
  [/\bdrafts?\b|\bdrafting\b|\badp\b/, "draft_board"],
  [
    /\broster\b|\baudit\b|\bmy team\b|\bsleeper\s+(?:user(?:name)?|handle|league|account)\b/,
    "roster",
  ],
  [/\bwaivers?\b|\badds?\b|\bdrops?\b|\bpick ?ups?\b|\bfree agents?\b/, "waivers"],
  [/\bsleepers\b|\bsleeper (?:picks?|candidates?)\b|\bbreakouts?\b/, "sleepers"],
  [/\bbriefing\b|\bweekly report\b|\breport\b/, "report"],
  [
    /\bstart\s*\/\s*sit\b|\bstart or sit\b|\bshould i (?:start|sit)\b|\bstart\b[^.?!]*\bover\b|\bversus\b|\bvs\.?(?=\s|$)|\s+or\s+/,
    "matchup",
  ],
  [/\btrend(?:s|ing)?\b|\bmarket\b/, "trending"],
];

export function endpointForQuestion(question) {
  // Collapse newlines and runs of spaces: a textarea Enter must not defeat a
  // rule that expects single-spaced words (" or " being the obvious one).
  const value = String(question || "")
    .toLowerCase()
    .replace(/\s+/g, " ")
    .trim();
  for (const [pattern, endpoint] of RULES) {
    if (pattern.test(value)) return endpoint;
  }
  return "player";
}

export function labelForEndpoint(key) {
  return {
    matchup: "start / sit",
    roster: "roster audit",
    waivers: "waiver board",
    sleepers: "weekly sleepers",
    draft_board: "draft board",
    trending: "trending board",
    report: "weekly briefing",
    team_report: "team report",
    player: "player deep dive",
  }[key] || "player deep dive";
}
