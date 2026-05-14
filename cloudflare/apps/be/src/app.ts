import { and, desc, eq, isNull } from 'drizzle-orm';
import { cors } from 'hono/cors';
import { z } from 'zod';
import { trpcServer } from '@hono/trpc-server';
import {
    calls,
    leads,
    metrics,
    organizations,
    templates,
    transcripts,
    user as authUsers,
} from './db/schema';
import { createAuth } from './auth';
import { createApp } from './app.utils';
import { createContext, createTrpcContext } from './context';
import { appRouter } from './trpc/app';
import { sql } from 'drizzle-orm';

const app = createApp();

app.use(
    '*',
    cors({
        origin: ['http://localhost:3000', 'http://localhost:3001', '*'],
        allowMethods: ['POST', 'GET', 'OPTIONS', 'PUT', 'DELETE', 'PATCH'],
        allowHeaders: ['Content-Type', 'Authorization', 'X-Requested-With'],
        exposeHeaders: ['Content-Length', 'Content-Type'],
        credentials: true,
        maxAge: 3600,
    }),
);

app.use(async (c, next) => {
    const context = await createContext({ env: c.env, request: c.req.raw });
    c.set('context', context);
    await next();
});

app.on(['GET', 'POST'], '/api/auth/*', (c) => {
    const context = c.get('context');
    const auth = createAuth(context.db.appDB, c.env, c.req.raw);
    return auth.handler(c.req.raw);
});

app.get('/api/health', (c) => c.json({ status: 'ok', service: 'rapid-x-api' }));

app.get('/api/calls', async (c) => {
    const context = c.get('context');
    if (!context.auth?.user) {
        return c.json({ error: 'Unauthorized' }, 401);
    }
    const limit = Number(c.req.query('limit') || '50');
    const offset = Number(c.req.query('offset') || '0');
    const callRecords = await context.db.appDB
        .select()
        .from(calls)
        .where(eq(calls.organizationId, context.auth.user.organizationId))
        .orderBy(desc(calls.createdAt))
        .limit(Math.min(200, Number.isInteger(limit) ? limit : 50))
        .offset(Math.max(0, Number.isInteger(offset) ? offset : 0));
    return c.json(callRecords);
});

app.get('/api/calls/:callSid', async (c) => {
    const context = c.get('context');
    if (!context.auth?.user) {
        return c.json({ error: 'Unauthorized' }, 401);
    }
    const callSid = c.req.param('callSid');
    const [record] = await context.db.appDB
        .select()
        .from(calls)
        .where(and(eq(calls.callSid, callSid), eq(calls.organizationId, context.auth.user.organizationId)));
    if (!record) {
        return c.json({ error: 'Call not found' }, 404);
    }
    return c.json(record);
});

app.get('/api/calls/:callSid/transcripts', async (c) => {
    const context = c.get('context');
    if (!context.auth?.user) {
        return c.json({ error: 'Unauthorized' }, 401);
    }
    const callSid = c.req.param('callSid');
    const [callRecord] = await context.db.appDB
        .select()
        .from(calls)
        .where(and(eq(calls.callSid, callSid), eq(calls.organizationId, context.auth.user.organizationId)));
    if (!callRecord) {
        return c.json({ error: 'Call not found' }, 404);
    }
    const rows = await context.db.appDB
        .select()
        .from(transcripts)
        .where(eq(transcripts.callSid, callSid))
        .orderBy(desc(transcripts.createdAt));
    return c.json(rows);
});

app.get('/api/calls/:callSid/metrics', async (c) => {
    const context = c.get('context');
    if (!context.auth?.user) {
        return c.json({ error: 'Unauthorized' }, 401);
    }
    const callSid = c.req.param('callSid');
    const [callRecord] = await context.db.appDB
        .select()
        .from(calls)
        .where(and(eq(calls.callSid, callSid), eq(calls.organizationId, context.auth.user.organizationId)));
    if (!callRecord) {
        return c.json({ error: 'Call not found' }, 404);
    }
    const rows = await context.db.appDB
        .select()
        .from(metrics)
        .where(eq(metrics.callSid, callSid))
        .orderBy(desc(metrics.createdAt));
    return c.json(rows);
});

app.post('/auth/register', async (c) => {
    const context = c.get('context');
    const payload = await safeJson(c, registerInputSchema);
    if (!payload.ok) {
        return payload.error;
    }

    const auth = createAuth(context.db.appDB, c.env, c.req.raw);
    const authResponse = await auth.api.signUpEmail({
        body: {
            email: payload.value.email,
            password: payload.value.password,
            name: payload.value.ownerName,
        },
        asResponse: true,
    } as any) as Response;
    const authBody = await readResponseJson(authResponse);

    if (!authResponse.ok) {
        return c.json(
            { error: authErrorMessage(authBody, 'Unable to create account') },
            authResponse.status as any,
        );
    }

    const signedUpUser = extractAuthUser(authBody);
    if (!signedUpUser?.id) {
        return c.json({ error: 'Better Auth did not return a user id' }, 500);
    }

    const now = currentEpochSec();
    const [organization] = await context.db.appDB
        .insert(organizations)
        .values({
            ownerUserId: signedUpUser.id,
            name: payload.value.companyName,
            domain: payload.value.domain,
            industry: payload.value.industry,
            targetCustomer: payload.value.targetCustomer,
            product: payload.value.product,
            conversionGoal: payload.value.conversionGoal,
            onboardingCompleted: 1,
            createdAt: now,
            updatedAt: now,
        })
        .returning({ id: organizations.id });

    if (!organization) {
        return c.json({ error: 'Unable to create organization' }, 500);
    }

    await seedOrganizationDefaults(context.db.appDB, organization.id);
    return jsonWithAuthCookies(
        c,
        authResponse,
        {
            ok: true,
            organizationId: organization.id,
            trialSecondsAllocated: 1200,
            trialSecondsUsed: 0,
        },
    );
});

app.post('/auth/login', async (c) => {
    const context = c.get('context');
    const payload = await safeJson(c, loginInputSchema);
    if (!payload.ok) {
        return payload.error;
    }

    const auth = createAuth(context.db.appDB, c.env, c.req.raw);
    const authResponse = await auth.api.signInEmail({
        body: {
            email: payload.value.email,
            password: payload.value.password,
        },
        headers: c.req.raw.headers,
        asResponse: true,
    } as any) as Response;
    const authBody = await readResponseJson(authResponse);

    if (!authResponse.ok) {
        return c.json(
            { error: authErrorMessage(authBody, 'Invalid credentials') },
            authResponse.status as any,
        );
    }

    let signedInUser = extractAuthUser(authBody);
    if (!signedInUser?.id) {
        const [fallbackUser] = await context.db.appDB
            .select({
                id: authUsers.id,
                name: authUsers.name,
                email: authUsers.email,
            })
            .from(authUsers)
            .where(eq(authUsers.email, payload.value.email))
            .limit(1);
        signedInUser = fallbackUser;
    }

    if (!signedInUser?.id) {
        return c.json({ error: 'Unable to resolve signed-in user' }, 500);
    }

    const [userRow] = await context.db.appDB
        .select({
            organizationId: organizations.id,
            organizationName: organizations.name,
            trialSecondsAllocated: organizations.trialSecondsAllocated,
            trialSecondsUsed: organizations.trialSecondsUsed,
            onboardingCompleted: organizations.onboardingCompleted,
        })
        .from(organizations)
        .where(eq(organizations.ownerUserId, signedInUser.id))
        .limit(1);

    if (!userRow) {
        return c.json({ error: 'No organization is linked to this user' }, 403);
    }

    return jsonWithAuthCookies(
        c,
        authResponse,
        {
            ok: true,
            user: {
                id: signedInUser.id,
                name: signedInUser.name,
                email: signedInUser.email,
                role: 'owner',
                organizationId: userRow.organizationId,
                organizationName: userRow.organizationName,
            },
            organization: {
                id: userRow.organizationId,
                name: userRow.organizationName,
                onboardingCompleted: userRow.onboardingCompleted,
            },
            trial: {
                trialSecondsAllocated: userRow.trialSecondsAllocated,
                trialSecondsUsed: userRow.trialSecondsUsed,
                trialSecondsRemaining: Math.max(
                    0,
                    userRow.trialSecondsAllocated - userRow.trialSecondsUsed,
                ),
            },
        },
    );
});

app.post('/auth/logout', async (c) => {
    const context = c.get('context');
    const auth = createAuth(context.db.appDB, c.env, c.req.raw);
    const authResponse = await auth.api.signOut({
        headers: c.req.raw.headers,
        asResponse: true,
    } as any) as Response;
    return jsonWithAuthCookies(c, authResponse, { ok: true });
});

app.get('/auth/me', async (c) => {
    const context = c.get('context');
    if (!context.auth?.user) {
        return c.json({ user: null, organization: null, trial: null }, 200);
    }
    const user = context.auth.user;
    return c.json({
        user: {
            id: user.id,
            name: user.name,
            email: user.email,
            role: user.role,
        },
        organization: {
            id: user.organizationId,
            name: user.organizationName,
            onboardingCompleted: user.onboardingCompleted,
        },
        trial: {
            trialSecondsAllocated: user.trialSecondsAllocated,
            trialSecondsUsed: user.trialSecondsUsed,
            trialSecondsRemaining: Math.max(0, user.trialSecondsAllocated - user.trialSecondsUsed),
        },
    });
});

app.post('/auth/onboarding', async (c) => {
    const context = c.get('context');
    if (!context.auth?.user) {
        return c.json({ error: 'Unauthorized' }, 401);
    }
    const payload = await safeJson(c, onboardingInputSchema);
    if (!payload.ok) {
        return payload.error;
    }
    const now = currentEpochSec();
    await context.db.appDB
        .update(organizations)
        .set({
            name: payload.value.name,
            domain: payload.value.domain,
            industry: payload.value.industry,
            targetCustomer: payload.value.targetCustomer,
            product: payload.value.product,
            conversionGoal: payload.value.conversionGoal,
            onboardingCompleted: 1,
            updatedAt: now,
        })
        .where(eq(organizations.id, context.auth.user.organizationId));

    return c.json({ ok: true });
});

app.post('/api/voice/transcript', async (c) => {
    const context = c.get('context');
    const payload = await safeJson(c, transcriptEventSchema);
    if (!payload.ok) {
        return payload.error;
    }
    const value = payload.value;
    const now = currentEpochSec();
    await context.db.appDB.insert(transcripts).values({
        callSid: value.call_sid,
        role: value.role,
        message: value.message,
        language: value.language,
        confidence: value.confidence,
        createdAt: now,
        updatedAt: now,
    });
    return c.json({ ok: true });
});

app.post('/api/voice/transcripts', async (c) => {
    const context = c.get('context');
    const payload = await safeJson(c, transcriptEventSchema);
    if (!payload.ok) {
        return payload.error;
    }
    const value = payload.value;
    const now = currentEpochSec();
    await context.db.appDB.insert(transcripts).values({
        callSid: value.call_sid,
        role: value.role,
        message: value.message,
        language: value.language,
        confidence: value.confidence,
        createdAt: now,
        updatedAt: now,
    });
    return c.json({ ok: true });
});

app.post('/api/voice/metric', async (c) => {
    const context = c.get('context');
    const payload = await safeJson(c, metricEventSchema);
    if (!payload.ok) {
        return payload.error;
    }
    const value = payload.value;
    const now = currentEpochSec();
    await context.db.appDB.insert(metrics).values({
        callSid: value.call_sid,
        eventType: value.event_type,
        metricMs: value.metric_ms,
        payload: JSON.stringify(value.payload ?? {}),
        createdAt: now,
        updatedAt: now,
    });
    return c.json({ ok: true });
});

app.post('/api/voice/metrics', async (c) => {
    const context = c.get('context');
    const payload = await safeJson(c, metricEventSchema);
    if (!payload.ok) {
        return payload.error;
    }
    const value = payload.value;
    const now = currentEpochSec();
    await context.db.appDB.insert(metrics).values({
        callSid: value.call_sid,
        eventType: value.event_type,
        metricMs: value.metric_ms,
        payload: JSON.stringify(value.payload ?? {}),
        createdAt: now,
        updatedAt: now,
    });
    return c.json({ ok: true });
});

app.post('/api/voice/event', async (c) => {
    const context = c.get('context');
    const payload = await safeJson(c, callEventSchema);
    if (!payload.ok) {
        return payload.error;
    }

    const now = currentEpochSec();
    const value = payload.value;
    await upsertCall(
        context.db.appDB,
        value.call_sid,
        {
            fromNumber: value.from_number || value.from || 'unknown',
            toNumber: value.to_number || value.to || 'unknown',
            status: value.status || 'active',
        },
        now,
        null,
        null,
    );

    if (value.event_type === 'call_ended' || value.event_type === 'call_completed') {
        const callDuration = await maybeCloseCall(context.db.appDB, value.call_sid, value.duration_seconds, now);
        if (callDuration > 0) {
            await accountForCallUsage(context.db.appDB, value.call_sid, callDuration);
        }
    }

    await context.db.appDB.insert(metrics).values({
        callSid: value.call_sid,
        eventType: value.event_type,
        metricMs: value.metric_ms,
        payload: JSON.stringify(value.payload ?? {}),
        createdAt: now,
        updatedAt: now,
    });

    return c.json({ ok: true, durationSeconds: null });
});

app.post('/api/voice/events', async (c) => {
    const context = c.get('context');
    const payload = await safeJson(c, callEventSchema);
    if (!payload.ok) {
        return payload.error;
    }
    const now = currentEpochSec();
    const value = payload.value;
    await upsertCall(
        context.db.appDB,
        value.call_sid,
        {
            fromNumber: value.from_number || value.from || 'unknown',
            toNumber: value.to_number || value.to || 'unknown',
            status: value.status || 'active',
        },
        now,
        null,
        null,
    );

    if (value.event_type === 'call_ended' || value.event_type === 'call_completed') {
        const callDuration = await maybeCloseCall(context.db.appDB, value.call_sid, value.duration_seconds, now);
        if (callDuration > 0) {
            await accountForCallUsage(context.db.appDB, value.call_sid, callDuration);
        }
    }

    await context.db.appDB.insert(metrics).values({
        callSid: value.call_sid,
        eventType: value.event_type,
        metricMs: value.metric_ms,
        payload: JSON.stringify(value.payload ?? {}),
        createdAt: now,
        updatedAt: now,
    });

    return c.json({ ok: true, durationSeconds: null });
});

app.post('/api/dispatch/start-call', async (c) => {
    const context = c.get('context');
    if (!context.auth?.user) {
        return c.json({ error: 'Unauthorized' }, 401);
    }
    const payload = await safeJson(c, dispatchCallSchema);
    if (!payload.ok) {
        return payload.error;
    }

    const now = currentEpochSec();
    const value = payload.value;
    const trialRemaining = Math.max(
        0,
        context.auth.user.trialSecondsAllocated - context.auth.user.trialSecondsUsed,
    );
    if (trialRemaining <= 0) {
        return c.json({ error: 'Trial minutes exhausted' }, 402);
    }

    await upsertCall(
        context.db.appDB,
        value.call_sid,
        {
            fromNumber: value.from_number,
            toNumber: value.to_number,
            status: value.status || 'queued',
            campaignId: value.campaign_id,
            leadId: value.lead_id,
        },
        now,
        context.auth.user.organizationId,
        context.auth.user.id,
    );

    const pythonUrl = (c.env as unknown as Record<string, string | undefined>).PYTHON_DISPATCH_URL;
    if (pythonUrl) {
        await fetch(pythonUrl, {
            method: 'POST',
            headers: {
                'content-type': 'application/json',
            },
            body: JSON.stringify({
                ...value,
                organization_id: context.auth.user.organizationId,
                user_id: context.auth.user.id,
            }),
        });
    }

    return c.json({ ok: true, callSid: value.call_sid });
});

app.use(
    '/trpc/*',
    trpcServer({
        endpoint: '/trpc',
        router: appRouter,
        createContext: (_, c) =>
            createTrpcContext({ env: c.env, request: c.req.raw }),
    }),
);

const callEventSchema = z.object({
    call_sid: z.string().min(1),
    event_type: z.string().min(1),
    status: z.string().optional(),
    from: z.string().optional(),
    to: z.string().optional(),
    from_number: z.string().optional(),
    to_number: z.string().optional(),
    metric_ms: z.number().int().nonnegative().optional(),
    duration_seconds: z.number().int().nonnegative().optional(),
    payload: z.record(z.string(), z.unknown()).optional(),
});

const transcriptEventSchema = z.object({
    call_sid: z.string().min(1),
    role: z.string().min(1),
    message: z.string().min(1),
    confidence: z.number().optional(),
    language: z.string().optional(),
});

const metricEventSchema = z.object({
    call_sid: z.string().min(1),
    event_type: z.string().min(1),
    metric_ms: z.number().int().nonnegative().optional(),
    payload: z.record(z.string(), z.unknown()).optional(),
});

const dispatchCallSchema = z.object({
    call_sid: z.string().min(1),
    from_number: z.string().min(3),
    to_number: z.string().min(3),
    campaign_id: z.number().int().positive().optional(),
    lead_id: z.number().int().positive().optional(),
    status: z.enum(['queued', 'active']).optional(),
});

const registerInputSchema = z.object({
    ownerName: z.string().min(1),
    email: z.string().email(),
    password: z.string().min(8),
    companyName: z.string().min(1),
    domain: z.string().min(3),
    industry: z.string().optional(),
    targetCustomer: z.string().optional(),
    product: z.string().optional(),
    conversionGoal: z.string().optional(),
});

const loginInputSchema = z.object({
    email: z.string().email(),
    password: z.string().min(8),
});

const onboardingInputSchema = z.object({
    name: z.string().min(1),
    domain: z.string().min(3),
    industry: z.string().optional(),
    product: z.string().optional(),
    targetCustomer: z.string().optional(),
    conversionGoal: z.string().optional(),
});

const upsertCall = async (
    db: any,
    callSid: string,
    payload: {
        fromNumber: string;
        toNumber: string;
        status: string;
        campaignId?: number;
        leadId?: number;
    },
    now: number,
    organizationId: number | null,
    userId: string | null,
) => {
    const existing = await db
        .select({ id: calls.id })
        .from(calls)
        .where(eq(calls.callSid, callSid));

    const record: {
        fromNumber: string;
        toNumber: string;
        campaignId?: number;
        leadId?: number;
        status: string;
        updatedAt: number;
        organizationId?: number;
        userId?: string;
    } = {
        fromNumber: payload.fromNumber,
        toNumber: payload.toNumber,
        campaignId: payload.campaignId,
        leadId: payload.leadId,
        status: payload.status,
        updatedAt: now,
    };
    if (organizationId !== null) {
        record.organizationId = organizationId;
    }
    if (userId !== null) {
        record.userId = userId;
    }

    if (existing.length > 0) {
        await db.update(calls).set(record).where(eq(calls.callSid, callSid));
        return;
    }

    await db.insert(calls).values({
        callSid,
        ...record,
        createdAt: now,
        startedAt: now,
    });
};

const maybeCloseCall = async (
    db: any,
    callSid: string,
    durationOverride?: number,
    now: number = currentEpochSec(),
) => {
    const [callRow] = await db
        .select({
            id: calls.id,
            organizationId: calls.organizationId,
            status: calls.status,
            startedAt: calls.startedAt,
            endedAt: calls.endedAt,
            durationSeconds: calls.durationSeconds,
        })
        .from(calls)
        .where(eq(calls.callSid, callSid))
        .limit(1);

    if (!callRow || callRow.status === 'ended' || !callRow.organizationId) {
        return 0;
    }

    const durationSeconds = durationOverride
        ? Math.floor(durationOverride / 1000)
        : Math.max(0, now - (callRow.startedAt || now));
    const finalDuration = durationSeconds > 0 ? durationSeconds : 0;

    await db
        .update(calls)
        .set({
            status: 'ended',
            endedAt: now,
            durationSeconds: finalDuration,
            updatedAt: now,
        })
        .where(eq(calls.id, callRow.id));

    return finalDuration;
};

const accountForCallUsage = async (db: any, callSid: string, durationSeconds: number) => {
    const [callRow] = await db
        .select({
            organizationId: calls.organizationId,
            durationSeconds: calls.durationSeconds,
            endedAt: calls.endedAt,
        })
        .from(calls)
        .where(eq(calls.callSid, callSid))
        .limit(1);
    if (!callRow?.organizationId) {
        return;
    }
    await db
        .update(organizations)
        .set({
            trialSecondsUsed: sql`${organizations.trialSecondsUsed} + ${durationSeconds}`,
            updatedAt: currentEpochSec(),
        })
        .where(eq(organizations.id, callRow.organizationId));
};

const seedOrganizationDefaults = async (db: any, organizationId: number) => {
    const defaultTemplates = await db
        .select({ id: templates.id })
        .from(templates)
        .where(and(isNull(templates.organizationId), eq(templates.isDefault, 1)));

    if (defaultTemplates.length > 0) {
        return;
    }

    const now = currentEpochSec();
    await db.insert(templates).values({
        organizationId: organizationId,
        name: 'AI Sales Representative',
        systemPromptTemplate:
            'You are an AI Sales Representative for {{company_name}}. Objective: Help customers understand offerings, qualify leads, build trust, and guide them toward conversion.',
        campaignContextTemplate:
            'Campaign Name: {{campaign_name}}. Description: {{campaign_description}}. Goal: {{campaign_goal}}.',
        leadContextTemplate:
            'Lead Status: {{lead_status}}. Known Information: Name: {{name}}, Company: {{company}}, Interest: {{interest}}.',
        isDefault: 1,
        createdAt: now,
        updatedAt: now,
    });
};

const currentEpochSec = () => Math.floor(Date.now() / 1000);

const readResponseJson = async (response: Response) => {
    try {
        return await response.clone().json();
    } catch {
        return null;
    }
};

const extractAuthUser = (value: any): { id: string; name: string; email: string } | null => {
    const candidate = value?.user ?? value?.data?.user;
    if (!candidate?.id) {
        return null;
    }
    return {
        id: String(candidate.id),
        name: String(candidate.name ?? ''),
        email: String(candidate.email ?? ''),
    };
};

const authErrorMessage = (value: any, fallback: string) =>
    value?.message ?? value?.error?.message ?? value?.error ?? fallback;

const jsonWithAuthCookies = (c: any, authResponse: Response, body: unknown) => {
    const headers = new Headers();
    const getSetCookie = (authResponse.headers as any).getSetCookie;
    const cookies =
        typeof getSetCookie === 'function'
            ? getSetCookie.call(authResponse.headers)
            : [authResponse.headers.get('set-cookie')].filter(Boolean);

    for (const cookie of cookies) {
        headers.append('Set-Cookie', cookie);
    }

    return c.json(body, { headers });
};

const safeJson = async (
    c: any,
    schema: z.ZodTypeAny,
): Promise<{ ok: false; error: Response } | { ok: true; value: any }> => {
    let body: unknown;
    try {
        body = await c.req.json();
    } catch {
        return {
            ok: false,
            error: c.json({ error: 'Invalid JSON body' }, 400),
        };
    }

    const parsed = schema.safeParse(body);
    if (!parsed.success) {
        return {
            ok: false,
            error: c.json({ error: parsed.error.flatten() }, 400),
        };
    }

    return { ok: true, value: parsed.data };
};

export default app;
