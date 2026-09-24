import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { readFile, readdir } from 'node:fs/promises';
import { createHash, randomUUID } from 'node:crypto';

const digest = s => createHash('sha256').update(s).digest('hex');
const day = 86400;
const device = 'd'.repeat(40), admin = 'a'.repeat(40);
const headers = {Authorization:'Bearer '+device};
let mf, db, bucket;
const event = (kind='measurement', extra={}) => ({schema_version:1,device_id:'home',
  event_id:randomUUID(),observed_at:Date.now()/1000,kind,source:kind==='photo'?'camera':'cpu',
  status:'ok',values:kind==='photo'?{}:{cpu_temperature_c:35.9},clock_synchronized:true,...extra});
const photo = new Uint8Array([255,216,255,224,0,2,255,217]); // framing fixture, no decoder in Worker
async function request(path, options={}) {
  const res = await mf.dispatchFetch('https://monitor.test'+path,options);
  return res;
}
async function upload(e) {
  return request('/v1/measurements',{method:'POST',headers:{...headers,'Content-Type':'application/json'},
    body:JSON.stringify({events:Array.isArray(e)?e:[e]})});
}
async function uploadPhoto(e, bytes=photo) {
  return request('/v1/photos',{method:'POST',headers:{...headers,'Content-Type':'image/jpeg','X-Event':JSON.stringify(e)},body:bytes});
}
const clip = new Uint8Array([...new TextEncoder().encode('OggS'),...new Array(40).fill(0)]); // framing fixture
async function uploadAudio(e, bytes=clip, type='audio/ogg') {
  return request('/v1/audio',{method:'POST',headers:{...headers,'Content-Type':type,'X-Event':JSON.stringify(e)},body:bytes});
}
async function login() {
  const res = await request('/v1/session',{method:'POST',body:JSON.stringify({key:admin})});
  assert.equal(res.status,200,await res.clone().text());
  return {Authorization:'Bearer '+(await res.json()).session_key};
}
before(async()=>{
  // Maintenance trigger exists only in the test bundle, never in production.
  const script = await readFile('dist/test-worker.js','utf8');
  const bindings={DEVICE_ID:'home',DEVICE_HASH:digest(device),ADMIN_HASH:digest(admin),ALLOWED_ORIGINS:'https://ui.test'};
  if(process.env.MONITOR_TEST_RUNTIME==='node') {
    const {portableRuntime}=await import('./portable-runtime.mjs');
    mf=portableRuntime(bindings);
  } else {
  const {Miniflare,convertV4MiniflareOptions}=await import('miniflare');
  mf = new Miniflare(convertV4MiniflareOptions({modules:true,script,compatibilityDate:'2026-09-01',bindings,
    d1Databases:['DB'],r2Buckets:['PHOTOS'],
    ratelimits:{LOGIN_LIMIT:{namespace_id:'1',simple:{limit:1000,period:60}},
      API_LIMIT:{namespace_id:'2',simple:{limit:10000,period:60}}}}));
  }
  db = await mf.getD1Database('DB'); bucket = await mf.getR2Bucket('PHOTOS');
  for(const file of (await readdir('migrations')).filter(f=>f.endsWith('.sql')).sort()) {
    const sql = await readFile('migrations/'+file,'utf8');
    for(const statement of sql.split('-- statement-breakpoint')) await db.prepare(statement.trim()).run();
  }
});
after(async()=>{await mf?.dispose();});

test('device token cannot read, session cannot write, logout revokes',async()=>{
  assert.equal((await request('/v1/latest')).status,401);
  assert.equal((await request('/v1/latest',{headers})).status,401);
  const session=await login();
  assert.equal((await request('/v1/latest',{headers:session})).status,200);
  assert.equal((await request('/v1/measurements',{method:'POST',headers:session,body:'{"events":[]}'})).status,401);
  assert.equal((await request('/v1/session',{method:'DELETE',headers:session})).status,204);
  assert.equal((await request('/v1/latest',{headers:session})).status,401);
});
test('partial batches, repeated acknowledgements, immutable IDs',async()=>{
  const e=event();
  const first=await upload([e,{event_id:'invalid'}]);
  assert.equal(first.status,200,await first.clone().text());
  assert.deepEqual((await first.json()).results.map(x=>x.status),['stored','invalid']);
  assert.equal((await (await upload(e)).json()).results[0].status,'duplicate');
  assert.equal((await (await upload({...e,values:{cpu_temperature_c:99}})).json()).results[0].status,'conflict');
});
test('photo is private and concurrent different contents cannot overwrite it',async()=>{
  const e=event('photo');
  const responses=await Promise.all([uploadPhoto(e),uploadPhoto(e,new Uint8Array([255,216,1,2,255,217]))]);
  const statuses=await Promise.all(responses.map(r=>r.json()));
  assert.equal(statuses.filter(x=>x.status==='conflict').length,1);
  assert.equal(statuses.filter(x=>x.status==='stored').length,1);
  assert.equal((await request('/v1/photos/'+e.event_id)).status,401);
  const session=await login();
  const row=await db.prepare('SELECT * FROM events WHERE id=?').bind(e.event_id).first();
  const object=await bucket.get(row.object_key);
  assert.equal(object.customMetadata.fingerprint,row.fingerprint);
  assert.equal((await request('/v1/photos/'+e.event_id,{headers:session})).status,200);
});
test('retry completes R2-written / D1-pending operation',async()=>{
  const e=event('photo');
  assert.equal((await (await uploadPhoto(e)).json()).status,'stored');
  await db.prepare("UPDATE events SET state='pending' WHERE id=?").bind(e.event_id).run();
  assert.equal((await (await uploadPhoto(e)).json()).status,'stored');
  const row=await db.prepare('SELECT * FROM events WHERE id=?').bind(e.event_id).first();
  await bucket.delete(row.object_key);
  assert.equal((await (await uploadPhoto(e)).json()).status,'duplicate');
  assert.ok(await bucket.head(row.object_key));
});
test('bounds, stale/future samples and cookie CSRF',async()=>{
  const result=await (await upload([event('measurement',{observed_at:Date.now()/1000-91*day}),
    event('measurement',{observed_at:Date.now()/1000+1000})])).json();
  assert.deepEqual(result.results.map(x=>x.status),['expired','invalid_time']);
  assert.equal((await uploadPhoto(event('photo'),new Uint8Array(4*1024*1024+1))).status,413);
  assert.equal((await uploadPhoto(event('photo'),new Uint8Array([1,2]))).status,422);
  const session=await login(); const cookie='monitor_session='+session.Authorization.slice(7);
  assert.equal((await request('/v1/session',{method:'DELETE',headers:{Cookie:cookie}})).status,403);
  assert.equal((await request('/v1/session',{method:'DELETE',headers:{Cookie:cookie,Origin:'https://ui.test','X-CSRF-Token':'1'}})).status,204);
});
test('pagination and aggregation use capture time',async()=>{
  const timestamp=Math.floor(Date.now()/3600000)*3600-3600;
  const e=Array.from({length:5},(_,i)=>event('measurement',{source:'test',observed_at:timestamp+i*100,values:{temperature_c:i*10}}));
  await upload(e.toReversed());
  const session=await login();let cursor='',found=[];
  do {
    const response=await request('/v1/measurements?source=test&limit=2'+(cursor?'&cursor='+encodeURIComponent(cursor):''),{headers:session});
    const page=await response.json(); found.push(...page.items);cursor=page.next_cursor;
  } while(cursor);
  assert.deepEqual(found.map(x=>x.event_id),e.map(x=>x.event_id));
  const result=await (await request(`/v1/measurements/aggregate?source=test&metric=temperature_c&start=${timestamp}&end=${timestamp+3600}`,{headers:session})).json();
  assert.equal(result.items[0].mean,20);
});
test('persistent quotas do not charge retries twice',async()=>{
  const e=event('photo');await uploadPhoto(e);
  const before=await db.prepare('SELECT photo_bytes FROM usage WHERE id=1').first();
  await uploadPhoto(e);
  assert.deepEqual(await db.prepare('SELECT photo_bytes FROM usage WHERE id=1').first(),before);
  await db.prepare("INSERT INTO daily VALUES (?,'photo',1600) ON CONFLICT(day,kind) DO UPDATE SET count=1600")
    .bind(Math.floor(Date.now()/1000/day)).run();
  assert.equal((await uploadPhoto(event('photo'))).status,429);
  assert.equal((await uploadPhoto(e)).status,200);
  await db.prepare("UPDATE daily SET count=1 WHERE kind='photo'").run();
});

test('scheduled retention and orphan recovery are restartable',async()=>{
  const e=event('photo'); await uploadPhoto(e);
  await db.prepare('UPDATE events SET observed=? WHERE id=?').bind(Date.now()/1000-31*day,e.event_id).run();
  const m=event(); await upload(m);
  await db.prepare('UPDATE events SET observed=? WHERE id=?').bind(Date.now()/1000-91*day,m.event_id).run();
  await bucket.put('orphan.jpg',photo);
  const future=Date.now()/1000+2*day;
  assert.equal((await request('/__test_cleanup?now='+future)).status,200);
  assert.equal((await request('/__test_cleanup?now='+future)).status,200);
  assert.equal(await db.prepare('SELECT * FROM events WHERE id=?').bind(e.event_id).first(),null);
  assert.equal(await db.prepare('SELECT * FROM events WHERE id=?').bind(m.event_id).first(),null);
  assert.equal(await bucket.head('home/'+e.event_id+'.jpg'),null);
  assert.equal(await bucket.head('orphan.jpg'),null);
});

test('measurement daily and photo byte quotas preserve retries and recover',async()=>{
  const m=event();
  assert.equal((await (await upload(m)).json()).results[0].status,'stored');
  const today=Math.floor(Date.now()/1000/day);
  const daily=await db.prepare("SELECT count FROM daily WHERE day=? AND kind='measurement'").bind(today).first();
  const usage=await db.prepare('SELECT photo_bytes FROM usage WHERE id=1').first();
  try {
    await db.prepare("UPDATE daily SET count=4096 WHERE day=? AND kind='measurement'").bind(today).run();
    const rejected=event();
    assert.equal((await upload(rejected)).status,429);
    assert.equal(await db.prepare('SELECT id FROM events WHERE id=?').bind(rejected.event_id).first(),null);
    assert.equal((await (await upload(m)).json()).results[0].status,'duplicate');
    await db.prepare('UPDATE usage SET photo_bytes=6000000000 WHERE id=1').run();
    const p=event('photo');
    assert.equal((await uploadPhoto(p)).status,429);
    assert.equal(await bucket.head('home/'+p.event_id+'.jpg'),null);
    assert.equal(await db.prepare('SELECT id FROM events WHERE id=?').bind(p.event_id).first(),null);
  } finally {
    await db.prepare("UPDATE daily SET count=? WHERE day=? AND kind='measurement'").bind(daily.count,today).run();
    await db.prepare('UPDATE usage SET photo_bytes=? WHERE id=1').bind(usage.photo_bytes).run();
  }
  assert.equal((await (await upload(event())).json()).results[0].status,'stored');
  assert.equal((await (await uploadPhoto(event('photo'))).json()).status,'stored');
});

test('audio clips are private, typed and share the object quota',async()=>{
  const e=event('audio',{source:'microphone',values:{duration_seconds:60,peak_dbfs:-12.5}});
  assert.equal((await uploadAudio(event('photo'))).status,422);
  assert.equal((await uploadAudio(e,clip,'image/jpeg')).status,400);
  assert.equal((await uploadAudio(e,photo)).status,422);
  assert.equal((await uploadPhoto(e)).status,422);
  const before=(await db.prepare('SELECT photo_bytes FROM usage WHERE id=1').first()).photo_bytes;
  const stored=await uploadAudio(e);
  assert.equal(stored.status,200,await stored.clone().text());
  assert.deepEqual(await stored.json(),{event_id:e.event_id,status:'stored'});
  assert.equal((await (await uploadAudio(e)).json()).status,'duplicate');
  assert.equal((await db.prepare('SELECT photo_bytes FROM usage WHERE id=1').first()).photo_bytes,before+clip.length);
  assert.equal((await request('/v1/audio/'+e.event_id)).status,401);
  const session=await login();
  const res=await request('/v1/audio/'+e.event_id,{headers:session});
  assert.equal(res.status,200);
  assert.equal(res.headers.get('Content-Type'),'audio/ogg');
  assert.deepEqual(new Uint8Array(await res.arrayBuffer()),clip);
  assert.equal((await request('/v1/photos/'+e.event_id,{headers:session})).status,404);
  const list=await (await request('/v1/audio',{headers:session})).json();
  assert.deepEqual(list.items.map(x=>x.event_id),[e.event_id]);
  assert.equal(list.items[0].values.peak_dbfs,-12.5);
  const latest=await (await request('/v1/latest',{headers:session})).json();
  assert.ok(latest.items.some(x=>x.kind==='audio' && x.event_id===e.event_id));
  assert.ok(await bucket.head('home/'+e.event_id+'.ogg'));
  await db.prepare("INSERT INTO daily VALUES (?,'audio',1600) ON CONFLICT(day,kind) DO UPDATE SET count=1600")
    .bind(Math.floor(Date.now()/1000/day)).run();
  assert.equal((await uploadAudio(event('audio',{source:'microphone'}))).status,429);
  await db.prepare("UPDATE daily SET count=1 WHERE kind='audio'").run();
  await db.prepare('UPDATE events SET observed=? WHERE id=?').bind(Date.now()/1000-31*day,e.event_id).run();
  assert.equal((await request('/__test_cleanup?now='+(Date.now()/1000+2*day))).status,200);
  assert.equal(await db.prepare('SELECT * FROM events WHERE id=?').bind(e.event_id).first(),null);
  assert.equal(await bucket.head('home/'+e.event_id+'.ogg'),null);
});
