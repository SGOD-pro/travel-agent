from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from src.functions.api.routes import jobs

app = FastAPI(title="Travel Planner API", description="AI Travel Itinerary Generator")

# --- ADDED THIS TO ALLOW FRONTEND CONNECTIONS ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # Allows any frontend file to connect
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# ------------------------------------------------

app.include_router(jobs.router, prefix="/api", tags=["Jobs"])

@app.get("/")
async def root():
    return {"status": "ok", "message": "Backend server is running!"}