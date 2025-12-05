import streamlit as st
import google.generativeai as genai
from streamlit_gsheets import GSheetsConnection
import edge_tts
import asyncio
import PyPDF2
import pandas as pd
import json
import base64
import io
import time
from datetime import datetime
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

# ==========================================
# 1. SETUP & CONFIGURATION
# ==========================================
st.set_page_config(page_title="Sales Dojo", layout="wide", page_icon="🥋")

# Check for Secrets
required_secrets = ["GOOGLE_API_KEY", "drive"]
if not all(k in st.secrets for k in required_secrets):
    st.error("🚨 Setup Needed: Missing Google API Key or Drive Folder ID in secrets.")
    st.stop()

# Configure Gemini
genai.configure(api_key=st.secrets["GOOGLE_API_KEY"])

# Initialize Session State
if "chat_history" not in st.session_state:
    st.session_state.chat_history = []
if "turn_count" not in st.session_state:
    st.session_state.turn_count = 0
if "roleplay_active" not in st.session_state:
    st.session_state.roleplay_active = True
if "session_started" not in st.session_state:
    st.session_state.session_started = False
if "current_tip" not in st.session_state:
    st.session_state.current_tip = None
if "active_model" not in st.session_state:
    st.session_state.active_model = None

# ==========================================
# 2. ROBUST MODEL SELECTOR
# ==========================================
def find_best_available_model():
    """Queries the API to find a working model name."""
    try:
        models = [m for m in genai.list_models() if 'generateContent' in m.supported_generation_methods]
        model_names = [m.name for m in models]
        
        preferences = [
            "models/gemini-1.5-flash",
            "models/gemini-1.5-flash-001",
            "models/gemini-1.5-flash-latest",
            "models/gemini-1.5-pro",
            "models/gemini-pro"
        ]
        
        for pref in preferences:
            if pref in model_names:
                return pref
        
        if model_names:
            return model_names[0]
            
        return None
    except Exception as e:
        return None

if not st.session_state.active_model:
    st.session_state.active_model = find_best_available_model()

# ==========================================
# 3. GOOGLE DRIVE HELPER FUNCTIONS
# ==========================================
@st.cache_resource
def get_drive_service():
    try:
        service_account_info = st.secrets["connections"]["gsheets"]
        creds = service_account.Credentials.from_service_account_info(
            service_account_info,
            scopes=['https://www.googleapis.com/auth/drive.readonly']
        )
        return build('drive', 'v3', credentials=creds)
    except Exception as e:
        st.error(f"Failed to connect to Google Drive: {e}")
        return None

@st.cache_data(ttl=3600) 
def load_knowledge_base_from_drive(folder_id):
    service = get_drive_service()
    if not service:
        return "", []
    
    full_text = ""
    file_list_summary = []

    try:
        results = service.files().list(
            q=f"'{folder_id}' in parents and mimeType='application/pdf' and trashed=false",
            fields="files(id, name)"
        ).execute()
        items = results.get('files', [])

        if not items:
            return "", []

        for item in items:
            request = service.files().get_media(fileId=item['id'])
            file_stream = io.BytesIO()
            downloader = MediaIoBaseDownload(file_stream, request)
            done = False
            while done is False:
                status, done = downloader.next_chunk()
            file_stream.seek(0)
            pdf_reader = PyPDF2.PdfReader(file_stream)
            file_text = ""
            for page in pdf_reader.pages:
                file_text += page.extract_text() + "\n"
            full_text += f"\n\n--- SOURCE: {item['name']} ---\n{file_text}"
            file_list_summary.append(item['name'])
    except Exception as e:
        st.error(f"Error reading from Drive: {e}")
        return "", []

    return full_text, file_list_summary

# ==========================================
# 4. AUDIO & GRADING HELPERS
# ==========================================
folder_id = st.secrets["drive"]["folder_id"]
with st.spinner("Loading Training Materials..."):
    kb_text, file_names = load_knowledge_base_from_drive(folder_id)

async def text_to_speech(text, voice):
    try:
        communicate = edge_tts.Communicate(text, voice)
        mp3_data = b""
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                mp3_data += chunk["data"]
        return mp3_data
    except Exception:
        return None

def play_audio_autoplay(audio_bytes):
    if audio_bytes:
        b64 = base64.b64encode(audio_bytes).decode()
        md = f"""
            <audio autoplay="true">
            <source src="data:audio/mp3;base64,{b64}" type="audio/mp3">
            </audio>
            """
        st.markdown(md, unsafe_allow_html=True)

# --- HARSH GRADING ENGINE ---
def calculate_final_grade_and_save(agent_name, kb_context):
    try:
        transcript = "\n".join([f"{msg['role']}: {msg['content']}" for msg in st.session_state.chat_history])
        
        coach_prompt = f"""
        You are a MASTER SALES COACH.
        
        TRAINING CONTEXT (The Correct Answers):
        {kb_context[:200000]}
        
        TRANSCRIPT TO GRADE:
        {transcript}
        
        INSTRUCTIONS:
        1. Give a STRICT Score (0-10).
           - 0-4: If they failed to handle the objection or sounded robotic.
           - 5-8: Good, but missed key phrases.
           - 9-10: Perfect execution of the "Perspective" close.
        2. Identify specific strengths and weaknesses.
        3. Provide the exact "Magic Words" they should have used.
        
        OUTPUT JSON:
        {{
            "score": (integer 0-10),
            "feedback_summary": "Detailed feedback.",
            "magic_words": "Phrase 1, Phrase 2"
        }}
        """
        
        model = genai.GenerativeModel(st.session_state.active_model)
        response = model.generate_content(
            coach_prompt,
            generation_config={"response_mime_type": "application/json"}
        )
        result = json.loads(response.text)
        
        final_score = result["score"]
        final_feedback = f"{result['feedback_summary']} \n\n🔥 MEMORIZE THIS: {result['magic_words']}"
        
        conn = st.connection("gsheets", type=GSheetsConnection)
        existing_data = conn.read(ttl=0)
        new_row = pd.DataFrame([{
            "Date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "Agent Name": agent_name,
            "Score": final_score,
            "Feedback": final_feedback
        }])
        updated_df = pd.concat([existing_data, new_row], ignore_index=True)
        conn.update(data=updated_df)
        
        return final_score, final_feedback
        
    except Exception as e:
        return 0, f"Error generating grade: {e}"

# ==========================================
# 5. SIDEBAR
# ==========================================
with st.sidebar:
    st.title("🥋 Dojo Settings")
    
    if st.session_state.active_model:
        st.success(f"🟢 Connected: {st.session_state.active_model}")
    else:
        st.error("🔴 No AI Models Found. Check API Key.")
    
    agent_name = st.text_input("Agent Name", placeholder="Enter your name")
    
    if file_names:
        st.info(f"📚 {len(file_names)} Files Loaded")
    
    voice_option = st.selectbox(
        "AI Voice",
        ["en-US-ChristopherNeural", "en-US-JennyNeural", "en-US-GuyNeural", "en-US-AriaNeural"],
        index=0
    )
    
    mode = st.radio("Select Training Mode", ["Roleplay as Realtor", "Roleplay as Homebuyer"])
    
    if st.button("Reset Session"):
        st.session_state.chat_history = []
        st.session_state.turn_count = 0
        st.session_state.roleplay_active = True
        st.session_state.session_started = False
        st.session_state.current_tip = None
        st.rerun()

# ==========================================
# 6. MODE 1: ROLEPLAY AS REALTOR
# ==========================================
if mode == "Roleplay as Realtor":
    st.title("🏡 Roleplay as Realtor")
    st.markdown("You are the **Realtor**. The AI is a **Skeptical Buyer**.")
    st.caption("OBJECTIVE: Handle ONE objection perfectly in 3 turns.")
    
    if not agent_name:
        st.warning("Enter Agent Name in sidebar.")
        st.stop()
        
    if not kb_text:
        st.error("Knowledge Base Empty.")
        st.stop()

    # Progress: 3 Turns Max
    prog_value = min(st.session_state.turn_count / 3, 1.0)
    st.progress(prog_value, text=f"Turn {st.session_state.turn_count}/3")

    context_safe = kb_text[:500000]
    
    # --- UPDATED "ALLOW PERMISSION" PERSONA ---
    system_persona = f"""
    You are a SKEPTICAL HOME BUYER.
    
    CONTEXT:
    {context_safe}
    
    INSTRUCTIONS:
    1. Start with ONE random objection.
    2. STAY ON THIS OBJECTION until it is resolved.
    3. IMPORTANT: If the agent asks for permission (e.g., "Can I share a perspective?", "Can I ask a question?"), YOU MUST SAY "YES".
       - Do NOT say "I hear you but..." to a permission question.
       - Say: "Sure, go ahead." or "Okay, what is it?"
       - AFTER they share the perspective, THEN judge if it makes sense.
    4. PROHIBITED PHRASES: Do NOT say "I hear you" or "I understand". Be direct.
    5. Always output JSON.
    """

    # --- STEP 1: START BUTTON ---
    if not st.session_state.session_started:
        if st.button("🚀 Start Roleplay (Buyer Speaks First)", type="primary"):
            with st.spinner("Buyer is selecting a random objection..."):
                try:
                    model = genai.GenerativeModel(
                        st.session_state.active_model,
                        system_instruction=system_persona
                    )
                    
                    init_prompt = ["""
                    Start the conversation. Pick one random objection from the context and say it to the agent.
                    Output JSON: {
                        "response_text": "The objection you say",
                        "strategy_tip": "One concise tip on how the agent should handle this specific objection."
                    }
                    """]
                    
                    response = model.generate_content(
                        init_prompt, 
                        generation_config={"response_mime_type": "application/json"}
                    )
                    
                    resp_json = json.loads(response.text)
                    opening_line = resp_json.get("response_text", "Hello.")
                    st.session_state.current_tip = resp_json.get("strategy_tip", "Acknowledge and validate.")
                    
                    st.session_state.chat_history.append({"role": "Buyer", "content": opening_line})
                    st.session_state.session_started = True
                    st.session_state.turn_count = 1
                    
                    tts_audio = asyncio.run(text_to_speech(opening_line, voice_option))
                    play_audio_autoplay(tts_audio)
                    st.rerun()
                    
                except Exception as e:
                    st.error(f"Error starting session: {e}")
                    if "404" in str(e):
                        st.session_state.active_model = None 

    # --- STEP 2: MAIN LOOP ---
    else:
        # Show History
        for msg in st.session_state.chat_history:
            if msg["role"] == "Buyer":
                st.info(f"**Buyer:** {msg['content']}")
            else:
                st.write(f"**You:** {msg['content']}")

        if st.session_state.current_tip:
            st.warning(f"💡 **Strategy Tip:** {st.session_state.current_tip}")

        audio_input = st.audio_input("Record your response")
        
        # Finish Button
        if st.button("🛑 Finish & Grade Session"):
            st.session_state.turn_count = 4 # Force end
            st.rerun()

        if audio_input and st.session_state.roleplay_active and st.session_state.turn_count <= 3:
             with st.spinner("The Buyer is thinking..."):
                
                audio_input.seek(0)
                audio_bytes = audio_input.read()
                
                if len(audio_bytes) < 100:
                    st.error("No audio captured.")
                    st.stop()
                
                if audio_bytes[:4].startswith(b'RIFF'):
                    mime_type = "audio/wav"
                else:
                    mime_type = "audio/webm"
                
                model = genai.GenerativeModel(
                    st.session_state.active_model,
                    system_instruction=system_persona
                )
                
                history_context = "\n".join([f"{x['role']}: {x['content']}" for x in st.session_state.chat_history])
                
                user_turn_prompt = f"""
                HISTORY SO FAR:
                {history_context}
                
                INSTRUCTIONS:
                1. Listen to the Agent.
                2. DECISION:
                   - IF they asked permission ("Can I...?", "May I...?"): Say "Yes" or "Go ahead."
                   - IF they explained a perspective/solution: Judge it.
                     - If good: "Okay, that makes sense. I can see that."
                     - If bad/evasive: "That doesn't help me with [Objection]."
                3. Output JSON:
                {{
                    "response_text": "Spoken response",
                    "strategy_tip": "Tip for the NEXT turn.",
                    "suggested_response": "The PERFECT script they should have used."
                }}
                """
                
                try:
                    response = model.generate_content(
                        [user_turn_prompt, {"mime_type": mime_type, "data": audio_bytes}],
                        generation_config={"response_mime_type": "application/json"}
                    )
                    
                    response_json = json.loads(response.text)
                    ai_text = response_json.get("response_text", "")
                    
                    st.session_state.current_tip = response_json.get("strategy_tip", "")
                    better_response = response_json.get("suggested_response", "")
                    
                    tts_audio = asyncio.run(text_to_speech(ai_text, voice_option))
                    
                    st.session_state.chat_history.append({"role": "Agent", "content": "(Audio Input)"})
                    st.session_state.chat_history.append({"role": "Buyer", "content": ai_text})
                    st.session_state.turn_count += 1
                    
                    play_audio_autoplay(tts_audio)
                    st.rerun()
                    
                except Exception as e:
                    st.error(f"AI Error: {e}")

    # --- STEP 3: FINAL GRADING ---
    if st.session_state.turn_count > 3:
        st.divider()
        st.header("🏁 Session Complete")
        
        if st.session_state.roleplay_active:
            with st.spinner("👨‍🏫 The Master Coach is grading your performance..."):
                score, feedback = calculate_final_grade_and_save(agent_name, kb_text)
                st.session_state.roleplay_active = False 
                
                if score >= 8:
                    st.balloons()
                    color = "green"
                elif score >= 5:
                    color = "orange"
                else:
                    color = "red"
                    
                st.markdown(f"## Final Score: :{color}[{score}/10]")
                st.info(f"**Coach's Feedback:**\n\n{feedback}")
                st.success("✅ Results saved to Google Sheets.")

# ==========================================
# 7. MODE 2: ROLEPLAY AS HOMEBUYER
# ==========================================
elif mode == "Roleplay as Homebuyer":
    st.title("🎓 Roleplay as Homebuyer")
    st.markdown("You act as the **Buyer**. Throw objections!")
    
    if not st.session_state.active_model:
        st.error("AI Brain not connected.")
        st.stop()

    context_safe_mc = kb_text[:500000]
    system_persona_mc = f"""
    You are the PERFECT REALTOR.
    CONTEXT: {context_safe_mc}
    Output JSON: {{ "rebuttal_text": "...", "why_it_works": "..." }}
    """
    
    audio_input_mc = st.audio_input("State your objection")
    
    if audio_input_mc and kb_text:
        with st.spinner("Formulating rebuttal..."):
            audio_input_mc.seek(0)
            audio_bytes_mc = audio_input_mc.read()
            
            if audio_bytes_mc[:4].startswith(b'RIFF'):
                mime_type_mc = "audio/wav"
            else:
                mime_type_mc = "audio/webm"

            try:
                model = genai.GenerativeModel(
                    st.session_state.active_model,
                    system_instruction=system_persona_mc
                )
                
                response_mc = model.generate_content(
                    ["Handle this objection perfectly:", {"mime_type": mime_type_mc, "data": audio_bytes_mc}],
                    generation_config={"response_mime_type": "application/json"}
                )
                resp_json_mc = json.loads(response_mc.text)
                rebuttal = resp_json_mc["rebuttal_text"]
                explanation = resp_json_mc["why_it_works"]
                
                tts_audio_mc = asyncio.run(text_to_speech(rebuttal, voice_option))
                
                st.success(f"**Agent Rebuttal:** {rebuttal}")
                play_audio_autoplay(tts_audio_mc)
                with st.expander("Why this works"):
                    st.write(explanation)
            except Exception as e:
                st.error(f"Error: {e}")
