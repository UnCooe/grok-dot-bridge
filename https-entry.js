// Optional stable workers.dev HTTPS entry. The VPS owns all bridge state.
export default {
  async fetch(request, env) {
    const upstream = new URL(env.ORIGIN);
    const incoming = new URL(request.url);
    upstream.pathname = incoming.pathname;
    upstream.search = incoming.search;
    return fetch(new Request(upstream, request), {redirect: "manual"});
  }
};
