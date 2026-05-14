import { initTRPC } from "@trpc/server";
import SuperJSON from "superjson";
import { TRPCError } from "@trpc/server";

import type { UserContext } from "../context";

type AuthedContext = Omit<UserContext, "auth"> & {
    auth: NonNullable<UserContext["auth"]>;
};

const t = initTRPC.context<UserContext>().create({
    transformer: SuperJSON,
});

export const router = t.router;
export const publicProcedure = t.procedure;

export const protectedProcedure = t.procedure.use(async ({ ctx, next }) => {
    if (!ctx.auth?.user) {
        throw new TRPCError({
            code: "UNAUTHORIZED",
            message: "Authentication required",
        });
    }
    return next({
        ctx: {
            ...ctx,
            auth: ctx.auth,
        } as AuthedContext,
    });
});
