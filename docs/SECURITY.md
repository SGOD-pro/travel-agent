# SWENA Security & Privacy Architecture

**Version:** 2.0  
**Status:** Approved Security Specification  
**Governing Standard:** Defense-in-depth across identity, transport, application, and persistence tiers.

---

## 1. Threat Model & Trust Boundaries

```
[Untrusted Client Browser]
       │
       ├── [Trust Boundary 1: Next.js BFF Gateway] ── (PKCE S256, HttpOnly Cookie, CSRF)
       │
       ├── [Trust Boundary 2: FastAPI Resource Server] ── (Offline RS256 JWKS Signature, aud/client_id)
       │
       ├── [Trust Boundary 3: Domain Authorization] ── (Server-derived user_id == trip.owner_id)
       │
       ├── [Trust Boundary 4: External Web Extraction] ── (SSRF Protection, Domain Allowlist, Sanitization)
       │
       └── [Trust Boundary 5: PostgreSQL Database] ── (Tenant Isolation, Row Locking, Least Privilege)
```

---

## 2. Authentication & Session Security (SWYRA Auth Integration)

### 2.1 Cryptographic Token Verification
* **Elimination of Base64 Decoding:** The previous insecure baseline (`api/auth/me/route.ts`), which decoded JWT payloads without signature verification, is strictly prohibited. All tokens must be cryptographically verified using remote public keys (`JWKS_URL`) via the `jose` library (Node.js) and `PyJWT` / `cryptography` (Python).
* **Token Claims Validation:**
  * Algorithm: Strict pinning to `RS256` (rejects `none` and symmetric `HS256` confusion attacks).
  * Expiry (`exp`): Token must be within valid timestamp window; clock skew allowance $\le 60$s.
  * Audience (`aud` / `azp`): Must strictly match configured `CLIENT_ID`.
  * Issuer (`iss`): Must strictly match configured `AUTH_ISSUER`.

### 2.2 Session Cookie Lifecycle
* **Flags:** Marked `HttpOnly; Secure; SameSite=Lax; Path=/`.
* **Zero Browser Secrets:** Tokens are never stored in client `localStorage`, `sessionStorage`, or accessible via client-side JavaScript.
* **CSRF Mitigation:** State parameter generated as 16 bytes of cryptographically secure randomness; verified strictly against HttpOnly cookie on callback.

---

## 3. Multi-Tenant Authorization & Ownership Isolation

1. **Zero Client Trust:** API endpoints strictly reject client-supplied `owner_id` fields. The aggregate owner is derived exclusively from the authenticated user context (`get_current_user` in FastAPI).
2. **Resource Ownership Checks:** Every operation on `/api/v1/trips/{id}/*` validates:
   ```python
   if trip.owner_id != current_user.id:
       raise HTTPException(status_code=404, detail="Trip not found")
   ```
   *Returning 404 instead of 403 prevents attackers from probing the existence of valid trip UUIDs.*
3. **Automated Two-User Test Requirement:** Automated tests must execute two concurrent user sessions (`User A` and `User B`), verifying that `User B` receives 404 when attempting to read, update, or delete any resource created by `User A`.

---

## 4. SSRF & External Provider Protection

When performing web extraction or initiating merchant handoffs:
* **Protocol & Port Whitelist:** Only outbound `https://` on port `443` is permitted. All other schemes (`http://`, `file://`, `ftp://`, `gopher://`) are blocked.
* **Forbidden Destinations:** Egress is strictly blocked to:
  * Localhost / Loopback: `127.0.0.0/8`, `::1`
  * Private Networks (RFC 1918): `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`
  * Link-Local / AWS Metadata: `169.254.169.254`, `fe80::/10`
* **DNS Rebinding Defense:** IP resolution is validated on initial connection and re-validated on every HTTP redirect.
* **Merchant Handoff Allowlist:** Handoff URLs are validated against a strict domain allowlist (`*.irctc.co.in`, `*.kstdc.co`, `*.makemytrip.com`, `*.booking.com`). Unregistered domains are blocked with an explicit security alert.

---

## 5. Public Sharing Privacy & Data Minimization

1. **Sanitized Projections:** Public itinerary views (`/trips/[id]`) display only a filtered `PublicTripProjection`:
   * **Included:** Trip title, duration, stop names, city coordinates, itinerary timeline, road distance, weather advisories.
   * **Excluded (Stripped):** Owner user ID, email address, exact home street address, private budget line item notes, passenger dietary requirements, and external provider credentials.
2. **High-Entropy Tokens:** Share URLs use 128-bit cryptographically secure random tokens (e.g. `sh_8f93a0b2...`). Guessing or brute-forcing share links is computationally infeasible.
3. **Instant Revocation:** Owners can revoke a share token with a single click. Revocation sets `revoked_at = NOW()`, rendering the public link immediately inaccessible (404) with zero CDN caching delay.
4. **Search Engine Protection:** All public trip routes emit `X-Robots-Tag: noindex, nofollow` headers to prevent public indexing.
