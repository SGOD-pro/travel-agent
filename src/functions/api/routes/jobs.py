from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import select
from src.contracts.jobs import JobCreate, JobResponse
from src.config.db import async_session_maker
from src.functions.api.models import JobModel, OutboxModel

# 1. Initialize the router FIRST
router = APIRouter()

# --- 2. Standard Job Route ---
@router.post("/jobs", response_model=JobResponse, status_code=202)
async def create_job(job_data: JobCreate):
    async with async_session_maker() as session:
        new_job = JobModel(
            destination=job_data.destination,
            days=job_data.days,
            budget=job_data.budget,
            status="PENDING"
        )
        session.add(new_job)
        await session.flush() 
        
        outbox_event = OutboxModel(job_id=new_job.id, status="UNPUBLISHED")
        session.add(outbox_event)
        await session.commit()
        
        return JobResponse(
            job_id=new_job.id, 
            status="PENDING", 
            message="Job created and queued for background processing."
        )

# --- 3. Magic Link Contract & Route ---
class MagicJobCreate(BaseModel):
    url: str

@router.post("/jobs/magic")
async def create_magic_job(job_data: MagicJobCreate):
    async with async_session_maker() as session:
        new_job = JobModel(source_url=job_data.url, status="DISCOVERING")
        session.add(new_job)
        await session.flush()
        
        outbox_event = OutboxModel(job_id=new_job.id, status="UNPUBLISHED")
        session.add(outbox_event)
        await session.commit()
        
        return {"job_id": new_job.id, "status": "DISCOVERING", "message": "Analyzing link..."}

# --- 4. Check Status Route ---
@router.get("/jobs/{job_id}")
async def get_job_status(job_id: int):
    async with async_session_maker() as session:
        result = await session.execute(select(JobModel).where(JobModel.id == job_id))
        job = result.scalar_one_or_none()
        
        if job:
            # Added destination, days, and budget so the frontend can display what the AI discovered
            return {
                "job_id": job.id, 
                "status": job.status, 
                "itinerary": job.itinerary,
                "destination": job.destination,
                "days": job.days,
                "budget": job.budget
            }
        return {"error": "Job not found"}