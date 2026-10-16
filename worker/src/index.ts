interface Env {}

export default {
  fetch(request: Request): Response {
    const url = new URL(request.url);

    if (request.method === "GET" && url.pathname === "/health") {
      return new Response(JSON.stringify({ status: "ok", service: "DenmarkTgBot" }), {
        status: 200,
        headers: { "Content-Type": "application/json; charset=utf-8" },
      });
    }

    return new Response(JSON.stringify({ error: "Not Found" }), {
      status: 404,
      headers: { "Content-Type": "application/json; charset=utf-8" },
    });
  },
} satisfies ExportedHandler<Env>;
