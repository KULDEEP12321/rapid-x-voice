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

export const user = sqliteTable('user', {
    id: text('id').primaryKey(),
    name: text('name').notNull(),
    email: text('email').notNull().unique(),
    emailVerified: integer('email_verified', { mode: 'boolean' }).notNull().default(false),
    image: text('image'),
    createdAt: integer('created_at', { mode: 'timestamp' }).notNull(),
    updatedAt: integer('updated_at', { mode: 'timestamp' }).notNull(),
});

export const session = sqliteTable('session', {
    id: text('id').primaryKey(),
    expiresAt: integer('expires_at', { mode: 'timestamp' }).notNull(),
    token: text('token').notNull().unique(),
    createdAt: integer('created_at', { mode: 'timestamp' }).notNull(),
    updatedAt: integer('updated_at', { mode: 'timestamp' }).notNull(),
    ipAddress: text('ip_address'),
    userAgent: text('user_agent'),
    userId: text('user_id')
        .notNull()
        .references(() => user.id, { onDelete: 'cascade' }),
});

export const account = sqliteTable('account', {
    id: text('id').primaryKey(),
    accountId: text('account_id').notNull(),
    providerId: text('provider_id').notNull(),
    userId: text('user_id')
        .notNull()
        .references(() => user.id, { onDelete: 'cascade' }),
    accessToken: text('access_token'),
    refreshToken: text('refresh_token'),
    idToken: text('id_token'),
    accessTokenExpiresAt: integer('access_token_expires_at', { mode: 'timestamp' }),
    refreshTokenExpiresAt: integer('refresh_token_expires_at', { mode: 'timestamp' }),
    scope: text('scope'),
    password: text('password'),
    createdAt: integer('created_at', { mode: 'timestamp' }).notNull(),
    updatedAt: integer('updated_at', { mode: 'timestamp' }).notNull(),
});

export const verification = sqliteTable('verification', {
    id: text('id').primaryKey(),
    identifier: text('identifier').notNull(),
    value: text('value').notNull(),
    expiresAt: integer('expires_at', { mode: 'timestamp' }).notNull(),
    createdAt: integer('created_at', { mode: 'timestamp' }),
    updatedAt: integer('updated_at', { mode: 'timestamp' }),
});

export const organizations = sqliteTable('organizations', {
    id: integer('id').primaryKey({ autoIncrement: true }),
    ownerUserId: text('owner_user_id').references(() => user.id, {
        onDelete: 'set null',
    }),
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
    userId: text('user_id').references(() => user.id, { onDelete: 'set null' }),
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
