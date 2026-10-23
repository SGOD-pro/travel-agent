# Travel Planner Backend MVP

This is your starter architecture! It includes the foundational pieces we discussed: 
* FastAPI routing
* Strict Pydantic contracts
* A local database setup with SQLAlchemy
* The Transactional Outbox pattern concept
* A background worker script

## How to run it:

1. **Install dependencies:**
   ```bash
   uv sync
   ```

2. **Initialize the Database:**
   (This creates a local file called `planner.db` so you don't have to install PostgreSQL yet)
   ```bash
   uv run python init_db.py
   ```

3. **Start the Web Server (The Waiter):**
   ```bash
   uv run uvicorn main:app --reload
   ```

4. **Start the Background Worker (The Kitchen):**
   Open a *second* terminal window in the same folder and run:
   ```bash
   uv run python -m src.functions.orchestrator.worker
   ```

5. **Test it!**
   Open your browser to: http://localhost:8000/docs
   Try out the `POST /api/jobs` endpoint and watch the logs in your background worker terminal to see it pick up the job and process it!
