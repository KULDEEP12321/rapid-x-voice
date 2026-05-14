import { integer, sqliteTable, text } from 'drizzle-orm/sqlite-core';
import { sql } from 'drizzle-orm';

const timestamps = {
    createdAt: integer('created_at')
        .notNull()
        .default(sql`(unixepoch())`),
    updatedAt: integer('updated_at')
        .notNull()
        .default(sql`(unixepoch())`),
};

const softDelete = {
    deletedAt: integer('deleted_at'),
};

export const organizations = sqliteTable('organizations', {
    id: integer('id').primaryKey({ autoIncrement: true }),
    name: text('name').notNull(),
    domain: text('domain'),
    industry: text('industry'),
    targetCustomer: text('target_customer'),
    product: text('product'),
    conversionGoal: text('conversion_goal'),
    onboardingCompleted: integer('onboarding_completed').notNull().default(0),
    trialSecondsAllocated: integer('trial_seconds_allocated').notNull().default(1200),
    trialSecondsUsed: integer('trial_seconds_used').notNull().default(0),
    ...timestamps,
    ...softDelete,
});

export const templates = sqliteTable('templates', {
    id: integer('id').primaryKey({ autoIncrement: true }),
    organizationId: integer('organization_id').references(() => organizations.id, {
        onDelete: 'set null',
    }),
    name: text('name').notNull(),
    systemPromptTemplate: text('system_prompt_template').notNull(),
    campaignContextTemplate: text('campaign_context_template').notNull(),
    leadContextTemplate: text('lead_context_template').notNull(),
    isDefault: integer('is_default').notNull().default(0),
    ...timestamps,
    ...softDelete,
});

export const users = sqliteTable('users', {
    id: integer('id').primaryKey({ autoIncrement: true }),
    email: text('email').notNull().unique(),
    name: text('name').notNull(),
    role: text('role').notNull().default('owner'),
    organizationId: integer('organization_id').references(() => organizations.id, {
        onDelete: 'cascade',
    }),
    passwordHash: text('password_hash').notNull(),
    ...timestamps,
    ...softDelete,
});

export const campaigns = sqliteTable('campaigns', {
    id: integer('id').primaryKey({ autoIncrement: true }),
    name: text('name').notNull(),
    organizationId: integer('organization_id').references(() => organizations.id, {
        onDelete: 'cascade',
    }),
    templateId: integer('template_id').references(() => templates.id, {
        onDelete: 'set null',
    }),
    status: text('status').notNull().default('active'),
    prompt: text('prompt').notNull(),
    systemPrompt: text('system_prompt'),
    campaignContext: text('campaign_context'),
    leadContextTemplate: text('lead_context'),
    notes: text('notes'),
    ...timestamps,
    ...softDelete,
});

export const leads = sqliteTable('leads', {
    id: integer('id').primaryKey({ autoIncrement: true }),
    organizationId: integer('organization_id').references(() => organizations.id, {
        onDelete: 'cascade',
    }),
    campaignId: integer('campaign_id').references(() => campaigns.id),
    name: text('name').notNull(),
    phone: text('phone').notNull(),
    email: text('email'),
    company: text('company'),
    status: text('status').notNull().default('open'),
    problem: text('problem'),
    budget: text('budget'),
    timeline: text('timeline'),
    teamSize: text('team_size'),
    currentTools: text('current_tools'),
    interactionHistory: text('interaction_history'),
    missingFields: text('missing_fields'),
    notes: text('notes'),
    ...timestamps,
    ...softDelete,
});

export const calls = sqliteTable('calls', {
    id: integer('id').primaryKey({ autoIncrement: true }),
    callSid: text('call_sid').notNull().unique(),
    organizationId: integer('organization_id').references(() => organizations.id, {
        onDelete: 'set null',
    }),
    userId: integer('user_id').references(() => users.id, {
        onDelete: 'set null',
    }),
    fromNumber: text('from_number').notNull(),
    toNumber: text('to_number').notNull(),
    campaignId: integer('campaign_id').references(() => campaigns.id),
    leadId: integer('lead_id').references(() => leads.id),
    status: text('status').notNull().default('queued'),
    startedAt: integer('started_at').default(sql`(unixepoch())`),
    endedAt: integer('ended_at'),
    durationSeconds: integer('duration_seconds').notNull().default(0),
    ...timestamps,
    ...softDelete,
});

export const transcripts = sqliteTable('transcripts', {
    id: integer('id').primaryKey({ autoIncrement: true }),
    callSid: text('call_sid').notNull(),
    role: text('role').notNull(),
    message: text('message').notNull(),
    language: text('language'),
    confidence: integer('confidence'),
    ...timestamps,
});

export const metrics = sqliteTable('metrics', {
    id: integer('id').primaryKey({ autoIncrement: true }),
    callSid: text('call_sid').notNull(),
    eventType: text('event_type').notNull(),
    metricMs: integer('metric_ms'),
    payload: text('payload'),
    ...timestamps,
});

export const settings = sqliteTable('settings', {
    id: integer('id').primaryKey({ autoIncrement: true }),
    key: text('setting_key').notNull().unique(),
    organizationId: integer('organization_id').references(() => organizations.id, {
        onDelete: 'cascade',
    }),
    value: text('value').notNull(),
    ...timestamps,
    ...softDelete,
});

export const sessions = sqliteTable('sessions', {
    id: integer('id').primaryKey({ autoIncrement: true }),
    userId: integer('user_id').notNull().references(() => users.id, {
        onDelete: 'cascade',
    }),
    tokenHash: text('token_hash').notNull().unique(),
    expiresAt: integer('expires_at').notNull(),
    ...timestamps,
    ...softDelete,
});
