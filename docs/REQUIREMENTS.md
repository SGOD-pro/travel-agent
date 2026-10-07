# Requirements — user behavior and acceptance

This file owns required behavior. TECHNICAL_SPEC.md owns exact fields, units and enums. Tests cite the IDs below.

## Travel Planner
| ID | Must do | Acceptance |
|---|---|---|
| TR-01 | Understand origin/destination, dates/flexible period, travelers, interests, diet, mobility, transport, stay needs, budget and must-visits | Ask about blocking ambiguities; preserve accepted hard requirements |
| TR-02 | Propose relevant destination research, not all categories blindly | Selected/skipped categories have reasons and observable tasks |
| TR-03 | Weather, season, extreme conditions, sunrise/sunset and seasonal closures | Dated forecasts only within provider horizon; seasonal guidance clearly distinguished |
| TR-04 | Public/regional holidays, festivals, fairs/events and practical effects | Date/location-scoped evidence; no inferred current event calendar |
| TR-05 | Famous/must-see landmarks, buildings, heritage, religious sites, museums and signature natural attractions | Distinguish popular, must-see, optional and skip-if-limited; explain source/user relevance |
| TR-06 | Nearby attractions, small towns, clusters and day trips | Configurable 2–3 km and 5–7 km discovery bands plus actual access/travel time; no forced irrelevant suggestion |
| TR-07 | Regional dishes, street food, seasonal food and dietary-compatible experiences | Explain local significance; dish claim separate from restaurant claim |
| TR-08 | Famous/local eateries, food streets, budget options and restaurants near activities | Evidence-backed business identity; hours/reservation availability not invented |
| TR-09 | Lesser-known viewpoints, beaches, trails, villages, cultural/food places | Treat hidden status as evidence-supported label or uncertain candidate; no private/restricted/unsafe recommendation |
| TR-10 | Scenic, sunrise/sunset, architecture and photoshoot points | Access, time-of-day and weather suitability preserved when known |
| TR-11 | Culture, markets, crafts, heritage walks, performances, workshops, nightlife, nature and activities | Relevant to family/couple/solo/accessibility and interests; do not force every activity |
| TR-12 | Hours, fees, permits, parking, crowds, safety, connectivity, payment practicalities, stays and transport | Preserve freshness, scope and uncertainty; no unsupported safety guarantee |
| TR-13 | Rank experiences by interests, access, time, weather, evidence, costs, local character and diversity | Explainable heuristic; no invented psychological memory score |
| TR-14 | Build schedule, route when needed, cost breakdown and grounded report | Hard constraints checked; unknown costs not zero; infeasible/partial output explicit |
| TR-15 | Compare requested trip variants | Same assumptions/units; show known vs estimated vs unavailable provider data |

## Route Optimizer
| ID | Must do | Acceptance |
|---|---|---|
| RO-01 | Resolve selected place names with locality/entrance context | Ambiguous Indian street/pandal names require confirmation/pin; do not use first result blindly |
| RO-02 | Optimize FASTEST, SHORTEST or BALANCED under visit windows, dwell, start/end and required stops | Actual road costs; solver optimum claimed only with proof/status |
| RO-03 | Research closures, no-entry, vehicle zones, time restrictions, diversions and delay | Verified hard restriction changes provider paths or blocks feasibility; a blog guess remains uncertain |
| RO-04 | Route vehicle to permitted hub then walking leg and return to vehicle when needed | Track mode/hub continuity, walking limits and hub access |
| RO-05 | Respect restriction hours during road traversal | Static unrestricted matrix cannot prove time-dependent correctness |
| RO-06 | Mark optional unreachable stops with reasons; never silently drop must-visits | Infeasible required stop causes explicit infeasible/repair question |
| RO-07 | Return geometry, alternative variants, duration/distance, return timestamp and fuel basis | Driving and walking separate; actual local dates across midnight |
| RO-08 | Replan blocked route from current GPS/last confirmed location | Freeze completed stops, version result, recompute affected legs and remaining order/windows |
| RO-09 | Prefer longer-but-faster route for FASTEST when evidence supports delay | Label traffic as live/modeled/unknown; no invented live congestion |

Festival example: City Centre → early-closing Pandal 6 → permitted parking before Pandal 8 → 700 m walk → skip optional Pandal 11 if closure prevents access → home next day at 12:35 AM. Example totals 42 km/5h48m are illustrative; calculate them from fixture/provider data before presenting as results.

## Media Location Identifier
| ID | Must do | Acceptance |
|---|---|---|
| ML-01 | Accept public URL or owned image/video upload with optional caption, hashtags and estimated locality | Missing/inaccessible URL offers upload/manual clues; no access bypass |
| ML-02 | Extract metadata, audio and useful frame timestamps without generative LLM preprocessing | Discard redundant/blurred frames; semantic scene classification requires an evaluated model |
| ML-03 | Use OCR, ASR and optional vision only when relevant | Still image/missing audio follows skipped path; hosted ASR does not block Lambda |
| ML-04 | Fuse observations, propose candidates, search and verify identity/location | Model certainty alone never yields verified location |
| ML-05 | Return verified, candidate, insufficient-evidence or unavailable outcome | Ambiguous candidates remain selectable; user selection does not create external verification |
| ML-06 | Feed accepted place to route/travel only within requested scope | New capability suggestion does not auto-execute |

## Shared functional and non-functional requirements
| ID | Requirement | Gate |
|---|---|---|
| SH-01 | Authenticate/authorize user jobs and artifacts; internal workers private | Cross-owner and forged-event tests |
| SH-02 | Durable async execution, idempotency, lease fencing, outbox recovery and cancellation | Duplicate/crash/late-result tests; no exactly-once external API claim |
| SH-03 | Bounded tool calls, tokens, spend, wall time, retries and repairs | Budgets survive resume and fan-out; no infinite graph loops |
| SH-04 | Source provenance, support, independence/conflict and freshness | Critical false verification release gate; preserved source locators |
| SH-05 | PostgreSQL authority; S3 blob references; Redis optional cache | Cache loss cannot lose executions/results |
| SH-06 | Accurate API/TS contracts, money/time/geography units and provider labels | Generated types and negative fixtures agree |
| SH-07 | Responsive/accessibility-aware UI with truthful progress and error states | Keyboard, reduced-motion and non-map route list checks |
| SH-08 | Separate module packages and changed-consumer CI/CD | Native import/size/build tests; no heavy global dependency spill |
| SH-09 | Traceable operations, safe logs and explicit measured performance/cost | DEVELOPMENT.md gates; no unmeasured latency/throughput promises |
| SH-10 | Web/media is untrusted; URL/upload/network validation and no auth/CAPTCHA bypass | Adversarial, SSRF and oversized media tests |

V1 excludes payment execution, booking confirmation and guaranteed budget totals with unknown items. Voice remains v1.1. Provider limitations never justify fabricated results.
