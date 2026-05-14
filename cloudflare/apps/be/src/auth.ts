import CryptoJS from 'crypto-js';
import { and, eq, gte } from 'drizzle-orm';
import { organizations, sessions, users } from './db/schema';

export const SESSION_COOKIE_NAME = 'rp_session';
const SESSION_TTL_SECONDS = 60 * 60 * 24 * 7;

export type AuthenticatedUser = {
    id: number;
    name: string;
    email: string;
    role: string;
    organizationId: number;
    organizationName: string;
    trialSecondsAllocated: number;
    trialSecondsUsed: number;
    onboardingCompleted: number;
};

export type AuthContext = {
    user: AuthenticatedUser;
};

const nowEpochSeconds = () => Math.floor(Date.now() / 1000);

const tokenHash = (token: string) => CryptoJS.SHA256(token).toString();

export const extractSessionToken = (request?: Request): string | null => {
    if (!request) return null;
    const cookieHeader = request.headers.get('cookie');
    if (!cookieHeader) return null;
    const match = cookieHeader
        .split(';')
        .map((part) => part.trim())
        .find((part) => part.startsWith(`${SESSION_COOKIE_NAME}=`));
    if (!match) return null;
    return decodeURIComponent(match.replace(`${SESSION_COOKIE_NAME}=`, ''));
};

export const getAuthContext = async (
    db: any,
    request?: Request,
): Promise<AuthContext | null> => {
    const rawToken = extractSessionToken(request);
    if (!rawToken) return null;

    const hashed = tokenHash(rawToken);
    const now = nowEpochSeconds();

    const [row] = await db
        .select({
            userId: users.id,
            userName: users.name,
            userEmail: users.email,
            userRole: users.role,
            orgId: organizations.id,
            orgName: organizations.name,
            orgTrialAllocated: organizations.trialSecondsAllocated,
            orgTrialUsed: organizations.trialSecondsUsed,
            orgOnboarded: organizations.onboardingCompleted,
            sessionId: sessions.id,
            sessionExpiresAt: sessions.expiresAt,
        })
        .from(sessions)
        .innerJoin(users, eq(users.id, sessions.userId))
        .innerJoin(organizations, eq(organizations.id, users.organizationId))
        .where(and(eq(sessions.tokenHash, hashed), gte(sessions.expiresAt, now)))
        .limit(1);

    if (!row) {
        return null;
    }

    await db
        .update(sessions)
        .set({ updatedAt: now })
        .where(eq(sessions.id, row.sessionId))
        .catch(() => {
            // Best-effort touch for activity timestamp.
        });

    return {
        user: {
            id: row.userId,
            name: row.userName,
            email: row.userEmail,
            role: row.userRole,
            organizationId: row.orgId,
            organizationName: row.orgName,
            trialSecondsAllocated: row.orgTrialAllocated,
            trialSecondsUsed: row.orgTrialUsed,
            onboardingCompleted: row.orgOnboarded,
        },
    };
};

const randomToken = () => `${crypto.randomUUID()}.${crypto.randomUUID()}`;

export const issueSession = async (db: any, userId: number): Promise<string> => {
    const token = randomToken();
    const expiresAt = nowEpochSeconds() + SESSION_TTL_SECONDS;
    await db.insert(sessions).values({
        userId,
        tokenHash: tokenHash(token),
        expiresAt,
        createdAt: nowEpochSeconds(),
        updatedAt: nowEpochSeconds(),
    });
    return token;
};

export const removeSession = async (db: any, rawToken: string): Promise<void> => {
    const hashed = tokenHash(rawToken);
    await db.delete(sessions).where(eq(sessions.tokenHash, hashed));
};

export const clearAuthCookieHeader = () =>
    `${SESSION_COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0`;

export const buildAuthCookie = (token: string) =>
    `${SESSION_COOKIE_NAME}=${encodeURIComponent(token)}; Path=/; HttpOnly; SameSite=Lax; Max-Age=${SESSION_TTL_SECONDS}`;

export const hashPassword = (password: string) => {
    const salt = CryptoJS.lib.WordArray.random(16).toString();
    const hash = CryptoJS.PBKDF2(password, salt, { keySize: 256 / 32, iterations: 120000 }).toString();
    return `${salt}:${hash}`;
};

export const verifyPassword = (password: string, passwordHash: string | null) => {
    if (!passwordHash) return false;
    const [salt, expected] = passwordHash.split(':');
    if (!salt || !expected) return false;
    const actual = CryptoJS.PBKDF2(password, salt, {
        keySize: 256 / 32,
        iterations: 120000,
    }).toString();
    return actual === expected;
};
