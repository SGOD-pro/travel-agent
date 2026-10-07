# Packages and APIs by module

Names below are implementation selections/candidates, not a claim that every version is pinned or every API is free. Pin compatible versions during each module build; test native wheels in the target Lambda environment.

| Module | Python packages or Node dependencies | External services | Output / build gate |
|---|---|---|---|
| api | fastapi, mangum, pydantic, pydantic-settings; PyJWT[crypto] for configured JWT/JWKS auth | API Gateway HTTP API, S3 presigned uploads | Job IDs, results; choose trusted issuer/audience before protected live API |
| orchestrator | langgraph, langgraph-checkpoint-postgres, psycopg[binary,pool], pydantic, boto3, httpx | Bedrock Converse, Aiven PostgreSQL, SQS, S3, optional Upstash REST | Plans, tasks, checkpoints; export/check package size |
| decision_router | onnxruntime, tokenizers, numpy; audited minimal LAYA inference adapter | Versioned ONNX/tokenizer artifacts in S3 | Typed choices/scores; no guessed upstream helper names |
| evidence_collector | httpx, scrapling, pydantic, boto3, psycopg[binary] | SerpAPI HTTP search; permitted source pages | Source observations, citations, NEEDS_BROWSER request |
| browser | playwright-core, @sparticuz/chromium or -min, TypeScript | Approved public URLs, S3 | Rendered observations; matched Chromium/Playwright packaging benchmark |
| media_extractor | numpy, Pillow, ImageHash, boto3, pydantic; FFmpeg/FFprobe binaries | S3 uploads or permitted public media fetch | Audio + selected frames/timestamps; duration/resolution/runtime caps |
| media_analyzer | rapidocr + onnxruntime, numpy, boto3; OpenCV headless only if required by chosen OCR build | Amazon Transcribe asynchronous jobs; optional Mapillary/KartaView through orchestrator tools | OCR/ASR observations; selected OCR languages must be evaluated |
| optimizer | ortools, pydantic, httpx, psycopg[binary]; shapely/pyproj only if needed for geometry checks | Configured Valhalla adapter | Route alternatives, validity, fuel; production endpoint/support gate |

## Travel-specific tools inside orchestrator
| Capability | Packages | Provider / limitation |
|---|---|---|
| Places and nearby POIs | httpx; PostGIS SQL via psycopg | Nominatim-compatible geocoder, Overpass; not guaranteed complete Indian street/pandal coverage |
| Weather | httpx | Open-Meteo candidate; commercial terms must be approved; dates beyond forecast horizon use labeled seasonal guidance |
| Festivals, foods, hidden places, precautions | Existing evidence tasks | SerpAPI + official/local sources + Scrapling; no universal reliable closures/crowd API assumed |
| Street imagery verification | httpx | Mapillary Graph API token; KartaView coverage/service contract must be checked before enabling |
| Ranking/schedule/budget | Python datetime, zoneinfo, decimal; shared typed contracts | Deterministic rules; LLM explanations cannot change calculated amounts |
| Flights/stays/transport discovery | Existing evidence/provider adapters | Initially editorial/indicative links. Amadeus/Duffel/Hotelbeds remain deferred until separately approved integration and credentials |

## Models
Nova Lite through Bedrock is the initial hosted planning/evidence-interpretation/synthesis choice; Nova Micro is optional simple text routing work only after evaluation. Model IDs are configurable and must exist in selected region/account. Hosted inference costs money; Lambda includes SDK clients, not their model weights.

LAYA is a bounded non-generative decision engine. The upstream package may pull Torch/Transformers: do not blindly ship laya[onnx]. Export outside Lambda, audit minimal imports and compare CPU decision quality before deployment. Quantization method must be chosen from the pinned release and evaluated; no per-channel default is assumed.

FFmpeg + pHash/blur/brightness filtering removes redundant/poor frames without an LLM. It cannot semantically identify buildings/nature/roads by itself. Optional MobileNetV3 ONNX classifier is an evaluated candidate, not a mandatory v1 dependency or proof of location. ASR initially uses Transcribe; no large Whisper/Gemma/20B model weights in ZIP Lambda.

Frontend: Next.js, React, TypeScript, mapcn components + maplibre-gl, existing shadcn/ui and selected GSAP motion. No frontend LLM. Select a licensed tile/style source; do not assume a map library includes unlimited free tiles. Voice v1.1 uses MediaRecorder, optional browser VAD, hosted ASR and existing workflow; no mandatory new Lambda.

## Credential and provider checklist
| Configuration | Needed before |
|---|---|
| AWS region and IAM role; local AWS_PROFILE if used | AWS live smoke tests; use normal SDK credential chain |
| DATABASE_URL | Durable execution integration tests |
| SERPAPI_API_KEY | Real evidence search smoke test |
| BEDROCK_MODEL_ID + model access | LLM live smoke test |
| LAYA artifact URI/version/checksum | CPU inference benchmark |
| ROUTING_BASE_URL + capabilities | Real route/closure smoke test |
| GEOCODER_BASE_URL + provider policy | Geocoding smoke test |
| MAPILLARY_ACCESS_TOKEN | Optional imagery verification |
| Weather endpoint/license; map tile/style configuration | Weather/map release tests |
| JWT issuer/audience/JWKS URL | Authenticated API release |
| Upstash REST URL/token | Optional shared cache/rate limit |

Missing keys block live integration only, not fixture development. Never paste secrets into documentation/source/chat.

## Free/open boundaries
Nominatim public endpoint policy: max 1 request/second for the whole application, identifying User-Agent, attribution, caching, no client autocomplete or distributed bulk search. Prefer configurable compatible provider for production. Read https://operations.osmfoundation.org/policies/nominatim/ before deliberately enabling the public endpoint.

Overpass/OSM data is open; public endpoints have capacity limits and attribution/license requirements. SerpAPI, Bedrock, Transcribe, AWS and hosted database usage are not inherently free. Open-Meteo free endpoint is for non-commercial use; verify commercial/self-host terms. Imagery coverage and credentials vary.

## Primary references checked during this revision
- https://github.com/D4Vinci/Scrapling
- https://github.com/NandhaKishorM/laya
- https://github.com/RapidAI/RapidOCR
- https://github.com/Sparticuz/chromium
- https://serpapi.com/search-api
- https://operations.osmfoundation.org/policies/nominatim/
- https://nominatim.org/release-docs/latest/api/Search/
- https://valhalla.github.io/valhalla/api/matrix/api-reference/
- https://github.com/valhalla/valhalla/blob/master/docs/docs/api/route/api-reference.md
- https://open-meteo.com/en/docs
- https://open-meteo.com/en/pricing
- https://docs.aws.amazon.com/lambda/latest/dg/gettingstarted-limits.html
