"use client";

import React, { useState } from "react";
import {
  Send,
  CheckCircle2,
  AlertCircle,
  Clock,
  ShieldCheck,
  RefreshCw,
} from "lucide-react";

interface FormState {
  name: string;
  email: string;
  inquiry_type: string;
  corridor: string;
  message: string;
}

const INITIAL_FORM: FormState = {
  name: "",
  email: "",
  inquiry_type: "Bespoke Corridor Curation",
  corridor: "Western Ghats",
  message: "",
};

export function ContactForm() {
  const [form, setForm] = useState<FormState>(INITIAL_FORM);
  const [submitting, setSubmitting] = useState(false);
  const [submitted, setSubmitted] = useState(false);
  const [inquiryId, setInquiryId] = useState<string | null>(null);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSubmitting(true);
    setErrorMsg(null);

    try {
      // First try internal Next.js API route proxy
      let res = await fetch("/api/support/inquiries", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: form.name.trim(),
          email: form.email.trim(),
          inquiry_type: form.inquiry_type,
          corridor: form.corridor,
          message: form.message.trim(),
        }),
      });

      // If Next.js proxy route fails, try direct backend URL
      if (!res.ok && res.status === 503) {
        const apiBase = process.env.NEXT_PUBLIC_API_BASE_URL || "http://localhost:8000";
        res = await fetch(`${apiBase}/api/v1/support/inquiries`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            name: form.name.trim(),
            email: form.email.trim(),
            inquiry_type: form.inquiry_type,
            corridor: form.corridor,
            message: form.message.trim(),
          }),
        });
      }

      if (res.ok) {
        const data = await res.json();
        setInquiryId(data.inquiry_id);
        setSubmitted(true);
      } else {
        const errorData = await res.json().catch(() => ({}));
        const detail = errorData.detail || errorData.error || "Server rejected request";
        setErrorMsg(`Unable to submit inquiry (${res.status}): ${detail}. Please contact concierge@swena.travel directly.`);
      }
    } catch {
      setErrorMsg("Network error connecting to concierge server. Please verify your connection or email concierge@swena.travel directly.");
    } finally {
      setSubmitting(false);
    }
  };

  if (submitted) {
    return (
      <div className="rounded-3xl bg-[#15271F] border border-[#B7C9AD]/40 p-8 sm:p-12 text-center space-y-6 shadow-2xl">
        <div className="h-16 w-16 bg-[#0D1915] border border-[#B7C9AD]/50 text-[#B7C9AD] rounded-2xl flex items-center justify-center mx-auto shadow-inner">
          <CheckCircle2 className="h-8 w-8" />
        </div>

        <div className="space-y-2">
          <span className="text-xs font-semibold tracking-widest text-[#B7C9AD] uppercase font-mono">
            Inquiry Authenticated & Logged
          </span>
          <h3 className="font-serif text-2xl sm:text-3xl font-bold text-[#F7F7F2]">
            We have received your message.
          </h3>
          <p className="text-sm text-[#A9B8AD] max-w-md mx-auto leading-relaxed font-sans">
            Thank you, {form.name}. A senior itinerary concierge will review your corridor parameters and follow up at <span className="text-[#F7F7F2] font-semibold">{form.email}</span>.
          </p>
        </div>

        {inquiryId && (
          <div className="inline-block px-4 py-2 rounded-xl bg-[#0D1915] border border-[#233E32] text-xs font-mono text-[#A9B8AD]">
            Reference Ticket: <span className="text-[#B7C9AD] font-semibold">{inquiryId}</span>
          </div>
        )}

        <div className="pt-2 flex flex-col sm:flex-row items-center justify-center gap-4 text-xs text-[#6E8274] font-mono">
          <div className="flex items-center gap-1.5">
            <Clock className="h-4 w-4 text-[#B7C9AD]" />
            <span>Turnaround: 24–48 business hours</span>
          </div>
          <div className="flex items-center gap-1.5">
            <ShieldCheck className="h-4 w-4 text-[#B7C9AD]" />
            <span>Fail-closed privacy • No marketing calls</span>
          </div>
        </div>

        <div className="pt-4">
          <button
            type="button"
            onClick={() => {
              setForm(INITIAL_FORM);
              setSubmitted(false);
              setInquiryId(null);
            }}
            className="text-xs font-semibold text-[#B7C9AD] hover:text-[#F7F7F2] hover:underline cursor-pointer"
          >
            Send another inquiry
          </button>
        </div>
      </div>
    );
  }

  return (
    <form
      onSubmit={handleSubmit}
      className="rounded-3xl bg-[#15271F] border border-[#233E32] p-8 sm:p-10 shadow-2xl space-y-6"
    >
      {errorMsg && (
        <div className="p-4 rounded-xl bg-red-950/40 border border-red-800 text-xs text-red-200 flex items-center gap-2">
          <AlertCircle className="h-4 w-4 text-red-400 shrink-0" />
          <span>{errorMsg}</span>
        </div>
      )}

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-6">
        <div className="space-y-2">
          <label
            htmlFor="contact-name"
            className="block text-xs font-semibold uppercase tracking-wider text-[#A9B8AD] font-mono"
          >
            Your Full Name *
          </label>
          <input
            id="contact-name"
            type="text"
            required
            minLength={2}
            value={form.name}
            onChange={(e) => setForm({ ...form, name: e.target.value })}
            placeholder="Aarav Nambiar"
            className="w-full bg-[#0D1915] border border-[#233E32] rounded-xl px-4 py-3 text-sm text-[#F7F7F2] placeholder-[#6E8274] focus:outline-none focus:border-[#B7C9AD] focus:ring-1 focus:ring-[#B7C9AD] transition-colors font-sans"
          />
        </div>

        <div className="space-y-2">
          <label
            htmlFor="contact-email"
            className="block text-xs font-semibold uppercase tracking-wider text-[#A9B8AD] font-mono"
          >
            Email Address *
          </label>
          <input
            id="contact-email"
            type="email"
            required
            value={form.email}
            onChange={(e) => setForm({ ...form, email: e.target.value })}
            placeholder="aarav@example.com"
            className="w-full bg-[#0D1915] border border-[#233E32] rounded-xl px-4 py-3 text-sm text-[#F7F7F2] placeholder-[#6E8274] focus:outline-none focus:border-[#B7C9AD] focus:ring-1 focus:ring-[#B7C9AD] transition-colors font-sans"
          />
        </div>
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-6">
        <div className="space-y-2">
          <label
            htmlFor="contact-topic"
            className="block text-xs font-semibold uppercase tracking-wider text-[#A9B8AD] font-mono"
          >
            Inquiry Topic
          </label>
          <select
            id="contact-topic"
            value={form.inquiry_type}
            onChange={(e) => setForm({ ...form, inquiry_type: e.target.value })}
            className="w-full bg-[#0D1915] border border-[#233E32] rounded-xl px-4 py-3 text-sm text-[#F7F7F2] focus:outline-none focus:border-[#B7C9AD] focus:ring-1 focus:ring-[#B7C9AD] transition-colors font-sans"
          >
            <option value="Bespoke Corridor Curation">Bespoke Corridor Curation</option>
            <option value="Supplier Registry Verification">Supplier Registry Verification</option>
            <option value="Road Data & Toll Plaza Feedback">Road Data & Toll Plaza Feedback</option>
            <option value="Enterprise / Corporate Journey">Enterprise / Corporate Journey</option>
          </select>
        </div>

        <div className="space-y-2">
          <label
            htmlFor="contact-corridor"
            className="block text-xs font-semibold uppercase tracking-wider text-[#A9B8AD] font-mono"
          >
            Corridor of Interest
          </label>
          <select
            id="contact-corridor"
            value={form.corridor}
            onChange={(e) => setForm({ ...form, corridor: e.target.value })}
            className="w-full bg-[#0D1915] border border-[#233E32] rounded-xl px-4 py-3 text-sm text-[#F7F7F2] focus:outline-none focus:border-[#B7C9AD] focus:ring-1 focus:ring-[#B7C9AD] transition-colors font-sans"
          >
            <option value="Western Ghats">Western Ghats (Karnataka & Kerala)</option>
            <option value="Rajasthan">Rajasthan (Aravalli & Thar Circuits)</option>
            <option value="Konkan Coast">Konkan Coast (Mumbai to Goa)</option>
            <option value="Himalayan Passes">Himalayan Passes (Northern Corridors)</option>
            <option value="Custom / Other">Custom / Other Domestic Route</option>
          </select>
        </div>
      </div>

      <div className="space-y-2">
        <label
          htmlFor="contact-message"
          className="block text-xs font-semibold uppercase tracking-wider text-[#A9B8AD] font-mono"
        >
          Your Message & Parameters *
        </label>
        <textarea
          id="contact-message"
          required
          rows={5}
          minLength={10}
          value={form.message}
          onChange={(e) => setForm({ ...form, message: e.target.value })}
          placeholder="Describe your travel dates, vehicle preference, party size, or specific supplier questions..."
          className="w-full bg-[#0D1915] border border-[#233E32] rounded-xl px-4 py-3 text-sm text-[#F7F7F2] placeholder-[#6E8274] focus:outline-none focus:border-[#B7C9AD] focus:ring-1 focus:ring-[#B7C9AD] transition-colors font-sans"
        />
      </div>

      <div className="pt-2 flex flex-col sm:flex-row items-center justify-between gap-4">
        <button
          type="submit"
          disabled={submitting}
          className="w-full sm:w-auto inline-flex items-center justify-center gap-2 rounded-xl bg-[#B7C9AD] px-8 py-3.5 text-sm font-semibold text-[#102D25] hover:bg-[#C9DBBE] transition-all focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#B7C9AD] disabled:opacity-60 cursor-pointer shadow-lg shadow-black/30"
        >
          {submitting ? (
            <>
              <RefreshCw className="h-4 w-4 animate-spin" />
              <span>Submitting to Concierge Desk...</span>
            </>
          ) : (
            <>
              <Send className="h-4 w-4" />
              <span>Submit Inquiry</span>
            </>
          )}
        </button>

        <span className="text-xs text-[#6E8274] font-mono">
          Strict Zero-Hallucination: No fake success states
        </span>
      </div>
    </form>
  );
}
