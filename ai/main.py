import os
import json
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
import pdfplumber
from groq import Groq
from google import genai
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="Dr. Audit AI Service")

GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

if not GROQ_API_KEY:
    print("WARNING: GROQ_API_KEY not set in environment.")
if not GEMINI_API_KEY:
    print("WARNING: GEMINI_API_KEY not set in environment.")

groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None
gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

def extract_text_from_pdf(file_path: str) -> str:
    text = ""
    try:
        with pdfplumber.open(file_path) as pdf:
            for page in pdf.pages:
                extracted = page.extract_text()
                if extracted:
                    text += extracted + "\n"
    except Exception as e:
        print(f"Error reading PDF: {e}")
    return text

@app.post("/check_resume")
async def check_resume(file: UploadFile = File(...)):
    """
    Validates whether the uploaded PDF looks like a resume using pure Python heuristics.
    """
    if not file.filename.endswith('.pdf'):
        raise HTTPException(status_code=400, detail="Only PDF files are allowed")
    
    temp_file_path = f"temp_{file.filename}"
    with open(temp_file_path, "wb") as f:
        f.write(await file.read())
        
    resume_text = extract_text_from_pdf(temp_file_path)
    os.remove(temp_file_path)
    
    if len(resume_text.strip()) < 50:
        return {"is_resume": False, "reason": "Not enough text found in document. Could be an image or empty."}
        
    # Python-based heuristic to check if it's a resume
    lower_text = resume_text.lower()
    resume_keywords = ["experience", "education", "skills", "summary", "project", "work", "university", "college", "profile"]
    
    match_count = sum(1 for kw in resume_keywords if kw in lower_text)
    
    if match_count >= 2:
        return {"is_resume": True, "reason": "Looks like a valid resume."}
    else:
        return {"is_resume": False, "reason": "Document does not contain standard resume sections (e.g. Experience, Education, Skills)."}

@app.post("/analyze")
async def analyze_resume(
    file: UploadFile = File(...),
    job_role: str = Form(...),
    job_description: str = Form(...)
):
    """
    Extracts text and sends to an AI Model (Groq -> fallback Gemini) to analyze skills, experience, location and generate ATS score.
    """
    if not file.filename.endswith('.pdf'):
        raise HTTPException(status_code=400, detail="Only PDF files are allowed")
        
    temp_file_path = f"temp_{file.filename}"
    with open(temp_file_path, "wb") as f:
        f.write(await file.read())
        
    resume_text = extract_text_from_pdf(temp_file_path)
    os.remove(temp_file_path)
    
    if not resume_text.strip():
        raise HTTPException(status_code=400, detail="Could not extract text from PDF.")
        
    def call_ai(prompt_str):
        errors = []
        result_text = None
        
        if groq_client:
            try:
                print("Attempting Groq...")
                response = groq_client.chat.completions.create(
                    messages=[{"role": "user", "content": prompt_str}],
                    model="llama-3.1-8b-instant",
                )
                result_text = response.choices[0].message.content.strip()
                print("Groq success!")
            except Exception as e:
                err_msg = f"Groq Error: {str(e)}"
                print(err_msg)
                errors.append(err_msg)
                
        if not result_text and gemini_client:
            try:
                print("Attempting Gemini (Fallback)...")
                response = gemini_client.models.generate_content(
                    model='gemini-2.5-flash-lite',
                    contents=prompt_str,
                )
                result_text = response.text.strip()
                print("Gemini success!")
            except Exception as e:
                err_msg = f"Gemini Error: {str(e)}"
                print(err_msg)
                errors.append(err_msg)
                
        if not result_text:
            import traceback
            full_error = "\n".join(errors) + f"\n\nTraceback: {traceback.format_exc()}"
            print(f"All AI Providers Failed:\n{full_error}")
            raise HTTPException(status_code=500, detail=f"All AI Providers Failed: {full_error}")
            
        try:
            if "```json" in result_text:
                result_text = result_text.split("```json")[1].split("```")[0].strip()
            elif "```" in result_text:
                result_text = result_text.split("```")[1].split("```")[0].strip()
                
            if "{" in result_text and "}" in result_text:
                start_idx = result_text.find("{")
                end_idx = result_text.rfind("}") + 1
                result_text = result_text[start_idx:end_idx]
                
            return json.loads(result_text)
        except json.JSONDecodeError as de:
            error_info = f"JSON Decode Error: {str(de)} | Raw Text: {result_text}"
            print(error_info)
            raise HTTPException(status_code=500, detail=error_info)

    try:
        # STEP 1: AI Extractions 
        extraction_prompt = f"""
        You are an expert technical recruiter. Extract the exact requirements from this Job Description.
        Target Job Role: {job_role}
        
        Job Description:
        {job_description}
        
        CRITICAL INSTRUCTIONS FOR EXTRACTION:
        1. For all skills, frameworks, databases, and tools, extract ONLY the base proper noun of the technology.
        2. DO NOT include adjectives, context, or phrases like "Strong", "Experience with", "Understanding of", or "Knowledge of".
        3. If the Job Description says "Experience with Generative AI / LLMs", you must extract just "Generative AI" and "LLMs" as separate items.
        4. If it says "Strong Python programming", extract just "Python".
        
        Strictly return a valid JSON object exactly matching this schema. Do NOT include markdown code blocks.
        {{
            "location": "string",
            "experience_required": "string",
            "education": "string",
            "technical_skills": ["array", "of", "strings"],
            "frameworks_libraries": ["array", "of", "strings"],
            "databases": ["array", "of", "strings"],
            "tools_technologies": ["array", "of", "strings"],
            "cloud_devops": ["array", "of", "strings"],
            "soft_skills": ["array", "of", "strings"]
        }}
        """
        extracted_jd = call_ai(extraction_prompt)
        
        # STEP 2: Python Native Matching
        resume_lower = resume_text.lower()
        matched_skills = []
        missing_skills = []
        
        all_jd_skills = list(set([
            skill.strip() for skill in (
                extracted_jd.get("technical_skills", []) + 
                extracted_jd.get("frameworks_libraries", []) + 
                extracted_jd.get("databases", []) + 
                extracted_jd.get("tools_technologies", []) + 
                extracted_jd.get("cloud_devops", []) +
                extracted_jd.get("soft_skills", [])
            ) if skill and skill.strip()
        ]))
        
        print(f"DEBUG EXTRACTED JD SKILLS TO MATCH: {all_jd_skills}")
        
        for skill in all_jd_skills:
            if skill.lower() in resume_lower:
                matched_skills.append(skill)
            else:
                missing_skills.append(skill)
                
        print(f"DEBUG MATCHED SKILLS: {matched_skills}")
        print(f"DEBUG MISSING SKILLS: {missing_skills}")
                
        # STEP 3: Final AI Scoring
        scoring_prompt = f"""
        You are an expert ATS (Applicant Tracking System) Calculator.
        
        1. I have already extracted the exact required skills from the Job Description and matched them against the Candidate's Resume.
        2. Here are the matching results:
           - Matched Skills: {json.dumps(matched_skills)}
           - Missing Skills: {json.dumps(missing_skills)}
           - Job Required Experience: {extracted_jd.get("experience_required", "")}
           - Job Required Education: {extracted_jd.get("education", "")}
           
        3. Here is the candidate's raw Resume text so you can evaluate their experience and education context against the Job Description requirements:
        {resume_text}
        
        Perform the following SCORING calculations ONLY:
        1. Generate an ATS evaluation output (Resume Health) based on these rules:
           - Technical skills should have the highest weight.
           - Frameworks, tools, and databases should have medium weight.
           - Experience should have medium weight.
           - Location and education should have lower weight.
        2. Output exactly 1-2 short sentences of feedback explaining how to improve if the score is under 75. 
        
        Strictly return a valid JSON object matching the following schema. Do NOT include markdown code blocks.
        {{
            "ats_score": 85,
            "matched_skills": {json.dumps(matched_skills)},
            "missing_skills": {json.dumps(missing_skills)},
            "experience_match": "string describing how experience aligns",
            "location_match": "string describing how location aligns",
            "education_match": "string describing how education aligns",
            "feedback": ["suggestion 1", "suggestion 2"]
        }}
        """
        
        final_analysis = call_ai(scoring_prompt)
        return final_analysis

    except HTTPException:
        raise
    except Exception as e:
        import traceback
        error_info = f"AI Error: {str(e)}\n\nTraceback: {traceback.format_exc()}"
        print(error_info)
        
        if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
            raise HTTPException(status_code=429, detail="Gemini API rate limit exceeded. Please wait about a minute and try again.")
            
        raise HTTPException(status_code=500, detail=error_info)

# Run locally for testing:
# uvicorn main:app --reload
