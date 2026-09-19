"use client";

import React, { useState } from "react";
import {
  Mail,
  MapPin,
  Send,
  MessageSquare,
  CheckCircle2,
  Phone,
  Sparkles,
  ShieldCheck,
} from "lucide-react";

export default function ContactPage() {
  const [submitted, setSubmitted] = useState(false);
  const [form, setForm] = useState({
    name: "",
    email: "",
    subject: "Bespoke Itinerary Curation",
    message: "",
  });

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    setSubmitted(true);
  };

  return (
    <div className="relative py-16 px-6 max-w-5xl mx-auto">
      {/* Header */}
      <div className="text-center max-w-3xl mx-auto mb-16">
        <div className="inline-flex items-center gap-2 px-3.5 py-1 rounded-full bg-[#15271F] border border-[#B7C9AD]/20 text-xs font-semibold text-[#B7C9AD] uppercase tracking-wider mb-6">
          <MessageSquare className="h-3.5 w-3.5" />
          <span>Connect with SWENA</span>
        </div>
        <h1 className="text-4xl sm:text-6xl font-bold tracking-tight text-[#F7F7F2]">
          Speak with our <span className="text-[#B7C9AD]">Concierge</span>
        </h1>
        <p className="mt-4 text-base sm:text-lg text-[#A9B8AD] leading-relaxed">
          Whether you are designing a high-altitude Himalayan crossing, seeking
          API integration, or requesting supplier registry access, our travel
          intelligence desk is here.
        </p>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-10">
        {/* Left: Contact Info */}
        <div className="space-y-6">
          <div className="glass-card p-6 rounded-2xl border border-[#233e32]">
            <div className="flex items-center gap-3 text-[#B7C9AD] mb-3">
              <MapPin className="h-5 w-5" />
              <h3 className="text-sm font-bold uppercase tracking-wider text-[#F7F7F2]">
                Headquarters
              </h3>
            </div>
            <p className="text-xs text-[#A9B8AD] leading-relaxed">
              SWENA Travel Intelligence Technologies
              <br />
              Indiranagar, Bengaluru, Karnataka 560038
              <br />
              India
            </p>
          </div>

          <div className="glass-card p-6 rounded-2xl border border-[#233e32]">
            <div className="flex items-center gap-3 text-[#B7C9AD] mb-3">
              <Mail className="h-5 w-5" />
              <h3 className="text-sm font-bold uppercase tracking-wider text-[#F7F7F2]">
                Direct Inquiries
              </h3>
            </div>
            <p className="text-xs text-[#A9B8AD] leading-relaxed">
              concierge@swena.travel
              <br />
              registry@swena.travel
            </p>
          </div>

          <div className="glass-card p-6 rounded-2xl border border-[#233e32]">
            <div className="flex items-center gap-3 text-[#B7C9AD] mb-3">
              <ShieldCheck className="h-5 w-5" />
              <h3 className="text-sm font-bold uppercase tracking-wider text-[#F7F7F2]">
                Registry Integrity
              </h3>
            </div>
            <p className="text-xs text-[#A9B8AD] leading-relaxed">
              Have evidence updates for Indian road corridors, toll plazas, or
              monument schedules? We verify all submissions against official
              sources.
            </p>
          </div>
        </div>

        {/* Right: Contact Form */}
        <div className="md:col-span-2 glass-card p-8 sm:p-10 rounded-3xl border border-[#B7C9AD]/20">
          {submitted ? (
            <div className="py-16 text-center space-y-4">
              <div className="h-16 w-16 bg-[#15271F] border border-[#B7C9AD]/40 text-[#B7C9AD] rounded-2xl flex items-center justify-center mx-auto mb-6">
                <CheckCircle2 className="h-8 w-8" />
              </div>
              <h3 className="text-2xl font-bold text-[#F7F7F2]">
                Message Received
              </h3>
              <p className="text-sm text-[#A9B8AD] max-w-md mx-auto">
                Thank you for contacting SWENA. Our travel intelligence concierge
                will respond to {form.email} within 24 hours.
              </p>
              <button
                onClick={() => setSubmitted(false)}
                className="btn-outline-forest px-6 py-2.5 rounded-xl text-xs font-semibold mt-4"
              >
                Send Another Message
              </button>
            </div>
          ) : (
            <form onSubmit={handleSubmit} className="space-y-6">
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-6">
                <div>
                  <label className="block text-xs font-medium text-[#A9B8AD] mb-2 uppercase tracking-wider">
                    Your Name
                  </label>
                  <input
                    type="text"
                    required
                    value={form.name}
                    onChange={(e) => setForm({ ...form, name: e.target.value })}
                    placeholder="Arjun Sharma"
                    className="w-full bg-[#0D1915] border border-[#233e32] rounded-xl px-4 py-3 text-sm text-[#F7F7F2] placeholder-[#687d6f] focus:outline-none focus:border-[#B7C9AD]"
                  />
                </div>

                <div>
                  <label className="block text-xs font-medium text-[#A9B8AD] mb-2 uppercase tracking-wider">
                    Email Address
                  </label>
                  <input
                    type="email"
                    required
                    value={form.email}
                    onChange={(e) => setForm({ ...form, email: e.target.value })}
                    placeholder="arjun@example.com"
                    className="w-full bg-[#0D1915] border border-[#233e32] rounded-xl px-4 py-3 text-sm text-[#F7F7F2] placeholder-[#687d6f] focus:outline-none focus:border-[#B7C9AD]"
                  />
                </div>
              </div>

              <div>
                <label className="block text-xs font-medium text-[#A9B8AD] mb-2 uppercase tracking-wider">
                  Inquiry Topic
                </label>
                <select
                  value={form.subject}
                  onChange={(e) => setForm({ ...form, subject: e.target.value })}
                  className="w-full bg-[#0D1915] border border-[#233e32] rounded-xl px-4 py-3 text-sm text-[#F7F7F2] focus:outline-none focus:border-[#B7C9AD]"
                >
                  <option value="Bespoke Itinerary Curation">Bespoke Itinerary Curation</option>
                  <option value="Enterprise / Concierge Desk">Enterprise / Concierge Desk</option>
                  <option value="Supplier Registry Integration">Supplier Registry Integration</option>
                  <option value="Data Verification & Feedback">Data Verification & Feedback</option>
                </select>
              </div>

              <div>
                <label className="block text-xs font-medium text-[#A9B8AD] mb-2 uppercase tracking-wider">
                  Your Message
                </label>
                <textarea
                  required
                  rows={5}
                  value={form.message}
                  onChange={(e) => setForm({ ...form, message: e.target.value })}
                  placeholder="Tell us about the travel corridor, dates, or partnership you'd like to explore..."
                  className="w-full bg-[#0D1915] border border-[#233e32] rounded-xl px-4 py-3 text-sm text-[#F7F7F2] placeholder-[#687d6f] focus:outline-none focus:border-[#B7C9AD]"
                />
              </div>

              <button
                type="submit"
                className="btn-sage w-full py-3.5 rounded-xl text-sm font-semibold flex items-center justify-center gap-2 shadow-lg"
              >
                <Send className="h-4 w-4" />
                <span>Submit Inquiry</span>
              </button>
            </form>
          )}
        </div>
      </div>
    </div>
  );
}
