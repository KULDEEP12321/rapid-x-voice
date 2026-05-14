import { betterAuth } from 'better-auth';
import { drizzleAdapter } from '@better-auth/drizzle-adapter';
import { eq } from 'drizzle-orm';
import * as schema from './db/schema';
import { organizations } from './db/schema';

type WorkerEnv = {
    BETTER_AUTH_SECRET?: string;
    BETTER_AUTH_URL?: string;
    BASE_URL?: string;
    FRONTEND_URL?: string;
};

export type AuthenticatedUser = {
    id: string;
    name: string;
    email: string;
    role: 'owner';
    organizationId: number;
    organizationName: string;
    trialSecondsAllocated: number;
    trialSecondsUsed: number;
    onboardingCompleted: number;
};

export type AuthContext = {
    user: AuthenticatedUser;
};

const localDevSecret = 'local-development-better-auth-secret-change-before-production';

const authBaseUrl = (env: WorkerEnv, request?: Request) => {
    if (env.BETTER_AUTH_URL) return env.BETTER_AUTH_URL;
    if (env.BASE_URL) return env.BASE_URL;
    if (request) return new URL(request.url).origin;
    return 'http://localhost:4000';
};

const trustedOrigins = (env: WorkerEnv) =>
    [env.FRONTEND_URL, env.BASE_URL, env.BETTER_AUTH_URL, 'http://localhost:3000', 'http://localhost:4000']
        .filter((value): value is string => Boolean(value))
        .map((value) => value.replace(/\/$/, ''));

export const createAuth = (db: any, env: WorkerEnv, request?: Request) =>
    betterAuth({
        database: drizzleAdapter(db, {
            provider: 'sqlite',
            schema,
        }),
        secret: env.BETTER_AUTH_SECRET || localDevSecret,
        baseURL: authBaseUrl(env, request),
        trustedOrigins: trustedOrigins(env),
        emailAndPassword: {
            enabled: true,
        },
    });

export const getAuthContext = async (
    db: any,
    env: WorkerEnv,
    request?: Request,
): Promise<AuthContext | null> => {
    if (!request) return null;

    const auth = createAuth(db, env, request);
    const session = await auth.api.getSession({
        headers: request.headers,
    });

    if (!session?.user) {
        return null;
    }

    const [organization] = await db
        .select({
            id: organizations.id,
            name: organizations.name,
            trialSecondsAllocated: organizations.trialSecondsAllocated,
            trialSecondsUsed: organizations.trialSecondsUsed,
            onboardingCompleted: organizations.onboardingCompleted,
        })
        .from(organizations)
        .where(eq(organizations.ownerUserId, session.user.id))
        .limit(1);

    if (!organization) {
        return null;
    }

    return {
        user: {
            id: session.user.id,
            name: session.user.name,
            email: session.user.email,
            role: 'owner',
            organizationId: organization.id,
            organizationName: organization.name,
            trialSecondsAllocated: organization.trialSecondsAllocated,
            trialSecondsUsed: organization.trialSecondsUsed,
            onboardingCompleted: organization.onboardingCompleted,
        },
    };
};
