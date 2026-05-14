import { QueryClient } from "@tanstack/react-query";
import { httpBatchLink } from "@trpc/client";

import superJSON from "superjson";
import { trpc } from "./trpc";
import { API_URL } from "./urls";

const queryClient = new QueryClient({
    defaultOptions: {
        queries: {
            staleTime: 1000 * 60 * 2,
            gcTime: 1000 * 60 * 10,
            refetchOnWindowFocus: false,
            retry: (failureCount, error: any) => {
                if (error?.status >= 400 && error?.status < 500) return false;
                return failureCount < 2;
            },
        },
    },
});
const trpcClient = trpc.createClient({
    links: [
        httpBatchLink({
            url: API_URL,
            fetch(url, options) {
                return fetch(url, {
                    ...options,
                    credentials: "include",
                });
            },
            transformer: superJSON,
        }),
    ],
});

export function Providers({ children }: { children: React.ReactNode }) {
    return (
        <trpc.Provider client={ trpcClient } queryClient = { queryClient } >
          {children}
        </trpc.Provider>
  );
}
