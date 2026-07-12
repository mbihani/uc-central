import {
  createContext,
  useContext,
  useState,
  useCallback,
  type ReactNode,
} from "react";
import { useQueryClient } from "@tanstack/react-query";
import { AUTH_TOKEN_KEY, SKIP_AUTH_RECOVERY_HEADER } from "@/lib/auth-interceptor";
import type { User } from "@/lib/api";

interface AuthContextValue {
  token: string | null;
  isCustomAuth: boolean;
  login: (token: string) => Promise<User>;
  logout: () => void;
}

const AuthContext = createContext<AuthContextValue>({
  token: null,
  isCustomAuth: false,
  login: async () => {
    throw new Error("AuthProvider not mounted");
  },
  logout: () => {},
});

export function AuthProvider({ children }: { children: ReactNode }) {
  const [token, setToken] = useState<string | null>(() =>
    localStorage.getItem(AUTH_TOKEN_KEY),
  );
  const queryClient = useQueryClient();

  const login = useCallback(
    async (newToken: string): Promise<User> => {
      const res = await fetch("/api/current-user", {
        headers: {
          "X-Forwarded-Access-Token": newToken,
          // This is a deliberate credential test — a 401 here means the token
          // the user typed is bad, so surface it in the dialog instead of
          // letting the interceptor clear it and reload the page.
          [SKIP_AUTH_RECOVERY_HEADER]: "1",
        },
      });
      if (!res.ok) {
        const body = await res.text();
        let message: string;
        try {
          const parsed = JSON.parse(body);
          message = parsed.detail || parsed.message || body;
        } catch {
          message = body || "Invalid token";
        }
        throw new Error(message);
      }
      const user: User = await res.json();

      localStorage.setItem(AUTH_TOKEN_KEY, newToken);
      setToken(newToken);

      await queryClient.invalidateQueries();

      return user;
    },
    [queryClient],
  );

  const logout = useCallback(() => {
    localStorage.removeItem(AUTH_TOKEN_KEY);
    setToken(null);
    queryClient.invalidateQueries();
  }, [queryClient]);

  return (
    <AuthContext.Provider
      value={{ token, isCustomAuth: !!token, login, logout }}
    >
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthContextValue {
  return useContext(AuthContext);
}
