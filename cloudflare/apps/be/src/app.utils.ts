import { Hono } from "hono";
import { AppContext } from "./context";


export const createApp = () =>
  new Hono<{
    Bindings: Env;
    Variables: {
      context: AppContext;
    };
  }>();
