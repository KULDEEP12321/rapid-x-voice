import { DatabaseService } from "./db";
import type { AuthContext } from "./auth";
import { getAuthContext } from "./auth";

type WorkerEnv = {
    DB: unknown;
    [key: string]: string | number | boolean | undefined | null | unknown;
};

const buildContext = async ({
    env,
    request,
}: {
    env: WorkerEnv | any;
    request?: Request;
}) => {
  const appDB = new DatabaseService(env.DB);
  const auth = await getAuthContext(appDB.db, env, request);
  return {
    env,
    db: {
      appDB: appDB.db,
      appDBService: appDB,
    },
    auth,
    request,
  };
};

export const createContext = async ({ env, request }: { env: WorkerEnv | any; request?: Request }) =>
  buildContext({ env, request });

export const createTrpcContext = async ({ env, request }: { env: WorkerEnv | any; request?: Request }) =>
  buildContext({ env, request });

export type AppContext = Awaited<ReturnType<typeof createContext>>;
export type UserContext = Awaited<ReturnType<typeof createTrpcContext>>;
export type { AuthContext };
