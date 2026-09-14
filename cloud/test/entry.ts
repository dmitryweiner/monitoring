import worker, { cleanup } from '../src/index';
export default {
  ...worker,
  async fetch(req: Request, env: any) {
    const url = new URL(req.url);
    if (url.pathname === '/__test_cleanup') {
      await cleanup(env, Number(url.searchParams.get('now')));
      return new Response('ok');
    }
    return worker.fetch(req, env);
  }
};
