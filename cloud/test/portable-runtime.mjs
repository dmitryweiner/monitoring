// Contract tests on CPUs unsupported by workerd. Uses real SQLite and Web APIs;
// this adapter does not establish compatibility with Cloudflare's runtime/quotas.
import { DatabaseSync } from 'node:sqlite';
import { createHash } from 'node:crypto';
import worker from '../dist/test-worker.js';

export function portableRuntime(bindings) {
  const sqlite = new DatabaseSync(':memory:');
  class Statement {
    constructor(sql, args=[]) { this.sql=sql; this.args=args; }
    bind(...args) { return new Statement(this.sql,args); }
    execute() {
      const result=sqlite.prepare(this.sql).run(...this.args);
      return {success:true,meta:{changes:Number(result.changes)}};
    }
    async run() { return this.execute(); }
    async first(column) {
      const row=sqlite.prepare(this.sql).get(...this.args);
      return row ? (column ? row[column] : {...row}) : null;
    }
    async all() { return {success:true,results:sqlite.prepare(this.sql).all(...this.args).map(r=>({...r}))}; }
  }
  const DB={
    prepare:sql=>new Statement(sql),
    async batch(statements) {
      sqlite.exec('BEGIN');
      try { const result=statements.map(s=>s.execute());sqlite.exec('COMMIT');return result; }
      catch(error) { sqlite.exec('ROLLBACK');throw error; }
    }
  };
  const objects=new Map();
  const PHOTOS={
    async put(key,data,options={}) {
      const bytes=new Uint8Array(data);
      if(options.sha256 && createHash('sha256').update(bytes).digest('hex')!==options.sha256)
        throw new Error('checksum mismatch');
      objects.set(key,{key,bytes,uploaded:new Date(),customMetadata:options.customMetadata||{}});
      return this.head(key);
    },
    async head(key) {
      const object=objects.get(key);
      return object ? {...object,bytes:undefined,size:object.bytes.length} : null;
    },
    async get(key) {
      const object=objects.get(key);
      return object ? {...await this.head(key),body:object.bytes.slice()} : null;
    },
    async delete(key) { objects.delete(key); },
    async list({limit=1000,cursor}={}) {
      const keys=[...objects.keys()].sort().filter(k=>!cursor||k>cursor);
      const selected=keys.slice(0,limit);
      return {objects:await Promise.all(selected.map(k=>this.head(k))),
        truncated:keys.length>limit,cursor:selected.at(-1)};
    }
  };
  const limiter={async limit(){return {success:true};}};
  const env={...bindings,DB,PHOTOS,LOGIN_LIMIT:limiter,API_LIMIT:limiter};
  return {
    async getD1Database(){return DB;},async getR2Bucket(){return PHOTOS;},
    async dispatchFetch(url,options){return worker.fetch(new Request(url,options),env,{});},
    async dispose(){sqlite.close();}
  };
}
