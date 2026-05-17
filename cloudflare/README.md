# Cloudflare Voice Control Stack

This folder hosts the Cloudflare replacement for dashboard + APIs for the Rapid-X voice service.

## What's inside

- `apps/be`: Hono + tRPC worker on Cloudflare Workers with D1 persistence.
- `apps/fe`: React dashboard using TanStack Router + tRPC client.
- `apps/be/migrations/0001_init.sql`: base schema.
- `apps/be/migrations/0002_saas_auth_templates.sql`: SaaS auth, onboarding, trial, templates, and usage tables.

## Local development

From this repository root:

- `cd cloudflare`
- `pnpm install`
- `pnpm --filter backend run db:migrate`
- `pnpm --filter backend dev`
- `pnpm --filter frontend dev`

Local D1 data is persisted under `cloudflare/data` so the backend keeps rows between restarts.

Backend expected at `http://localhost:4000`; frontend expects API at `VITE_API_BASE_URL` (or uses `http://localhost:4000` by default).
Wrangler reads backend secrets from `apps/be/.dev.vars` and `apps/be/.dev.vars.development`; both are ignored by git.
Use this format locally:

```ini
BASE_URL=http://localhost:4000
FRONTEND_URL=http://localhost:3000
BETTER_AUTH_URL=http://localhost:4000
BETTER_AUTH_SECRET=<32-byte-dev-secret>
PYTHON_DISPATCH_URL=https://call-agent.protechplanner.com/api/dispatch
GROQ_BASE_URL=https://gateway.ai.cloudflare.com/v1/<account_id>/<gateway_id>/compat
GROQ_MODEL=groq/llama-3.3-70b-versatile
CLOUDFLARE_AI_GATEWAY_TOKEN=<only-if-authenticated-gateway-is-on>
```

Optional provider keys (used when you switch model providers):

```ini
GROQ_API_KEY=<groq-key>
CLOUDFLARE_AI_GATEWAY_TOKEN=<cloudflare-ai-gateway-run-token>
DEEPGRAM_API_KEY=<deepgram-key>
SARVAM_API_KEY=<sarvam-key>
```

Use the same `GROQ_BASE_URL` value in the Python agent `.env` when routing
the live voice worker through Cloudflare AI Gateway or a Worker proxy. If
Authenticated Gateway is enabled, set `CLOUDFLARE_AI_GATEWAY_TOKEN` too; the
Groq API key remains the provider key.

## API endpoints

- `GET /api/health`
- `GET /api/calls`
- `GET /api/calls/:callSid`
- `GET /api/calls/:callSid/transcripts`
- `GET /api/calls/:callSid/metrics`
- `POST /api/voice/transcript`
- `POST /api/voice/transcripts`
- `POST /api/voice/metric`
- `POST /api/voice/metrics`
- `POST /api/voice/event`
- `POST /api/voice/events`
- `POST /api/dispatch/start-call`
- `POST /trpc/*` (dashboard procedures)

## Deployment notes

Create a real D1 database and update `apps/be/wrangler.jsonc` with your `database_id` values.

Local migrations:

- `pnpm --filter backend run db:migrate` (uses `--local --persist-to=../../data --env=development`).

Remote migrations:

- `pnpm --filter backend run db:migrate:remote` (after production DB is set).

Set production secrets with:

- `pnpm --filter backend exec wrangler secret put BETTER_AUTH_SECRET --env=production`
- `pnpm --filter backend exec wrangler secret put GROQ_API_KEY --env=production`
- `pnpm --filter backend exec wrangler secret put DEEPGRAM_API_KEY --env=production` (if using Deepgram)
- `pnpm --filter backend exec wrangler secret put SARVAM_API_KEY --env=production` (if using Sarvam)

Use `PYTHON_DISPATCH_URL` to point to your Python service endpoint.
