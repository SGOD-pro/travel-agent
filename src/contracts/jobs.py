from pydantic import BaseModel, Field

class JobCreate(BaseModel):
    destination: str = Field(..., example="Tokyo, Japan")
    days: int = Field(..., gt=0, le=14, example=5)
    budget: float = Field(..., gt=0, example=2000.00)

class JobResponse(BaseModel):
    job_id: int
    status: str
    message: str
