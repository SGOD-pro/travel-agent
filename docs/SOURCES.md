# SWENA Sourced References & Verification Scope

**Version:** 2.0  
**Audit Date:** 2026-09-19  
**Scope:** Sourced standards, regulatory boundaries, and provider licensing terms.

---

## 1. Verified Official References & Standards

| Source / Standard | What It Supports & Verifies | Operational Limit & Policy |
| :--- | :--- | :--- |
| **[AWS Lambda Quotas](https://docs.aws.amazon.com/lambda/latest/dg/gettingstarted-limits.html)** | Package limits (50 MB zip, 250 MB uncompressed, 10 GB container), memory (128 MB–10,240 MB), timeout (900s). | Hard limits, not recommended target sizes. Dependencies $>250$ MB require container deployment on ECS. |
| **[W3C Web Speech API Specification](https://wicg.github.io/speech-api/)** | Browser JavaScript speech recognition and synthesis interfaces. | **Privacy Reality:** Chromium/Edge implementations stream audio to cloud servers for recognition. UI must transparently disclose browser cloud streaming. |
| **[OAuth 2.1 Draft & RFC 8252 (PKCE)](https://datatracker.ietf.org/doc/html/draft-ietf-oauth-v2-1-10)** | Authorization Code Flow with PKCE ($S256$), exact redirect URI matching, single-use codes. | Client secrets never stored in public clients; tokens validated offline against RS256 JWKS. |
| **[Google Places API Policies](https://developers.google.com/maps/documentation/places/web-service/policies)** | Access point coordinates, operating hours, place details, and photo references. | Unrestricted permanent storage is prohibited. Displaying Google POIs on non-Google maps requires adherence to provider attribution and licensing terms. |
| **[Mapbox Navigation APIs](https://docs.mapbox.com/api/navigation/directions/)** | Driving, traffic, walking, and cycling routing matrices. | Dedicated motorcycle routing is not natively guaranteed; profiles must be checked against Indian highway vehicle restrictions. |
| **[Open-Meteo Weather API](https://open-meteo.com/)** | Non-commercial and attribution-backed regional temperature, precipitation, and elevation forecasts. | Short-term forecasts are distinct from seasonal monsoon climatology. |
| **[ReportLab PDF Toolkit](https://www.reportlab.com/)** | Deterministic, vector-based PDF compilation with exact pagination and font embedding. | Heavy CPU/memory task; must execute in bounded worker threads, not web request threads. |

---

## 2. Superseded Historical Assumptions (Historical PDF Review)

The initial concept document (`multi_agent_travel_planner_docs.pdf`) was reviewed as historical context. Subsequent architectural decisions ([`.agent/DECISIONS.md`](.agent/DECISIONS.md)) explicitly supersede the following assumptions:

1. **Internal Payments & Stripe:** Superseded by **D012 (Direct Official Booking Handoff)**. SWENA operates zero internal checkout, holds zero customer payments, and creates zero bookings.
2. **Universal Price Locks:** Superseded by **D005 (Evidence UX Contract)**. Indicative search observations are never disguised as guaranteed held prices.
3. **Electric Vehicle (EV) Battery/Charging Feasibility:** Superseded by **D010 (Deferred EV Modeling)**. Petrol cars and motorcycles are supported; EV modeling is deferred.
4. **Historical Cost Claims:** The claim that a full multi-agent plan costs "$0.04 and 80,000 tokens" is unverified and rejected as a planning constraint. Token and tool budgets are managed dynamically via bounded quotas.
