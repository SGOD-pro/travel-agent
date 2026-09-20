import type { Metadata } from "next";
import {
  MessageSquare,
  MapPin,
  Mail,
  ShieldCheck,
  Clock,
  Compass,
  CheckCircle2,
} from "lucide-react";
import { ContactForm } from "@/components/marketing/ContactForm";

export const metadata: Metadata = {
  title: "Contact Concierge & Supplier Registry",
  description:
    "Connect with SWENA Travel Intelligence: bespoke itinerary curation, verified supplier registry submissions, and Indian corridor data feedback.",
  alternates: {
    canonical: "/contact",
  },
  openGraph: {
    title: "Contact Concierge & Supplier Registry | SWENA",
    description:
      "Connect with SWENA Travel Intelligence for bespoke itinerary inquiries, supplier verification, and road corridor data feedback.",
    url: "https://swena.travel/contact",
    type: "website",
    images: [
      {
        url: "/images/destinations/western-ghats.jpg",
        width: 1200,
        height: 630,
        alt: "SWENA Travel Intelligence Concierge Desk",
      },
    ],
  },
  twitter: {
    card: "summary_large_image",
    title: "Contact SWENA Travel Intelligence",
    description:
      "Connect with SWENA for bespoke itinerary curation, supplier verification, and corridor feedback.",
    images: ["/images/destinations/western-ghats.jpg"],
  },
};

const contactJsonLd = {
  "@context": "https://schema.org",
  "@type": "ContactPage",
  name: "SWENA Concierge & Registry Contact Desk",
  url: "https://swena.travel/contact",
  description:
    "Direct contact channels for SWENA Travel Intelligence: bespoke journey assistance, supplier registry submissions, and corridor feedback.",
  mainEntity: {
    "@type": "TouristInformationCenter",
    name: "SWENA Travel Intelligence Technologies",
    address: {
      "@type": "PostalAddress",
      streetAddress: "Indiranagar",
      addressLocality: "Bengaluru",
      addressRegion: "Karnataka",
      postalCode: "560038",
      addressCountry: "IN",
    },
    email: "concierge@swena.travel",
    telephone: "+91-80-4900-SWENA",
    openingHours: "Mo-Fr 09:00-18:00",
  },
};

export default function ContactPage() {
  return (
    <div className="relative min-h-screen bg-[#0D1915] text-[#F7F7F2]">
      {/* Structured SEO Data */}
      <script
        type="application/ld+json"
        dangerouslySetInnerHTML={{ __html: JSON.stringify(contactJsonLd) }}
      />

      {/* Header Chapter */}
      <section className="relative pt-24 pb-16 px-6 sm:px-12 border-b border-[#233E32]">
        <div className="mx-auto max-w-4xl text-center space-y-6">
          <div className="inline-flex items-center gap-2 px-3.5 py-1 rounded-full bg-[#15271F] border border-[#B7C9AD]/20 text-xs font-semibold text-[#B7C9AD] uppercase tracking-wider">
            <MessageSquare className="h-3.5 w-3.5" />
            <span>Direct Concierge & Registry Desk</span>
          </div>

          <h1 className="font-serif text-4xl sm:text-6xl lg:text-7xl font-bold tracking-tight text-[#F7F7F2] leading-[1.04]">
            Speak with our <br className="hidden sm:inline" />
            <span className="italic text-[#B7C9AD] font-normal">concierge desk.</span>
          </h1>

          <p className="text-base sm:text-lg text-[#A9B8AD] leading-relaxed max-w-2xl mx-auto font-normal">
            Whether you are designing a complex Western Ghats road circuit, verifying estate accommodation rates, or submitting toll plaza feedback, our team is here.
          </p>
        </div>
      </section>

      {/* Main Content Grid */}
      <section className="py-20 px-6 sm:px-12">
        <div className="mx-auto max-w-6xl grid grid-cols-1 lg:grid-cols-12 gap-12 items-start">
          {/* Left Column: Direct Info & Integrity Guarantees (5 cols) */}
          <div className="lg:col-span-5 space-y-6">
            <div className="rounded-2xl bg-[#15271F] border border-[#233E32] p-6 space-y-3">
              <div className="flex items-center gap-2.5 text-[#B7C9AD]">
                <MapPin className="h-5 w-5" />
                <h3 className="text-xs font-bold uppercase tracking-wider text-[#F7F7F2]">
                  Registered Headquarters
                </h3>
              </div>
              <p className="text-sm text-[#A9B8AD] leading-relaxed">
                SWENA Travel Intelligence Technologies
                <br />
                Indiranagar, Bengaluru, Karnataka 560038
                <br />
                India
              </p>
            </div>

            <div className="rounded-2xl bg-[#15271F] border border-[#233E32] p-6 space-y-3">
              <div className="flex items-center gap-2.5 text-[#B7C9AD]">
                <Mail className="h-5 w-5" />
                <h3 className="text-xs font-bold uppercase tracking-wider text-[#F7F7F2]">
                  Dedicated Desks
                </h3>
              </div>
              <div className="space-y-2 text-sm text-[#A9B8AD]">
                <div>
                  <span className="text-xs text-[#6E8274] block">Traveler & Journey Concierge:</span>
                  <a href="mailto:concierge@swena.travel" className="text-[#B7C9AD] hover:underline font-mono">
                    concierge@swena.travel
                  </a>
                </div>
                <div>
                  <span className="text-xs text-[#6E8274] block">Supplier Registry & Partnerships:</span>
                  <a href="mailto:registry@swena.travel" className="text-[#B7C9AD] hover:underline font-mono">
                    registry@swena.travel
                  </a>
                </div>
              </div>
            </div>

            <div className="rounded-2xl bg-[#15271F] border border-[#233E32] p-6 space-y-3">
              <div className="flex items-center gap-2.5 text-[#B7C9AD]">
                <ShieldCheck className="h-5 w-5" />
                <h3 className="text-xs font-bold uppercase tracking-wider text-[#F7F7F2]">
                  Supplier Registry Integrity
                </h3>
              </div>
              <p className="text-xs sm:text-sm text-[#A9B8AD] leading-relaxed">
                Are you a registered heritage homestay, licensed transport operator, or tourism authority? We verify all partner records through our typed evidence pipeline without charging listing fees.
              </p>
            </div>

            <div className="rounded-2xl bg-[#0A1411] border border-[#233E32]/60 p-5 space-y-2 text-xs text-[#6E8274]">
              <div className="flex items-center gap-2 text-[#A9B8AD] font-semibold">
                <Clock className="h-4 w-4 text-[#B7C9AD]" />
                <span>Response Commitment</span>
              </div>
              <p className="leading-relaxed">
                We do not use synthetic AI chatbots that fabricate promises. Inquiries are handled directly by our engineering and concierge team within 24 to 48 business hours.
              </p>
            </div>
          </div>

          {/* Right Column: Contact Form (7 cols) */}
          <div className="lg:col-span-7">
            <ContactForm />
          </div>
        </div>
      </section>
    </div>
  );
}
