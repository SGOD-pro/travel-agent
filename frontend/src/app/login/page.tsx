"use client";

import React, { Suspense, useState } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { ShieldAlert, Compass, LogIn, ArrowLeft, KeyRound } from "lucide-react";
import { Navbar } from "@/components/layout/Navbar";
import { Footer } from "@/components/layout/Footer";
import { authClient } from "@/lib/auth-client";

function LoginContent() {
  const searchParams = useSearchParams();
  const error = searchParams.get("error");
  const details = searchParams.get("details");
  const message = searchParams.get("message");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [name, setName] = useState("");
  const [mode, setMode] = useState<"sign-in" | "sign-up">("sign-in");
  const [formError, setFormError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const submit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setSubmitting(true);
    setFormError(null);
    const result =
      mode === "sign-in"
        ? await authClient.signIn.email({ email, password, callbackURL: "/dashboard" })
        : await authClient.signUp.email({ email, password, name, callbackURL: "/dashboard" });
    if (result.error) {
      setFormError(result.error.message || "Authentication failed.");
      setSubmitting(false);
      return;
    }
    window.location.assign("/dashboard");
  };

  return (
    <div className="w-full max-w-md bg-[#15271F] border border-[#233E32] rounded-3xl p-8 shadow-2xl space-y-6">
      <div className="flex items-center gap-3 border-b border-[#233E32] pb-5">
        <div className="w-10 h-10 rounded-xl bg-[#102D25] border border-[#B7C9AD]/20 flex items-center justify-center text-[#B7C9AD]">
          <Compass className="w-5 h-5" />
        </div>
        <div>
          <h1 className="text-lg font-bold text-[#F7F7F2]">SWENA Authentication</h1>
          <p className="text-xs text-[#A9B8AD]">Local Better Auth</p>
        </div>
      </div>

      {error && (
        <div className="bg-[#271E15] border border-amber-500/30 rounded-2xl p-4 text-xs space-y-2">
          <div className="flex items-center gap-2 text-amber-400 font-semibold">
            <ShieldAlert className="w-4 h-4 shrink-0" />
            <span>Identity Provider Notice: {error}</span>
          </div>
          <p className="text-[#A9B8AD] leading-relaxed">
            {details || message || "Authentication request was not completed by the authorization server."}
          </p>
          {error === "oauth_unconfigured" && (
            <div className="bg-[#0D1915] p-3 rounded-xl border border-[#233E32] space-y-1 font-mono text-[11px] text-[#B7C9AD]">
              <div className="text-[10px] uppercase text-[#A9B8AD] font-sans font-semibold">Required Setup (.env.local):</div>
              <div>AUTH_ISSUER=https://auth.swyra.internal</div>
              <div>CLIENT_ID=your_client_id</div>
              <div>CLIENT_SECRET=your_client_secret</div>
            </div>
          )}
        </div>
      )}

      <div className="space-y-3">
        <form onSubmit={submit} className="space-y-3">
          {mode === "sign-up" && (
            <input value={name} onChange={(event) => setName(event.target.value)} placeholder="Name" required className="w-full rounded-xl bg-[#0D1915] border border-[#233E32] px-4 py-3 text-sm" />
          )}
          <input type="email" value={email} onChange={(event) => setEmail(event.target.value)} placeholder="Email" required className="w-full rounded-xl bg-[#0D1915] border border-[#233E32] px-4 py-3 text-sm" />
          <input type="password" value={password} onChange={(event) => setPassword(event.target.value)} placeholder="Password" minLength={8} required className="w-full rounded-xl bg-[#0D1915] border border-[#233E32] px-4 py-3 text-sm" />
          {formError && <p className="text-xs text-amber-400">{formError}</p>}
          <button type="submit" disabled={submitting} className="btn-sage w-full py-3.5 rounded-xl font-semibold flex items-center justify-center gap-2 text-sm shadow-lg disabled:opacity-50">
            <LogIn className="w-4 h-4" />
            {submitting ? "Please wait..." : mode === "sign-in" ? "Sign In" : "Create Account"}
          </button>
        </form>
        <button type="button" onClick={() => setMode(mode === "sign-in" ? "sign-up" : "sign-in")} className="text-xs text-[#B7C9AD] underline">
          {mode === "sign-in" ? "Create a new account" : "Already have an account? Sign in"}
        </button>
      </div>

      <div className="pt-4 border-t border-[#233E32] flex items-center justify-between text-xs text-[#A9B8AD]">
        <Link href="/" className="hover:text-[#F7F7F2] flex items-center gap-1">
          <ArrowLeft className="w-3.5 h-3.5" />
          <span>Back to Home</span>
        </Link>
        <span className="flex items-center gap-1 text-[11px]">
          <KeyRound className="w-3 h-3 text-[#B7C9AD]" />
          Email + password
        </span>
      </div>
    </div>
  );
}

export default function LoginPage() {
  return (
    <div className="min-h-screen bg-[#0D1915] text-[#F7F7F2] flex flex-col font-sans">
      <Navbar />
      <main className="flex-1 flex items-center justify-center px-4 py-32">
        <Suspense fallback={<div className="text-xs text-[#A9B8AD]">Loading authentication view...</div>}>
          <LoginContent />
        </Suspense>
      </main>
      <Footer />
    </div>
  );
}
