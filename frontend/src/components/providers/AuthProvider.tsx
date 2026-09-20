"use client";

import React, { createContext, useContext, useState, useEffect, ReactNode } from "react";

export interface AuthUser {
  id: string;
  name: string;
  email: string;
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

const STATIC_USER: AuthUser = {
  id: "usr_swena_traveler",
  name: "Karnataka Explorer",
  email: "traveler@swena.internal",
};

export function AuthProvider({ children }: { children: ReactNode }) {
  // Auth system temporarily disabled per user instruction — using static user and ID
  const [user, setUser] = useState<AuthUser | null>(STATIC_USER);
  const [isLoading, setIsLoading] = useState(false);

  // useEffect(() => {
  //   fetch("/api/auth/me")
  //     .then((res) => res.json())
  //     .then((data) => {
  //       setUser(data.user || null);
  //     })
  //     .catch(() => {
  //       setUser(null);
  //     })
  //     .finally(() => {
  //       setIsLoading(false);
  //     });
  // }, []);

  const login = () => {
    window.location.href = "/api/auth/login";
  };

  const simulateLogin = () => {
    const mockUser: AuthUser = {
      id: "usr_swena_default_traveler",
      name: "Karnataka Explorer",
      email: "traveler@swena.internal",
    };
    setUser(mockUser);
  };

  const logout = async () => {
    try {
      await fetch("/api/auth/logout", { method: "POST" });
    } catch {
      // ignore
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
