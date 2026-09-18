# Security and privacy

Version 1.0 | Security acceptance requirements, not an audit certificate.

## Threat boundaries

Browser, external identity, API, workers, providers, LLM and persistence are separate trust boundaries. Threats include cross-user trip access, stale approval replay, token theft, SSRF via discovered URLs, hostile page prompts, data leakage in logs/checkpoints, duplicate jobs, abusive scraping and exposed exports.

## Identity acceptance

SWYRA Auth production use requires review of OAuth/OIDC flows, PKCE S256, exact redirect URIs, state/nonce, token type/issuer/audience/expiry validation, refresh token reuse detection, signing-key/JWKS rotation, cookie flags, CSRF/CORS, rate limiting, tenant isolation, recovery, email verification, secrets, logging and deployment configuration. JWT verification must pin permitted algorithms and validate issuer and intended audience. Do not assume README claims constitute test evidence.

MongoDB belongs to the external identity service. Do not copy the README's unrestricted network access recommendation as a production requirement. Choose reviewed egress/network rules. Travel data remains in PostgreSQL. Application authorization checks trip ownership regardless of successful authentication.

## Scraping and SSRF

Approved automation only. No CAPTCHA solving, authentication/paywall bypass, rate-limit evasion, robots/access-rule evasion or anti-bot bypass. Discovery does not authorize extraction. Registry records authorization/terms and last verification; blocked or unreviewed sources do not execute.

Allow HTTPS to approved merchant/provider hosts and ports. Reject userinfo URLs, private/loopback/link-local/metadata IPs, unexpected schemes and redirect destinations. Validate DNS resolution on connection and every redirect against rebinding; use outbound network controls as defense in depth. Limit bytes/time/redirect count/content types. Browser extraction runs isolated without application secrets or arbitrary filesystem access. Scraped instructions cannot change tools, destinations, system prompts or policy.

## Secrets and data

Use runtime secret injection/references, never Git-tracked credentials or registry values. Distinguish public map tokens with origin/scope restrictions from secret tokens. Enforce TLS, least-privilege IAM/DB roles and private S3. Generate short-lived object download URLs after ownership checks. No raw GPS in durable logs/checkpoints by default; stripping only top-level request fields is insufficient. Hash/query identifiers do not automatically anonymize sensitive locations.

## Approval and replay

Approvals bind actor, trip, base version, delta hash and expiry. Atomic consumption plus idempotent commands prevent replay. Change of dates/party/budget invalidates affected evidence/approval. Worker lease generation prevents stale workers committing after recovery. User cancellation/deletion prevents later job results or notifications from resurrecting data.

## Privacy lifecycle

Long-term preferences require consent by purpose/policy version. Apply provider retention/display rights to database, cache, exports and checkpoints. Deletion is an asynchronous observable workflow across records, S3 and Redis; report incomplete stores honestly. Account removal in this application must not destroy a shared SWYRA identity without explicit scope. Backup retention and restoration tombstones are documented before release.

Do not claim full GDPR/DPDP compliance from this design. Applicable obligations, notices, processors, retention and rights handling require a separate review for the actual deployment and markets.

## Release evidence

Negative ownership tests, CSRF/auth failures, expired/rotated keys, duplicate approvals, DNS/redirect SSRF, malicious extraction content, resource exhaustion, secret scanning, dependency review, S3 access and deletion recovery must be exercised. Security findings have owner/severity/resolution or explicit acceptance. No production auth gate is marked passed in this pack.
