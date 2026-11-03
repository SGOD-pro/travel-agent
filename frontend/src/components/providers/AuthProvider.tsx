"use client";

import React, { createContext, useContext, useState, useEffect, ReactNode } from "react";
import { authClient } from "@/lib/auth-client";

export interface AuthUser {
  id: string;
  name: string;
  email: string;
  isDemo?: boolean;
  isLocalAuth?: boolean;
}

interface AuthContextType {
  user: AuthUser | null;
  isLoading: boolean;
  login: () => void;
  logout: () => Promise<void>;
  simulateLogin: () => void;
}

const AuthContext = createContext<AuthContextType>({
  user: null,
  isLoading: true,
  login: () => {},
  logout: async () => {},
  simulateLogin: () => {},
});

const DEMO_USER: AuthUser = {
  id: "demo-user",
  name: "Karnataka Tourer",
  email: "",
  isDemo: true,
};

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<AuthUser | null>(null);
  const [isLoading, setIsLoading] = useState(true);

  useEffect(() => {
    fetch("/api/auth/me")
      .then(async (response) => {
        const data = (await response.json()) as {
          user?: (AuthUser & { auth_mode?: string }) | null;
        };
        setUser(
          response.ok && data.user
            ? {
                ...data.user,
                isDemo: false,
                isLocalAuth: data.user.auth_mode === "local_bypass",
              }
            : null
        );
      })
      .catch(() => setUser(null))
      .finally(() => setIsLoading(false));
  }, []);

  const login = () => {
    window.location.href = "/login";
  };

  const simulateLogin = () => setUser(DEMO_USER);

  const logout = async () => {
    try {
      await authClient.signOut();
    } catch {
      return;
    }
    setUser(null);
  };

  return (
    <AuthContext.Provider value={{ user, isLoading, login, logout, simulateLogin }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  return useContext(AuthContext);
}
