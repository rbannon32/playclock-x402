/**
 * Build form fields from the API's own OpenAPI document.
 *
 * The UI used to hard-code every endpoint's inputs, which had two costs. The
 * obvious one: a new paid endpoint was invisible until someone edited this
 * directory, so the draft board and draft report shipped and could not be
 * bought by a human. The quieter one: every field's bounds were a second copy
 * of the Pydantic model, free to drift from what the server will actually
 * accept.
 *
 * So fields are derived — the same trick `playclock_mcp/tools.py` uses to turn
 * the catalog into tools. `FORM_SPECS` survives as an *override* for the three
 * inputs that are genuinely custom widgets (the player repeater, the pasted
 * roster) rather than a text box with a label.
 *
 * Dependency-free and pure: it takes the document and returns field
 * descriptors, so it is testable in node with no browser and no network.
 */

/** Fields the server accepts but a person should never be asked to fill in. */
const HIDDEN_FIELDS = new Set(["league_id"]);

/** `snake_case` -> `Sentence case`, for a label nobody wrote by hand. */
export function humanize(name) {
  const words = String(name).replace(/[_-]+/g, " ").trim().split(/\s+/);
  if (words.length === 0) return "";
  const [first, ...rest] = words;
  return [first.charAt(0).toUpperCase() + first.slice(1), ...rest].join(" ");
}

/** Follow a `#/components/schemas/X` (or `#/$defs/X`) reference. */
function resolve(doc, node) {
  const ref = node && node.$ref;
  if (!ref) return node || {};
  const name = ref.split("/").pop();
  const components = (doc.components && doc.components.schemas) || {};
  return components[name] || {};
}

/** Unwrap the `anyOf: [T, null]` FastAPI emits for an optional field. */
function unwrapOptional(schema) {
  const options = schema && schema.anyOf;
  if (!Array.isArray(options)) return schema || {};
  const real = options.find((option) => option && option.type !== "null");
  return real || schema || {};
}

/**
 * Turn one JSON-Schema property into a form field descriptor.
 *
 * @returns {object|null} null for anything with no sensible single input —
 *   an array or an object needs a bespoke widget, and guessing at one produces
 *   a control that silently sends the wrong shape.
 */
function fieldFor(name, rawSchema, required) {
  const schema = unwrapOptional(rawSchema);
  const type = schema.type;
  if (type === "array" || type === "object") return null;

  const field = {
    name,
    label: humanize(name),
    type: type === "integer" || type === "number" ? "number" : "text",
    required: Boolean(required),
  };
  if (schema.description) field.hint = schema.description;
  if (schema.minimum !== undefined) field.min = schema.minimum;
  if (schema.maximum !== undefined) field.max = schema.maximum;
  if (schema.default !== undefined) field.placeholder = String(schema.default);
  else if (name === "week") field.placeholder = "current week";
  return field;
}

/**
 * Derive `{in, fields}` for one endpoint from the OpenAPI document.
 *
 * @param {object} doc the parsed `/openapi.json`
 * @param {object} entry a catalog endpoint (`{path, method}`)
 * @returns {object|null} a form spec, or null when the path is not described
 */
export function formSpecFor(doc, entry) {
  const paths = (doc && doc.paths) || {};
  const operation = (paths[entry.path] || {})[String(entry.method || "GET").toLowerCase()];
  if (!operation) return null;

  const fields = [];

  for (const parameter of operation.parameters || []) {
    if (parameter.in !== "query" || HIDDEN_FIELDS.has(parameter.name)) continue;
    const field = fieldFor(parameter.name, parameter.schema, parameter.required);
    if (field) {
      if (parameter.description && !field.hint) field.hint = parameter.description;
      fields.push(field);
    }
  }

  const body = operation.requestBody;
  const bodySchema = body
    ? resolve(doc, ((body.content || {})["application/json"] || {}).schema)
    : null;
  if (bodySchema) {
    const required = new Set(bodySchema.required || []);
    for (const [name, property] of Object.entries(bodySchema.properties || {})) {
      if (HIDDEN_FIELDS.has(name)) continue;
      const field = fieldFor(name, property, required.has(name));
      if (field) fields.push(field);
    }
  }

  // A POST with no describable body would render an empty form and submit
  // nothing, which is worse than saying the build cannot draw it.
  if (bodySchema && fields.length === 0) return null;

  return { in: bodySchema ? "body" : "query", fields, derived: true };
}
