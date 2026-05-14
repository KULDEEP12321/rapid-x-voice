import { createTRPCReact } from "@trpc/react-query";
import type { AppRouter, RouterInputs, RouterOutputs } from "backend/trpc";


export type RouterInput = RouterInputs
export type RouterOutput = RouterOutputs
export const trpc = createTRPCReact<AppRouter>();
