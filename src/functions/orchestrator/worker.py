import asyncio
import json
import requests
from bs4 import BeautifulSoup
from src.config.db import async_session_maker
from sqlalchemy import text
from langchain_community.llms import Ollama
from langchain_core.prompts import PromptTemplate

llm = Ollama(model="llama3.2")

# Prompt 1: The Detective
discovery_prompt = PromptTemplate.from_template(
    "You are a travel detective. Read this webpage metadata and figure out the travel destination.\n"
    "Metadata: {scraped_text}\n"
    "Based on this location, suggest a realistic trip duration (days) and budget in Indian Rupees (INR).\n"
    "Return ONLY valid JSON in this exact format: {{\"destination\": \"City, Country\", \"days\": 5, \"budget\": 50000}}"
)
discovery_chain = discovery_prompt | llm

# Prompt 2: The Planner
planner_prompt = PromptTemplate.from_template(
    "Create a {days}-day travel itinerary for {destination} with a budget of ₹{budget}.\n"
    "Return ONLY valid JSON in this exact format: {{\"Day 1\": \"description\", \"Day 2\": \"description\"}}"
)
planner_chain = planner_prompt | llm

def scrape_url(url):
    try:
        # Standard header to prevent basic blocks from YouTube/Instagram
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        response = requests.get(url, headers=headers, timeout=5)
        soup = BeautifulSoup(response.text, 'html.parser')
        
        title = soup.title.string if soup.title else "No Title"
        # Try to grab Instagram/YouTube meta descriptions
        meta = soup.find("meta", property="og:description")
        desc = meta["content"] if meta else "No Description"
        
        return f"Title: {title} | Description: {desc}"
    except Exception as e:
        return f"Failed to scrape: {str(e)}"

async def main():
    print("------------------------------------------")
    print("🤖 Agentic AI Worker is online!")
    print("------------------------------------------")
    
    while True:
        async with async_session_maker() as session:
            result = await session.execute(text("SELECT id, job_id FROM outbox WHERE status = 'UNPUBLISHED' LIMIT 1"))
            row = result.fetchone()
            
            if row:
                outbox_id, job_id = row
                job_result = await session.execute(text(f"SELECT destination, days, budget, source_url FROM jobs WHERE id = {job_id}"))
                dest, days, budget, url = job_result.fetchone()
                
                # --- PHASE 1: DISCOVERY (If it's a magic link) ---
                if url and dest == "UNKNOWN":
                    print(f"\n🔍 [DETECTIVE] Analyzing link for Job #{job_id}: {url}")
                    scraped_text = scrape_url(url)
                    
                    try:
                        raw_discovery = discovery_chain.invoke({"scraped_text": scraped_text})
                        clean_discovery = raw_discovery.strip().removeprefix("```json").removesuffix("```").strip()
                        discovered_data = json.loads(clean_discovery)
                        
                        dest = discovered_data.get("destination", "Unknown")
                        days = discovered_data.get("days", 3)
                        budget = discovered_data.get("budget", 1000)
                        print(f"   🎯 Found: {dest} | {days} Days | ${budget}")
                        
                        # Update the DB with discovered facts
                        await session.execute(
                            text("UPDATE jobs SET destination = :dest, days = :days, budget = :budget, status = 'PLANNING' WHERE id = :id"),
                            {"dest": dest, "days": days, "budget": budget, "id": job_id}
                        )
                        await session.commit()
                    except Exception as e:
                        print(f"   ❌ Discovery failed: {e}")
                
                # --- PHASE 2: PLANNING ---
                print(f"   🧠 [PLANNER] Generating itinerary for {dest}...")
                try:
                    raw_plan = planner_chain.invoke({"destination": dest, "days": days, "budget": budget})
                    clean_json = raw_plan.strip().removeprefix("```json").removesuffix("```").strip()
                    
                    await session.execute(
                        text("UPDATE jobs SET status = 'COMPLETED', itinerary = :itinerary WHERE id = :job_id"),
                        {"itinerary": clean_json, "job_id": job_id}
                    )
                except Exception as e:
                    print(f"   ❌ Planning failed: {e}")
                
                await session.execute(text(f"UPDATE outbox SET status = 'PUBLISHED' WHERE id = {outbox_id}"))
                await session.commit()
                print(f"✅ Job #{job_id} COMPLETED.\n")
                
        await asyncio.sleep(2)

if __name__ == "__main__":
    asyncio.run(main())