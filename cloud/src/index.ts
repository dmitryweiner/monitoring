interface Env {
  DB: D1Database;
  PHOTOS: R2Bucket;
  DEVICE_ID: string;
  DEVICE_HASH: string;
  ADMIN_HASH: string;
  ALLOWED_ORIGINS: string;
  LOGIN_LIMIT: RateLimit;
  API_LIMIT: RateLimit;
}
type Kind = "measurement" | "photo" | "audio";
type Media = Exclude<Kind, "measurement">;
interface Event {
  schema_version: 1; device_id: string; event_id: string; observed_at: number;
  kind: Kind; source: string; status: "ok" | "error";
  values: Record<string, number>; clock_synchronized: boolean;
}
interface Row {
  device: string; id: string; observed: number; received: number; kind: Kind;
  source: string; payload: string; fingerprint: string; object_key: string | null;
  bytes: number; state: "pending" | "ready" | "deleting";
}
const DAY = 86400;
const MAX_OBJECT = 4 * 1024 * 1024;
// Private R2 objects: each kind's upload path, content type and object suffix.
const MEDIA: Record<Media, { path: string; type: string; suffix: string }> = {
  photo: { path: "/v1/photos", type: "image/jpeg", suffix: "jpg" },
  audio: { path: "/v1/audio", type: "audio/ogg", suffix: "ogg" },
};
const encoder = new TextEncoder();
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const name = /^[a-zA-Z0-9_-]{1,64}$/;
const now = () => Date.now() / 1000;
class HttpError extends Error {
  constructor(public status: number, message: string) { super(message); }
}
function fail(status: number, message: string): never { throw new HttpError(status, message); }
function json(value: unknown, status = 200) { return Response.json(value, { status }); }
function canonical(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  if (value !== null && typeof value === "object") {
    const object = value as Record<string, unknown>;
    return `{${Object.keys(object).sort().map(k => `${JSON.stringify(k)}:${canonical(object[k])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}
async function hash(value: string | Uint8Array): Promise<string> {
  const data = typeof value === "string" ? encoder.encode(value) : value;
  const result = await crypto.subtle.digest("SHA-256", data);
  return Array.from(new Uint8Array(result), b => b.toString(16).padStart(2, "0")).join("");
}
function secureEqual(a: string, b: string) {
  if (a.length !== b.length) return false;
  let result = 0;
  for (let i = 0; i < a.length; ++i) result |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return result === 0;
}
function validate(raw: unknown, device: string, kind: Kind): Event {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) fail(422, "invalid event");
  const e = raw as Event;
  const fields = ["schema_version", "device_id", "event_id", "observed_at", "kind", "source", "status", "values", "clock_synchronized"];
  if (Object.keys(e).some(k => !fields.includes(k)) || e.schema_version !== 1 || e.device_id !== device ||
      typeof e.event_id !== "string" || !uuid.test(e.event_id) || e.kind !== kind ||
      typeof e.source !== "string" || !name.test(e.source) || !["ok", "error"].includes(e.status) ||
      typeof e.observed_at !== "number" || !Number.isFinite(e.observed_at) || e.observed_at < 0 ||
      typeof e.clock_synchronized !== "boolean" || !e.values || typeof e.values !== "object" || Array.isArray(e.values))
    fail(422, "invalid event");
  const entries = Object.entries(e.values);
  if (entries.length > 32 || entries.some(([k,v]) => !/^[a-zA-Z0-9_]{1,64}$/.test(k) || typeof v !== "number" || !Number.isFinite(v)))
    fail(422, "invalid metric");
  return { ...e, event_id: e.event_id.toLowerCase() };
}
async function body(req: Request, max: number): Promise<Uint8Array> {
  if (Number(req.headers.get("Content-Length")) > max) fail(413, "body too large");
  if (!req.body) return new Uint8Array();
  const reader = req.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    size += value.length;
    if (size > max) { await reader.cancel(); fail(413, "body too large"); }
    chunks.push(value);
  }
  const result = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) { result.set(chunk, offset); offset += chunk.length; }
  return result;
}
async function readJSON(req: Request): Promise<any> {
  const bytes = await body(req, 256 * 1024);
  try { return JSON.parse(new TextDecoder().decode(bytes)); }
  catch { return fail(400, "invalid JSON"); }
}
function bearer(req: Request) { return /^Bearer ([^\s]+)$/i.exec(req.headers.get("Authorization") || "")?.[1]; }
async function deviceAuth(req: Request, env: Env) {
  if (!secureEqual(await hash(bearer(req) || ""), env.DEVICE_HASH)) fail(401, "invalid device credential");
}
function origins(env: Env) { return (env.ALLOWED_ORIGINS || "").split(/\s+/).filter(Boolean); }
async function sessionAuth(req: Request, env: Env) {
  const header = bearer(req);
  const cookie = /(?:^|;\s*)monitor_session=([^;]+)/.exec(req.headers.get("Cookie") || "")?.[1];
  const tokenHash = await hash(header || cookie || "");
  const row = await env.DB.prepare("SELECT expires,key_hash FROM sessions WHERE hash=?").bind(tokenHash)
    .first<{expires: number; key_hash: string}>();
  if (!row || row.expires <= now() || !secureEqual(row.key_hash, env.ADMIN_HASH)) fail(401, "invalid or expired session");
  if (!header && !["GET", "HEAD"].includes(req.method) &&
      (!origins(env).includes(req.headers.get("Origin") || "") || req.headers.get("X-CSRF-Token") !== "1"))
    fail(403, "CSRF check failed");
  return tokenHash;
}
async function getRow(env: Env, id: string) {
  return env.DB.prepare("SELECT * FROM events WHERE device=? AND id=?").bind(env.DEVICE_ID, id).first<Row>();
}
async function seen(env: Env) {
  await env.DB.prepare("INSERT INTO devices VALUES (?,?) ON CONFLICT(id) DO UPDATE SET last_seen=excluded.last_seen")
    .bind(env.DEVICE_ID, now()).run();
}
function retention(kind: Kind) { return kind === "measurement" ? 90 : 30; }
function ageStatus(e: Event) {
  if (e.observed_at > now() + 300) return "invalid_time";
  if (e.observed_at < now() - retention(e.kind) * DAY) return "expired";
  return null;
}
async function reserve(env: Env, e: Event, fingerprint: string, bytes: number, key: string | null) {
  try {
    await env.DB.prepare(`INSERT OR IGNORE INTO events
      (device,id,observed,received,kind,source,payload,fingerprint,object_key,bytes,state)
      VALUES (?,?,?,?,?,?,?,?,?,?,?)`).bind(e.device_id, e.event_id, e.observed_at, now(), e.kind,
      e.source, canonical(e), fingerprint, key, bytes, e.kind === "measurement" ? "ready" : "pending").run();
  } catch (error) {
    if (String(error).includes("monitor_quota")) fail(429, "storage or daily upload quota exceeded");
    throw error;
  }
  const row = await getRow(env, e.event_id);
  if (!row) fail(503, "reservation unavailable");
  return row;
}
async function putMeasurement(env: Env, e: Event) {
  const age = ageStatus(e);
  if (age) return age;
  const fingerprint = await hash(canonical(e));
  const existing = await getRow(env, e.event_id);
  if (existing) return existing.fingerprint === fingerprint ? "duplicate" : "conflict";
  const row = await reserve(env, e, fingerprint, 0, null);
  return row.fingerprint === fingerprint ? "stored" : "conflict";
}
async function putObject(env: Env, e: Event, bytes: Uint8Array) {
  const age = ageStatus(e);
  if (age) return age;
  const contentHash = await hash(bytes);
  const fingerprint = await hash(canonical(e) + ":" + contentHash);
  const media = MEDIA[e.kind as Media];
  const key = `${e.device_id}/${e.event_id}.${media.suffix}`;
  // Reserve the immutable fingerprint in D1 BEFORE writing R2. Concurrent conflicting
  // uploads can never overwrite the accepted object's contents.
  const row = await reserve(env, e, fingerprint, bytes.length, key);
  if (row.fingerprint !== fingerprint) return "conflict";
  if (row.state === "deleting") fail(503, "object is being reconciled; retry");
  const stored = await env.PHOTOS.head(key);
  if (!stored || stored.customMetadata?.fingerprint !== fingerprint) {
    await env.PHOTOS.put(key, bytes, { httpMetadata: {contentType: media.type},
      customMetadata: { fingerprint, observed_at: String(e.observed_at) }, sha256: contentHash });
  }
  const update = await env.DB.prepare("UPDATE events SET state='ready' WHERE device=? AND id=? AND fingerprint=? AND state<>'deleting'")
    .bind(e.device_id, e.event_id, fingerprint).run();
  if (!update.meta.changes) fail(503, "object metadata changed; retry");
  return row.state === "ready" ? "duplicate" : "stored";
}
function parseNumber(url: URL, key: string, fallback: number) {
  const text = url.searchParams.get(key);
  const value = text === null ? fallback : Number(text);
  if (!Number.isFinite(value)) fail(422, "invalid numeric parameter");
  return value;
}
function timeRange(url: URL, kind: Kind) {
  const start = Math.max(parseNumber(url,"start",0), now()-retention(kind)*DAY);
  const end = parseNumber(url,"end",now()+300);
  if (end < start || end-start > 91*DAY) fail(422,"invalid time range");
  return { start, end };
}
async function page(env: Env, url: URL, kind: Kind) {
  const {start,end} = timeRange(url, kind);
  const limit = parseNumber(url, "limit", 100);
  if (!Number.isInteger(limit) || limit < 1 || limit > 1000) fail(422, "invalid limit");
  let sql = "SELECT * FROM events WHERE device=? AND kind=? AND state='ready' AND observed>=? AND observed<=?";
  const args: (string | number)[] = [env.DEVICE_ID,kind,start,end];
  const source = url.searchParams.get("source");
  if (source) { if (!name.test(source)) fail(422,"invalid source"); sql += " AND source=?"; args.push(source); }
  const cursor = url.searchParams.get("cursor");
  if (cursor) {
    try {
      if (cursor.length > 256) throw new Error();
      const [timestamp, id] = JSON.parse(atob(cursor.replace(/-/g,"+").replace(/_/g,"/")));
      if (!Number.isFinite(timestamp) || typeof id !== "string" || !uuid.test(id)) throw new Error();
      sql += " AND (observed,id)>(?,?)"; args.push(timestamp,id);
    } catch { fail(422,"invalid cursor"); }
  }
  const {results} = await env.DB.prepare(sql + " ORDER BY observed,id LIMIT ?").bind(...args,limit+1).all<Row>();
  const items = results.slice(0,limit).map(row => ({...JSON.parse(row.payload), received_at: row.received}));
  const last = results[limit-1];
  return json({ items, next_cursor: results.length > limit ? btoa(JSON.stringify([last.observed,last.id])) : null });
}
async function aggregate(env: Env, url: URL) {
  const { start, end } = timeRange(url,"measurement");
  const source = url.searchParams.get("source") || "";
  const metric = url.searchParams.get("metric") || "";
  const bucket = parseNumber(url,"bucket_seconds",3600);
  if (!name.test(source) || !/^[a-zA-Z0-9_]{1,64}$/.test(metric) || !Number.isInteger(bucket) ||
      bucket < 600 || bucket > DAY || (end-start)/bucket > 1000) fail(422,"invalid aggregation");
  const path = "$.values."+metric;
  const {results} = await env.DB.prepare(`SELECT CAST(observed/? AS INTEGER)*? AS bucket,
    COUNT(*) AS count,AVG(json_extract(payload,?)) AS mean,MIN(json_extract(payload,?)) AS minimum,
    MAX(json_extract(payload,?)) AS maximum FROM events WHERE device=? AND kind='measurement'
    AND state='ready' AND source=? AND observed>=? AND observed<? AND json_extract(payload,?) IS NOT NULL
    GROUP BY bucket ORDER BY bucket`).bind(bucket,bucket,path,path,path,env.DEVICE_ID,source,start,end,path).all();
  return json({source,metric,bucket_seconds:bucket,items:results});
}
async function route(req: Request, env: Env): Promise<Response> {
  const url = new URL(req.url), path = url.pathname;
  if (req.method === "OPTIONS") {
    if (!origins(env).includes(req.headers.get("Origin") || "")) fail(403,"origin not allowed");
    return new Response(null,{status:204});
  }
  const ip = req.headers.get("CF-Connecting-IP") || "local";
  const limiter = path === "/v1/session" && req.method === "POST" ? env.LOGIN_LIMIT : env.API_LIMIT;
  if (!(await limiter.limit({key:ip})).success) fail(429,"rate limited");
  if (path === "/healthz") {
    await env.DB.prepare("SELECT 1").first();
    return json({status:"ok"});
  }
  if (!/^[a-f0-9]{64}$/.test(env.DEVICE_HASH || "") || !/^[a-f0-9]{64}$/.test(env.ADMIN_HASH || ""))
    fail(503,"credentials not configured");
  if (path === "/v1/session" && req.method === "POST") {
    if (req.headers.has("Origin") && !origins(env).includes(req.headers.get("Origin")!)) fail(403,"origin not allowed");
    const payload = await readJSON(req);
    if (typeof payload?.key !== "string" || payload.key.length < 32 || payload.key.length > 256 ||
        !secureEqual(await hash(payload.key),env.ADMIN_HASH)) fail(401,"invalid key");
    const token = Array.from(crypto.getRandomValues(new Uint8Array(32)),b=>b.toString(16).padStart(2,"0")).join("");
    const expires = now()+7*DAY;
    await env.DB.batch([
      env.DB.prepare("DELETE FROM sessions WHERE expires<=? OR key_hash<>?").bind(now(),env.ADMIN_HASH),
      env.DB.prepare("DELETE FROM sessions WHERE hash IN (SELECT hash FROM sessions ORDER BY expires DESC LIMIT -1 OFFSET 63)"),
      env.DB.prepare("INSERT INTO sessions VALUES (?,?,?)").bind(await hash(token),expires,env.ADMIN_HASH)
    ]);
    const response = json({session_key:token,expires_at:expires});
    response.headers.set("Set-Cookie",`monitor_session=${token}; Path=/; Max-Age=${7*DAY}; Secure; HttpOnly; SameSite=Strict`);
    return response;
  }
  const upload = (Object.keys(MEDIA) as Media[]).find(kind => MEDIA[kind].path === path);
  if (req.method === "POST" && (path === "/v1/measurements" || upload)) {
    await deviceAuth(req,env);
    if (!upload) {
      const payload = await readJSON(req);
      if (!Array.isArray(payload?.events) || payload.events.length < 1 || payload.events.length > 12) fail(422,"invalid batch");
      const results = [];
      // Twelve items leave headroom within Workers Free's subrequest budget.
      for (const raw of payload.events) {
        let status: string;
        try { status = await putMeasurement(env,validate(raw,env.DEVICE_ID,"measurement")); }
        catch (error) { if (error instanceof HttpError && error.status === 422) status="invalid"; else throw error; }
        results.push({event_id:String(raw?.event_id || "").slice(0,64),status});
      }
      await seen(env);
      return json({results});
    }
    const metadata = req.headers.get("X-Event") || "";
    if (metadata.length > 8192 || req.headers.get("Content-Type") !== MEDIA[upload].type) fail(400,`invalid ${upload} headers`);
    let raw: unknown;
    try {raw=JSON.parse(metadata);} catch {fail(422,"invalid metadata");}
    const event = validate(raw,env.DEVICE_ID,upload);
    if (event.status !== "ok") fail(422,`invalid ${upload} status`);
    const bytes = await body(req,MAX_OBJECT);
    // Keep media processing off the 10 ms CPU-budget Worker. Check framing only;
    // the agent/ffmpeg produces the actual content, and output is never served as HTML.
    if (upload === "photo" && (bytes.length < 4 || bytes[0]!==255 || bytes[1]!==216 || bytes.at(-2)!==255 || bytes.at(-1)!==217))
      fail(422,"invalid JPEG framing");
    if (upload === "audio" && (bytes.length < 28 || new TextDecoder().decode(bytes.subarray(0,4)) !== "OggS"))
      fail(422,"invalid Ogg framing");
    const status = await putObject(env,event,bytes);
    await seen(env);
    return json({event_id:event.event_id,status});
  }
  const session = await sessionAuth(req,env);
  if (path === "/v1/session" && req.method === "DELETE") {
    await env.DB.prepare("DELETE FROM sessions WHERE hash=?").bind(session).run();
    return new Response(null,{status:204,headers:{"Set-Cookie":"monitor_session=; Path=/; Max-Age=0; Secure; HttpOnly; SameSite=Strict"}});
  }
  if (req.method !== "GET") fail(405,"method not allowed");
  if (path === "/v1/measurements") return page(env,url,"measurement");
  if (path === "/v1/photos") return page(env,url,"photo");
  if (path === "/v1/audio") return page(env,url,"audio");
  if (path === "/v1/measurements/aggregate") return aggregate(env,url);
  if (path === "/v1/latest") {
    const {results} = await env.DB.prepare(`SELECT e.* FROM latest l JOIN events e
      ON e.device=l.device AND e.id=l.id WHERE l.device=? AND e.state='ready'
      AND ((e.kind IN ('photo','audio') AND e.observed>=?) OR (e.kind='measurement' AND e.observed>=?))`)
      .bind(env.DEVICE_ID,now()-30*DAY,now()-90*DAY).all<Row>();
    const seen = await env.DB.prepare("SELECT last_seen FROM devices WHERE id=?").bind(env.DEVICE_ID).first<{last_seen:number}>();
    return json({device_id:env.DEVICE_ID,last_seen:seen?.last_seen || null,
      items:results.map(row=>({...JSON.parse(row.payload),received_at:row.received}))});
  }
  for (const kind of Object.keys(MEDIA) as Media[]) {
    if (!path.startsWith(MEDIA[kind].path + "/")) continue;
    const id = path.slice(MEDIA[kind].path.length + 1);
    if (!uuid.test(id)) fail(404,`${kind} not found`);
    const row = await getRow(env,id.toLowerCase());
    if (!row || row.kind!==kind || row.state!=="ready" || row.observed < now()-30*DAY) fail(404,`${kind} not found`);
    const object = await env.PHOTOS.get(row.object_key!);
    if (!object) fail(503,`${kind} temporarily unavailable`);
    return new Response(object.body,{headers:{"Content-Type":MEDIA[kind].type}});
  }
  fail(404,"not found");
}

export async function cleanup(env: Env, timestamp=now()) {
  // Index-bounded chunks: one scheduled invocation never processes the entire archive.
  await env.DB.batch([
    env.DB.prepare("DELETE FROM events WHERE rowid IN (SELECT rowid FROM events WHERE kind='measurement' AND observed<? LIMIT 500)").bind(timestamp-90*DAY),
    env.DB.prepare("DELETE FROM sessions WHERE expires<=?").bind(timestamp),
    env.DB.prepare("DELETE FROM daily WHERE day<?").bind(Math.floor(timestamp/DAY)-2)
  ]);
  const {results} = await env.DB.prepare(`SELECT * FROM events WHERE kind IN ('photo','audio') AND
    (observed<? OR (state='pending' AND received<?) OR state='deleting') LIMIT 6`)
    .bind(timestamp-30*DAY,timestamp-DAY).all<Row>();
  for (const row of results) {
    await env.DB.prepare("UPDATE events SET state='deleting' WHERE device=? AND id=?").bind(row.device,row.id).run();
    await env.PHOTOS.delete(row.object_key!);
    await env.DB.prepare("DELETE FROM events WHERE device=? AND id=? AND state='deleting'").bind(row.device,row.id).run();
  }
  // Recover the rare object written while an old pending row was being removed.
  const state = await env.DB.prepare("SELECT cursor FROM maintenance WHERE id=1").first<{cursor:string|null}>();
  const objects = await env.PHOTOS.list({limit:10,cursor:state?.cursor || undefined});
  for (const object of objects.objects) {
    if (object.uploaded.getTime()/1000 > timestamp-DAY) continue;
    const exists = await env.DB.prepare("SELECT 1 FROM events WHERE object_key=?").bind(object.key).first();
    if (!exists) await env.PHOTOS.delete(object.key);
  }
  await env.DB.prepare("UPDATE maintenance SET cursor=? WHERE id=1").bind(objects.truncated?objects.cursor:null).run();
}
export default {
  async fetch(req: Request, env: Env): Promise<Response> {
    let response: Response;
    try { response=await route(req,env); }
    catch (error) {
      response=error instanceof HttpError ? json({detail:error.message},error.status) : json({detail:"temporarily unavailable"},503);
      // Never log request headers, session keys, configuration, photographs or audio.
    }
    response.headers.set("Cache-Control","no-store");
    response.headers.set("X-Content-Type-Options","nosniff");
    response.headers.set("Vary","Origin");
    if (response.status===429) response.headers.set("Retry-After","60");
    const origin=req.headers.get("Origin");
    if (origin && origins(env).includes(origin)) {
      response.headers.set("Access-Control-Allow-Origin",origin);
      response.headers.set("Access-Control-Allow-Credentials","true");
      response.headers.set("Access-Control-Allow-Methods","GET, POST, DELETE");
      response.headers.set("Access-Control-Allow-Headers","Authorization, Content-Type, X-CSRF-Token");
    }
    return response;
  },
  async scheduled(_event: ScheduledController, env: Env, ctx: ExecutionContext) {
    ctx.waitUntil(cleanup(env));
  }
} satisfies ExportedHandler<Env>;
