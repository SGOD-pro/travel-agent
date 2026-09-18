# Sources and verification scope

Checked 2026-09-12. Live accounts, credentials, plan entitlements and supplier market coverage were not tested.

| Source | What it supports | Limit |
| --- | --- | --- |
| [AWS Lambda quotas](https://docs.aws.amazon.com/lambda/latest/dg/gettingstarted-limits.html) | Package, memory, runtime limits | Not account-specific quota or performance |
| [Scrapling repository](https://github.com/D4Vinci/Scrapling) | Adaptive parsing, async fetching and crawling capabilities | Not authorization to scrape a merchant; not a guarantee of fare accuracy |
| [Google Places policy](https://developers.google.com/maps/documentation/places/web-service/policies) | Storage, attribution and map display requirements | Applicable contract and exact feature usage must be reviewed |
| [Mapbox Directions](https://docs.mapbox.com/api/navigation/directions/) | Driving, traffic, walking and cycling routing interfaces | No assumed native motorcycle/e-bike legality model |
| [Mapbox Matrix](https://docs.mapbox.com/api/navigation/matrix/) | Matrix limits and element-based billing | Account costs/quotas must be checked |
| [SWYRA Auth](https://github.com/SGOD-pro/OAuth2.1) | Identity-service candidate architecture | README is not a security audit |
| [LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence) | Checkpoint-based workflow persistence | Application idempotency/recovery still required |

The uploaded multi_agent_travel_planner_docs.pdf was inspected as historical vision input. User decisions in this conversation supersede its Supabase/Stripe/payment execution, universal price holds, EV assumptions and cost/competitor claims.

Scrapling README inspected through GitHub connector: content blob SHA 6f8d8405b5691adb305aeae5d7a83f543ede1756. This records inspected README content, not a pinned library release.
