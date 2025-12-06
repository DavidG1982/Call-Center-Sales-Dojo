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
import random
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
if "kb_text" not in st.session_state:
    st.session_state.kb_text = ""
if "file_names" not in st.session_state:
    st.session_state.file_names = []
if "active_model" not in st.session_state:
    st.session_state.active_model = None
if "mode_2_chat" not in st.session_state:
    st.session_state.mode_2_chat = []

# ==========================================
# 2. SMART MODEL SELECTOR (CACHED)
# ==========================================
@st.cache_data(ttl=3600)
def get_best_model_name():
    try:
        models = [m.name for m in genai.list_models() if 'generateContent' in m.supported_generation_methods]
        preferences = [
            "models/gemini-1.5-flash",
            "models/gemini-1.5-flash-001",
            "models/gemini-1.5-flash-latest",
            "models/gemini-1.5-pro",
            "models/gemini-pro"
        ]
        for pref in preferences:
            if pref in models:
                return pref
        if models:
            return models[0]
        return "models/gemini-1.5-flash"
    except Exception:
        return "models/gemini-1.5-flash"

if not st.session_state.active_model:
    st.session_state.active_model = get_best_model_name()

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
        return build('drive', 'v3', credentials=creds, cache_discovery=False)
    except Exception as e:
        st.error(f"Failed to connect to Google Drive: {e}")
        return None

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
    # This is the "HTML Hack" for Mode 1 that works well
    if audio_bytes:
        b64 = base64.b64encode(audio_bytes).decode()
        md = f"""
            <audio autoplay="true">
            <source src="data:audio/mp3;base64,{b64}" type="audio/mp3">
            </audio>
            """
        st.markdown(md, unsafe_allow_html=True)

# --- INITIALIZE KB ---
if not st.session_state.kb_text:
    folder_id = st.secrets["drive"]["folder_id"]
    with st.spinner("Loading Training Materials from Drive..."):
        text, files = load_knowledge_base_from_drive(folder_id)
        if text:
            st.session_state.kb_text = text
            st.session_state.file_names = files
        else:
            st.session_state.kb_text = ""
            st.session_state.file_names = []

# --- GRADING LOGIC ---
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
        1. Grade the agent on how they handled the ONE specific objection discussed.
        2. Give a STRICT Score (0-10).
           - 0-4: Weak, evasive, or robotic.
           - 5-8: Good logic, but wrong tone/phrasing.
           - 9-10: Perfect mastery of the objection.
        3. Identify specific strengths and weaknesses.
        4. Provide the exact "Magic Words" they should have used.
        
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
# 4. SIDEBAR
# ==========================================
with st.sidebar:
    st.title("🥋 Dojo Settings")
    st.success("🟢 System Ready")
    
    agent_name = st.text_input("Agent Name", placeholder="Enter your name")
    
    if st.button("📂 Reload Training Data"):
        folder_id = st.secrets["drive"]["folder_id"]
        with st.spinner("Connecting to Drive..."):
            text, files = load_knowledge_base_from_drive(folder_id)
            if text:
                st.session_state.kb_text = text
                st.session_state.file_names = files
                st.rerun()
            else:
                st.error("Could not load files.")

    if st.session_state.file_names:
        st.info(f"📚 {len(st.session_state.file_names)} Files Loaded")
    else:
        st.warning("⚠️ No Data Loaded")
    
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
        st.session_state.mode_2_chat = []
        st.rerun()

# ==========================================
# 5. MODE 1: ROLEPLAY AS REALTOR
# ==========================================
if mode == "Roleplay as Realtor":
    st.title("🏡 Roleplay as Realtor")
    st.markdown("You are the **Realtor**. The AI is a **Skeptical Buyer**.")
    st.info("🎯 FOCUS: Master ONE objection. The Coach will give you the exact scripts.")
    
    if not agent_name:
        st.warning("Enter Agent Name in sidebar.")
        st.stop()
        
    if not st.session_state.kb_text:
        st.info("👈 Please click 'Reload Training Data' in the sidebar if empty.")
        st.stop()

    context_safe = st.session_state.kb_text[:500000]
    
    # --- DEEP DIVE PERSONA ---
    system_persona = f"""
    You are a SKEPTICAL HOME BUYER.
    
    CONTEXT:
    {context_safe}
    
    INSTRUCTIONS:
    1. Start with ONE random objection from the text.
    2. STAY ON THIS EXACT OBJECTION for the entire session. Do NOT switch topics.
    3. If the agent gives a weak answer, push back hard. Say "I hear you, but..."
    4. If the agent asks "Can I share a perspective?", ALWAYS say "Yes, go ahead."
    5. If the agent handles it perfectly, acknowledge it: "Okay, fair point." BUT do not offer a new objection. Just let them know they won.
    6. VARIETY: Do not over-use the phrase "That makes sense." Use varied language like "I see," "Okay," "Fair enough," or "I understand."
    7. Always output JSON.
    """

    # --- START ---
    if not st.session_state.session_started:
        if st.button("🚀 Start Roleplay (Buyer Speaks First)", type="primary"):
            with st.spinner("Buyer is selecting an objection..."):
                try:
                    model = genai.GenerativeModel(
                        st.session_state.active_model,
                        system_instruction=system_persona
                    )
                    
                    seed_val = random.randint(1, 1000)
                    init_prompt = [f"""
                    Pick ONE random objection from the context.
                    CRITICAL: Do NOT pick the most common objections (like Price or Interest Rates). 
                    Scroll deep into the list and pick a specific, harder, or more obscure objection.
                    Random Seed: {seed_val}
                    
                    Output JSON: {{
                        "response_text": "The objection",
                        "strategy_tip": "The Magic Phrase to handle this specific objection is: '[Insert Script Here]'"
                    }}
                    """]
                    
                    response = model.generate_content(
                        init_prompt, 
                        generation_config={"response_mime_type": "application/json"}
                    )
                    
                    resp_json = json.loads(response.text)
                    opening_line = resp_json.get("response_text", "Hello.")
                    st.session_state.current_tip = resp_json.get("strategy_tip", "Use the 'Feel, Felt, Found' method.")
                    
                    st.session_state.chat_history.append({"role": "Buyer", "content": opening_line})
                    st.session_state.session_started = True
                    st.session_state.turn_count = 1
                    
                    # Generate Audio Check (Safety)
                    tts_audio = asyncio.run(text_to_speech(opening_line, voice_option))
                    if tts_audio:
                        play_audio_autoplay(tts_audio)
                    
                    st.rerun()
                    
                except Exception as e:
                    st.error(f"Error starting session: {e}")

    # --- MAIN LOOP ---
    else:
        # History
        for msg in st.session_state.chat_history:
            if msg["role"] == "Buyer":
                st.info(f"**Buyer:** {msg['content']}")
            else:
                st.write(f"**You:** {msg['content']}")

        # --- COACH'S CHEAT SHEET ---
        if st.session_state.current_tip:
            st.warning(f"🧠 **Coach's Cheat Sheet:** {st.session_state.current_tip}")

        audio_key = f"rec_{st.session_state.turn_count}"
        audio_input = st.audio_input("Record your response", key=audio_key)
        
        if st.button("🛑 Finish & Grade Session"):
            st.session_state.roleplay_active = False # Trigger grading
            st.rerun()

        if audio_input and st.session_state.roleplay_active:
             with st.spinner("The Buyer is thinking..."):
                
                audio_input.seek(0)
                audio_bytes = audio_input.read()
                
                if len(audio_bytes) < 100:
                    st.error("No audio captured.")
                    st.stop()
                
                # Format check
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
                   - IF they asked permission ("Can I...?", "May I...?"): Say "Yes, go ahead."
                   - IF they tried to handle the objection:
                     - BAD/WEAK? Push back ("I'm still not convinced...").
                     - GOOD? "Okay, I see your point."
                3. VARY YOUR VOCABULARY. Do not overuse "That makes sense."
                4. Output JSON:
                {{
                    "response_text": "Spoken response",
                    "strategy_tip": "CRITICAL: Do not just give advice. Give the Agent the EXACT SENTENCE (Magic Words) they should say next to win this argument."
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
                    
                    # Safe Audio Generation
                    tts_audio = asyncio.run(text_to_speech(ai_text, voice_option))
                    
                    st.session_state.chat_history.append({"role": "Agent", "content": "(Audio Input)"})
                    st.session_state.chat_history.append({"role": "Buyer", "content": ai_text})
                    st.session_state.turn_count += 1
                    
                    # Play if audio exists (fix crash)
                    if tts_audio:
                        play_audio_autoplay(tts_audio)
                    
                    st.rerun()
                    
                except Exception as e:
                    st.error(f"AI Error: {e}")

    # --- FINAL GRADING ---
    if not st.session_state.roleplay_active:
        st.divider()
        st.header("🏁 Session Complete")
        if "graded" not in st.session_state:
            with st.spinner("👨‍🏫 The Master Coach is grading your performance..."):
                score, feedback = calculate_final_grade_and_save(agent_name, st.session_state.kb_text)
                st.session_state.graded = True
                st.session_state.final_score = score
                st.session_state.final_feedback = feedback
        
        if "final_score" in st.session_state:
            score = st.session_state.final_score
            color = "green" if score >= 8 else "orange" if score >= 5 else "red"
            st.markdown(f"## Final Score: :{color}[{score}/10]")
            st.info(f"**Coach's Feedback:**\n\n{st.session_state.final_feedback}")
            st.success("✅ Results saved to Google Sheets.")

# ==========================================
# 6. MODE 2: ROLEPLAY AS HOMEBUYER
# ==========================================
elif mode == "Roleplay as Homebuyer":
    st.title("🎓 Roleplay as Homebuyer")
    st.markdown("You act as the **Buyer**. Throw objections!")
    st.info("The AI acts as the **Perfect Realtor**. Listen to how it handles your toughest questions.")
    
    if not st.session_state.kb_text:
        st.info("👈 Please click 'Reload Training Data' in the sidebar.")
        st.stop()

    context_safe_mc = st.session_state.kb_text[:500000]
    system_persona_mc = f"""
    You are the PERFECT REALTOR.
    CONTEXT: {context_safe_mc}
    Output JSON: {{ "user_transcript": "Transcript of user audio", "rebuttal_text": "...", "why_it_works": "..." }}
    """
    
    # --- DISPLAY CHAT HISTORY ---
    # We display past interactions here so they persist on refresh
    for i, msg in enumerate(st.session_state.mode_2_chat):
        with st.chat_message("user"):
            st.write(msg["user_text"])
        with st.chat_message("assistant"):
            st.write(msg["rebuttal"])
            with st.expander("Why this works"):
                st.write(msg["explanation"])
            
            # Show audio player. Auto-play ONLY if it's the very last message added.
            if msg.get("audio"):
                is_latest = (i == len(st.session_state.mode_2_chat) - 1)
                st.audio(msg["audio"], format="audio/mp3", autoplay=is_latest)

    # Dynamic Key for Mic
    audio_key_mc = f"mc_rec_{st.session_state.turn_count}"
    audio_input_mc = st.audio_input("State your objection", key=audio_key_mc)
    
    if audio_input_mc and st.session_state.kb_text:
        with st.spinner("Formulating perfect rebuttal..."):
            audio_input_mc.seek(0)
            audio_bytes_mc = audio_input_mc.read()
            
            # Robust Mime Type
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
                    ["Transcribe the audio and then handle this objection perfectly:", {"mime_type": mime_type_mc, "data": audio_bytes_mc}],
                    generation_config={"response_mime_type": "application/json"}
                )
                resp_json_mc = json.loads(response_mc.text)
                
                transcript = resp_json_mc.get("user_transcript", "(No transcript available)")
                rebuttal = resp_json_mc["rebuttal_text"]
                explanation = resp_json_mc["why_it_works"]
                
                # Generate Audio
                tts_audio_mc = asyncio.run(text_to_speech(rebuttal, voice_option))
                
                # Add to history
                st.session_state.mode_2_chat.append({
                    "user_text": transcript,
                    "rebuttal": rebuttal,
                    "explanation": explanation,
                    "audio": tts_audio_mc 
                })
                
                st.session_state.turn_count += 1
                st.rerun()

            except Exception as e:
                st.error(f"Error: {e}")
