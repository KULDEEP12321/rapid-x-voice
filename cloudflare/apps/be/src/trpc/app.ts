import { and, desc, eq, inArray, isNull, or, sql } from 'drizzle-orm';
import { z } from 'zod';
import { campaigns, calls, leads, metrics, organizations, settings, templates, transcripts } from '../db/schema';
import { publicProcedure, protectedProcedure, router } from './trpc';
import type { inferRouterInputs, inferRouterOutputs } from '@trpc/server';

const nowUnix = () => Math.floor(Date.now() / 1000);

const trialStatus = (auth: { user: { trialSecondsAllocated: number; trialSecondsUsed: number } }) => ({
    trialSecondsAllocated: auth.user.trialSecondsAllocated,
    trialSecondsUsed: auth.user.trialSecondsUsed,
    trialSecondsRemaining: Math.max(
        0,
        auth.user.trialSecondsAllocated - auth.user.trialSecondsUsed,
    ),
});

const campaignInput = z.object({
    name: z.string().min(1),
    templateId: z.number().int().positive().optional(),
    prompt: z.string().optional(),
    systemPrompt: z.string().optional(),
    campaignContext: z.string().optional(),
    leadContextTemplate: z.string().optional(),
    status: z.enum(['active', 'paused']).default('active'),
    notes: z.string().optional(),
}).refine((value) => Boolean(value.prompt) || Boolean(value.templateId), {
    message: 'Provide either a campaign prompt or a template',
    path: ['prompt'],
});

const leadInput = z.object({
    campaignId: z.number().int().positive().optional(),
    name: z.string().min(1),
    phone: z.string().min(6),
    email: z.string().email().optional(),
    company: z.string().optional(),
    status: z.string().optional(),
    problem: z.string().optional(),
    budget: z.string().optional(),
    timeline: z.string().optional(),
    teamSize: z.string().optional(),
    currentTools: z.string().optional(),
    interactionHistory: z.string().optional(),
    notes: z.string().optional(),
});

const callCreateInput = z.object({
    callSid: z.string().min(1),
    fromNumber: z.string().min(3),
    toNumber: z.string().min(3),
    campaignId: z.number().int().positive().optional(),
    leadId: z.number().int().positive().optional(),
    status: z.enum(['queued', 'active', 'ended', 'failed']).default('queued'),
});

const templateInput = z.object({
    name: z.string().min(1),
    systemPromptTemplate: z.string().min(1),
    campaignContextTemplate: z.string().min(1),
    leadContextTemplate: z.string().min(1),
});

const onboardingInput = z.object({
    name: z.string().min(1),
    domain: z.string().min(3),
    industry: z.string().optional(),
    product: z.string().optional(),
    targetCustomer: z.string().optional(),
    conversionGoal: z.string().optional(),
});

const paginationInput = z.object({
    limit: z.number().int().positive().max(200).default(50),
    offset: z.number().int().min(0).default(0),
});

export const appRouter = router({
    auth: router({
        me: publicProcedure.query(async ({ ctx }) => {
            if (!ctx.auth?.user) {
                return null;
            }
            return {
                user: {
                    id: ctx.auth.user.id,
                    name: ctx.auth.user.name,
                    email: ctx.auth.user.email,
                    role: ctx.auth.user.role,
                },
                organization: {
                    id: ctx.auth.user.organizationId,
                    name: ctx.auth.user.organizationName,
                    onboardingCompleted: ctx.auth.user.onboardingCompleted,
                },
                trial: trialStatus(ctx.auth),
            };
        }),
    }),

    organization: router({
        get: protectedProcedure.query(async ({ ctx }) => {
            const [row] = await ctx.db.appDB
                .select({
                    id: organizations.id,
                    name: organizations.name,
                    domain: organizations.domain,
                    industry: organizations.industry,
                    targetCustomer: organizations.targetCustomer,
                    product: organizations.product,
                    conversionGoal: organizations.conversionGoal,
                    onboardingCompleted: organizations.onboardingCompleted,
                })
                .from(organizations)
                .where(eq(organizations.id, ctx.auth.user.organizationId))
                .limit(1);

            return row ?? null;
        }),
        upsertOnboarding: protectedProcedure
            .input(onboardingInput)
            .mutation(async ({ ctx, input }) => {
                const now = nowUnix();
                await ctx.db.appDB
                    .update(organizations)
                    .set({
                        name: input.name,
                        domain: input.domain,
                        industry: input.industry,
                        targetCustomer: input.targetCustomer,
                        product: input.product,
                        conversionGoal: input.conversionGoal,
                        onboardingCompleted: 1,
                        updatedAt: now,
                    })
                    .where(eq(organizations.id, ctx.auth.user.organizationId));

                return { ok: true };
            }),
    }),

    templates: router({
        list: protectedProcedure.query(async ({ ctx }) => {
            return ctx.db.appDB
                .select()
                .from(templates)
                .where(
                    or(
                        eq(templates.isDefault, 1),
                        eq(templates.organizationId, ctx.auth.user.organizationId),
                    ),
                )
                .orderBy(desc(templates.createdAt));
        }),
        create: protectedProcedure.input(templateInput).mutation(async ({ ctx, input }) => {
            const now = nowUnix();
            await ctx.db.appDB.insert(templates).values({
                organizationId: ctx.auth.user.organizationId,
                name: input.name,
                systemPromptTemplate: input.systemPromptTemplate,
                campaignContextTemplate: input.campaignContextTemplate,
                leadContextTemplate: input.leadContextTemplate,
                createdAt: now,
                updatedAt: now,
            });
            return { ok: true };
        }),
    }),

    campaigns: router({
        list: protectedProcedure.query(async ({ ctx }) => {
            return ctx.db.appDB
                .select()
                .from(campaigns)
                .where(eq(campaigns.organizationId, ctx.auth.user.organizationId))
                .orderBy(desc(campaigns.createdAt));
        }),
        create: protectedProcedure.input(campaignInput).mutation(async ({ ctx, input }) => {
            const now = nowUnix();
            let prompt = input.prompt ?? '';
            let systemPrompt = input.systemPrompt ?? '';
            let campaignContext = input.campaignContext ?? '';
            let leadContextTemplate = input.leadContextTemplate ?? '';
            if (!prompt && input.templateId) {
                const [template] = await ctx.db.appDB
                    .select()
                    .from(templates)
                    .where(eq(templates.id, input.templateId))
                    .limit(1);
                if (template) {
                    prompt = template.name;
                    systemPrompt = template.systemPromptTemplate;
                    campaignContext = template.campaignContextTemplate;
                    leadContextTemplate = template.leadContextTemplate;
                }
            }
            await ctx.db.appDB.insert(campaigns).values({
                organizationId: ctx.auth.user.organizationId,
                templateId: input.templateId,
                name: input.name,
                prompt: prompt || `Campaign ${input.name}`,
                systemPrompt,
                campaignContext,
                leadContextTemplate,
                status: input.status,
                notes: input.notes,
                createdAt: now,
                updatedAt: now,
            });
            return {
                status: 'ok',
                campaign: {
                    name: input.name,
                    status: input.status,
                },
            };
        }),
    }),

    leads: router({
        list: protectedProcedure
            .input(leadInput.pick({ campaignId: true }).optional())
            .query(async ({ ctx, input }) => {
                const orgId = ctx.auth.user.organizationId;
                if (!input?.campaignId) {
                    return ctx.db.appDB
                        .select()
                        .from(leads)
                        .where(eq(leads.organizationId, orgId))
                        .orderBy(desc(leads.createdAt));
                }
                return ctx.db.appDB
                    .select()
                    .from(leads)
                    .where(and(eq(leads.organizationId, orgId), eq(leads.campaignId, input.campaignId)))
                    .orderBy(desc(leads.createdAt));
            }),
        create: protectedProcedure.input(leadInput).mutation(async ({ ctx, input }) => {
            const now = nowUnix();
            await ctx.db.appDB.insert(leads).values({
                organizationId: ctx.auth.user.organizationId,
                campaignId: input.campaignId,
                name: input.name,
                phone: input.phone,
                email: input.email,
                company: input.company,
                status: input.status || 'open',
                problem: input.problem,
                budget: input.budget,
                timeline: input.timeline,
                teamSize: input.teamSize,
                currentTools: input.currentTools,
                interactionHistory: input.interactionHistory,
                notes: input.notes,
                createdAt: now,
                updatedAt: now,
            });
            return {
                status: 'ok',
                lead: {
                    name: input.name,
                    phone: input.phone,
                },
            };
        }),
    }),

    calls: router({
        list: protectedProcedure.input(paginationInput.default({ limit: 50, offset: 0 })).query(async ({ ctx, input }) => {
            return ctx.db.appDB
                .select()
                .from(calls)
                .where(eq(calls.organizationId, ctx.auth.user.organizationId))
                .orderBy(desc(calls.createdAt))
                .limit(input.limit)
                .offset(input.offset);
        }),
        get: protectedProcedure.input(z.object({ callSid: z.string().min(1) })).query(async ({ ctx, input }) => {
            const [record] = await ctx.db.appDB
                .select()
                .from(calls)
                .where(and(eq(calls.callSid, input.callSid), eq(calls.organizationId, ctx.auth.user.organizationId)));
            if (!record) {
                throw new Error('Call not found');
            }
            return record;
        }),
        transcripts: protectedProcedure.input(z.object({ callSid: z.string().min(1) })).query(async ({ ctx, input }) => {
            return ctx.db.appDB
                .select()
                .from(transcripts)
                .where(eq(transcripts.callSid, input.callSid))
                .orderBy(desc(transcripts.createdAt));
        }),
        metrics: protectedProcedure.input(z.object({ callSid: z.string().min(1) })).query(async ({ ctx, input }) => {
            return ctx.db.appDB
                .select()
                .from(metrics)
                .where(eq(metrics.callSid, input.callSid))
                .orderBy(desc(metrics.createdAt));
        }),
        create: protectedProcedure.input(callCreateInput).mutation(async ({ ctx, input }) => {
            const now = nowUnix();
            const existing = await ctx.db.appDB
                .select({ id: calls.id })
                .from(calls)
                .where(eq(calls.callSid, input.callSid))
                .limit(1);
            const base = {
                organizationId: ctx.auth.user.organizationId,
                userId: ctx.auth.user.id,
                fromNumber: input.fromNumber,
                toNumber: input.toNumber,
                campaignId: input.campaignId,
                leadId: input.leadId,
                status: input.status,
                updatedAt: now,
            };
            if (existing.length > 0) {
                await ctx.db.appDB
                    .update(calls)
                    .set(base)
                    .where(eq(calls.callSid, input.callSid));
            } else {
                await ctx.db.appDB.insert(calls).values({
                    ...base,
                    callSid: input.callSid,
                    createdAt: now,
                    startedAt: now,
                });
            }
            return {
                callSid: input.callSid,
                status: input.status,
            };
        }),
    }),

    usage: router({
        trial: protectedProcedure.query(async ({ ctx }) => {
            const orgId = ctx.auth.user.organizationId;
            const [org] = await ctx.db.appDB
                .select({
                    trialSecondsAllocated: organizations.trialSecondsAllocated,
                    trialSecondsUsed: organizations.trialSecondsUsed,
                })
                .from(organizations)
                .where(eq(organizations.id, orgId));
            const [endedCalls] = await ctx.db.appDB
                .select({ endedCallsCount: sql<number>`count(*)` })
                .from(calls)
                .where(and(eq(calls.organizationId, orgId), eq(calls.status, 'ended')));
            const used = org?.trialSecondsUsed ?? 0;
            const allocated = org?.trialSecondsAllocated ?? ctx.auth.user.trialSecondsAllocated;
            const endedCallsCount = endedCalls?.endedCallsCount ?? 0;
            return {
                trialSecondsAllocated: allocated,
                trialSecondsUsed: used,
                trialSecondsRemaining: Math.max(0, allocated - used),
                endedCallsCount,
            };
        }),
    }),

    settings: router({
        get: protectedProcedure
            .input(
                z
                    .object({
                        keys: z.array(z.string().min(1)).optional(),
                    })
                    .optional(),
            )
            .query(async ({ ctx, input }) => {
                const orgKeyFilter = or(
                    isNull(settings.organizationId),
                    eq(settings.organizationId, ctx.auth.user.organizationId),
                );
                if (!input?.keys || input.keys.length === 0) {
                    return ctx.db.appDB
                        .select()
                        .from(settings)
                        .where(orgKeyFilter)
                        .orderBy(desc(settings.createdAt));
                }
                return ctx.db.appDB
                    .select()
                    .from(settings)
                    .where(and(orgKeyFilter, inArray(settings.key, input.keys)));
            }),
        upsert: protectedProcedure
            .input(z.object({
                key: z.string().min(1),
                value: z.string(),
            }))
            .mutation(async ({ ctx, input }) => {
                const now = nowUnix();
                const existing = await ctx.db.appDB
                    .select({ id: settings.id })
                    .from(settings)
                    .where(
                        and(
                            eq(settings.key, input.key),
                            eq(settings.organizationId, ctx.auth.user.organizationId),
                        ),
                    )
                    .limit(1);
                if (existing.length > 0) {
                    await ctx.db.appDB
                        .update(settings)
                        .set({ value: input.value, updatedAt: now })
                        .where(eq(settings.key, input.key));
                } else {
                    await ctx.db.appDB.insert(settings).values({
                        organizationId: ctx.auth.user.organizationId,
                        key: input.key,
                        value: input.value,
                        createdAt: now,
                        updatedAt: now,
                    });
                }
                return {
                    key: input.key,
                    value: input.value,
                };
            }),
    }),

    hello: publicProcedure.input(z.object({ name: z.string() })).query(({ input }) => ({
        greeting: `Hello, ${input.name}!`,
    })),
});

export type AppRouter = typeof appRouter;

export type RouterInputs = inferRouterInputs<AppRouter>;
export type RouterOutputs = inferRouterOutputs<AppRouter>;
