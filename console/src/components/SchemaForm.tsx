// Renders a form from a tool's JSON Schema, so any tool in the catalog can be called from the UI.

export type Json = Record<string, any>;

function resolveType(schema: Json): Json {
  // pydantic/OpenAPI optionals: anyOf: [{type: X}, {type: null}]
  const variants = schema.anyOf ?? schema.oneOf;
  if (Array.isArray(variants)) {
    const nonNull = variants.find((v: Json) => v.type !== "null");
    if (nonNull) return { ...nonNull, description: schema.description ?? nonNull.description, default: schema.default };
  }
  return schema;
}

export function coerceArgs(schema: Json, raw: Json): Json {
  const out: Json = {};
  const props: Json = schema?.properties ?? {};
  for (const [name, value] of Object.entries(raw)) {
    if (value === "" || value === undefined) continue;
    const s = resolveType(props[name] ?? {});
    if ((s.type === "number" || s.type === "integer") && typeof value === "string") {
      const n = Number(value);
      out[name] = Number.isNaN(n) ? value : n; // invalid input passes through → gateway validation shows the error
    } else if (s.type === "array" && typeof value === "string") {
      out[name] = value.split(",").map((v) => v.trim()).filter(Boolean);
    } else if ((s.type === "object" || (!s.type && typeof value === "string" && /^[[{]/.test(value))) && typeof value === "string") {
      try { out[name] = JSON.parse(value); } catch { out[name] = value; }
    } else {
      out[name] = value;
    }
  }
  return out;
}

export function SchemaForm({ schema, values, onChange }: {
  schema: Json; values: Json; onChange: (v: Json) => void;
}) {
  const props: Json = schema?.properties ?? {};
  const required: string[] = schema?.required ?? [];
  const names = Object.keys(props);
  if (!names.length) return <div className="muted">This tool takes no arguments.</div>;

  const set = (name: string, v: any) => onChange({ ...values, [name]: v });

  return (
    <div className="schema-form">
      {names.map((name) => {
        const s = resolveType(props[name]);
        const isReq = required.includes(name);
        const val = values[name];
        const id = `f-${name}`;
        let input;
        if (Array.isArray(s.enum)) {
          input = (
            <select id={id} value={val ?? ""} onChange={(e) => set(name, e.target.value)}>
              {!isReq && <option value="">—</option>}
              {s.enum.map((o: any) => <option key={String(o)} value={o}>{String(o)}</option>)}
            </select>
          );
        } else if (s.type === "boolean") {
          input = <input id={id} type="checkbox" checked={!!val} onChange={(e) => set(name, e.target.checked)} />;
        } else if (s.type === "number" || s.type === "integer") {
          input = <input id={id} type="text" inputMode="decimal" value={val ?? ""} placeholder={s.default != null ? String(s.default) : ""}
                         onChange={(e) => set(name, e.target.value)} />;
        } else if (s.type === "array") {
          input = <input id={id} type="text" value={Array.isArray(val) ? val.join(", ") : val ?? ""} placeholder="comma, separated"
                         onChange={(e) => set(name, e.target.value)} />;
        } else if (s.type === "object") {
          input = <textarea id={id} rows={3} value={typeof val === "string" ? val : JSON.stringify(val ?? {}, null, 2)}
                            onChange={(e) => set(name, e.target.value)} />;
        } else {
          input = <input id={id} type="text" value={val ?? ""} placeholder={s.default != null ? String(s.default) : ""}
                         onChange={(e) => set(name, e.target.value)} />;
        }
        return (
          <label key={name} className="field" htmlFor={id}>
            <span className="field-name">
              {name}{isReq && <span className="req">*</span>}
              <span className="field-type">{Array.isArray(s.enum) ? "enum" : s.type ?? "any"}</span>
            </span>
            {input}
            {s.description && <span className="field-help">{s.description}</span>}
          </label>
        );
      })}
    </div>
  );
}

// Sensible demo inputs so every tool is one click away from a real call.
const SF = { lat: 37.7749, lng: -122.4194 };
const SFO = { lat: 37.6213, lng: -122.379 };
export const EXAMPLES: Record<string, Json> = {
  rides__estimate_fare: { pickup_lat: SF.lat, pickup_lng: SF.lng, dropoff_lat: SFO.lat, dropoff_lng: SFO.lng, product: "economy" },
  rides__request_ride: { rider_id: "u1", pickup_lat: SF.lat, pickup_lng: SF.lng, dropoff_lat: SFO.lat, dropoff_lng: SFO.lng, product: "comfort" },
  rides__cancel_ride: { reason: "Plans changed" },
  rides__list_ride_history: { rider_id: "u1", limit: 5 },
  eats__search_restaurants: { query: "sushi" },
  eats__get_menu: { restaurant_id: "r1" },
  eats__place_order: { user_id: "u1", restaurant_id: "r1", item_ids: "m1, m2" },
  payments__list_payment_methods: { user_id: "u1" },
  payments__charge_payment: { user_id: "u1", amount_usd: 23.43, method_id: "pm_visa_4242", description: "Ride to SFO" },
  payments__refund_payment: { amount_usd: 5, reason: "Driver took a longer route" },
  support__create_ticket: { user_id: "u1", subject: "Lost item", body: "I left my phone in the car.", category: "ride" },
  support__escalate_ticket: { reason: "Customer is waiting at the airport" },
  maps__geocode: { address: "sfo airport" },
  maps__reverse_geocode: { lat: SF.lat, lng: SF.lng },
  maps__get_eta: { origin_lat: SF.lat, origin_lng: SF.lng, dest_lat: 37.7955, dest_lng: -122.3937, mode: "driving" },
  maps__get_route: { origin_lat: SF.lat, origin_lng: SF.lng, dest_lat: SFO.lat, dest_lng: SFO.lng, mode: "driving" },
};

/** Prefill: examples, then IDs remembered from earlier results (ride_id, charge_id, …). */
export function initialValues(toolName: string, schema: Json, memory: Json): Json {
  const values: Json = { ...(EXAMPLES[toolName] ?? {}) };
  for (const name of Object.keys(schema?.properties ?? {})) {
    if (values[name] === undefined && memory[name] !== undefined) values[name] = memory[name];
  }
  return values;
}

/** Collect *_id fields from a tool result so follow-up calls can reuse them. */
export function harvestIds(payload: any, memory: Json): Json {
  const next = { ...memory };
  const walk = (v: any, depth: number) => {
    if (depth > 3 || v == null || typeof v !== "object") return;
    if (Array.isArray(v)) { v.slice(0, 3).forEach((x) => walk(x, depth + 1)); return; }
    for (const [k, val] of Object.entries(v)) {
      if (k.endsWith("_id") && (typeof val === "string" || typeof val === "number")) next[k] = val;
      else if (typeof val === "object") walk(val, depth + 1);
    }
  };
  walk(payload, 0);
  return next;
}
