"use client";
import {
  createContext,
  useContext,
  useEffect,
  useState,
  useCallback,
  type ReactNode,
} from "react";
import type { User, Session } from "@supabase/supabase-js";
import { createClient } from "@/utils/supabase/client";

interface AuthContextType {
  user: User | null;
  session: Session | null;
  loading: boolean;
  signOut: () => Promise<void>;
  refreshSession: () => Promise<void>;
}

const AuthContext = createContext<AuthContextType | undefined>(undefined);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [session, setSession] = useState<Session | null>(null);
  const [loading, setLoading] = useState(true);
  const supabase = createClient();

  useEffect(() => {
    const init = async () => {
      const {
        data: { session: initial },
      } = await supabase.auth.getSession();
      setSession(initial);
      setUser(initial?.user ?? null);
      if (initial) {
        localStorage.setItem("access_token", initial.access_token);
        localStorage.setItem("refresh_token", initial.refresh_token);
      }
      setLoading(false);
    };
    init();

    const {
      data: { subscription },
    } = supabase.auth.onAuthStateChange((_event, newSession) => {
      setSession(newSession);
      setUser(newSession?.user ?? null);
      if (newSession) {
        localStorage.setItem("access_token", newSession.access_token);
        localStorage.setItem("refresh_token", newSession.refresh_token);
      } else {
        localStorage.removeItem("access_token");
        localStorage.removeItem("refresh_token");
      }
      setLoading(false);
    });

    return () => subscription.unsubscribe();
  }, [supabase.auth]);

  const signOut = useCallback(async () => {
    await supabase.auth.signOut();
    setUser(null);
    setSession(null);
    window.location.href = "/sign-in";
  }, [supabase.auth]);

  const refreshSession = useCallback(async () => {
    const { data } = await supabase.auth.refreshSession();
    if (data.session) {
      setSession(data.session);
      localStorage.setItem("access_token", data.session.access_token);
    }
  }, [supabase.auth]);

  return (
    <AuthContext.Provider
      value={{ user, session, loading, signOut, refreshSession }}
    >
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}
